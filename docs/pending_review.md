# 3라운드 검증 결과 — 브랜치별 반박

각 브랜치를 머지하기 전에 **여기 적힌 반박을 먼저 읽으세요.** 전부
재현된 것이고, 재현 방법과 관측값이 함께 적혀 있습니다.

`clean` 은 0건입니다 — 아홉 건 전부 반박이 나왔습니다. 이 일곱 건은
이번이 **3라운드** 이고, 매 라운드 원래 결함 진단은 맞았는데 고치면서
새 결함을 만들었습니다. 그 패턴이 이번에도 반복됐다는 뜻입니다.

> **판정 읽는 법:** 각 절 첫머리의 `상태: done · 전체 테스트 통과` 또는
> `partial`은 해당 **후보 브랜치 작성자가 제출 당시 적은 완료 주장**입니다.
> 리뷰의 최종 판정이 아닙니다. 최종 판정은 이 문서의 `반박`이며,
> `origin/pending/*` 아홉 브랜치는 전부 merge·cherry-pick 금지입니다.
>
> 이후 현재 릴리스에서 일부 원래 결함을 다시 고쳤더라도, 아래 후보 diff를
> 재사용하거나 반박이 해소됐다는 뜻은 아닙니다. 현재 코드에서 증상을 다시
> 재현하고 별도 설계·별도 회귀 테스트로 구현한 것만 새 수정으로 취급합니다.


---

## `pending/manual-pending-orders` — 수동 대기주문 표시·취소

상태: `done` · 전체 테스트 통과


### 일부러 안 한 것

이번에도 "핵심 하나만" 원칙을 지켰습니다. 지난 두 번의 반증이 전부 핵심 옆에 붙인 확장에서 났기 때문입니다.

1. **중복 접수 경고·confirm 전부 안 함.** `pending_like()`, `duplicates` 응답 필드, 매수·매도·청산·close_all 어디에도 사전 confirm 을 붙이지 않았습니다. 1라운드가 여기서 반증당했습니다(스냅샷 경합, 경로별 비대칭). 사람이 다시 누르는 **원인**(접수한 주문이 화면에 없음)을 없애는 것이 이번 범위입니다. 진짜로 서버에서 막으려면 같은 봉의 중복 청산을 `build_orders` 쪽에서 억제해야 하고, 그건 주문 생성 로직을 바꾸는 별개 작업입니다.

2. **`recent`(status/detail) 표시 안 함.** lot 미만 스킵과 정상 발주가 목록에서 똑같이 사라지는 문제는 그대로 남습니다. 화면 요소를 하나 더 늘리는 일이라 이번에 안 했습니다.

3. **처리 예정 시각 안 만듦.** 캘린더 기준 다음 세션 마감을 계산해 내려주는 것도 안 했습니다. `trader.calendar` + `next_candle_close` + 휴장 전파를 새로 계산해 **화면에 없던 숫자를 만들어 넣는** 일이고, 그게 1라운드를 무너뜨린 종류입니다. 지금 화면은 시각을 말하지 않고, `POST /api/manual/buy` 응답이 이미 쓰던 문구("다음 봉 처리 시 브로커 안전장치를 거쳐 발주됩니다")만 그대로 씁니다.

4. **차트 주문 경로(`chartOrder`, `#cMsg`)와 `#mCloseAll`·`manualPayload` 한 글자도 안 건드림.**

5. **`/api/manual` 응답 스키마 무변경.** `timeframe` 을 비롯해 필드를 하나도 추가하지 않았습니다. 화면은 이미 있던 `pending`(과 서버가 이미 붙여 주던 `symbol_name`)만 씁니다.

6. **`signedOut()` 에서 `#mPending` 비우기 안 함.** 수동 패널은 `.app-only` 안이라 로그아웃 시 `display:none` 으로 통째로 숨습니다(`body.authed .app-only{display:block}`). 게다가 `#mPins`·`#mBudget` 등 기존 요소도 아무도 비우지 않으므로, 제 것만 예외로 두면 오히려 일관성이 깨집니다. 다만 "같은 브라우저에서 A 로그아웃 → B 로그인 → 첫 새로고침 응답 도착 전"의 아주 짧은 창은 이론적으로 남습니다 — 이건 이 화면 전체의 기존 성질이지 이번 diff 가 만든 것이 아닙니다.

7. **`cancelling` 집합을 성공 뒤에 비우지 않음(의도).** 성공한 id 를 지우면 늦게 도착한 새로고침이 같은 줄을 살아 있는 버튼으로 다시 그릴 수 있고, 그게 정확히 이번에 막으려는 창입니다. 한 세션에서 취소한 건수만큼 짧은 문자열이 쌓이는 것이 대가입니다.

8. **`test_the_panel_has_a_place_to_draw_the_queue` 만 정적 검사.** DOM 이


### 반박


[money-and-regression/medium] [money/medium] 대기 줄의 `지정가` 는 **절대 발주되지 않을 가격**입니다 — 이 diff 가 새로 만든 유일한 숫자가 틀렸습니다.

`_build_one` (quant/live/manual.py:223) 은 `limit_price=float(symbol.round_price(request.limit_price, side))` 로 **틱 그리드에 스냅**해서 주문을 만듭니다. `Symbol.round_price` (quant/core/types.py:143-154) 는 매수 ROUND_FLOOR, 매도 ROUND_CEILING 입니다. KRX 틱은 config 에 실재합니다 — 005930 tick_size=100, 000660 tick_size=500 (configs/kr_toss.yaml:37-38, kr_toss_desk.yaml:41-44). 그런데 `pendingText` 는 `r.limit_price`(접수 원값)를 그대로 찍습니다.

실측(목 없이 실제 엔진 + 이 diff 의 jsc 하네스):
· 보유 1000주 000660 을 지정가 71234 로 매도 접수
  화면 줄  : `매도 · SK하이닉스 (000660) · 1000주 · 지정가 71234`
  실제 주문: `SELL 1000 @ 71500`  → 주당 266원 × 1000주 = 266,000원
· 005930 매수 지정가 71234 → 화면 `지정가 71234`, 실제 `@ 71200`
· 수량 10.7주 → 화면 `10.7주`, 실제 `10주` (round_qty)

돈이 새는 지점: 운영자는 71,234 에 걸린 줄 알고 기다립니다. 호가가 71,300 을 찍고 돌아서면 본인은 청산됐다고 믿지만 71,500 짜리 주문은 그대로 남아 있습니다. 이 줄의 존재 이유가 server.py:1964 주석의 "이걸 정말 낼 것인가를 묻는 줄" 인데, 그 질문에 실행되지 않을 가격으로 답합니다. diff 자신의 docstring 이 세운 기준("0.0421 로 낸 지정가가 화면에 0.04 로 뜨면 그건 다른 주문이고, 확인하려고 보는 화면이 확인을 망칩니다")에 그대로 걸립니다 — 방향만 반대입니다. scope_cut 아님: 안 만든 것으로 선언한 건 '처리 예정 시각'이고, 표시값은 "접수된 값 그대로"를 **장점으로** 적었지 접수값≠주문값이라는 사실은 어디에도 없습니다. 새 계산도 아닙니다(`round_price` 는 엔진 자기 함수, 서버는 이미 Symbol 을 들고 있음). 더 나쁜 건 `test_the_row_shows_the_numbers_that_were_submitted` 가 이 괴리를 **고정**한다는 점입니다 — 브로커가 실제로 받는 값을 찍도록 고치면 그 테스트가 깨집니다.

되돌려 확인한 회귀 테스트 구멍(둘 다 11/11 green, 즉 아무것도 안 지킴):
[1] 줄↔id 대응이 고정돼 있지 않습니다. `renderPending` 을 `list.map(r => pendingRow({...r, id: list[0].id}))` 로 바꾸니 두 줄 모두 `data-cancel="man_AAA"`(이 파일의 jsc 하네스로 직접 확인) — `pytest tests/test_manual_pending_is_visible.py tests/test_dashboard_script_parses.py` → **22 passed**. DOM 배선을 `const btns=…querySelectorAll("[data-cancel]"); btns.forEach(el=>el.onclick=()=>cancelPending(btns[0]))` 로 바꿔도 → **11 passed** (정적 정규식이 그대로 매치). 실행 검사는 전부 `renderPending([row])` 처럼 **한 줄짜리 리스트**만 쓰고, DOM 은 어디서도 만들지 않습니다. 이 버그가 나면 SK 줄의 취소가 삼성 주문을 죽이고 — 원치 않은 SK 주문은 다음 봉에 그대로 나가는데 화면은 "발주되지 않습니다" 라고 말합니다. 게다가 성공 시 `cancelling` 을 안 비우는 설계(의도 #7) 때문에 그 뒤 남은 모든 줄이 같은 id 로 영구 disabled 가 되어 세션 내내 아무것도 취소할 수 없습니다. 이건 이 기능에서 돈이 걸린 최상위 성질인데 선언된 한계 목록(#8 은 "정적 검사 1건"과 "jsc 없으면 skip"만 밝힘)에 없습니다.
[2] `test_the_cancel_route_belongs_to_its_owner` 는 소유권을 검사할 수 없습니다. B 는 봇이 없어 `require_trader()` 가 `NotRunning` 을 던지므로 `request_id` 는 읽히지도 않습니다 — 어떤 구현이든 통과합니다(실 취약점은 없음: id 가 uuid4 이고 큐가 trader 별).
[3] 새 404 문구가 **스킵된 주문**에 "이미 발주됐거나" 라고 단정합니다. 재현: `m.buy(SYM, notional=50)` (가격 100, lot 1) → `build_orders` 가 `[]`, `status="skipped"`, `detail="최소 주문 단위보다 작습니다"`, `cancel()` → False → 404 "이미 발주됐거나 취소된 주문입니다". 아무것도 안 나갔는데 나갔다고 말하고, 서버는 `manual.history` 로 사실을 알고 있습니다. docstring 이 스스로 막겠다던 반대매매 오독이 그대로 재생산됩니다(선언된 scope_cut #2 와 절반 겹쳐 가중치는 낮춤).

확인된 것: 전체 스위트 재현 `1236 passed in 150.67s`, 라우트 경쟁 상태 없음(엔진은 `asyncio.create_task` 로 같은 루프, registry.py:701), 취소가 `record_fill`/예산/핀 어느 쪽에도 이중 계상을 만들지 않음, `/api/manual` 스키마 무변경. 사보타주는 전부 원복했고 `shasum -c` OK · `git status` clean 입니다(작업 중 워크트리에 커밋 08a5b6c 가 생겼는데 제가 만든 것이 아닙니다 — git 쓰기 명령은 하나도 실행하지 않았습니다).


---

## `pending/pnl-split-by-mode` — 손익 모드 분리

상태: `done` · 전체 테스트 통과


### 일부러 안 한 것

1) signedOut() 의 패널 비우기. 로그아웃 뒤 앞사람의 실현 수익이 남는 것은 HEAD 부터 있던 별개 결함입니다(대시보드가 .app-only/.page 게이트 뒤라 실제로 보이려면 같은 탭에서 다른 사람이 곧바로 로그인해야 합니다). 이번 결함(모드 분리)과 독립이고, 지난 라운드에 이것을 같이 건드렸다가 호출부를 검사하지 못한다로 반증됐습니다. 손대지 않았습니다 — 제 변경이 이 상태를 더 나쁘게 만들지도 않습니다(남는 숫자에 이제 어느 모드인지가 적혀 있습니다).

2) 고를 것이 하나뿐이면 선택기를 숨긴다. modes_with_trades() 도 applyModes 도 만들지 않았습니다. 실거래를 한 적 없는 계정도 실거래 항목을 볼 수 있고, 고르면 0 과 "아직 완료된 매매가 없습니다" 가 뜹니다. 저장소 주석이 경고한 "빈 실거래 탭에 0 이 떠 있는" 상태와는 다릅니다 — 가만히 서 있는 탭이 아니라 사용자가 직접 물어본 질문에 대한 참인 대답이고 이름표가 그 자리에 있습니다. 숨기기는 CSS 오리진 함정을 두 라운드 연속 불러온 자리라, 모드 분리가 확정되기 전에는 다시 열지 않는 편이 낫다고 판단했습니다.

3) 통화 표시. 손대지 않았습니다. trades 에 통화 컬럼이 없고 run 단위 base_currency 로 대리하면 반드시 틀립니다. 지금 숫자는 예전처럼 무단위이고, 모호하지만 거짓은 아닙니다.

4) offset=tradeShown 페이지네이션 중복. 1페이지와 더 보기 사이에 체결이 확정되면 행 하나가 겹칩니다 — 선행 결함이고 모드와 무관합니다.

5) strategy 필터. strategyPick 의 onchange 가 두 함수를 부르지만 전략은 여전히 안 보냅니다(수정 전에도 그랬습니다). 한 줄이면 붙지만 "이 계정 전체" 이던 실현 수익이 "이 전략만" 으로 의미가 바뀌고, 화면의 어떤 문구도 그것을 예고하지 않습니다. 제품 결정이라 그대로 뒀습니다.

6) loadTradeLog 폴링. 실현 수익만 30초마다 갱신되고 매매 기록은 그대로인 것은 HEAD 그대로입니다 — 폴링을 붙이면 더 보기로 펼친 줄이 주기마다 접힙니다.


### 반박


[correctness/high] 반증 1 (재현 완료, high) — **실거래 체결이 "모의매매" 라고 적힌 표에 섞여 남습니다.** 이 변경이 없애겠다던 바로 그 상태입니다.

`loadTradeLog` 는 await 전에 `tradeShown = 0` 을 찍고(3408), 실패하면 `catch { return; }`(3413) 로 나갑니다 — `tradeShown` 을 되돌리지도, `#tradeMore` 를 감추지도 않습니다. 새 가드 `if (asked !== pnlMode) return;` 는 이 경로를 못 막습니다. asked 와 pnlMode 가 **같기** 때문입니다. 문제는 모드가 아니라 "표에 이미 들어 있는 줄이 남의 모드" 라는 것이고, `more` 분기는 그 위에 `insertAdjacentHTML("beforeend")` 로 덧붙입니다(3437).

작성자의 DOM 스텁을 그대로 쓰고 시나리오만 바꿔 돌린 실측(`fetch` 를 감싸 `/api/tradelog` 를 한 번만 500 으로):
- ① 실거래 1페이지: 줄 = LIVE_A, LIVE_B / `#tradeCount` = "2 / 80건" / `#tradeMore` hidden=false
- ② 모의매매로 전환, 그 한 번의 `/api/tradelog` 가 500 → 표·카운트·`더 보기` 전부 그대로(pnlMode 는 이미 dry_run, tradeShown 은 0)
- ③ 아직 보이는 `더 보기` 클릭 → 나간 주소 `/api/tradelog?limit=40&offset=0&mode=dry_run`
- **결과**: `#tradeLog` = `LIVE_A, LIVE_B, DRY_A` (실거래 2줄 + 모의 1줄이 한 표), `#tradeModeShown` = "**모의매매**", `#tradeCount` = "**1 / 5건**" — 화면에는 3줄이 떠 있는데 카운트는 1/5.

