"""재시작 안전성과 청산 사유 귀속.

Two failures a live bot only shows you once, expensively:

- It restarts believing it holds nothing and has its full starting cash, then
  buys a second position on top of the one it already has.
- It restarts with every protection cleared, and immediately re-enters exactly
  the names it had just locked out.
"""
import os
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, ClosedTrade, Fill, OrderSide, Symbol
from quant.live.state import StateStore
from quant.risk.protections import StoplossGuard

SYM = Symbol("005930", venue="kis", quote_currency="KRW")
OTHER = Symbol("000660", venue="kis", quote_currency="KRW")
T0 = datetime(2024, 6, 3, tzinfo=UTC)


@pytest.fixture
def db_path():
    return os.path.join(tempfile.mkdtemp(), "state.db")


def stocked_portfolio():
    pf = Portfolio(10_000_000.0, "KRW")
    pos = pf.position(SYM)
    pos.quantity = Decimal("100")
    pos.avg_price = 70_000.0
    pos.opened_at = T0
    pf.cash = 3_000_000.0
    pf.high_water_mark = 10_500_000.0
    pf.total_fees = 12_345.0
    return pf


# ── restart ──────────────────────────────────────────────────────────────
def test_a_clean_shutdown_does_not_orphan_the_book(db_path):
    """The regression: `stop_run` stamped `stopped_at`, and resume only looked
    at runs that had never been stopped — so every normal restart lost the book."""
    st = StateStore(db_path)
    st.start_run("s", "live", 10_000_000.0)
    st.snapshot_positions(stocked_portfolio())
    st.stop_run()
    st.close()

    st2 = StateStore(db_path)
    assert st2.resume_run("s", "live") is not None


def test_restart_restores_cash_not_just_positions(db_path):
    """Restoring 100 shares while resetting cash to the configured balance
    produces an equity figure wrong by the cost of the position — and every
    later position size is computed from that wrong number."""
    st = StateStore(db_path)
    st.start_run("s", "live", 10_000_000.0)
    st.snapshot_positions(stocked_portfolio())
    st.close()

    st2 = StateStore(db_path)
    st2.resume_run("s", "live")
    fresh = Portfolio(10_000_000.0, "KRW")
    assert st2.restore_positions(fresh, {SYM.key: SYM}) == 1
    fresh.mark(SYM, 70_000.0)

    assert fresh.cash == pytest.approx(3_000_000.0)
    assert fresh.quantity(SYM) == Decimal("100")
    assert fresh.equity == pytest.approx(10_000_000.0)
    assert fresh.high_water_mark == pytest.approx(10_500_000.0)
    assert fresh.total_fees == pytest.approx(12_345.0)


def test_restart_without_stored_state_falls_back_to_the_configured_balance(db_path):
    st = StateStore(db_path)
    st.start_run("s", "live", 5_000_000.0)
    fresh = Portfolio(5_000_000.0, "KRW")
    assert st.restore_positions(fresh, {SYM.key: SYM}) == 0
    assert fresh.cash == pytest.approx(5_000_000.0)


def test_locks_survive_a_restart(db_path):
    st = StateStore(db_path)
    st.start_run("s", "live", 1_000.0)
    until = datetime.now(UTC) + timedelta(hours=2)
    st.save_locks({SYM.key: (until, "cooldown after stop")})
    st.close()

    st2 = StateStore(db_path)
    st2.resume_run("s", "live")
    restored = st2.restore_locks(datetime.now(UTC))
    assert SYM.key in restored
    assert restored[SYM.key][1] == "cooldown after stop"


def test_expired_locks_are_not_restored(db_path):
    st = StateStore(db_path)
    st.start_run("s", "live", 1_000.0)
    st.save_locks({SYM.key: (datetime.now(UTC) - timedelta(hours=1), "stale")})
    st.close()

    st2 = StateStore(db_path)
    st2.resume_run("s", "live")
    assert st2.restore_locks(datetime.now(UTC)) == {}


def test_context_round_trips_its_locks():
    ctx = Context(SimClock(T0), Portfolio(1_000.0), EventBus(), timeframe="1d")
    ctx.lock(SYM, T0 + timedelta(days=2), "stoploss_guard")
    exported = ctx.export_locks()
    assert SYM.key in exported

    other = Context(SimClock(T0), Portfolio(1_000.0), EventBus(), timeframe="1d")
    other.import_locks(exported)
    locked, reason = other.is_locked(SYM)
    assert locked and reason == "stoploss_guard"


