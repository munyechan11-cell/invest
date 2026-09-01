"""하루 거래 한도와 수동 개입.

The daily budget is the only limit in the engine that assumes the *strategy is
broken*. Everything else — position weight, leverage, drawdown — assumes it is
behaving. So the tests that matter here are the ones where something has gone
wrong: a loop, a signal oscillating on noise, a bad day.

The manual controls have one rule that outranks the rest: an operator must
always be able to get out. Pausing, hitting a cap, being locked by a protection
— none of them may block an exit.
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
from quant.core.events import EventBus
from quant.core.types import (
    UTC,
    Bar,
    Direction,
    Insight,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Symbol,
)
from quant.execution.models import ImmediateExecution
from quant.live.limits import TradingBudget
from quant.live.manual import ManualControl
from quant.portfolio.models import EqualWeighting

SYM = Symbol("AAA", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))
OTHER = Symbol("BBB", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))
T0 = datetime(2024, 6, 3, 4, 0, tzinfo=UTC)      # 13:00 KST


def order(qty=10, side=OrderSide.BUY, symbol=SYM):
    return Order(symbol, side, Decimal(str(qty)), OrderType.LIMIT, limit_price=100.0)


def ctx_with(qty=0, price=100.0, cash=100_000.0, bars=10):
    pf = Portfolio(cash)
    ctx = Context(SimClock(T0), pf, EventBus(), timeframe="1d")
    ctx.universe = [SYM, OTHER]
    for s in (SYM, OTHER):
        for i in range(bars):
            ctx.push_bar(Bar(s, T0 - timedelta(days=bars - i), price, price * 1.01,
                             price * 0.99, price, 1e6, "1d"))
    if qty:
        pos = pf.position(SYM)
        pos.quantity = Decimal(str(qty))
        pos.avg_price = price
        pos.mark(price)
    return ctx


# ── 하루 거래대금 ─────────────────────────────────────────────────────────
def test_notional_cap_blocks_the_order_that_would_cross_it():
    budget = TradingBudget(max_daily_notional=5_000)
    ok, _ = budget.check(order(10), 100.0, is_reducing=False, now=T0)
    assert ok
    budget.record_order(order(10), 100.0, now=T0)      # 1,000 used

    ok, reason = budget.check(order(50), 100.0, is_reducing=False, now=T0)
    assert not ok and "거래대금" in reason


def test_order_count_cap_halts_for_the_day():
    budget = TradingBudget(max_daily_orders=2)
    for _ in range(2):
        assert budget.check(order(), 100.0, False, now=T0)[0]
        budget.record_order(order(), 100.0, now=T0)
    ok, reason = budget.check(order(), 100.0, False, now=T0)
    assert not ok and "주문 건수" in reason
    assert budget.halted


def test_loss_cap_stops_new_entries():
    budget = TradingBudget(max_daily_loss=1_000)
    budget.record_trade(-1_200, now=T0)
    ok, reason = budget.check(order(), 100.0, False, now=T0)
    assert not ok and "손실" in reason


def test_percentage_loss_cap_uses_the_days_opening_equity():
    budget = TradingBudget(max_daily_loss_pct=0.02)
    budget.roll(T0, equity=1_000_000)
    budget.record_trade(-25_000, now=T0)
    ok, reason = budget.check(order(), 100.0, False, now=T0)
    assert not ok and "2.50%" in reason


# ── 절대 막으면 안 되는 것 ────────────────────────────────────────────────
@pytest.mark.parametrize("budget", [
    TradingBudget(max_daily_notional=1),
    TradingBudget(max_daily_orders=1),
    TradingBudget(max_daily_loss=1),
])
def test_no_cap_ever_blocks_an_exit(budget):
    """A limit that traps you in a losing position has stopped being a safety
    feature and become the risk."""
    budget.record_order(order(), 100.0, now=T0)
    budget.record_trade(-10_000, now=T0)
    budget.check(order(), 100.0, False, now=T0)          # trip it
    allowed, _ = budget.check(order(side=OrderSide.SELL), 100.0,
                              is_reducing=True, now=T0)
    assert allowed


# ── 날짜 경계 ─────────────────────────────────────────────────────────────
def test_the_day_rolls_over_on_korean_time_not_utc():
    """A budget that resets at UTC midnight resets in the middle of the KRX
    session it is supposed to be bounding."""
    budget = TradingBudget(max_daily_orders=1, timezone_offset_hours=9)
    kst_afternoon = datetime(2024, 6, 3, 4, 0, tzinfo=UTC)      # 13:00 KST 6/3
    kst_evening = datetime(2024, 6, 3, 14, 0, tzinfo=UTC)       # 23:00 KST 6/3
    next_morning = datetime(2024, 6, 3, 23, 0, tzinfo=UTC)      # 08:00 KST 6/4

    budget.record_order(order(), 100.0, now=kst_afternoon)
    assert not budget.check(order(), 100.0, False, now=kst_evening)[0]
    assert budget.check(order(), 100.0, False, now=next_morning)[0], \
        "새 거래일에 한도가 초기화되어야 합니다"


def test_release_waives_todays_caps_and_actually_lets_orders_through():
    """Clearing only the halt flag would not work: the counters are still over
    the line, so the next check re-halts and the button appears to do nothing."""
    budget = TradingBudget(max_daily_orders=1)
    budget.record_order(order(), 100.0, now=T0)
    budget.check(order(), 100.0, False, now=T0)
    assert budget.halted

    budget.release()
    assert budget.check(order(), 100.0, False, now=T0)[0]
    budget.record_order(order(), 100.0, now=T0)
    assert budget.check(order(), 100.0, False, now=T0)[0]


def test_a_release_does_not_carry_into_the_next_day():
    budget = TradingBudget(max_daily_orders=1)
    budget.record_order(order(), 100.0, now=T0)
    budget.check(order(), 100.0, False, now=T0)
    budget.release()
    assert budget.check(order(), 100.0, False, now=T0)[0]

    next_day = T0 + timedelta(days=1)
    budget.roll(next_day)
    assert budget.check(order(), 100.0, False, now=next_day)[0]
    budget.record_order(order(), 100.0, now=next_day)
    assert not budget.check(order(), 100.0, False, now=next_day)[0], \
        "해제가 다음 날까지 유지되면 안 됩니다"


def test_an_unconfigured_budget_never_interferes():
    budget = TradingBudget()
    assert not budget.configured
    assert budget.check(order(1_000_000), 100.0, False, now=T0)[0]


# ── 브로커에서 실제로 막히는가 ────────────────────────────────────────────
def test_the_paper_broker_enforces_the_budget():
    pf = Portfolio(1_000_000.0)
    broker = PaperBrokerage(pf)
    broker.portfolio = pf
    broker.budget = TradingBudget(max_daily_orders=1)

    first = asyncio.run(broker.submit(order()))
    assert first.status.is_open
    second = asyncio.run(broker.submit(order()))
    assert second.status.value == "rejected"
    assert "주문 건수" in second.reject_reason


# ── 수동 개입 ─────────────────────────────────────────────────────────────
def test_manual_buy_by_notional_rounds_to_the_lot_grid():
    ctx = ctx_with()
    manual = ManualControl()
    manual.buy(SYM, notional=1_050.0, note="test")
    orders = manual.build_orders(ctx)
    assert len(orders) == 1
    assert orders[0].side is OrderSide.BUY
    assert orders[0].quantity == Decimal("10")          # 1050/100 floored


def test_a_manual_buy_pins_the_symbol_so_the_strategy_cannot_sell_it_back():
    """Without the pin, the portfolio model computes a target of zero next bar
    and undoes the operator's trade within a minute."""
    ctx = ctx_with()
    manual = ManualControl()
    manual.buy(SYM, quantity=Decimal("10"))
    manual.build_orders(ctx)
    assert ctx.is_pinned(SYM)

    ctx.portfolio.position(SYM).quantity = Decimal("10")
    targets = EqualWeighting(cash_reserve_pct=0.0).create_targets(ctx, [])
    assert all(t.symbol.key != SYM.key for t in targets), \
        "핀 고정된 종목에 목표가 나오면 안 됩니다"


