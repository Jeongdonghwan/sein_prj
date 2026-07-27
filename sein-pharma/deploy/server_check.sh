#!/usr/bin/env bash
# ============================================================
# 세인약품 배포 전 서버 여유 자원 점검 스크립트
# 사용법: Cafe24 가상서버(Ubuntu)에 SSH 접속 후
#   bash server_check.sh
# ============================================================

echo "############################################################"
echo "# 세인약품 배포 전 서버 점검  ($(date '+%Y-%m-%d %H:%M'))"
echo "############################################################"

echo ""
echo "=== 1. CPU 코어 수 / 부하 (load average가 코어 수보다 낮아야 여유) ==="
echo "코어 수: $(nproc)"
uptime

echo ""
echo "=== 2. 메모리 (available 이 300MB 이상이면 배포 가능) ==="
free -h

echo ""
echo "=== 3. 디스크 (1GB 이상 여유 필요, 이미지+로그 포함 앱 용량 ~50MB) ==="
df -h / /srv /var 2>/dev/null | sort -u

echo ""
echo "=== 4. 메모리 상위 프로세스 TOP 10 ==="
ps aux --sort=-%mem | awk 'NR<=11{printf "%-10s %6s %6s  %s\n", $1, $3"%", $4"%", $11}'

echo ""
echo "=== 5. 사용 중인 웹 포트 (8000 이 비어 있어야 기본 설정 그대로 사용 가능) ==="
ss -tlnp 2>/dev/null | grep -E ':(80|443|3000|500[0-9]|800[0-9]|8080|9000)\s' || echo "(해당 포트 리스너 없음)"

echo ""
echo "=== 6. nginx 가상호스트 (현재 운영 중인 사이트 목록) ==="
ls -1 /etc/nginx/sites-enabled/ 2>/dev/null || echo "(sites-enabled 없음 — conf.d 방식일 수 있음)"
echo "server_name 선언 수: $(nginx -T 2>/dev/null | grep -c 'server_name' || echo '확인 불가')"

echo ""
echo "=== 7. 실행 중인 웹앱 서비스 (gunicorn/uwsgi/node/php 등) ==="
systemctl list-units --type=service --state=running 2>/dev/null \
  | grep -Ei 'gunicorn|uwsgi|node|pm2|php|flask|django|apache|nginx' || echo "(감지된 웹앱 서비스 없음)"

echo ""
echo "=== 8. MariaDB/MySQL 상태 및 메모리 ==="
if pgrep -x mariadbd >/dev/null || pgrep -x mysqld >/dev/null; then
  ps -o rss=,comm= -p "$(pgrep -x mariadbd || pgrep -x mysqld)" | awk '{printf "%s: %.0f MB\n", $2, $1/1024}'
else
  echo "MariaDB/MySQL 미실행 — 설치/기동 필요 (sudo apt install mariadb-server)"
fi

echo ""
echo "############################################################"
echo "# 판단 기준 (세인약품 앱 예상 소요 자원)"
echo "#  - gunicorn 1워커+4스레드: 약 60~90MB RAM"
echo "#  - nginx 가상호스트 추가: 수 MB 수준"
echo "#  - DB: 기존 MariaDB 공유 시 추가 부담 미미"
echo "#  - 디스크: 앱+이미지 약 50MB + 로그"
echo "# → 메모리 available 300MB↑, 디스크 1GB↑, load < 코어수 면 OK"
echo "############################################################"
