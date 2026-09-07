"""노화 정책이 "빼라" 고 한 주문이 실제로 거래소에서 빠지는가.

`ExecutionModel.review_orders` 는 오래 매달린 주문에 판정을 내리고
`pending_cancellations` 에 담습니다. 그런데 **그 목록을 읽는 코드가 어디에도
없었습니다** — 정책은 매 봉 돌면서 아무 일도 하지 않았고, 취소·재가격·시장가
전환은 전부 죽은 경로였습니다.

토스·한투는 DAY 주문이라 장 마감이 대신 치워 줍니다. ccxt 는 아닙니다:
`timeInForce` 를 보내지 않아 GTC 로 남고, 8bp 아래 매수 지정가가 며칠 뒤 시장이
그 가격을 뚫고 내려올 때 **낡은 판단으로** 체결됩니다. 그동안 그 종목에는
새 주문도 나가지 않습니다(정책이 stand-down 시킵니다).

거래소가 스스로 끝낸 주문을 로컬이 계속 열린 것으로 알면 같은 결과입니다 —
`projected quantity` 가 없는 주문을 세고 그 종목의 매매가 멈춥니다.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from quant.core.types import (
    UTC,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Symbol,
)
from quant.live.trader import LiveTrader

SYM = Symbol("BTC/USDT", venue="binance", quote_currency="USDT")
T0 = datetime(2026, 9, 1, tzinfo=UTC)


class Broker:
    venue_backed = True

    def __init__(self, *, refuse=False):
        self.canceled: list[Order] = []
        self.refuse = refuse

    async def cancel(self, order):
        self.canceled.append(order)
        if self.refuse:
            return False
        order.status = OrderStatus.CANCELED
        return True


class Model:
    """노화 판정을 이미 내린 실행 모델의 자리."""

    def __init__(self, pending):
        self._pending = list(pending)
        self.reviews = 0

    def review_orders(self, _ctx):
        self.reviews += 1
        return []

    @property
    def pending_cancellations(self):
        return [o for o in self._pending if o.status.is_open]


def trader(model, broker):
    t = LiveTrader.__new__(LiveTrader)
    settled = []

    async def settle_live_fills():
        settled.append(True)

    t.engine = SimpleNamespace(execution_model=model, brokerage=broker,
                               ctx=SimpleNamespace(now=T0),
                               settle_live_fills=settle_live_fills)
    t._settled = settled
    return t


def resting(side=OrderSide.BUY, price=60_000.0):
    order = Order(SYM, side, Decimal("1"), OrderType.LIMIT, limit_price=price)
    order.status = OrderStatus.SUBMITTED
    order.broker_id = "venue-1"
    return order


@pytest.mark.asyncio
async def test_a_stale_order_is_actually_canceled():
    stale = resting()
    broker = Broker()
    t = trader(Model([stale]), broker)

    removed = await t._flush_aged_orders()

    assert removed == 1
    assert broker.canceled == [stale], "정책이 빼라고 했는데 아무도 빼지 않았다"
    assert stale.status is OrderStatus.CANCELED


@pytest.mark.asyncio
async def test_the_policy_is_asked_before_draining():
    """판정을 갱신하지 않으면 목록이 언제나 비어 있습니다."""
    model = Model([])
    t = trader(model, Broker())

    await t._flush_aged_orders()

    assert model.reviews == 1


@pytest.mark.asyncio
async def test_a_race_fill_is_booked_before_the_next_decision():
    """취소와 체결이 겹쳤을 수 있습니다 — 그 체결을 먼저 장부에 넣어야
    다음 판단이 있지도 않은 수량을 팔지 않습니다."""
    t = trader(Model([resting()]), Broker())

    await t._flush_aged_orders()

    assert t._settled, "취소 뒤 체결 정산을 하지 않습니다"


@pytest.mark.asyncio
async def test_a_refused_cancel_is_not_counted_and_does_not_raise():
    """거래소가 거절하면 다음 주기가 다시 시도합니다 — 봇은 계속 돕니다."""
    t = trader(Model([resting()]), Broker(refuse=True))

    assert await t._flush_aged_orders() == 0
    assert t._settled == []


@pytest.mark.asyncio
async def test_nothing_pending_makes_no_requests():
    """회귀 방지: 평상시 3초마다 쓸데없는 취소 요청을 만들지 않습니다."""
    broker = Broker()
    t = trader(Model([]), broker)

    assert await t._flush_aged_orders() == 0
    assert broker.canceled == []


@pytest.mark.asyncio
async def test_a_model_without_the_policy_is_left_alone():
    """모든 실행 모델이 노화 정책을 갖는 것은 아닙니다."""
    t = trader(SimpleNamespace(), Broker())
    assert await t._flush_aged_orders() == 0


def test_the_maintenance_cycle_actually_calls_it():
    """함수만 있고 아무도 부르지 않으면 지금과 똑같습니다."""
    import inspect

    src = inspect.getsource(LiveTrader._maintenance_cycle)
    assert "_flush_aged_orders" in src


# ── 거래소가 스스로 끝낸 주문 ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_an_order_the_exchange_killed_is_closed_locally():
    from quant.brokerage.ccxt_broker import CcxtBrokerage
    from quant.core.account import Portfolio

    broker = CcxtBrokerage.__new__(CcxtBrokerage)
    from quant.brokerage.live_base import LiveBrokerage

    LiveBrokerage.__init__(broker, Portfolio(10_000.0, "USDT"), live=True)
    order = resting()
    broker._orders[order.id] = order

    class Exchange:
        async def fetch_order(self, _bid, _ticker):
            return {"status": "canceled", "filled": 0}

    broker.ex = Exchange()
    await broker.poll_fills()

    assert order.status is OrderStatus.CANCELED
    assert not order.status.is_open


@pytest.mark.asyncio
async def test_an_order_the_exchange_filled_and_closed_is_marked_filled():
    from quant.brokerage.ccxt_broker import CcxtBrokerage
    from quant.brokerage.live_base import LiveBrokerage
    from quant.core.account import Portfolio

    broker = CcxtBrokerage.__new__(CcxtBrokerage)
    LiveBrokerage.__init__(broker, Portfolio(10_000.0, "USDT"), live=True)
    order = resting()
    broker._orders[order.id] = order

    class Exchange:
        async def fetch_order(self, _bid, _ticker):
            return {"status": "closed", "filled": 1, "average": 60_000.0}

    broker.ex = Exchange()
    await broker.poll_fills()

    assert order.filled_qty == Decimal("1")


@pytest.mark.asyncio
async def test_a_still_open_order_is_left_open():
    """회귀 방지: 살아 있는 주문을 닫으면 그 수량이 장부에서 사라집니다."""
    from quant.brokerage.ccxt_broker import CcxtBrokerage
    from quant.brokerage.live_base import LiveBrokerage
    from quant.core.account import Portfolio

    broker = CcxtBrokerage.__new__(CcxtBrokerage)
    LiveBrokerage.__init__(broker, Portfolio(10_000.0, "USDT"), live=True)
    order = resting()
    broker._orders[order.id] = order

    class Exchange:
        async def fetch_order(self, _bid, _ticker):
            return {"status": "open", "filled": 0}

    broker.ex = Exchange()
    await broker.poll_fills()

    assert order.status.is_open
