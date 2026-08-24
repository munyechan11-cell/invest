# 인수인계 — 다음 사람이 알아야 할 것

이 문서 하나만 읽고 이어받을 수 있게 씁니다. 코드가 이미 말하는 것(구조,
API 목록)은 안 적고, **코드를 읽어도 모르는 것**만 적습니다.

작성 시점: 커밋 `7d59d0b`. 테스트 1286개 통과.

---

## 0. 이 서비스는 지금 **실거래 중**입니다

여섯 전략이 `mode: live` 입니다. 시작 버튼을 누르면 **진짜 주문이 나가고
진짜 돈이 움직입니다.** 토스에는 모의투자 환경이 없어서 되돌릴 수단이 없습니다.

| 전략 | 하루 한도 |
|---|---|
| 국내 · 수급 추종 (토스) | 50만원 · 10건 · 손실 5만원 |
| 국내 · AI 데스크 (토스) | 50만원 · 10건 · 손실 5만원 |
| 국내 · AI 데스크 + 수급 (한투) | 50만원 · 10건 · 손실 5만원 |
| 미국 · 상대강도 상위 (토스) | $500 · 10건 · 손실 $50 |
| 미국 · AI 데스크 (토스) | $500 · 10건 · 손실 $50 |
| 코인 · 추세추종 4시간봉 | $300 · 10건 · 손실 $30 |

데모(백테스트)는 `demo`, `demo_flow`, `kr_equity`, `us_equity` 넷입니다.

**작업 중 지킬 것**

- `quant live` 를 직접 실행하지 마세요. 테스트로 충분합니다.
- 실제 증권사 엔드포인트를 호출하지 마세요. 토스 공식 스펙 사본이
  `scratchpad/toss_openapi.json` 에 있고, 목(mock)으로 검증하면 됩니다.
- LLM 을 실제로 부르지 마세요(크레딧). `POST /api/evaluate` 의 성공 경로가
  그 길입니다.
- `.env` 내용을 출력하거나 복사하지 마세요.
- **장이 열려 있는 동안 배포하지 마세요.** 이유는 3번에 있습니다.

---

## 1. 화면은 파일 셋입니다

```
quant/api/static/app.css      스타일 전부 (39,577자)  ← 디자인은 여기만
quant/api/static/index.html   마크업 + 동작 (139,335자)
quant/api/static/chart.js     캔들 차트
```

### 절대 깨면 안 되는 계약 셋

**(a) 스크립트가 붙잡는 id 145개.** 이름을 바꾸면 그 기능이 조용히 멈춥니다.
`tests/test_ui_ids.py` 가 검사하니, 바꿔야 하면 스크립트도 같이 고치세요.

`acctSetupBtn`, `acctWho`, `analystSeats`, `authCard`, `brandHome`, `brokerAcct`, `cBuy`,
`cLast`, `cLimit`, `cNotional`, `cPos`, `cQty`, `cSell`, `cStale`, `cSym`, `cTf`, `chart`,
`conn`, `ctaDemo`, `ctaJoin`, `ctaLogin`, `debateSeats`, `decisionSeats`, `demoBtn`,
`demoClose`, `demoStage`, `deskSubject`, `equity`, `feed`, `flowBody`, `hudChange`,
`hudEquity`, `hudKpis`, `hudSym`, `inspectBtn`, `inspectOut`, `lLoss`, `lLossPct`,
`lNotional`, `lOrders`, `liEmail`, `liPassword`, `liveBarText`, `lkGo`, `lkMsg`, `lkQ`,
`lkRes`, `loginForm`, `loginSubmit`, `logoutBtn`, `mAsk`, `mAskCost`, `mAskOut`, `mBudget`,
`mBuy`, `mClose`, `mCloseAll`, `mLimit`, `mNotional`, `mPause`, `mPins`, `mQty`, `mSell`,
`mSymbol`, `meBack`, `miniAcct`, `miniChange`, `miniEquity`, `mode`, `pApply`, `pAxes`,
`pDerived`, `pRetake`, `pType`, `pageTabs`, `passwordForm`, `planNow`, `playBar`, `pnlGrid`,
`pnlMode`, `pnlModeShown`, `positions`, `pwConfirm`, `pwCurrent`, `pwNew`, `pwSubmit`,
`quiz`, `quizProg`, `quizResult`, `quizSkip`, `refApply`, `refCode`, `registerForm`,
`registerSubmit`, `replayBtn`, `rgEmail`, `rgName`, `rgPassword`, `riskSeats`, `riskTag`,
`rooms`, `roomsHome`, `roundTag`, `runStart`, `runStop`, `runSum`, `say-analyst`,
`serverSince`, `setup`, `setupBtn`, `setupClose`, `setupOperator`, `setupSave`,
`setupSteps`, `setupVenues`, `setupWho`, `stBrief`, `stDetail`, `stMeta`, `stNeed`,
`stSignals`, `stepProfile`, `strategy`, `strategyPick`, `tabChart`, `tabDesk`, `tabLogin`,
`tabRegister`, `tape`, `tourBox`, `tourBtn`, `tourNext`, `tourRing`, `tourSkip`, `tourStep`,
`tourText`, `tourTitle`, `tradeCount`, `tradeLog`, `tradeModeShown`, `tradeMore`, `tryNow`,
`tryNowGo`, `venueLinks`, `verdict`

