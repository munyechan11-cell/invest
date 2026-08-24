"""Exits must never be blocked.

Every rule in this file exists because the opposite behaviour was observed at
least once: a stop-loss that fired on paper while the position quietly rode to
-34% because some earlier layer refused to emit the closing order. A risk model
that cannot actually close a position is worse than no risk model, because it
reports safety it is not providing.
"""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, Bar, PortfolioTarget, Symbol
from quant.execution.models import ImmediateExecution, LimitExecution, TwapExecution
from quant.risk.models import (
    MaximumDrawdownPerSecurity,
    MaximumDrawdownPortfolio,
    MaxPositionCount,
    TradingLockGate,
    TrailingStopRiskModel,
)

SYM = Symbol("AAA", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))
T0 = datetime(2024, 6, 3, tzinfo=UTC)


def make_ctx(qty=100, avg=100.0, last=80.0, equity_cash=10_000.0):
    pf = Portfolio(equity_cash)
    ctx = Context(SimClock(T0), pf, EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    for i in range(30):
        ts = T0 - timedelta(days=30 - i)
        ctx.push_bar(Bar(SYM, ts, last, last * 1.01, last * 0.99, last, 1e6, "1d"))
    if qty:
        pos = pf.position(SYM)
        pos.quantity = Decimal(str(qty))
        pos.avg_price = avg
        pos.opened_at = T0 - timedelta(days=10)
        pos.mark(last)
    return ctx


def test_exit_survives_a_minimum_notional_floor():
    """The regression that motivated this file.

    A tiny leftover position must still be closable. Applying the entry-side
    notional floor to an exit is how a stop-loss silently fails.
    """
    ctx = make_ctx(qty=5, avg=100.0, last=80.0)          # position worth $400
    execution = ImmediateExecution(min_order_notional=10_000)   # far above it
    orders = execution.execute(ctx, [PortfolioTarget(SYM, Decimal("0"), tag="stop")])
    assert len(orders) == 1
    assert orders[0].side.value == "sell"
    assert orders[0].quantity == Decimal("5")


def test_entry_below_the_floor_is_still_skipped():
    """The floor must keep working for entries — that is what it is for."""
    ctx = make_ctx(qty=0)
    execution = ImmediateExecution(min_order_notional=10_000)
    tiny = PortfolioTarget(SYM, Decimal("1"))            # $80 of stock
    assert execution.execute(ctx, [tiny]) == []


def test_partial_reduction_is_also_exempt():
    ctx = make_ctx(qty=100, last=80.0)
    execution = ImmediateExecution(min_order_notional=10_000)
    orders = execution.execute(ctx, [PortfolioTarget(SYM, Decimal("99"))])
    assert len(orders) == 1 and orders[0].side.value == "sell"


@pytest.mark.parametrize("execution", [
    ImmediateExecution(min_order_notional=10_000),
    LimitExecution(min_order_notional=10_000),
    TwapExecution(slices=3, min_order_notional=10_000),
])
def test_every_execution_model_can_close(execution):
    ctx = make_ctx(qty=5, last=80.0)
    orders = execution.execute(ctx, [PortfolioTarget(SYM, Decimal("0"), tag="stop")])
    assert orders, f"{execution.name} refused to emit a closing order"
    assert orders[0].side.value == "sell"


def test_stop_loss_produces_a_flatten_target():
    ctx = make_ctx(qty=100, avg=100.0, last=80.0)        # -20%
    model = MaximumDrawdownPerSecurity(max_drawdown_pct=0.08)
    out = model.manage(ctx, [])
    assert len(out) == 1 and out[0].quantity == Decimal("0")


def test_trailing_stop_produces_a_flatten_target():
    ctx = make_ctx(qty=100, avg=100.0, last=120.0)
    pos = ctx.portfolio.position(SYM)
    pos.peak_price = 150.0                                # gave back 20% from the peak
    model = TrailingStopRiskModel(trail_pct=0.10, activate_at_pct=0.0)
    out = model.manage(ctx, [])
    assert out and out[0].quantity == Decimal("0")


def test_lock_gate_lets_exits_through_and_blocks_entries():
    ctx = make_ctx(qty=100)
    ctx.lock(SYM, T0 + timedelta(days=5), "cooldown")
    gate = TradingLockGate()
    assert gate.manage(ctx, [PortfolioTarget(SYM, Decimal("0"))])[0].quantity == Decimal("0")
    assert gate.manage(ctx, [PortfolioTarget(SYM, Decimal("50"))])[0].quantity == Decimal("50")
    assert gate.manage(ctx, [PortfolioTarget(SYM, Decimal("200"))])[0].quantity == Decimal("100")


def test_position_cap_never_drops_a_closing_target():
    ctx = make_ctx(qty=100)
    model = MaxPositionCount(max_positions=1)
    other = Symbol("BBB", venue="SIM")
    out = model.manage(ctx, [
        PortfolioTarget(SYM, Decimal("0"), tag="stop"),
        PortfolioTarget(other, Decimal("10")),
    ])
    closing = [t for t in out if t.symbol.key == SYM.key]
    assert closing and closing[0].quantity == Decimal("0")


def test_drawdown_halt_flattens_rather_than_freezing():
    ctx = make_ctx(qty=100)
    ctx.portfolio.high_water_mark = 20_000.0
    ctx.portfolio.cash = 1_000.0                          # deep drawdown
    model = MaximumDrawdownPortfolio(max_drawdown_pct=0.10, halt_bars=5)
    out = model.manage(ctx, [PortfolioTarget(SYM, Decimal("100"))])
    assert all(t.quantity == Decimal("0") for t in out)
    assert model.tripped


def test_drawdown_halt_lifts_and_rebases_instead_of_locking_forever():
    """A kill switch that can never un-trip ends the strategy, not the drawdown."""
    ctx = make_ctx(qty=0)
    ctx.portfolio.high_water_mark = 20_000.0
    model = MaximumDrawdownPortfolio(max_drawdown_pct=0.10, halt_bars=2, max_trips=3)
    model.manage(ctx, [])
    assert model.tripped
    ctx.clock.set(T0 + timedelta(days=3))
    model.manage(ctx, [])
    assert not model.tripped
    assert ctx.portfolio.high_water_mark == pytest.approx(ctx.portfolio.equity)
