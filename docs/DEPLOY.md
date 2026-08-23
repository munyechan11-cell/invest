# 실제 운용 배포

이 문서는 **진짜 도메인에서 24시간 돌리는 방법**입니다. 재생용 데모 페이지
(`docs/deliberation.html`)와는 다릅니다.

---

## 먼저 정할 것 — 어디서 돌릴 것인가

| | 내 맥 | 클라우드(Render 등) |
|---|---|---|
| 비용 | 0원 | 월 $7~ |
| 주소 | `localhost:8000` (외부 접속 불가) | `*.onrender.com` 또는 내 도메인 |
| 맥을 꺼도 | 멈춤 | 계속 돎 |
| 증권사 IP 등록 | 집 IP 하나면 끝 | 호스팅 IP가 바뀔 수 있음 |
| KRX 지연 | 국내 | 싱가포르 리전 기준 +40~60ms |

**한국 주식만 한다면 내 맥이 낫습니다.** 한국투자증권 API는 IP를 등록해야 하고,
클라우드는 재배포마다 IP가 바뀔 수 있습니다. 24시간 돌아야 하는 코인이거나
맥을 꺼도 굴러야 한다면 클라우드입니다.

---

## A. 클라우드 (Render) — 진짜 도메인

### 1. 배포

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/munyechan11-cell/invest)

위 버튼이 `render.yaml`을 읽어 서비스 하나를 만듭니다. Render 계정은 직접
만드셔야 합니다.

**플랜은 Starter 이상으로 두세요.** Free는 유휴 시 슬립되는데, 자동매매 봇이
자다가 깨면 그동안 장이 움직인 걸 못 봅니다.

### 2. 환경변수

`QUANT_API_TOKEN`은 Render가 자동 생성합니다. **이 값이 없으면 서버가 아예 뜨지
않습니다** — 매수·매도·전량청산 엔드포인트를 인증 없이 인터넷에 여는 걸 코드가
막습니다. 값은 Render 대시보드 → Environment에서 확인하세요.

직접 넣어야 하는 것:

| 변수 | 언제 필요 |
|---|---|
| `GOOGLE_API_KEY` | AI 데스크를 켤 때 (유료 전환하신 그 키) |
| `CORS_ORIGINS` | 배포 후 받은 실제 URL. 예: `https://quant-desk-ab12.onrender.com` |
| `KIS_APP_KEY` / `KIS_APP_SECRET` / `KIS_ACCOUNT_NO` | 한국투자증권 실매매 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 체결 알림 |

LLM 키가 하나도 없어도 돕니다. 데스크만 꺼진 채로 규칙 기반 알파(수급·추세·돌파)가
동작하고, 그쪽은 **비용이 0원**입니다.

### 3. 접속

```
https://<서비스이름>.onrender.com/?token=<QUANT_API_TOKEN>
```

내 도메인을 붙이려면 Render → Settings → Custom Domain에서 CNAME을 겁니다.

---

## B. 내 맥에서 (무료, 한국 주식 권장)

```bash
cd ~/Downloads/invest-main
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m quant serve configs/demo.yaml
```

`http://127.0.0.1:8000` 으로 열립니다. 루프백이라 토큰 없이도 뜨고, 다른 기기에서는
접속되지 않습니다.

맥이 로그인된 동안 계속 돌게 하려면 `launchd`에 등록합니다.
`~/Library/LaunchAgents/com.quant.desk.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.quant.desk</string>
  <key>ProgramArguments</key><array>
    <string>/Users/사용자명/Downloads/invest-main/.venv/bin/python</string>
    <string>-m</string><string>quant</string><string>serve</string>
    <string>configs/demo.yaml</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/사용자명/Downloads/invest-main</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/quant.log</string>
  <key>StandardErrorPath</key><string>/tmp/quant.err</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.quant.desk.plist
launchctl list | grep quant          # 떴는지 확인
tail -f /tmp/quant.err               # 문제 있으면 여기 찍힙니다
```

경로의 `사용자명`을 실제 값으로 바꾸세요. 끄려면 `launchctl unload` 입니다.

외부에서 접속하고 싶다면 **포트포워딩 대신 Cloudflare Tunnel**을 쓰세요. 공유기에
구멍을 뚫으면 이 API가 그대로 인터넷에 노출됩니다.

```bash
brew install cloudflared
QUANT_API_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))") \
  .venv/bin/python -m quant serve configs/demo.yaml --host 127.0.0.1 &
cloudflared tunnel --url http://127.0.0.1:8000
```

---

## 실매매로 올리기

배포한다고 실제 주문이 나가지 않습니다. **사람이 따로 결정해야 합니다.**

1. 설정 파일에서 `mode: live`
2. 같은 파일에서 `broker.live_trading_confirmed: true`
3. 대시보드 → 설정에서 **하루 거래 한도**를 반드시 채우기
   (거래대금 · 주문 건수 · 손실 한도 — 셋 다)
4. 대시보드 → `트레이더 시작`

한도를 비워두면 버그 하나가 하루에 계좌를 다 돌릴 수 있습니다. 한도는
브로커 계층에 있어서 전략이 뭘 하든 그 아래에서 막습니다. 청산 주문은
어떤 한도로도 막지 않습니다 — 손실 포지션에 갇히게 만드는 안전장치는
안전장치가 아닙니다.

---

## 배포 전 점검

```bash
.venv/bin/python -m pytest tests/ -q          # 215개 통과해야 합니다
.venv/bin/python -m quant validate configs/demo.yaml
```

## 안전장치 요약

- `QUANT_API_TOKEN` 없이 `0.0.0.0` 바인딩 → **서버가 뜨지 않음**
- 토큰이 설정되면 `CORS_ORIGINS`에 적은 출처만 허용 (기본 거부)
- 상태 변경 엔드포인트 전부 `Bearer` 토큰 필요
- `.env`는 0600으로 저장되고 git에 올라가지 않음 (`.gitignore`)
- 실매매는 설정 두 곳을 직접 바꿔야 켜짐
