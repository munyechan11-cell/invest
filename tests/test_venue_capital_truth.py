"""Account-authoritative live capital without breaking strategy sub-allocations."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from decimal import Decimal

import pytest

from quant.alpha.base import AlphaModel
from quant.brokerage.base import BrokerageError
from quant.brokerage.live_base import LiveBrokerage
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
    Symbol,
)
from quant.execution.models import ImmediateExecution
from quant.live.limits import TradingBudget
from quant.live.state import StateStore
from quant.live.trader import LiveTrader
from quant.portfolio.models import EqualWeighting

SYM = Symbol("005930", venue="toss", quote_currency="KRW")
OTHER = Symbol("000660", venue="toss", quote_currency="KRW")
THIRD = Symbol("035420", venue="toss", quote_currency="KRW")
T0 = datetime(2026, 8, 25, tzinfo=UTC)


def order(
    side: OrderSide,
    qty: str,
    price: float = 100.0,
    symbol: Symbol = SYM,
) -> Order:
    return Order(
        symbol=symbol,
        side=side,
        quantity=Decimal(qty),
        type=OrderType.LIMIT,
        limit_price=price,
    )


class VenueCapitalBroker(LiveBrokerage):
    name = "venue-capital-test"
    venue_capital_truth = True

    def __init__(self, portfolio: Portfolio, **kwargs):
        super().__init__(portfolio, live=True, max_order_notional=10_000_000, **kwargs)
        self.capital: dict = {
            "currency": "KRW",
            "cash": 420_000.0,
            "holdings_value": 0.0,
        }
        self.remote_positions: dict[str, Decimal] = {}
        self.remote_costs: dict[str, float] = {}
        self.capital_error: Exception | None = None
        self.capital_calls = 0
        self.sent: list[Order] = []

    async def _venue_capital(self) -> dict:
        self.capital_calls += 1
        if self.capital_error is not None:
            raise self.capital_error
        return dict(self.capital)

    async def _venue_positions(self) -> dict[str, Decimal]:
        return dict(self.remote_positions)

    async def _venue_costs(self) -> dict[str, float]:
        return dict(self.remote_costs)

    async def _venue_submit(self, item: Order) -> str:
        self.sent.append(item)
        return f"venue-{len(self.sent)}"

    async def _venue_cancel(self, item: Order) -> bool:
        return True


class SnapshotCachingBroker(VenueCapitalBroker):
    """Models adapters that share one response across sequential hooks."""

    def __init__(self, portfolio: Portfolio):
        super().__init__(portfolio)
        self.snapshot_in_use = False

    async def _venue_capital(self) -> dict:
        if self.snapshot_in_use:
            raise BrokerageError("another sync replaced the cached snapshot")
        self.snapshot_in_use = True
        await asyncio.sleep(0)
        return dict(self.capital)

    async def _venue_positions(self) -> dict[str, Decimal]:
        await asyncio.sleep(0)
        self.snapshot_in_use = False
        return dict(self.remote_positions)


class ReapFailBroker(VenueCapitalBroker):
    async def poll_fills(self):
        raise BrokerageError("execution lookup unavailable")


class UnconfirmedCancelBroker(VenueCapitalBroker):
    async def _venue_cancel(self, item: Order) -> bool:
        return False


class VerifiedThenOtherOrderFailsBroker(VenueCapitalBroker):
    def __init__(self, portfolio: Portfolio, verified_fill: Fill):
        super().__init__(portfolio)
        self.verified_fill = verified_fill
        self.injected = False
        self.fail_remote = True

    async def poll_fills(self):
        if not self.injected:
            self._pending_fills.append(self.verified_fill)
            self.injected = True
        if self.fail_remote:
            raise BrokerageError("second order detail unavailable")
        return await super().poll_fills()


class ObservingAlpha(AlphaModel):
    name = "account-observer"

    def __init__(self):
        self.seen: list[tuple[float, Decimal]] = []

    async def update(self, ctx, bars):
        self.seen.append((ctx.portfolio.cash, ctx.portfolio.quantity(SYM)))
        return []


async def test_actual_cash_and_stock_value_replace_a_legacy_live_number():
    pf = Portfolio(800_000.0, "KRW")
    pf.mark(SYM, 100.0)  # makes the configured instrument safe to reconcile
    broker = VenueCapitalBroker(pf)
    broker.capital = {"currency": "KRW", "cash": 120_000, "holdings_value": 300_000}
    broker.remote_positions = {SYM.key: Decimal("3")}
    broker.remote_costs = {SYM.key: 80.0}

    report = await broker.sync()

    assert report["ok"] and report["capital_ready"]
    assert pf.capital_source == "venue"
    assert pf.cash == pytest.approx(120_000.0)
    assert pf.holdings_value == pytest.approx(300_000.0)
    assert pf.gross_exposure == pytest.approx(300_000.0)
    assert pf.equity == pytest.approx(420_000.0)
    assert pf.performance_baseline == pytest.approx(420_000.0)
    assert pf.high_water_mark == pytest.approx(420_000.0)
    assert pf.total_return == pytest.approx(0.0)
    # Stock valuation contributes to equity/exposure, never to spendable cash.
    assert pf.free_cash() == pytest.approx(120_000.0)


async def test_concurrent_syncs_cannot_mix_two_cached_account_snapshots():
    pf = Portfolio(800_000.0, "KRW")
    pf.mark(SYM, 100.0)
    broker = SnapshotCachingBroker(pf)

    first, second = await asyncio.gather(broker.sync(), broker.sync())

    assert first["ok"] and second["ok"]
    assert broker.account_ready
    assert not broker.snapshot_in_use


async def test_tick_books_a_cumulative_fill_once_then_reconciles_before_strategy():
    pf = Portfolio(100.0, "KRW")
    pf.mark(SYM, 10.0)
    pf.adopt_venue_capital(cash=20.0, holdings_value=80.0)
    held = pf.position(SYM)
    held.quantity = Decimal("8")
    held.avg_price = 10.0

    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 20.0, "holdings_value": 80.0}
    broker.remote_positions = {SYM.key: Decimal("8")}
    broker.remote_costs = {SYM.key: 10.0}
    closing = order(OrderSide.SELL, "8", price=10.0)
    await broker.submit(closing)
    broker.capital = {"cash": 98.0, "holdings_value": 0.0}
    broker.remote_positions = {}
    fill = Fill(
        closing.id, SYM, OrderSide.SELL, Decimal("8"), 10.0, 2.0, T0,
        tag="stop_loss",
    )
    closing.apply_fill(fill)  # the adapter tracks cumulative filled quantity
    broker._pending_fills.append(fill)  # the portfolio has not booked it yet

    bus = EventBus()
    events = []
    bus.on(None, lambda event: events.append(event.type))
    ctx = Context(SimClock(T0), pf, bus, timeframe="1m")
    ctx.universe = [SYM]
    alpha = ObservingAlpha()
    engine = Engine(
        ctx,
        alpha,
        EqualWeighting(),
        ImmediateExecution(),
        broker,
    )
    trader = LiveTrader.__new__(LiveTrader)
    trader.engine = engine
    trader.errors = 0
    trader.max_errors = 10
    trader.last_bar_ts = None

    async def no_refresh():
        return None

    batches = [
        {SYM.key: Bar(SYM, T0, 10.0, 10.0, 10.0, 10.0, 1_000.0, "1m")},
        {},
    ]

    async def fetch():
        return batches.pop(0)

    trader._refresh_universe = no_refresh
    trader._fetch_new_bars = fetch

    await trader._tick()
    await trader._tick()  # the same cumulative venue state must stay idempotent

    assert alpha.seen == [(98.0, Decimal("0"))]
    assert pf.cash == pytest.approx(98.0)
    assert pf.quantity(SYM) == 0
    assert pf.total_fees == pytest.approx(2.0)
    assert len(pf.closed_trades) == 1
    assert engine.budget.today.fees == pytest.approx(2.0)
    assert events.count(EventType.ORDER_FILLED) == 1
    assert events.count(EventType.TRADE_CLOSED) == 1


async def test_successful_live_fill_drain_is_exactly_once():
    pf = Portfolio(100.0, "KRW")
    pf.mark(SYM, 10.0)
    broker = VenueCapitalBroker(pf)
    fill = Fill("verified", SYM, OrderSide.BUY, Decimal("1"), 10.0, 1.0, T0)
    broker._pending_fills.append(fill)
    bus = EventBus()
    events = []
    bus.on(None, lambda event: events.append(event.type))
    engine = Engine(
        Context(SimClock(T0), pf, bus, timeframe="1m"),
        ObservingAlpha(),
        EqualWeighting(),
        ImmediateExecution(),
        broker,
    )

    first = await engine.settle_live_fills()
    second = await engine.settle_live_fills()

    assert first == [fill]
    assert second == []
    assert pf.quantity(SYM) == 1
    assert pf.cash == pytest.approx(89.0)
    assert pf.total_fees == pytest.approx(1.0)
    assert events.count(EventType.ORDER_FILLED) == 1


async def test_verified_fill_is_booked_once_even_when_another_order_poll_fails():
    pf = Portfolio(100.0, "KRW")
    pf.mark(SYM, 10.0)
    entry = Fill("entry", SYM, OrderSide.BUY, Decimal("1"), 10.0, 1.0, T0)
    pf.apply_fill(entry)
    exit_fill = Fill("exit", SYM, OrderSide.SELL, Decimal("1"), 12.0, 1.0, T0)
    broker = VerifiedThenOtherOrderFailsBroker(pf, exit_fill)
    bus = EventBus()
    events = []
    bus.on(None, lambda event: events.append(event.type))
    engine = Engine(
        Context(SimClock(T0), pf, bus, timeframe="1m"),
        ObservingAlpha(),
        EqualWeighting(),
        ImmediateExecution(),
        broker,
    )

    with pytest.raises(BrokerageError, match="second order detail unavailable"):
        await engine.settle_live_fills()
    # A repeated failure has an empty verified queue and cannot double-book.
    with pytest.raises(BrokerageError, match="second order detail unavailable"):
        await engine.settle_live_fills()
    broker.fail_remote = False
    assert await engine.settle_live_fills() == []

    assert pf.quantity(SYM) == 0
    assert pf.cash == pytest.approx(100.0)
    assert pf.total_fees == pytest.approx(2.0)
    assert len(pf.closed_trades) == 1
    assert engine.budget.today.fees == pytest.approx(1.0)
    assert events.count(EventType.ORDER_FILLED) == 1
    assert events.count(EventType.TRADE_CLOSED) == 1


async def test_later_sync_preserves_the_real_starting_baseline_and_high_water():
    pf = Portfolio(800_000.0, "KRW")
    pf.mark(SYM, 100.0)
    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 120_000, "holdings_value": 300_000}
    broker.remote_positions = {SYM.key: Decimal("3")}
    broker.remote_costs = {SYM.key: 80.0}
    await broker.sync()

    broker.capital = {"cash": 130_000, "holdings_value": 350_000}
    await broker.sync()

    assert pf.performance_baseline == pytest.approx(420_000.0)
    assert pf.equity == pytest.approx(480_000.0)
    assert pf.high_water_mark == pytest.approx(480_000.0)
    assert pf.total_return == pytest.approx(480_000 / 420_000 - 1)


async def test_capital_failure_is_atomic_and_blocks_new_exposure():
    pf = Portfolio(800_000.0, "KRW")
    pf.mark(SYM, 100.0)
    broker = VenueCapitalBroker(pf)
    await broker.connect()
    before = (pf.cash, pf.holdings_value, pf.equity)

    broker.capital_error = BrokerageError("buying power unavailable")
    item = order(OrderSide.BUY, "1")
    await broker.submit(item)

    assert item.status is OrderStatus.REJECTED
    assert "실계좌" in item.reject_reason
    assert broker.sent == []
    assert not broker.account_ready
    assert (pf.cash, pf.holdings_value, pf.equity) == before


async def test_a_buy_cannot_exceed_fresh_cash_buying_power():
    pf = Portfolio(800_000.0, "KRW")
    pf.mark(SYM, 100.0)
    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 100.0, "holdings_value": 0.0}

    item = order(OrderSide.BUY, "2")
    await broker.submit(item)

    assert broker.capital_calls == 1, "new exposure must refresh immediately before submit"
    assert item.status is OrderStatus.REJECTED
    assert "매수 가능 금액" in item.reject_reason
    assert broker.sent == []


async def test_two_open_buys_cannot_spend_the_same_stale_buying_power_twice():
    pf = Portfolio(800_000.0, "KRW")
    pf.mark(SYM, 10.0)
    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 100.0, "holdings_value": 0.0}

    first = order(OrderSide.BUY, "8", price=10.0)
    second = order(OrderSide.BUY, "3", price=10.0)
    await broker.submit(first)
    await broker.submit(second)

    assert first.status is OrderStatus.SUBMITTED
    assert second.status is OrderStatus.REJECTED
    assert "미결 주문" in second.reject_reason
    assert broker.sent == [first]


async def test_even_a_small_second_buy_waits_for_the_first_order_to_settle():
    pf = Portfolio(800_000.0, "KRW")
    pf.mark(SYM, 10.0)
    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 100.0, "holdings_value": 0.0}

    first = order(OrderSide.BUY, "8", price=10.0)
    await broker.submit(first)
    within_remaining_cash = order(OrderSide.BUY, "1", price=10.0)
    await broker.submit(within_remaining_cash)

    assert within_remaining_cash.status is OrderStatus.REJECTED
    assert "미결 주문" in within_remaining_cash.reject_reason
    assert broker.sent == [first]


async def test_fill_between_empty_poll_and_sync_defers_venue_mutation():
    pf = Portfolio(100.0, "KRW")
    pf.mark(SYM, 10.0)
    pf.adopt_venue_capital(cash=20.0, holdings_value=80.0)
    held = pf.position(SYM)
    held.quantity = Decimal("8")
    held.avg_price = 10.0
    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 20.0, "holdings_value": 80.0}
    broker.remote_positions = {SYM.key: Decimal("8")}
    broker.remote_costs = {SYM.key: 10.0}
    closing = order(OrderSide.SELL, "8", price=10.0)
    await broker.submit(closing)  # the preceding order poll saw no fill
    broker.capital = {"cash": 98.0, "holdings_value": 0.0}
    broker.remote_positions = {}

    report = await broker.sync()  # holdings already sees the just-landed fill

    assert not report["ok"]
    assert report["transient"] == "open_orders"
    assert not broker.account_ready
    assert pf.cash == pytest.approx(20.0)
    assert pf.quantity(SYM) == Decimal("8")

    fill = Fill(closing.id, SYM, OrderSide.SELL, Decimal("8"), 10.0, 2.0, T0)
    closing.apply_fill(fill)
    pf.apply_fill(fill)  # next poll/book owns the accounting event
    report = await broker.sync()

    assert report["ok"]
    assert pf.cash == pytest.approx(98.0)
    assert pf.quantity(SYM) == 0


async def test_terminal_sell_waits_for_cash_and_holdings_to_reflect_the_exit():
    pf = Portfolio(100.0, "KRW")
    pf.mark(SYM, 10.0)
    pf.adopt_venue_capital(cash=20.0, holdings_value=80.0)
    held = pf.position(SYM)
    held.quantity = Decimal("8")
    held.avg_price = 10.0
    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 20.0, "holdings_value": 80.0}
    broker.remote_positions = {SYM.key: Decimal("8")}
    broker.remote_costs = {SYM.key: 10.0}
    closing = order(OrderSide.SELL, "8", price=10.0)
    await broker.submit(closing)
    fill = Fill(closing.id, SYM, OrderSide.SELL, Decimal("8"), 10.0, 2.0, T0)
    closing.apply_fill(fill)
    pf.apply_fill(fill)

    report = await broker.sync()  # both venue endpoints are still pre-fill

    assert report["transient"] == "terminal_order_settlement"
    assert pf.cash == pytest.approx(98.0)
    assert pf.quantity(SYM) == 0

    broker.capital = {"cash": 98.0, "holdings_value": 0.0}
    broker.remote_positions = {}
    report = await broker.sync()

    assert report["ok"]
    assert broker.account_ready
    assert pf.cash == pytest.approx(98.0)
    assert pf.quantity(SYM) == 0


async def test_partial_fill_keeps_account_mutation_deferred_until_terminal():
    pf = Portfolio(100.0, "KRW")
    pf.mark(SYM, 10.0)
    broker = VenueCapitalBroker(pf)
    await broker.connect()
    pending = order(OrderSide.BUY, "8", price=10.0)
    pending.status = OrderStatus.PARTIAL
    pending.filled_qty = Decimal("3")
    pending.avg_fill_price = 10.0
    broker._orders[pending.id] = pending
    before = (pf.cash, pf.holdings_value)
    broker.capital = {"cash": 70.0, "holdings_value": 30.0}
    broker.remote_positions = {SYM.key: Decimal("3")}
    broker.remote_costs = {SYM.key: 10.0}

    report = await broker.sync()

    assert report["transient"] == "open_orders"
    assert not broker.account_ready
    assert (pf.cash, pf.holdings_value) == before


async def test_checkpoint_cannot_settle_while_its_order_is_still_nonterminal():
    pf = Portfolio(100.0, "KRW")
    pf.mark(SYM, 10.0)
    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 100.0, "holdings_value": 0.0}
    pending = order(OrderSide.BUY, "8", price=10.0)
    await broker.submit(pending)
    fill = Fill(pending.id, SYM, OrderSide.BUY, Decimal("3"), 10.0, 0.0, T0)
    pending.apply_fill(fill)
    pf.apply_fill(fill)
    # Even if a separate bug loses the local open-order index, matching account
    # cash and holdings do not prove that the remaining five shares cannot fill.
    broker._orders.pop(pending.id)
    broker.capital = {"cash": 70.0, "holdings_value": 30.0}
    broker.remote_positions = {SYM.key: Decimal("3")}
    broker.remote_costs = {SYM.key: 10.0}

    report = await broker.sync()

    assert not report["ok"]
    assert report["transient"] == "terminal_order_settlement"
    assert report["unsettled_orders"][pending.id]["reason"] == (
        "order checkpoint is not terminal"
    )
    assert pending.id in broker._capital_order_checkpoints
    assert not broker.account_ready


async def test_cancelled_buy_releases_the_local_reservation_on_fresh_sync():
    pf = Portfolio(800_000.0, "KRW")
    pf.mark(SYM, 10.0)
    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 100.0, "holdings_value": 0.0}

    first = order(OrderSide.BUY, "8", price=10.0)
    await broker.submit(first)
    assert await broker.cancel(first)
    second = order(OrderSide.BUY, "7", price=10.0)
    await broker.submit(second)

    assert first.status is OrderStatus.CANCELED
    assert second.status is OrderStatus.SUBMITTED
    assert broker.sent == [first, second]


@pytest.mark.parametrize("status", [OrderStatus.CANCELED, OrderStatus.REJECTED])
async def test_observed_zero_fill_terminal_releases_its_checkpoint(status):
    pf = Portfolio(800_000.0, "KRW")
    pf.mark(SYM, 10.0)
    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 100.0, "holdings_value": 0.0}

    first = order(OrderSide.BUY, "8", price=10.0)
    await broker.submit(first)
    first.status = status
    broker._mark_terminal_observed(first)

    report = await broker.sync()
    second = order(OrderSide.BUY, "7", price=10.0)
    await broker.submit(second)

    assert report["ok"] and report["capital_ready"]
    assert first.id not in broker._capital_order_checkpoints
    assert first.id not in broker._capital_reservations
    assert second.status is OrderStatus.SUBMITTED
    assert broker.sent == [first, second]


async def test_failed_cancel_reap_never_mistakes_unknown_fills_for_zero():
    pf = Portfolio(800_000.0, "KRW")
    pf.mark(SYM, 10.0)
    broker = ReapFailBroker(pf)
    broker.capital = {"cash": 100.0, "holdings_value": 0.0}

    first = order(OrderSide.BUY, "8", price=10.0)
    await broker.submit(first)
    assert await broker.cancel(first)
    second = order(OrderSide.BUY, "3", price=10.0)
    await broker.submit(second)

    assert first.status is OrderStatus.CANCELED
    assert second.status is OrderStatus.REJECTED
    assert "종료 주문" in second.reject_reason
    assert broker.sent == [first]


async def test_unconfirmed_cancel_keeps_the_order_and_capital_guards_intact():
    pf = Portfolio(800_000.0, "KRW")
    pf.mark(SYM, 10.0)
    broker = UnconfirmedCancelBroker(pf)
    broker.capital = {"cash": 100.0, "holdings_value": 0.0}
    pending = order(OrderSide.BUY, "8", price=10.0)
    await broker.submit(pending)

    assert not await broker.cancel(pending)

    assert pending.status is OrderStatus.SUBMITTED
    assert pending.id in broker._orders
    assert pending.id in broker._capital_order_checkpoints
    assert pending.id in broker._capital_reservations


async def test_same_symbol_order_waits_for_the_previous_checkpoint_but_other_exit_does_not():
    pf = Portfolio(100.0, "KRW")
    pf.mark(SYM, 10.0)
    pf.mark(OTHER, 20.0)
    pf.adopt_venue_capital(cash=0.0, holdings_value=100.0)
    first_position = pf.position(SYM)
    first_position.quantity = Decimal("8")
    first_position.avg_price = 10.0
    other_position = pf.position(OTHER)
    other_position.quantity = Decimal("1")
    other_position.avg_price = 20.0
    broker = VenueCapitalBroker(pf)

    first = order(OrderSide.SELL, "4", price=10.0)
    duplicate_exit = order(OrderSide.SELL, "4", price=10.0)
    independent_exit = order(OrderSide.SELL, "1", price=20.0, symbol=OTHER)
    await broker.submit(first)
    await broker.submit(duplicate_exit)
    await broker.submit(independent_exit)

    assert first.status is OrderStatus.SUBMITTED
    assert duplicate_exit.status is OrderStatus.REJECTED
    assert "같은 종목" in duplicate_exit.reject_reason
    assert independent_exit.status is OrderStatus.SUBMITTED
    assert broker.sent == [first, independent_exit]


async def test_filled_status_cannot_release_cash_before_buying_power_reflects_it():
    pf = Portfolio(800_000.0, "KRW")
    pf.mark(SYM, 10.0)
    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 100.0, "holdings_value": 0.0}

    first = order(OrderSide.BUY, "8", price=10.0)
    await broker.submit(first)
    fill = Fill(first.id, SYM, OrderSide.BUY, Decimal("8"), 10.0, 0.0, T0)
    first.apply_fill(fill)
    pf.apply_fill(fill)
    broker.capital = {"cash": 100.0, "holdings_value": 80.0}
    broker.remote_positions = {SYM.key: Decimal("8")}
    broker.remote_costs = {SYM.key: 10.0}
    second = order(OrderSide.BUY, "7", price=10.0)
    await broker.submit(second)

    assert second.status is OrderStatus.REJECTED
    assert "종료 주문" in second.reject_reason
    assert broker.sent == [first]

    # Only after the cash endpoint itself includes the 80 debit may the
    # reservation clear and the confirmed remaining 20 become available.
    broker.capital = {"cash": 20.0, "holdings_value": 80.0}
    third = order(OrderSide.BUY, "1", price=10.0)
    await broker.submit(third)
    assert third.status is OrderStatus.SUBMITTED


async def test_a_partial_unrelated_cash_drop_cannot_impersonate_the_full_buy_debit():
    pf = Portfolio(800_000.0, "KRW")
    pf.mark(SYM, 10.0)
    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 100.0, "holdings_value": 0.0}

    first = order(OrderSide.BUY, "8", price=10.0)
    await broker.submit(first)
    fill = Fill(first.id, SYM, OrderSide.BUY, Decimal("8"), 10.0, 0.0, T0)
    first.apply_fill(fill)
    pf.apply_fill(fill)
    broker.remote_positions = {SYM.key: Decimal("8")}
    broker.remote_costs = {SYM.key: 10.0}
    broker.capital = {"cash": 50.0, "holdings_value": 80.0}

    report = await broker.sync()

    assert not report["ok"]
    assert report["transient"] == "terminal_order_settlement"
    assert "exact-once local fill ledger" in report["unsettled_orders"][first.id]["reason"]
    assert not broker.account_ready
    assert first.id in broker._capital_reservations


async def test_opposite_cash_flows_reconcile_as_one_exact_terminal_batch():
    pf = Portfolio(200.0, "KRW")
    for symbol in (SYM, OTHER, THIRD):
        pf.mark(symbol, 10.0)
    pf.adopt_venue_capital(cash=100.0, holdings_value=150.0)
    for symbol, qty in ((OTHER, "10"), (THIRD, "5")):
        position = pf.position(symbol)
        position.quantity = Decimal(qty)
        position.avg_price = 10.0
    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 100.0, "holdings_value": 150.0}
    broker.remote_positions = {
        OTHER.key: Decimal("10"), THIRD.key: Decimal("5"),
    }
    broker.remote_costs = {OTHER.key: 10.0, THIRD.key: 10.0}

    entering = order(OrderSide.BUY, "8", price=10.0)
    first_exit = order(OrderSide.SELL, "10", price=10.0, symbol=OTHER)
    second_exit = order(OrderSide.SELL, "5", price=10.0, symbol=THIRD)
    await broker.submit(entering)
    await broker.submit(first_exit)
    await broker.submit(second_exit)

    fills = (
        Fill(entering.id, SYM, OrderSide.BUY, Decimal("8"), 10.0, 0.0, T0),
        Fill(first_exit.id, OTHER, OrderSide.SELL, Decimal("10"), 10.0, 0.0, T0),
        Fill(second_exit.id, THIRD, OrderSide.SELL, Decimal("5"), 10.0, 0.0, T0),
    )
    for submitted, fill in zip((entering, first_exit, second_exit), fills):
        submitted.apply_fill(fill)
        pf.apply_fill(fill)
    broker.capital = {"cash": 170.0, "holdings_value": 80.0}
    broker.remote_positions = {SYM.key: Decimal("8")}
    broker.remote_costs = {SYM.key: 10.0}

    report = await broker.sync()

    assert report["ok"] and report["capital_ready"]
    assert pf.cash == pytest.approx(170.0)
    assert pf.quantity(SYM) == 8
    assert pf.quantity(OTHER) == 0
    assert pf.quantity(THIRD) == 0
    assert not broker._capital_order_checkpoints
    assert not broker._capital_reservations


async def test_zero_fill_and_filled_orders_release_together_only_after_batch_proof():
    pf = Portfolio(200.0, "KRW")
    pf.mark(SYM, 10.0)
    pf.mark(OTHER, 10.0)
    pf.adopt_venue_capital(cash=100.0, holdings_value=100.0)
    held = pf.position(OTHER)
    held.quantity = Decimal("10")
    held.avg_price = 10.0
    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 100.0, "holdings_value": 100.0}
    broker.remote_positions = {OTHER.key: Decimal("10")}
    broker.remote_costs = {OTHER.key: 10.0}

    canceled_entry = order(OrderSide.BUY, "8", price=10.0)
    closing = order(OrderSide.SELL, "10", price=10.0, symbol=OTHER)
    await broker.submit(canceled_entry)
    canceled_entry.status = OrderStatus.CANCELED
    broker._mark_terminal_observed(canceled_entry)
    await broker.submit(closing)
    fill = Fill(closing.id, OTHER, OrderSide.SELL, Decimal("10"), 10.0, 0.0, T0)
    closing.apply_fill(fill)
    pf.apply_fill(fill)
    broker.capital = {"cash": 200.0, "holdings_value": 0.0}
    broker.remote_positions = {}
    broker.remote_costs = {}

    report = await broker.sync()

    assert report["ok"] and report["capital_ready"]
    assert not broker._capital_order_checkpoints
    assert not broker._capital_reservations


@pytest.mark.parametrize("venue_cash", [19.0, 21.0])
async def test_external_cash_movement_cannot_impersonate_terminal_batch_settlement(
    venue_cash,
):
    pf = Portfolio(100.0, "KRW")
    pf.mark(SYM, 10.0)
    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 100.0, "holdings_value": 0.0}
    entering = order(OrderSide.BUY, "8", price=10.0)
    await broker.submit(entering)
    fill = Fill(entering.id, SYM, OrderSide.BUY, Decimal("8"), 10.0, 0.0, T0)
    entering.apply_fill(fill)
    pf.apply_fill(fill)
    broker.capital = {"cash": venue_cash, "holdings_value": 80.0}
    broker.remote_positions = {SYM.key: Decimal("8")}
    broker.remote_costs = {SYM.key: 10.0}

    report = await broker.sync()

    assert not report["ok"]
    assert report["transient"] == "terminal_order_settlement"
    assert entering.id in broker._capital_order_checkpoints
    assert not broker.account_ready


async def test_one_quantity_mismatch_keeps_every_checkpoint_in_the_batch():
    pf = Portfolio(200.0, "KRW")
    pf.mark(SYM, 10.0)
    pf.mark(OTHER, 10.0)
    pf.adopt_venue_capital(cash=0.0, holdings_value=200.0)
    for symbol in (SYM, OTHER):
        position = pf.position(symbol)
        position.quantity = Decimal("10")
        position.avg_price = 10.0
    broker = VenueCapitalBroker(pf)

    first = order(OrderSide.SELL, "10", price=10.0)
    second = order(OrderSide.SELL, "10", price=10.0, symbol=OTHER)
    await broker.submit(first)
    await broker.submit(second)
    for submitted in (first, second):
        fill = Fill(
            submitted.id, submitted.symbol, OrderSide.SELL,
            Decimal("10"), 10.0, 0.0, T0,
        )
        submitted.apply_fill(fill)
        pf.apply_fill(fill)
    broker.capital = {"cash": 200.0, "holdings_value": 100.0}
    broker.remote_positions = {OTHER.key: Decimal("10")}
    broker.remote_costs = {OTHER.key: 10.0}

    first_report = await broker.sync()

    assert not first_report["ok"]
    assert set(first_report["unsettled_orders"]) == {first.id, second.id}
    assert set(broker._capital_order_checkpoints) == {first.id, second.id}

    broker.capital = {"cash": 200.0, "holdings_value": 0.0}
    broker.remote_positions = {}
    broker.remote_costs = {}
    second_report = await broker.sync()

    assert second_report["ok"] and second_report["capital_ready"]
    assert not broker._capital_order_checkpoints


async def test_sell_settlement_uses_exact_symbol_quantity_not_total_holdings_value():
    pf = Portfolio(200.0, "KRW")
    pf.mark(SYM, 10.0)
    pf.mark(OTHER, 100.0)
    pf.adopt_venue_capital(cash=20.0, holdings_value=180.0)
    sold = pf.position(SYM)
    sold.quantity = Decimal("8")
    sold.avg_price = 10.0
    remaining = pf.position(OTHER)
    remaining.quantity = Decimal("1")
    remaining.avg_price = 100.0
    broker = VenueCapitalBroker(pf)

    closing = order(OrderSide.SELL, "8", price=10.0)
    await broker.submit(closing)
    fill = Fill(closing.id, SYM, OrderSide.SELL, Decimal("8"), 10.0, 2.0, T0)
    closing.apply_fill(fill)
    pf.apply_fill(fill)
    broker.capital = {"cash": 98.0, "holdings_value": 150.0}
    broker.remote_positions = {OTHER.key: Decimal("1")}
    broker.remote_costs = {OTHER.key: 100.0}

    report = await broker.sync()

    assert report["ok"] and report["capital_ready"]
    assert pf.quantity(SYM) == 0
    assert pf.quantity(OTHER) == 1
    assert pf.holdings_value == pytest.approx(150.0)


async def test_partial_fill_cancel_waits_for_the_actual_debit_before_releasing():
    pf = Portfolio(800_000.0, "KRW")
    pf.mark(SYM, 10.0)
    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 100.0, "holdings_value": 0.0}

    first = order(OrderSide.BUY, "8", price=10.0)
    await broker.submit(first)
    fill = Fill(first.id, SYM, OrderSide.BUY, Decimal("3"), 10.0, 0.0, T0)
    first.apply_fill(fill)
    pf.apply_fill(fill)
    assert await broker.cancel(first)
    broker.capital = {"cash": 100.0, "holdings_value": 30.0}
    broker.remote_positions = {SYM.key: Decimal("3")}
    broker.remote_costs = {SYM.key: 10.0}

    stale = order(OrderSide.BUY, "7", price=10.0)
    await broker.submit(stale)
    assert stale.status is OrderStatus.REJECTED
    assert broker.sent == [first]

    broker.capital = {"cash": 70.0, "holdings_value": 30.0}
    reflected = order(OrderSide.BUY, "7", price=10.0)
    calls_before_reflected_submit = broker.capital_calls
    await broker.submit(reflected)
    assert reflected.status is OrderStatus.SUBMITTED
    assert broker.sent == [first, reflected]
    assert broker.capital_calls == calls_before_reflected_submit + 1


async def test_an_exit_stays_available_during_an_account_lookup_outage():
    pf = Portfolio(800_000.0, "KRW")
    pf.mark(SYM, 100.0)
    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 0.0, "holdings_value": 200.0}
    broker.remote_positions = {SYM.key: Decimal("2")}
    broker.remote_costs = {SYM.key: 100.0}
    await broker.connect()

    broker.capital_error = BrokerageError("temporary outage")
    await broker.sync()  # runtime refresh marks new exposure unsafe
    assert not broker.account_ready
    calls = broker.capital_calls

    item = order(OrderSide.SELL, "1")
    await broker.submit(item)

    assert item.status is OrderStatus.SUBMITTED
    assert broker.sent == [item]
    assert broker.capital_calls == calls, "an exit must not wait on a broken balance lookup"


async def test_an_oversell_is_not_treated_as_an_exit_during_an_outage():
    pf = Portfolio(800_000.0, "KRW")
    pf.mark(SYM, 100.0)
    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 0.0, "holdings_value": 100.0}
    broker.remote_positions = {SYM.key: Decimal("1")}
    broker.remote_costs = {SYM.key: 100.0}
    await broker.connect()
    broker.capital_error = BrokerageError("temporary outage")

    item = order(OrderSide.SELL, "2")
    await broker.submit(item)

    assert item.status is OrderStatus.REJECTED
    assert "실계좌" in item.reject_reason
    assert broker.sent == []


async def test_an_oversell_cannot_bypass_the_daily_new_exposure_cap():
    pf = Portfolio(800_000.0, "KRW")
    pf.mark(SYM, 100.0)
    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 1_000.0, "holdings_value": 100.0}
    broker.remote_positions = {SYM.key: Decimal("1")}
    broker.remote_costs = {SYM.key: 100.0}
    await broker.connect()
    broker.budget = TradingBudget(max_daily_orders=1)
    broker.budget.roll(equity=pf.equity).orders = 1

    item = order(OrderSide.SELL, "2")
    await broker.submit(item)

    assert item.status is OrderStatus.REJECTED
    assert "주문 건수 한도" in item.reject_reason
    assert broker.sent == []


async def test_live_connect_fails_closed_before_any_account_truth_exists():
    pf = Portfolio(800_000.0, "KRW")
    pf.mark(SYM, 100.0)
    broker = VenueCapitalBroker(pf)
    broker.capital_error = BrokerageError("account unavailable")

    with pytest.raises(BrokerageError, match="실거래를 시작하지 않습니다"):
        await broker.connect()

    assert pf.capital_source == "configured"
    assert pf.cash == pytest.approx(800_000.0)


async def test_restart_refuses_unexplained_loss_fill_drift_and_blocks_next_buy(
    tmp_path,
):
    """Accepted SELL vanished in the crash, so venue truth cannot become a fill."""
    db = str(tmp_path / "crashed-sell.db")
    first = StateStore(db)
    first.start_run("crashed-sell", "live", 100.0)
    before_crash = Portfolio(100.0, "KRW")
    before_crash.mark(SYM, 10.0)
    before_crash.adopt_venue_capital(cash=100.0, holdings_value=100.0)
    held = before_crash.position(SYM)
    held.quantity = Decimal("10")
    held.avg_price = 10.0
    first.snapshot_positions(before_crash)
    first.mark_reconciliation_required()
    first.close()

    restored_state = StateStore(db)
    assert restored_state.resume_run("crashed-sell", "live")
    pf = Portfolio(100.0, "KRW")
    pf.mark(SYM, 10.0)
    restored_state.restore_positions(pf, {SYM.key: SYM})
    assert restored_state.restored_venue_truth
    assert restored_state.restored_reconciliation_required

    broker = VenueCapitalBroker(pf)
    broker.expect_restored_venue_truth(
        restored_state.restored_venue_truth,
        reconciliation_required=restored_state.restored_reconciliation_required,
    )
    # The process crashed after an accepted stop-loss SELL but before it stored
    # the order id or polled its fill. Account endpoints lag one poll, so the
    # first response still matches the stored pre-fill state exactly.
    broker.capital = {"cash": 100.0, "holdings_value": 100.0}
    broker.remote_positions = {SYM.key: Decimal("10")}
    broker.remote_costs = {SYM.key: 10.0}
    broker.budget = TradingBudget(max_daily_loss=1.0)

    stale = await broker.sync()

    assert not stale["ok"]
    assert stale["transient"] == "restored_run_requires_reconciliation"
    assert stale["recovery_required"]
    assert "자동 주문 이력 재구성은 지원하지 않습니다" in stale["recovery"]
    assert not broker.account_ready
    assert pf.cash == pytest.approx(100.0)
    assert pf.quantity(SYM) == Decimal("10")
    assert pf.closed_trades == []
    assert not broker.budget.halted

    # The next poll finally reflects the terminal loss-making SELL. Quarantine
    # must remain armed; otherwise normal drift adoption erases the closed trade
    # and leaves the daily realized-loss ledger at zero.
    broker.capital = {"cash": 150.0, "holdings_value": 0.0}
    broker.remote_positions = {}
    broker.remote_costs = {}
    settled = await broker.sync()

    assert not settled["ok"]
    assert settled["transient"] == "restored_run_requires_reconciliation"
    assert pf.cash == pytest.approx(100.0)
    assert pf.quantity(SYM) == Decimal("10")
    assert pf.closed_trades == []

    apparent_exit = order(OrderSide.SELL, "5", price=10.0)
    await broker.submit(apparent_exit)
    assert apparent_exit.status is OrderStatus.REJECTED
    assert "어떤 주문도 보내지 않습니다" in apparent_exit.reject_reason

    next_entry = order(OrderSide.BUY, "1", price=10.0, symbol=OTHER)
    pf.mark(OTHER, 10.0)
    await broker.submit(next_entry)

    assert next_entry.status is OrderStatus.REJECTED
    assert "어떤 주문도 보내지 않습니다" in next_entry.reject_reason
    assert broker.sent == []
    assert pf.cash == pytest.approx(100.0)
    assert pf.quantity(SYM) == Decimal("10")
    restored_state.close()


async def test_clean_restored_account_can_refresh_market_value_and_trade_again():
    pf = Portfolio(100.0, "KRW")
    pf.mark(SYM, 10.0)
    pf.adopt_venue_capital(cash=20.0, holdings_value=80.0)
    held = pf.position(SYM)
    held.quantity = Decimal("8")
    held.avg_price = 10.0
    broker = VenueCapitalBroker(pf)
    broker.expect_restored_venue_truth(True)
    broker.capital = {"cash": 20.0, "holdings_value": 88.0}
    broker.remote_positions = {SYM.key: Decimal("8")}
    broker.remote_costs = {SYM.key: 10.0}

    report = await broker.sync()

    assert report["ok"] and report["capital_ready"]
    assert broker.account_ready
    assert pf.cash == pytest.approx(20.0)
    assert pf.quantity(SYM) == Decimal("8")
    assert pf.holdings_value == pytest.approx(88.0)
    assert not broker._restored_venue_truth_guard


async def test_an_unmapped_holding_fails_atomically_instead_of_trading_blind():
    pf = Portfolio(800_000.0, "KRW")
    pf.mark(SYM, 100.0)
    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 120_000, "holdings_value": 300_000}
    broker.remote_positions = {"toss:UNKNOWN": Decimal("3")}
    broker.remote_costs = {"toss:UNKNOWN": 100_000.0}

    report = await broker.sync()

    assert not report["ok"]
    assert report["uncorrected"]
    assert not broker.account_ready
    assert pf.capital_source == "configured"
    assert pf.cash == pytest.approx(800_000.0)
    assert pf.positions[SYM.key].is_flat


def test_venue_baseline_survives_restart_without_restoring_stale_venue_totals(tmp_path):
    db = str(tmp_path / "state.db")
    first = StateStore(db)
    first.start_run("s", "live", 800_000.0)
    pf = Portfolio(800_000.0, "KRW")
    pf.adopt_venue_capital(cash=120_000.0, holdings_value=300_000.0)
    first.snapshot_positions(pf)
    first.mark_reconciliation_required()
    first.stop_run()
    first.close()

    second = StateStore(db)
    assert second.resume_run("s", "live")
    restored = Portfolio(800_000.0, "KRW")
    second.restore_positions(restored, {})

    assert not second.restored_reconciliation_required
    assert second.restored_venue_truth
    assert restored.capital_source == "venue"
    assert restored.performance_baseline == pytest.approx(420_000.0)
    # Stale total holdings are never considered authoritative after restart.
    assert restored.holdings_value == 0.0
    second.close()


async def test_a_legacy_run_is_rebased_from_configured_cash_on_first_truth_sync(tmp_path):
    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE run_state (run_id INTEGER PRIMARY KEY, cash REAL NOT NULL, "
        "realized_pnl REAL NOT NULL DEFAULT 0, high_water_mark REAL NOT NULL DEFAULT 0, "
        "total_fees REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    store = StateStore(db)  # migrates old run_state with source=configured
    run_id = store.start_run("s", "live", 800_000.0)
    store.conn.execute(
        "INSERT INTO run_state(run_id,cash,realized_pnl,high_water_mark,total_fees,updated_at) "
        "VALUES(?,?,?,?,?,?)",
        (run_id, 800_000.0, 0.0, 800_000.0, 0.0, "2026-08-24T00:00:00+00:00"),
    )
    store.conn.commit()
    pf = Portfolio(800_000.0, "KRW")
    store.restore_positions(pf, {})
    assert not store.restored_venue_truth
    assert pf.capital_source == "configured"

    pf.mark(SYM, 100.0)
    broker = VenueCapitalBroker(pf)
    broker.capital = {"cash": 420_000.0, "holdings_value": 0.0}
    await broker.sync()

    assert pf.capital_source == "venue"
    assert pf.equity == pytest.approx(420_000.0)
    assert pf.performance_baseline == pytest.approx(420_000.0)
    assert pf.high_water_mark == pytest.approx(420_000.0)
    assert pf.total_return == 0.0
    store.close()
