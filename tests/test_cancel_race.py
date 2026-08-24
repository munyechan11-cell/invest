"""취소가 체결에 지는 경우.

취소를 냈다고 취소된 것이 아닙니다. 주문이 호가에 걸려 있는 동안 상대가
체결시켜 버릴 수 있고, 그 체결은 취소 요청과 경주해서 이깁니다.

거래소에는 체결로 남았는데 엔진 장부에는 취소로 남으면, 그 주식은 계좌에
있으면서 엔진이 모르는 상태가 됩니다. 손절도, 사이징도, 하루 한도도 걸리지
않습니다. 다음 `sync()` 가 "어디서 온지 모를 포지션"으로 주워 담기는 하지만,
그때는 이미 매입 단가를 모릅니다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant.brokerage.live_base import LiveBrokerage
from quant.core.account import Portfolio
from quant.core.types import (
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Symbol,
    utcnow,
)

UTC = timezone.utc
SYM = Symbol("005930", venue="kis", quote_currency="KRW",
             tick_size=Decimal("100"))


class RacingBroker(LiveBrokerage):
    """취소는 성공했다고 답하지만, 그 전에 일부가 체결된 거래소."""

    name = "racing"

    def __init__(self, portfolio, *, fills_before_cancel: Decimal):
        # paper_venue: 주문은 실제로 나가지만 진짜 돈은 아닌 모드 —
        # 이 테스트가 필요로 하는 것은 sends_orders 가 켜지는 것뿐입니다.
        # ₩70,000 × 10주 = 70만원. 기본 주문 한도는 1만(USD 기준)이라
        # 여기서 막히면 이 테스트가 재현하려는 경주 자체가 일어나지 않습니다.
        super().__init__(portfolio, paper_venue=True, max_order_notional=1e9)
        self._racing_qty = fills_before_cancel
        self.cancel_calls = 0

    async def _venue_submit(self, order): return "BROKER-1"
    async def _venue_cancel(self, order):
        self.cancel_calls += 1
        return True
    async def _venue_open_orders(self): return []
    async def _venue_positions(self): return {}

    async def poll_fills(self):
        """장부에 남아 있는 주문에 한해 경주에서 이긴 체결을 붙입니다."""
        for order in list(self._orders.values()):
            if self._racing_qty <= 0 or order.filled_qty >= self._racing_qty:
                continue
            newly = self._racing_qty - order.filled_qty
            fill = Fill(order_id=order.id, symbol=order.symbol, side=order.side,
                        quantity=newly, price=70000.0, fee=105.0,
                        ts=datetime(2026, 3, 2, 5, 30, tzinfo=UTC), tag=order.tag)
            order.apply_fill(fill)
            self._pending_fills.append(fill)
        return await super().poll_fills()


def make_order(qty="10") -> Order:
    return Order(symbol=SYM, side=OrderSide.BUY, quantity=Decimal(qty),
                 type=OrderType.LIMIT, limit_price=70000.0, created_at=utcnow())


@pytest.fixture
def portfolio():
    return Portfolio(starting_cash=10_000_000.0)


@pytest.mark.asyncio
async def test_a_partial_fill_that_beat_the_cancel_is_still_booked(portfolio):
    broker = RacingBroker(portfolio, fills_before_cancel=Decimal("4"))
    order = make_order("10")
    await broker.submit(order)

    assert await broker.cancel(order) is True

    assert order.filled_qty == Decimal("4"), (
        "취소보다 먼저 체결된 4주가 사라졌습니다 — 거래소에는 있고 장부에는 "
        "없는 주식입니다")
    assert order.status is OrderStatus.CANCELED   # 남은 6주는 정말 취소됐습니다
    fills = await broker.poll_fills()
    assert sum(f.quantity for f in fills) == Decimal("4"), \
        "체결이 엔진에 전달되지 않았습니다"


@pytest.mark.asyncio
async def test_an_order_fully_filled_before_the_cancel_is_not_canceled(portfolio):
    """전부 체결됐으면 그건 취소된 주문이 아닙니다."""
    broker = RacingBroker(portfolio, fills_before_cancel=Decimal("10"))
    order = make_order("10")
    await broker.submit(order)

    await broker.cancel(order)

    assert order.filled_qty == Decimal("10")
    assert order.status is OrderStatus.FILLED, \
        "전부 체결된 주문에 CANCELED 를 덮었습니다"


@pytest.mark.asyncio
async def test_a_clean_cancel_is_still_a_clean_cancel(portfolio):
    """경주가 없었으면 예전과 똑같이 동작해야 합니다."""
    broker = RacingBroker(portfolio, fills_before_cancel=Decimal("0"))
    order = make_order("10")
    await broker.submit(order)

    assert await broker.cancel(order) is True
    assert order.status is OrderStatus.CANCELED
    assert order.filled_qty == Decimal("0")
    assert await broker.open_orders() == []


@pytest.mark.asyncio
async def test_fills_the_engine_has_not_taken_yet_are_not_swallowed(portfolio):
    """`_reap` 은 큐를 비우는 poll_fills 를 부릅니다 — 되돌려 놓아야 합니다."""
    broker = RacingBroker(portfolio, fills_before_cancel=Decimal("0"))
    other = make_order("5")
    await broker.submit(other)
    stray = Fill(order_id=other.id, symbol=SYM, side=OrderSide.BUY,
                 quantity=Decimal("5"), price=69900.0, fee=100.0,
                 ts=datetime(2026, 3, 2, 5, 0, tzinfo=UTC))
    broker._pending_fills.append(stray)

    victim = make_order("10")
    await broker.submit(victim)
    await broker.cancel(victim)

    assert stray in await broker.poll_fills(), \
        "취소가 남의 체결을 삼켰습니다"
