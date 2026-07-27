# 세인약품 홈페이지 + 문의 어드민

CSO 전문기업 세인약품 기업 홈페이지(랜딩) + 상담문의 접수/관리 어드민.

- **스택**: Flask + Jinja2 SSR + MariaDB(PyMySQL), 순수 HTML/CSS/JS (프론트 빌드도구 없음)
- **배포**: Cafe24 가상서버 — gunicorn + nginx 리버스프록시

## 구조

```
sein-pharma/
├── app.py             # 라우트/비즈니스 로직 전체
├── config.py          # 환경변수(.env) 기반 설정
├── requirements.txt
├── schema.sql         # inquiries / admins 테이블
├── templates/
│   ├── index.html     # 랜딩페이지
│   └── admin/         # 어드민 (login / list / detail)
└── static/
    └── admin.css      # 어드민 전용 CSS
```

## 주요 기능

| 구분 | 내용 |
|---|---|
| `POST /api/inquiry` | 문의 접수. 이름/연락처 필수, 연락처 형식 검증, IP당 1분 3회 레이트리밋(429), 허니팟 스팸 차단 |
| `/admin` | 세션 로그인, 문의 목록(20개 페이지네이션, 상태/유형 필터, 이름·연락처 검색, 신규 뱃지) |
| `/admin/inquiry/<id>` | 상세, 상태 변경(수동), 관리자 메모, 삭제 |
| `/admin/export` | 현재 필터 적용 CSV 다운로드 (UTF-8 BOM, 엑셀 호환) |
| 보안 | login_required, 세션 CSRF 토큰, SQL 파라미터 바인딩, HttpOnly/SameSite=Lax 쿠키, 로그인 5회 실패 시 10분 잠금, robots.txt로 /admin 차단 |
| 알림 | 문의 접수 시 텔레그램 봇 알림 (미설정 시 자동 스킵) |

---

## 1. 로컬 실행

### 1-1. 사전 준비
- Python 3.10+
- MariaDB (또는 MySQL) 실행 중

### 1-2. DB 생성

```sql
-- root 로 접속 후
CREATE DATABASE sein_pharma DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'sein'@'localhost' IDENTIFIED BY '비밀번호';
GRANT ALL PRIVILEGES ON sein_pharma.* TO 'sein'@'localhost';
FLUSH PRIVILEGES;
```

```bash
mysql -u sein -p sein_pharma < schema.sql
```

### 1-3. 앱 설정 및 실행

```bash
cd sein-pharma
python -m venv venv
venv\Scripts\activate        # Windows  (리눅스: source venv/bin/activate)
pip install -r requirements.txt

cp .env.example .env         # .env 열어서 DB 접속정보/SECRET_KEY 입력
python -c "import secrets; print(secrets.token_hex(32))"   # SECRET_KEY 생성용

flask create-admin admin 비밀번호    # 관리자 계정 생성
flask run                            # http://127.0.0.1:5000
```

- 랜딩: http://127.0.0.1:5000/
- 어드민: http://127.0.0.1:5000/admin

> **주의**: 레이트리밋/로그인 잠금은 프로세스 메모리 기반입니다. gunicorn 워커를 2개 이상
> 두면 워커별로 따로 집계되므로, 엄격한 제한이 필요하면 Redis 기반으로 교체하세요.

---

## 2. Cafe24 가상서버 배포 (Ubuntu 기준)

### 2-0. 배포 전 서버 여유 점검 (다른 사이트가 이미 여러 개 떠 있는 경우)

```bash
# 서버에 스크립트 업로드 후
bash deploy/server_check.sh
```

메모리 available 300MB 이상, 디스크 1GB 이상, load average < CPU 코어 수면 배포 가능.
이 앱의 예상 소요 자원: gunicorn 1워커 기준 RAM 약 60~90MB + 디스크 약 50MB (기존 MariaDB 공유 시 DB 추가 부담 미미).

### 2-1. 패키지 설치

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip nginx mariadb-server
sudo mysql_secure_installation
```

### 2-2. DB 생성

위 **1-2** 와 동일하게 `sudo mysql` 로 접속해 DB/계정 생성 후 스키마 적용.

### 2-3. 앱 배치

```bash
sudo mkdir -p /var/www/sein-pharma
# 코드 업로드 (git clone 또는 scp/sftp)
cd /var/www/sein-pharma
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env     # 운영 DB정보 / SECRET_KEY / SESSION_COOKIE_SECURE=1
./venv/bin/flask create-admin admin '강력한비밀번호'
```

### 2-4. gunicorn systemd 서비스

`/etc/systemd/system/sein-pharma.service`:

```ini
[Unit]
Description=Sein Pharma Flask (gunicorn)
After=network.target mariadb.service

[Service]
User=www-data
Group=www-data
WorkingDirectory=/var/www/sein-pharma
# 포트 8003: 이 서버는 8000(glaos)/8001/8002(greatcar)/8080(ilioom)/8500(angimo) 등이 이미 사용 중
# 레이트리밋/로그인잠금이 메모리 기반이므로 workers=1 권장 (트래픽 증가 시 Redis 도입 후 확장)
ExecStart=/var/www/sein-pharma/venv/bin/gunicorn -w 1 --threads 4 -b 127.0.0.1:8003 app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo chown -R www-data:www-data /var/www/sein-pharma
sudo systemctl daemon-reload
sudo systemctl enable --now sein-pharma
sudo systemctl status sein-pharma
```

### 2-5. nginx 리버스프록시

`/etc/nginx/sites-available/sein-pharma`:

```nginx
server {
    listen 80;
    server_name example.com www.example.com;   # 실제 도메인으로 교체

    client_max_body_size 2m;

    location /static/ {
        alias /var/www/sein-pharma/static/;
        expires 7d;
    }

    location / {
        proxy_pass http://127.0.0.1:8003;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/sein-pharma /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

### 2-6. HTTPS (Let's Encrypt)

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d example.com -d www.example.com
```

발급 후 `.env` 에서 `SESSION_COOKIE_SECURE=1` 로 변경하고 `sudo systemctl restart sein-pharma`.

### 2-7. 텔레그램 알림 설정 (선택)

1. 텔레그램에서 **@BotFather** 에게 `/newbot` → 봇 토큰 발급
2. 봇과 대화 시작(아무 메시지 전송) 후 `https://api.telegram.org/bot<토큰>/getUpdates` 에서 `chat.id` 확인
3. `.env` 의 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 에 입력 후 서비스 재시작

미설정 시 알림은 조용히 스킵되며 문의 접수는 정상 동작합니다.

---

## 3. 운영 체크리스트

- [ ] `SECRET_KEY` 를 무작위 값으로 교체했는가
- [ ] `SESSION_COOKIE_SECURE=1` (HTTPS 적용 후)
- [ ] 관리자 비밀번호가 충분히 강력한가
- [ ] `index.html` 의 주소/전화번호/사업자번호 플레이스홀더(○○) 교체
- [ ] DB 정기 백업 (`mysqldump -u sein -p sein_pharma > backup.sql` cron 등록)
