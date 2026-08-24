# 고정 IP VPS 배포

토스증권 Open API 는 **허용 IP 목록**을 씁니다. 목록에 없는 곳에서 부르면
키가 맞아도 403 입니다. 그래서 이 서비스를 실제로 운영하려면 **나가는 IP 가
변하지 않는 곳**이 필요합니다.

Render 같은 PaaS 는 지역별 공유 IP 범위를 쓰고, 전용 IP 는 Pro 워크스페이스
전용에 IP 세트당 월 $100 입니다. 우리에게 필요한 것은 고정 IP 하나뿐이라
그 값은 과합니다. VPS 한 대가 월 $6–12 이고, 자동매매처럼 **상태를 들고
오래 도는 프로세스**에는 원래 그쪽이 더 맞습니다.

## 무엇을 고를까

서울 리전이 있는 곳을 고르세요. KRX 까지의 왕복이 짧을수록 지정가가 의도한
자리에 걸립니다.

| | 사양 | 월 요금 | 비고 |
|---|---|---|---|
| **Vultr 서울** | 1 vCPU / 2GB | $12 | 고정 IP 기본, 가입 간단 |
| **AWS Lightsail 서울** | 1 vCPU / 2GB | $12 | 고정 IP 는 별도 할당(무료) |
| **Oracle Cloud 춘천** | ARM 4 vCPU / 24GB | 무료 | 영구 무료지만 용량 확보가 어려울 때가 있음 |

1GB 로도 돌지만 2GB 를 권합니다. 백테스트가 메모리를 씁니다.

## 왜 도메인이 필요한가

세션 쿠키에 `__Host-` 접두사를 씁니다. 이 접두사는 브라우저가 `Secure` 를
요구하고, `Secure` 는 HTTPS 를 요구합니다. **HTTP 로 열면 브라우저가 쿠키를
조용히 버려서 로그인이 되지 않습니다.**

도메인 하나(연 1–2만원)와 아래 Caddy 설정이면 인증서는 자동입니다.

## 설치

```bash
# 1. 서버에서 (Ubuntu 24.04 기준)
sudo apt update && sudo apt install -y python3.12 python3.12-venv git curl
sudo useradd -m -s /bin/bash quant

# 2. 코드
sudo -u quant git clone https://github.com/<계정>/invest.git /home/quant/app
cd /home/quant/app
sudo -u quant python3.12 -m venv .venv
sudo -u quant .venv/bin/pip install -r requirements.txt

# 3. 비밀키 — 이 값으로 모든 사용자의 증권사 키를 암호화합니다.
#    잃어버리면 저장된 자격증명을 되살릴 수 없고 전원이 다시 등록해야 합니다.
#    서버 밖에도 따로 보관하세요.
sudo -u quant mkdir -p /home/quant/data
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
sudo -u quant tee /home/quant/app/.env >/dev/null <<'EOF'
QUANT_SECRET_KEY=<위에서 나온 값>
QUANT_USER_DATA=/home/quant/data/users
DB_PATH=/home/quant/data/quant_state.db
QUANT_ENV_FILE=/home/quant/data/.env
CORS_ORIGINS=https://<내 도메인>
LOG_FORMAT=json
EOF
sudo -u quant chmod 600 /home/quant/app/.env

# 4. 서비스 등록
sudo cp deploy/quant.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now quant

# 5. HTTPS
sudo apt install -y caddy
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo sed -i 's/YOUR_DOMAIN/<내 도메인>/' /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

## 증권사에 등록할 IP

```bash
curl -s https://api.ipify.org
```

여기서 나온 값을 토스증권 앱 → 설정 → Open API → **허용 IP 관리** 에
등록하세요. 화면의 「키 검증」이 실패하면 그 결과에도 같은 값이 뜹니다.

**내 컴퓨터 IP 가 아니라 이 서버의 IP 입니다.** 이 둘을 헷갈리는 것이
"키가 맞는데 403" 의 가장 흔한 원인입니다.

## 확인

```bash
systemctl status quant          # 살아 있는가
journalctl -u quant -f          # 무슨 일이 있는가
curl -s https://<도메인>/api/health | python3 -m json.tool
```

`started_at` 이 나옵니다. 코드를 고친 뒤에는 이 값이 **고친 시각보다 뒤**
여야 합니다 — 파이썬은 시작할 때 모듈을 읽으므로, 재시작 전에는 옛 코드가
계속 돕니다.

## 배포

```bash
cd /home/quant/app
sudo -u quant git pull
sudo -u quant .venv/bin/pip install -r requirements.txt
sudo systemctl restart quant
```

상태 DB 와 계정 DB 는 `/home/quant/data` 에 있어서 재배포와 무관합니다.

## 실거래로 넘어가기 전에

`DEPLOY.md` 의 체크리스트를 그대로 밟으세요. 특히:

- 하루 거래 한도(거래대금·건수·손실)를 **반드시** 채우세요. 전략이 정상일
  때를 가정하는 다른 한도와 달리, 이건 전략이 고장났을 때를 가정합니다.
- 최소 2주 이상 `dry_run` 으로 돌리고 그 결과가 같은 구간 백테스트와 비슷한지
  보세요. 크게 다르면 전략이 아니라 데이터나 환경 문제입니다.
- 증권사 API 키는 **출금 권한 없이** 발급하세요. 이 서비스는 주문만 필요합니다.
