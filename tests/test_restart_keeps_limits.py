"""재시작이 안전장치를 지우면 안 된다.

Three ways a restart used to hand the money back to the bot:

- The daily ledger was rebuilt empty on every start. A budget whose stated job
  is bounding a bot that is crash-looping gave that bot a fresh allowance on
  every crash, and "다음 거래일까지 중단" lasted until the next deploy.
- Operator pins were lost. Two positions bought by hand, one restart, and the
  strategy — which has no insight behind either name — computed a target of
  zero and sold them back.
- Two processes on one `--state` file both resumed the same run and both sent
  orders. The venue filled twice; the DB recorded the last writer only.
"""
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, Fill, Order, OrderSide, OrderType, Symbol
from quant.live.limits import TradingBudget
from quant.live.state import StateInUseError, StateStore

SYM = Symbol("005930", venue="kis", quote_currency="KRW")
OTHER = Symbol("000660", venue="kis", quote_currency="KRW")
T0 = datetime(2026, 8, 23, 4, 0, tzinfo=UTC)       # 13:00 KST, 장중
NEXT_DAY = T0 + timedelta(days=1)


@pytest.fixture
def db_path():
    return os.path.join(tempfile.mkdtemp(), "state.db")


def order(qty=10, side=OrderSide.BUY, symbol=SYM):
    return Order(symbol, side, Decimal(str(qty)), OrderType.LIMIT, limit_price=100.0)


def opened(db, strategy="s", mode="live"):
    """A store attached to the run, the way LiveTrader attaches to it."""
    st = StateStore(db)
    if st.resume_run(strategy, mode) is None:
        st.start_run(strategy, mode, 10_000_000.0)
    return st


def spent_budget(**kw):
    """A budget that has traded today and hit its loss limit."""
    budget = TradingBudget(max_daily_loss=1_000, timezone_offset_hours=9, **kw)
    budget.record_order(order(10), 70_000.0, now=T0)
    budget.record_fill(Fill("ord_1", SYM, OrderSide.BUY, Decimal("10"), 70_000.0,
                            350.0, T0), now=T0)
    budget.record_trade(-4_930.0, now=T0)
    allowed, _ = budget.check(order(10), 70_000.0, is_reducing=False, now=T0)
    assert not allowed and budget.halted
    return budget


# ── 일일 한도 원장 ────────────────────────────────────────────────────────
def test_a_halted_bot_does_not_resume_buying_after_a_restart(db_path):
    """The persona's run: the loss cap tripped, the process died, and the same
    day it came back it bought again."""
    st = opened(db_path)
    st.restore_budget(spent_budget(), now=T0)          # binds the write-through
    st.close()                                          # crash / redeploy / OOM

    st2 = opened(db_path)
    fresh = TradingBudget(max_daily_loss=1_000, timezone_offset_hours=9)
    assert st2.restore_budget(fresh, now=T0) is True

    assert fresh.halted
    assert "일일 손실 한도" in fresh.status(now=T0)["halt_reason"]
    allowed, reason = fresh.check(order(10), 70_000.0, is_reducing=False, now=T0)
    assert not allowed and "일일 손실 한도" in reason


def test_the_days_usage_survives_a_restart(db_path):
    st = opened(db_path)
    st.restore_budget(spent_budget(), now=T0)
    st.close()

    fresh = TradingBudget(max_daily_loss=1_000, timezone_offset_hours=9)
    st2 = opened(db_path)
    st2.restore_budget(fresh, now=T0)

    ledger = fresh.roll(T0)
    assert ledger.orders == 1
    assert ledger.notional == pytest.approx(700_000.0)
    assert ledger.realized_pnl == pytest.approx(-4_930.0)
    assert ledger.fees == pytest.approx(350.0)
    assert ledger.blocked == 1
    assert ledger.day == T0.date()


def test_a_restart_does_not_hand_back_the_notional_allowance(db_path):
    """The restart-abuser's arithmetic: five restarts, five times the cap."""
    st = opened(db_path)
    budget = TradingBudget(max_daily_notional=1_000_000, timezone_offset_hours=9)
    st.restore_budget(budget, now=T0)
    budget.record_order(order(12), 70_000.0, now=T0)    # 840,000 used
    st.close()

    fresh = TradingBudget(max_daily_notional=1_000_000, timezone_offset_hours=9)
    st2 = opened(db_path)
    st2.restore_budget(fresh, now=T0)

    allowed, reason = fresh.check(order(10), 70_000.0, is_reducing=False, now=T0)
    assert not allowed and "거래대금" in reason