즉 실제 돈으로 낸 -1,000/-2,000 두 줄이 "모의매매" 라는 이름표 아래 서고, 카운트는 화면과 어긋난 숫자를 자신 있게 뜁니다. 트리거는 흔합니다: 전환 중 `/api/tradelog` 한 번 실패(502·서버 재시작·모바일 끊김) + 클릭 한 번. HEAD 에서 같은 경로의 결과는 "1페이지 중복"(scope_cut 4 로 인정된 것)이었는데, 모드가 붙으면서 결과가 **중복에서 모드 혼합 + 거짓 이름표** 로 바뀌었습니다 — 인정된 선행 결함의 재분류가 아니라 새 피해입니다.
새 테스트가 이걸 못 보는 이유는 구조적입니다: 하네스의 `__reply` 는 언제나 `ok:true` 라, 11개 검사 중 실패 응답을 태우는 것이 하나도 없습니다.
최소 수정: `catch` 에서 `tradeShown` 을 되돌리고 `#tradeMore` 를 감추거나, `more` 분기가 표의 현재 모드(`#tradeModeShown` 또는 별도 `renderedMode`)와 `asked` 가 같을 때만 이어 붙이는 것.

반증 2 (재현 완료, medium) — **봇이 꺼져 있으면 실거래만 해 온 계정에 0 이 뜹니다.**
`adoptRunMode` 는 `if (s.running)` 뒤에만 불립니다(1931). 봇이 멈춰 있으면 `registry.status()` 는 `{"running": False, "message": ...}` 만 주므로 모드가 없고, `pnlMode` 는 `dry_run` 에 머뭅니다. 실측(봇 off, 계정에 live 37건 -412,350, dry_run 0건):
- 나간 주소 `/api/pnl?mode=dry_run`, `/api/tradelog?...&mode=dry_run`
- `#pnlGrid` = 오늘/이번 주/이번 달/올해 전부 **0**, "0건 · 승률 —"
- `#tradeLog` = "아직 완료된 매매가 없습니다."
HEAD 에서는 같은 사용자가 자기 실거래 손익(-412,350)을 봤습니다 — 필터가 없어서 합계가 곧 live 합계였기 때문입니다. 장 마감 후·주말·휴장일에 결과를 보러 들어오는 것이 실거래 사용자의 표준 동선이고, 그때가 정확히 봇이 꺼져 있는 때입니다. 이름표가 "모의매매 기준입니다" 라 거짓말은 아니지만, `pnlMode` 는 어디에도 저장되지 않아서 **새로고침마다 매번 dry_run 으로 돌아갑니다** — 보고서의 risks("봇이 멈춰 있을 때 마지막에 보던 쪽에 머문다")는 한 세션 안에서만 참이고, 페이지 로드 뒤에는 성립하지 않습니다. scope_cut 2 는 반대 방향(사용자가 직접 live 를 골라 0 을 보는 경우)만 적었지, 이쪽은 적혀 있지 않습니다. 그리고 진실은 같은 응답에 들어 있습니다 — 서버는 `/api/pnl` 에 `modes: ["live"]` 를 여전히 실어 보내는데 프런트가 그 필드를 버립니다(저장소 주석 `modes_with_trades` 가 경고하는 것의 정확한 대칭형).

반증 3 (minor) — 워밍업 구간의 배지/패널 불일치. `LiveTrader.start()` 는 `await self.warmup()` 이 끝난 뒤에야 `self.running = True` 를 세웁니다(trader.py 184→202). 그 사이 `/api/status` 는 `running:false`, `mode:"live"`, `portfolio:{...}` 를 함께 줍니다 → 머리말 배지는 `s.portfolio` 가드만 통과하면 되므로 "실거래" 로 그려지는데, `adoptRunMode` 는 `s.running` 게이트에 막혀 안 불립니다. 1928~1930행 주석이 "배지가 '실거래' 인데 아래 표는 모의를 세고 있는 화면이 나오지 않습니다" 라고 단언하는 상태가 워밍업 내내(심볼 수에 따라 수초~수십초) 실제로 뜹니다. 숫자 자체는 이름표가 맞아 낮게 봅니다.

확인한 것 / 문제 없던 것: `pnl_by_period`·`trade_log` 의 mode 필터(runs 조인), KST 경계·ISO 주 시작은 이번 diff 밖이고 그대로 건전; `/api/status` 의 `mode` 는 `config.mode.value` 로 `runs.mode` 와 같은 값(kis_broker 는 `live` + `paper_trading` 조합을 아예 거부하므로 live 는 진짜 실계좌); `asked` 로 이름표를 뽑아 숫자와 한 쌍으로 찍는 설계 자체는 늦은 응답 시나리오에서 실제로 동작(999999/LATEDRY 미도달 재현); `encodeURIComponent`·부호·`win_rate` null 처리에 문제 없음. 워크트리는 읽기만 했고 `git status --short` 는 스테이지된 2개 그대로입니다(하네스는 scratchpad 에만 작성).


[money-and-regression/medium] ## 먼저 확인된 것(반증 실패)

- **되돌림은 진짜입니다.** 다만 주의: 검토 중에 오케스트레이터가 `588a8d6 pending: pnl-split-by-mode` 로 커밋해 **HEAD 가 변경본 위로 이동**했습니다. 이제 `git show HEAD:index.html` 로 되돌리면 11개가 **전부 통과**합니다(제가 실제로 겪었습니다). 변경 전 blob `860952d` 를 직접 꺼내 되돌려야 보고서의 주장이 재현됩니다 — 그렇게 하면 **11 failed**, 대표 메시지 `모드 없이 부르는 자리: /api/pnl"); } catch { return; }`. 다음 라운드의 되돌림 절차는 blob 기준으로 바꾸세요.
- 사보타주 A·B·C·D 전부 보고서대로 재현(1/5/1/2 failed). 제가 추가한 4종도 잡힙니다: E(pnl 은 dry_run·tradelog 는 live 로 갈라놓기)→`test_the_numbers_and_the_table_ask_for_the_same_mode`, F(tradelog 쪽 늦은응답 가드만 제거)→late 검사, H(`pnlMode="dry-run"` 오타)→boot 라벨, J(pnl 에서만 mode 제거)→4 failed.
- 서버·저장소 무변경 확인. `/api/pnl`·`/api/tradelog` 는 이미 `mode` 를 받고 `runs.mode`(NOT NULL, RunMode 3값) 서브쿼리로 거릅니다. 제거·타입변경된 응답 필드 없음(`modes` 는 그대로 나가고 프런트가 안 쓸 뿐). 전체 **1236 passed**, ruff clean.
- 실제 Chrome(shipping index.html, `showPage('account')` 로 계좌 탭 열림): `#pnlMode` display inline-block / rects 1 / h 44 / options [dry_run, live] / value dry_run, **pageerror 0**. F5 재적재로 select 값이 복원되지 않아(dry_run 유지) 선택기와 `pnlMode` 가 갈리는 경로는 없습니다. S3 시나리오로 `seenRunMode` 가드도 실동작 확인(사용자가 고른 dry_run 이 다음 refresh tick 을 넘김).

## 반박 1 — [regression/medium] 실거래만 한 사람이 봇을 꺼 두면 실현 수익이 0 으로 바뀝니다

`let pnlMode = "dry_run"` 는 영구 저장이 없어 **매 페이지 로드마다** dry_run 이고, `adoptRunMode` 는 `if (s.running)` 뒤에만 붙습니다. 봇이 안 돌면 `/api/status` 에 `mode` 자체가 없습니다(`registry.status`: 죽은 봇은 `{running:False, ...}`, mode 키 없음).

저자의 하네스를 그대로 쓴 실측(`scratchpad/probe/scenario.py`, S1) — live 거래만 있는 계정(실현 4,218,500 / 37건), 봇 정지 상태로 부팅:

```
urls      : /api/pnl?mode=dry_run , /api/tradelog?limit=40&offset=0&mode=dry_run
grid      : 오늘 0 / 이번 주 0 / 이번 달 0 / 올해 0
rows      : "아직 완료된 매매가 없습니다."
pnl_label : "모의매매 기준입니다."   count: (빈칸)
```

변경 전에는 같은 로드가 mode 없는 질의 → `pnl_by_period(mode=None)` → 필터 없음 → 이 계정에는 live 밖에 없으므로 **정확히 4,218,500 을 그렸습니다.** 즉 이 diff 는 실제 돈을 번 사람의 headline 숫자를 가장 흔한 상태에서 0 으로 바꿉니다. "어제 봇 끄고 오늘 얼마 벌었나 보러 들어온다" 가 이 패널의 1순위 용도이고, 서버 재시작(`registry._bots` 는 프로세스 메모리)마다 전원이 이 상태가 됩니다.

보고서의 risks 는 이것을 "봇이 멈춰 있을 때 **마지막에 보던 쪽**에 머무는 한계" 라고 적었는데 사실과 다릅니다 — 마지막에 보던 쪽이라는 상태는 존재하지 않습니다(저장 없음, 항상 dry_run). scope_cut 2 는 "선택기 **숨기기**" 를 안 한 것이지 "실거래 계정에 모의를 기본값으로 준다" 를 적어 둔 게 아닙니다. 게다가 `test_the_word_and_the_numbers_come_from_the_same_answer[boot-모의매매]` 가 이 기본값을 **테스트로 못박아** 놨습니다.

부수 효과(S2): 봇이 live 로 돌 때도 부팅 요청 순서는 `pnl?dry_run, tradelog?dry_run, pnl?live, tradelog?live` — `/api/status` 왕복 동안 실거래 사용자에게 "0 / 모의매매 기준입니다" 가 먼저 그려집니다.

값싼 해법: `/api/pnl` 이 이미 돌려주는 `modes` 를 초기값에 쓰거나, 첫 loadPnl 을 refresh 응답 뒤로 미루거나, 이 파일이 이미 쓰는 localStorage 에 마지막 선택을 남기는 것.

## 반박 2 — [correctness/medium] 모드를 바꾼 직후 "더 보기" 를 누르면 첫 페이지가 통째로 두 번 실립니다

`setPnlMode` → `loadTradeLog()` 가 await 전에 `tradeShown = 0` 으로 되돌리는데, `#tradeMore` 는 그 사이 계속 보이고 눌립니다. 그 클릭은 `more=true, offset=0` 으로 나가 **innerHTML 을 지우지 않고 append** 합니다.

실측(S4, live 60건 / dry_run 45건):

```
전환 전 : 45 rows, "45 / 45건"
전환 + 더 보기(같은 tick) : 80 rows, "80 / 60건"
                            LIVE-* 셀 80개 중 distinct 40 → 40행 전부 2회 표시
```

행마다 손익 셀이 있으므로 표를 눈으로 더하는 사람은 그대로 **이중 계상**하고, 건수 표시는 총계(60)를 넘는 80 을 보여 줍니다. `strategyPick.onchange` 로 같은 경합이 전에도 있었지만 그쪽은 전략을 보내지 않아 **같은 집합**의 중복이었고, 이번에 표 바로 위에 상시 노출되는 주 조작부가 생기면서 창이 크게 넓어졌습니다. scope_cut 4 로 적어 둔 것은 "체결 하나가 확정돼 한 행이 겹친다" 이지 이것이 아닙니다. `if (!more) tradeShown = 0;` 을 응답 도착 후로 옮기거나, 요청 중에 `#tradeMore.disabled = true` 한 줄이면 닫힙니다.

## 반박 3 — [regression/low] 보고서가 자랑하는 성질 중 둘은 테스트가 전혀 지키지 않습니다

프리스틴 대비 사보타주(모두 **11 passed**):

- **G**: `adoptRunMode` 의 `if (m === seenRunMode) return;` 삭제 → live 봇이 도는 동안 사용자가 모의매매를 열어 봐도 **15초마다** 실거래로 튕기고(매번 요청 2개 추가) 다른 모드를 들여다볼 방법이 사라집니다. 보고서는 "사용자가 고른 값이 30초마다 튕기지도 않습니다" 라고 명시적으로 주장하는데 그 주장을 지키는 단언이 하나도 없습니다. (덧: 튕김 주기는 30초가 아니라 15초입니다 — index.html 3000행 `setInterval(refresh, 15000)`; 30초짜리는 `loadPnl` 폴링입니다.)
- **I**: 마크업의 `<option>` 두 개 순서만 뒤집기 → 실제 브라우저에서 선택기는 "실거래" 를 표시하는데 나가는 요청과 두 이름표는 전부 dry_run. 스텁 `<select>` 에 옵션 선택 의미가 없고 시나리오가 `sel.value` 를 손으로 넣기 때문에 하네스가 원리상 못 봅니다. 지금은 순서가 맞아 실결함은 아니지만, "선택기가 가리키는 것 == pnlMode" 를 보장하는 코드도 테스트도 없습니다. `let pnlMode` 옆에 `$("#pnlMode").value = pnlMode;` 한 줄이면 코드가 보장하고(스텁도 `value` 는 평범한 프로퍼티라 볼 수 있어) 단언까지 가능합니다.

## 파일
- `/private/tmp/claude-501/-Users-munyechan-Downloads-invest-main/ed36ad0a-1a1a-4c75-b03b-4d7cb3317b46/scratchpad/r3/1/quant/api/static/index.html` (3339 `let pnlMode`, 3353 `adoptRunMode`, 3361 `setPnlMode`, 3404 `loadTradeLog`, 1931 refresh 훅, 3000 `setInterval(refresh, 15000)`)
- `/private/tmp/claude-501/-Users-munyechan-Downloads-invest-main/ed36ad0a-1a1a-4c75-b03b-4d7cb3317b46/scratchpad/r3/1/tests/test_pnl_and_tradelog_are_split_by_mode.py`
- 재현 스크립트(워크트리 밖): `/private/tmp/claude-501/-Users-munyechan-Downloads-invest-main/ed36ad0a-1a1a-4c75-b03b-4d7cb3317b46/scratchpad/probe/{sabotage.py,scenario.py,reloadcheck.py}`

워크트리는 한 바이트도 건드리지 않았습니다 — 모든 사보타주·되돌림은 scratchpad 사본에서 했고, 최종 `git status --short` 비어 있음, `index.html` 해시 `a7cfe15…` = HEAD.


---

## `pending/daily-cap-survives-restart` — 하루 한도 재시작 초기화

상태: `done` · 전체 테스트 통과

> **최종 판정: 반박 · 이 브랜치 사용 금지.** 현재 릴리스의 수정은 이 후보를
> 고쳐 쓴 것이 아니라, exact run 재개와 Toss 계좌 단위 전략 전환 차단,
> legacy/corrupt fail-closed, 이벤트-원장 대조, 감사 기록 보존 복구를 새로 설계한
> 별도 구현입니다. 따라서 원래 결함이 현재 코드에서 해결됐더라도 이 절의
> 반박은 유효하며 `origin/pending/daily-cap-survives-restart`를 merge하거나
> cherry-pick하면 안 됩니다.