# ── exit reason attribution ──────────────────────────────────────────────
def closed(exit_tag: str, pnl=-100.0):
    return ClosedTrade(
        symbol=SYM, side=OrderSide.BUY, quantity=Decimal("10"),
        entry_price=100.0, exit_price=99.0, entry_ts=T0,
        exit_ts=T0 + timedelta(hours=1), pnl=pnl, pnl_pct=pnl / 1000,
        fees=1.0, exit_tag=exit_tag,
    )


def test_exit_reason_is_parsed_from_the_tag():
    assert closed("stop_loss: -8.2% < -8.00%").exit_reason == "stop_loss"
    assert closed("trailing_stop: 6.00% from 132.4").exit_reason == "trailing_stop"
    assert closed("take_profit: +15.1%").exit_reason == "take_profit"
    assert closed("").exit_reason == "unknown"


def test_only_risk_forced_exits_count_as_stop_outs():
    assert closed("stop_loss: ...").was_stopped_out
    assert closed("trailing_stop: ...").was_stopped_out
    assert closed("time_stop: ...").was_stopped_out
    assert not closed("take_profit: ...", pnl=200).was_stopped_out
    assert not closed("w=+0.0000").was_stopped_out


def test_a_fill_carries_its_orders_tag_into_the_closed_trade():
    """`exit_tag` used to be the maker/taker flag, which no protection can use."""
    pf = Portfolio(100_000.0)
    pf.apply_fill(Fill("o1", SYM, OrderSide.BUY, Decimal("10"), 100.0, 0.0, T0,
                       tag="w=+0.2500"))
    trade = pf.apply_fill(Fill("o2", SYM, OrderSide.SELL, Decimal("10"), 90.0, 0.0,
                               T0 + timedelta(hours=1), tag="stop_loss: -10%"))
    assert trade is not None
    assert trade.exit_reason == "stop_loss"
    assert trade.was_stopped_out


def test_stoploss_guard_counts_stop_outs_not_ordinary_losers():
    """A run of small signal-driven losses is ordinary. A run of stop-outs means
    the setup is misreading the regime — which is what this guard is for."""
    ctx = Context(SimClock(T0 + timedelta(days=5)), Portfolio(100_000.0),
                  EventBus(), timeframe="1d")
    ctx.portfolio.closed_trades.extend(
        ClosedTrade(symbol=SYM, side=OrderSide.BUY, quantity=Decimal("1"),
                    entry_price=100, exit_price=99, entry_ts=T0,
                    exit_ts=T0 + timedelta(days=1), pnl=-1, pnl_pct=-0.01,
                    fees=0, exit_tag="w=+0.1000")
        for _ in range(6)
    )
    guard = StoplossGuard(lookback_bars=60, trade_limit=3, only_per_symbol=True)
    triggered, _ = guard.check(ctx, SYM)
    assert not triggered, "signal-driven losses should not trip the stoploss guard"

    ctx.portfolio.closed_trades.extend(
        ClosedTrade(symbol=SYM, side=OrderSide.BUY, quantity=Decimal("1"),
                    entry_price=100, exit_price=92, entry_ts=T0,
                    exit_ts=T0 + timedelta(days=2), pnl=-8, pnl_pct=-0.08,
                    fees=0, exit_tag="stop_loss: -8%")
        for _ in range(3)
    )
    triggered, reason = guard.check(ctx, SYM)
    assert triggered and "stop_loss" in reason


def test_the_guard_can_still_count_plain_losers_when_asked():
    ctx = Context(SimClock(T0 + timedelta(days=5)), Portfolio(100_000.0),
                  EventBus(), timeframe="1d")
    ctx.portfolio.closed_trades.extend(
        ClosedTrade(symbol=SYM, side=OrderSide.BUY, quantity=Decimal("1"),
                    entry_price=100, exit_price=99, entry_ts=T0,
                    exit_ts=T0 + timedelta(days=1), pnl=-1, pnl_pct=-0.01,
                    fees=0, exit_tag="w=+0.1000")
        for _ in range(5)
    )
    guard = StoplossGuard(lookback_bars=60, trade_limit=3, only_per_symbol=True,
                          stops_only=False)
    assert guard.check(ctx, SYM)[0]