def test_manage_true_hands_the_position_to_the_strategy():
    ctx = ctx_with()
    manual = ManualControl()
    manual.buy(SYM, quantity=Decimal("10"), manage=True)
    manual.build_orders(ctx)
    assert not ctx.is_pinned(SYM)


def test_closing_unpins_so_the_strategy_gets_the_symbol_back():
    ctx = ctx_with(qty=10)
    ctx.pin(SYM, "manual")
    manual = ManualControl()
    manual.close(SYM)
    orders = manual.build_orders(ctx)
    assert orders and orders[0].side is OrderSide.SELL
    assert orders[0].quantity == Decimal("10")
    assert not ctx.is_pinned(SYM)


def test_manual_sell_is_clamped_to_the_held_quantity():
    ctx = ctx_with(qty=5)
    manual = ManualControl()
    request = manual.sell(SYM, quantity=Decimal("100"))
    orders = manual.build_orders(ctx)
    assert orders[0].quantity == Decimal("5")
    assert "보유 수량까지만" in request.detail


def test_close_all_covers_every_open_position():
    ctx = ctx_with(qty=10)
    ctx.portfolio.position(OTHER).quantity = Decimal("7")
    ctx.portfolio.position(OTHER).avg_price = 100.0
    manual = ManualControl()
    manual.close_all()
    orders = manual.build_orders(ctx)
    assert {o.symbol.ticker for o in orders} == {"AAA", "BBB"}
    assert all(o.side is OrderSide.SELL for o in orders)


