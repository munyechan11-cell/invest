"""동적 유니버스 — the selection chain.

Most of the ways a strategy quietly bleeds are universe problems, not signal
problems: a pair listed three days ago, an 80bp spread, a name that has not
moved in a month. These tests pin the filters that catch each one — and the
one rule that must never break, which is that a held position stays in the
universe so an exit can still be emitted for it.
"""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, Bar, Symbol
from quant.data.provider import DataProvider
from quant.data.universe import (
    AgeFilter,
    CorrelationFilter,
    HeldPositionFilter,
    LimitFilter,
    PriceFilter,
    RangeStabilityFilter,
    SelectionReport,
    ShuffleFilter,
    StaticSource,
    UniverseSelector,
    VolatilityFilter,
    VolumeFilter,
)

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def sym(t, tick="0.01"):
    return Symbol(t, venue="SIM", tick_size=Decimal(tick))


class NullProvider(DataProvider):
    name = "null"

    async def history(self, symbol, timeframe, start, end):
        return []


def ctx_with(series: dict[Symbol, list[tuple[float, float]]], bars_back=None):
    """series: symbol -> [(close, volume), ...] oldest first."""
    longest = max(len(v) for v in series.values())
    pf = Portfolio(1_000_000.0)
    clock = SimClock(T0 + timedelta(days=longest + 1))
    ctx = Context(clock, pf, EventBus(), timeframe="1d", history_size=2000)
    ctx.universe = list(series)
    for s, points in series.items():
        for i, (close, vol) in enumerate(points):
            ts = T0 + timedelta(days=i)
            ctx.push_bar(Bar(s, ts, close, close * 1.01, close * 0.99, close, vol, "1d"))
    return ctx


def run(f, ctx, symbols):
    report = SelectionReport()
    out = asyncio.run(f.apply(ctx, symbols, report))
    return out, report


# ── age ──────────────────────────────────────────────────────────────────
def test_age_filter_drops_freshly_listed_instruments():
    old, new = sym("OLD"), sym("NEW")
    ctx = ctx_with({old: [(100, 1e6)] * 250, new: [(100, 1e6)] * 20})
    out, report = run(AgeFilter(min_bars=200), ctx, [old, new])
    assert out == [old]
    assert "NEW" in report.reasons and "bars" in report.reasons["NEW"]


# ── volume ───────────────────────────────────────────────────────────────
def test_volume_filter_ranks_by_traded_value_not_share_count():
    """10m shares of a cheap stock is a different market from 10m of a dear one."""
    cheap, dear = sym("CHEAP"), sym("DEAR")
    ctx = ctx_with({cheap: [(10, 10_000_000)] * 30, dear: [(1000, 1_000_000)] * 30})
    out, _ = run(VolumeFilter(lookback_bars=20, top_n=1), ctx, [cheap, dear])
    assert out == [dear]        # 1e9 of turnover vs 1e8


def test_volume_filter_enforces_a_floor():
    thin = sym("THIN")
    ctx = ctx_with({thin: [(10, 100)] * 30})
    out, report = run(VolumeFilter(min_value=1_000_000), ctx, [thin])
    assert out == []
    assert "turnover" in report.reasons["THIN"]


# ── price / tick ─────────────────────────────────────────────────────────
def test_price_filter_drops_a_coarse_tick_grid():
    """A ₩1,000 name on a ₩1 tick has 10bp of granularity — a limit order can
    only sit on a grid coarser than many strategies' whole edge."""
    coarse = Symbol("COARSE", venue="SIM", tick_size=Decimal("1"))
    fine = Symbol("FINE", venue="SIM", tick_size=Decimal("0.01"))
    ctx = ctx_with({coarse: [(1000, 1e6)] * 5, fine: [(1000, 1e6)] * 5})
    out, report = run(PriceFilter(max_tick_pct=0.0005), ctx, [coarse, fine])
    assert out == [fine]
    assert "tick" in report.reasons["COARSE"]


def test_price_filter_bounds():
    penny, normal = sym("PENNY"), sym("NORMAL")
    ctx = ctx_with({penny: [(0.4, 1e6)] * 5, normal: [(50, 1e6)] * 5})
    out, _ = run(PriceFilter(min_price=1.0), ctx, [penny, normal])
    assert out == [normal]


# ── volatility / range ───────────────────────────────────────────────────
def test_volatility_filter_rejects_both_ends():
    flat, wild, ok = sym("FLAT"), sym("WILD"), sym("OK")
    ctx = ctx_with({
        flat: [(100 + i * 1e-6, 1e6) for i in range(80)],
        wild: [(100 * (1.4 if i % 2 else 0.6) ** 1, 1e6) for i in range(80)],
        ok:   [(100 * (1.01 if i % 2 else 0.995), 1e6) for i in range(80)],
    })
    out, report = run(VolatilityFilter(min_annual_vol=0.05, max_annual_vol=1.5),
                      ctx, [flat, wild, ok])
    assert flat not in out and wild not in out
    assert "below" in report.reasons["FLAT"]
    assert "above" in report.reasons["WILD"]