def test_a_new_trading_day_starts_clean(db_path):
    st = opened(db_path)
    st.restore_budget(spent_budget(), now=T0)
    st.close()

    fresh = TradingBudget(max_daily_loss=1_000, timezone_offset_hours=9)
    st2 = opened(db_path)
    assert st2.restore_budget(fresh, now=NEXT_DAY) is False

    assert not fresh.halted
    assert fresh.roll(NEXT_DAY).realized_pnl == 0.0
    allowed, _ = fresh.check(order(10), 70_000.0, is_reducing=False, now=NEXT_DAY)
    assert allowed


def test_a_changed_timezone_discards_the_stored_day(db_path):
    """'오늘'의 범위가 달라졌으니 그 원장은 오늘 것이 아니다."""
    st = opened(db_path)
    st.restore_budget(spent_budget(), now=T0)
    st.close()

    fresh = TradingBudget(max_daily_loss=1_000, timezone_offset_hours=0)
    st2 = opened(db_path)
    assert st2.restore_budget(fresh, now=T0) is False
    assert not fresh.halted


def test_the_ledger_is_written_through_between_snapshots(db_path):
    """A SIGKILL lands between two equity snapshots, not on one. An order that
    went out in that gap still has to count."""
    st = opened(db_path)
    budget = TradingBudget(max_daily_orders=2, timezone_offset_hours=9)
    st.restore_budget(budget, now=T0)
    budget.record_order(order(10), 70_000.0, now=T0)
    budget.record_order(order(10), 70_000.0, now=T0)
    st.close()                                          # nothing saved explicitly

    fresh = TradingBudget(max_daily_orders=2, timezone_offset_hours=9)
    st2 = opened(db_path)
    st2.restore_budget(fresh, now=T0)
    allowed, reason = fresh.check(order(1), 70_000.0, is_reducing=False, now=T0)
    assert not allowed and "주문 건수" in reason


def test_a_restored_halt_still_lets_you_out(db_path):
    """A cap that traps you in a losing position is not a safety feature — and
    that has to stay true across a restart."""
    st = opened(db_path)
    st.restore_budget(spent_budget(), now=T0)
    st.close()

    fresh = TradingBudget(max_daily_loss=1_000, timezone_offset_hours=9)
    st2 = opened(db_path)
    st2.restore_budget(fresh, now=T0)
    allowed, _ = fresh.check(order(10, side=OrderSide.SELL), 70_000.0,
                             is_reducing=True, now=T0)
    assert allowed


def test_an_operator_release_does_not_survive_a_restart(db_path):
    """Waiving the caps is a decision about a bot that is running. The counters
    come back, the waiver does not, so the bot re-halts and asks again."""
    st = opened(db_path)
    budget = spent_budget()
    st.restore_budget(budget, now=T0)
    budget.release()
    assert budget.check(order(10), 70_000.0, is_reducing=False, now=T0)[0]
    st.close()

    fresh = TradingBudget(max_daily_loss=1_000, timezone_offset_hours=9)
    st2 = opened(db_path)
    st2.restore_budget(fresh, now=T0)
    assert not fresh.roll(T0).released
    allowed, _ = fresh.check(order(10), 70_000.0, is_reducing=False, now=T0)
    assert not allowed


def test_the_budget_follows_the_engine_clock_across_a_restart(db_path):
    """The ledger is keyed by the *simulated* local day when a clock is set."""
    clock = SimClock(T0)
    st = opened(db_path)
    budget = TradingBudget(max_daily_orders=1, timezone_offset_hours=9, clock=clock)
    st.restore_budget(budget)
    budget.record_order(order(10), 70_000.0)
    st.close()

    fresh = TradingBudget(max_daily_orders=1, timezone_offset_hours=9,
                          clock=SimClock(T0 + timedelta(hours=2)))
    st2 = opened(db_path)
    assert st2.restore_budget(fresh) is True
    assert not fresh.check(order(1), 70_000.0, is_reducing=False)[0]


def test_an_empty_ledger_is_not_written(db_path):
    """A budget that has not traded must not stamp a row that later reads as
    'today, zero used' for a different day."""
    st = opened(db_path)
    st.save_budget(TradingBudget(max_daily_orders=1))
    assert st.conn.execute("SELECT count(*) c FROM day_budget").fetchone()["c"] == 0


# ── 운영자 고정(pin) ──────────────────────────────────────────────────────
def live_ctx():
    ctx = Context(SimClock(T0), Portfolio(10_000_000.0, "KRW"), EventBus(),
                  timeframe="1d")
    ctx.universe = [SYM, OTHER]
    return ctx


def test_pins_survive_a_restart(db_path):
    """수동으로 산 두 종목이 재시작 2분 뒤 청산되던 자리."""
    ctx = live_ctx()
    ctx.pin(SYM, "수동 매수: 실적 발표 대응")
    ctx.pin(OTHER, "수동 매수")
    st = opened(db_path)
    st.save_pins(ctx.pinned)
    st.close()

    st2 = opened(db_path)
    fresh = live_ctx()
    assert st2.restore_pins(fresh, {SYM.key: SYM, OTHER.key: OTHER}) == 2
    assert fresh.is_pinned(SYM) and fresh.is_pinned(OTHER)
    assert fresh.pinned[SYM.key] == "수동 매수: 실적 발표 대응"