### 일부러 안 한 것

**거래대금·주문 건수·손익·`starting_equity` 는 여전히 이어받지 않습니다.** 한 상태 파일에 kis·토스·바이낸스처럼 통화도 계좌도 다른 run 이 섞여 있고 `runs` 에는 통화·거래소가 기록돼 있지 않아, 두 run 의 금액을 더해도 되는지 판단할 데이터가 없습니다. 특히 `starting_equity` 를 물려받으면 비율 손실 한도의 분모가 남의 자산이 됩니다(1라운드가 반증당한 지점). 따라서 **"한도 직전까지 쓰고 전략을 바꿔 남은 여유를 다시 얻는" 경우는 이번에도 못 막습니다.** 다만 그 한도들은 걸리면 `_halt()` 를 거쳐 중단이 되므로, 중단이 걸린 뒤에는 이번 수정이 막습니다.

**시간대(`limits.timezone_offset_hours`)를 바꿔 켜면 중단이 따라오지 않습니다.** '오늘' 이 가리키는 구간이 달라져 같은 날인지 말할 수 없고, `load_state` 도 같은 이유로 자기 원장 복원을 거부합니다. 전략 목록에서 항목을 고르는 것과 달리 설정 파일의 시간대를 고치는 것은 훨씬 의도적인 행위라 뒤로 뒀습니다(`test_a_changed_timezone_does_not_carry_the_halt_either` 로 명시).

**통화·거래소 단위로 좁히지 못합니다 — 근사치는 `(mode, 거래일, 시간대)` 입니다.** `configs/kr_toss.yaml`(KRW)과 `configs/us_toss.yaml`(USD)은 둘 다 `mode: dry_run` + `timezone_offset_hours: 9` 이고, 웹앱은 사용자당 상태 파일 하나(`registry.state_path(user.id)`)라 원화 봇의 중단이 달러 봇도 멈춥니다. **너무 넓은 쪽으로 틀리는 것을 일부러 골랐습니다** — 청산은 통과하고, 다음 거래일에 저절로 풀리고, 이번 라운드부터 운영자 해제가 재시작을 넘겨 실제로 동작하기 때문입니다. 제대로 좁히려면 `runs` 에 거래소/통화를 남기고 키를 넓혀야 하는데 그건 스키마 변경 + 이관이라 이 결함과 분리했습니다.

**`/api/limits/release` 의 UI·응답 형태는 손대지 않았습니다.** 이어받은 중단을 해제하면 `status()` 의 `released` 는 false 로 남습니다(자기 한도는 안 풀렸으므로 사실 그대로). 어느 run 이 사유를 들고 있는지 보여주는 화면도 만들지 않았습니다 — 로그에만 `run <id> '<전략>' — <원본 사유>` 로 남깁니다.

**화면에 새 숫자를 만들지 않았습니다.** `status()` 의 거래대금·주문·손익은 이 run 이 실제로 쓴 값 그대로이고, 바뀌는 것은 `halted`/`halt_reason` 뿐입니다. 이어받은 사유 문자열에는 숫자가 하나도 없습니다(`test_a_carried_halt_does_not_show_another_accounts_numbers` 가 `any(ch.isdigit())` 로 검사).


### 반박


[money-and-regression/medium] Verified first: pristine HEAD sources + new tests = 8 failed/25 passed (exactly the listed 8); worktree = 1240 passed, ruff clean; worktree shas unchanged (72c84ef/5769bbd) — all work done in scratchpad copies. Exits genuinely cannot be trapped (brokerage/base.py:39 _reduces_position + check()'s is_reducing early return). No schema change, no status() key/type change. No money leak, double-count, or KRX-specific (20bp tax / ±30% / 공매도 / 15:20 종가단일가) impact found.

What refutes the writeup is mutation testing of the NEW code: 19 targeted mutants vs 68 tests (test_restart_keeps_limits + test_manual_and_limits + test_limits_bite + test_restart_wiring). 14 died, 5 survived — four of them on properties the writeup explicitly claims.

(1) M9 SURVIVES: changing status()["halt_reason"] back to `self._halted_reason` only — so a carried-halted bot renders index.html:2175 as "일일 한도 중단: " with an EMPTY reason — passes 68/68. test_a_carried_halt_does_not_show_another_accounts_numbers asserts `not any(ch.isdigit())`, which is vacuously true of "". The test cited as proof the operator sees a correct reason cannot tell a correct reason from no reason. (M19, injecting "-4,930 (한도 -1,000)" into the carried string, does kill it — so it pins "no foreign numbers", not "a reason exists".)

(2) M4 SURVIVES: deleting the timezone predicate from state.py:687 release_day_halts passes 68/68. Claim (2) is that release_day_halts clears "정확히 같은 집합" as _halt_in_force — 오늘·같은 모드·같은 시간대. Day and mode are pinned (M3/M14/M15/M16 all die); the timezone third is unpinned. Unguarded, a tz-9 bot's release would wipe a tz-0 bot's independent halt row.

(3) M1 SURVIVES and exposes an UNCLAIMED widening: release()'s non-carried path (limits.py:292-296) also calls _forget_stored_halts(). Since _persist() already blanked this run's own row, that UPDATE's only effect is on OTHER runs. The writeup frames release_day_halts strictly as the inverse of *carrying*; turning "waive my own caps today" into "clear the account's halt record" is stated nowhere and tested nowhere. Probe (scratchpad/probe_m1.py) on a DB shaped as the pre-change code wrote it (two strategies latched the same day): rows before = [(1,'2026-08-23','일일 손실 한도 도달: -4,930 …'),(2,'2026-08-23','일일 손실 한도 도달: -200 (한도 -200) …')]; after ONE self-release on run 1 = [(1,''),(2,'')]; a brand-new 제3전략 then reports halted: False and new buy (True, ''). Blast radius is narrow — probe section 1 shows that through post-change product paths a second run can never acquire its own halt row (it always carries first), so this reaches only a DB written by the old build (deploy day), where the outcome is no worse than the pre-change status quo. It turns into a live money bug the moment a second run can hold a halt — i.e. the per-currency/exchange scoping the writeup itself names as the follow-up.

(4) M5 SURVIVES (dropping b.run_id<>? from _halt_in_force) — benign, the caller already gates on budget.halted.

Smaller: test_carrying_a_halt_does_not_stamp_an_empty_ledger and test_releasing_a_carried_halt_does_not_waive_this_bots_own_caps both open with `assert budget.halted`, so pre-fix they fail on line 1 for the generic reason rather than on the property they name — several of the "8 failures" are one failure under different names. Separately, quant/webapp/registry.py:483 save_limits mutates cap attributes in place (so a limits edit does not silently drop _carried_halt — good), but zeroing all four caps still makes configured False and brokerage/base.py:47 _budget_check returns (True, "") before check() runs; the "한도를 0 으로 만들어 캡 자체를 끄는 최악의 탈출구는 더 이상 필요하지 않습니다" line is about necessity, not availability — that hatch still bypasses both the carried halt and a self-halt.

Files: /private/tmp/claude-501/-Users-munyechan-Downloads-invest-main/ed36ad0a-1a1a-4c75-b03b-4d7cb3317b46/scratchpad/r3/2/quant/live/limits.py (292-296, 408), .../quant/live/state.py (687), .../tests/test_restart_keeps_limits.py. Probes/mutation harness: scratchpad/probe_m1.py, scratchpad/mutate.py, scratchpad/mutate2.py.


---

## `pending/kis-unfinished-daily-bar` — KIS 당일 미완성 봉

상태: `done` · 전체 테스트 통과


### 일부러 안 한 것

1) **주봉 라벨 규칙은 추론하지 않았습니다.** KIS 가 주봉 행의 `stck_bsop_date` 에 그 주의 첫 거래일을 쓰는지 마지막 거래일을 쓰는지 실 응답으로 확인할 방법이 이 환경에 없습니다(실 엔드포인트 호출 금지). 그래서 라벨 날짜의 장 마감만 봅니다 — 라벨이 오늘이면 어느 규칙이든 그 주는 미완이므로 확실히 거르고, 라벨이 이미 지난 거래일이면 수정 전과 동일하게 통과합니다. 주봉은 개선되지만 완전히 닫히지 않았고, 수정 전보다 나빠지는 경우는 없습니다. `_session_closed` 의 docstring 에 그대로 적어 두었습니다.

2) **15:30:00 경계에 집계 지연 여유를 넣지 않았습니다.** 반박 부수(2)에 답한 대로, 검증할 수 없는 초 단위 상수를 넣는 것은 1라운드의 실수를 크기만 줄여 반복하는 것입니다. 배포 설정의 라이브 루프는 09:00:03 KST 에만 폴링해 이 경계를 밟지 않습니다.

3) **`end` 를 과거로 고정한 백테스트에서 `end` 자체가 장중인 경우**(마지막 봉이 붙었다 떨어졌다 하는 문제)는 다루지 않았습니다. 판정을 `min(now, end)` 로 바꾸면 닫히지만, 지금까지 나온 과거 백테스트 수치가 조용히 달라집니다. 보고된 결함은 "지금 미완성 봉이 나온다" 이고 그 경로는 `now` 만으로 닫힙니다.

4) **시간외 단일가(15:40~18:00)와 수능일 같은 특례 개장 시각은 모델링하지 않았습니다.** 저장소의 `KRX_REGULAR` 자체가 정규장만 정의하고 있고, 그 모델을 넓히는 것은 캘린더 쪽 별건입니다.

5) **`quant/data/providers/kis_flow.py` 의 `_volumes` 는 그대로 두었습니다.** 같은 일봉 엔드포인트를 직접 부르므로 이번 변경이 닿지 않고, 수급의 "확정" 판정 근거는 봉과 달라 실 응답 없이 규칙을 옮겨 붙일 수 없습니다(토스는 `individual is null` 이라는 스펙상의 표식을 쓰는데, KIS 의 대응 표식은 확인 불가).

6) **`quant/brokerage/toss_broker.py` 의 같은 `YYYYMMDD → UTC 자정` 관례 + `end_ts` 필터**도 같은 성격으로 보이지만 다른 담당 결함일 수 있어 건드리지 않았습니다.

7) **`CachingProvider._key` 에 시계가 없는 문제**는 손대지 않았습니다. `history` 결과가 벽시계에 의존하는 것은 야후·ccxt·토스가 이미 같고, 수정 전에도 캐시에는 그 시각의 미완성 봉이 그대로 굳어 있었으므로 나빠지지 않습니다. `latest_bars` 는 캐시를 우회합니다(`CachingProvider.latest_bars` → `inner.latest_bars`). 캐시 키 스키마 변경은 네 프로바이더에 걸치는 별건입니다.


### 반박


[correctness/high] 필터 자체의 수학은 반증하지 못했습니다. `_session_closed` 는 라벨 날짜 < 오늘이면 항상 True, 라벨 == 오늘이면 15:30 KST 기준으로만 갈립니다 — 즉 정확히 "장 마감 전 오늘 봉 하나"만 거릅니다. 부호·경계·KST 산술 모두 맞고, `end_ts <= now` 변형이 왜 틀리는지도 맞습니다.

반증되는 것은 "한 줄 필터라 파급이 없다"는 전제입니다. **이 변경은 라이브 루프가 쓰는 유일한 가격 기준을 "지금"에서 "어제 15:30"으로 조용히 옮깁니다.** scope_cut 7개 어디에도 없습니다.

근거(코드):
- `/private/tmp/.../r3/3/quant/core/context.py:89-95` — `ctx.price()` 는 신선한 호가가 있으면 호가 mid, 없으면 `latest(symbol).close`.
- `set_quote` 는 **저장소 전체에서 정의만 있고 호출부가 0개**입니다(`grep -rn set_quote quant/` → `context.py:97` 정의 한 줄뿐). 즉 라이브에서 `ctx.quote()` 는 항상 None 이고, `ctx.price()` 는 **언제나 마지막 봉의 종가**입니다.
- 그 값을 쓰는 곳: `quant/risk/models.py:105`(트레일링 손절 트리거), `quant/portfolio/base.py:84`(포지션 사이징), `quant/execution/base.py:397`(주문 델타), `:497-499`(에스컬레이션 지정가 기준). `MaximumUnrealizedProfit` 은 `on_bars` 의 `ctx.portfolio.mark(bar.symbol, bar.close)`(`quant/core/engine.py:143`)에 걸립니다.
- 배포 설정(1d)의 라이브 루프는 `next_candle_close(..., lag=3)` 으로 하루 한 번 09:00:03 KST 에만 틱합니다(`quant/live/trader.py:362`). 그 한 번이 그날의 전부입니다.

실측(스크래치패드 `repro_mark.py`, 워크트리 밖, 네트워크 없음 — `_get` 만 가짜):
005930 보유, 손절선 67,500. 금 08-21 종가 75,000 → 월 08-24 시가 60,000(갭 -20%), 종가 62,000.
- 수정 후: 08-24 09:00:03 → `on_bars(라벨 2026-08-21)`, `ctx.price = 75,000` → **손절 미발동**. 08-25 09:00:03 → `on_bars(라벨 08-24)`, `ctx.price = 62,000` → 그제서야 발동.
- 수정 전: 08-24 09:00:03 → `ctx.price = 60,000` → 그날 아침 발동.
→ **갭 하락에 대한 손절이 정확히 한 거래일 늦습니다.** 손절이 존재하는 이유인 바로 그 사건에서요. 같은 하루 동안 사이징과 지정가 기준가도 17.5시간 낡은 값입니다.

이건 선언된 축소가 아니라 못 본 파급입니다. 오히려 제출문은 "빈 배치에도 risk.manage 를 돌린다"를 거절하면서 "마크 가격 없이 손절을 평가하게 됨"이라고 썼습니다 — 마크가 봉에서 온다는 걸 알면서, 자기 변경이 그 마크를 한 세션 낡게 만든 것은 짚지 않았습니다. 새 테스트 6개도 프로바이더 출력만 보고 `ctx.price`/`risk.manage` 는 한 번도 타지 않아 이 자리를 볼 수 없습니다.

blocker 는 아닙니다. 되돌리는 쪽이 더 나쁩니다 — 수정 전에는 `_fetch_new_bars` 의 중복 제거(`bar.ts <= last`, `quant/live/trader.py:502-505`) 때문에 **확정된 종가가 ctx 이력에 한 번도 들어가지 못했고**(다음날 아침 확정판은 ts 가 같아 통째로 버려짐), 라이브 지표 전체가 "시가를 종가라고 부른 시계열" 위에서 계산되고 있었습니다. 제 재현에서 수정 전 세 틱이 전부 그날 라벨·그날 시가로 들어오는 것으로 확인했습니다.

