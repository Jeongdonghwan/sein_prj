import csv
import functools
import io
import math
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

import click
import pymysql
import requests
from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from config import Config

app = Flask(__name__)
app.config.from_object(Config)

INQUIRY_TYPES = ("CSO 품목 거래", "개원컨설팅", "병원마케팅", "영업 파트너 지원", "기타 문의")
STATUSES = ("신규", "상담중", "완료")
PER_PAGE = 20

PHONE_RE = re.compile(r"^[0-9-]{9,20}$")

# 문의 접수 레이트리밋: 같은 IP 1분 내 3회까지
RATE_LIMIT_MAX = 3
RATE_LIMIT_WINDOW = 60
# 로그인 잠금: 5회 실패 시 10분
MAX_LOGIN_FAILS = 5
LOCK_MINUTES = 10

# 프로세스 메모리 기반 저장소.
# gunicorn 워커를 2개 이상 두면 워커별로 따로 집계되므로,
# 정확한 제한이 필요해지면 Redis 등 외부 저장소로 교체할 것.
_inquiry_hits = defaultdict(deque)
_login_fails = {}
_mem_lock = threading.Lock()


# ---------------------------------------------------------------- DB

def get_db():
    if "db" not in g:
        g.db = pymysql.connect(
            host=app.config["DB_HOST"],
            port=app.config["DB_PORT"],
            user=app.config["DB_USER"],
            password=app.config["DB_PASSWORD"],
            database=app.config["DB_NAME"],
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------- 공통 유틸

def client_ip():
    # nginx 리버스프록시 뒤에서는 X-Forwarded-For 의 첫 IP 가 실제 클라이언트
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()[:45]
    return (request.remote_addr or "")[:45]


def is_rate_limited(ip):
    now = time.time()
    with _mem_lock:
        q = _inquiry_hits[ip]
        while q and now - q[0] > RATE_LIMIT_WINDOW:
            q.popleft()
        if len(q) >= RATE_LIMIT_MAX:
            return True
        q.append(now)
        return False


def login_locked_until(ip):
    with _mem_lock:
        rec = _login_fails.get(ip)
        if not rec:
            return None
        until = rec.get("locked_until")
        if until and until > datetime.now():
            return until
        if until:  # 잠금 만료 → 초기화
            _login_fails.pop(ip, None)
        return None


def record_login_fail(ip):
    with _mem_lock:
        rec = _login_fails.setdefault(ip, {"count": 0, "locked_until": None})
        rec["count"] += 1
        if rec["count"] >= MAX_LOGIN_FAILS:
            rec["locked_until"] = datetime.now() + timedelta(minutes=LOCK_MINUTES)
            rec["count"] = 0


def clear_login_fails(ip):
    with _mem_lock:
        _login_fails.pop(ip, None)


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(32)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


def check_csrf():
    token = request.form.get("_csrf", "")
    if not token or not secrets.compare_digest(token, session.get("_csrf", "")):
        abort(400, "CSRF 토큰이 유효하지 않습니다.")


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_id"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def notify_telegram(name, phone, inquiry_type, message):
    """문의 접수 텔레그램 알림. 토큰/챗ID 미설정 시 조용히 스킵."""
    token = app.config["TELEGRAM_BOT_TOKEN"]
    chat_id = app.config["TELEGRAM_CHAT_ID"]
    if not token or not chat_id:
        return
    text = (
        "🔔 새 상담문의가 접수되었습니다\n"
        f"성함: {name}\n"
        f"연락처: {phone}\n"
        f"유형: {inquiry_type or '-'}\n"
        f"내용: {(message or '-')[:500]}"
    )

    def _send():
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=5,
            )
        except Exception:
            app.logger.warning("텔레그램 알림 발송 실패", exc_info=True)

    # 응답을 지연시키지 않도록 백그라운드 발송
    threading.Thread(target=_send, daemon=True).start()


# ---------------------------------------------------------------- 사용자

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /admin\n", mimetype="text/plain")


