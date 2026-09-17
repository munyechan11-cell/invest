"""전략이 사고 싶어 하는데 **한 주도 살 수 없는** 종목은 그렇다고 말합니다.

실행 모델은 격자에서 0 이 된 주문과 최소 주문금액 아래인 주문을 조용히
버립니다. 보유를 늘리는 미세 조정에서는 그게 이 문턱의 목적입니다. 그런데
보유가 **0** 인 종목이면 뜻이 완전히 다릅니다 — "이 계좌에서 이 종목은 못
산다" 이고, 같은 판단이 매 봉 반복되므로 봇은 그 종목을 영원히 사지 않습니다.

미국 전략에서 실제로 걸리는 경로입니다: `lot_size: 1` 인데 목표 금액이 한 주
값보다 작으면 `round_qty` 가 0 을 만들고, 주문은 만들어지지도 않습니다. 화면은
"실행 중", 로그는 조용, 그 종목만 영원히 안 삽니다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus, EventType
from quant.core.types import UTC, Bar, PortfolioTarget, Symbol
from quant.execution.models import ImmediateExecution

T0 = datetime(2024, 6, 3, tzinfo=UTC)
#: 한 주가 비싼 미국 주식. 정수 주만 살 수 있습니다.
PRICEY = Symbol("LLY", venue="toss", quote_currency="USD",
                tick_size=Decimal("0.01"), lot_size=Decimal("1"))
CHEAP = Symbol("AAPL", venue="toss", quote_currency="USD",
               tick_size=Decimal("0.01"), lot_size=Decimal("1"))


def ctx_at(prices: dict[Symbol, float], held: dict[Symbol, float] | None = None):
    pf = Portfolio(1_000.0)
    ctx = Context(SimClock(T0), pf, EventBus(), timeframe="1d")
    ctx.universe = list(prices)
    for symbol, price in prices.items():
        for i in range(5):
            ctx.push_bar(Bar(symbol, T0 - timedelta(days=5 - i), price, price,
                             price, price, 1e6, "1d"))
    for symbol, qty in (held or {}).items():
        pos = pf.position(symbol)
        pos.quantity = Decimal(str(qty))
        pos.avg_price = prices[symbol]
        pos.mark(prices[symbol])
    return ctx


def target(symbol: Symbol, qty: str) -> PortfolioTarget:
    return PortfolioTarget(symbol, Decimal(qty), tag="test")


# ── 격자에서 0 이 되는 진입 ──────────────────────────────────────────────
def test_a_share_more_expensive_than_the_target_is_named():
    """$900 짜리 한 주를 $285 목표로는 살 수 없습니다."""
    model = ImmediateExecution(min_order_notional=1)
    ctx = ctx_at({PRICEY: 900.0})
    orders = model.execute(ctx, [target(PRICEY, "0.31")])   # 0.31주 → 0주
    assert orders == []
    assert PRICEY.key in model.unreachable
    assert "최소 주문 단위" in model.unreachable[PRICEY.key]
    assert "900" in model.unreachable[PRICEY.key]


def test_below_the_notional_floor_is_named_too():
    model = ImmediateExecution(min_order_notional=200)
    ctx = ctx_at({CHEAP: 50.0})
    assert model.execute(ctx, [target(CHEAP, "2")]) == []   # $100 < $200
    assert "최소 주문금액" in model.unreachable[CHEAP.key]


def test_a_symbol_it_can_buy_is_not_named():
    model = ImmediateExecution(min_order_notional=1)
    ctx = ctx_at({CHEAP: 50.0})
    assert model.execute(ctx, [target(CHEAP, "4")])
    assert model.unreachable == {}


# ── 미세 조정은 조용합니다 ───────────────────────────────────────────────
def test_trimming_an_existing_position_stays_quiet():
    """이미 들고 있는 종목의 미세 조정이 문턱에 걸리는 것은 그 문턱의
    목적입니다. 그것까지 알리면 진짜 못 사는 종목이 묻힙니다."""
    model = ImmediateExecution(min_order_notional=200)
    ctx = ctx_at({CHEAP: 50.0}, held={CHEAP: 10})
    model.execute(ctx, [target(CHEAP, "10.4")])             # +0.4주 = $20
    assert model.unreachable == {}


def test_a_flat_target_on_a_flat_symbol_stays_quiet():
    """목표가 0 인데 보유도 0 이면 아무 일도 없는 것입니다."""
    model = ImmediateExecution(min_order_notional=1)
    ctx = ctx_at({PRICEY: 900.0})
    model.execute(ctx, [target(PRICEY, "0")])
    assert model.unreachable == {}


def test_the_list_is_rebuilt_each_pass_not_accumulated():
    """값이 오르거나 잔고가 늘어 다시 살 수 있게 되면 목록에서 빠져야
    합니다 — 안 그러면 화면이 옛날 사실을 계속 말합니다."""
    model = ImmediateExecution(min_order_notional=1)
    model.execute(ctx_at({PRICEY: 900.0}), [target(PRICEY, "0.31")])
    assert model.unreachable
    model.execute(ctx_at({PRICEY: 900.0}), [target(PRICEY, "2")])
    assert model.unreachable == {}


# ── 엔진이 그 사실을 내보낸다 ────────────────────────────────────────────
class _Bus(EventBus):
    def __init__(self):
        super().__init__()
        self.seen: list[tuple] = []

    async def publish(self, event_type, payload=None, source=""):
        self.seen.append((event_type, payload))
        return await super().publish(event_type, payload, source)


def engine_with(model: ImmediateExecution, ctx: Context):
    from quant.alpha.base import AlphaModel
    from quant.brokerage.paper import PaperBrokerage
    from quant.core.engine import Engine
    from quant.execution.costs import NoSlippage, PercentFeeModel
    from quant.portfolio.models import EqualWeighting

    class _Quiet(AlphaModel):
        name = "quiet"

        async def update(self, ctx, bars):
            return []

    broker = PaperBrokerage(ctx.portfolio, fee_model=PercentFeeModel(taker_bps=0),
                            slippage_model=NoSlippage())
    return Engine(ctx, _Quiet(), EqualWeighting(), model, broker)


def rejections(bus: _Bus) -> list[dict]:
    return [p for t, p in bus.seen if t is EventType.ORDER_REJECTED]


def test_the_engine_reports_it_as_a_refused_order():
    """출하 실거래 설정은 전부 `order_rejected` 를 알림으로 받습니다 —
    운영자가 알고 싶은 것이 정확히 "왜 주문이 안 나갔는가" 입니다."""
    model = ImmediateExecution(min_order_notional=1)
    ctx = ctx_at({PRICEY: 900.0})
    ctx.bus = bus = _Bus()
    engine = engine_with(model, ctx)
    model.execute(ctx, [target(PRICEY, "0.31")])
    asyncio.run(engine._announce_unreachable_entries())

    events = rejections(bus)
    assert len(events) == 1
    assert events[0]["symbol"] == "LLY" and events[0]["source"] == "execution"


def test_it_says_so_once_not_every_bar():
    """봉마다 같은 줄을 보내면 그 알림 채널이 통째로 안 읽히게 됩니다 —
    이 수정이 막으려는 것보다 나쁩니다."""
    model = ImmediateExecution(min_order_notional=1)
    ctx = ctx_at({PRICEY: 900.0})
    ctx.bus = bus = _Bus()
    engine = engine_with(model, ctx)
    for _ in range(5):
        model.execute(ctx, [target(PRICEY, "0.31")])
        asyncio.run(engine._announce_unreachable_entries())
    assert len(rejections(bus)) == 1


def test_it_says_so_again_after_the_block_clears():
    """다시 막히면 다시 말해야 합니다. 한 번 말하고 영영 입을 닫으면
    그것도 침묵입니다."""
    model = ImmediateExecution(min_order_notional=1)
    ctx = ctx_at({PRICEY: 900.0})
    ctx.bus = bus = _Bus()
    engine = engine_with(model, ctx)

    for qty in ("0.31", "2", "0.31"):
        model.execute(ctx, [target(PRICEY, qty)])
        asyncio.run(engine._announce_unreachable_entries())
    assert len(rejections(bus)) == 2


@pytest.mark.parametrize("qty", ["0.31", "0.99"])
def test_the_reason_names_the_share_price_so_it_is_actionable(qty):
    model = ImmediateExecution(min_order_notional=1)
    model.execute(ctx_at({PRICEY: 912.34}), [target(PRICEY, qty)])
    assert "912.34" in model.unreachable[PRICEY.key]
