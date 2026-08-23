"""Position and cash accounting.

These are the tests that matter most: an error here silently invents or
destroys money, and every performance number downstream inherits the lie.
"""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.core.account import Portfolio
from quant.core.types import UTC, Fill, OrderSide, Position, Symbol

SYM = Symbol("TEST", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def fill(side, qty, price, fee=0.0, minutes=0):
    return Fill("o1", SYM, side, Decimal(str(qty)), price, fee,
                T0 + timedelta(minutes=minutes))


def test_open_long_sets_basis():
    pos = Position(SYM)
    pos.apply(fill(OrderSide.BUY, 10, 100.0))
    assert pos.quantity == Decimal("10")
    assert pos.avg_price == pytest.approx(100.0)
    assert pos.is_long


def test_adding_averages_the_basis():
    pos = Position(SYM)
    pos.apply(fill(OrderSide.BUY, 10, 100.0))
    pos.apply(fill(OrderSide.BUY, 10, 120.0, minutes=1))
    assert pos.quantity == Decimal("20")
    assert pos.avg_price == pytest.approx(110.0)


def test_partial_close_realizes_proportional_pnl():
    pos = Position(SYM)
    pos.apply(fill(OrderSide.BUY, 10, 100.0))
    realized = pos.apply(fill(OrderSide.SELL, 4, 110.0, minutes=1))
    assert realized == pytest.approx(40.0)
    assert pos.quantity == Decimal("6")
    # the remaining basis must not move when part of the position is sold
    assert pos.avg_price == pytest.approx(100.0)


def test_flip_through_zero_rebases():
    pos = Position(SYM)
    pos.apply(fill(OrderSide.BUY, 10, 100.0))
    pos.apply(fill(OrderSide.SELL, 15, 110.0, minutes=1))
    assert pos.quantity == Decimal("-5")
    assert pos.avg_price == pytest.approx(110.0)
    assert pos.realized_pnl == pytest.approx(100.0)


def test_short_pnl_sign():
    pos = Position(SYM)
    pos.apply(fill(OrderSide.SELL, 10, 100.0))
    realized = pos.apply(fill(OrderSide.BUY, 10, 90.0, minutes=1))
    assert realized == pytest.approx(100.0)     # covered lower → profit
    assert pos.is_flat


def test_fees_reduce_realized_pnl():
    pos = Position(SYM)
    pos.apply(fill(OrderSide.BUY, 10, 100.0, fee=1.0))
    realized = pos.apply(fill(OrderSide.SELL, 10, 110.0, fee=1.0, minutes=1))
    assert realized == pytest.approx(99.0)
    assert pos.fees_paid == pytest.approx(2.0)


def test_portfolio_cash_conservation():
    pf = Portfolio(10_000.0)
    pf.apply_fill(fill(OrderSide.BUY, 10, 100.0, fee=5.0))
    assert pf.cash == pytest.approx(10_000 - 1_000 - 5)
    pf.mark(SYM, 100.0)
    assert pf.equity == pytest.approx(10_000 - 5)      # only the fee was lost

    pf.apply_fill(fill(OrderSide.SELL, 10, 110.0, fee=5.0, minutes=1))
    assert pf.cash == pytest.approx(10_000 - 1_000 - 5 + 1_100 - 5)
    assert pf.equity == pytest.approx(10_090.0)


def test_round_trip_is_booked_once():
    pf = Portfolio(10_000.0)
    pf.apply_fill(fill(OrderSide.BUY, 10, 100.0))
    closed = pf.apply_fill(fill(OrderSide.SELL, 10, 110.0, minutes=60))
    assert closed is not None
    assert closed.pnl == pytest.approx(100.0)
    assert closed.pnl_pct == pytest.approx(0.1)
    assert closed.duration == timedelta(minutes=60)
    assert len(pf.closed_trades) == 1


def test_drawdown_tracks_high_water_mark():
    pf = Portfolio(1_000.0)
    pf.record_equity(T0)
    pf.cash = 1_200.0
    pf.record_equity(T0 + timedelta(days=1))
    pf.cash = 900.0
    pf.record_equity(T0 + timedelta(days=2))
    assert pf.high_water_mark == pytest.approx(1_200.0)
    assert pf.drawdown == pytest.approx(0.25)


def test_multiplier_scales_notional():
    fut = Symbol("ESZ4", venue="SIM", multiplier=Decimal("50"), lot_size=Decimal("1"))
    pf = Portfolio(100_000.0)
    pf.apply_fill(Fill("o", fut, OrderSide.BUY, Decimal("2"), 5_000.0, 0.0, T0))
    assert pf.cash == pytest.approx(100_000 - 2 * 5_000 * 50)
    pf.mark(fut, 5_010.0)
    assert pf.positions[fut.key].unrealized_pnl == pytest.approx(2 * 10 * 50)


def test_lot_and_tick_rounding_never_rounds_up():
    sym = Symbol("BTC/USDT", venue="x", lot_size=Decimal("0.001"),
                 tick_size=Decimal("0.5"))
    assert sym.round_qty(Decimal("1.23456")) == Decimal("1.234")
    assert sym.round_qty(Decimal("-1.23456")) == Decimal("-1.234")
    assert sym.round_price(100.4, OrderSide.BUY) == Decimal("100.0")
    assert sym.round_price(100.1, OrderSide.SELL) == Decimal("100.5")
