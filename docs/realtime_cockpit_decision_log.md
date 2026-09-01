# 근실시간 거래 cockpit 결정 로그

작성일: 2026-09-01

## 목표와 등급

- 등급: Production 개선 후보. 실제 주문 wire 검증이 끝나기 전까지 실거래 시작은 NO-GO다.
- 목표: 픽셀아트 정체성을 유지하면서 계좌, 호가, 최근 체결, 주문 상태를 실제 출처 시각과 함께 근실시간으로 보여준다.
- 비목표: 이번 변경에서 새 비트코인 거래소를 연결하거나 실주문을 발생시키지 않는다.

## 결정

1. **안전 수정이 화면 개선보다 먼저다.**
   - 근거: 현재 일봉 scheduler는 장 상태 재확인과 체결 polling이 candle 주기에 묶여 있다.
   - 폐기한 대안: UI polling만 빠르게 만들어 내부 장부 지연을 숨기는 방식.

2. **시세의 source of truth는 Toss 공식 REST OpenAPI 1.2.14와 AsyncAPI 1.2.2다.**
   - 근거: 저장소 문서가 가리키던 `scratchpad/toss_openapi.json`은 현재 checkout에 없다. 공식 서버 문서가 orderbook, trades, personal order stream과 한도를 명시한다.
   - 폐기한 대안: 반박된 `origin/pending/toss-websocket`의 diff 또는 과거 추정 protocol 재사용.

3. **첫 전달은 coalesced REST snapshot을 기본 경로로 사용한다.**
   - 근거: 초기 snapshot은 WebSocket에서도 REST 선조회가 필요하고, active symbol 하나를 2초 간격으로 조회하면 공식 MARKET_DATA 한도 안에서 실패와 복구를 명확히 표현할 수 있다.
   - 대안: 서버 WebSocket collector. lifecycle, 재연결, 사용자별 token 격리, 다중 탭 fan-out을 독립 테스트로 증명한 뒤 같은 snapshot 계약 뒤에 교체한다.

4. **실시간성은 색이 아니라 숫자로 표시한다.**
   - 근거: `source timestamp`, 수신 경과시간, `live/delayed/stale/error`를 함께 보여야 조용한 단절과 장 휴장을 구분할 수 있다.
   - 폐기한 대안: 연결 여부만 나타내는 초록색 점.

5. **디자인은 `8px Ledger Cockpit`으로 확장한다.**
   - 근거: 픽셀아트는 유지하되 본문은 가독성 높은 system sans, 숫자는 mono, 짧은 상태 라벨만 pixel display font를 사용한다.
   - 폐기한 대안: 모든 글자를 pixel font로 두거나 카드 수를 늘려 정보를 분산하는 방식.

6. **자동매매 준비 판정과 시세 화면 완성 판정을 분리한다.**
   - 근거: 호가와 체결이 잘 보이는 것과 실제 주문 lifecycle이 검증된 것은 다른 성질이다.
   - 결과: read-only 시장 화면은 배포할 수 있어도, live bot은 dry-run 체결과 실제 최소 주문 wire 검증 전까지 계속 꺼 둔다.

## 현장 실패 조건

가장 그럴듯한 실패는 브라우저 탭 여러 개가 같은 계정으로 동시에 polling하여 Toss rate limit을 소진하는 경우다. 서버에서 같은 사용자, 전략, 종목 요청을 짧게 coalesce하고 429의 `Retry-After`를 freshness 응답으로 전파한다. 화면은 backoff 동안 이전 값을 현재값처럼 표시하지 않는다.
