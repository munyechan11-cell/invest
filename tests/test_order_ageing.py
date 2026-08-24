"""미체결 주문의 나이 — the half of the escalation that was missing.

`LimitExecution.urgent_after_bars` decided the *signal* had gone stale and said
nothing about the order still sitting at the venue. That is not half a feature,
it is an incoherent one: the engine diffs targets against `projected_quantity`,
so a resting order suppresses every future target on its symbol, the escalation
never gets a delta to escalate, and the position silently stops being managed.
On KRX it is worse than silent — 지정가 die at 15:30 and no adapter says so, so
the dead order keeps suppressing that symbol until the process restarts, and a
resting sell that died unfilled disarms an exit on a market where the owner
cannot short to hedge.

Every test here is about the same invariant, from a different side: **one
intended position must never become two.** A cancel and a fill can race, and
the only safe thing to do while that race is unsettled is nothing at all.
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
    Fill,
    Insight,
    Order,
    OrderStatus,
    OrderType,
    PortfolioTarget,
    Symbol,
)
from quant.execution.base import AgeAction, OrderAgePolicy
from quant.execution.models import (
    ImmediateExecution,
    LimitExecution,
    StandardDeviationExecution,
    TwapExecution,
    VolumeParticipationExecution,
)
from quant.portfolio.models import EqualWeighting

SYM = Symbol("AAA", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))
KRW = Symbol("005930", venue="kis", quote_currency="KRW",
             tick_size=Decimal("100"), lot_size=Decimal("1"))
#: 2024-06-03 09:30 KST — 정규장 한복판, 종가 단일가 전.
T0 = datetime(2024, 6, 3, 0, 30, tzinfo=UTC)


class Book:
    """The brokerage half of the loop, reduced to what order ageing needs.

    Accept, fill, cancel, and republish the resting quantity into the context:
    that is exactly the cycle `Engine._refresh_pending` runs. Like the engine it
    hands back the very `Order` objects the execution model emitted, which is
    what lets the model see a fill it never asked about.
    """

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.resting: dict[str, Order] = {}
        self.cancelled: list[Order] = []

    def submit(self, orders: list[Order]) -> list[Order]:
        for order in orders:
            order.status = OrderStatus.SUBMITTED
            order.broker_id = f"venue-{order.id}"
            self.resting[order.id] = order
        self.refresh()
        return orders

    def fill(self, order: Order, quantity=None, price: float = 100.0) -> None:
        qty = order.remaining if quantity is None else Decimal(str(quantity))
        fill = Fill(order.id, order.symbol, order.side, qty, price, 0.0, self.ctx.now,
                    tag=order.tag)
        order.apply_fill(fill)
        self.ctx.portfolio.apply_fill(fill)
        if order.status is OrderStatus.FILLED:
            self.resting.pop(order.id, None)
        self.refresh()

    def cancel(self, order: Order) -> bool:
        if self.resting.pop(order.id, None) is None:
            return False        # already gone — a fill beat us to it
        order.status = OrderStatus.CANCELED
        self.cancelled.append(order)
        self.refresh()
        return True

    def drain(self, model) -> None:
        """What a live loop does at the top of its cycle, before refreshing."""
        for order in model.pending_cancellations:
            self.cancel(order)

    def refresh(self) -> None:
        pending: dict[str, Decimal] = {}
        for order in self.resting.values():
            key = order.symbol.key
            pending[key] = pending.get(key, Decimal("0")) + order.remaining * order.side.sign
        self.ctx.set_pending(pending)


def make_ctx(symbol: Symbol = SYM, price: float = 100.0, cash: float = 1_000_000.0,
             start: datetime = T0, timeframe: str = "1d") -> Context:
    pf = Portfolio(cash)
    ctx = Context(SimClock(start), pf, EventBus(), timeframe=timeframe)
    ctx.universe = [symbol]
    for i in range(30):
        ts = start - timedelta(days=30 - i)
        ctx.push_bar(Bar(symbol, ts, price, price * 1.01, price * 0.99, price, 1e6, "1d"))
    return ctx


def setup(symbol: Symbol = SYM, **kwargs) -> tuple[Context, Book]:
    ctx = make_ctx(symbol, **kwargs)
    return ctx, Book(ctx)


def advance(ctx: Context, days: int = 1, minutes: int = 0) -> None:
    ctx.clock.set(ctx.now + timedelta(days=days, minutes=minutes))


def hold(ctx: Context, symbol: Symbol, quantity: str, avg: float = 100.0) -> None:
    pos = ctx.portfolio.position(symbol)
    pos.quantity = Decimal(quantity)
    pos.avg_price = avg
    pos.opened_at = ctx.now
    pos.mark(avg)


class _AlwaysLong(AlphaModel):
    name = "always_long"

    async def update(self, ctx, bars):
        return [Insight(b.symbol, Direction.UP, ctx.bar_delta * 50, ctx.now,
                        confidence=1.0, source=self.name) for b in bars.values()]


def patient(**overrides) -> OrderAgePolicy:
    """A policy with the knobs the test is not about turned off."""
    base = {"entry_bars": 3, "exit_bars": 2, "exit_timeout_count": 9, "max_reprices": 9}
    return OrderAgePolicy(**{**base, **overrides})


# ── the age itself ───────────────────────────────────────────────────────
def test_an_unfilled_entry_is_asked_back_only_after_its_own_timeout():
    ctx, book = setup()
    model = ImmediateExecution(min_order_notional=1, order_age=patient(entry_bars=3))
    target = PortfolioTarget(SYM, Decimal("100"))
    sent = book.submit(model.execute(ctx, [target]))
    assert len(sent) == 1

    for _ in range(2):
        advance(ctx)
        assert model.execute(ctx, [target]) == [], "a resting order was re-sent"
        assert model.pending_cancellations == [], "asked for the order back too early"

    advance(ctx)
    model.execute(ctx, [target])
    assert [o.id for o in model.pending_cancellations] == [sent[0].id]


def test_a_fill_restarts_the_patience_rather_than_ageing_through_it():
    """부분체결은 거래소가 우리와 거래하고 있다는 뜻입니다 — 멈춘 주문이 아닙니다."""
    ctx, book = setup()
    model = ImmediateExecution(min_order_notional=1, order_age=patient(entry_bars=3))
    target = PortfolioTarget(SYM, Decimal("100"))
    order = book.submit(model.execute(ctx, [target]))[0]

    for _ in range(2):
        advance(ctx)
        model.execute(ctx, [target])
    book.fill(order, 40)                       # 40/100 filled on the third bar
    for _ in range(2):
        advance(ctx)
        model.execute(ctx, [target])
        assert model.pending_cancellations == []

    advance(ctx)
    model.execute(ctx, [target])
    assert [o.id for o in model.pending_cancellations] == [order.id]


def test_entries_and_exits_get_different_patience():
    ctx, book = setup()
    hold(ctx, SYM, "100")
    model = ImmediateExecution(min_order_notional=1,
                               order_age=patient(entry_bars=5, exit_bars=1))
    exiting = book.submit(model.execute(ctx, [PortfolioTarget(SYM, Decimal("0"))]))[0]
    assert exiting.side.value == "sell"

    advance(ctx)
    model.execute(ctx, [PortfolioTarget(SYM, Decimal("0"))])
    assert [o.id for o in model.pending_cancellations] == [exiting.id], (
        "an exit waited as long as an entry")


def test_an_order_that_never_reached_a_venue_is_not_tracked():
    """Only an order on a book can be a zombie — a rejected one is just gone."""
    ctx, book = setup()
    model = ImmediateExecution(min_order_notional=1, order_age=patient(entry_bars=1))
    target = PortfolioTarget(SYM, Decimal("100"))
    rejected = model.execute(ctx, [target])[0]
    rejected.status = OrderStatus.REJECTED

    advance(ctx)
    assert model.execute(ctx, [target]) != [], "a rejected order still suppressed the target"
    assert model.pending_cancellations == []


def test_a_disabled_policy_never_asks_for_anything_back():
    ctx, book = setup()
    model = ImmediateExecution(min_order_notional=1, order_age=False)
    target = PortfolioTarget(SYM, Decimal("100"))
    book.submit(model.execute(ctx, [target]))
    for _ in range(20):
        advance(ctx)
        assert model.execute(ctx, [target]) == []
    assert model.pending_cancellations == []


# ── the race: a cancel and a fill can cross ──────────────────────────────
def test_nothing_new_goes_out_while_our_own_cancel_is_in_flight():
    """The whole guard. Sizing the replacement now assumes the cancel won."""
    ctx, book = setup()
    model = ImmediateExecution(min_order_notional=1, order_age=patient(entry_bars=1))
    target = PortfolioTarget(SYM, Decimal("100"))
    book.submit(model.execute(ctx, [target]))

    advance(ctx)
    assert model.execute(ctx, [target]) == []
    assert model.pending_cancellations, "never asked for the stale order back"

    # The caller has not acted yet — several bars of it, and still nothing.
    for _ in range(3):
        advance(ctx)
        assert model.execute(ctx, [target]) == []
    assert ctx.projected_quantity(SYM) == Decimal("100")


def test_the_replacement_goes_out_once_the_cancel_has_actually_settled():
    ctx, book = setup()
    model = ImmediateExecution(min_order_notional=1, order_age=patient(entry_bars=1))
    target = PortfolioTarget(SYM, Decimal("100"))
    book.submit(model.execute(ctx, [target]))

    advance(ctx)
    model.execute(ctx, [target])
    book.drain(model)                          # the live loop cancels
    assert ctx.projected_quantity(SYM) == Decimal("0")

    advance(ctx)
    replacement = book.submit(model.execute(ctx, [target]))
    assert len(replacement) == 1
    assert replacement[0].quantity == Decimal("100")
    assert ctx.projected_quantity(SYM) == Decimal("100")


def test_a_cancel_that_races_a_fill_does_not_double_the_position():
    ctx, book = setup()
    model = ImmediateExecution(min_order_notional=1, order_age=patient(entry_bars=1))
    target = PortfolioTarget(SYM, Decimal("100"))
    order = book.submit(model.execute(ctx, [target]))[0]

    advance(ctx)
    model.execute(ctx, [target])
    assert model.pending_cancellations == [order]
    book.fill(order)                           # the venue filled it first
    assert book.cancel(order) is False         # the cancel arrives too late

    advance(ctx)
    assert model.execute(ctx, [target]) == [], "re-sent an order that had already filled"
    assert ctx.portfolio.quantity(SYM) == Decimal("100")
    assert ctx.projected_quantity(SYM) == Decimal("100")
    assert model.pending_cancellations == []


def test_a_partial_fill_needs_no_resizing_because_the_target_is_absolute():
    """freqtrade resizes the trade on `handle_cancel_enter`; here the invariant
    does it — the next diff sees the partial as position and asks for the rest."""
    ctx, book = setup()
    model = ImmediateExecution(min_order_notional=1, order_age=patient(entry_bars=1))
    target = PortfolioTarget(SYM, Decimal("100"))
    order = book.submit(model.execute(ctx, [target]))[0]
    book.fill(order, 30)

    advance(ctx)
    advance(ctx)
    model.execute(ctx, [target])
    book.drain(model)

    advance(ctx)
    top_up = book.submit(model.execute(ctx, [target]))
    assert len(top_up) == 1 and top_up[0].quantity == Decimal("70")
    assert ctx.projected_quantity(SYM) == Decimal("100")


def test_the_projection_never_exceeds_the_target_through_a_long_stall():
    """The regression this whole feature exists for, run to exhaustion."""
    ctx, book = setup()
    model = ImmediateExecution(min_order_notional=1,
                               order_age=OrderAgePolicy(entry_bars=2, max_reprices=99))
    target = PortfolioTarget(SYM, Decimal("100"))
    for _ in range(40):
        book.submit(model.execute(ctx, [target]))
        book.drain(model)
        assert ctx.projected_quantity(SYM) <= Decimal("100")
        assert sum(o.remaining for o in book.resting.values()) <= Decimal("100")
        advance(ctx)


# ── escalation: an exit that will not fill ───────────────────────────────
def test_a_second_exit_timeout_escalates_to_a_market_order():
    ctx, book = setup()
    hold(ctx, SYM, "100")
    model = LimitExecution(offset_bps=20, urgent_after_bars=99, min_order_notional=1,
                           order_age=OrderAgePolicy(exit_bars=1, exit_timeout_count=2,
                                                    entry_bars=9))
    flat = PortfolioTarget(SYM, Decimal("0"), tag="stop")

    first = book.submit(model.execute(ctx, [flat]))[0]
    assert first.type is OrderType.LIMIT
    advance(ctx)
    model.execute(ctx, [flat])                 # 1회 미체결 → 재주문
    book.drain(model)
    advance(ctx)
    second = book.submit(model.execute(ctx, [flat]))[0]
    assert second.type is OrderType.LIMIT

    advance(ctx)
    reviews = model.review_orders(ctx)
    assert [r.action for r in reviews] == [AgeAction.ESCALATE]
    model.execute(ctx, [flat])
    book.drain(model)

    advance(ctx)
    crossing = book.submit(model.execute(ctx, [flat]))
    assert len(crossing) == 1
    assert crossing[0].type is OrderType.MARKET, "the exit expired instead of escalating"
    assert crossing[0].quantity == Decimal("100")


def test_an_escalation_changes_how_we_trade_and_never_how_much():
    ctx, book = setup()
    hold(ctx, SYM, "100")
    model = TwapExecution(slices=4, min_order_notional=1,
                          order_age=OrderAgePolicy(exit_bars=1, exit_timeout_count=1))
    flat = PortfolioTarget(SYM, Decimal("0"), tag="stop")
    book.submit(model.execute(ctx, [flat]))

    advance(ctx)
    model.execute(ctx, [flat])
    book.drain(model)
    advance(ctx)
    escalated = model.execute(ctx, [flat])[0]
    assert escalated.type is OrderType.MARKET
    # the twap schedule still owns the size; only the patience was revoked
    assert escalated.quantity < Decimal("100")


def test_an_escalation_can_cross_with_a_limit_where_market_orders_are_refused():
    """KIS 해외주식은 시장가 자체가 없습니다 — 뚫는 지정가가 같은 뜻입니다."""
    ctx, book = setup()
    hold(ctx, SYM, "100")
    model = ImmediateExecution(
        min_order_notional=1,
        order_age=OrderAgePolicy(exit_bars=1, exit_timeout_count=1,
                                 escalate_cross_bps=30.0),
    )
    flat = PortfolioTarget(SYM, Decimal("0"), tag="stop")
    book.submit(model.execute(ctx, [flat]))

    advance(ctx)
    model.execute(ctx, [flat])
    book.drain(model)
    advance(ctx)
    crossing = model.execute(ctx, [flat])[0]
    assert crossing.type is OrderType.LIMIT
    assert crossing.limit_price is not None and crossing.limit_price < 100.0


def test_an_escalation_is_dropped_when_the_fill_wins_the_race():
    ctx, book = setup()
    hold(ctx, SYM, "100")
    model = ImmediateExecution(min_order_notional=1,
                               order_age=OrderAgePolicy(exit_bars=1, exit_timeout_count=1))
    flat = PortfolioTarget(SYM, Decimal("0"), tag="stop")
    order = book.submit(model.execute(ctx, [flat]))[0]

    advance(ctx)
    model.execute(ctx, [flat])
    book.fill(order)                           # the exit filled after all
    advance(ctx)
    assert model.execute(ctx, [flat]) == []
    assert ctx.portfolio.quantity(SYM) == Decimal("0")


# ── the chase cap ────────────────────────────────────────────────────────
def test_the_entry_chase_is_capped():
    """얇은 종목에서 재주문 한 번은 스프레드 한 번 — 거래세와 맞먹습니다."""
    def chase(max_reprices: int) -> tuple[int, int]:
        ctx, book = setup()
        model = ImmediateExecution(
            min_order_notional=1,
            order_age=OrderAgePolicy(entry_bars=2, max_reprices=max_reprices),
        )
        target = PortfolioTarget(SYM, Decimal("100"))
        sent = idle = 0
        for _ in range(24):
            new = book.submit(model.execute(ctx, [target]))
            sent += len(new)
            # neither trading nor waiting on the venue — the cap is biting
            idle += not new and not book.resting
            book.drain(model)                  # the venue takes every cancel
            advance(ctx)
        return sent, idle

    capped, uncapped = chase(2), chase(99)
    assert capped[0] < uncapped[0], "the retry cap did not slow the chase down"
    assert uncapped[1] == 0, "the uncapped model was supposed to chase every bar"
    assert capped[1] > 0, "the capped model never actually stood down"


def test_the_chase_cap_never_stands_an_exit_down():
    ctx, book = setup()
    hold(ctx, SYM, "100")
    model = ImmediateExecution(min_order_notional=1,
                               order_age=OrderAgePolicy(entry_bars=1, exit_bars=1,
                                                        exit_timeout_count=99,
                                                        max_reprices=0))
    flat = PortfolioTarget(SYM, Decimal("0"), tag="stop")
    for _ in range(6):
        assert book.submit(model.execute(ctx, [flat])), "an exit was withheld"
        advance(ctx)
        model.execute(ctx, [flat])
        book.drain(model)
        advance(ctx)


def test_a_stood_down_entry_gets_another_chance_after_the_cooldown():
    ctx, book = setup()
    model = ImmediateExecution(min_order_notional=1,
                               order_age=OrderAgePolicy(entry_bars=2, max_reprices=0))
    target = PortfolioTarget(SYM, Decimal("100"))
    book.submit(model.execute(ctx, [target]))
    for _ in range(2):
        advance(ctx)
        model.execute(ctx, [target])
    book.drain(model)

    advance(ctx)
    assert model.execute(ctx, [target]) == [], "ignored its own cooldown"
    for _ in range(3):
        advance(ctx)
        again = model.execute(ctx, [target])
        if again:
            break
    assert again, "the entry was suppressed permanently"


# ── KRX ──────────────────────────────────────────────────────────────────
def test_a_krx_order_is_reconciled_once_the_session_that_holds_it_has_closed():
    """KRX 에는 GTC 가 없습니다 — 15:30 이후에 남아 있는 주문은 우리 장부에만
    있습니다. 그대로 두면 그 종목의 목표가 영원히 억눌립니다."""
    ctx, book = setup(KRW, price=70_000.0, cash=100_000_000.0, timeframe="5m")
    model = ImmediateExecution(min_order_notional=1, order_age=patient(entry_bars=99))
    target = PortfolioTarget(KRW, Decimal("10"))
    order = book.submit(model.execute(ctx, [target]))[0]

    advance(ctx, days=0, minutes=60)           # 10:30 KST, still open
    model.execute(ctx, [target])
    assert model.pending_cancellations == []

    advance(ctx, days=0, minutes=360)          # 16:30 KST — the session is over
    model.execute(ctx, [target])
    assert [o.id for o in model.pending_cancellations] == [order.id]
    assert "장 종료" in model.review_orders(ctx)[0].reason


def test_a_non_korean_order_is_not_killed_by_the_krx_close():
    ctx, book = setup(timeframe="5m")
    model = ImmediateExecution(min_order_notional=1, order_age=patient(entry_bars=99))
    target = PortfolioTarget(SYM, Decimal("100"))
    book.submit(model.execute(ctx, [target]))
    advance(ctx, days=0, minutes=600)
    model.execute(ctx, [target])
    assert model.pending_cancellations == []


def test_the_closing_auction_takes_a_cancel_but_no_replacement():
    """15:20–15:30 종가 단일가에는 정정이 안 됩니다. 취소 후 재주문은 정정과
    같은 것이므로 단일가가 끝날 때까지 미룹니다."""
    auction = datetime(2024, 6, 3, 6, 22, tzinfo=UTC)          # 15:22 KST
    ctx, book = setup(KRW, price=70_000.0, cash=100_000_000.0, timeframe="5m",
                      start=auction - timedelta(minutes=90))
    model = ImmediateExecution(min_order_notional=1,
                               order_age=OrderAgePolicy(entry_bars=1, max_reprices=9))
    target = PortfolioTarget(KRW, Decimal("10"))
    book.submit(model.execute(ctx, [target]))

    ctx.clock.set(auction)
    model.execute(ctx, [target])
    assert model.pending_cancellations, "the cancel stood down too — it should not"
    assert "단일가" in model.review_orders(ctx)[0].reason
    book.drain(model)

    ctx.clock.set(auction + timedelta(minutes=2))
    assert model.execute(ctx, [target]) == [], "재주문이 단일가 시간에 나갔습니다"

    ctx.clock.set(auction + timedelta(hours=18))               # 09:22 KST, next session
    assert model.execute(ctx, [target]), "재주문이 영원히 막혔습니다"


# ── LimitExecution: the two halves land together ─────────────────────────
def test_urgent_after_bars_is_no_longer_inert():
    """The original defect: the resting limit is exactly what stops the diff
    from asking for anything, so the urgency counter reset every bar and the
    escalation never fired."""
    ctx, book = setup()
    model = LimitExecution(offset_bps=20, urgent_after_bars=2, min_order_notional=1)
    target = PortfolioTarget(SYM, Decimal("100"))
    posted = book.submit(model.execute(ctx, [target]))[0]
    assert posted.type is OrderType.LIMIT

    crossed = None
    for _ in range(6):
        advance(ctx)
        book.submit(model.execute(ctx, [target]))
        book.drain(model)
        for order in book.resting.values():
            if order.type is OrderType.MARKET:
                crossed = order
        if crossed:
            break
    assert crossed is not None, "urgent_after_bars never converted to a market order"
    assert crossed.quantity == Decimal("100")
    assert ctx.projected_quantity(SYM) == Decimal("100")


def test_limit_execution_derives_its_timeout_from_the_urgency_it_was_given():
    assert LimitExecution(urgent_after_bars=7).order_age.entry_bars == 7
    assert LimitExecution(urgent_after_bars=7).order_age.exit_bars == 7
    explicit = LimitExecution(urgent_after_bars=7, order_age={"entry_bars": 2})
    assert explicit.order_age.entry_bars == 2

    # "never post, always cross" is a legal thing to ask a limit model for
    ctx, _ = setup()
    always = LimitExecution(urgent_after_bars=0, min_order_notional=1)
    assert always.execute(ctx, [PortfolioTarget(SYM, Decimal("100"))])[0].type \
        is OrderType.MARKET


# ── shared plumbing ──────────────────────────────────────────────────────
@pytest.mark.parametrize("model", [
    ImmediateExecution(min_order_notional=1),
    LimitExecution(min_order_notional=1),
    TwapExecution(slices=2, min_order_notional=1),
    VolumeParticipationExecution(participation=1.0, min_order_notional=1),
    StandardDeviationExecution(period=2, deviations=0.0, min_order_notional=1),
])
def test_every_model_ages_its_orders_exactly_once_per_bar(model):
    """Ageing hangs off `_deltas`, so a model that diffs twice would age twice."""
    ctx, book = setup()
    target = PortfolioTarget(SYM, Decimal("100"))
    book.submit(model.execute(ctx, [target]))
    resting = next(iter(model._resting.values()))

    advance(ctx)
    model.execute(ctx, [target])
    model.execute(ctx, [target])
    assert resting.age == 1, f"{model.name} aged its own order {resting.age} times in one bar"


def test_pending_cancellations_is_a_view_and_survives_a_caller_that_drops_it():
    ctx, book = setup()
    model = ImmediateExecution(min_order_notional=1, order_age=patient(entry_bars=1))
    target = PortfolioTarget(SYM, Decimal("100"))
    order = book.submit(model.execute(ctx, [target]))[0]

    advance(ctx)
    model.execute(ctx, [target])
    assert model.pending_cancellations == [order]
    assert model.pending_cancellations == [order], "reading it once consumed it"

    advance(ctx)
    model.execute(ctx, [target])
    assert model.pending_cancellations == [order], "a refused cancel was forgotten"

    book.cancel(order)
    advance(ctx)
    model.execute(ctx, [target])
    assert model.pending_cancellations == []


def test_review_orders_can_be_driven_by_the_caller_instead():
    """A live loop wants this at the top of its cycle, before it refreshes the
    projection. Calling it there and calling `execute` after must age once."""
    ctx, book = setup()
    model = ImmediateExecution(min_order_notional=1, order_age=patient(entry_bars=1))
    target = PortfolioTarget(SYM, Decimal("100"))
    book.submit(model.execute(ctx, [target]))

    advance(ctx)
    reviews = model.review_orders(ctx)
    assert [r.action for r in reviews] == [AgeAction.REPRICE]
    assert reviews[0].age_bars == 1
    assert model.review_orders(ctx) == reviews
    model.execute(ctx, [target])
    assert next(iter(model._resting.values())).age == 1


def test_a_paper_backtest_never_produces_a_zombie():
    """The simulated venue kills its own orders, which is exactly why this
    defect could never show up in a backtest — and why the policy must stay
    quiet there instead of inventing cancellations nobody asked for."""
    pf = Portfolio(1_000_000.0)
    ctx = Context(SimClock(T0), pf, EventBus(), timeframe="1d")
    ctx.universe = [KRW]
    broker = PaperBrokerage(pf)
    engine = Engine(ctx, _AlwaysLong(),
                    EqualWeighting(cash_reserve_pct=0.0, max_position_weight=1.0),
                    LimitExecution(offset_bps=10, min_order_notional=1), broker)
    asyncio.run(engine.start())

    price = 70_000.0
    for i in range(40):
        bar = Bar(KRW, T0 + timedelta(days=i), price, price * 1.01, price * 0.99,
                  price, 1e6, "1d")
        asyncio.run(engine.on_bars({KRW.key: bar}))
        price *= 1.001
        assert engine.execution_model.pending_cancellations == [], (
            f"the policy asked for a cancel on bar {i} of a paper backtest")
    assert engine.orders, "the backtest never traded at all"


def test_the_policy_refuses_settings_that_cannot_mean_anything():
    with pytest.raises(ValueError):
        OrderAgePolicy(entry_bars=0)
    with pytest.raises(ValueError):
        OrderAgePolicy(exit_bars=-1)
    with pytest.raises(ValueError):
        OrderAgePolicy(exit_timeout_count=0)
    with pytest.raises(ValueError):
        OrderAgePolicy(max_reprices=-1)
    with pytest.raises(TypeError):
        OrderAgePolicy.coerce("4 bars")


def test_the_policy_reads_the_shapes_a_yaml_file_can_produce():
    assert OrderAgePolicy.coerce(None).enabled is True
    assert OrderAgePolicy.coerce(False).enabled is False
    assert OrderAgePolicy.coerce({"entry_bars": 6, "exit_bars": 1}).entry_bars == 6
    policy = OrderAgePolicy(entry_bars=6)
    assert OrderAgePolicy.coerce(policy) is policy
