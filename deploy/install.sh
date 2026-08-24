#!/usr/bin/env bash
# 자동매매 서비스 설치 — Ubuntu 24.04.
#
# 여러 번 실행해도 안전합니다. 이미 되어 있는 단계는 건너뛰고, 비밀키는
# 한 번 만들어진 뒤로는 절대 다시 만들지 않습니다 — 그 값을 갈아엎으면
# 저장된 증권사 키를 전부 못 읽게 됩니다.
#
#   sudo bash deploy/install.sh
set -euo pipefail

APP_USER=quant
APP_DIR=/home/$APP_USER/app
DATA_DIR=/home/$APP_USER/data
REPO=${REPO:-https://github.com/munyechan11-cell/invest.git}

say()  { printf '\n\033[1;33m▸ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[0;32m✓\033[0m %s\n' "$*"; }
die()  { printf '\n\033[0;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "sudo 로 실행하세요: sudo bash deploy/install.sh"

say "1/7  시스템 패키지"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl ufw >/dev/null
ok "python $(python3 --version | cut -d' ' -f2)"

say "2/7  서비스 계정"
if id "$APP_USER" >/dev/null 2>&1; then
    ok "$APP_USER 이미 있음"
else
    useradd -m -s /bin/bash "$APP_USER"
    ok "$APP_USER 생성"
fi
install -d -o "$APP_USER" -g "$APP_USER" -m 700 "$DATA_DIR"

say "3/7  코드"
if [ -d "$APP_DIR/.git" ]; then
    sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only
    ok "최신으로 갱신"
else
    sudo -u "$APP_USER" git clone --depth 50 "$REPO" "$APP_DIR"
    ok "$REPO 에서 받음"
fi

say "4/7  파이썬 환경"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
    sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
fi
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
ok "의존성 설치 완료"

say "5/7  설정"
ENV_FILE=$APP_DIR/.env
if [ -f "$ENV_FILE" ] && grep -q '^QUANT_SECRET_KEY=..' "$ENV_FILE"; then
    ok "기존 설정 유지 — 비밀키는 건드리지 않습니다"
else
    # 이 값으로 모든 사용자의 증권사 키를 암호화합니다. 다시 만들면 지금까지
    # 저장된 자격증명을 전부 못 읽게 되므로, 있으면 절대 덮어쓰지 않습니다.
    SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
    cat > "$ENV_FILE" <<EOF
QUANT_SECRET_KEY=$SECRET
QUANT_USER_DATA=$DATA_DIR/users
DB_PATH=$DATA_DIR/quant_state.db
QUANT_ENV_FILE=$DATA_DIR/.env
LOG_FORMAT=json
EOF
    chown "$APP_USER:$APP_USER" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    ok "새 비밀키 생성 — 아래에서 백업하세요"
fi

say "6/7  서비스 등록"
sed -e "s#/home/quant/app#$APP_DIR#g" -e "s#/home/quant/data#$DATA_DIR#g" \
    "$APP_DIR/deploy/quant.service" > /etc/systemd/system/quant.service
systemctl daemon-reload
systemctl enable quant >/dev/null 2>&1
systemctl restart quant
sleep 4
systemctl is-active --quiet quant \
    && ok "서비스 실행 중" \
    || die "서비스가 뜨지 않았습니다 — journalctl -u quant -n 40 으로 확인하세요"

say "7/7  방화벽"
# 앱은 127.0.0.1 에만 붙습니다. 바깥에서 직접 닿지 못하고, 나중에 붙일
# Caddy(HTTPS)나 SSH 터널을 통해서만 들어옵니다 — 인증서 없이 열어 두면
# 로그인 비밀번호가 평문으로 오갑니다.
ufw allow 22/tcp   >/dev/null
ufw allow 80/tcp   >/dev/null
ufw allow 443/tcp  >/dev/null
ufw --force enable >/dev/null
ok "22 / 80 / 443 만 열림 (앱 포트 8000 은 바깥에 닫힘)"

IP=$(curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || echo "확인 실패")

cat <<EOF

────────────────────────────────────────────────────────────────
 설치 완료
────────────────────────────────────────────────────────────────

 이 서버의 공인 IP:  $IP

   이 값을 증권사에 등록하세요.
   토스증권 앱 → 설정 → Open API → 허용 IP 관리
   (내 컴퓨터 IP 가 아니라 이 서버의 IP 입니다.)

 접속 방법 — 도메인이 아직 없다면 SSH 터널로:

   내 컴퓨터에서:
     ssh -L 8000:127.0.0.1:8000 $(logname 2>/dev/null || echo linuxuser)@$IP
   그 창을 켜 둔 채로 브라우저에서:
     http://127.0.0.1:8000

   localhost 로 접속하므로 비밀번호가 네트워크에 노출되지 않습니다.
   도메인을 붙이면 이 터널 없이 어디서든 쓸 수 있습니다 (deploy/README.md).

 비밀키 백업 — 지금 하세요:

   sudo cat $ENV_FILE | grep QUANT_SECRET_KEY

   이 값을 잃으면 저장된 증권사 키를 되살릴 수 없고, 모든 사용자가
   처음부터 다시 등록해야 합니다. 서버 밖에 보관하세요.

 상태 확인:
   systemctl status quant
   journalctl -u quant -f

────────────────────────────────────────────────────────────────
EOF