def test_closing_a_flat_symbol_is_a_no_op_not_an_error():
    ctx = ctx_with(qty=0)
    manual = ManualControl()
    request = manual.close(SYM)
    assert manual.build_orders(ctx) == []
    assert request.status == "skipped"


def test_manual_close_stays_queued_while_a_same_symbol_order_is_open():
    ctx = ctx_with(qty=10)
    ctx.set_pending({SYM.key: Decimal("-10")})
    manual = ManualControl()
    request = manual.close(SYM)

    assert manual.build_orders(ctx) == []
    assert request.status == "pending"
    assert [item.id for item in manual.pending] == [request.id]
    assert "미체결 주문 정산 후" in request.detail


def test_unpriced_market_buy_expires_instead_of_waiting_for_a_future_quote():
    ctx = ctx_with()
    manual = ManualControl()
    request = manual.buy(SYM, quantity=Decimal("1"))
    request.requested_at = ctx.now - timedelta(seconds=31)

    assert manual.build_orders(ctx) == []
    assert manual.pending == []
    assert request.status == "skipped"
    assert "만료" in request.detail


def test_aged_limit_buy_remains_price_bounded_and_can_be_built():
    ctx = ctx_with()
    manual = ManualControl()
    request = manual.buy(
        SYM, quantity=Decimal("1"), limit_price=99.0,
    )
    request.requested_at = ctx.now - timedelta(hours=1)

    built = manual.build_orders(ctx)

    assert len(built) == 1
    assert built[0].type is OrderType.LIMIT
    assert built[0].limit_price == 99.0


def test_close_all_is_atomic_while_any_position_has_an_open_order():
    ctx = ctx_with(qty=10)
    other = ctx.portfolio.position(OTHER)
    other.quantity = Decimal("7")
    other.avg_price = 100.0
    ctx.set_pending({SYM.key: Decimal("-10")})
    manual = ManualControl()
    request = manual.close_all()

    assert manual.build_orders(ctx) == []
    assert request.status == "pending"
    assert [item.id for item in manual.pending] == [request.id]


def test_same_flush_sell_then_close_never_oversells_the_position():
    ctx = ctx_with(qty=10)
    manual = ManualControl()
    first = manual.sell(SYM, quantity=Decimal("6"))
    deferred = manual.close(SYM)

    orders = manual.build_orders(ctx)

    assert [(order.side, order.quantity) for order in orders] == [
        (OrderSide.SELL, Decimal("6")),
    ]
    assert first.status == "submitted"
    assert deferred.status == "pending"
    assert [item.id for item in manual.pending] == [deferred.id]


def test_same_flush_close_all_then_close_defers_the_duplicate_symbol():
    ctx = ctx_with(qty=10)
    other = ctx.portfolio.position(OTHER)
    other.quantity = Decimal("7")
    other.avg_price = 100.0
    manual = ManualControl()
    first = manual.close_all()
    deferred = manual.close(SYM)

    orders = manual.build_orders(ctx)

    assert {order.symbol.key for order in orders} == {SYM.key, OTHER.key}
    assert first.status == "submitted"
    assert deferred.status == "pending"
    assert [item.id for item in manual.pending] == [deferred.id]


def test_closed_session_rejection_keeps_a_manual_exit_for_next_open():
    ctx = ctx_with(qty=10)
    manual = ManualControl()
    request = manual.close(SYM)
    order = manual.build_orders(ctx)[0]
    order.status = OrderStatus.REJECTED
    order.reject_reason = "venue rejected: market closed"

    manual.record_submission(order, reducing=True)

    assert manual.history[-1].status == "rejected"
    assert manual.history[-1].detail == order.reject_reason
    assert [item.id for item in manual.pending] == [request.id]
    assert manual.pending[0].status == "pending"
    assert "장이 열리면" in manual.pending[0].detail