따라서 요구되는 것은 되돌림이 아니라 보완입니다: `KisProvider.quote()` 는 이미 구현돼 있으므로(`quant/data/providers/kis.py`, `stck_prpr` 기반 Quote) `_tick` 에서 `ctx.set_quote(await provider.quote(sym))` 한 줄이면 `ctx.price` 의 호가 경로가 살아나고, 봉은 확정봉으로 두면서 손절만 현재가로 평가됩니다. 그 전까지는 이 변경을 실계좌에 올리면 안 됩니다 — 지표는 맞아지지만 갭 방어가 하루 사라진 채로 도는 상태이고, 화면에는 그 사실이 아무 데도 안 뜹니다.

부수 확인(별건, 새 지적 아님): `quant/data/flow.py:381-383` `FlowFeed.get` 의 docstring 은 "sessions that had already closed by `now`" 라고 말하지만 `f.ts <= now` 는 09:00:03 KST 에 오늘 미완성 수급 행을 통과시킵니다 — 봉 쪽과 같은 종류의 결함이 남아 있고, 이번 변경으로 두 시계열이 한 세션 어긋난다는 제출문의 서술과 일치합니다. 이건 scope_cut 5로 정직하게 선언돼 있어 결함으로 세지 않습니다.


[money-and-regression/high] 두 가지를 반증합니다. 하나는 실제 돈이 새는 조용한 동작 변화이고, 하나는 "테스트가 결함을 잡는가" 에 대한 반증입니다. 저자 주장(M1/M2 재현, 1231 passed, ruff)은 전부 독립적으로 확인했고 그대로 맞습니다.

━━ 반증 1 [money/high] 라이브 북의 **유일한 마크 가격**이 한 세션 늦어졌습니다. scope_cut 어디에도 없습니다.

배선을 끝까지 따라가면 마크 경로가 하나뿐입니다:
· `Context.set_quote` 는 `quant/` 전체에서 **한 번도 호출되지 않습니다**(정의만 있음, quant/core/context.py:97). 따라서 `ctx.price()` 는 항상 `self.latest(symbol).close` 로 떨어집니다(context.py:89-95).
· `portfolio.mark()` 는 오직 `bar.close` 로만 불립니다 — quant/core/engine.py:143, quant/live/trader.py:141·162·439. 다른 소스 없음.
· `LiveBrokerage.sync()` 는 수량·평단·현금만 맞추고 `last_price` 는 건드리지 않습니다(quant/brokerage/live_base.py:282-346).
· `Position.unrealized_pct` / `market_value` 는 그 `last_price` 하나에 달려 있습니다(quant/core/types.py:389-406).
· 1d 라이브 루프는 하루 **한 번** 09:00:03 KST 에만 틱합니다(`next_candle_close(...,lag=3)`, quant/core/clock.py:64-73 + trader.py:362).

즉 그 하루 한 번의 `bar.close` 가 그날 하루의 손절·트레일링·포트폴리오 DD·노셔널 가드 전부의 기준가입니다.
· 수정 전: 그 값은 KIS 가 실어 준 **그 순간의 현재가**(3초 전).
· 수정 후: **전 거래일 15:30 확정 종가** — 적용되는 순간 이미 17시간 33분 묵었고, 다음 마크까지 41시간 묵습니다.

재현(`scratchpad/repro_mark_staleness.py`, 워크트리 밖. 진입 100,000 / 08-21 종가 98,000 / 08-24 시가 88,000·종가 68,600(하한가) / 08-25 시가 48,020(또 하한가) — KRX ±30% 안에서 전부 합법):
  2026-08-24 09:00:03 KST (하루 한 번뿐인 폴링)
    수정 전  마크 봉=2026-08-24  close=88,000  평가손익=-12.00%  손절 **발동**
    수정 후  마크 봉=2026-08-21  close=98,000  평가손익= -2.00%  손절 **발동 안 함**
  2026-08-25 09:00:03 KST
    수정 후  마크 봉=2026-08-24  close=68,600  평가손익=-31.40%  이제야 발동 → 체결은 08-25 시가 48,020 근처

같은 사건에서 청산가가 88,000 → 약 48,020. 손실이 -12% → 약 -52%, 한 종목에서 40%p 차이입니다. 배포 설정 두 개 모두 이 경로 위에 있습니다: configs/kr_equity.yaml (provider kis, timeframe 1d, max_dd_per_security 10%, trailing_stop, max_dd_portfolio 20%), configs/kr_desk_gemini.yaml (동 20%/18%). 국내 상하한가가 ±30% 라 "밤새 갭이 손절폭보다 큰" 상황이 예외가 아니라 기본값이고, 그 갭이 이제 한 세션 통째로 안 보입니다. 포트폴리오 킬스위치(max_dd_portfolio)도 같은 이유로 하루 늦게 걸립니다.
부수 피해(같은 `last_price`): `market_value` 기반의 `max_order_notional`·`max_daily_notional`·사이징이 하루 묵은 가격으로 계산됩니다(account.py:112 주석이 바로 이 가드를 지목합니다). 화면도 `/api/symbols` 의 `price`·`change_pct` 가 `ctx.history(sym,2)` 라(server.py:1940) 장중 내내 전일 종가를, 등락률은 D-2→D-1 을 보여줍니다 — 사용자가 보는 "현재가" 가 하루 밀립니다.

이건 선언된 축소가 아닙니다. scope_cut 2번은 09:00:03 폴링을 근거로 "15:30 경계는 안 밟는다" 고 했는데, **같은 그 폴링이 유일한 마크라는 점**은 보지 못했습니다. 되돌리라는 뜻이 아닙니다 — 프로바이더 계약은 이제 맞습니다. 다만 봉에서 뺀 신선도를 어디에도 되돌려 놓지 않았고, 배관은 이미 있습니다: `KisProvider.quote()` 가 있고 `ctx.set_quote()`/`ctx.price()` 가 quote 우선 로직을 이미 갖고 있는데(context.py:89-95) 죽은 코드입니다. `_tick` 에서 보유 종목 quote 를 받아 `set_quote`+`mark` 하거나, 최소한 "1d KRX 에서는 손절이 다음 개장까지 평가되지 않는다" 를 배포 설정 옆에 명시해야 합니다.

━━ 반증 2 [regression/test] 15:30 경계는 사실상 테스트되지 않습니다 — 되돌려도 통과합니다.

`_session_closed` 의 마감 시각만 바꾸고(가짜 피드는 15:30 그대로) 돌린 결과:
  마감 → 14:00 : 6 passed | 마감 → 15:20 : 6 passed | 마감 → 12:00 : 6 passed
(11:00, 15:30] 구간 어디로 옮겨도 안 걸립니다. 그리고 그 변형들은 관측 가능하게 틀립니다 — `scratchpad/repro_survivor.py`, 15:20 변형:
  정상 15:30 → 2026-08-24 봉 종가 {11:00: 없음, 15:25: 없음, 15:35: 77,000, 16:00: 77,000}
  변형 15:20 → 2026-08-24 봉 종가 {11:00: 없음, **15:25: 71,000**, **15:35: 77,000**, 16:00: 77,000}
같은 날짜 봉이 하루 안에 71,000 → 77,000 으로 바뀝니다. `test_a_bar_never_changes_after_it_has_been_handed_out` 이 막겠다고 선언한 바로 그 성질인데, 통과합니다. 하필 15:20 은 종가단일가 시작 시각이라 실제로 값이 다시 잡히는 유일한 구간입니다.

원인: 가짜 피드 `FakeKis._close_for` 가 구현과 **같은 상수**를 읽습니다 — `if here.time() >= KRX_REGULAR.close: return SETTLED` (tests/test_kis_candles.py:93-95). 그래서 경계가 틀려도 피드가 같이 틀려 줍니다. 모듈 docstring 의 "마감 시각(15:30)을 테스트 안에서 다시 계산해 대조하지 않습니다 — 구현식을 베끼면 구현이 틀려도 같이 틀려 줍니다" 는 정확히 반대로 실현됐습니다(베낀 것이 식이 아니라 상수일 뿐입니다). 더해서 시계를 11:00 과 16:00 두 점에서만 찍어, 두 점 사이는 전부 사각지대입니다. 고치는 법은 작습니다: 피드가 확정 시각을 테스트가 정한 리터럴(15:30)로 갖고, 15:25 / 15:29 / 15:31 를 추가로 찍으면 세 변형이 전부 깨집니다.

━━ 확인했지만 반증이 아닌 것
· M1(수정 전 필터 `uniq = {b.ts: b for b in bars if start <= b.ts < end}`) → 5 failed, 1 passed. M2(`b.end_ts <= now`) → 1 failed(`test_a_settled_close_is_not_hidden`), 5 passed. 저자 주장 그대로 재현됩니다.
· 전체 1231 passed, `ruff check .` All checks passed.
· 과거 `end` 백테스트: 라벨이 전부 지난 날짜라 `_session_closed` 가 항상 True → 봉 동일, 수치 회귀 없음(다만 그 성질을 고정하는 테스트는 없습니다 — scope_cut 3번을 나중에 `min(now,end)` 로 열면 과거 수치가 소리 없이 달라지고 아무것도 안 깨집니다).
· 반환 타입·필드 변화 없음(`list[Bar]` 그대로, 개수만 줄 수 있음). 중복 계상은 `ts` 키 dict 라 그대로 없음. 거래세·틱·상하한가 계산 경로는 손대지 않았습니다.
· 주봉 라벨·시간외·`_volumes`·`toss_broker`·`CachingProvider._key` 는 선언된 scope_cut 이라 결함으로 세지 않았습니다.

━━ 복원: 변형 전부 되돌렸습니다. `quant/data/providers/kis.py` md5 = 477da0bde0e51f827d52e5badc3802ae (백업과 동일), `git status --porcelain` 비었음. git 쓰기 명령은 실행하지 않았습니다(중간에 하네스가 스테이지된 변경을 78f793d 로 커밋했습니다). 임시 스크립트 2개는 워크트리 밖(scratchpad)에 있습니다.


---

## `pending/toss-fees` — 토스 수수료·거래세

상태: `done` · 전체 테스트 통과


### 일부러 안 한 것

일부러 안 한 것 다섯. 이번 라운드의 목표는 "완벽한 수정" 이 아니라 "반증되지 않는 수정" 이라, 결함 진술("모든 체결의 수수료·거래세를 0원으로 기록") 밖은 전부 잘랐습니다.

(1) **주문응답 경로와 조회 경로의 수량 이중계상.** `_venue_submit` 이 체결을 `_pending_fills` 에 넣지만 `order.apply_fill` 은 부르지 않으므로, 두 경로가 같은 체결을 보면 수량이 두 번 잡힙니다(제출 4 + 조회 10 → 장부 14). 2라운드는 이걸 `_book()` 안에서 `apply_fill` 을 불러 고쳤고, 바로 그것이 좀비 주문(FILLED 에 못 닿음)을 만들어 반증당했습니다. 그래서 이번에는 **손대지 않았습니다** — 고치기 전과 완전히 같은 구조입니다. 덧붙여, 토스 공식 스펙상 `POST /api/v1/orders` 의 응답 스키마는 `OrderResponse`(`orderId`, `clientOrderId`) 뿐이라 실제 API 에서는 이 경로가 애초에 체결을 실어 오지 않습니다. 제대로 고치려면 `LiveBrokerage.submit()` 이 상태를 무조건 SUBMITTED 로 덮는 것(live_base.py:168)부터 함께 봐야 하고, 그건 네 어댑터 전부에 걸리는 변경입니다.

(2) **응답에 실린 실제 청구액을 읽는 일.** 2라운드 scope_cut 은 "필드 이름을 공식 레퍼런스로 확인하지 못했다" 고 적었는데, 이번에 로컬 스펙(`scratchpad/toss_openapi.json`)에서 확인했고 **그 전제는 틀렸습니다**: `GET /api/v1/orders/{orderId}` 의 `Order.execution` 에 `commission`("총 체결 수수료")과 `tax`("총 체결 세금")가 명시돼 있습니다. 그럼에도 이번에 읽지 않은 이유는 이름이 아니라 **위치** 입니다 — 스펙상 체결 수량·평균가도 `execution.filledQuantity` / `execution.averageFilledPrice` 로 한 겹 안쪽인데, 지금 어댑터는 둘 다 최상위(`filledQuantity`/`averagePrice`)에서 찾습니다. 비용만 안쪽에서 읽으면 앞뒤가 안 맞고, 필드 경로를 통째로 고치는 것은 이 결함과 별개인 데다 "총액" 필드에서 조각 비용을 빼내는 누계-델타 산술(1라운드가 반증당한 바로 그 지점)이 따라옵니다. 다음 단계는 명확합니다: 필드 경로를 먼저 고치고, 그 다음 `commission+tax` 를 모델보다 우선시하되 null 이면 모델로 물러서는 것.

(3) **단가 미상 시 `fill_channel_down`.** KIS 는 체결단가를 못 읽으면 채널을 내려 신규 주문까지 막습니다. 지금은 보류 + WARNING 까지만 합니다 — 거래 중단은 "수수료 0원" 보다 훨씬 넓은 행동 변경이고 자기 테스트를 따로 가져야 합니다. 부작용으로, 응답이 계속 단가를 안 주면 폴링마다 WARNING 이 반복됩니다.

(4) **KIS·ccxt·


### 반박


[correctness/medium] 핵심 수정의 산술은 재현했고 다투지 않습니다: buy 105.0 / sell 1505.0 (700,000×1.5bp, +700,000×20bp), `broker.fees is fee`, 1234 passed. 반박은 이번에 **새로 넣은 `price <= 0` 가드**가 취소·종료 경로에서 체결을 통째로 없앤다는 것입니다 — 이건 declare 된 scope_cut 이 아니라 이 diff 가 만든 새 동작입니다.

■ 재현 (소켓 없음, 가짜 client. probe: scratchpad/probe_rebut.py, probe_sync.py)
입력: 005930 BUY 10 @70,000, broker_id=T-1, status=SUBMITTED. 조회 응답 `{"filledQuantity":"10","averagePrice":null}`. 그 상태에서 `await broker.cancel(order)`.
 · 이 코드   : `_pending_fills == []`, `order.filled_qty == 0`, `order.status == canceled`, `order.id in broker._orders == False`
 · HEAD 코드 : `order.filled_qty == 10`, `order.status == filled`, `_pending_fills == [(10, 0.0, 0.0)]`
(HEAD 비교는 워크트리를 건드리지 않고 고치기 전 `poll_fills` 본문을 그대로 옮긴 함수를 인스턴스에 바인딩해 돌렸습니다. 전체 되돌리기가 아니므로 TypeError 함정 없음.)