**(b) 상태 class 14개.** 스크립트가 켜고 끕니다. CSS 에서 이름을 바꾸면
화면이 상태를 표현하지 못합니다.

| class | 뜻 |
|---|---|
| `running` | 봇이 돌고 있음 (전략 선택기를 감춤) |
| `live` | **실거래 중** — 배지가 붉게 뛰고 상단 띠가 뜸 |
| `anon` / `showauth` | 로그인 전 / 로그인 화면 |
| `nodesk` | 이 전략엔 AI 데스크가 없음 |
| `idle` / `talking` | 발언 대기 / 재생 중 |
| `up` / `down` | 상승 / 하락 |
| `demoing` / `touring` | 데모 재생 / 첫 방문 안내 |
| `hidden` / `on` / `active` | 일반 토글 |

**(c) `<script>` 블록은 통째로 하나입니다.** 문법 오류 하나 —
같은 블록에 `const` 를 두 번 선언하는 것만으로도 — 스크립트가 **한 줄도**
실행되지 않고 화면이 백지가 됩니다. 콘솔에는 한 줄만 뜹니다. 실제로 겪었고
20분을 잃었습니다. `tests/test_dashboard_script_parses.py` 가 막습니다.

### 색 규칙

**방향과 상태는 다른 색입니다.**

- `--up` 빨강 / `--down` 파랑 — **한국식**, 등락 전용
- `--ok` / `--warn` / `--bad` — 성공·경고·실패

겸용하면 한국식으로 바꾸는 순간 "저장 완료" 가 빨강이 되고 "오류" 가
파랑이 됩니다. 실제로 한 번 그랬습니다.

새 토큰은 `:root` 에 먼저 정의하세요. **정의 없는 `var()` 는 오류 없이 그
속성만 사라집니다** — 배경이 통째로 투명해진 적이 있습니다.

---

## 2. 이 코드베이스가 지키는 원칙

고칠 때 이 셋만 지키면 나머지는 코드가 설명합니다.

**없는 숫자를 지어내지 마세요.** 모르면 표시하지 않는 편이 낫습니다. 화면에
자신 있게 뜬 틀린 값이 빈칸보다 훨씬 나쁩니다. 예: 토스가 예수금을 안 주므로
`cash: None` 으로 두고 "제공하지 않습니다" 라고 씁니다. 0 으로 채우면
"돈이 없다" 로 읽힙니다.

**조용히 실패하지 마세요.** 안 되면 **왜** 안 되는지 화면에 쓰세요. 이 저장소의
버그 절반이 "아무 일도 안 일어나는데 이유가 어디에도 없음" 이었습니다.
서버는 이유를 만들어 두고 화면이 안 읽는 패턴이 반복됐습니다
(`disabled_reason`, `desk_note`, `market.minutes_to_open`).

**테스트는 성질을 검사하세요.** 구현식을 베끼면 구현이 틀려도 통과합니다.
고치기 전 코드로 되돌려 실제로 실패하는지 확인하세요. 실제로 "문자열이 있는가"
만 보는 테스트를 썼다가, 수정을 통째로 되돌려도 통과하는 것이 드러났습니다.

주석은 **한국어**로, "무엇을" 이 아니라 **"왜"** 를, 특히 **"언제 이게 안
통하는가"** 를 적습니다.

---

## 3. 아직 안 고친 것 — 실거래에 영향

### 하루 손실 한도가 재시작으로 초기화됩니다 ⚠️

`resume_run(전략, 모드)` 이 전략 이름으로 run 을 찾습니다. 목록에서 다른
전략을 고르면 원장이 빈 새 run 이 열리고 `restore_budget` 이 0 부터 셉니다.
**`install.sh` 를 돌릴 때마다 서비스가 재시작되므로, 장중에 배포하면 그날
한도가 리셋됩니다.**

수정본은 **원격 브랜치**에 있습니다: `origin/pending/daily-cap-survives-restart`

### 워크트리에 있던 것을 전부 브랜치로 옮겼습니다

세션 임시 폴더에 있으면 세션이 끝날 때 경로째 사라집니다. 전부
`origin/pending/*` 으로 푸시했습니다. `git fetch` 하면 받아집니다.