def test_uncertain_rejection_is_not_automatically_retried():
    ctx = ctx_with(qty=10)
    manual = ManualControl()
    manual.close(SYM)
    order = manual.build_orders(ctx)[0]
    order.status = OrderStatus.REJECTED
    order.reject_reason = "venue rejected: connection reset"

    manual.record_submission(order, reducing=True)

    assert manual.history[-1].status == "rejected"
    assert manual.pending == []


def test_a_request_without_quantity_or_notional_is_rejected_cleanly():
    ctx = ctx_with()
    manual = ManualControl()
    request = manual.buy(SYM)
    assert manual.build_orders(ctx) == []
    assert request.status == "error"


# ── 일시정지 ──────────────────────────────────────────────────────────────
class _AlwaysLong(AlphaModel):
    name = "always_long"

    async def update(self, ctx, bars):
        return [Insight(b.symbol, Direction.UP, ctx.bar_delta * 20, ctx.now,
                        confidence=1.0, source=self.name) for b in bars.values()]


def _engine(pf, ctx, **kw):
    broker = PaperBrokerage(pf)
    return Engine(ctx, _AlwaysLong(),
                  EqualWeighting(cash_reserve_pct=0.0, max_position_weight=1.0),
                  ImmediateExecution(min_order_notional=1), broker, **kw)