def test_range_stability_drops_a_name_that_has_not_moved():
    """Different question from volatility: a name can be jumpy and still go
    nowhere, and a range smaller than a round trip cannot pay for one."""
    stuck = sym("STUCK")
    ctx = ctx_with({stuck: [(100.0, 1e6)] * 40})
    out, report = run(RangeStabilityFilter(lookback_bars=30, min_range_pct=0.05),
                      ctx, [stuck])
    assert out == []
    assert "range" in report.reasons["STUCK"]


# ── correlation ──────────────────────────────────────────────────────────
def test_correlation_filter_keeps_one_of_a_pair_of_twins():
    a, b, c = sym("A"), sym("B"), sym("C")
    import math
    base = [100 * math.exp(math.sin(i / 7) * 0.05) for i in range(120)]
    other = [100 * math.exp(math.cos(i / 3) * 0.05) for i in range(120)]
    ctx = ctx_with({
        a: [(p, 1e6) for p in base],
        b: [(p * 1.5, 1e6) for p in base],       # perfectly correlated with A
        c: [(p, 1e6) for p in other],
    })
    out, report = run(CorrelationFilter(lookback_bars=100, max_correlation=0.9),
                      ctx, [a, b, c])
    assert a in out and c in out
    assert b not in out
    assert "corr" in report.reasons["B"]


# ── ordering and caps ────────────────────────────────────────────────────
def test_limit_filter_caps_the_book():
    syms = [sym(f"S{i}") for i in range(10)]
    ctx = ctx_with({s: [(100, 1e6)] * 5 for s in syms})
    out, _ = run(LimitFilter(max_symbols=3), ctx, syms)
    assert len(out) == 3


def test_shuffle_changes_order_between_rounds():
    """A stable order plus a downstream cap means the same names always win,
    and a backtest of that is a backtest of the ordering."""
    syms = [sym(f"S{i}") for i in range(20)]
    ctx = ctx_with({s: [(100, 1e6)] * 3 for s in syms})
    f = ShuffleFilter(seed=1)
    first, _ = run(f, ctx, syms)
    second, _ = run(f, ctx, syms)
    assert first != second
    assert sorted(s.ticker for s in first) == sorted(s.ticker for s in second)


# ── the rule that must never break ───────────────────────────────────────
def test_a_held_position_is_always_re_added():
    """Dropping a symbol you hold means no model emits an exit and the position
    becomes permanent. This is the most dangerous failure a dynamic universe has."""
    held, other = sym("HELD"), sym("OTHER")
    ctx = ctx_with({held: [(100, 1e6)] * 5, other: [(100, 1e6)] * 5})
    ctx.portfolio.position(held).quantity = Decimal("10")
    out, report = run(HeldPositionFilter(), ctx, [other])
    assert held.key in {s.key for s in out}
    assert "exited" in report.reasons["HELD"]


def test_the_chain_appends_the_held_filter_even_if_you_forget_it():
    selector = UniverseSelector(StaticSource([]), [LimitFilter(max_symbols=1)])
    assert any(isinstance(f, HeldPositionFilter) for f in selector.filters)


def test_a_held_position_survives_a_hard_cap():
    held = sym("HELD")
    others = [sym(f"S{i}") for i in range(5)]
    ctx = ctx_with({s: [(100, 1e6)] * 5 for s in [held, *others]})
    ctx.portfolio.position(held).quantity = Decimal("10")
    selector = UniverseSelector(StaticSource(others), [LimitFilter(max_symbols=2)])
    out = asyncio.run(selector.select(ctx, NullProvider()))
    assert held.key in {s.key for s in out}
    assert len(out) == 3        # 2 capped + the held one re-added


# ── chain behaviour ──────────────────────────────────────────────────────
def test_a_broken_filter_does_not_empty_the_book():
    class Exploding(AgeFilter):
        name = "exploding"

        async def apply(self, ctx, symbols, report):
            raise RuntimeError("boom")

    s = sym("A")
    ctx = ctx_with({s: [(100, 1e6)] * 300})
    selector = UniverseSelector(StaticSource([s]), [Exploding()])
    assert asyncio.run(selector.select(ctx, NullProvider())) == [s]


def test_refresh_cadence():
    selector = UniverseSelector(StaticSource([]), [], refresh_every_bars=3)
    assert [selector.due() for _ in range(7)] == [True, False, False,
                                                  True, False, False, True]


def test_report_records_why_each_symbol_left():
    keep, drop = sym("KEEP"), sym("DROP")
    ctx = ctx_with({keep: [(100, 1e6)] * 300, drop: [(100, 1e6)] * 10})
    selector = UniverseSelector(StaticSource([keep, drop]), [AgeFilter(min_bars=200)])
    asyncio.run(selector.select(ctx, NullProvider()))
    report = selector.last_report
    assert report.candidates == 2
    assert report.selected == ["KEEP"]
    assert "DROP" in report.reasons
    assert "age" in report.dropped
