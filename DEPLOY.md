# 배포 가이드 (Deployment)

실거래로 넘어가는 경로는 **의도적으로 번거롭게** 만들어져 있습니다.
배포 파이프라인이 오타 하나로 실계좌를 건드리는 일이 없어야 하기 때문입니다.

```
backtest  →  dryrun (실시간 시세 + 가상 체결)  →  live (실거래)
```

각 단계는 앞 단계를 통과해야 의미가 있습니다. 특히 `dryrun`은 백테스트와
체결·수수료·거절 로직을 **같은 코드**로 공유하므로, 두 결과가 크게 다르면
전략이 아니라 데이터나 환경에 문제가 있다는 신호입니다.

---

## 1. 로컬

```bash
pip install -r requirements.txt
python -m quant backtest configs/demo.yaml      # API 키 없이 동작
python -m quant serve   configs/demo.yaml       # http://127.0.0.1:8000
```

---

## 2. Docker

```bash
cp .env.example .env          # 필요한 키만 채우기
docker compose up --build
```

- `dashboard` — 대시보드 + 제어 API (`:8000`)
- `trader` — `STRATEGY_CONFIG` 전략을 **dryrun**으로 실행
- 상태는 `quant-data` 볼륨의 SQLite 한 파일. 재시작해도 포지션이 복원됩니다.

실거래로 바꾸려면 `docker-compose.yml`의 `trader.command`를 `dryrun` → `live`로
바꾸고, 해당 전략 config에서 `mode: live`와
`broker.live_trading_confirmed: true`를 모두 설정해야 합니다.

---

## 3. 어디에 올릴까 — 증권사 IP 제한이 결정합니다

**토스증권 Open API 는 허용 IP 목록을 씁니다.** 목록에 없는 곳에서 부르면
키가 맞아도 403 입니다. 그래서 배포 위치를 고를 때 첫 질문은 성능이나 가격이
아니라 **나가는 IP 가 고정인가** 입니다.

| | 나가는 IP | 이 서비스에 |
|---|---|---|
| VPS (Vultr·Lightsail·Oracle) | 고정 | ✅ 이 서비스가 쓰는 방식 — `deploy/README.md` |
| PaaS (Render 등) | 지역 공유 범위 | ❌ 토스가 거부합니다 |

한국투자증권(KIS)은 IP 제한이 없어서 어디서든 됩니다. 토스를 쓰실 거면
**고정 IP VPS** 외에 답이 없습니다 — 서울 리전 2GB 가 월 $12 이고, 자동매매처럼
상태를 들고 오래 도는 프로세스에는 원래 그쪽이 맞습니다.

> **Render 블루프린트(`render.yaml`)는 없앴습니다.** 토스에 못 붙는 곳인데
> 저장소에 남겨 두니 `main` 에 푸시할 때마다 **쓰지도 않는 곳이 자동 배포**
> 되고 있었습니다. 배포처가 둘로 보이는 것 자체가 "어느 쪽이 진짜 도는
> 봇인가" 를 묻게 만들고, 실거래에서 그 혼동은 비쌉니다.

설치 절차는 [`deploy/README.md`](deploy/README.md) 에 있습니다.

---

## 4. 실거래 체크리스트

실거래 전에 아래가 **전부** 사실이어야 합니다. 하나라도 아니면 아직 이릅니다.

- [ ] `quant walkforward`가 PASS이고 walk-forward efficiency ≥ 0.5
- [ ] 최소 2주 이상 `dryrun`을 돌렸고, 그 결과가 같은 구간 백테스트와 유사
- [ ] `broker.max_order_notional`이 "이 금액을 통째로 잃어도 감당 가능한" 수준
- [ ] `risk.models`에 `max_dd_portfolio` 킬스위치가 있고 한도가 현실적
- [ ] `notify`로 텔레그램 알림이 실제로 도착하는지 확인함
- [ ] 거래소/증권사 API 키가 **출금 권한 없이** 발급됨
- [ ] `QUANT_SECRET_KEY`가 설정되어 있고 **서버 밖에도** 백업되어 있음
      (이 값이 없으면 저장된 증권사 키를 되살릴 수 없습니다)
- [ ] 로그인 없이 열리는 것은 `/api/health` 하나뿐임을 확인함 (나머지는 전부 401)
- [ ] 프로세스가 죽었을 때 무슨 일이 일어나는지 알고 있음 (포지션은 그대로 남습니다)

```bash
python -m quant live configs/live_crypto.yaml
# 전략 이름을 직접 입력해야 시작됩니다.
```

---

## 5. 올린 뒤 다시 올리기 (siftai.kr)

서비스는 `quant` 계정이 돌리지만 **그 계정으로는 로그인할 수 없습니다** —
`install.sh` 가 비밀번호 없는 시스템 계정으로 만들기 때문입니다(그게 맞습니다).
들어가는 문은 `linuxuser` 이고, 거기서 `sudo -u quant` 로 건너갑니다.

```bash
ssh linuxuser@siftai.kr 'sudo -u quant git -C /home/quant/app pull --ff-only \
  && sudo -u quant /home/quant/app/.venv/bin/pip install -q -r /home/quant/app/requirements.txt \
  && sudo systemctl restart quant'
```

확인은 `curl -s https://siftai.kr/api/health` 로 합니다 — `trader_running` 이
`false` 이고 `uptime_s` 가 방금 값이면 새 코드가 올라간 것입니다.

⚠️ **장이 열려 있는 동안 재시작하지 마세요.** `SIGTERM` 을 받으면 현재
사이클을 끝내고 멈추지만, 그 사이 시세는 계속 움직입니다. 봇이 돌고 있으면
먼저 `POST /api/trader/stop` 으로 세우고, `trader_running: false` 를 확인한
뒤에 올리세요.

---

## 6. 운영 중

| 확인 | 방법 |
|---|---|
| 살아 있는가 | `GET /api/health` → `trader_running` |
| 포지션이 맞는가 | `POST /api/trader/sync` → `drift`가 비어 있어야 정상 |
| 무슨 일이 있었나 | `GET /api/events?limit=200` 또는 대시보드 이벤트 피드 |
| 성과 | `GET /api/equity`, `GET /api/trades` |
| 중지 | `POST /api/trader/stop` (현재 사이클까지 마치고 정지, **포지션은 청산하지 않음**) |

제어 API는 전부 **세션 쿠키**로 보호됩니다. 한때 공유 토큰(`QUANT_API_TOKEN`)
하나가 그 자리를 대신했지만, 그 토큰을 가진 요청은 **어느 계정이 보낸 것인지
구분되지 않아** 남의 증권사 키로 주문을 낼 수 있었습니다. 다중 사용자
서비스에서는 성립할 수 없는 설계라 통째로 없앴습니다.

`SIGTERM`(컨테이너 재배포)을 받으면 현재 사이클을 끝내고 상태를 저장한 뒤
종료합니다. 열린 포지션은 그대로 두므로, 재시작 시 SQLite에서 복원하고
거래소와 대조(reconcile)합니다.

---

## 7. 하지 말아야 할 것

- 백테스트 결과만 보고 실거래로 직행 — 그래서 walk-forward가 있습니다
- `costs.preset: zero_cost`로 낸 성적을 실현 가능한 수익으로 착각
- LLM council을 백테스트에서 켜 놓고 나온 숫자를 믿기 — 모델의 학습 데이터가
  그 시점 이후를 이미 알고 있습니다 (기본적으로 꺼져 있고, 켜면 경고합니다)
- 출금 권한이 있는 API 키 사용
- 토큰 없이 제어 API를 공개 노출