def test_unpinning_is_persisted_too(db_path):
    ctx = live_ctx()
    ctx.pin(SYM, "수동 매수")
    st = opened(db_path)
    st.save_pins(ctx.pinned)
    ctx.unpin(SYM)
    st.save_pins(ctx.pinned)
    st.close()

    st2 = opened(db_path)
    fresh = live_ctx()
    assert st2.restore_pins(fresh, {SYM.key: SYM}) == 0
    assert not fresh.is_pinned(SYM)


def test_a_pin_outside_the_universe_is_reported_not_swallowed(db_path, caplog):
    ctx = live_ctx()
    ctx.pin(OTHER, "수동 매수")
    st = opened(db_path)
    st.save_pins(ctx.pinned)

    fresh = live_ctx()
    with caplog.at_level("WARNING"):
        assert st.restore_pins(fresh, {SYM.key: SYM}) == 0
    assert OTHER.key in caplog.text


# ── 상태 파일의 단독 소유 ─────────────────────────────────────────────────
HOLDER = """
import sys, time
from quant.core.account import Portfolio
from quant.live.state import StateStore

st = StateStore(sys.argv[1])
st.start_run("s", "live", 10_000_000.0)
st.snapshot_positions(Portfolio(10_000_000.0, "KRW"))
print("owned", flush=True)
time.sleep(120)
"""


def test_a_second_process_refuses_to_share_the_state_db(db_path):
    """Both processes attached to the same run and both sent orders: the venue
    filled twice, the DB recorded once."""
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, db_path],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "owned"

        second = StateStore(db_path)
        second.resume_run("s", "live")          # attaching read-only is fine
        with pytest.raises(StateInUseError) as err:
            second.restore_positions(Portfolio(10_000_000.0, "KRW"), {})
        assert str(holder.pid) in str(err.value)
        assert "--state" in str(err.value)
        with pytest.raises(StateInUseError):
            second.start_run("s", "live", 10_000_000.0)
    finally:
        holder.kill()
        holder.wait()


def test_starting_a_second_trader_in_one_process_is_refused(db_path):
    """The dashboard's Stop then Start, before the first loop has finished
    winding down: still two books sending orders."""
    running = opened(db_path)
    running.snapshot_positions(Portfolio(10_000_000.0, "KRW"))

    second = StateStore(db_path)
    with pytest.raises(StateInUseError) as err:
        second.restore_locks(datetime.now(UTC))
    assert "종료된 뒤에" in str(err.value)


def test_the_owner_is_released_on_a_clean_shutdown(db_path):
    first = opened(db_path)
    first.close()

    second = opened(db_path)                    # must not raise
    second.snapshot_positions(Portfolio(10_000_000.0, "KRW"))
    second.close()


def test_a_dead_owner_does_not_brick_the_state_db(db_path):
    """A hard kill leaves the row behind. Refusing to start on a row nobody is
    behind would turn every SIGKILL into manual surgery."""
    dead = subprocess.Popen([sys.executable, "-c", ""])
    dead.wait()
    conn = sqlite3.connect(db_path)
    conn.executescript("CREATE TABLE IF NOT EXISTS db_owner ("
                       "id INTEGER PRIMARY KEY CHECK (id = 1), pid INTEGER NOT NULL,"
                       "host TEXT NOT NULL, started_at TEXT NOT NULL,"
                       "heartbeat_at TEXT NOT NULL)")
    now = datetime.now(UTC).isoformat()
    conn.execute("INSERT OR REPLACE INTO db_owner VALUES(1,?,?,?,?)",
                 (dead.pid, socket.gethostname(), now, now))
    conn.commit()
    conn.close()

    st = opened(db_path)                        # takes over
    st.snapshot_positions(Portfolio(10_000_000.0, "KRW"))
    assert st._owner_row()["pid"] == os.getpid()


def test_the_dashboard_can_still_read_a_db_a_bot_owns(db_path):
    """Reading is not owning — the read-only endpoints must keep working while
    a bot holds the file."""
    bot = opened(db_path)
    bot.record_equity(T0, 10_000_000.0, 3_000_000.0, 0.0)

    reader = StateStore(db_path)
    reader.resume_run("s", "live")
    assert len(reader.equity_curve()) == 1
    assert reader.recent_trades() == []
    reader.close()

    assert bot._owner_row()["pid"] == os.getpid()
    bot.record_equity(T0 + timedelta(days=1), 10_000_000.0, 3_000_000.0, 0.0)