@app.post("/api/inquiry")
def api_inquiry():
    data = request.get_json(silent=True) or {}

    # 허니팟: 봇이 채운 경우 저장하지 않고 성공한 척 응답
    if (data.get("website") or "").strip():
        return jsonify({"ok": True})

    ip = client_ip()
    if is_rate_limited(ip):
        return jsonify({"ok": False, "error": "요청이 너무 많습니다. 잠시 후 다시 시도해주세요."}), 429

    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip().replace(" ", "")
    if not name or not phone:
        return jsonify({"ok": False, "error": "성함과 연락처를 입력해주세요."}), 400
    if len(name) > 50:
        return jsonify({"ok": False, "error": "성함은 50자 이내로 입력해주세요."}), 400
    if not PHONE_RE.fullmatch(phone) or sum(c.isdigit() for c in phone) < 9:
        return jsonify({"ok": False, "error": "연락처 형식이 올바르지 않습니다. (숫자, 하이픈만 가능)"}), 400

    inquiry_type = (data.get("inquiry_type") or "").strip()
    if inquiry_type not in INQUIRY_TYPES:
        inquiry_type = None
    message = (data.get("message") or "").strip()[:5000] or None

    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inquiries (name, phone, inquiry_type, message, ip_address)"
            " VALUES (%s, %s, %s, %s, %s)",
            (name, phone, inquiry_type, message, ip),
        )
    db.commit()

    notify_telegram(name, phone, inquiry_type, message)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 어드민

@app.context_processor
def inject_admin_globals():
    ctx = {"STATUSES": STATUSES, "INQUIRY_TYPES": INQUIRY_TYPES}
    if session.get("admin_id") and request.path.startswith("/admin"):
        with get_db().cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM inquiries WHERE status = '신규'")
            ctx["new_count"] = cur.fetchone()["c"]
    return ctx


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_id"):
        return redirect(url_for("admin_list"))

    if request.method == "POST":
        check_csrf()
        ip = client_ip()
        locked = login_locked_until(ip)
        if locked:
            remain = max(1, math.ceil((locked - datetime.now()).total_seconds() / 60))
            flash(f"로그인 시도가 너무 많습니다. 약 {remain}분 후 다시 시도해주세요.", "error")
            return render_template("admin/login.html"), 429

        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        row = None
        if username:
            with get_db().cursor() as cur:
                cur.execute("SELECT * FROM admins WHERE username = %s", (username,))
                row = cur.fetchone()

        if row and check_password_hash(row["password_hash"], password):
            clear_login_fails(ip)
            session.clear()
            session["admin_id"] = row["id"]
            session["admin_username"] = row["username"]
            session.permanent = True
            csrf_token()  # 로그인 후 새 CSRF 토큰 발급
            next_url = request.args.get("next", "")
            if not next_url.startswith("/"):  # 오픈 리다이렉트 방지
                next_url = url_for("admin_list")
            return redirect(next_url)

        record_login_fail(ip)
        flash("아이디 또는 비밀번호가 올바르지 않습니다.", "error")

    return render_template("admin/login.html")


@app.get("/admin/logout")
@login_required
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


def build_filters(status, itype, q):
    where, params = [], []
    if status in STATUSES:
        where.append("status = %s")
        params.append(status)
    if itype in INQUIRY_TYPES:
        where.append("inquiry_type = %s")
        params.append(itype)
    if q:
        where.append("(name LIKE %s OR phone LIKE %s)")
        like = f"%{q}%"
        params.extend([like, like])
    return (" WHERE " + " AND ".join(where)) if where else "", params


@app.get("/admin")
@login_required
def admin_list():
    page = max(request.args.get("page", 1, type=int) or 1, 1)
    status = request.args.get("status", "")
    itype = request.args.get("type", "")
    q = (request.args.get("q") or "").strip()

    where_sql, params = build_filters(status, itype, q)
    db = get_db()
    with db.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS c FROM inquiries{where_sql}", params)
        total = cur.fetchone()["c"]
        cur.execute(
            f"SELECT id, name, phone, inquiry_type, status, created_at"
            f" FROM inquiries{where_sql} ORDER BY id DESC LIMIT %s OFFSET %s",
            params + [PER_PAGE, (page - 1) * PER_PAGE],
        )
        rows = cur.fetchall()

    pages = max(math.ceil(total / PER_PAGE), 1)
    return render_template(
        "admin/list.html",
        rows=rows, total=total, page=page, pages=pages,
        status=status, itype=itype, q=q,
    )


