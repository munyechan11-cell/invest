"""미체결 주문과 리스크의 인사이트 취소.

Two bugs that both present as "the engine keeps sending the same order":

1. Sizing against the *filled* position while an order rests. A limit that sits
   for three bars becomes three times the intended position — and the logs look
   completely normal, because every individual order is correctly sized.
2. A risk model flattening a position without cancelling the insight that asked
   for it. The insight is still live next bar, the portfolio model rebuilds the
   same target, and the book oscillates in and out paying the spread each way.
   The stop looks broken when in fact it fires every single bar.
"""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.alpha.base import AlphaModel
from quant.brokerage.paper import PaperBrokerage
from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.engine import Engine
from quant.core.events import EventBus, EventType
from quant.core.types import (
    UTC,
    Bar,
    Direction,
    Insight,
    OrderSide,
    PortfolioTarget,
    Symbol,
)
from quant.execution.models import ImmediateExecution, LimitExecution, TwapExecution
from quant.portfolio.models import EqualWeighting
from quant.risk.models import MaximumDrawdownPerSecurity

SYM = Symbol("AAA", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def make_ctx(bars=6, price=100.0, cash=100_000.0):
    pf = Portfolio(cash)
    ctx = Context(SimClock(T0 + timedelta(days=bars)), pf, EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    for i in range(bars):
        ctx.push_bar(Bar(SYM, T0 + timedelta(days=i), price, price * 1.01,
                         price * 0.99, price, 1e6, "1d"))
    return ctx


# ── projected holdings ───────────────────────────────────────────────────
def test_projected_quantity_includes_resting_orders():
    ctx = make_ctx()
    assert ctx.projected_quantity(SYM) == Decimal("0")
    ctx.set_pending({SYM.key: Decimal("100")})
    assert ctx.pending_quantity(SYM) == Decimal("100")
    assert ctx.projected_quantity(SYM) == Decimal("100")

    ctx.portfolio.position(SYM).quantity = Decimal("40")
    assert ctx.projected_quantity(SYM) == Decimal("140")


def test_a_resting_order_is_not_sent_again():
    """The regression: 3 bars of a resting limit used to become 300 shares."""
    ctx = make_ctx()
    execution = LimitExecution(offset_bps=20, urgent_after_bars=99,
                               min_order_notional=1)
    target = PortfolioTarget(SYM, Decimal("100"))

    first = execution.execute(ctx, [target])
    assert len(first) == 1 and first[0].quantity == Decimal("100")

    ctx.set_pending({SYM.key: Decimal("100")})       # it is now resting
    assert execution.execute(ctx, [target]) == []
    assert execution.execute(ctx, [target]) == []


def test_a_partially_resting_order_only_tops_up_the_remainder():
    ctx = make_ctx()
    ctx.set_pending({SYM.key: Decimal("60")})
    orders = ImmediateExecution(min_order_notional=1).execute(
        ctx, [PortfolioTarget(SYM, Decimal("100"))])
    assert len(orders) == 1
    assert orders[0].quantity == Decimal("40")
    assert orders[0].side is OrderSide.BUY


def test_a_resting_sell_is_netted_too():
    ctx = make_ctx()
    ctx.portfolio.position(SYM).quantity = Decimal("100")
    ctx.set_pending({SYM.key: Decimal("-100")})      # already selling it all
    assert ImmediateExecution(min_order_notional=1).execute(
        ctx, [PortfolioTarget(SYM, Decimal("0"))]) == []


@pytest.mark.parametrize("execution", [
    ImmediateExecution(min_order_notional=1),
    LimitExecution(urgent_after_bars=99, min_order_notional=1),
    TwapExecution(slices=2, min_order_notional=1),
])
def test_every_execution_model_respects_resting_orders(execution):
    ctx = make_ctx()
    ctx.set_pending({SYM.key: Decimal("100")})
    assert execution.execute(ctx, [PortfolioTarget(SYM, Decimal("100"))]) == []


def test_the_engine_feeds_resting_orders_back_into_the_context():
    class WantsFullSize(AlphaModel):
        name = "wants_full"

        async def update(self, ctx, bars):
            return [Insight(b.symbol, Direction.UP, ctx.bar_delta * 20, ctx.now,
                            confidence=1.0, source=self.name) for b in bars.values()]

    pf = Portfolio(100_000.0)
    ctx = Context(SimClock(T0), pf, EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    broker = PaperBrokerage(pf)
    engine = Engine(
        ctx, WantsFullSize(),
        EqualWeighting(cash_reserve_pct=0.0, max_position_weight=1.0),
        LimitExecution(urgent_after_bars=99, min_order_notional=1), broker,
    )
    asyncio.run(engine.start())

    sizes = []
    for i in range(3):
        bar = Bar(SYM, T0 + timedelta(days=i), 100, 100.5, 99.5, 100, 1e6, "1d")
        before = len(engine.orders)
        asyncio.run(engine.on_bars({SYM.key: bar}))
        sizes.append(sum(float(o.quantity) for o in engine.orders[before:]))

    # only the first bar should size a full position; the rest see it resting
    assert sizes[0] > 0
    assert sum(sizes[1:]) == 0, f"engine re-sent a resting order: {sizes}"


# ── risk cancels the insight ─────────────────────────────────────────────
def test_a_stop_out_cancels_the_insight_that_opened_the_position():
    class AlwaysLong(AlphaModel):
        name = "always_long"

        async def update(self, ctx, bars):
            return [Insight(b.symbol, Direction.UP, ctx.bar_delta * 50, ctx.now,
                            confidence=1.0, source=self.name) for b in bars.values()]

    pf = Portfolio(100_000.0)
    ctx = Context(SimClock(T0), pf, EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    engine = Engine(
        ctx, AlwaysLong(),
        EqualWeighting(cash_reserve_pct=0.0, max_position_weight=1.0),
        ImmediateExecution(min_order_notional=1), PaperBrokerage(pf),
        risk_models=[MaximumDrawdownPerSecurity(max_drawdown_pct=0.05, lock_bars=0)],
    )
    actions = []
    ctx.bus.on(EventType.RISK_ACTION, lambda e: actions.append(e.payload))
    asyncio.run(engine.start())

    price = 100.0
    for i in range(6):
        bar = Bar(SYM, T0 + timedelta(days=i), price, price * 1.005,
                  price * 0.995, price, 1e6, "1d")
        asyncio.run(engine.on_bars({SYM.key: bar}))
        price *= 0.94                                  # walk it into the stop

    assert actions, "risk never reported an action"
    assert actions[-1]["insights_cancelled"] is True
    assert len(engine.insights) == 0


def test_an_untouched_target_does_not_cancel_anything():
    pf = Portfolio(100_000.0)
    ctx = Context(SimClock(T0), pf, EventBus(), timeframe="1d")
    ctx.universe = [SYM]

    class Quiet(AlphaModel):
        name = "quiet"

        async def update(self, ctx, bars):
            return [Insight(b.symbol, Direction.UP, ctx.bar_delta * 50, ctx.now,
                            confidence=1.0, source=self.name) for b in bars.values()]

    engine = Engine(ctx, Quiet(),
                    EqualWeighting(cash_reserve_pct=0.0, max_position_weight=1.0),
                    ImmediateExecution(min_order_notional=1), PaperBrokerage(pf))
    asyncio.run(engine.start())
    for i in range(3):
        asyncio.run(engine.on_bars({SYM.key: Bar(
            SYM, T0 + timedelta(days=i), 100, 101, 99, 100, 1e6, "1d")}))
    assert len(engine.insights) > 0