def test_pausing_stops_new_entries():
    pf = Portfolio(100_000.0)
    ctx = Context(SimClock(T0), pf, EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    engine = _engine(pf, ctx)
    asyncio.run(engine.start())
    engine.manual.pause("test")

    for i in range(3):
        asyncio.run(engine.on_bars({SYM.key: Bar(
            SYM, T0 + timedelta(days=i), 100, 101, 99, 100, 1e6, "1d")}))
    assert not engine.orders, "일시정지 중에 신규 진입이 나갔습니다"


def test_a_manual_order_still_goes_out_while_paused():
    """Pause means "open nothing new", not "the operator loses the controls"."""
    pf = Portfolio(100_000.0)
    ctx = Context(SimClock(T0), pf, EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    engine = _engine(pf, ctx)
    asyncio.run(engine.start())
    engine.manual.pause("test")
    engine.manual.buy(SYM, quantity=Decimal("5"))

    asyncio.run(engine.on_bars({SYM.key: Bar(
        SYM, T0, 100, 101, 99, 100, 1e6, "1d")}))
    assert len(engine.orders) == 1
    assert engine.orders[0].source == "manual"


def test_a_stop_still_fires_while_paused():
    from quant.risk.models import MaximumDrawdownPerSecurity

    pf = Portfolio(100_000.0)
    ctx = Context(SimClock(T0), pf, EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    pos = pf.position(SYM)
    pos.quantity = Decimal("100")
    pos.avg_price = 100.0
    pos.mark(80.0)
    engine = _engine(pf, ctx,
                     risk_models=[MaximumDrawdownPerSecurity(max_drawdown_pct=0.05)])
    asyncio.run(engine.start())
    engine.manual.pause("test")

    asyncio.run(engine.on_bars({SYM.key: Bar(
        SYM, T0, 80, 81, 79, 80, 1e6, "1d")}))
    assert any(o.side is OrderSide.SELL for o in engine.orders), \
        "일시정지가 손절을 막았습니다"


def test_paused_risk_exit_wins_and_conflicting_manual_sell_stays_queued():
    from quant.risk.models import MaximumDrawdownPerSecurity

    pf = Portfolio(100_000.0)
    ctx = Context(SimClock(T0), pf, EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    pos = pf.position(SYM)
    pos.quantity = Decimal("10")
    pos.avg_price = 100.0
    pos.mark(80.0)
    engine = _engine(
        pf,
        ctx,
        risk_models=[MaximumDrawdownPerSecurity(max_drawdown_pct=0.05)],
    )
    asyncio.run(engine.start())
    engine.manual.pause("test")
    request = engine.manual.sell(SYM, quantity=Decimal("6"))

    asyncio.run(engine.on_bars({SYM.key: Bar(
        SYM, T0, 80, 81, 79, 80, 1e6, "1d",
    )}, settle=False))

    assert [(item.side, item.quantity, item.meta.get("model"))
            for item in engine.orders] == [
        (OrderSide.SELL, Decimal("10"), "risk"),
    ]
    assert request.status == "pending"


def test_unpaused_risk_exit_wins_and_conflicting_manual_buy_stays_queued():
    from quant.risk.models import MaximumDrawdownPerSecurity

    pf = Portfolio(100_000.0)
    ctx = Context(SimClock(T0), pf, EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    pos = pf.position(SYM)
    pos.quantity = Decimal("10")
    pos.avg_price = 100.0
    pos.mark(80.0)
    engine = _engine(
        pf,
        ctx,
        risk_models=[MaximumDrawdownPerSecurity(max_drawdown_pct=0.05)],
    )
    asyncio.run(engine.start())
    request = engine.manual.buy(SYM, quantity=Decimal("1"))

    asyncio.run(engine.on_bars({SYM.key: Bar(
        SYM, T0, 80, 81, 79, 80, 1e6, "1d",
    )}, settle=False))

    assert [(item.side, item.quantity, item.meta.get("model"))
            for item in engine.orders] == [
        (OrderSide.SELL, Decimal("10"), "risk"),
    ]
    assert request.status == "pending"


def test_risk_cap_that_preserves_strategy_source_still_wins_manual_buy():
    from quant.risk.models import SectorExposureCap

    pf = Portfolio(1_000.0)
    ctx = Context(SimClock(T0), pf, EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    pos = pf.position(SYM)
    pos.quantity = Decimal("10")
    pos.avg_price = pos.last_price = 100.0
    engine = _engine(
        pf,
        ctx,
        risk_models=[SectorExposureCap({SYM.ticker: "tech"}, max_group_weight=0.2)],
    )
    asyncio.run(engine.start())
    request = engine.manual.buy(SYM, quantity=Decimal("1"))

    asyncio.run(engine.on_bars({SYM.key: Bar(
        SYM, T0, 100, 101, 99, 100, 1e6, "1d",
    )}, settle=False))

    assert [(item.side, item.quantity, item.meta.get("model"))
            for item in engine.orders] == [
        (OrderSide.SELL, Decimal("6"), "equal_weight"),
    ]
    assert request.status == "pending"


@pytest.mark.parametrize("action", ["close", "buy"])
def test_missing_risk_order_allows_exit_but_blocks_new_manual_exposure(action):
    from quant.risk.models import MaximumDrawdownPerSecurity

    class NoOrdersExecution(ImmediateExecution):
        def execute(self, ctx, targets):
            return []

    pf = Portfolio(100_000.0)
    ctx = Context(SimClock(T0), pf, EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    pos = pf.position(SYM)
    pos.quantity = Decimal("10")
    pos.avg_price = pos.last_price = 100.0
    broker = PaperBrokerage(pf)
    engine = Engine(
        ctx,
        _AlwaysLong(),
        EqualWeighting(cash_reserve_pct=0.0, max_position_weight=1.0),
        NoOrdersExecution(min_order_notional=1),
        broker,
        risk_models=[MaximumDrawdownPerSecurity(max_drawdown_pct=0.05)],
    )
    asyncio.run(engine.start())
    request = (
        engine.manual.close(SYM)
        if action == "close"
        else engine.manual.buy(SYM, quantity=Decimal("1"))
    )

    asyncio.run(engine.on_bars({SYM.key: Bar(
        SYM, T0, 80, 81, 79, 80, 1e6, "1d",
    )}, settle=False))

    if action == "close":
        assert [(item.source, item.side, item.quantity)
                for item in engine.orders] == [
            ("manual", OrderSide.SELL, Decimal("10")),
        ]
        assert request.status == "submitted"
    else:
        assert engine.orders == []
        assert request.status == "pending"
        assert "리스크 축소" in request.detail


def test_resume_restores_normal_trading():
    pf = Portfolio(100_000.0)
    ctx = Context(SimClock(T0), pf, EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    engine = _engine(pf, ctx)
    asyncio.run(engine.start())
    engine.manual.pause("test")
    asyncio.run(engine.on_bars({SYM.key: Bar(SYM, T0, 100, 101, 99, 100, 1e6, "1d")}))
    assert not engine.orders

    engine.manual.resume()
    asyncio.run(engine.on_bars({SYM.key: Bar(
        SYM, T0 + timedelta(days=1), 100, 101, 99, 100, 1e6, "1d")}))
    assert engine.orders