def get_inquiry_or_404(inquiry_id):
    with get_db().cursor() as cur:
        cur.execute("SELECT * FROM inquiries WHERE id = %s", (inquiry_id,))
        row = cur.fetchone()
    if row is None:
        abort(404)
    return row


@app.get("/admin/inquiry/<int:inquiry_id>")
@login_required
def admin_detail(inquiry_id):
    return render_template("admin/detail.html", iq=get_inquiry_or_404(inquiry_id))


@app.post("/admin/inquiry/<int:inquiry_id>/status")
@login_required
def admin_status(inquiry_id):
    check_csrf()
    get_inquiry_or_404(inquiry_id)
    new_status = request.form.get("status", "")
    if new_status not in STATUSES:
        abort(400, "잘못된 상태값입니다.")
    db = get_db()
    with db.cursor() as cur:
        cur.execute("UPDATE inquiries SET status = %s WHERE id = %s", (new_status, inquiry_id))
    db.commit()
    flash(f"상태가 '{new_status}' 로 변경되었습니다.", "success")
    return redirect(url_for("admin_detail", inquiry_id=inquiry_id))


@app.post("/admin/inquiry/<int:inquiry_id>/memo")
@login_required
def admin_memo(inquiry_id):
    check_csrf()
    get_inquiry_or_404(inquiry_id)
    memo = (request.form.get("admin_memo") or "").strip() or None
    db = get_db()
    with db.cursor() as cur:
        cur.execute("UPDATE inquiries SET admin_memo = %s WHERE id = %s", (memo, inquiry_id))
    db.commit()
    flash("메모가 저장되었습니다.", "success")
    return redirect(url_for("admin_detail", inquiry_id=inquiry_id))


@app.post("/admin/inquiry/<int:inquiry_id>/delete")
@login_required
def admin_delete(inquiry_id):
    check_csrf()
    get_inquiry_or_404(inquiry_id)
    db = get_db()
    with db.cursor() as cur:
        cur.execute("DELETE FROM inquiries WHERE id = %s", (inquiry_id,))
    db.commit()
    flash(f"문의 #{inquiry_id} 가 삭제되었습니다.", "success")
    return redirect(url_for("admin_list"))


@app.get("/admin/export")
@login_required
def admin_export():
    status = request.args.get("status", "")
    itype = request.args.get("type", "")
    q = (request.args.get("q") or "").strip()
    where_sql, params = build_filters(status, itype, q)

    with get_db().cursor() as cur:
        cur.execute(
            f"SELECT id, name, phone, inquiry_type, message, status, admin_memo,"
            f" ip_address, created_at, updated_at FROM inquiries{where_sql} ORDER BY id DESC",
            params,
        )
        rows = cur.fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ID", "성함", "연락처", "문의유형", "문의내용", "상태",
                     "관리자메모", "IP", "접수일시", "수정일시"])
    for r in rows:
        writer.writerow([
            r["id"], r["name"], r["phone"], r["inquiry_type"] or "",
            r["message"] or "", r["status"], r["admin_memo"] or "",
            r["ip_address"] or "",
            r["created_at"].strftime("%Y-%m-%d %H:%M:%S") if r["created_at"] else "",
            r["updated_at"].strftime("%Y-%m-%d %H:%M:%S") if r["updated_at"] else "",
        ])

    filename = f"inquiries_{datetime.now():%Y%m%d_%H%M%S}.csv"
    # UTF-8 BOM: 엑셀에서 한글 깨짐 방지
    return Response(
        "﻿" + buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------- CLI

@app.cli.command("create-admin")
@click.argument("username")
@click.argument("password")
def create_admin_command(username, password):
    """관리자 계정 생성 (이미 존재하면 비밀번호 재설정)."""
    pw_hash = generate_password_hash(password)
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT id FROM admins WHERE username = %s", (username,))
        if cur.fetchone():
            cur.execute("UPDATE admins SET password_hash = %s WHERE username = %s",
                        (pw_hash, username))
            click.echo(f"기존 관리자 '{username}' 의 비밀번호를 재설정했습니다.")
        else:
            cur.execute("INSERT INTO admins (username, password_hash) VALUES (%s, %s)",
                        (username, pw_hash))
            click.echo(f"관리자 '{username}' 를 생성했습니다.")
    db.commit()


if __name__ == "__main__":
    app.run(debug=True)