■ 왜 결함인가 — 가드의 근거가 이 경로에서 거짓
toss_broker.py 의 가드 주석은 "`order.filled_qty` 를 올리지 않았으므로 같은 수량이 그대로 다시 잡힙니다" 라고 적었습니다. 일반 폴링에서는 참입니다(연속 3회 폴링 확인: 주문은 계속 open, 매번 다시 봄). 그러나 `LiveBrokerage.cancel()` 은 `_reap()` → `poll_fills()` **직후** `self._orders.pop(order.id)` 합니다(live_base.py:196). 즉 취소 경로에서는 "다음 폴링" 이 없습니다. `Engine.stop()` 은 열린 주문을 전부 cancel 하므로(engine.py:104-105) 세션 종료 시에도 같습니다.
그리고 `_reap` 의 docstring 이 스스로 밝힌 목적이 정확히 이 사고입니다: "거래소에는 체결로 남았는데 여기서는 취소로 남으면 … 손절도 사이징도 하루 한도도 걸리지 않는 포지션이 생깁니다."

■ 상위층으로 번지는 지점
`ExecutionModel._resolved`(execution/base.py:320)는 `rec.cancel_requested and rec.order.filled_qty > rec.filled` 로 "취소가 체결에 졌다" 를 판정합니다. 이제 0 을 읽으므로 판정이 안 서고 `self._cross_next.pop(key)` 가 실행되지 않습니다 — ESCALATE 로 취소된 건이면 이미 체결된 청산인데도 그 종목의 다음 주문이 시장가로 승격된 채 남습니다(스프레드를 도로 냅니다).
`sync()` 로 얼마나 복구되나: 수량만입니다. `TossBrokerage` 는 `_venue_costs()` 를 오버라이드하지 않아 `avg=0` → `if avg > 0` 이 거짓 → 현금이 차감되지 않습니다. 실측: cancel+sync 후 `cash=10,000,000`(실제로는 700,000원 지출), `qty=10`, `avg_price=0.0`, `total_fees=0.0`.

■ 공정하게 — 이게 blocker 가 아닌 이유
(a) 고치기 전에도 같은 입력은 price 0 으로 booking 해 원가 0 인 포지션을 만들었으므로 **자산가치 왜곡 자체는 무승부**입니다. 새로 나빠진 것은 주문 수명주기 신호(filled_qty 0, status=canceled)와 체결 이벤트 소실입니다. (b) `_tick` 이 매 사이클 `sync()` 를 돌려 수량은 한 틱 안에 되돌아옵니다(단, 종료 시 cancel-all 뒤에는 sync 가 없습니다). (c) 공식 스펙대로면 `filledQuantity` 자체가 최상위에 없어 조회 경로가 애초에 아무것도 잡지 않습니다 — 다만 그건 이 가드(와 그 가드를 검사하는 새 테스트 2개)가 통째로 죽은 코드라는 뜻이기도 해서, 둘 다 취할 수는 없습니다.
따라서 반박 대상은 수수료 산술이 아니라 **scope_cut (3) 의 서술**입니다: 가드의 대가를 "폴링마다 WARNING 반복" 으로만 적었는데, 실제 대가에는 "취소·종료 경로에서 체결 소실 + 취소경주 판정 파괴" 가 포함됩니다.
한 줄 처방: 단가를 못 읽으면 KIS 처럼 `self.fill_channel_down(...)` 을 부르거나, 최소한 `cancel()` 이 `filledQuantity > order.filled_qty` 인 주문을 버리지 않게 할 것.

■ 부차 (판정을 좌우하지 않음)
1) `_fee_model_for` 는 **`fee_model is None` 인 fallback 에서만** 통화로 갈라집니다. 설정이 넘어온 정상 경로에는 그 분기가 없어, kr_equity 모델이 USD 종목 매도에 그대로 붙습니다: AAPL 10주 @190.5 SELL → fee 4.0957 (거래대금 1,905 = 21.5bp, 한국 증권거래세 20bp 포함). 같은 체결이 `fee_model=None` 이면 1.00(us_equity). fallback 이 본 경로보다 통화를 더 잘 아는 셈입니다. 배포 config 는 KR/US 파일이 분리돼 있어 손으로 섞어야 도달하고 백테스트도 같이 틀리므로 low. (KIS 는 `_fill_fee` 에서 `quote_currency` 로 갈라 이걸 피합니다.)
2) 부분체결의 요금 기준가가 증분가가 아니라 **누적평균가**입니다. 4주@70,000 → 6주@71,000(누적평균 70,600) 시 장부 수수료 1,512.74 vs 실제 거래대금 기준 1,517.90 (−0.34%). 기존 price 처리에서 물려받은 것이지만 KIS 는 `_delta_price(row, order, total, newly)` 로 해결하고, 이제 수수료가 그 오차를 함께 탑니다.


---

## `pending/backtest-trade-counting` — 백테스트 거래수 집계

상태: `done` · 전체 테스트 통과


### 일부러 안 한 것

(1) **미청산 자리를 성적표에서 빼지 않았습니다** — 2라운드는 뺐고 반박도 그걸 정직한 판단으로 봐줬지만, 빼면 "닫힌 자리 0" 인 장부에서 `report.trades` 가 0 이 되어 `multi_metric_loss` 의 `_NO_TRADES` 절벽이 새로 생깁니다. 넣어 두면 "조각 0 ⟺ 자리 0" 이 유지돼 러너의 "zero closed trades" 경고와 목적함수의 0 분기가 예전 그대로 동작합니다. 대신 익절만 트림하고 물린 자리가 승리로 남는 낙관 편향은 그대로입니다(고치기 전에도 같았으니 회귀는 아닙니다). 별개 티켓.
(2) `_thin_penalty` 의 `minimum=20` 은 손대지 않았고, 대신 함수가 세는 대상을 명시적으로 조각 수로 고정했습니다. 그래서 "자리 하나를 다섯 조각으로 나눠 표본 20 을 채우는" 게이밍 구멍은 여전히 열려 있습니다 — 눈금과 문턱을 함께 다시 정하려면 기존 walk-forward 판정을 전부 다시 돌려야 하므로 별개 티켓입니다. docstring 에 이 한계를 그대로 적었습니다.
(3) 기대값의 자리 간 크기 가중(작은 익절 자리와 큰 손절 자리가 같은 무게)은 이 티켓의 병이 아니라 기대값 정의의 문제라 손대지 않고 주석에만 남겼습니다.
(4) `quant/live/state.py` 의 `/api/pnl` 과 `index.html` 의 "N건 · 승률" 은 손대지 않았습니다 — 기간별 실현 현금을 묻는 화면이라 조각 단위가 맞고, 건수만 자리 단위로 바꾸려면 날짜 창을 가로지르는 자리와 run 을 가로지르는 세션 연결을 먼저 정해야 합니다.
(5) `ClosedTrade.peak_invested` 를 `_trade_dict()` / `record_trade()` 의 직렬화에 넣지 않았습니다 — 화면과 DB 에 숫자를 늘리는 것이 지난 실패의 유형이었고, 성적표 계산에만 필요합니다.
(6) `metrics.py` 의 `tail_ratio` ZeroDivisionError(수익률이 전부 0 인 장부에서 `analyze` 가 죽음)는 재현했지만 기존 결함이라 고치지 않았습니다 — 테스트 헬퍼 `_mark_curve` 에 회피 이유만 적었습니다.
(7) `risk/protections.py` 는 계속 조각을 봅니다 — 부분 손절도 손절이라 그게 맞습니다.


### 반박


[correctness/medium] 불변성이 반쪽입니다 — "무동작 쌍"의 다리 순서를 뒤집으면 지난 라운드를 죽인 그 병(꼬리손실 축소)이 그대로 재현됩니다.

`peak_invested` 는 "체결마다 (유출−유입) 을 누적한 값의 **최대치**" 입니다. 최대치이므로 **먼저 팔았다 되사면**(inv 가 내려갔다 돌아옴) 절대 안 움직이지만, **먼저 샀다 되팔면**(inv 가 올라갔다 돌아옴) 분모가 영구히 올라갑니다. 두 조작 모두 계좌에 대해 완전히 무동작인데, 테스트는 `rebuy_price` 만 파라미터화하고 **다리 순서는 파라미터화하지 않아** 통과하는 쪽만 잽니다.

재현 (`/private/tmp/.../scratchpad/rt5_probe_c.py`, 수수료 0):
  A: BUY 100@100 → (마크) → SELL 100@40
  B: BUY 100@100 → **BUY 50@300, SELL 50@300** (같은 날·같은 값, 손익 0·순현금 0) → (마크) → SELL 100@40
실측 결과 — cash 994,000.0 **동일**, equity 994,000.0 **동일**, equity_curve 원소 단위 **동일**, total_fees 0 동일, 실현손익 -6,000 동일, `report.trades` 둘 다 1. 그런데
  `worst_trade` / `expectancy` / `avg_loss` : A = **-60.0000%**, B = **-24.0000%**
분모가 10,000 → 25,000 으로 뛰어 진실 -60% 자리가 **-24% 로 2.5배 축소**되어 인쇄됩니다. 승자 쪽도 같습니다: 100→200 자리에 같은 쌍을 끼우면 `expectancy` 가 **+100% → +50%** (rt5_probe_c 형 probe 로 실측). 이는 반박 #2 가 "worst_trade 를 3배 축소해 보여주는 것은 자금배분에 직접 걸린다"며 거부한 것과 **같은 형태·같은 방향**이고, `ClosedTrade.peak_invested` docstring 의 "…최대치는 워시 트림에 구조적으로 불변이라 '매도 스케줄이 성적을 정하는' 병이 없습니다" 라는 일반 주장을 정면으로 반증합니다. `test_wash_trims_do_not_shrink_a_loss` 는 이 절반을 재지 않습니다.

엔진에서 닿는 경로인가 — 예. `configs/demo.yaml` 실행에서 닫힌 자리 90 개 중 **70 개가 "줄였다가 다시 늘리는"** 패턴을 갖고, `peak_invested` 는 진입 명목금액의 최대 **2.148배**(중앙값 1.027, 30/90 이 1.05배 초과)까지 올라갑니다. vol_target + `rebalance_every` 리밸런싱 구조상 일시적 증량→재감량은 상시 발생하고, 증량·감량 가격이 가까울수록 그 상승분은 순수 인공물입니다.

정상참작 (그래서 blocker/high 가 아님): (a) 인쇄되는 값 자체는 "최대 투입자본 대비 수익률" 로 정의상 정합적이지 지어낸 수가 아닙니다; (b) 순수 무동작 쌍은 인위적이고, 진짜 피라미딩(다른 가격의 증량)은 올바르게 처리됩니다; (c) 자동 판단에 걸린 `multi_metric_loss` 는 `sharpe·max_drawdown·profit_factor·win_rate·turnover` 만 읽어 **분모에 전혀 민감하지 않습니다** — 영향은 사람이 읽는 성적표 줄에 한정됩니다; (d) 나머지 주장은 실측으로 확인됐습니다: demo 재현 `trades 91 · win 32.967% · expectancy -0.236% · worst -12.078%`(계좌 -7.969%), `_thin_penalty` 는 `runner.py:275` 의 `trades=[_trade_dict(t) for t in portfolio.closed_trades]` 로 구성상 항등이며 `BacktestResult` 생성 지점은 러너 하나뿐이라 회귀 없음, 전체 1267 passed · ruff clean, 닫힌 자리 3,381 개 무작위 재생에서 `basis<=0` 0 건, 미청산 자리는 demo 에서 91 중 1 개(영향 미미)라 scope_cut (1) 은 정직합니다.

부차 (low): `test_wash_trims_do_not_move_the_scorecard` 의 부동소수 **완전 일치** 단언은 고른 다섯 상수의 성질입니다 — `rebuy_price` 를 [50,400] 에서 무작위로 4,000 회 뽑으면 **1,498 회**가 1e-15 상대오차로 `==` 를 깨뜨립니다(예: 368.4 → 1.0 vs 0.9999999999999994). 돈에는 무의미하나, 상수를 손대면 바로 깨지는 단언입니다.

권고: `wash_book` 에 `leg_order ∈ {sell_first, buy_first}` 를 추가해 실패를 고정하고, 분모를 순서 대칭인 값(예: 자리 수명 동안의 **시간가중 평균 순투입** 또는 실현 시점별 투입액 가중)으로 바꾸거나, 최소한 docstring 의 일반 주장을 "매도 스케줄에는 불변, 일시 증량에는 불변이 아님" 으로 좁히고 그 한계를 scope_cut 에 명시할 것.


[money-and-regression/high] 분모는 워시 트림(팔았다 되사기)에만 불변이고, 거울상인 "담았다 되팔기"에는 무너집니다 — 2라운드를 죽인 바로 그 결함이 두 다리 순서만 바꾼 채 그대로 살아 있습니다.

재현(수수료 0, 계좌·현금·자산곡선·포지션이 모든 시점에 비트 단위로 동일함을 assert 로 확인):
  A: BUY 100@100 → SELL 100@40                                   → trips=1, worst_trade = -60.0000%
  B: BUY 100@100 → (SELL 50@300, BUY 50@300)×3 → SELL 100@40      → trips=1, worst_trade = -60.0000%  (테스트가 재는 순서, 통과)
  C: BUY 100@100 → (BUY 50@300, SELL 50@300)×3 → SELL 100@40      → trips=1, worst_trade = -24.0000%  (재지 않는 순서, 파탄)
A·B·C 는 cash=994,000, equity_curve 전 원소, 매 시점 포지션이 완전히 같습니다. peak_invested 만 10,000 → 25,000 으로 뜁니다. 즉 `test_wash_trims_do_not_shrink_a_loss` 가 -60.0000% 를 완전 일치로 요구하는 그 자리가, 두 체결의 순서만 뒤집으면 -24% 로 인쇄됩니다(2.5배 축소). 2라운드 peak_basis 의 -21.82% 와 같은 등급입니다.

승자 쪽은 더 나쁩니다. 진실 +100.0000% 인 자리(BUY 100@100 → SELL 100@200)에 무동작 (BUY 50@P, SELL 50@P) 쌍을 끼우면, 테스트 파일이 쓰는 바로 그 재매수가 스윕에서
  P=100 → +66.6667% / 105 → +65.5738% / 130 → +60.6061% / 199 → +50.1253% / 400 → +33.3333%
쌍을 1회 넣든 3회 넣든 같습니다(경로가 아니라 최고점이 분모라). 반박 #1 이 "무동작인데 +100% 가 +62% 로 움직인다" 고 지적한 크기를 그대로 재현합니다.

2026 거래세 20bp 를 넣으면 극성이 반박 #2 가 기각한 그 방향입니다. 손실 자리 100→40, 고점 300 에서 3회:
  plain  equity 993,992 (세금 8)   worst -60.0800%
  wash   equity 993,902 (세금 98)  worst -60.9800%   ← 90원 더 잃고 더 나쁘게 인쇄(옳음)
  pair   equity 993,902 (세금 98)  worst -24.3920%   ← 같은 90원을 더 잃고 2.5배 좋게 인쇄
체결 수도 세금도 계좌도 wash 와 pair 가 동일합니다. 다른 건 두 다리의 순서뿐입니다.

