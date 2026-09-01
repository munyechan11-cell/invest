"""Near-real-time live maintenance without turning L1 into strategy history.

These tests pin the money-moving properties, not implementation strings:

* a session check made before a long sleep expires before the next decision;
* fills are booked while the strategy candle is still sleeping;
* only validated L1 marks the live book, while closed bars remain history;
* a quote outage blocks new exposure but never withholds a reducing exit.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

import quant.live.trader as trader_module
from quant.alpha.base import AlphaModel
from quant.brokerage.live_base import LiveBrokerage
from quant.brokerage.paper import PaperBrokerage
from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.engine import Engine
from quant.core.events import EventBus, EventType
from quant.core.types import (
    UTC,
    Bar,
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PortfolioTarget,
    Quote,
    RunMode,
    Symbol,
)
from quant.execution.models import ImmediateExecution, LimitExecution
from quant.live.manual import ManualControl
from quant.live.trader import LiveTrader
from quant.portfolio.models import EqualWeighting
from quant.risk.models import MaximumDrawdownPerSecurity

SYM = Symbol("005930", venue="toss", quote_currency="KRW")
OTHER = Symbol("000660", venue="toss", quote_currency="KRW")
T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


class QuietAlpha(AlphaModel):
    async def update(self, ctx, bars):
        return []


class TestLiveBroker(LiveBrokerage):
    """Network-free live adapter whose local order book behaves like production."""

    __test__ = False

    def __init__(self, portfolio: Portfolio):
        super().__init__(portfolio, live=True, max_order_notional=1_000_000_000)
        self.poll_calls = 0
        self.sync_calls = 0
        self.next_fill: Fill | None = None

    async def _venue_submit(self, order):
        return "never-called"

    async def _venue_cancel(self, order):
        return True

    async def _venue_open_orders(self):
        return []

    async def _venue_positions(self):
        return {}

    async def sync(self, *, expected_positions=None, independent_position_keys=None):
        self.sync_calls += 1
        return {"ok": True}

    async def poll_fills(self):
        self.poll_calls += 1
        if self.next_fill is not None:
            fill, self.next_fill = self.next_fill, None
            tracked = self._orders[fill.order_id]
            tracked.apply_fill(fill)
            self._pending_fills.append(fill)
        return await super().poll_fills()


def context(*, cash: float = 1_000.0, symbol: Symbol = SYM) -> Context:
    ctx = Context(
        SimClock(T0), Portfolio(cash, "KRW"), EventBus(),
        timeframe="1m", run_mode=RunMode.LIVE,
    )
    ctx.universe = [symbol]
    return ctx


def engine_with(ctx: Context, broker) -> Engine:
    return Engine(
        ctx,
        QuietAlpha(),
        EqualWeighting(),
        ImmediateExecution(),
        broker,
    )


def bare_trader(engine, provider) -> LiveTrader:
    trader = LiveTrader.__new__(LiveTrader)
    trader.engine = engine
    trader.provider = provider
    trader.calendar = None
    trader.errors = 0
    trader.max_errors = 10
    trader.last_bar_ts = None
    trader.running = True
    trader._last_quote_refresh = float("-inf")
    trader._quote_failures = {}
    trader._quote_blocked_decision = False
    trader._decision_due_at_next_open = False
    trader._next_fill_poll_at = 0.0
    trader._fill_poll_backoff_s = trader.FILL_POLL_S
    return trader


def test_market_permission_is_rechecked_after_the_long_candle_sleep(monkeypatch):
    trader = LiveTrader.__new__(LiveTrader)
    trader.config = SimpleNamespace(data=SimpleNamespace(timeframe="1d"))
    trader._stop = None
    trader.started_at = None
    trader.running = False
    open_now = True
    waits = 0
    ticks = 0

    class Calendar:
        def is_open(self, _now):
            return open_now

    trader.calendar = Calendar()

    async def start():
        trader.running = True
        trader.started_at = datetime.now(UTC)

    async def wait_for_market():
        nonlocal waits
        waits += 1
        if waits > 1:
            trader.running = False
            return True
        return False

    async def sleep_through_close(_seconds):
        nonlocal open_now
        open_now = False
        return True

    async def tick():
        nonlocal ticks
        ticks += 1

    trader.start = start
    trader._wait_for_market = wait_for_market
    trader._sleep_serving_manual = sleep_through_close
    trader._tick = tick
    trader.shutdown = lambda: asyncio.sleep(0)
    monkeypatch.setattr(
        trader_module,
        "next_candle_close",
        lambda now, _tf, lag=3.0: now + timedelta(seconds=1),
    )

    asyncio.run(trader.run())

    assert waits == 2
    assert ticks == 0, "the pre-sleep open check must not authorise a post-close tick"


def test_a_boundary_that_lands_closed_is_re_evaluated_at_the_next_open(monkeypatch):
    trader = LiveTrader.__new__(LiveTrader)
    trader.config = SimpleNamespace(data=SimpleNamespace(timeframe="1d"))
    trader._stop = None
    trader.started_at = None
    trader.running = False
    trader._decision_due_at_next_open = False
    open_now = True
    waits = 0
    ticks = 0

    class Calendar:
        def is_open(self, _now):
            return open_now

    trader.calendar = Calendar()

    async def start():
        trader.running = True
        trader.started_at = datetime.now(UTC)

    async def wait_for_market():
        nonlocal waits, open_now
        waits += 1
        if waits == 2:
            # Models one closed-session wait that wakes at the next opening.
            open_now = True
            return True
        return False

    async def sleep_through_close(_seconds):
        nonlocal open_now
        open_now = False
        return True

    async def tick():
        nonlocal ticks
        ticks += 1
        trader.running = False

    trader.start = start
    trader._wait_for_market = wait_for_market
    trader._sleep_serving_manual = sleep_through_close
    trader._tick = tick
    trader.shutdown = lambda: asyncio.sleep(0)
    monkeypatch.setattr(
        trader_module,
        "next_candle_close",
        lambda now, _tf, lag=3.0: now + timedelta(seconds=1),
    )

    asyncio.run(trader.run())

    assert waits == 3
    assert ticks == 1
    assert trader._decision_due_at_next_open is False


@pytest.mark.asyncio
async def test_slow_maintenance_does_not_extend_the_strategy_sleep(monkeypatch):
    trader = LiveTrader.__new__(LiveTrader)
    trader.running = True
    now = 0.0
    sleeps: list[float] = []
    maintenance_calls = 0

    async def sleep(seconds):
        nonlocal now
        sleeps.append(seconds)
        now += seconds
        return True

    async def maintenance():
        nonlocal now, maintenance_calls
        maintenance_calls += 1
        now += 100.0

    trader._sleep = sleep
    trader._maintenance_cycle = maintenance
    monkeypatch.setattr(trader_module.time, "monotonic", lambda: now)

    assert await trader._sleep_serving_manual(6.0) is True
    assert sleeps == [trader.MANUAL_FLUSH_S]
    assert maintenance_calls == 1


@pytest.mark.asyncio
async def test_empty_watch_list_makes_no_quote_request():
    ctx = context()
    ctx.universe = []
    calls = 0

    class Provider:
        async def quote(self, _symbol):
            nonlocal calls
            calls += 1

    engine = SimpleNamespace(ctx=ctx, manual=ManualControl(), brokerage=object())
    trader = bare_trader(engine, Provider())

    usable, missing = await trader._refresh_quotes(force=True)

    assert usable == set()
    assert missing == {}
    assert calls == 0


@pytest.mark.asyncio
async def test_last_quote_failure_stays_closed_until_a_network_refresh_succeeds(
        monkeypatch,
):
    ctx = context()
    quote = Quote(SYM, datetime.now(UTC), bid=99.0, ask=101.0)
    ctx.set_quote(quote)
    ctx.portfolio.mark(SYM, quote.mid)
    manual = ManualControl()
    request = manual.buy(SYM, quantity=Decimal("1"))
    calls = 0

    class Provider:
        async def quote(self, _symbol):
            nonlocal calls
            calls += 1
            return quote

    async def flush_manual():
        raise AssertionError("a cached quote must not clear a failed provider channel")

    async def refresh_pending():
        return True

    engine = SimpleNamespace(
        ctx=ctx,
        manual=manual,
        brokerage=object(),
        flush_manual=flush_manual,
        _refresh_pending=refresh_pending,
        _submit=lambda _orders: asyncio.sleep(0),
    )
    trader = bare_trader(engine, Provider())
    trader._last_quote_refresh = 100.0
    trader._quote_failures = {SYM.key: "provider unavailable"}
    monkeypatch.setattr(trader_module.time, "monotonic", lambda: 101.0)

    usable, missing = await trader._refresh_quotes()
    sent = await trader._flush_manual_quote_safe(missing)

    assert usable == set()
    assert missing == {SYM.key: "provider unavailable"}
    assert sent == 0
    assert calls == 0
    assert [pending.id for pending in manual.pending] == [request.id]


@pytest.mark.asyncio
async def test_valid_quote_marks_the_position_but_never_enters_bar_history():
    ctx = context()
    pos = ctx.portfolio.position(SYM)
    pos.quantity = Decimal("2")
    pos.avg_price = 100.0
    pos.mark(100.0)
    quote = Quote(SYM, datetime.now(UTC), bid=89.0, ask=91.0)

    class Provider:
        async def quote(self, _symbol):
            return quote

    engine = SimpleNamespace(ctx=ctx, manual=ManualControl(), brokerage=object())
    trader = bare_trader(engine, Provider())

    usable, missing = await trader._refresh_quotes(force=True)

    assert usable == {SYM.key}
    assert missing == {}
    assert ctx.quote(SYM) is quote
    assert pos.last_price == pytest.approx(90.0)
    assert ctx.history(SYM) == [], "L1 must not masquerade as a settled candle"


@pytest.mark.asyncio
async def test_rate_limited_later_quotes_are_validated_against_batch_completion(monkeypatch):
    class FakeDatetime(datetime):
        current = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    ctx = context()
    ctx.universe = [SYM, OTHER]

    class Provider:
        async def quote(self, symbol):
            if symbol is OTHER:
                await asyncio.sleep(0)
                FakeDatetime.current += timedelta(seconds=6)
            stamped = FakeDatetime.fromtimestamp(FakeDatetime.current.timestamp(), tz=UTC)
            return Quote(symbol, stamped, bid=99.0, ask=101.0)

    engine = SimpleNamespace(ctx=ctx, manual=ManualControl(), brokerage=object())
    trader = bare_trader(engine, Provider())
    monkeypatch.setattr(trader_module, "datetime", FakeDatetime)

    usable, missing = await trader._refresh_quotes(force=True)

    assert usable == {SYM.key, OTHER.key}
    assert missing == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_quote",
    [
        Quote(SYM, datetime.now(UTC), bid=101.0, ask=99.0),
        Quote(SYM, datetime.now(UTC) - timedelta(minutes=2), bid=99.0, ask=101.0),
        Quote(OTHER, datetime.now(UTC), bid=99.0, ask=101.0),
        Quote(SYM, datetime.now(UTC), bid=1e308, ask=1e308),
    ],
)
async def test_crossed_stale_or_wrong_symbol_quote_never_reprices_the_book(bad_quote):
    ctx = context()
    ctx.portfolio.mark(SYM, 100.0)

    class Provider:
        async def quote(self, _symbol):
            return bad_quote

    engine = SimpleNamespace(ctx=ctx, manual=ManualControl(), brokerage=object())
    trader = bare_trader(engine, Provider())

    usable, missing = await trader._refresh_quotes(force=True)

    assert usable == set()
    assert SYM.key in missing
    assert ctx.portfolio.position(SYM).last_price == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_engine_values_with_fresh_l1_while_history_keeps_the_closed_bar():
    ctx = context()
    pos = ctx.portfolio.position(SYM)
    pos.quantity = Decimal("1")
    pos.avg_price = 100.0
    pos.mark(100.0)
    end = T0 + timedelta(minutes=1)
    ctx.set_quote(Quote(SYM, end, bid=79.0, ask=81.0))
    engine = engine_with(ctx, PaperBrokerage(ctx.portfolio, run_mode=RunMode.LIVE))
    bar = Bar(SYM, T0, 100.0, 105.0, 95.0, 100.0, 1_000.0, "1m")

    await engine.on_bars({SYM.key: bar})

    assert ctx.latest(SYM).close == pytest.approx(100.0)
    assert pos.last_price == pytest.approx(80.0)


@pytest.mark.asyncio
async def test_engine_falls_back_to_the_closed_bar_when_l1_is_absent():
    ctx = context()
    pos = ctx.portfolio.position(SYM)
    pos.quantity = Decimal("1")
    pos.avg_price = 100.0
    pos.mark(70.0)
    engine = engine_with(ctx, PaperBrokerage(ctx.portfolio, run_mode=RunMode.LIVE))
    bar = Bar(SYM, T0, 100.0, 105.0, 95.0, 100.0, 1_000.0, "1m")

    await engine.on_bars({SYM.key: bar})

    assert pos.last_price == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_quote_failure_blocks_the_bar_decision_without_advancing_seen_cursor():
    ctx = context()
    broker = TestLiveBroker(ctx.portfolio)
    engine = engine_with(ctx, broker)

    class NoQuote:
        async def quote(self, _symbol):
            return None

    trader = bare_trader(engine, NoQuote())
    refreshed = 0
    fetched = 0
    exits = 0

    async def refresh_universe():
        nonlocal refreshed
        refreshed += 1

    async def fetch():
        nonlocal fetched
        fetched += 1
        return {}

    async def exit_safety():
        nonlocal exits
        exits += 1

    trader._refresh_universe = refresh_universe
    trader._fetch_new_bars = fetch
    trader._run_exit_safety = exit_safety

    await trader._tick()

    assert refreshed == 1
    assert exits == 1
    assert fetched == 0
    assert trader._quote_blocked_decision is True


@pytest.mark.asyncio
async def test_quote_outage_exit_safety_filters_any_exposure_increase():
    ctx = context()
    pos = ctx.portfolio.position(SYM)
    pos.quantity = Decimal("10")
    pos.avg_price = 100.0
    pos.mark(90.0)
    ctx.portfolio.mark(OTHER, 50.0)
    broker = TestLiveBroker(ctx.portfolio)
    submitted: list[Order] = []

    sell = Order(SYM, OrderSide.SELL, Decimal("10"), OrderType.MARKET)
    buy = Order(OTHER, OrderSide.BUY, Decimal("1"), OrderType.MARKET)

    class Risk:
        def manage(self, _ctx, holding_targets):
            assert holding_targets == [
                PortfolioTarget(SYM, Decimal("10"), tag="quote outage hold", source="safety")
            ]
            return [PortfolioTarget(SYM, Decimal("0"), source="risk")]

    class Execution:
        def execute(self, _ctx, _targets):
            return [sell, buy]

    async def refresh_pending():
        return True

    async def submit(orders):
        submitted.extend(orders)

    engine = SimpleNamespace(
        ctx=ctx,
        brokerage=broker,
        risk=Risk(),
        execution_model=Execution(),
        _refresh_pending=refresh_pending,
        _submit=submit,
        manual=ManualControl(),
    )
    trader = bare_trader(engine, SimpleNamespace())

    await trader._run_exit_safety()

    assert submitted == [sell]


@pytest.mark.asyncio
async def test_unchanged_risk_target_never_advances_a_stateful_execution_model():
    ctx = context()
    pos = ctx.portfolio.position(SYM)
    pos.quantity = Decimal("10")
    pos.avg_price = pos.last_price = 100.0
    broker = TestLiveBroker(ctx.portfolio)
    executed = 0

    class Risk:
        def manage(self, _ctx, holding_targets):
            return holding_targets

    class StatefulExecution:
        def execute(self, _ctx, _targets):
            nonlocal executed
            executed += 1
            return []

    async def refresh_pending():
        return None

    engine = SimpleNamespace(
        ctx=ctx,
        brokerage=broker,
        risk=Risk(),
        execution_model=StatefulExecution(),
        _refresh_pending=refresh_pending,
        _submit=lambda _orders: asyncio.sleep(0),
        manual=ManualControl(),
    )
    trader = bare_trader(engine, SimpleNamespace())

    await trader._run_exit_safety()

    assert executed == 0


@pytest.mark.asyncio
async def test_fresh_intrabar_mark_can_trigger_a_reducing_stop_without_a_new_candle():
    ctx = context()
    pos = ctx.portfolio.position(SYM)
    pos.quantity = Decimal("10")
    pos.avg_price = 100.0
    pos.mark(100.0)
    ctx.set_quote(Quote(SYM, T0, bid=79.0, ask=81.0))
    broker = TestLiveBroker(ctx.portfolio)
    engine = Engine(
        ctx,
        QuietAlpha(),
        EqualWeighting(),
        ImmediateExecution(),
        broker,
        risk_models=[MaximumDrawdownPerSecurity(max_drawdown_pct=0.10)],
    )
    submitted: list[Order] = []

    async def submit(orders):
        submitted.extend(orders)

    engine._submit = submit
    trader = bare_trader(engine, SimpleNamespace())

    await trader._run_exit_safety()

    assert len(submitted) == 1
    assert submitted[0].side is OrderSide.SELL
    assert submitted[0].quantity == Decimal("10")


@pytest.mark.asyncio
async def test_pending_entry_is_canceled_and_reconciled_before_emergency_stop():
    ctx = context()
    pos = ctx.portfolio.position(SYM)
    pos.quantity = Decimal("10")
    pos.avg_price = 100.0
    pos.mark(80.0)
    ctx.set_quote(Quote(SYM, T0, bid=79.0, ask=81.0))
    broker = TestLiveBroker(ctx.portfolio)
    pending_buy = Order(
        SYM,
        OrderSide.BUY,
        Decimal("1"),
        OrderType.LIMIT,
        limit_price=79.0,
        status=OrderStatus.SUBMITTED,
    )
    broker._orders[pending_buy.id] = pending_buy
    engine = Engine(
        ctx,
        QuietAlpha(),
        EqualWeighting(),
        LimitExecution(offset_bps=10, urgent_after_bars=2),
        broker,
        risk_models=[MaximumDrawdownPerSecurity(max_drawdown_pct=0.10)],
    )
    submitted: list[Order] = []

    async def submit(orders):
        submitted.extend(orders)

    engine._submit = submit
    trader = bare_trader(engine, SimpleNamespace())

    await trader._run_exit_safety()

    assert pending_buy.status is OrderStatus.CANCELED
    assert broker.sync_calls == 1
    assert len(submitted) == 1
    assert submitted[0].side is OrderSide.SELL
    assert submitted[0].quantity == Decimal("10")
    assert submitted[0].type is OrderType.MARKET
    assert submitted[0].meta["emergency_exit"] is True


@pytest.mark.asyncio
async def test_one_stale_cancel_settlement_does_not_trap_an_independent_exit():
    ctx = context()
    ctx.universe = [SYM, OTHER]
    first = ctx.portfolio.position(SYM)
    first.quantity = Decimal("6")
    first.avg_price = first.last_price = 100.0
    first.mark(80.0)
    second = ctx.portfolio.position(OTHER)
    second.quantity = Decimal("10")
    second.avg_price = second.last_price = 100.0
    second.mark(80.0)
    ctx.set_quote(Quote(SYM, T0, bid=79.0, ask=81.0))
    ctx.set_quote(Quote(OTHER, T0, bid=79.0, ask=81.0))

    class PartialBroker(LiveBrokerage):
        def __init__(self):
            super().__init__(
                ctx.portfolio, live=True, max_order_notional=1_000_000_000,
            )

        async def _venue_submit(self, _order):
            return "unused"

        async def _venue_cancel(self, _order):
            return True

        async def _venue_open_orders(self):
            return []

        async def _venue_positions(self):
            # A's cancel-race fill is still absent from holdings (stale 10).
            # B was independently reduced in the venue app and is already 8.
            return {SYM.key: Decimal("10"), OTHER.key: Decimal("8")}

        async def _venue_costs(self):
            return {SYM.key: 100.0, OTHER.key: 100.0}

    broker = PartialBroker()
    canceled = Order(
        SYM, OrderSide.SELL, Decimal("10"), OrderType.LIMIT,
        limit_price=79.0, status=OrderStatus.SUBMITTED,
    )
    # No broker id keeps this test network-free; the already-booked local 6 is
    # the exact post-cancel quantity the barrier must protect.
    broker._orders[canceled.id] = canceled
    engine = Engine(
        ctx, QuietAlpha(), EqualWeighting(), ImmediateExecution(), broker,
        risk_models=[MaximumDrawdownPerSecurity(max_drawdown_pct=0.10)],
    )
    submitted: list[Order] = []

    async def submit(orders):
        submitted.extend(orders)

    engine._submit = submit
    trader = bare_trader(engine, SimpleNamespace())

    await trader._run_exit_safety()

    assert ctx.portfolio.quantity(SYM) == Decimal("6")
    assert ctx.portfolio.quantity(OTHER) == Decimal("8")
    # This broker keeps a strategy-local allocation.  The two shares sold in
    # the external app return their cost basis just like ordinary sync().
    assert ctx.portfolio.cash == pytest.approx(1_200.0)
    assert [(order.symbol.key, order.quantity) for order in submitted] == [
        (OTHER.key, Decimal("8")),
    ]


@pytest.mark.asyncio
async def test_live_fetch_never_consumes_a_still_forming_daily_bar():
    ctx = context()
    closed = Bar(SYM, T0 - timedelta(days=1), 100, 101, 99, 100, 10, "1d")
    forming = Bar(SYM, T0, 100, 200, 50, 180, 99, "1d")

    class Provider:
        async def latest_bars(self, _symbol, _timeframe, _count):
            return [closed, forming]

    engine = SimpleNamespace(ctx=ctx)
    trader = bare_trader(engine, Provider())
    trader.config = SimpleNamespace(data=SimpleNamespace(timeframe="1d"))
    trader._seen = {}

    bars = await trader._fetch_new_bars()

    assert bars == {SYM.key: closed}


@pytest.mark.asyncio
async def test_fresh_emergency_exit_is_not_canceled_every_maintenance_cycle():
    ctx = context()
    pos = ctx.portfolio.position(SYM)
    pos.quantity = Decimal("10")
    pos.avg_price = 100.0
    pos.mark(80.0)
    ctx.set_quote(Quote(SYM, T0, bid=79.0, ask=81.0))
    broker = TestLiveBroker(ctx.portfolio)
    emergency = Order(
        SYM,
        OrderSide.SELL,
        Decimal("10"),
        OrderType.MARKET,
        status=OrderStatus.SUBMITTED,
        meta={"emergency_exit": True},
    )
    broker._orders[emergency.id] = emergency
    engine = Engine(
        ctx,
        QuietAlpha(),
        EqualWeighting(),
        ImmediateExecution(),
        broker,
        risk_models=[MaximumDrawdownPerSecurity(max_drawdown_pct=0.10)],
    )
    submitted: list[Order] = []
    engine._submit = lambda orders: submitted.extend(orders) or asyncio.sleep(0)
    trader = bare_trader(engine, SimpleNamespace())

    await trader._run_exit_safety()

    assert emergency.status is OrderStatus.SUBMITTED
    assert submitted == []
    assert broker.sync_calls == 0


@pytest.mark.asyncio
async def test_manual_close_is_not_trapped_behind_a_buy_with_no_quote():
    ctx = context()
    held = ctx.portfolio.position(SYM)
    held.quantity = Decimal("3")
    held.avg_price = held.last_price = 100.0
    manual = ManualControl()
    close = manual.close(SYM)
    buy = manual.buy(OTHER, quantity=Decimal("1"))
    submitted: list[Order] = []

    async def submit(orders):
        submitted.extend(orders)

    async def flush_manual():
        orders = manual.build_orders(ctx)
        await submit(orders)
        return len(orders)

    async def refresh_pending():
        ctx.set_pending({})
        return True

    engine = SimpleNamespace(
        ctx=ctx,
        manual=manual,
        _submit=submit,
        flush_manual=flush_manual,
        _refresh_pending=refresh_pending,
        brokerage=object(),
    )
    trader = bare_trader(engine, SimpleNamespace())

    sent = await trader._flush_manual_quote_safe({SYM.key: "down", OTHER.key: "down"})

    assert sent == 1
    assert submitted[0].side is OrderSide.SELL
    assert submitted[0].quantity == Decimal("3")
    assert [request.id for request in manual.pending] == [buy.id]
    assert close.status == "submitted"
    assert buy.status == "pending"
    assert "보류" in buy.detail


@pytest.mark.asyncio
async def test_manual_close_never_duplicates_an_existing_full_exit_order():
    ctx = context()
    held = ctx.portfolio.position(SYM)
    held.quantity = Decimal("10")
    held.avg_price = held.last_price = 100.0
    broker = TestLiveBroker(ctx.portfolio)
    existing = Order(
        SYM, OrderSide.SELL, Decimal("10"), OrderType.LIMIT,
        limit_price=99.0, status=OrderStatus.SUBMITTED,
    )
    broker._orders[existing.id] = existing
    engine = engine_with(ctx, broker)
    request = engine.manual.close(SYM)

    submitted = await engine.flush_manual()

    assert submitted == 0
    assert request.status == "pending"
    assert [order.id for order in await broker.open_orders()] == [existing.id]
    assert len(engine.orders) == 0


@pytest.mark.asyncio
async def test_open_order_fill_is_booked_during_maintenance_and_poll_is_throttled():
    ctx = context()
    events = []
    ctx.bus.on(None, lambda event: events.append(event.type))
    broker = TestLiveBroker(ctx.portfolio)
    engine = engine_with(ctx, broker)
    order = Order(
        SYM, OrderSide.BUY, Decimal("1"), OrderType.LIMIT, limit_price=100.0,
        status=OrderStatus.SUBMITTED,
    )
    broker._orders[order.id] = order
    broker.next_fill = Fill(
        order.id, SYM, OrderSide.BUY, Decimal("1"), 100.0, 1.0, T0,
    )
    trader = bare_trader(engine, SimpleNamespace())

    first = await trader._poll_live_fills()
    second = await trader._poll_live_fills()

    assert len(first) == 1
    assert second == []
    assert broker.poll_calls == 1
    assert broker.sync_calls == 1
    assert ctx.portfolio.quantity(SYM) == Decimal("1")
    assert ctx.portfolio.cash == pytest.approx(899.0)
    assert events.count(EventType.ORDER_FILLED) == 1


@pytest.mark.asyncio
async def test_cached_terminal_fill_reconciles_capital_without_waiting_for_a_candle():
    ctx = context()
    broker = TestLiveBroker(ctx.portfolio)
    engine = engine_with(ctx, broker)
    order = Order(
        SYM, OrderSide.BUY, Decimal("1"), OrderType.LIMIT, limit_price=100.0,
        status=OrderStatus.FILLED,
    )
    broker._orders[order.id] = order
    broker._pending_fills.append(Fill(
        order.id, SYM, OrderSide.BUY, Decimal("1"), 100.0, 1.0, T0,
    ))
    trader = bare_trader(engine, SimpleNamespace())
    snapshots: list[tuple[Decimal, float]] = []

    class State:
        accounting_persistence_failed = False

        def record_accounting_event(self, _event_type, _payload):
            return None

        def snapshot_positions(self, portfolio):
            snapshots.append((portfolio.quantity(SYM), portfolio.cash))

        def mark_accounting_persistence_failed(self):
            self.accounting_persistence_failed = True

    trader.state = State()
    trader.notifier = SimpleNamespace(handle=lambda _event: None)
    trader._attach_observers()

    booked = await trader._poll_live_fills()

    assert len(booked) == 1
    assert broker.poll_calls == 0, "a cached fill needs no order-detail request"
    assert broker.sync_calls == 1
    assert ctx.portfolio.quantity(SYM) == Decimal("1")
    assert snapshots == [(Decimal("1"), 899.0)]


@pytest.mark.asyncio
async def test_unexpected_fill_poll_failure_blocks_later_submissions():
    ctx = context()
    broker = TestLiveBroker(ctx.portfolio)
    engine = engine_with(ctx, broker)
    order = Order(
        SYM, OrderSide.BUY, Decimal("1"), OrderType.LIMIT, limit_price=100.0,
        status=OrderStatus.SUBMITTED,
    )
    broker._orders[order.id] = order

    async def fail():
        raise RuntimeError("transport down")

    broker.poll_fills = fail
    trader = bare_trader(engine, SimpleNamespace())

    assert await trader._poll_live_fills() == []
    assert broker.fill_channel_ok is False
    assert "transport down" in broker.fill_channel_error


@pytest.mark.asyncio
async def test_fill_poll_budget_slows_a_ten_order_batch(monkeypatch):
    ctx = context()
    broker = TestLiveBroker(ctx.portfolio)
    engine = engine_with(ctx, broker)
    for index in range(10):
        order = Order(
            Symbol(f"{index:06d}", venue="toss", quote_currency="KRW"),
            OrderSide.BUY,
            Decimal("1"),
            OrderType.LIMIT,
            limit_price=100.0,
            status=OrderStatus.SUBMITTED,
        )
        broker._orders[order.id] = order
    trader = bare_trader(engine, SimpleNamespace())
    monkeypatch.setattr(trader_module.time, "monotonic", lambda: 100.0)

    await trader._poll_live_fills()

    assert trader._next_fill_poll_at == pytest.approx(110.0)


@pytest.mark.asyncio
async def test_final_submission_guard_runs_after_adapter_preflight():
    ctx = context()
    ctx.portfolio.mark(SYM, 100.0)
    broker = TestLiveBroker(ctx.portfolio)
    allowed = True
    venue_calls = 0

    async def preflight(_order):
        nonlocal allowed
        allowed = False

    async def venue_submit(_order):
        nonlocal venue_calls
        venue_calls += 1
        return "must-not-send"

    broker._pre_venue_submit = preflight
    broker._venue_submit = venue_submit
    broker.set_submission_guard(
        lambda _order: "정규장이 닫힘" if not allowed else "",
    )

    result = await broker.submit(
        Order(SYM, OrderSide.BUY, Decimal("1"), OrderType.MARKET),
    )

    assert result.status is OrderStatus.REJECTED
    assert "정규장이 닫힘" in result.reject_reason
    assert venue_calls == 0


@pytest.mark.asyncio
async def test_accounting_quarantine_blocks_new_exposure_but_allows_reduction():
    ctx = context()
    pos = ctx.portfolio.position(SYM)
    pos.quantity = Decimal("2")
    pos.avg_price = pos.last_price = 100.0
    broker = TestLiveBroker(ctx.portfolio)
    engine = engine_with(ctx, broker)
    trader = bare_trader(engine, SimpleNamespace())
    trader.state = SimpleNamespace(accounting_persistence_failed=True)
    trader._bind_submission_guard()

    buy = await broker.submit(
        Order(SYM, OrderSide.BUY, Decimal("1"), OrderType.MARKET),
    )
    sell = await broker.submit(
        Order(SYM, OrderSide.SELL, Decimal("1"), OrderType.MARKET),
    )

    assert buy.status is OrderStatus.REJECTED
    assert "회계 기록 저장" in buy.reject_reason
    assert sell.status is OrderStatus.SUBMITTED


@pytest.mark.asyncio
async def test_final_guard_rejects_stale_quote_but_allows_position_reduction():
    now = datetime.now(UTC)
    ctx = Context(
        SimClock(now), Portfolio(1_000, "KRW"), EventBus(),
        timeframe="1d", run_mode=RunMode.LIVE,
    )
    ctx.universe = [SYM]
    ctx.portfolio.mark(SYM, 100.0)
    ctx.set_quote(Quote(
        SYM, now - timedelta(seconds=LiveTrader.QUOTE_MAX_AGE_S + 1),
        bid=99.0, ask=101.0,
    ))
    broker = TestLiveBroker(ctx.portfolio)
    engine = engine_with(ctx, broker)
    trader = bare_trader(engine, SimpleNamespace())
    trader.state = SimpleNamespace(accounting_persistence_failed=False)
    trader._market_is_open = lambda: True
    trader._bind_submission_guard()

    buy = await broker.submit(
        Order(SYM, OrderSide.BUY, Decimal("1"), OrderType.MARKET),
    )

    position = ctx.portfolio.position(SYM)
    position.quantity = Decimal("2")
    position.avg_price = position.last_price = 100.0
    sell = await broker.submit(
        Order(SYM, OrderSide.SELL, Decimal("1"), OrderType.MARKET),
    )

    assert buy.status is OrderStatus.REJECTED
    assert "호가가" in buy.reject_reason and "전송 직전" in buy.reject_reason
    assert sell.status is OrderStatus.SUBMITTED


def test_accepted_small_clock_skew_uses_the_same_quote_for_sizing():
    now = datetime.now(UTC)
    ctx = Context(
        SimClock(now), Portfolio(1_000, "KRW"), EventBus(),
        timeframe="1d", run_mode=RunMode.LIVE,
    )
    quote = Quote(SYM, now + timedelta(seconds=1), bid=199.0, ask=201.0)
    ctx.set_quote(quote)
    broker = TestLiveBroker(ctx.portfolio)
    trader = bare_trader(engine_with(ctx, broker), SimpleNamespace())

    assert trader._quote_error(SYM, quote, now) == ""
    assert ctx.price(SYM) == 200.0


@pytest.mark.asyncio
async def test_fetch_that_crosses_the_close_does_not_consume_or_process_the_bar():
    ctx = context()
    bar = Bar(
        SYM, T0 - timedelta(minutes=1),
        100.0, 101.0, 99.0, 100.0, 1000.0, "1m",
    )

    class Provider:
        async def latest_bars(self, _symbol, _timeframe, _count):
            return [bar]

    processed = 0

    async def on_bars(_bars, *, settle=True):
        nonlocal processed
        processed += 1

    engine = SimpleNamespace(
        ctx=ctx,
        brokerage=object(),
        on_bars=on_bars,
    )
    trader = bare_trader(engine, Provider())
    trader.config = SimpleNamespace(data=SimpleNamespace(timeframe="1m"))
    trader._seen = {}
    trader._refresh_universe = lambda: asyncio.sleep(0)
    checks = iter((True, True, False))
    trader._market_is_open = lambda: next(checks)

    await trader._tick()

    assert processed == 0
    assert trader._seen == {}
    assert trader._decision_due_at_next_open is True


@pytest.mark.asyncio
async def test_closed_wait_marks_a_fresh_decision_due_when_the_market_opens():
    trader = LiveTrader.__new__(LiveTrader)
    trader.running = True
    trader._announced_closed = False
    trader._decision_due_at_next_open = False
    is_open = False

    class Calendar:
        name = "test venue"

        def is_open(self, _now):
            return is_open

        def next_open(self, now):
            return now + timedelta(seconds=1)

    async def sleep_to_open(_seconds):
        nonlocal is_open
        is_open = True
        return True

    trader.calendar = Calendar()
    trader._closed_market_deliberation = lambda: asyncio.sleep(0)
    trader._sleep_serving_manual = sleep_to_open

    assert await trader._wait_for_market() is True
    assert trader._decision_due_at_next_open is True
