"""슬리브 — 에이전트 하나에게 "네 몫만 보이는 계좌" 를 건네는 어댑터.

한 계좌에서 성향이 다른 에이전트 넷을 굴리려면, 각 엔진이 자기 몫만 볼 수 있어야
합니다. 그런데 `Engine` 은 `Portfolio` 하나와 `Brokerage` 하나를 전제로 짜여
있고, 거기에 슬리브 개념을 넣으면 alpha·risk·execution·portfolio 모델이 전부
영향을 받습니다.

그래서 엔진을 고치는 대신 **엔진이 받는 브로커를 바꿉니다.** `Brokerage` 추상
인터페이스는 `submit / cancel / open_orders / positions / balances / sync /
connect / close` 여덟 개뿐이라, 그것을 구현한 얇은 층 하나면 엔진은 자기가 계좌
하나를 통째로 쓴다고 믿은 채로 돕니다. 엔진·알파·리스크·실행 모델은 한 줄도
바뀌지 않습니다.

이 층이 지키는 것은 정확히 하나입니다:

    **에이전트는 자기 슬리브 수량 이상 팔 수 없다.**

증권사는 005930 을 합계 수량 하나로만 알려줍니다. 공격형이 10주, 보수형이
10주를 들고 있으면 계좌에는 20주가 있을 뿐이고, "누구의 10주인가" 는 우리
원장에만 있는 사실입니다. 공격형의 손절이 20주를 팔면 보수형은 자기가 아무것도
하지 않았는데 포지션이 사라집니다 — 그 손실은 원장 어디에도 이유가 남지 않습니다.

**클램프이지 거절이 아닙니다.** 자기 물량을 넘는 매도는 자기 물량까지 줄여서
내보냅니다. 거절하면 공격형의 손절이 통과하지 못하고 손실 포지션에 갇히는데,
`quant.live.limits` 가 적어 둔 대로 *"빠져나오지 못하게 하는 한도는 안전장치가
아닙니다"*. 줄여서 내보내면 자기 포지션은 온전히 정리되고 남의 물량은 그대로
남습니다 — 두 요구가 동시에 만족되는 유일한 지점입니다.

**공매도는 아예 거절합니다.** 처음에는 "공매도를 허용한 전략에서 보유량 초과
매도는 진입이니 클램프하지 않는다" 로 두었는데, 그러면 클램프에 상한이 사라져
`allow_short` 하나로 이 층 전체가 무력해집니다. 더 근본적으로는 표현이 불가능합니다
— a1 이 10주를 공매도하고 a2 가 10주를 들고 있으면 증권사 순수량은 0 이고, 합계
불변식은 성립하지만 **a2 의 10주는 계좌에 없습니다.** 조용히 허용한 결과는
"공매도가 된다" 가 아니라 "한 에이전트가 다른 에이전트의 주식을 소비한다" 입니다.
토스·한국투자 현금계좌는 공매도를 주지 않으므로 지금 잃는 기능도 없습니다.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from quant.brokerage.base import Brokerage, BrokerageError
from quant.core.types import Fill, Order, OrderSide, OrderStatus, RunMode, utcnow

if TYPE_CHECKING:  # pragma: no cover - 순환 임포트를 피하기 위한 타입 전용
    from quant.core.types import OrderType, Symbol

log = logging.getLogger("quant.brokerage.sleeve")


class SleeveGateway(Protocol):
    """`SleeveBrokerage` 가 계좌 층에 요구하는 전부.

    좁게 적어 둡니다 — 슬리브가 게이트웨이의 다른 부분에 손을 뻗기 시작하면
    "자기 몫만 본다" 는 이 층의 유일한 약속이 조용히 깨집니다.
    """

    async def submit_for(self, agent_id: str, order: Order) -> Order: ...
    async def cancel_for(self, agent_id: str, order: Order) -> bool: ...
    async def open_orders_for(self, agent_id: str) -> list[Order]: ...
    async def poll_fills_for(self, agent_id: str) -> list[Fill]: ...
    def drain_pending_fills_for(self, agent_id: str) -> list[Fill]: ...
    def fill_channel_down(self, reason: str) -> None: ...
    def fill_channel_up(self) -> None: ...
    async def sync_for(self, agent_id: str) -> dict: ...
    def sleeve_positions(self, agent_id: str) -> dict[str, Decimal]: ...
    def sleeve_balances(self, agent_id: str) -> dict[str, float]: ...
    def exact_flatten_order_type_for(
        self, symbol: Symbol, current_quantity: Decimal,
        target_quantity: Decimal,
    ) -> OrderType | None: ...


class SleeveBrokerage(Brokerage):
    """한 에이전트가 보는 계좌. 실제 증권사와는 게이트웨이를 통해서만 만납니다.

    `LiveBrokerage` 를 상속하지 **않습니다.** 그쪽에는 계좌 전체를 자기 자본으로
    채택하는 경로(`venue_capital_truth`)와 계좌 단위 복구 격리가 들어 있고, 그
    둘은 계좌에 하나만 있어야 하는 것들입니다. 슬리브 넷이 각자 계좌 진실을
    채택하면 같은 현금이 네 번 계산됩니다. 계좌 진실은 게이트웨이만 압니다.

    **그렇다고 `LiveTrader` 의 모든 `isinstance(brokerage, LiveBrokerage)` 분기가
    꺼져도 되는 것은 아닙니다.** 그 검사는 두 가지를 한꺼번에 결정하고 있었습니다
    — 계좌 자본을 채택해도 되는가(슬리브는 아니다), 그리고 **주문이 진짜 증권사로
    나가는가**(슬리브도 그렇다). 뒤쪽에 매달린 것이 봉 사이 안전 작업 전부입니다:
    호가 갱신(trader.py:974), 손절·낙폭 재평가(:982), 체결 폴링(:712), 제출 직전
    관문(:116). 이것들이 함께 꺼지면 일봉 전략의 손절은 **하루에 한 번** 만
    평가되고, 봉 사이 체결은 영영 장부에 오르지 않으며, 주문은 마지막 관문 없이
    나갑니다 — 예외도 로그도 없이. 그래서 뒤쪽 질문은 `venue_backed` 라는 별도
    능력 플래그로 갈라져 있고, 슬리브는 거기에 **예** 라고 답합니다.
    """

    #: 슬리브는 계좌 자본을 채택하지 않습니다. `LiveBrokerage` 의 같은 이름
    #: 속성을 읽는 코드가 슬리브를 만나도 올바른 답을 얻도록 명시해 둡니다.
    venue_capital_truth = False
    uses_venue_capital = False

    def __init__(
        self,
        agent_id: str,
        gateway: SleeveGateway,
        *,
        mode: RunMode = RunMode.DRY_RUN,
        allow_short: bool = False,
        name: str = "",
    ):
        if allow_short:
            # 한 계좌를 나눠 쓰는 동안에는 공매도를 표현할 방법이 없습니다.
            #
            # a1 이 10주를 공매도하고 a2 가 10주를 들고 있으면 증권사의 순수량은
            # 0 입니다. 슬리브 원장은 a1 −10, a2 +10 이라 합계 불변식은 성립하지만,
            # **a2 의 10주는 계좌에 더 이상 존재하지 않습니다.** a2 가 그것을 팔려
            # 하면 팔 물건이 없고, 그 사실은 우리 원장 어디에도 나타나지 않습니다.
            #
            # 조용히 허용하면 결과는 "공매도가 된다" 가 아니라 "한 에이전트가 다른
            # 에이전트의 주식을 소비한다" 입니다 — 이 층이 막으려고 존재하는 바로
            # 그 사고. 토스·한국투자 현금계좌는 애초에 공매도를 주지 않으므로
            # 지금 잃는 기능도 없습니다. 표현할 수 없는 것은 거절합니다.
            raise BrokerageError(
                f"{agent_id}: 여러 에이전트가 한 계좌를 나눠 쓰는 동안에는 "
                f"공매도를 켤 수 없습니다. 한 에이전트의 공매도는 같은 종목을 든 "
                f"다른 에이전트의 보유를 계좌에서 없애는데, 그 사실을 원장이 "
                f"표현하지 못합니다. 전략 설정에서 portfolio.allow_short 를 끄세요."
            )
        self.agent_id = agent_id
        self.gateway = gateway
        self.allow_short = False
        self.run_mode = mode
        #: 진짜 돈인지. 게이트웨이가 최종 판정하지만, 배너·화면·이벤트가 이
        #: 값을 읽으므로 슬리브도 자기 등급을 알고 있어야 합니다.
        self.live = mode is RunMode.LIVE
        self.name = name or f"sleeve[{agent_id}]"
        # `Engine.__init__` 이 자기 budget/portfolio 를 여기에 꽂습니다. 즉 이
        # 슬리브가 보는 한도와 장부는 **그 에이전트의 것** 입니다.
        self.budget = None
        self.portfolio = None
        #: `LiveTrader._bind_submission_guard` 가 여기에 꽂습니다. 이것이 없으면
        #: 슬리브로 도는 에이전트는 장 마감·회계 격리·호가 노후를 보는 마지막
        #: 관문 없이 주문을 냅니다 — 아무 예외도 로그도 없이.
        self._submission_guard: Callable[[Order], str] | None = None

    # ── 실제 증권사를 등에 업고 있는가 ───────────────────────────────────
    @property
    def venue_backed(self) -> bool:
        """이 슬리브의 주문이 진짜 증권사로 나가는가.

        `LiveTrader` 의 봉 사이 작업(호가 갱신·체결 폴링·손절 재평가·제출 가드)이
        전부 이 값을 봅니다. 슬리브가 `LiveBrokerage` 가 아니라는 이유로 그
        작업들이 꺼지면, 일봉 전략의 손절은 **하루에 한 번** 만 평가됩니다.
        """
        return bool(getattr(self.gateway, "venue_backed", False))

    # ── 매도 클램프 ──────────────────────────────────────────────────────
    def clamp_to_sleeve(self, order: Order) -> tuple[Order, Decimal]:
        """자기 슬리브 보유량을 넘는 매도를 자기 몫까지 줄인다.

        줄어든 수량과 함께 돌려줍니다(줄지 않았으면 0). 호출자가 그 사실을
        기록할 수 있어야 합니다 — 조용히 줄이면 사이징 버그가 "왜 다 안
        팔렸지" 로만 보이고 원인은 어디에도 남지 않습니다.
        """
        trimmed = Decimal("0")
        if order.side is not OrderSide.SELL:
            return order, trimmed
        held = self._sleeve_quantity(order.symbol)
        if order.quantity <= held:
            return order, trimmed

        allowed = order.symbol.round_qty(max(held, Decimal("0")))
        trimmed = order.quantity - allowed
        if allowed <= 0:
            raise BrokerageError(
                f"{self.agent_id}: {order.symbol} 을(를) {order.quantity} 팔려 "
                f"했으나 이 에이전트의 보유는 {held} 입니다 — 같은 종목의 다른 "
                f"에이전트 물량은 팔 수 없습니다"
            )
        log.warning(
            "%s: %s 매도 %s → %s 로 줄였습니다 (이 에이전트 보유 %s). "
            "나머지는 같은 종목을 든 다른 에이전트의 물량입니다",
            self.agent_id, order.symbol, order.quantity, allowed, held,
        )
        order.quantity = allowed
        order.meta = {**(order.meta or {}),
                      "sleeve_trimmed": str(trimmed),
                      "sleeve_held": str(held)}
        return order, trimmed

    def _sleeve_quantity(self, symbol: Symbol) -> Decimal:
        """이 에이전트가 팔 수 있는 최대 수량. **둘 중 작은 쪽입니다.**

        후보가 둘 있습니다. 엔진의 장부(`self.portfolio`)는 방금 체결을 반영한
        값이라 더 최신이고, 게이트웨이의 슬리브 원장은 계좌 귀속의 진실입니다.
        둘이 어긋날 때 큰 쪽을 믿으면 안 됩니다.

        장부를 그냥 믿으면 안 되는 구체적인 경로가 있습니다. 증권사 어댑터는
        `_sync_once` 에서 **계좌 전체** 의 수량과 현금을 자기 `portfolio` 에 적는데
        (`live_base.py`), 그 portfolio 가 실수로 어느 에이전트의 장부이면 그
        에이전트의 장부에 계좌 합계가 들어앉습니다. 그러면 여기서 20주가 아니라
        520주가 나오고, 클램프는 통과이고, 손절 하나가 다른 에이전트와 사용자의
        보유까지 전부 팔아 버립니다. 합계 불변식도 이것을 잡지 못합니다 — 나간
        520주가 발주 에이전트에게 귀속되므로 Σ 는 여전히 맞습니다.

        그래서 **작은 쪽** 을 씁니다. 클램프는 원장보다 큰 수를 읽을 수 없어야
        합니다.
        """
        ledger = self.gateway.sleeve_positions(self.agent_id).get(
            symbol.key, Decimal("0"))
        if self.portfolio is None:
            return ledger
        book = self.portfolio.quantity(symbol)
        if book > ledger:
            log.warning(
                "%s: %s 장부 %s 가 슬리브 원장 %s 보다 큽니다 — 원장을 씁니다. "
                "계좌 합계가 이 에이전트 장부에 들어왔을 수 있습니다",
                self.agent_id, symbol, book, ledger,
            )
            return ledger
        return book

    # ── 제출 직전 관문 ───────────────────────────────────────────────────
    def set_submission_guard(self, guard: Callable[[Order], str] | None) -> None:
        """마지막 경계에서 도는 동기 관문을 건다.

        `LiveBrokerage` 와 같은 계약입니다: 통과면 빈 문자열, 아니면 사람이 읽는
        거절 사유. 에이전트마다 따로 겁니다 — 장 마감 여부는 계좌 공통이지만
        호가 노후와 회계 격리는 그 에이전트의 판단 주기에 매인 사실입니다.
        """
        self._submission_guard = guard

    def _submission_guard_error(self, order: Order) -> str:
        guard = self._submission_guard
        if guard is None:
            return ""
        try:
            return str(guard(order) or "")
        except Exception as exc:  # noqa: BLE001 - 불확실하면 막는 쪽으로 닫습니다
            log.exception("%s: 제출 가드가 실패했습니다 (%s)",
                          self.agent_id, order.symbol.ticker)
            return f"주문 직전 안전 상태를 확인하지 못했습니다: {exc}"

    # ── 체결 채널 ────────────────────────────────────────────────────────
    #
    # 체결이 보이지 않는 어댑터는 눈을 감고 거래하는 것이라 새 주문을 멈춥니다.
    # 그 사실은 **계좌 전체의 사실** 입니다 — 채널이 죽으면 네 에이전트가 다
    # 눈을 감은 것이므로, 상태는 게이트웨이가 한 벌만 들고 있습니다.
    @property
    def fill_channel_ok(self) -> bool:
        return bool(getattr(self.gateway, "fill_channel_ok", True))

    @property
    def fill_channel_error(self) -> str:
        return str(getattr(self.gateway, "fill_channel_error", "") or "")

    def fill_channel_down(self, reason: str) -> None:
        self.gateway.fill_channel_down(f"[{self.agent_id}] {reason}")

    def fill_channel_up(self) -> None:
        self.gateway.fill_channel_up()

    def drain_pending_fills(self) -> list[Fill]:
        """어댑터가 이미 검증해 둔 이 에이전트 몫의 체결. 네트워크를 타지 않습니다."""
        return self.gateway.drain_pending_fills_for(self.agent_id)

    # ── Brokerage 인터페이스 ─────────────────────────────────────────────
    async def submit(self, order: Order) -> Order:
        self.validate(order)
        order, _ = self.clamp_to_sleeve(order)
        blocked = self._submission_guard_error(order)
        if blocked:
            order.status = OrderStatus.REJECTED
            order.reject_reason = blocked
            order.updated_at = utcnow()
            return order
        # 한도는 두 겹입니다. 여기서 걸리는 것은 **이 에이전트의** 하루 한도이고,
        # 계좌 전체 한도는 게이트웨이가 봅니다. 순서가 이래야 하는 이유는
        # 에이전트 한도를 넘긴 주문이 계좌 한도를 소모하지 않기 때문입니다.
        ok, reason = self._budget_check(order)
        if not ok:
            order.status = OrderStatus.REJECTED
            order.reject_reason = reason
            order.updated_at = utcnow()
            return order
        submitted = await self.gateway.submit_for(self.agent_id, order)
        if submitted.status is not OrderStatus.REJECTED:
            self._budget_record(submitted)
        return submitted

    async def cancel(self, order: Order) -> bool:
        return await self.gateway.cancel_for(self.agent_id, order)

    async def open_orders(self) -> list[Order]:
        return await self.gateway.open_orders_for(self.agent_id)

    async def poll_fills(self) -> list[Fill]:
        """이 에이전트 몫의 체결. 엔진이 이것으로 자기 장부를 적습니다.

        `Engine.settle_live_fills` 가 이 이름을 찾습니다. 슬리브가 이것을
        구현하지 않으면 엔진은 체결을 영영 받지 못하고, 주문은 나가는데
        포지션은 생기지 않는 상태로 계속 같은 주문을 냅니다.

        게이트웨이가 증권사를 한 번만 훑고 에이전트별로 갈라 두므로, 넷이
        각자 불러도 각자 자기 것만 가져갑니다.
        """
        return await self.gateway.poll_fills_for(self.agent_id)

    async def positions(self) -> dict[str, Decimal]:
        """이 에이전트의 슬리브 수량. **계좌 합계를 돌려주면 안 됩니다.**

        여기서 합계를 돌려주는 순간 엔진의 재조정이 남의 물량을 자기 것으로
        채택하고, 다음 손절이 그것까지 팝니다. 이 한 줄이 그 사고를 막습니다.
        """
        return self.gateway.sleeve_positions(self.agent_id)

    async def balances(self) -> dict[str, float]:
        return self.gateway.sleeve_balances(self.agent_id)

    async def sync(self) -> dict:
        return await self.gateway.sync_for(self.agent_id)

    async def connect(self) -> None:
        """아무것도 하지 않습니다 — 연결은 계좌에 하나뿐입니다.

        슬리브마다 연결하면 같은 증권사에 네 개의 세션이 열리고, 인증 토큰
        재발급이 서로를 무효화합니다. 게이트웨이가 그룹 시작 시 한 번 겁니다.
        """
        return None

    async def close(self) -> None:
        """마찬가지로 게이트웨이가 그룹 종료 시 한 번 닫습니다."""
        return None

    def exact_flatten_order_type(
        self,
        symbol: Symbol,
        current_quantity: Decimal,
        target_quantity: Decimal,
    ) -> OrderType | None:
        """정확 청산 경로는 증권사의 능력이므로 게이트웨이에 물어봅니다."""
        return self.gateway.exact_flatten_order_type_for(
            symbol, current_quantity, target_quantity)

    def status(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "mode": self.run_mode.value,
            "allow_short": self.allow_short,
        }
