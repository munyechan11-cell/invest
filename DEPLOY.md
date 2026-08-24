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

## 3. Render

`render.yaml`이 블루프린트로 들어 있습니다.

1. 이 저장소를 GitHub에 푸시
2. Render → New → Blueprint → 저장소 선택
3. 환경변수 입력 (`sync: false`로 표시된 항목들 — `CORS_ORIGINS`, `GOOGLE_API_KEY`)
4. `QUANT_SECRET_KEY`는 Render가 자동 생성합니다. 이 값으로 **모든 사용자의
   증권사 키를 암호화**하므로, 없으면 서버가 뜨지 않습니다 — 남의 키를
   평문으로 받아두는 상태로 뜨느니 안 뜨는 편이 낫습니다.

   ⚠️ **잃어버리면 저장된 자격증명을 되살릴 수 없고 전원이 다시 등록해야
   합니다.** Render 밖에도 따로 보관하세요.

**증권사 키는 여기 넣지 않습니다.** 사용자가 각자 가입한 뒤 마이페이지에서
입력하고, 사용자별로 암호화되어 계정 DB에 저장되며 다시 조회할 수 없습니다.
여기에 중복으로 넣으면 어느 쪽이 실제 값인지 알 수 없게 됩니다.

worker 서비스도 `dryrun`으로 시작합니다. Render 대시보드에서 명시적으로
바꾸기 전에는 실주문이 나가지 않습니다.

---

## 4. 실거래 체크리스트

실거래 전에 아래가 **전부** 사실이어야 합니다. 하나라도 아니면 아직 이릅니다.

- [ ] `quant walkforward`가 PASS이고 walk-forward efficiency ≥ 0.5
- [ ] 최소 2주 이상 `dryrun`을 돌렸고, 그 결과가 같은 구간 백테스트와 유사
- [ ] `broker.max_order_notional`이 "이 금액을 통째로 잃어도 감당 가능한" 수준
- [ ] `risk.models`에 `max_dd_portfolio` 킬스위치가 있고 한도가 현실적
- [ ] `notify`로 텔레그램 알림이 실제로 도착하는지 확인함
- [ ] 거래소/증권사 API 키가 **출금 권한 없이** 발급됨
- [ ] `QUANT_SECRET_KEY`가 설정되어 있고 Render 밖에도 백업되어 있음
- [ ] 로그인 없이 열리는 것은 `/api/health` 하나뿐임을 확인함 (나머지는 전부 401)
- [ ] 프로세스가 죽었을 때 무슨 일이 일어나는지 알고 있음 (포지션은 그대로 남습니다)

```bash
python -m quant live configs/live_crypto.yaml
# 전략 이름을 직접 입력해야 시작됩니다.
```

---

## 5. 운영 중

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

## 6. 하지 말아야 할 것

- 백테스트 결과만 보고 실거래로 직행 — 그래서 walk-forward가 있습니다
- `costs.preset: zero_cost`로 낸 성적을 실현 가능한 수익으로 착각
- LLM council을 백테스트에서 켜 놓고 나온 숫자를 믿기 — 모델의 학습 데이터가
  그 시점 이후를 이미 알고 있습니다 (기본적으로 꺼져 있고, 켜면 경고합니다)
- 출금 권한이 있는 API 키 사용
- 토큰 없이 제어 API를 공개 노출
