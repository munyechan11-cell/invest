#!/usr/bin/env bash
# 도메인과 HTTPS 붙이기.
#
#   sudo bash deploy/domain.sh app.example.com
#
# 먼저 DNS 에 A 레코드가 이 서버를 가리키고 있어야 합니다. 인증서는 Let's
# Encrypt 가 도메인 소유를 확인한 뒤에 나오는데, 그 확인이 이 서버로 들어오는
# HTTP 요청으로 이뤄집니다 — DNS 가 아직 다른 곳을 가리키면 발급이 실패하고,
# 몇 번 실패하면 한 시간쯤 잠깁니다.
set -euo pipefail

DOMAIN=${1:-}
APP_DIR=/home/quant/app
ENV_FILE=$APP_DIR/.env

say() { printf '\n\033[1;33m▸ %s\033[0m\n' "$*"; }
ok()  { printf '  \033[0;32m✓\033[0m %s\n' "$*"; }
die() { printf '\n\033[0;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "sudo 로 실행하세요"
[ -n "$DOMAIN" ] || die "도메인을 인자로 주세요: sudo bash deploy/domain.sh app.example.com"
[ -f "$ENV_FILE" ] || die "$ENV_FILE 이 없습니다 — install.sh 를 먼저 실행하세요"

say "1/5  DNS 확인"
SERVER_IP=$(curl -fsS --max-time 8 https://api.ipify.org)
RESOLVED=$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1 || true)
if [ -z "$RESOLVED" ]; then
    die "$DOMAIN 이 아직 어디도 가리키지 않습니다.
     DNS 에 A 레코드를 추가하세요:  $DOMAIN  →  $SERVER_IP
     변경이 퍼지는 데 몇 분에서 한 시간쯤 걸립니다."
fi
if [ "$RESOLVED" != "$SERVER_IP" ]; then
    die "$DOMAIN 이 $RESOLVED 을 가리킵니다. 이 서버는 $SERVER_IP 입니다.
     A 레코드를 고치고 잠시 뒤 다시 실행하세요 — 지금 진행하면 인증서
     발급이 실패하고 한동안 재시도가 막힙니다."
fi
ok "$DOMAIN → $SERVER_IP"

say "2/5  Caddy 설치"
if command -v caddy >/dev/null 2>&1; then
    ok "이미 설치됨"
else
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl >/dev/null
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    apt-get update -qq
    apt-get install -y -qq caddy >/dev/null
    ok "설치 완료"
fi

say "3/5  설정"
sed "s/YOUR_DOMAIN/$DOMAIN/" "$APP_DIR/deploy/Caddyfile" > /etc/caddy/Caddyfile
install -d -o caddy -g caddy /var/log/caddy
ok "/etc/caddy/Caddyfile"

say "4/5  앱에 출처 알리기"
# CORS 는 이 주소에서만 허용합니다. 비워 두면 교차출처를 전부 막는데,
# 대시보드는 같은 출처에서 서빙되므로 그대로도 동작합니다 — 다만 나중에
# 다른 곳에서 이 API 를 부를 때를 위해 적어 둡니다.
if grep -q '^CORS_ORIGINS=' "$ENV_FILE"; then
    sed -i "s#^CORS_ORIGINS=.*#CORS_ORIGINS=https://$DOMAIN#" "$ENV_FILE"
else
    echo "CORS_ORIGINS=https://$DOMAIN" >> "$ENV_FILE"
fi
systemctl restart quant
sleep 3
systemctl is-active --quiet quant || die "앱이 뜨지 않았습니다 — journalctl -u quant -n 40"
ok "앱 재시작"

say "5/5  인증서 발급"
systemctl reload caddy 2>/dev/null || systemctl restart caddy
# Let's Encrypt 왕복에 몇 초 걸립니다. 여기서 기다리지 않으면 바로 아래
# 확인이 "아직 안 됐다" 를 잡아서, 실제로는 성공한 발급을 실패로 보고합니다.
for _ in $(seq 1 20); do
    sleep 3
    if curl -fsS --max-time 6 "https://$DOMAIN/api/health" >/dev/null 2>&1; then
        ok "HTTPS 동작"
        break
    fi
done

if curl -fsS --max-time 8 "https://$DOMAIN/api/health" >/dev/null 2>&1; then
    cat <<EOF

────────────────────────────────────────────────────────────────
 완료

   https://$DOMAIN

 이제 SSH 터널 없이 어디서든 접속됩니다 — 폰이든 노트북이든.
 인증서는 Caddy 가 자동으로 갱신합니다.

 다음에 하실 일:
   1. 위 주소로 들어가 계정을 만드세요(첫 가입자가 관리자입니다).
   2. 설정 → 증권사 연동 → 키 검증.
      허용 IP 에 $SERVER_IP 가 등록되어 있어야 합니다.
   3. 모의매매(dry_run)로 충분히 돌려 본 뒤에 실거래를 켜세요.

────────────────────────────────────────────────────────────────
EOF
else
    die "HTTPS 가 아직 응답하지 않습니다.
     확인:  journalctl -u caddy -n 40 --no-pager
     흔한 원인: DNS 가 아직 퍼지는 중, 또는 80/443 이 막혀 있음(ufw status)."
fi
