"""계좌 게이트웨이 — 에이전트 넷과 증권사 하나 사이의 유일한 통로.

에이전트는 각자 자기 장부를 갖지만 **계좌는 하나** 입니다. 그 하나뿐인 것들이
전부 여기 삽니다:

  · 증권사 연결 하나 (넷이 각자 붙으면 토큰 재발급이 서로를 무효화한다)
  · 계좌 자본의 진실 (넷이 각자 채택하면 같은 현금이 네 번 계산된다)
  · **계좌 단위 하루 한도** (에이전트마다 한도를 두면 방어선이 봇 수만큼 곱해진다)
  · 슬리브 원장 — "005930 20주 중 누구의 10주인가"

마지막 것이 이 모듈의 심장입니다. 증권사는 종목별 **합계 수량 하나** 만
알려줍니다. 공격형 10주와 보수형 10주는 계좌에서 구별되지 않고, 그 구별은 오직
우리 원장에만 존재합니다. 원장이 틀리면 틀렸다는 사실을 알려 줄 바깥 세계가
없습니다 — 그래서 매 동기화마다 다음을 확인합니다:

    Σ 에이전트 슬리브(종목) + 미귀속(종목) == 증권사 합계(종목)

**어긋나면 그룹 전체를 멈춥니다.** 어느 슬리브가 틀렸는지 알 방법이 없기
때문입니다. 셋은 맞고 하나만 틀렸을 수도, 넷 다 조금씩 틀렸을 수도 있습니다.
한 에이전트만 멈추는 것은 "누가 틀렸는지 안다" 는 뜻인데 우리는 모릅니다.
계속 돌리는 것은 더 나쁩니다 — 다음 손절이 남의 물량을 팝니다.

**미귀속(unassigned)** 은 봇이 만들지 않은 보유입니다. 사용자가 앱에서 직접 산
주식, 이전에 다른 전략으로 산 것들. 어느 에이전트도 팔 수 없고 사이징에도
쓰이지 않지만, 합계에는 분명히 들어 있으므로 불변식이 그것을 알아야 합니다.
이것이 없으면 기존 보유가 있는 계좌는 시작하자마자 불변식 위반으로 멈춥니다.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from quant.brokerage.base import Brokerage
from quant.core.aio import LazyLock
from quant.core.types import Fill, Order, OrderStatus, RunMode, Symbol, utcnow
from quant.live.agents import AgentGroup
from quant.live.limits import TradingBudget

log = logging.getLogger("quant.live.gateway")

#: 수량 비교 허용 오차. 소수점 수량을 쓰는 종목(암호화폐)에서 부동소수 왕복이
#: 남기는 먼지만 흡수합니다. 주식 한 주는 절대 이 안에 들어오지 않습니다.
QUANTITY_EPSILON = Decimal("0.00000001")


class GroupHalted(RuntimeError):
    """슬리브 원장과 계좌가 갈라져 그룹 전체가 멈췄다.

    이 예외는 사람이 읽는 문장이 그대로 화면에 나갑니다. 어느 종목이 어떻게
    어긋났는지가 그 문장에 들어 있어야 사용자가 증권사 앱에서 확인할 수
    있습니다.
    """

    def __init__(self, message: str, drift: dict | None = None):
        super().__init__(message)
        self.drift = drift or {}


class AccountGateway:
    """에이전트 여럿과 증권사 하나 사이의 통로. 계좌의 진실을 여기서만 압니다."""

    def __init__(
        self,
        group: AgentGroup,
        venue: Brokerage,
        *,
        master_budget: TradingBudget | None = None,
        base_currency: str = "KRW",
        allocation_quantum: str = "1",
    ):
        self.group = group
        self.venue = venue
        #: 계좌 단위 한도. 에이전트 한도보다 **나중에** 검사되지만 더 강합니다 —
        #: 에이전트 넷이 각자 자기 한도 안에 있어도 계좌 합계는 넘을 수 있고,
        #: 실제로 돈이 나가는 곳은 계좌입니다.
        self.master_budget = master_budget or TradingBudget()
        self.base_currency = base_currency
        self.allocation_quantum = allocation_quantum

        #: agent_id → {symbol.key: 부호 있는 수량}
        self._sleeves: dict[str, dict[str, Decimal]] = {
            agent_id: {} for agent_id in group.ids
        }
        #: 봇이 만들지 않은 보유. 어느 에이전트도 팔 수 없습니다.
        self._unassigned: dict[str, Decimal] = {}
        #: agent_id → 배분된 현금
        self._allocations: dict[str, float] = dict.fromkeys(group.ids, 0.0)
        #: order.id → agent_id. 체결이 돌아왔을 때 누구 것인지 아는 유일한 길.
        self._order_agent: dict[str, str] = {}
        #: agent_id → 아직 그 엔진이 가져가지 않은 체결.
        #:
        #: 증권사는 한 번만 폴링합니다. 슬리브마다 폴링하면 같은 체결을 넷이
        #: 각자 받거나(사배), 먼저 부른 하나가 나머지 셋의 체결까지 비워
        #: 갑니다. 처음 부른 슬리브가 증권사를 훑어 여기에 갈라 담고, 나머지는
        #: 자기 몫만 꺼내 갑니다.
        self._fill_buffer: dict[str, list[Fill]] = {}
        #: 마지막으로 관측한 계좌 자산.
        self._account_equity: float = 0.0
        self._connected = False
        #: 계좌 한도의 확인·발주·기록을 하나로 묶는 잠금. 없으면 네 에이전트가
        #: 확인과 기록 사이의 네트워크 왕복 안으로 동시에 들어갑니다.
        self._submit_lock = LazyLock()
        #: 시작 시점의 미귀속 채택을 한 번만 하기 위한 표시. 매번 채택하면
        #: `unassigned := 증권사 − Σ슬리브` 가 불변식을 항상 참으로 만들어,
        #: 불변식이 잡으라고 있는 바로 그 사건을 지워 버립니다.
        self._unassigned_adopted = False

        self._halted_reason = ""
        self._halt_drift: dict = {}

    # ── 정지 ─────────────────────────────────────────────────────────────
    @property
    def halted(self) -> bool:
        return bool(self._halted_reason)

    @property
    def halt_reason(self) -> str:
        return self._halted_reason

    def halt(self, reason: str, drift: dict | None = None) -> None:
        """그룹 전체를 멈춘다. 되돌리는 것은 사람만 할 수 있습니다.

        스스로 풀리게 두지 않습니다 — 다음 동기화에서 우연히 숫자가 맞아떨어져도
        그 사이에 무슨 일이 있었는지는 여전히 모릅니다.
        """
        if self._halted_reason:
            return
        self._halted_reason = reason
        self._halt_drift = drift or {}
        log.error("계좌 그룹 정지: %s", reason)

    def _assert_running(self) -> None:
        if self.halted:
            raise GroupHalted(self._halted_reason, self._halt_drift)

    # ── 자본 배분 ────────────────────────────────────────────────────────
    def allocate_capital(self, account_equity: float) -> dict[str, float]:
        """계좌 자산을 에이전트 몫으로 나눈다. 합은 계좌를 넘지 않습니다."""
        self._account_equity = float(account_equity)
        self._allocations = self.group.allocate(
            account_equity, quantum=self.allocation_quantum)
        log.info("자본 배분 (계좌 %.0f): %s", account_equity,
                 ", ".join(f"{k} {v:.0f}" for k, v in self._allocations.items()))
        return dict(self._allocations)

    @property
    def account_equity(self) -> float:
        return self._account_equity

    # ── 슬리브 원장 ──────────────────────────────────────────────────────
    def sleeve_positions(self, agent_id: str) -> dict[str, Decimal]:
        """이 에이전트가 든 수량. 0 은 지워서 돌려줍니다."""
        return {key: qty for key, qty in self._sleeves.get(agent_id, {}).items()
                if qty != 0}

    def sleeve_balances(self, agent_id: str) -> dict[str, float]:
        return {self.base_currency: self._allocations.get(agent_id, 0.0)}

    def unassigned_positions(self) -> dict[str, Decimal]:
        return {key: qty for key, qty in self._unassigned.items() if qty != 0}

    def adopt_unassigned(self, venue_positions: dict[str, Decimal]) -> dict[str, Decimal]:
        """시작 시점의 계좌 보유를 미귀속으로 받는다.

        봇이 만들지 않은 보유입니다 — 사용자가 앱에서 직접 산 것이거나 이전
        전략의 잔재. 어느 에이전트도 팔 수 없고 사이징에도 쓰이지 않지만,
        증권사 합계에는 분명히 들어 있으므로 불변식이 그것을 알아야 합니다.
        이 단계가 없으면 기존 보유가 있는 계좌는 시작하자마자 멈춥니다.
        """
        if self._unassigned_adopted:
            # 채택은 그룹 수명당 한 번뿐입니다. 매번 하면 미귀속이 언제나
            # `증권사 − Σ슬리브` 로 다시 계산되어 불변식이 항상 참이 됩니다 —
            # 누가 우리 물량을 옮겼다는 사실을 매 시작마다 지우는 셈입니다.
            raise GroupHalted(
                "미귀속 채택은 그룹당 한 번만 할 수 있습니다 — 다시 채택하면 "
                "원장과 계좌의 차이가 조용히 사라집니다."
            )

        assigned = self.aggregate_sleeves()
        adopted: dict[str, Decimal] = {}
        short: dict[str, dict] = {}
        for key in sorted(set(venue_positions) | set(assigned)):
            observed = Decimal(str(venue_positions.get(key, 0)))
            leftover = observed - assigned.get(key, Decimal("0"))
            if leftover > 0:
                adopted[key] = leftover
            elif leftover < 0:
                # **음수 잔여는 미귀속이 아닙니다.** 우리 원장이 주장하는 수량이
                # 계좌에 있는 것보다 많다는 뜻이고, 그것은 "봇이 만들지 않은
                # 보유" 가 아니라 사라진 보유입니다. 여기서 음수를 받아 적으면
                # 합계는 맞아떨어지고 도난은 지워집니다 — 불변식이 잡으라고
                # 있는 단 하나의 사건이 채택 단계에서 소멸합니다.
                short[key] = {"원장": str(assigned.get(key, Decimal("0"))),
                              "증권사": str(observed), "부족": str(-leftover)}

        if short:
            detail = "; ".join(f"{k} 원장 {v['원장']} · 증권사 {v['증권사']}"
                               for k, v in short.items())
            self.halt(
                f"시작 시점에 원장이 주장하는 보유가 계좌에 없습니다 — {detail}. "
                f"봇이 멈춰 있는 동안 누군가 팔았거나 체결이 유실됐습니다. "
                f"증권사 앱에서 보유와 체결을 확인한 뒤 다시 시작하세요.",
                short,
            )
            raise GroupHalted(self._halted_reason, short)

        self._unassigned = adopted
        self._unassigned_adopted = True
        if self._unassigned:
            log.info("봇이 만들지 않은 보유를 미귀속으로 둡니다 (매도 대상 아님): %s",
                     {k: str(v) for k, v in self._unassigned.items()})
        return dict(self._unassigned)

    def aggregate_sleeves(self) -> dict[str, Decimal]:
        """Σ 에이전트 슬리브. 미귀속은 포함하지 않습니다."""
        total: dict[str, Decimal] = {}
        for book in self._sleeves.values():
            for key, qty in book.items():
                total[key] = total.get(key, Decimal("0")) + qty
        return {key: qty for key, qty in total.items() if qty != 0}

    def expected_venue_positions(self) -> dict[str, Decimal]:
        """우리 원장이 예상하는 증권사 합계 = Σ 슬리브 + 미귀속."""
        total = self.aggregate_sleeves()
        for key, qty in self._unassigned.items():
            total[key] = total.get(key, Decimal("0")) + qty
        return {key: qty for key, qty in total.items() if qty != 0}

    def apply_fill(self, agent_id: str, symbol: Symbol, signed_quantity: Decimal) -> None:
        """체결을 발주 에이전트의 슬리브에 귀속한다."""
        book = self._sleeves.setdefault(agent_id, {})
        book[symbol.key] = book.get(symbol.key, Decimal("0")) + signed_quantity

    def attribute(self, order: Order) -> str | None:
        """이 주문을 낸 에이전트. 모르면 None — 우리가 낸 주문이 아닙니다."""
        return self._order_agent.get(order.id)

    # ── 불변식 ───────────────────────────────────────────────────────────
    def check_invariant(self, venue_positions: dict[str, Decimal]) -> dict:
        """`Σ 슬리브 + 미귀속 == 증권사 합계` 를 확인한다.

        어긋난 종목들을 돌려주고, 하나라도 있으면 그룹을 멈춥니다. 어느
        슬리브가 틀렸는지 알 방법이 없으므로 부분 정지는 하지 않습니다.
        """
        expected = self.expected_venue_positions()
        observed = {key: Decimal(str(qty))
                    for key, qty in (venue_positions or {}).items()
                    if Decimal(str(qty)) != 0}

        drift: dict[str, dict] = {}
        for key in sorted(set(expected) | set(observed)):
            ours = expected.get(key, Decimal("0"))
            theirs = observed.get(key, Decimal("0"))
            if abs(ours - theirs) > QUANTITY_EPSILON:
                drift[key] = {"원장": str(ours), "증권사": str(theirs),
                              "차이": str(theirs - ours)}

        if drift:
            detail = "; ".join(
                f"{key} 원장 {d['원장']} · 증권사 {d['증권사']}"
                for key, d in drift.items()
            )
            self.halt(
                f"슬리브 원장과 증권사 잔고가 다릅니다 — {detail}. "
                f"어느 에이전트의 물량이 달라졌는지 알 수 없어 그룹 전체를 "
                f"멈췄습니다. 증권사 앱에서 보유와 체결을 확인한 뒤 다시 "
                f"시작하세요.",
                drift,
            )
        return drift

    # ── SleeveGateway 인터페이스 ─────────────────────────────────────────
    async def submit_for(self, agent_id: str, order: Order) -> Order:
        """에이전트의 주문을 계좌 한도를 통과시킨 뒤 증권사로 보낸다.

        **한도 확인과 기록 사이에 다른 에이전트가 끼어들 수 없어야 합니다.**
        `TradingBudget.check` 는 통과시켜도 아무것도 기록하지 않고, 소모는
        `record_order` 가 합니다. 그 사이에 `await venue.submit(...)` 가 있으면
        네트워크 왕복 내내 창이 열려 있고, 에이전트 넷이 그 안으로 동시에
        들어갑니다 — 하루 1건짜리 계좌 한도에 4건이 통과합니다. 방어선이 봇
        수만큼 곱해지는 것, 이 모듈이 존재하는 이유 그 자체입니다.

        그래서 확인·발주·기록을 통째로 하나의 잠금 안에서 합니다. 증권사
        어댑터도 이미 제출을 자기 잠금으로 직렬화하므로(`live_base`) 여기서
        직렬화한다고 잃는 처리량은 없습니다.
        """
        async with self._submit_lock:
            return await self._submit_locked(agent_id, order)

    async def _submit_locked(self, agent_id: str, order: Order) -> Order:
        self._assert_running()
        self._assert_may_send_real_money(agent_id, order)

        ok, reason = self._master_check(order)
        if not ok:
            order.status = OrderStatus.REJECTED
            # 어느 한도에 걸렸는지 화면이 구별할 수 있어야 합니다. 에이전트
            # 한도와 계좌 한도는 사용자가 고치는 자리가 다릅니다.
            order.reject_reason = f"계좌 한도: {reason}"
            order.updated_at = utcnow()
            return order

        self._order_agent[order.id] = agent_id
        submitted = await self.venue.submit(order)
        if submitted.status is OrderStatus.REJECTED:
            self._order_agent.pop(order.id, None)
            return submitted

        self._master_record(submitted)
        if submitted.filled_qty > 0 and not self._venue_polls_fills:
            # 체결을 따로 알려 주지 않고 주문 응답에 실어 보내는 증권사입니다
            # (페이퍼·즉시 체결). 그 응답을 체결로 옮겨 적어 두지 않으면
            # 슬리브는 영원히 비어 있고, 다음 불변식 검사가 계좌와의 차이를
            # 드리프트로 읽어 그룹을 멈춥니다.
            self._record_fill(agent_id, _fill_from(submitted))
        return submitted

    def _assert_may_send_real_money(self, agent_id: str, order: Order) -> None:
        """관찰만 하기로 한 에이전트의 주문이 진짜 계좌로 나가지 못하게 한다.

        `AgentSpec.mode` 는 화면의 라벨이 아니라 약속입니다. 그런데 슬리브의
        주문은 게이트웨이를 거쳐 **그룹이 물고 있는 하나뿐인 어댑터** 로 갑니다 —
        그 어댑터가 실거래면 `mode=dry_run` 인 에이전트의 주문도 진짜 돈으로
        체결됩니다. "하나는 실거래, 하나는 관찰만" 은 이 기능을 쓰는 가장 흔한
        방식이고, 그 조합에서 조용히 진짜 주문이 나가는 것은 사고입니다.

        올바른 최종 형태는 관찰용 에이전트가 슬리브가 아니라 **자기 페이퍼
        브로커** 를 받는 것입니다(그래야 가상 체결도 되고 계좌 원장도 건드리지
        않습니다). 그 배선이 붙기 전까지, 여기서는 닫는 쪽으로 실패합니다 —
        가상 체결이 안 되는 것은 불편이고, 진짜 주문이 나가는 것은 손실입니다.
        """
        if not getattr(self.venue, "live", False):
            return
        try:
            spec = self.group.get(agent_id)
        except KeyError:
            raise GroupHalted(
                f"그룹에 없는 에이전트의 주문입니다: {agent_id}"
            ) from None
        if spec.is_live:
            return
        raise GroupHalted(
            f"{spec.label}({agent_id}) 은(는) 관찰(dry_run) 로 설정됐는데 "
            f"실거래 계좌로 주문이 나가려 했습니다 — 막았습니다. 이 에이전트로 "
            f"실제 매매를 하려면 설정을 실거래로 바꾸고, 관찰만 하려면 실거래 "
            f"계좌를 쓰는 그룹에서 빼세요.",
            {"agent_id": agent_id, "mode": spec.mode.value,
             "symbol": order.symbol.key},
        )

    async def cancel_for(self, agent_id: str, order: Order) -> bool:
        owner = self._order_agent.get(order.id)
        if owner is not None and owner != agent_id:
            # 남의 주문을 취소하는 경로는 열어 두지 않습니다. 취소는 곧 그
            # 에이전트의 의도를 되돌리는 일입니다.
            log.warning("%s 가 %s 의 주문을 취소하려 했습니다: %s",
                        agent_id, owner, order.id)
            return False
        return await self.venue.cancel(order)

    async def open_orders_for(self, agent_id: str) -> list[Order]:
        orders = await self.venue.open_orders()
        return [o for o in orders if self._order_agent.get(o.id) == agent_id]

    async def sync_for(self, agent_id: str) -> dict:
        """계좌를 재조정하고 불변식을 확인한다.

        어느 에이전트가 불렀든 하는 일은 같습니다 — 계좌는 하나이므로 확인할
        것도 하나입니다. 호출자만 기록에 남깁니다.

        **체결을 먼저 비웁니다.** 마지막 폴링 이후에 들어온 체결은 증권사 잔고에는
        이미 반영돼 있지만 우리 슬리브 원장에는 아직 없습니다. 그 상태로 불변식을
        보면 정상 체결이 드리프트로 읽히고, 그 오판이 그룹을 멈춥니다 — 그리고
        멈춘 그룹은 **나가는 주문도 못 냅니다.** 이 저장소가 반복해서 적어 둔
        규칙("빠져나오지 못하게 하는 것은 안전장치가 아니다")을 정면으로 어깁니다.
        """
        await self._drain_venue_fills()
        report = await self.venue.sync() or {}
        venue_positions = await self.venue.positions()
        drift = self.check_invariant(venue_positions)
        return {**report, "requested_by": agent_id,
                "sleeve_drift": drift, "halted": self.halted}

    def exact_flatten_order_type_for(
        self, symbol: Symbol, current_quantity: Decimal,
        target_quantity: Decimal,
    ):
        return self.venue.exact_flatten_order_type(
            symbol, current_quantity, target_quantity)

    # ── 계좌 단위 한도 ───────────────────────────────────────────────────
    def _master_check(self, order: Order) -> tuple[bool, str]:
        if not self.master_budget.configured:
            return True, ""
        price = order.limit_price or 0.0
        if price <= 0:
            price = self._last_price(order.symbol)
        return self.master_budget.check(
            order, price, self._reduces_account_position(order),
            equity=self._account_equity,
        )

    def _master_record(self, order: Order) -> None:
        price = order.limit_price or 0.0
        if price <= 0:
            price = self._last_price(order.symbol)
        self.master_budget.record_order(order, price)

    def _reduces_account_position(self, order: Order) -> bool:
        """계좌 전체로 봐서 노출을 줄이는 주문인가.

        슬리브 기준이 아니라 **계좌 기준** 입니다. 공격형이 파는 10주가 계좌
        전체로도 줄이는 것이면 계좌 한도는 그것을 막지 않습니다 — 나가는 길을
        막는 한도는 안전장치가 아닙니다.
        """
        held = self.expected_venue_positions().get(order.symbol.key, Decimal("0"))
        if held == 0 or order.quantity <= 0 or order.quantity > abs(held):
            return False
        from quant.core.types import OrderSide
        return (held > 0) == (order.side is OrderSide.SELL)

    def _last_price(self, symbol: Symbol) -> float:
        book = getattr(self.venue, "portfolio", None)
        if book is None:
            return 0.0
        return book.position(symbol).last_price

    # ── 체결 귀속 ────────────────────────────────────────────────────────
    #
    # 슬리브 원장과 에이전트 장부는 **같은 체결로만** 움직여야 합니다. 한쪽은
    # 주문 응답으로, 다른 쪽은 체결 폴링으로 갱신하면 같은 체결이 두 번 반영되고
    # (`Engine.settle_live_fills` 가 경고하는 그 사고), 합계 불변식은 이유 없이
    # 깨집니다. 그래서 슬리브를 건드리는 곳은 `_record_fill` 하나뿐입니다.
    @property
    def _venue_polls_fills(self) -> bool:
        """이 증권사가 체결을 따로 알려 주는가.

        알려 주면 주문 응답의 체결 수량은 **무시** 합니다 — 같은 체결이 두 경로로
        들어옵니다.
        """
        return getattr(self.venue, "poll_fills", None) is not None

    def _record_fill(self, agent_id: str, fill: Fill) -> None:
        """체결 하나를 슬리브에 반영하고 그 에이전트 몫으로 담아 둔다."""
        self.apply_fill(agent_id, fill.symbol, fill.quantity * fill.side.sign)
        self.master_budget.record_fill(fill)
        self._fill_buffer.setdefault(agent_id, []).append(fill)

    def settle(self, fills: list[Fill]) -> dict[str, list[Fill]]:
        """증권사에서 온 체결을 발주 에이전트별로 가른다.

        누구 것인지 모르는 체결은 미귀속으로 갑니다. 아무 에이전트에게나 주면
        그 에이전트가 팔 수 있게 되는데, 그것은 원장에 없는 물량을 파는 일입니다.
        """
        by_agent: dict[str, list[Fill]] = {}
        for fill in fills or []:
            agent_id = self._order_agent.get(fill.order_id)
            if agent_id is None:
                signed = fill.quantity * fill.side.sign
                self._unassigned[fill.symbol.key] = (
                    self._unassigned.get(fill.symbol.key, Decimal("0")) + signed
                )
                log.warning("발주자를 모르는 체결을 미귀속으로 둡니다: %s %s",
                            fill.symbol, fill.quantity)
                continue
            self._record_fill(agent_id, fill)
            by_agent.setdefault(agent_id, []).append(fill)
        return by_agent

    # ── 체결 채널 ────────────────────────────────────────────────────────
    #
    # 체결이 보이지 않으면 눈을 감고 거래하는 것이라 어댑터가 새 주문을 멈춥니다.
    # 그것은 **계좌의 사실** 입니다 — 채널이 죽으면 네 에이전트가 다 눈을 감은
    # 것이므로, 상태를 슬리브마다 복제하지 않고 여기서 하나만 들고 있습니다.
    @property
    def venue_backed(self) -> bool:
        return bool(getattr(self.venue, "venue_backed", False))

    @property
    def fill_channel_ok(self) -> bool:
        return bool(getattr(self.venue, "fill_channel_ok", True))

    @property
    def fill_channel_error(self) -> str:
        return str(getattr(self.venue, "fill_channel_error", "") or "")

    def fill_channel_down(self, reason: str) -> None:
        down = getattr(self.venue, "fill_channel_down", None)
        if down is not None:
            down(reason)

    def fill_channel_up(self) -> None:
        up = getattr(self.venue, "fill_channel_up", None)
        if up is not None:
            up()

    def drain_pending_fills_for(self, agent_id: str) -> list[Fill]:
        """어댑터가 이미 검증해 둔 체결 중 이 에이전트 몫. 네트워크를 타지 않습니다.

        증권사에서 한 번 빼 온 뒤 에이전트별로 갈라 담습니다 — 슬리브마다
        `drain_pending_fills()` 를 부르면 먼저 부른 하나가 나머지 셋의 체결까지
        비워 가고, 그 셋의 장부에는 그 체결이 영영 나타나지 않습니다.
        """
        drain = getattr(self.venue, "drain_pending_fills", None)
        if drain is not None:
            cached = drain()
            if cached:
                self.settle(cached)
        return self._fill_buffer.pop(agent_id, [])

    async def poll_fills_for(self, agent_id: str) -> list[Fill]:
        """이 에이전트가 아직 가져가지 않은 체결. 엔진이 자기 장부에 적습니다.

        증권사 폴링은 그룹에 한 번뿐입니다 — 슬리브마다 훑으면 먼저 부른
        하나가 나머지 셋의 체결까지 비워 가고, 그 셋의 장부에는 포지션이
        영원히 나타나지 않습니다.
        """
        await self._drain_venue_fills()
        return self._fill_buffer.pop(agent_id, [])

    async def _drain_venue_fills(self) -> None:
        if not self._venue_polls_fills:
            return
        fills = await self.venue.poll_fills()
        if fills:
            self.settle(fills)

    # ── 계좌 손실 한도가 실제로 걸리게 하는 배선 ─────────────────────────
    def record_closed_trade(self, agent_id: str, pnl: float) -> None:
        """청산된 매매의 손익을 **계좌** 원장에 적는다.

        이것이 없으면 계좌 하루 손실 한도는 영원히 걸리지 않습니다.
        `TradingBudget` 의 손실 게이트는 `ledger.realized_pnl` 만 보고, 그 값을
        쓰는 곳은 `record_trade` 하나뿐이며, 그것을 부르는 곳은
        `Engine._book_fills` 하나뿐입니다 — 그리고 거기서 불리는 것은 **에이전트의**
        예산입니다. 계좌 예산은 주문 건수와 거래대금과 수수료만 받고 손익은 0 인
        채로 남습니다.

        결과는 구체적입니다: 에이전트별 한도 10만원, 계좌 한도 20만원으로 두고
        넷이 각각 9만원씩 잃으면, 각자는 자기 한도 안이라 멈추지 않고 계좌
        한도는 실현손익 0 을 보고 하루 종일 통과시킵니다. 계좌는 36만원을
        잃었고 화면의 계좌 한도는 초록색입니다. 걸리지 않는 한도를 숫자로
        보여주는 것은 한도가 없는 것보다 나쁩니다.
        """
        self.master_budget.record_trade(float(pnl))

    def attach_engine(self, agent_id: str, engine) -> None:
        """엔진의 청산 이벤트를 계좌 원장으로 잇는다.

        손익은 포지션이 닫힐 때 `Portfolio.apply_fill` 이 계산합니다. 게이트웨이는
        체결만 보므로 스스로 계산할 수 없고, 엔진의 버스를 통해 받아야 합니다.
        """
        from quant.core.events import EventType

        def _on_trade_closed(event) -> None:
            payload = getattr(event, "payload", None) or {}
            try:
                self.record_closed_trade(agent_id, float(payload["pnl"]))
            except (KeyError, TypeError, ValueError):
                # 손익을 못 읽었다는 것은 계좌 손실 한도가 그만큼 눈을 감는다는
                # 뜻입니다. 조용히 넘기지 않고 남깁니다.
                log.warning("%s: 청산 손익을 계좌 원장에 적지 못했습니다: %r",
                            agent_id, payload)

        engine.ctx.bus.on(EventType.TRADE_CLOSED, _on_trade_closed)

    def forget_order(self, order_id: str) -> None:
        """종결된 주문의 귀속 기록을 지운다. 오래 돌면 이 표만 커집니다."""
        self._order_agent.pop(order_id, None)

    # ── 수명주기 ─────────────────────────────────────────────────────────
    async def connect(self) -> None:
        """증권사 연결은 계좌에 하나. 그룹 시작 시 한 번만 겁니다."""
        if self._connected:
            return
        await self.venue.connect()
        self._connected = True

    async def close(self) -> None:
        if not self._connected:
            return
        await self.venue.close()
        self._connected = False

    def status(self) -> dict[str, Any]:
        return {
            "halted": self.halted,
            "halt_reason": self._halted_reason,
            "halt_drift": self._halt_drift,
            "account_equity": round(self._account_equity, 2),
            "allocations": {k: round(v, 2) for k, v in self._allocations.items()},
            "sleeves": {
                agent_id: {key: str(qty) for key, qty in book.items() if qty != 0}
                for agent_id, book in self._sleeves.items()
            },
            "unassigned": {k: str(v) for k, v in self.unassigned_positions().items()},
            "master_budget": self.master_budget.status(),
            "mode": (RunMode.LIVE.value if self.group.has_live
                     else RunMode.DRY_RUN.value),
        }


def _fill_from(order: Order) -> Fill:
    """주문 응답에 실려 온 체결을 `Fill` 로 옮겨 적는다.

    체결을 따로 알려 주지 않는 증권사(페이퍼·즉시 체결)에서만 씁니다. 그쪽에서
    주문 응답은 체결 통지를 겸하므로, 여기서 옮겨 적지 않으면 그 체결은 어디에도
    기록되지 않습니다.
    """
    return Fill(
        order_id=order.id,
        symbol=order.symbol,
        side=order.side,
        quantity=order.filled_qty,
        price=order.avg_fill_price,
        fee=order.fees,
        ts=order.updated_at,
        tag=order.tag,
    )