| 브랜치 | 무엇 | 상태 |
|---|---|---|
| `pending/toss-fees` | 토스 수수료·거래세 0원 | **이미 main 에 머지됨** |
| `pending/pnl-split-by-mode` | 모의·실거래 손익 혼합 | **이미 main 에 머지됨** |
| `pending/daily-cap-survives-restart` | **하루 한도 재시작 초기화** (위) | 검증 미완 |
| `pending/manual-pending-orders` | 수동 대기주문이 화면에 안 보이고 취소 불가 | 검증 미완 |
| `pending/kis-unfinished-daily-bar` | KIS 가 안 끝난 당일 봉을 확정봉으로 줌 | 검증 미완 |
| `pending/backtest-trade-counting` | 성적표의 거래수·승률이 분할매도에 지배됨 | 검증 미완 |
| `pending/desk-metering` | 봇 데스크 심의가 요금제 한도를 안 거침 | 검증 미완 |
| `pending/fx-layer` | 통화 환산 계층 (아래 5번) | 검증 미완 |
| `pending/toss-websocket` | 실시간 시세 — **못 했습니다**, 아래 참고 | — |

**전부 갈라진 시점이 `4126f58` 입니다.** main 은 그 뒤로 여섯 커밋 더 갔으니
그냥 머지하면 충돌합니다. 이렇게 하세요:

```bash
git fetch origin
git diff 4126f58 origin/pending/daily-cap-survives-restart > /tmp/p.patch
git apply --3way /tmp/p.patch
```

**검증 결과: 아홉 건 전부 반박이 나왔습니다.** 통과 0건.

브랜치별 반박이 `docs/pending_review.md` 에 있습니다 — 전부 재현된 것이고,
재현 방법과 관측값이 함께 적혀 있습니다. **머지 전에 그 문서의 해당 절을
먼저 읽으세요.**

이 일곱 건은 이번이 3라운드입니다. 매 라운드 **원래 결함 진단은 맞았는데
고치면서 새 결함을 만들었습니다.** 그 패턴이 이번에도 반복됐습니다. 그러니
"이미 세 번 고쳤으니 괜찮겠지" 가 아니라 그 반대로 읽으세요.

셋은 이미 main 에서 고쳤습니다(`944f41b`) — 배포된 코드에 걸려 있던
것들이라 먼저 처리했습니다:

- 모드 전환 중 요청이 실패하면 실거래 체결이 "모의매매" 표에 섞이던 것
- 봇이 꺼져 있으면 실거래만 한 계정에 0 이 뜨던 것
- 취소 경로에서 체결이 통째로 사라지던 것

**머지 전에 하세요**

1. `docs/pending_review.md` 의 해당 절을 읽으세요.
2. `git diff 4126f58 origin/pending/<브랜치>` 를 **끝까지** 읽으세요.
3. 전체 테스트를 돌리세요.
4. **수정을 되돌려서 테스트가 실제로 실패하는지 보세요.** 되돌려도 통과하는
   테스트는 아무것도 지키지 않습니다 — 이 저장소에서 실제로 두 번 나왔습니다.

---

## 4. 아직 확인 못 한 것

**실제 토스 API 로 한 번도 안 찍어 봤습니다.** 경로·필드명·페이징은 전부
로컬 스펙 파일에서 확인했고, 실제 wire 응답으로는 확인하지 않았습니다
(크레딧·실호출 금지 규칙 때문). `toss_broker.py` 의 `_FIELDS` 가 한때
추정으로 **전부 틀렸던** 전례가 있습니다. 실거래 전에 한 종목으로 한 번
찍어 보는 것을 권합니다.

같은 이유로 이것들도 미검증입니다:
- 수급(`/api/v1/stocks/{symbol}/investor-trading`) 응답의 실제 모양
- 종목명(`/api/v1/stocks`) 다건 조회
- 계좌(`/api/v1/holdings`) 응답의 실제 모양
- 주문 응답의 `execution.commission`/`execution.tax` (지금은 우리 비용
  모델로 **추정**해서 기록합니다 — 우대 요율 계좌면 어긋납니다)

---

## 5. 설계만 하고 구현 안 한 것

`docs/cross_market.md` — 미국+국내+코인 동시 운용.

1단계(**통화 환산 계층**)가 `origin/pending/fx-layer` 에 있습니다. 이건 크로스마켓을
안 하더라도 해야 합니다: 지금 원화 종목과 달러 종목을 한 유니버스에 넣으면
**에러가 나지 않고** 7만(원)과 250(달러)이 그냥 더해집니다.

2~4단계(`CompositeBrokerage`, 종목별 캘린더, 설정 스키마)는 미착수입니다.

---

## 6. 배포

```bash
sudo bash /home/quant/app/deploy/install.sh
```

여러 번 돌려도 안전합니다. 코드를 당기고 의존성을 맞추고 서비스를 재시작합니다.
`QUANT_SECRET_KEY` 는 절대 다시 만들지 않습니다 — 그 값을 갈아엎으면 저장된
증권사 키를 전부 못 읽습니다.

배포 확인: `https://siftai.kr/api/health` 의 `started_at` 이 고친 시각보다
**뒤**여야 합니다. 파이썬은 시작할 때 모듈을 읽으므로, 재시작 전에는 옛 코드가
계속 돕니다 — 이것 때문에 몇 시간을 잃은 적이 있습니다.
