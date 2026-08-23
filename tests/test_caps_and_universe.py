"""Two limits that have to actually limit.

`max_positions` is an account-wide cap on how many names the book holds, not a
per-bar entry quota: a holding the portfolio model skipped this bar (the
rebalance deadband drops anything that needs no trade) still occupies a slot.
Counting only this bar's targets let a cap of 5 carry 24 names.

An empty universe means the filter chain admitted nothing today. It must never
be read as "no restriction" — that inversion turned a screen that selected zero
symbols into a mandate to trade the whole candidate list, live as well as in
backtest.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from quant.alpha.base import AlphaModel
from quant.brokerage.paper import PaperBrokerage
from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.engine import Engine
from quant.core.events import EventBus
from quant.core.types import UTC, Bar, Direction, Insight, PortfolioTarget, Symbol
from quant.execution.models import ImmediateExecution
from quant.portfolio.base import PortfolioConstructionModel
from quant.portfolio.models import EqualWeighting
from quant.risk.models import MaxPositionCount

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def sym(ticker: str) -> Symbol:
    return Symbol(ticker, venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))


def make_ctx(cash: float = 1_000_000.0) -> Context:
    return Context(SimClock(T0), Portfolio(cash), EventBus(), timeframe="1d")


def seed(ctx: Context, symbol: Symbol, price: float = 100.0) -> None:
    ctx.push_bar(Bar(symbol, T0 - timedelta(days=1), price, price * 1.01,
                     price * 0.99, price, 1e6, "1d"))


def hold(ctx: Context, symbol: Symbol, qty: int = 10, price: float = 100.0) -> None:
    seed(ctx, symbol, price)
    pos = ctx.portfolio.position(symbol)
    pos.quantity = Decimal(str(qty))
    pos.avg_price = price
    pos.opened_at = T0 - timedelta(days=5)
    pos.mark(price)


# ── max_positions is an account cap ──────────────────────────────────────
def test_cap_counts_holdings_that_have_no_target_this_bar():
    """The reported failure: 30 names held against a cap of 5, 5 more allowed."""
    ctx = make_ctx()
    for i in range(30):
        hold(ctx, sym(f"H{i:02d}"))
    fresh = [sym(f"N{i:02d}") for i in range(8)]
    for s in fresh:
        seed(ctx, s)

    out = MaxPositionCount(max_positions=5).manage(
        ctx, [PortfolioTarget(s, Decimal("10"), tag="alpha") for s in fresh])

    assert all(t.quantity == Decimal("0") for t in out)
    assert len(ctx.portfolio.open_positions) == 30      # book cannot grow further


def test_cap_still_admits_entries_while_the_book_is_under_it():
    ctx = make_ctx()
    for i in range(3):
        hold(ctx, sym(f"H{i:02d}"))
    fresh = [sym("N00"), sym("N01"), sym("N02")]
    for s in fresh:
        seed(ctx, s)

    out = MaxPositionCount(max_positions=5).manage(
        ctx, [PortfolioTarget(s, Decimal("10")) for s in fresh])

    allowed = [t for t in out if t.quantity != 0]
    assert len(allowed) == 2                            # 3 held + 2 = the cap


def test_a_name_being_closed_this_bar_frees_its_slot():
    """A slot released this bar is available this bar — otherwise a full book
    could never rotate, only shrink."""
    ctx = make_ctx()
    held = [sym(f"H{i:02d}") for i in range(5)]
    for s in held:
        hold(ctx, s)
    entrant = sym("N00")
    seed(ctx, entrant)

    out = MaxPositionCount(max_positions=5).manage(ctx, [
        PortfolioTarget(held[0], Decimal("0"), tag="stop"),
        PortfolioTarget(entrant, Decimal("10")),
    ])

    by_key = {t.symbol.key: t for t in out}
    assert by_key[held[0].key].quantity == Decimal("0")
    assert by_key[entrant.key].quantity == Decimal("10")


def test_cap_never_blocks_an_exit_from_an_over_cap_book():
    """An over-cap book is exactly when exits matter most."""
    ctx = make_ctx()
    held = [sym(f"H{i:02d}") for i in range(10)]
    for s in held:
        hold(ctx, s)
    entrant = sym("N00")
    seed(ctx, entrant)

    out = MaxPositionCount(max_positions=2).manage(ctx, [
        PortfolioTarget(held[0], Decimal("0"), tag="stop"),
        PortfolioTarget(held[1], Decimal("0"), tag="stop"),
        PortfolioTarget(entrant, Decimal("10")),
    ])

    by_key = {t.symbol.key: t for t in out}
    assert by_key[held[0].key].quantity == Decimal("0")
    assert by_key[held[1].key].quantity == Decimal("0")
    assert by_key[entrant.key].quantity == Decimal("0")   # no room for the entrant


class _FixedWeight(PortfolioConstructionModel):
    """Constant weight per name, so a holding that needs no rebalance falls into
    the deadband and emits no target at all — the state the cap used to miss."""

    name = "fixed_weight"

    def weights(self, ctx, insights):
        return {i.symbol.key: 0.05 for i in insights}


class _OneNewNamePerBar(AlphaModel):
    """Long a new symbol on every bar, holding every earlier view alive."""

    name = "one_per_bar"

    def __init__(self, symbols):
        self.symbols = symbols
        self.issued = 0

    async def update(self, ctx, bars):
        if self.issued >= len(self.symbols):
            return []
        target = self.symbols[self.issued]
        if target.key not in bars:
            return []
        self.issued += 1
        return [Insight(target, Direction.UP, ctx.bar_delta * 500, ctx.now,
                        confidence=1.0, source=self.name)]


def test_cap_holds_across_bars_when_holdings_go_quiet():
    """End to end: one new entry per bar against a cap of 3.

    Under the per-bar reading of the cap the book grew one name every bar,
    because the names already held emitted no target and so were not counted.
    """
    symbols = [sym(f"S{i:02d}") for i in range(12)]
    ctx = make_ctx(100_000.0)
    ctx.universe = list(symbols)
    pf = ctx.portfolio
    engine = Engine(
        ctx, _OneNewNamePerBar(symbols),
        _FixedWeight(max_position_weight=0.10, max_gross_leverage=1.0,
                     cash_reserve_pct=0.0),
        ImmediateExecution(min_order_notional=1.0), PaperBrokerage(pf),
        risk_models=[MaxPositionCount(max_positions=3)],
    )
    asyncio.run(engine.start())

    peak = 0
    for day in range(len(symbols) + 4):
        ts = T0 + timedelta(days=day)
        batch = {s.key: Bar(s, ts, 100.0, 101.0, 99.0, 100.0, 1e6, "1d") for s in symbols}
        asyncio.run(engine.on_bars(batch))
        peak = max(peak, len(pf.open_positions))

    assert peak == 3


# ── an empty screen means "nothing today", not "everything" ──────────────
class _LongEverything(AlphaModel):
    name = "long_everything"

    def __init__(self):
        self.seen: set[str] = set()

    async def update(self, ctx, bars):
        self.seen.update(bars)
        return [Insight(b.symbol, Direction.UP, ctx.bar_delta * 5, ctx.now,
                        confidence=1.0, source=self.name) for b in bars.values()]


def build_engine(ctx, alpha, symbols):
    return Engine(ctx, alpha,
                  EqualWeighting(max_position_weight=0.2, max_gross_leverage=0.95,
                                 cash_reserve_pct=0.0),
                  ImmediateExecution(min_order_notional=1.0),
                  PaperBrokerage(ctx.portfolio))


def bars_for(symbols, day: int = 0) -> dict[str, Bar]:
    ts = T0 + timedelta(days=day)
    return {s.key: Bar(s, ts, 100.0, 101.0, 99.0, 100.0, 1e6, "1d") for s in symbols}


def test_empty_screen_blocks_new_entries_instead_of_opening_the_gate():
    symbols = [sym(f"S{i:02d}") for i in range(10)]
    ctx = make_ctx()
    ctx.universe = list(symbols)
    alpha = _LongEverything()
    engine = build_engine(ctx, alpha, symbols)
    asyncio.run(engine.start())

    engine.set_universe([])                              # the chain admitted nothing
    for day in range(2):
        asyncio.run(engine.on_bars(bars_for(symbols, day)))

    assert alpha.seen == set()
    assert engine.orders == []
    assert ctx.portfolio.open_positions == []


def test_a_strategy_without_universe_selection_is_unrestricted():
    """The behaviour the old `if not universe` shortcut existed to protect."""
    symbols = [sym(f"S{i:02d}") for i in range(4)]
    ctx = make_ctx()                                     # universe never configured
    alpha = _LongEverything()
    engine = build_engine(ctx, alpha, symbols)
    asyncio.run(engine.start())

    asyncio.run(engine.on_bars(bars_for(symbols)))

    assert alpha.seen == {s.key for s in symbols}
    assert len(engine.orders) == len(symbols)


def test_universe_still_narrows_the_batch_when_it_is_not_empty():
    symbols = [sym(f"S{i:02d}") for i in range(4)]
    ctx = make_ctx()
    ctx.universe = list(symbols)
    alpha = _LongEverything()
    engine = build_engine(ctx, alpha, symbols)
    asyncio.run(engine.start())

    engine.set_universe(symbols[:2])
    asyncio.run(engine.on_bars(bars_for(symbols)))

    assert alpha.seen == {s.key for s in symbols[:2]}


def test_an_emptied_screen_is_logged(caplog):
    ctx = make_ctx()
    ctx.universe = [sym("S00"), sym("S01")]
    engine = build_engine(ctx, _LongEverything(), ctx.universe)

    with caplog.at_level(logging.WARNING, logger="quant.engine"):
        engine.set_universe([])

    assert any("0종목" in r.getMessage() for r in caplog.records)


def test_positions_can_still_exit_while_the_screen_is_empty():
    """No new entries must not mean no exits — a screen hiccup that trapped the
    book would be worse than the original bug."""
    held = sym("S00")
    ctx = make_ctx(10_000.0)
    ctx.universe = [held]
    hold(ctx, held, qty=10)
    engine = build_engine(ctx, _LongEverything(), [held])
    asyncio.run(engine.start())

    engine.set_universe([])
    asyncio.run(engine.on_bars(bars_for([held])))

    assert [o.side.value for o in engine.orders] == ["sell"]
    assert engine.orders[0].quantity == Decimal("10")