이건 정의의 선택이 아니라 결함입니다. `_peak_invested` 는 체결 단위 누적값의 **순간 최대치**를 잡으므로, 지속시간 0 의 상방 과도(담았다 즉시 되팔기)는 분모에 남고 지속시간 0 의 하방 과도(팔았다 즉시 되담기)는 안 남습니다. 0초 묶인 돈은 세고 0초 풀린 돈은 안 세는 경제적 해석은 없습니다. 시간가중 평균투입 또는 바 경계의 마크된 익스포저 최고치를 쓰면 대칭이 됩니다.

도달 가능성: 인위적 케이스가 아닙니다. quant/portfolio/base.py:88-95 는 목표비중 리밸런서로 `desired = round_qty(weight * investable / price)` 를 매 바 `ctx.equity`·`price` 에 대해 다시 계산하고 `min_trade_weight` 데드밴드만 걸립니다. 자산·가격·알파점수가 데드밴드를 넘게 움직이면 보유 중인 종목에 BUY 를 내고 나중에 SELL 을 냅니다 — 담기→덜기는 기본 경로입니다. demo.yaml 에서 580 조각 중 490 이 분할매도라는 사실 자체가 데드밴드가 상시 열린다는 뜻입니다.

테스트가 못 잡는 이유(구조적): tests/test_scorecard_round_trips.py 의 13개 함수 전부가 `wash_book()` 을 통해 장부를 만들고, 그 함수는 64-65 행에서 SELL 다음 BUY 만 냅니다. 열린 자리에 BUY 를 먼저 내는 케이스가 파일 전체에 없습니다. 42 케이스와 재매수가 5-스윕은 두 순서 중 한쪽만 증명합니다.

되돌려 확인한 것(요청대로 수행 후 전부 복원, git status 비어 있음, md5 원본 일치):
  - [잡음] `_peak_invested` 를 진입 시점에 얼림 → test_adding_real_capital_does_enlarge_the_denominator 실패(1.0 vs 0.3333).
  - [잡음] `n = len(result.trades)` → `n = result.report.trades` 복귀 → test_the_sample_floor_did_not_move_when_the_scorecard_unit_changed 실패(1.9 vs 0.0).
  - [못 잡음] account.py 에서 수수료를 분모에 접어 넣음(`invested = ... + (notional*sign + fill.fee)*lifetime_dir`) → **전체 1267 passed**. `wash_book()` 의 fee 기본값이 0.0 이고 이를 덮어쓰는 호출자가 없어서, 20bp 거래세가 분모에 들어가는지 여부를 리포지토리 전체가 구분하지 못합니다. 주석이 밝힌 제외 근거("워시 트림마다 분모가 수수료만큼 밀린다")에 회귀 보호가 0 입니다.

부수(미고지 동작 변화, 낮음): `_streaks` 가 이제 `trips` 를 **첫 등장(진입) 순서**로 훑습니다. 예전에는 `closed_trades` 를 청산 순서로 훑었습니다. 다종목 장부에서 longest_win_streak/longest_loss_streak 이 더 이상 결과의 시간 수열이 아닙니다(먼저 열고 늦게 닫은 자리가 먼저 닫힌 자리보다 앞에 세어짐). 화면 전용이라 게이트에 걸리진 않지만 scope_cut 목록에 없습니다.

문제없이 확인된 것(회귀 없음): PerformanceReport 의 필드명·타입 불변(`trades` 는 여전히 int), `ClosedTrade.peak_invested` 는 기본값 있는 추가 필드이며 어디서도 직렬화되지 않음(ClosedTrade 에 asdict 사용처 없음, 생성처는 account.py:201 한 곳). `report.trades == 0 ⟺ len(closed_trades) == 0` 성립하므로 runner.py:246 경고와 losses.py:96 의 _NO_TRADES 분기 동작 불변. `_thin_penalty` 의 `len(result.trades)` 는 runner.py:271 이 유일한 프로덕션 생성처이고 조각당 dict 하나를 넣으므로 옛 값과 항등적으로 같음 — 여기까지는 주장대로입니다. 최적화 손실함수들이 expectancy/worst_trade/basis 를 읽지 않아(multi_metric 은 sharpe·max_dd·profit_factor·win_rate·turnover) 탐색 순위는 이 결함에 오염되지 않습니다 — 그래서 blocker 가 아니라 high 입니다. 다만 자금배분이 직접 읽는 worst_trade·expectancy·avg_loss 가 경제적 무동작 회전만으로 2.5~3배 축소되고, 그 성질을 재는 테스트가 없습니다.


---

## `pending/desk-metering` — 데스크 계량

상태: `done` · 전체 테스트 통과


### 일부러 안 한 것

1) **상한에 걸렸을 때 PROTECTION 이벤트를 쏘지 않습니다 — 로그만 남깁니다.** 2라운드는 이벤트를 쐈고, 그래서 `quant/live/notifier.py` 와 `quant/api/static/index.html` 두 표시 경로를 "종목 없는 보호" 를 견디게 함께 고쳐야 했습니다. 그런데 이번 베이스의 index.html 은 2라운드 베이스 이후 729줄이 바뀌었고 보호 렌더가 `symLabel(p.symbol, …)` 을 거치도록 달라져, 같은 수정을 새로 짜야 합니다 — 화면에 "· undefined ·" 를 찍을 위험을 만드는 쪽보다, 같은 상황(계량기 거절)을 이미 로그로만 알리고 있던 `_deliberate_now` 의 선례를 따르는 쪽을 골랐습니다. 사용자는 심의 버튼을 누르면 `/api/evaluate` 가 429 로 같은 문장을 돌려줍니다.
2) **사이클 단위 오버슛** — 상한을 최대 한 사이클치 넘길 수 있습니다. 사이클 중간에 끊으면 어떤 종목은 심의되고 어떤 종목은 안 된 봉이 남고, 제대로 고치려면 요금제 문구를 "일 심의 수" 에서 "일 사이클 수" 로 바꾸는 제품 결정이 먼저입니다. `test_the_free_plan_actually_bites_a_running_bot` 이 이 동작(8/5)을 감추지 않고 그대로 검사합니다.
3) **`on_start` 의 `_preflight()` 1콜은 계량 밖입니다.** `record_spend` 는 부를 때마다 `deliberations` 를 1 올리므로, $0.001 짜리 사전점검이 하루 5회 중 한 건을 먹으면 $0.06 짜리 심의와 같은 무게가 됩니다. 금액보다 상한 왜곡이 커서 그대로 뒀습니다.
4) **`registry._meter()` 는 손대지 않았습니다** — `plan` 과 `own_key` 를 봇 세울 때 한 번 읽고 고정합니다. 베이스가 이미 그렇게 돌고 있었고 `_deliberate_now` 가 이미 그 값으로 상한을 걸고 있었습니다. 도중에 결제하거나 키를 넣은 사람은 봇을 다시 띄우면 반영됩니다.
5) **`/api/evaluate` 의 `own_key` 재계산 함정**(2라운드 검증자의 관찰 1)은 안 고쳤습니다 — 다른 결함이고 별도 진단이 필요합니다. 이번 diff 는 그 두 줄을 건드리지 않았습니다.
6) **`record_spend` 의 sqlite 쓰기는 이벤트 루프에서 동기로 돕니다.** 심의당 1회 커밋이고, 그 심의는 이미 LLM 호출 수십 건을 기다린 뒤입니다.
7) **`TradingDesk.status()` 나 API 응답에 상한 관련 필드를 추가하지 않았습니다.** 아무도 읽지 않는 값을 만드는 것이 지난 실패의 한 갈래였습니다.
8) `LiveTrader.desk()` 는 여전히 이름이 `desk` 인 알파만 찾으므로, 카운슬만 있는 전략에는 개장 전 심의가 안 나갑니다 — 기존 동작이고 이번 결함과 무관합니다.


### 반박


[money-and-regression/medium] ## 먼저, 확인된 것 (반증 실패한 부분)

되돌리기 실험을 전부 직접 재현했고 주장한 숫자가 정확히 맞습니다. 워크트리는 실험 후 `git checkout` 으로 복원했고 `git status` 는 비어 있습니다.

- council 의 `_within_plan` 게이트 + `_deliberate_cached` metered/record 삭제 → **7 failed** (주장과 일치)
- desk 의 같은 두 훅 삭제 → **9 failed**
- `LiveTrader._bind_meter_to_llm_alphas()` 호출 삭제 → **1 failed** (`test_a_bot_binds_its_meter_to_every_llm_alpha`)
- `_deliberate_now` 이중청구 가드 삭제 → **1 failed**, 메시지가 `20콜을 쓰고 40콜이 청구됐습니다` — 주장한 그 값입니다
- `CallMeter.add` 의 parent 전파 삭제 → **8 failed**
- 전체 스위트 **1249 passed** (146s), `ruff check .` **All checks passed**

계량 자체는 제가 만들어 본 경로에서 다 맞습니다. 봇 사이클(`_deliberate_cached` 의 A ← `deliberate` 의 B) 과 `/api/evaluate`(E ← F) 는 태스크 컨텍스트가 갈라져 서로 섞이지 않고, `deliberate()` 는 `self.meter.record` 를 부르지 않으므로 봇 데스크를 공유하는 evaluate 가 이중청구되지 않습니다. `wait_for` 는 좌석을 취소하고 기다리므로 스코프 밖에 살아남는 태스크가 없고(도크스트링이 경고한 그 구멍은 현재 코드에 없습니다), `_calls()` 는 `decision_client is client` 일 때 중복 합산하지 않으며, `flow_feed` 는 `InvestorFlowAlpha.update` 가 스스로 갱신하므로(`quant/alpha/flow.py:69`) 데스크가 상한에 걸려 `refresh` 를 건너뛰어도 규칙 알파의 수급이 굳지 않습니다. API 필드 제거·타입 변경도 없습니다(`metered.llm_calls`/`cost_usd`/`billed_to`, `DeskDecision.llm_calls` 전부 같은 이름·같은 타입, 값만 정확해짐). 덤으로 `ResearchCouncilAlpha` 에는 `status()`/`estimated_cost_usd` 가 아예 없어서 베이스의 `/api/evaluate` 는 council 설정에서 AttributeError 로 500 이 났을 텐데, 이 diff 가 그걸 같이 없앴습니다.

## 반증: 상한이 걸리면 장부가 조용히 청산됩니다 — 이건 어디에도 안 적혀 있습니다

`update()` 가 `_within_plan()` 에서 `[]` 를 돌려주는 순간, 그건 과금 사건이 아니라 **매매 사건**이 됩니다. 데스크가 침묵하면 그 데스크가 만든 인사이트가 `period` 만큼 뒤에 만료되고, `PortfolioConstructionModel.create_targets`(`quant/portfolio/base.py:67`)는 활성 인사이트가 없는 보유 종목에 **명시적 0 타깃**을 냅니다.

실제로 돌려봤습니다(`VolatilityTargeting`, 설정값은 `configs/kr_toss_desk.yaml` 그대로: max_position_weight 0.35 / leverage 0.9 / cash_reserve 0.05, 자본 ₩10,000,000):

```
데스크 살아있음 → [('005930', 45.0, 'w=+0.3500')]     # 보유 ₩3,298,680
데스크 침묵     → [('005930',  0.0, 'w=+0.0000')]     # 전량 시장가 청산
전량 청산 시 거래세 20bp = ₩6,597
```

왜 이게 이번 diff 의 문제인가: 베이스에서는 봉마감 사이클이 **한 번도 상한에 걸리지 않았습니다**. `_deliberate_now` 만 계량됐고 그 경로는 인사이트를 더할 뿐 지우지 않으며, 봉마감 데스크가 계속 인사이트를 재발행했으므로 과금 상한이 장부를 되돌리는 경로는 존재하지 않았습니다. 이번에 생겼습니다.

일 상한(무료 5회)만 보면 약합니다 — `max_symbols_per_run: 2`, 캐시 덕에 휴장 중 반복 심의는 0콜이라 봇 자체는 하루 ~2회만 쓰고, `horizon_bars` 10 × 일봉이면 인사이트가 상한 리셋보다 오래 삽니다. 문제는 **월 비용 상한**입니다: 무료는 `Plan("free", …, monthly_cost_usd=3.0)` 이고 이건 하루마다 리셋되지 않습니다. `usage.allow` 가 월 상한에서 False 를 돌려주면 데스크는 **그 달 남은 기간 내내** 침묵하고, 그동안 데스크 인사이트는 전부 만료되어 데스크만 지지하던 종목은 시장가로 풀립니다. 그리고 이 상황은 화면에 아무것도 안 띄웁니다(scope_cut 1 — 로그만). 사용자가 보게 되는 유일한 설명은 `/api/evaluate` 를 눌렀을 때의 429 문구인데, 거기엔 "규칙 기반 전략은 그대로 돌아갑니다" 라고만 적혀 있습니다. 맞는 말이지만 절반입니다 — 데스크가 들고 있던 부분은 20bp 거래세를 물고 정리됩니다.

정직하게 깎을 부분도 적습니다: (a) 출시 설정은 전부 규칙 알파와 함께 있어서(`test_every_live_strategy_has_a_desk` 가 강제) 규칙 알파도 좋아하는 종목은 가중치가 0 이 되지 않고, 리스크 계층(trailing_stop 등)은 계속 돕니다. 전량 0 이 되는 건 데스크만 지지하던 이름입니다. (b) 같은 모양이 `cost_limit_usd` 와 `QuotaExhausted` 의 `_disabled` 경로에 이미 존재합니다 — 새로 만든 메커니즘이 아니라, **드물던 고장 경로를 정기적인 과금 경로로 바꾼 것**입니다. (c) 상한을 아예 안 거는 것보다는 명백히 낫습니다.

그래도 이건 scope_cut 으로 선언된 항목이 아닙니다. 선언된 건 "상한 시 PROTECTION 이벤트를 안 쏜다" 는 **알림** 쪽이고, 매매 쪽 결과 — 상한이 포지션을 판다 — 는 diff 설명 어디에도 없고("화면에는 새 숫자를 하나도 만들지 않았습니다") 24개 테스트 중 어느 것도 `update()` 가 `[]` 를 돌려준 뒤 장부에 무슨 일이 생기는지 보지 않습니다. `test_a_capped_account_stops_the_bot_before_it_spends` 는 `insights == []` 를 확인하고 거기서 멈추는데, 돈이 새는 자리는 정확히 그 다음 줄입니다.

고치는 방향은 작습니다: 상한에 걸렸을 때 마지막 결정의 인사이트를 재발행하거나(hold 유지), 최소한 상한이 장부를 건드리기 전에 사용자에게 알리는 것. 지금은 사용자가 "왜 내 종목이 팔렸지" 에 답할 수 있는 자료가 서버 로그 한 줄뿐입니다.

## 그 외 (반증 아님, 기록용)

