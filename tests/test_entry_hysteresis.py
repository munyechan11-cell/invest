"""It must cost more conviction to open a position than to keep one.

Novy-Marx & Velikov (RFS 29(1) 2016) tested the simple cost-mitigation tricks
against each other and found the buy/hold spread the most effective of them;
Chen & Velikov's 120-anomaly replication puts the gain at roughly 7-15bp a
month. On KRX it is worth more still, because every sell pays 거래세 with no
offset — a signal that flickers across one threshold is charged for the same
opinion again and again.

The other half of the file is the half that matters for real money: a band that
holds a position is one bar away from being a widened stop-loss. Nothing here
may keep a position that the risk layer, a stop, or a FLAT regime veto wants
gone, and the tests below say so in every way the engine can express it.
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.alpha.base import AlphaModel
from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, Bar, Direction, Fill, Insight, OrderSide, Symbol
from quant.portfolio.base import PortfolioConstructionModel
from quant.portfolio.models import BUILTIN_PORTFOLIO_MODELS, EqualWeighting
from quant.risk.base import CompositeRiskModel
from quant.risk.models import (
    MaximumDrawdownPerSecurity,
    TimeStopRiskModel,
    TrailingStopRiskModel,
)

SYM = Symbol("005930", venue="KRX", quote_currency="KRW",
             tick_size=Decimal("1"), lot_size=Decimal("1"))
T0 = datetime(2026, 3, 2, tzinfo=UTC)
PRICE = 100.0
PERIOD = timedelta(days=5)
#: a full-budget position for a 1,000,000원 book at `PRICE`
FULL = 10_000

#: 2026 매도 거래세 (증권거래세 + 농어촌특별세), KOSPI/KOSDAQ 동일.
SELL_TAX_BPS = 20.0


def make_ctx(price: float = PRICE, cash: float = 1_000_000.0) -> Context:
    """A 150-bar KRX book. The series wiggles so covariance-based models have
    something to invert, and closes exactly on `price` so fills settle flat."""
    ctx = Context(SimClock(T0), Portfolio(cash, "KRW"), EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    for i in range(150):
        ts = T0 - timedelta(days=150 - i)
        close = price if i == 149 else price * (1 + 0.02 * math.sin(i))
        ctx.push_bar(Bar(SYM, ts, close, close * 1.01, close * 0.99, close, 1e6, "1d"))
    return ctx


def hold(ctx: Context, quantity: float, price: float = PRICE) -> Context:
    """Put a real position on the book, cash and all, marked as the engine marks it."""
    side = OrderSide.BUY if quantity > 0 else OrderSide.SELL
    ctx.portfolio.apply_fill(Fill(order_id="seed", symbol=SYM, side=side,
                                  quantity=Decimal(str(abs(quantity))), price=price,
                                  fee=0.0, ts=T0 - timedelta(days=1)))
    ctx.portfolio.position(SYM).mark(ctx.price(SYM))
    return ctx


def insight(confidence: float, direction: Direction = Direction.UP, *,
            source: str = "a", age: timedelta = timedelta(0),
            magnitude: float | None = None, period: timedelta = PERIOD) -> Insight:
    return Insight(symbol=SYM, direction=direction, period=period,
                   generated_at=T0 - age, magnitude=magnitude,
                   confidence=confidence, source=source)


def target_qty(pm: PortfolioConstructionModel, ctx: Context,
               insights: list[Insight]) -> Decimal:
    """What the portfolio layer wants to hold, in shares."""
    targets = pm.create_targets(ctx, insights)
    for t in targets:
        if t.symbol.key == SYM.key:
            return t.quantity
    # No target at all means "leave it alone" — the deadband's way of holding.
    return ctx.portfolio.quantity(SYM)


def model(**kwargs) -> EqualWeighting:
    kwargs.setdefault("cash_reserve_pct", 0.0)
    kwargs.setdefault("max_position_weight", 1.0)
    kwargs.setdefault("min_trade_weight", 0.0)
    return EqualWeighting(**kwargs)


# ── the band itself ──────────────────────────────────────────────────────
def test_a_signal_between_the_two_bars_opens_nothing():
    ctx = make_ctx()
    # 0.25 clears the hold bar (0.15) but not the entry bar (0.40)
    assert target_qty(model(), ctx, [insight(0.25)]) == 0


def test_the_very_same_signal_keeps_a_position_already_open():
    """The whole point: identical evidence, opposite answer, because of the tax."""
    ctx = hold(make_ctx(), FULL)
    # not merely non-zero — the target equals the position, so nothing trades
    assert target_qty(model(), ctx, [insight(0.25)]) == Decimal(FULL)


def test_below_the_hold_bar_the_position_goes_flat():
    ctx = hold(make_ctx(), FULL)
    assert target_qty(model(), ctx, [insight(0.10)]) == 0


def test_above_the_entry_bar_a_flat_book_opens():
    ctx = make_ctx()
    assert target_qty(model(), ctx, [insight(0.55)]) > 0


def test_the_hold_bar_may_not_sit_above_the_entry_bar():
    """A band that is easier to enter than to keep is not a band."""
    with pytest.raises(ValueError, match="hold_conviction"):
        EqualWeighting(entry_conviction=0.2, hold_conviction=0.5)
    with pytest.raises(ValueError):
        EqualWeighting(entry_conviction=1.5, hold_conviction=0.1)


def test_the_band_can_be_switched_off_entirely():
    off = model(entry_conviction=0.0, hold_conviction=0.0)
    ctx = make_ctx()
    assert target_qty(off, ctx, [insight(0.01)]) > 0


# ── conviction, not the direction vote ───────────────────────────────────
def test_the_raw_consensus_is_untouched_by_the_band():
    """The score every model sizes off must still be the old formula exactly."""
    off = model(entry_conviction=0.0, hold_conviction=0.0)
    items = [insight(0.8, magnitude=0.1, source="a"),
             insight(0.3, Direction.DOWN, source="b")]
    score = off._net_scores(items, make_ctx())[SYM.key][1]
    assert score == pytest.approx((0.8 * 1.1 - 0.3) / (0.8 * 1.1 + 0.3))


def test_a_unanimous_book_scores_one_however_faint_its_evidence():
    """Why the band is measured against conviction and not against the score.

    Banding the score would be a no-op on this engine: agreeing models score ±1
    whether they are 5% sure or 95% sure.
    """
    off = model(entry_conviction=0.0, hold_conviction=0.0)
    timid = off._net_scores([insight(0.05, source="a"), insight(0.05, source="b")],
                            make_ctx())[SYM.key][1]
    assert timid == pytest.approx(1.0)
    # ...and the band still refuses to open on it.
    assert target_qty(model(), make_ctx(), [insight(0.05, source="a"),
                                            insight(0.05, source="b")]) == 0


def test_magnitude_reweights_the_vote_but_cannot_buy_conviction():
    """Otherwise a band in confidence units would mean something different for
    every expected move, and the thresholds would be uninterpretable."""
    ctx = make_ctx()
    assert target_qty(model(), ctx, [insight(0.25, magnitude=5.0)]) == 0
    assert target_qty(model(), ctx, [insight(0.55, magnitude=5.0)]) > 0


def test_two_models_at_a_near_tie_never_clear_the_entry_bar():
    ctx = make_ctx()
    contested = [insight(0.55, Direction.UP, source="a"),
                 insight(0.45, Direction.DOWN, source="b")]
    assert target_qty(model(), ctx, contested) == 0


# ── the money: round trips avoided ───────────────────────────────────────
def _simulate(pm: PortfolioConstructionModel, convictions: list[tuple[float, float]]
              ) -> int:
    """Run a two-model near-tie through the portfolio layer; count taxed sells.

    Each bar the two alphas re-fire with slightly different confidence, so the
    consensus flips sign on a hair. Fills are settled at a flat price, which is
    the honest way to price churn: no P&L, only the 거래세.
    """
    ctx = make_ctx()
    sells = 0
    for n, (up, down) in enumerate(convictions):
        items = [insight(up, Direction.UP, source="a"),
                 insight(down, Direction.DOWN, source="b")]
        want = target_qty(pm, ctx, items)
        delta = want - ctx.portfolio.quantity(SYM)
        if delta == 0:
            continue
        side = OrderSide.BUY if delta > 0 else OrderSide.SELL
        if side is OrderSide.SELL:
            sells += 1
        ctx.portfolio.apply_fill(Fill(order_id=f"o{n}", symbol=SYM, side=side,
                                      quantity=abs(delta), price=PRICE, fee=0.0,
                                      ts=T0 + timedelta(days=n)))
        ctx.portfolio.position(SYM).mark(PRICE)
    return sells


def test_the_band_pays_for_itself_on_an_oscillating_signal():
    """The reason this exists.

    Two alphas one hair apart decide a full-size position by the sign of a
    rounding error. Unbanded, the book is bought and sold every other bar and
    each sell hands 20bp to the 국세청 for an opinion that never changed.
    """
    flicker = [(0.55, 0.45), (0.45, 0.55)] * 4
    unbanded = _simulate(model(entry_conviction=0.0, hold_conviction=0.0), flicker)
    banded = _simulate(model(), flicker)

    assert unbanded >= 4, "the churn this technique targets is not being reproduced"
    assert banded == 0
    saved_bps = (unbanded - banded) * SELL_TAX_BPS
    assert saved_bps >= 80.0


def test_a_signal_that_really_does_turn_still_trades():
    """The band must not become a ratchet that traps the book."""
    turning = [(0.90, 0.05)] * 3 + [(0.05, 0.90)] * 3
    assert _simulate(model(), turning) == 1


# ── decay: stale evidence may sustain, never initiate ────────────────────
def test_a_stale_signal_keeps_a_position_but_cannot_start_one():
    fresh = insight(0.9, period=timedelta(days=10))
    stale = insight(0.9, period=timedelta(days=10), age=timedelta(days=8))
    assert stale.is_active(T0), "the fixture must still be a live insight"

    assert target_qty(model(), make_ctx(), [fresh]) > 0
    assert target_qty(model(), make_ctx(), [stale]) == 0
    assert target_qty(model(), hold(make_ctx(), FULL), [stale]) == Decimal(FULL)


def test_defaults_admit_every_fresh_signal_this_engine_emits():
    """A tripwire on the default entry bar.

    The lowest confidence any built-in alpha emits is RetailFlowContrarian's
    0.42 floor. If a future default rises past it, entries stop happening
    engine-wide and the only symptom is a book that never trades.
    """
    assert target_qty(model(), make_ctx(), [insight(0.42)]) > 0


# ── exits are never delayed ──────────────────────────────────────────────
def test_the_band_can_only_ever_subtract_exposure():
    """The invariant the whole safety argument rests on.

    Whatever the position and whatever the evidence, the banded score is the
    unbanded score or it is zero. Never larger, never the other sign — so no
    position survives the band that would not have survived without it.
    """
    off = model(entry_conviction=0.0, hold_conviction=0.0)
    on = model()
    for held in (-100, 0, 100):
        for direction in (Direction.UP, Direction.DOWN):
            for confidence in (0.02, 0.14, 0.16, 0.39, 0.41, 0.95):
                for other in (None, Direction.UP, Direction.DOWN):
                    ctx = make_ctx()
                    if held:
                        hold(ctx, held)
                    items = [insight(confidence, direction, source="a")]
                    if other is not None:
                        items.append(insight(0.3, other, source="b"))
                    raw = off._net_scores(items, ctx)[SYM.key][1]
                    got = on._net_scores(items, ctx)[SYM.key][1]
                    assert got in (raw, 0.0), (held, direction, confidence, other)


def test_a_flat_veto_is_exempt_from_the_band():
    """TrendRegimeFilter is a stop wearing an alpha's clothes.

    It reaches the portfolio layer as an insight, so a band applied naively
    would let a held position sit through the regime veto — a widened stop
    nobody configured.
    """
    ctx = hold(make_ctx(), FULL)
    vetoed = [insight(0.95, Direction.UP, source="momentum"),
              insight(0.9, Direction.FLAT, source="regime_filter")]
    assert target_qty(model(), ctx, vetoed) == 0
    # ...and no width of band changes that.
    assert target_qty(model(entry_conviction=1.0, hold_conviction=0.0), ctx, vetoed) == 0


def _armed_trailing_stop():
    ctx = hold(make_ctx(), FULL)
    pos = ctx.portfolio.position(SYM)
    pos.mark(120.0)                              # ran up 20%...
    pos.mark(ctx.price(SYM))                     # ...and handed all of it back
    return ctx, TrailingStopRiskModel(trail_pct=0.06)


def _armed_stop_loss():
    ctx = hold(make_ctx(cash=2_000_000.0), FULL, price=120.0)   # -16.7% since entry
    return ctx, MaximumDrawdownPerSecurity(max_drawdown_pct=0.08)


def _armed_time_stop():
    ctx = hold(make_ctx(), FULL)
    ctx.portfolio.position(SYM).opened_at = T0 - timedelta(days=41)
    return ctx, TimeStopRiskModel(max_bars_held=40, min_progress_pct=0.01)


@pytest.mark.parametrize("arm", [_armed_trailing_stop, _armed_stop_loss, _armed_time_stop])
def test_the_band_cannot_delay_a_risk_mandated_exit(arm):
    """Portfolio says hold, the risk layer says out. Out wins, every time.

    Banding a risk exit would be indistinguishable from quietly widening the
    stop, which is the one failure mode this technique is known for.
    """
    ctx, risk_model = arm()

    targets = model().create_targets(ctx, [insight(0.25)])
    assert targets and targets[0].quantity > 0, "the band should want to hold here"

    managed = CompositeRiskModel(risk_model).manage(ctx, targets)
    assert [t.quantity for t in managed] == [Decimal("0")]
    assert managed[0].source == "risk"


class _WeakBull(AlphaModel):
    """Enough conviction to keep a position, never enough to have opened one."""

    name = "weak_bull"

    async def update(self, ctx, bars):
        return [Insight(symbol=b.symbol, direction=Direction.UP, period=PERIOD,
                        generated_at=ctx.now, confidence=0.25, source=self.name)
                for b in bars.values()]


def _one_engine_bar(risk_models: list) -> list:
    """One bar through the real pipeline over a position the band wants to keep."""
    from quant.brokerage.paper import PaperBrokerage
    from quant.core.engine import Engine
    from quant.execution.models import ImmediateExecution

    ctx = hold(make_ctx(), FULL)
    ctx.portfolio.position(SYM).mark(120.0)          # peak, then all of it back
    engine = Engine(ctx, _WeakBull(), model(), ImmediateExecution(min_order_notional=1.0),
                    PaperBrokerage(ctx.portfolio), risk_models=risk_models)
    asyncio.run(engine.start())
    asyncio.run(engine.on_bars({SYM.key: Bar(SYM, T0, PRICE, PRICE * 1.01, PRICE * 0.99,
                                             PRICE, 1e6, "1d")}))
    return [o for o in engine.orders if o.side is OrderSide.SELL]


def test_a_stop_fires_through_the_band_in_the_running_engine():
    """The same claim again, but through the real pipeline.

    Ordering is the whole defence: risk runs after portfolio construction and
    may only reduce, so no band the portfolio layer applies can reach past it.
    Testing the two layers separately would not notice if that order were ever
    swapped, and the control run is what shows the band really was holding —
    otherwise a stop-shaped exit could just as well be the band's own.
    """
    assert _one_engine_bar([]) == [], "the band should be holding without a stop"

    stopped = _one_engine_bar([TrailingStopRiskModel(trail_pct=0.06)])
    assert [o.quantity for o in stopped] == [Decimal(FULL)]
    assert "trailing_stop" in stopped[0].tag


def test_the_hold_bar_cannot_resurrect_a_position_risk_has_closed():
    """After a risk flatten the engine clears the symbol's insights.

    With nothing live to sustain it, the next bar must still target zero — the
    band has no memory of its own to hold the position with.
    """
    ctx = hold(make_ctx(), FULL)
    assert target_qty(model(), ctx, []) == 0


def test_an_unfilled_entry_order_has_not_earned_the_hold_rate():
    """A resting order is an intention, not a position.

    Nothing has been paid yet and cancelling costs nothing, so the entry bar
    still applies — the cheap decision stays the conservative one.
    """
    ctx = make_ctx()
    ctx.set_pending({SYM.key: Decimal(FULL)})
    assert ctx.projected_quantity(SYM) == Decimal(FULL)
    assert target_qty(model(), ctx, [insight(0.25)]) == 0


def test_an_expired_signal_is_not_held_by_the_band():
    ctx = hold(make_ctx(), FULL)
    dead = insight(0.9, period=PERIOD, age=PERIOD + timedelta(days=1))
    assert not dead.is_active(T0)
    assert target_qty(model(), ctx, [dead]) == 0


# ── direction changes are opens, not holds ───────────────────────────────
def test_a_flip_against_an_open_position_must_clear_the_entry_bar():
    ctx = hold(make_ctx(), FULL)
    # hold-band conviction in the *opposite* direction: the long is not
    # entitled to become a short, so the book goes flat.
    assert target_qty(model(allow_short=True), ctx, [insight(0.25, Direction.DOWN)]) == 0
    assert target_qty(model(allow_short=True), ctx, [insight(0.55, Direction.DOWN)]) < 0


def test_a_short_is_held_on_the_same_asymmetry():
    ctx = hold(make_ctx(), -FULL)
    short = model(allow_short=True)
    assert target_qty(short, ctx, [insight(0.25, Direction.DOWN)]) < 0
    assert target_qty(short, ctx, [insight(0.10, Direction.DOWN)]) == 0
    assert target_qty(short, make_ctx(), [insight(0.25, Direction.DOWN)]) == 0


# ── the band belongs to the layer, not to one model ──────────────────────
@pytest.mark.parametrize("name", sorted(BUILTIN_PORTFOLIO_MODELS))
def test_every_built_in_model_is_gated_the_same_way(name):
    cls = BUILTIN_PORTFOLIO_MODELS[name]
    kwargs = {"cash_reserve_pct": 0.0, "min_trade_weight": 0.0, "max_position_weight": 1.0}
    weak = [insight(0.25)]

    assert target_qty(cls(**kwargs), make_ctx(), weak) == 0, name
    assert target_qty(cls(**kwargs), hold(make_ctx(), 100), weak) > 0, name
    assert target_qty(cls(**kwargs), make_ctx(), [insight(0.9)]) > 0, name


def test_the_band_survives_a_restart_because_it_keeps_no_state():
    """Every input is the live book and the live insights — nothing to persist."""
    ctx = hold(make_ctx(), FULL)
    weak = [insight(0.25)]
    first = target_qty(model(), ctx, weak)
    restarted = target_qty(model(), ctx, weak)
    assert first == restarted > 0


def test_the_band_is_configurable_from_the_strategy_file():
    """`portfolio.model.params` reaches the constructor as keyword arguments."""
    from quant.config.schema import ModelSpec, PortfolioConfig, StrategyConfig
    from quant.strategy.builder import build_portfolio_model

    cfg = StrategyConfig(
        name="band",
        portfolio=PortfolioConfig(model=ModelSpec(
            type="equal_weight",
            params={"entry_conviction": 0.7, "hold_conviction": 0.3},
        )),
    )
    built = build_portfolio_model(cfg)
    assert (built.entry_conviction, built.hold_conviction) == (0.7, 0.3)