- `_preflight` 1콜이 계량 밖인 건 선언대로입니다. 다만 봇 재시작마다 나가고 상한에 걸린 계정도 통과하므로, 재시작을 스크립트로 돌리면 상한 밖에서 운영자 키가 열립니다($0.001/회라 실질 위협은 낮음).
- `meter.record` → `record_spend` 의 sqlite commit 이 이벤트 루프에서 동기로 도는 건 선언대로이고, `_sum()`(=`allow()` 가 타는 읽기 경로)은 `self._lock` 을 안 잡습니다 — 다만 이건 베이스부터 그랬습니다.
- 묶인 데스크에서는 개장 전 심의가 원장에 N줄(종목 수)로, 묶이지 않은 폴백에서는 1줄로 적혀 `deliberations` 세는 단위가 다릅니다. 다만 `desk()` 와 `_bind_meter_to_llm_alphas()` 가 같은 목록을 훑으므로 후자는 실제로 도달 불가능한 경로입니다(`test_an_unbound_desk_is_still_billed_by_the_trader` 는 스텁을 주입해서 검사합니다).


---

## `pending/toss-websocket` — 토스 실시간 시세

상태: `partial` · 전체 테스트 통과


### 일부러 안 한 것

일부러 안 한 것.

1) **토스 웹소켓 클라이언트** — 프로토콜 근거가 없어서. 추측으로 구독 메시지를 쓰면 실패 모양이 "연결은 되는데 아무것도 안 오는" 것이 되고, 그건 호가창이 조용히 멈춘 것과 구분되지 않습니다. 이 파일의 `_FIELDS` 표가 한때 추측이었다가 전부 틀렸던 전례가 그대로 있습니다.

2) **`DataProvider.stream()` 구현** — 폴링으로 감싼 `stream()` 은 지금 아무도 부르지 않는 경로이고(엔진은 이미 폴링 루프), 있으면 "스트리밍 된다" 는 인상만 줍니다. 대신 `stream()` docstring 에 붙일 자리(`LiveBarFeed.admit()`)와 `supports_streaming` 을 같이 켜야 한다는 것을 적었습니다.

3) **index.html 배지** — 범위 밖이라고 명시되어 손대지 않았습니다. 서버 쪽 `status().feed` 까지만 했습니다.

4) **서버 `/ws` 로 봉 밀어내기** — 지금 밀 수 있는 것은 폴링으로 받은 봉뿐이라 브라우저가 `/api/candles` 를 폴링하는 것과 실질이 같습니다. 소켓 시세가 생긴 뒤에 하는 것이 순서입니다.

5) **밀린 봉을 `on_bars` 로 하나씩 재생** — 지표 연속성은 `ctx.push_bar` 로 잇고 판단은 가장 최근 확정봉으로 한 번만 합니다. 재생하면 지나간 시장에 대해 판단을 N번 내리고(데스크가 붙어 있으면 LLM 도 N번), 그 주문은 지금 가격에 나갑니다. `test_a_pile_up_still_decides_only_once` 가 이 선택을 고정합니다.

6) **`glossary.PROVIDER` / `DELAYED_PROVIDERS` 문구** — 토스 호가·현재가는 실제로 실시간이라 지금 문구가 거짓은 아닙니다. 화면 쪽 작업과 같이 다듬는 편이 낫다고 봐서 뒀습니다.


### 반박


[correctness/medium] 반박 1건 (핵심). `LiveBarFeed._backfill` 의 한도 초과 분기가, 이 작업이 스스로 피하려고 설계했다고 적은 바로 그 거짓 경고를 냅니다 — 그것도 확인 없이 지어낸 숫자로.

feed.py:177-180
```
if missing > self.max_backfill_bars:
    self._record_gap(symbol, last + step, next_ts - step, missing,
                     f"되메우기 한도 {self.max_backfill_bars}봉을 넘습니다")
    return []
```
`history()` 되묻기(=휴장 판정)는 **그 아래**에 있습니다. 즉 구멍이 240봉을 넘으면 거래소에 물어보지도 않고 "못 봤다" 로 단정합니다. 그런데 장 마감→다음 개장 사이는 분/틱 주기에서 언제나 240봉을 넘습니다.

관측값(워크트리 코드 그대로, 네트워크 0건. `/private/tmp/.../scratchpad/probe_feed.py`):
KRX 금요일 15:30 마감(06:30 UTC) → 월요일 09:00 개장(00:00 UTC), `history()` 는 빈 목록을 주는 휴장 상황:
```
[ 1m] history() 호출 0회 | degraded=True | unseen=[{'bars': 3930, 'reason': '되메우기 한도 240봉을 넘습니다'}]
[ 5m] history() 호출 0회 | degraded=True | unseen=[{'bars': 786,  ...}]
[15m] history() 호출 0회 | degraded=True | unseen=[{'bars': 262,  ...}]
[ 1h] history() 호출 1회 | degraded=False | unseen=[]      ← 되물어서 조용
[ 1d] history() 호출 1회 | degraded=False | unseen=[]
```
1분봉은 주말뿐 아니라 **매일 밤**(15:29→익일 09:00, 1050봉) 납니다.

왜 결함인가:
1. **없는 값을 지어냈습니다.** "3930봉을 못 봤습니다" 는 사실이 아닙니다 — 그 구간에 봉은 애초에 생기지 않았습니다. 화면(`status().feed.unseen_windows`)에 봉 수가 정수로 자신 있게 뜹니다. 로그 문구까지 "이 구간의 지표는 이어 붙인 것이고, 그 위의 판단은 그만큼 덜 믿을 수 있습니다" 라고 단정합니다.
2. **작업 설명의 근거와 모순됩니다.** "빈 응답은 휴장으로 봅니다 — 간격만 보고 우기면 주말마다 거짓 경고가 납니다" 가 이 설계의 정당화인데, 한도 초과 분기는 정확히 "간격만 보고 우기는" 코드입니다. `일부러 안 한 것` 목록에도 없으므로 scope_cut 이 아닙니다.
3. **경보 채널이 파괴됩니다.** `_record_gap` 은 심볼당 1건이고 `gaps` 링버퍼는 20건입니다. 20종목 유니버스면 **월요일 아침 한 번에 20건이 차서** 그 이전의 진짜 결손(되묻기 실패)이 전부 밀려납니다. 게다가 `gaps` 는 회복해도 비워지지 않아 `degraded` 가 프로세스 수명 내내 True 로 고정됩니다 — 정상 5봉을 더 받아도 `degraded=True, unseen 1건` 이 그대로였습니다.
4. 테스트가 이 분기를 덮지 않습니다. `test_a_holiday_is_not_reported_as_a_hole` 은 1d·2봉 구멍(한도 이하)만 봅니다. 즉 스위트는 "휴장을 결손으로 신고하지 않는다" 는 성질을 주장하지만 코드는 분/틱 주기에서 그 성질을 위반합니다.

도달 가능성: 출하 config 는 전부 1d(암호화폐 4h)라 기본 경로는 안전합니다. 다만 `DataConfig.timeframe` 은 사용자 값이고 `TossProvider._INTERVAL` 이 `1m` 을 공식 지원하므로 1분봉 주식 봇은 지원 구성입니다. 1d 에서도 재현 경로가 있습니다: KRX 장기 거래정지(>240일) 종목이 재개되면 같은 문구로 "365봉 못 봤습니다" 가 뜹니다. `LiveTrader.calendar` 가 이미 있는데 피드에 전달되지 않는 것도 같은 뿌리입니다.

고칠 방향(둘 중 하나면 충분): (a) 한도 초과여도 **되메우기 대신 확인만** 하십시오 — 구간 앞뒤 1~2봉만 `history()` 로 찍어 빈 응답이면 휴장으로 조용히 넘기고, 봉이 실제로 존재할 때만 `FeedGap`; 또는 (b) `LiveTrader.calendar` 를 `LiveBarFeed` 에 넘겨 `missing` 을 거래일 기준으로 세십시오. 더불어 `gaps` 는 시간·회복 기준으로 만료시켜 `degraded` 가 내려올 수 있어야 합니다.

부수 관찰(결함 아님, 참고): 1d 정상 경로에서도 주말·연휴 직후 첫 틱마다 심볼당 `history()` 가 한 번씩 추가로 나갑니다(휴장 확인용). 200종목이면 그 틱에 200회가 더해집니다 — 토스 레이트 리밋과 부딪히면 `fetch_failures` 로 되돌아옵니다.

나머지 검토 결과(반박 실패, 즉 주장대로 맞음): 밀린 봉 전량 전달·심볼당 1회 판단(`_tick` 의 `push_bar`/`on_bars` 분리)은 `push_bar` 가 단순 append 이고 `pending` 이 전역 시간순이라 심볼별 순서가 보존됩니다. 미완성 봉 가드(`end_ts > now` 를 버리지 않고 넘김)는 `seen` 을 올리지 않아 확정본이 다음 폴에서 통과하는 것을 확인했습니다(1d KRX/US 모두 `next_candle_close(lag=3)` 덕에 경계에서 3초 여유로 통과). `_seen` 프로퍼티는 `feed.seen` 을 그대로 돌려주므로 warmup·유니버스 갱신의 제자리 변경이 정상 반영됩니다. 웹소켓을 만들지 않은 판단과 근거(`toss_openapi.json` 에 AsyncAPI 주소만 존재)는 스펙 파일에서 확인했고 타당합니다. 워크트리는 수정하지 않았습니다(`git status` 깨끗). 네트워크 호출 0건.


[money-and-regression/high] 기준: 1236 passed / ruff 통과 재현함. 토스 AsyncAPI 주장도 확인함 — toss_openapi.json 안의 websocket/wss/asyncapi 언급은 info.description, tags, externalDocs 넷뿐이고 전부 외부 문서 포인터입니다. 소켓을 안 만든 것은 판단이지 결함이 아닙니다. 아래는 **만든 부분**의 결함입니다.

■ [high] 새로 붙인 미완성봉 가드가 엔진을 굶길 수 있고, 그 상태를 새 health 판이 "정상"이라고 보고합니다
feed.py:148-150 은 end_ts > now 인 봉을 seen 을 올리지 않고 넘깁니다(설계대로). 그러면 그 사이클의 pending 은 비고, trader.py:479-481 이 `if not pending: return` 으로 조기 반환합니다 — on_bars 가 안 돌면 risk.manage 도 안 돌고, 열린 포지션의 손절·익절이 **그날 한 번도 평가되지 않습니다**. 문제는 이게 화면에 안 뜬다는 것입니다.
관측(probe p3, 네트워크 0건). 프로바이더가 200 OK 로 "오늘 진행 중 일봉"만 계속 주는 모양(KIS 일봉 엔드포인트가 오늘 행만 돌려주는 흔한 형태):
  20틱 동안 엔진에 전달된 봉: 0
  degraded : False | unseen_windows: [] | fetch_failures: []
  held_partial_bars : 20        ← 유일한 흔적인데 degraded 계산(feed.py:217)에 안 들어감
  mode_ko  : "REST 폴링 — 봉은 마감된 뒤에야 갱신됩니다"
관측(probe p2-a). 빈 배열을 30틱 연속 받은 경우:
  degraded False / unseen [] / fetch_failures [] / backfilled 0
즉 "연결은 되는데 아무것도 안 오는" 모양 — 보고서가 소켓을 안 만든 **이유**로 든 바로 그 실패 모양 — 을 REST 경로에서 만들어 놓고, 그것만 감지 못 합니다. gap 은 구멍 **건너편에 새 봉이 도착해야** 기록되므로, 구멍이 아직 안 닫힌 동안에는 정의상 조용합니다. fetch_failures 는 예외가 나야만 찹니다.
돈: 상하한가 30% 시장에서 손절이 하루 한 번도 평가되지 않는데 대시보드는 초록입니다. 정지 자체는 기존에도 있었지만(옛 코드도 `if not bars: return`), 옛 코드는 진행 중 봉이라도 통과시켜 매 틱 on_bars 가 돌았습니다. 이번 변경은 "쓰레기로 판단" 을 "아예 판단 안 함" 으로 바꿔 놓고, 동시에 "이상 없음" 이라고 말하는 판을 새로 달았습니다. degraded 에 held_partial 연속 증가/last_bar 노후를 넣거나, N틱 연속 무입력을 gap 으로 올려야 합니다.
테스트: 이 성질을 잡는 테스트가 없습니다. test_a_bar_still_forming_never_reaches_the_engine 은 `held_partial_bars >= 1` 만 봅니다 — 20이 되어도, 영원히 굶어도 통과합니다.

■ [medium] degraded/unseen_windows 가 회복해도 안 내려갑니다 — 저자 자신이 쓴 실패 모드
feed.py:199-201 은 gaps 를 20개로 자르기만 하고 성공 시 비우지 않습니다.
관측(probe p2-b): history() 1회 실패 → degraded True. 이후 **34회 연속 정상**, 되메우기 3봉 성공. 그래도 degraded True, unseen_windows 1건(2026-08-04) 그대로. 프로세스 재시작 전까지 영구 빨강입니다. FeedGap docstring 이 경계하는 "사람이 경고를 안 읽게 된다" 를 구현이 스스로 만듭니다. 잡는 테스트 없음.

■ [medium] 한도 초과 분기가 장중 주기에서 매일 거짓 경보를 냅니다 — 그리고 커버리지 0
feed.py:177-180 은 missing > max_backfill_bars 일 때 history() 를 **부르기 전에** gap 을 기록합니다. "빈 응답 = 휴장" 안전장치가 이 분기만 통째로 우회됩니다.
관측(probe p1, KRX 1m, 정규장 00:00~06:30 UTC):
  금 15:29 → 월 09:00 : history() 호출 0회, unseen_windows 에 3930봉 "못 봤다", degraded True
  월 15:29 → 화 09:00 : 1050봉 "못 봤다"  → 누적 2건
즉 1m/5m/15m 설정에서는 **매일 아침** 거짓 결손이 쌓이고 degraded 가 첫날부터 영구 빨강입니다(위 항목과 겹침). 배포된 config 는 전부 1d/4h 라 오늘 당장 터지지는 않지만, timeframe 은 설정값이고 TossProvider._FIELDS 는 1m 을 지원한다고 적혀 있습니다.
되돌려 확인(MUTANT-A): 이 분기의 _record_gap 을 통째로 지우고 `return []` 만 남겨도 → tests/test_live_bar_feed.py + tests/test_shutdown_and_reporting.py 26 passed. 피드를 건드리는 테스트 파일은 test_live_bar_feed.py 하나뿐이므로(grep 확인) 전체 1236개도 초록입니다. 운영에서 매일 도는 분기를 아무 테스트도 안 지킵니다.

■ 되돌려 확인한 것 전부 (수정 후 두 파일 md5 원복 확인)
잡힘: MUTANT-B 미완성봉 통과 → 2 failed / MUTANT-D mode 상수화 → 2 failed / MUTANT-E 되메운 봉 폐기 → 1 failed / MUTANT-C 밀린 봉 재폐기(옛 동작) → 2 failed / MUTANT-J 최신봉 이중 적재 → 2 failed. 핵심 세 가지(결손·되메우기·미완성봉)와 중복 계상은 실제로 물려 있습니다.
안 잡힘: MUTANT-A(위) / MUTANT-G `fresh[bar.ts]=bar` → `setdefault` (같은 시각이면 **먼저 온 것**을 채택 = 진행 중본이 확정본을 이김) → 26 passed. 소켓 붙는 날 admit() 이 기대야 할 규칙인데, 코드 주석에만 있고 테스트에 없습니다 / MUTANT-K `last_bar_ts = max(...)` → `min(...)` → 26 passed. 노후 판정의 나머지 한 축인데 무방비입니다.

■ 기존 계약 파손은 없음(확인함)
_fetch_new_bars dict→list 는 private, 호출부는 trader 와 테스트뿐. status() 는 "feed" 추가만(제거·타입 변경 없음). /api/strategies 의 realtime 과 index.html 배지는 그대로 — 보고대로 손 안 댔습니다. provider.py 변경은 docstring 뿐(git diff HEAD~1 확인), TossProvider.supports_streaming=False 는 베이스와 동일값이라 무동작. 밀린 봉을 한 번만 판단하는 선택은 LLM 이중 청구를 막는 쪽으로 옳고 test_a_pile_up_still_decides_only_once 가 실제로 물립니다.


---

## `pending/fx-layer` — 통화 환산 계층

상태: `done` · 전체 테스트 통과


### 일부러 안 한 것

· **장부에 물리지 않았습니다.** `Portfolio.apply_fill` / `holdings_value` 는
  그대로입니다. 물리려면 동기 장부를 비동기로 바꾸거나 환율 선조회 경로를
  따로 내야 하는데 둘 다 파급이 크고, 무엇보다 지금은 통화가 섞인 설정이
  시작 단계에서 막히므로 환산이 필요한 상황 자체가 존재하지 않습니다.
  (실제로 `Portfolio` 에 통화 확인을 넣어 보니 KRW 종목을 기본 USD 장부에
  쓰는 기존 테스트들이 깨졌습니다 — "기존 동작을 바꾸지 마세요" 와 정면
  충돌합니다.) `docs/cross_market.md` 2~4단계에서 할 일입니다.

· **`CompositeBrokerage`·종목별 캘린더·설정 스키마 확장** — 지시대로 손대지
  않았습니다. `markets:` 블록도 추가하지 않았습니다.

· **`base_currency` 라벨 어긋남은 거부하지 않고 경고만** 합니다. 처음에는
  거부하게 만들었다가 45개 테스트가 깨졌습니다. 단일 통화면 더하는 값이 전부
  같은 통화라 환산이 끼어들 자리가 없고 `base_currency` 는 오늘 표시에만
  쓰이므로, 산수가 멀쩡한 설정을 막는 쪽이 규칙 위반이라고 판단했습니다.

· **알파에 직접 적는 종목(`pairs`)** 은 검증이 보지 못합니다. 임의 모델의
  `params` 를 뒤지기 시작하면 검증이 모델 내부 규약에 묶입니다.

· **환전 스프레드를 비용으로 물리지 않습니다.** 평가에는 `midRate`(매매기준율)
  를 쓰고 실제 거래 환율은 모델링하지 않습니다.


### 반박


[correctness/high] 핵심 반박: 이번에 **실제로 출하되는 유일한 보호막**은 설정 검증뿐인데(`Fx` 는 프로덕션 코드 어디에서도 생성되지 않습니다 — `grep` 결과 `quant/` 안의 사용처는 `toss_fx.py` 의 import 뿐, 나머지는 전부 `tests/`), 그 검증이 보는 것은 **종목의 통화가 아니라 설정에 적힌 라벨**이고 그 라벨에는 기본값 `"USD"` 가 있습니다(`quant/config/schema.py:63`). 그래서 라벨을 안 적으면 통화가 섞인 책이 **오류도 경고도 없이** 통과합니다.

재현(워크트리는 읽기만 했고 수정 없음, `git status` 깨끗):
```yaml
universe:
  symbols:
    - {ticker: "005930", venue: toss, tick_size: 100,  lot_size: 1}   # 원화
    - {ticker: "AAPL",   venue: toss, tick_size: 0.01, lot_size: 1}   # 달러
portfolio: {starting_cash: 10000000}      # base_currency 기본값 USD
```
`load_config()` → 통과. 로그 캡처 결과 **경고 문자열 자체가 빈 문자열**(`''`)입니다. 두 라벨 모두 `USD` 로 묶여 `by_currency` 가 1개이고, base 도 `USD` 라 라벨 어긋남 경고 분기(`base not in by_currency`)마저 타지 않습니다. 이어서 `build_symbol` → `Portfolio` 에 70,000원 1주 + $250 1주를 넣으면

    holdings_value = 70250.0
    snapshot(): equity = 10,070,250.0, currency = "USD"

— 고치기 전 증거 (1) 에 적힌 바로 그 숫자가, 이번 변경 **후에도** 화면에 자신 있게 뜹니다. `base_currency: KRW` 로만 바꾸면 경고 한 줄(WARNING 로그)은 뜨지만 여전히 통과하고 숫자는 같습니다.

이게 단순한 범위 축소가 아닌 이유:
1. 요약과 `docs/cross_market.md` 머리말이 "통화가 섞인 유니버스는 설정 단계에서 거부됩니다" 라고 **무조건**으로 적었습니다. 실제로는 "정직하게 라벨을 적은 경우에만" 입니다. 라벨을 제대로 적는 사용자는 이미 통화가 다르다는 걸 아는 사용자이고, 걸려야 할 사람은 안 적은 사람입니다.
2. 라벨 어긋남을 경고로 낮춘 근거 문장 — "한 통화짜리 유니버스는 더하는 값이 전부 같은 통화라 환산이 끼어들 자리가 없다"(`schema.py` docstring) — 이 위 반례가 정확히 반증합니다. **라벨이 하나인 것과 통화가 하나인 것은 다른 명제**인데, 검증은 앞을 재고 뒤를 결론냅니다. 45개 테스트를 살리려 규칙을 좁힌 판단 자체는 타당하지만, 좁힌 뒤의 규칙이 참이라는 논거가 성립하지 않습니다.
3. 저장소에 이미 티커→통화 규칙이 있습니다: `quant/brokerage/toss_broker.py:343` `krx = code.isdigit() and len(code) == 6` → `quote_currency="KRW" if krx else "USD"`, `quant/data/providers/kis.py:180` 은 항상 KRW. 즉 "venue=toss/kis + 6자리 숫자 티커인데 라벨이 USD" 는 **검증 시점에 판별 가능한 거짓말**이고, 유니버스에 6자리 숫자 티커와 영문 티커가 함께 있는 것만 봐도 라벨과 무관하게 섞임 신호입니다. 없는 정보를 지어내라는 요구가 아니라, 이미 세 곳에 적혀 있는 규칙을 안 쓴 것입니다.

부차 지적 두 개(둘 다 `Fx` 가 아직 안 물려 있어 오늘 숫자는 안 바뀝니다):
· `quant/data/providers/toss_fx.py:69` 가 `TossProvider` 를 **새로** 만들면서 `_TossClient` 도 새로 생깁니다. 토큰 캐시는 모듈 전역 `_TOKENS` 라 docstring 이 말한 중복은 원래 안 생기지만, **레이트 리밋 페이서 `_gap` 은 인스턴스 필드**(`toss_broker.py:174`)라 시세용 8rps 와 환율용 8rps 가 따로 돕니다. 스펙상 `exchange-rate` 는 시세와 같은 `MARKET_INFO` 리밋 그룹이라 합산 초과 → 429 → (설계대로) `FxUnavailable` 하드 스톱입니다.
· `Fx.quote` 의 `self._locks.pop(key, None)` 이 `try/finally` 밖이라 조회 실패 시 락이 영구히 남습니다. 실패하는 소스로 500분치를 돌려 확인: `cache entries: 0 (max=8)`, `lock entries: 500`. 캐시는 LRU 로 묶여 있는데 락 딕셔너리만 무한 증가합니다.

맞게 되어 있다고 확인한 것(반박거리 아님): 환율 방향(`_source_target` → `baseCurrency=출발/quoteCurrency=도착`)이 공식 스펙 예시(1 USD = 1375 KRW)와 일치, `midRate` 선택과 그 대가의 명시, `_at_minute` 의 UTC 분 절단이 토스의 1분 유효 구간(`validFrom`~`validUntil`, 반열림 비교)과 정합, `dateTime` 의 `+00:00` 이 httpx 에서 `%2B` 로 정상 인코딩됨(공백으로 뭉개져 9시간 밀리는 사고 없음), 0·음수·NaN·빈 문자열·비숫자 환율 전부 예외로 차단, 기준통화 경로에서 곱셈·조회 없음. 전체 스위트 1267 passed 재현 확인.


[money-and-regression/medium] 돈이 오늘 새지는 않습니다(어떤 프로덕션 경로도 `Fx`를 부르지 않고, 검증자는 raise/warn만 합니다). 베이스라인 재현: 1267 passed. 기존 API 필드 삭제·타입 변경 없음(`_FIELDS`에 키 1개 추가뿐). 다만 다음이 반증됩니다.

■ F1 (핵심). "base_currency 라벨 어긋남은 오늘 표시에만 쓰인다"는 거짓이고, 그 어긋남의 반대편이 **주문 라우팅 키**입니다. `SymbolSpec.quote_currency` 기본값이 `"USD"`(schema.py:63)이라, KRX 종목에 통화를 안 적은 설정은 base=KRW인데 유니버스가 전부 USD가 됩니다. 실측:
  StrategyConfig(base_currency="KRW", symbols=[SymbolSpec(ticker="005930", venue="kis")]) → **검증 통과, 로그 WARNING 한 줄뿐**.
 그 결과 —
 · kis_broker.py:132 `domestic = quote_currency == "KRW"` → False → 삼성전자 주문이 TR_OVERSEAS tr_id + 해외주식 주문 경로로 나갑니다.
 · toss_broker.py:454 → MARKET 주문이 "해외 주식은 지정가만 지원합니다"로 거부 → **시장가 청산이 막힙니다**.
 · execution/base.py:362 `_is_krx` → False → 15:20~15:30 종가단일가 취소전용 가드(`_amend_window_closed`)와 KRX 당일소멸(`_session_over`)이 그 종목에 안 걸립니다. 그 파일 자신의 표현대로 "죽은 주문이 projected_quantity에 남아 종목을 조용히 무장해제"합니다.
 · kis_broker.py:328 `_venue_cash`는 base_currency!="KRW"면 None → live_base.py의 "현금 불일치" 경보가 통째로 사라집니다.
 이건 diff가 만든 버그는 아니지만, diff는 **바로 그 어긋남을 직접 들여다보고** 경고로 낮췄고 그 근거를 코드 주석·docs·테스트(`test_a_single_currency_book_still_starts_whatever_its_label_says`)에 "표시에만"으로 못박았습니다. 다음 사람이 그 문장을 믿습니다.

■ F2. 새 경고에 스로틀이 없습니다. `UserRegistry.prepare()`가 `StrategyConfig.model_validate`를 다시 돌리고(registry.py:561), `data_provider()`가 요청마다 `prepare()`를 부릅니다(registry.py:580; server.py:1515/1668/1778/1791). 실측: 같은 설정 500회 검증 → **WARNING 501줄**. 대시보드 폴링 내내 쌓여서 정작 봐야 할 경고(`미체결 %d봉 — 취소를 요청합니다`, `현금 불일치`, `UNCORRECTED position drift`)를 묻습니다. 같은 저장소에 `_RENOTIFY_EVERY`, `self._cash_warned` 같은 "한 번만 말하기" 장치가 이미 있는데 새 경고만 없습니다.

■ F3. `Fx._locks`에 상한이 없습니다. `quote()`의 `self._locks.pop(key, None)`이 `async with lock` **뒤**라 `_fetch`가 던지면 실행되지 않습니다. 실측(max_entries=8, 2,000분 연속 실패): `_cache` 0, `_locks` **2,000**. 규칙2가 작동하는 바로 그 상황 — 환율 소스가 죽었는데 라이브 봇은 계속 도는 상황 — 에서만 새고, 통화당 하루 1,440개입니다.

■ F4 (되돌림 확인). `_at_minute`의 tz-naive 가드는 **어떤 테스트도 지키지 않습니다**. `if when.tzinfo is None: when = when.replace(tzinfo=UTC)` 두 줄만 지우고 전체 스위트를 돌리면 그 변이는 통과합니다(같이 넣은 다른 두 변이만 실패, 1265 passed). TZ=Asia/Seoul에서 `_at_minute(datetime(2026,3,25,0,30))` → 원본 `2026-03-25T00:30Z`, 가드 제거 후 `2026-03-24T15:30Z` — **9시간·하루 경계**가 틀린 환율. 이 모듈의 존재 이유가 "그 시각의 환율"인데 그 시각을 정하는 유일한 함수의 절반이 무방비입니다. (대조군: `covers()` 불일치 경고 제거 → 잡힘, 라벨 어긋남 경고 제거 → 잡힘. 저자가 신고한 네 변이도 테스트가 무는 것을 확인했습니다.) 모든 변이는 실행 후 복원했고 `git status` 깨끗함을 확인했습니다.

■ F5. 토스 응답의 통화 쌍 확인이 필드 누락에 무력합니다: `got = ((data.get("baseCurrency") or source).upper(), ...)` — 응답에서 두 필드가 빠지거나 null이면 요청값으로 대체되어 검사가 **항상 통과**합니다. 이건 "1,380 대신 1/1,380"을 막는 유일한 장치인데, 스펙이 required로 둔 필드가 사라지는 상황이 곧 뒤집힘이 생길 상황입니다. 없으면 `FxUnavailable`이 맞습니다.

■ 반증 실패(인정). "설정 검증 뒤로는 통화가 섞인 장부가 생기지 않는다"는 전제를 깨려고 다섯 경로를 팠지만 전부 막혀 있었습니다: `ExchangeSource`가 quote로 필터(universe.py:123), `HeldPositionFilter`는 이미 장부에 있는 것만 재추가, `live_base.sync()`는 모르는 종목을 채택하지 않고 `uncorrected`로 던짐(live_base.py:313), `restore_positions`는 유니버스 밖 종목 skip, 수동주문 `_resolve`는 유니버스∪보유로 제한. 장부 미연동·스프레드 미반영·`pairs` 미검증은 정직하게 적힌 scope cut이라 결함으로 세지 않았습니다. 단일통화 비트동일 테스트는 오늘 아무것도 지키지 못하지만(어떤 경로도 `Fx`를 안 부름) 미래 연동용 카나리로는 타당합니다. 스펙 대조 결과 `midRate`=매매기준율, `rate`=매수환율, `dateTime` 선택, MARKET_INFO 그룹은 이 저장소에서 exchange-rate 외에 쓰는 곳이 없어 레이트리밋 경합도 없습니다 — 이 부분들은 맞습니다.
