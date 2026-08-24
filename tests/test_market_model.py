"""Rolling market model, and the attribution ledger that now rides on it.

The ledger used to compute excess as `realised - benchmark`, which prices every
name at beta 1. `test_the_naive_ledger_ranks_the_wrong_alpha_model_first` is the
reason this file exists: with a plain difference the ledger prefers a model that
is merely levered to a rising tape over one that is actually adding return, and
that ranking is what culls alpha models.
"""
import math
from datetime import datetime, timedelta

import pytest

from quant.alpha import attribution
from quant.alpha.attribution import InsightLedger, is_krx_equity
from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, AssetClass, Bar, Direction, Insight, Symbol
from quant.indicators.streaming import (
    KRX_PRICE_LIMIT,
    MarketModel,
    is_limit_move,
)

T0 = datetime(2024, 1, 1, tzinfo=UTC)

#: A repeating market that rises on average (+0.32%/bar) but genuinely moves.
#: A reference with no variance carries no information about beta, so a flat
#: tape would test nothing.
PATTERN = (0.010, -0.006, 0.008, -0.002, 0.006)

MARKET = Symbol("MKT", venue="SIM")
NAME = Symbol("T", venue="SIM")


def market_returns(n: int) -> list[float]:
    return [PATTERN[i % len(PATTERN)] for i in range(n)]


def walk(returns: list[float], start: float = 100.0) -> list[float]:
    """Prices implied by `returns` — one more price than there are returns."""
    price, prices = start, [start]
    for r in returns:
        price *= 1.0 + r
        prices.append(price)
    return prices


def bars(symbol: Symbol, closes: list[float], start: datetime = T0) -> list[Bar]:
    return [Bar(symbol, start + timedelta(days=i), c, c, c, c, 1e6, "1d")
            for i, c in enumerate(closes)]


def feed(model: MarketModel, target_rets: list[float], reference_rets: list[float]) -> MarketModel:
    for t, r in zip(target_rets, reference_rets):
        model.observe(t, r)
    return model


# ── the regression itself ────────────────────────────────────────────────
def test_market_model_recovers_a_known_beta_and_intercept():
    market = market_returns(120)
    model = feed(MarketModel(period=60), [1.5 * m + 0.0004 for m in market], market)

    assert model.is_ready
    assert model.raw_beta == pytest.approx(1.5, abs=1e-9)
    assert model.alpha == pytest.approx(0.0004, abs=1e-9)
    assert model.value == pytest.approx(1.5, abs=1e-6)      # a clean fit is not shrunk


def test_residuals_are_the_regression_residuals():
    market = market_returns(80)
    target = [1.2 * m + 0.001 + (0.004 if i % 7 == 0 else -0.001)
              for i, m in enumerate(market)]
    model = feed(MarketModel(period=60), target, market)

    assert len(model.residuals) == 60
    assert sum(model.residuals) == pytest.approx(0.0, abs=1e-12)
    assert model.residual == pytest.approx(model.residuals[-1])
    # orthogonal to the reference — the defining property of an OLS residual
    assert sum(e * m for e, m in zip(model.residuals, market[-60:])) == pytest.approx(
        0.0, abs=1e-12)


def test_shrinkage_pulls_a_noisy_estimate_toward_the_prior():
    """Vasicek: the noisier the window, the closer to the cross-sectional prior."""
    market = market_returns(60)
    noise = [0.05 * math.sin(i * 2.399) for i in range(60)]       # deterministic, large
    noisy = feed(MarketModel(period=60), [3.0 * m + n for m, n in zip(market, noise)], market)
    clean = feed(MarketModel(period=60), [3.0 * m for m in market], market)

    assert clean.value == pytest.approx(clean.raw_beta, abs=1e-6)
    assert abs(noisy.value - noisy.prior_beta) < abs(noisy.raw_beta - noisy.prior_beta)
    assert min(noisy.raw_beta, 1.0) <= noisy.value <= max(noisy.raw_beta, 1.0)


def test_a_reference_that_never_moves_yields_the_prior():
    model = feed(MarketModel(period=20), [0.01, -0.02, 0.03, 0.01] * 10, [0.0] * 40)
    assert model.value == pytest.approx(1.0)
    assert model.raw_beta is None      # never estimated, and never pretended to be


def test_the_running_sums_match_a_direct_recomputation():
    """The sums are kept by subtraction; they are rebuilt once per window so a
    live session cannot drift. Feed far more than one window to exercise that."""
    market = market_returns(500)
    target = [1.3 * m + 0.0002 * (i % 3) for i, m in enumerate(market)]
    model = feed(MarketModel(period=60), target, market)

    window_x, window_y = market[-60:], target[-60:]
    mean_x, mean_y = sum(window_x) / 60, sum(window_y) / 60
    sxx = sum((x - mean_x) ** 2 for x in window_x)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(window_x, window_y))
    assert model.raw_beta == pytest.approx(sxy / sxx, rel=1e-12)


# ── alignment: drop, never forward-fill ──────────────────────────────────
def test_an_unmatched_bar_is_dropped_rather_than_carried_forward():
    """A stale price is a zero return against a live market. Filling one biases
    beta toward zero on exactly the halted, thin names it is worst on."""
    market = market_returns(12)
    price_m, price_t = walk(market), walk([2.0 * m for m in market])
    halted = {4, 7, 9}

    dropped = MarketModel(period=12)
    kept = [(i, b) for i, b in enumerate(bars(NAME, price_t)) if i not in halted]
    reference = bars(MARKET, price_m)
    for i, bar in kept:
        dropped.update_reference(reference[i])
        dropped.update_target(bar)

    filled = MarketModel(period=12)
    stale = list(price_t)
    for i in halted:
        stale[i] = stale[i - 1]                       # the forward-fill LEAN is accused of
    feed(filled,
         [stale[i] / stale[i - 1] - 1.0 for i in range(1, len(stale))],
         market)

    assert dropped.samples == len(kept) - 1
    assert dropped.raw_beta == pytest.approx(2.0, abs=0.05)
    assert filled.raw_beta < dropped.raw_beta - 0.2   # the bias the fill introduces


def test_a_gap_leaves_both_legs_spanning_the_same_interval():
    """The observation after a gap is a two-day return on *both* sides, never a
    two-day target against a one-day market."""
    market = market_returns(6)
    reference, target = bars(MARKET, walk(market)), bars(NAME, walk(market))
    model = MarketModel(period=6)
    for i, bar in enumerate(target):
        if i == 3:
            continue                                  # the target never printed this bar
        model.update_reference(reference[i])
        model.update_target(bar)

    assert model.samples == 5                          # 7 bars, one gone, one pair lost
    assert model.raw_beta == pytest.approx(1.0, abs=1e-9)


def test_one_leg_running_ahead_forms_no_observation():
    reference = bars(MARKET, [100.0, 101.0, 102.0])
    target = bars(NAME, [100.0, 102.0, 104.0])
    model = MarketModel(period=3)
    model.update_reference(reference[0])
    model.update_reference(reference[1])              # the target has not printed yet
    model.update_target(target[0])                     # ... and this bar is a stale one
    assert model.samples == 0
    model.update_target(target[1])                     # the first match is a baseline
    assert model.samples == 0
    model.update_pair(target[2], reference[2])
    assert model.samples == 1
    assert model._y[-1] == pytest.approx(104.0 / 102.0 - 1.0)


def test_prime_pairs_equals_incremental_feeding():
    market = market_returns(80)
    reference = bars(MARKET, walk(market))
    target = bars(NAME, walk([1.4 * m for m in market]))

    primed = MarketModel(period=60).prime_pairs(target, reference)
    incremental = MarketModel(period=60)
    for t, r in zip(target, reference):
        incremental.update_pair(t, r)
    assert primed.value == pytest.approx(incremental.value)
    assert primed.samples == incremental.samples


def test_a_two_symbol_indicator_refuses_a_single_stream():
    with pytest.raises(TypeError):
        MarketModel().update(101.0)


# ── KRX: the ±30% limit censors returns ──────────────────────────────────
def test_is_limit_move_allows_for_the_tick_rounded_ceiling():
    assert is_limit_move(0.30) and is_limit_move(-0.30)
    assert is_limit_move(0.2995)          # 상한가 snapped onto the tick ladder
    assert not is_limit_move(0.25)
    assert not is_limit_move(0.0)


def test_limit_hit_bars_never_enter_the_regression_window():
    """A 상한가 is a censored observation: the true move was cut off at +30%.
    Regressing on it estimates the limit, not the name's market exposure."""
    market = market_returns(60)
    target = [1.5 * m for m in market]
    limit_days = [i for i, m in enumerate(market) if m == max(PATTERN)][:6]
    for i in limit_days:
        target[i] = 0.2995                # would have been ~+45% on the news

    filtered = feed(MarketModel(period=60, price_limit=KRX_PRICE_LIMIT), target, market)
    unfiltered = feed(MarketModel(period=60), target, market)

    assert filtered.dropped == len(limit_days)
    assert filtered.samples == 60 - len(limit_days)
    assert filtered.raw_beta == pytest.approx(1.5, abs=1e-9)   # the clean days, exactly
    assert abs(unfiltered.raw_beta - 1.5) > 1.0                # the censored estimate


def test_the_window_refills_from_further_back_when_bars_are_dropped():
    """Otherwise one 상한가 disables the adjustment for a whole window."""
    market = market_returns(90)
    target = [1.5 * m for m in market]
    for i in range(0, 90, 10):
        target[i] = -0.30                 # 하한가, nine of them
    model = feed(MarketModel(period=60, price_limit=KRX_PRICE_LIMIT), target, market)

    assert model.dropped == 9
    assert model.is_ready
    assert model.raw_beta == pytest.approx(1.5, abs=1e-9)


# ── what the ledger does with it ─────────────────────────────────────────
def make_ctx(now: datetime) -> Context:
    return Context(SimClock(now), Portfolio(1_000_000.0), EventBus(), timeframe="1d")


def end_of(index: int) -> datetime:
    """When bar `index` has closed — `ctx.history` will include it from here."""
    return T0 + timedelta(days=index + 1)


def push(ctx: Context, symbol: Symbol, closes: list[float]) -> None:
    for bar in bars(symbol, closes):
        ctx.push_bar(bar)


def score_one(ctx: Context, ledger: InsightLedger, symbol: Symbol, at: int,
              horizon: int = 5, source: str = "test"):
    """Record one call at bar `at` and settle it `horizon` bars later."""
    ctx.clock.set(end_of(at))
    ledger.record(ctx, [Insight(symbol=symbol, direction=Direction.UP,
                                period=timedelta(days=horizon), generated_at=ctx.now,
                                source=source, confidence=0.6)])
    ctx.clock.set(end_of(at + horizon))
    settled = ledger.settle(ctx)
    assert settled, "the insight never settled"
    return settled[0]


def test_excess_is_the_return_the_market_did_not_deliver():
    market = market_returns(200)
    ctx = make_ctx(end_of(130))
    push(ctx, MARKET, walk(market))
    push(ctx, NAME, walk([2.0 * m for m in market]))
    ctx.universe = [NAME]

    record = score_one(ctx, InsightLedger(benchmark=MARKET), NAME, at=130)

    assert record.beta == pytest.approx(2.0, abs=1e-6)
    assert record.excess_pct == pytest.approx(
        record.realised_pct - 2.0 * record.benchmark_pct, abs=1e-12)
    # and the plain difference it replaces would have said something else
    assert record.excess_pct != pytest.approx(record.realised_pct - record.benchmark_pct)


def test_beta_is_estimated_before_the_call_not_over_its_horizon():
    """Estimating on the window that contains the holding period would let the
    same bars set the adjustment and the thing being adjusted."""
    market = market_returns(200)
    target = [(1.5 if i <= 130 else 6.0) * m for i, m in enumerate(market)]
    ctx = make_ctx(end_of(130))
    push(ctx, MARKET, walk(market))
    push(ctx, NAME, walk(target))
    ctx.universe = [NAME]

    record = score_one(ctx, InsightLedger(benchmark=MARKET), NAME, at=130)
    assert record.beta == pytest.approx(1.5, abs=0.05)


def test_with_no_benchmark_the_excess_column_is_the_raw_return():
    """An unbenchmarked run must say so, not invent a reference."""
    market = market_returns(200)
    ctx = make_ctx(end_of(130))
    push(ctx, MARKET, walk(market))
    push(ctx, NAME, walk([2.0 * m for m in market]))
    ctx.universe = [NAME]

    record = score_one(ctx, InsightLedger(benchmark=None), NAME, at=130)
    assert record.benchmark_pct == 0.0
    assert record.excess_pct == record.realised_pct
    assert record.reference == ""


def test_an_unpriceable_benchmark_leaves_the_return_unadjusted():
    market = market_returns(200)
    ctx = make_ctx(end_of(130))
    push(ctx, NAME, walk([2.0 * m for m in market]))
    ctx.universe = [NAME]

    record = score_one(ctx, InsightLedger(benchmark=MARKET), NAME, at=130)
    assert record.beta == 1.0
    assert record.excess_pct == record.realised_pct


def test_a_short_history_holds_beta_at_the_prior_rather_than_guessing():
    market = market_returns(40)               # fewer bars than the regression window
    ctx = make_ctx(end_of(20))
    push(ctx, MARKET, walk(market))
    push(ctx, NAME, walk([2.0 * m for m in market]))
    ctx.universe = [NAME]

    record = score_one(ctx, InsightLedger(benchmark=MARKET), NAME, at=20)
    assert record.beta == 1.0
    assert record.excess_pct == pytest.approx(record.realised_pct - record.benchmark_pct)


# ── the defect this whole file exists for ────────────────────────────────
HIGH_BETA, LOW_BETA = "high_beta", "low_beta"


def build_two_models() -> tuple[Context, InsightLedger]:
    """Two alpha models in a rising tape. `high_beta` picks a name levered to
    the market with a *negative* true alpha; `low_beta` picks one with half the
    exposure and a positive one. `low_beta` is the better model."""
    market = market_returns(200)
    ctx = make_ctx(end_of(130))
    push(ctx, MARKET, walk(market))
    push(ctx, Symbol("HB", venue="SIM"), walk([2.0 * m - 0.0005 for m in market]))
    push(ctx, Symbol("LB", venue="SIM"), walk([0.5 * m + 0.0010 for m in market]))
    ctx.universe = [Symbol("HB", venue="SIM"), Symbol("LB", venue="SIM")]

    ledger = InsightLedger(benchmark=MARKET)
    for i in range(130, 155):
        ctx.clock.set(end_of(i))
        ledger.record(ctx, [
            Insight(symbol=Symbol("HB", venue="SIM"), direction=Direction.UP,
                    period=timedelta(days=5), generated_at=ctx.now, source=HIGH_BETA),
            Insight(symbol=Symbol("LB", venue="SIM"), direction=Direction.UP,
                    period=timedelta(days=5), generated_at=ctx.now, source=LOW_BETA),
        ])
        ledger.settle(ctx)
    ctx.clock.set(end_of(161))
    ledger.settle(ctx)
    return ctx, ledger


def test_the_naive_ledger_ranks_the_wrong_alpha_model_first():
    _, ledger = build_two_models()
    scored = ledger.scored
    naive = {name: [s.realised_pct - s.benchmark_pct for s in scored if s.source == name]
             for name in (HIGH_BETA, LOW_BETA)}

    # The plain difference: the levered name looks like the winner, and the
    # model that is actually adding return looks like the one to cut.
    assert sum(naive[HIGH_BETA]) / len(naive[HIGH_BETA]) > 0
    assert sum(naive[LOW_BETA]) / len(naive[LOW_BETA]) < 0


def test_the_beta_adjusted_ledger_ranks_them_the_right_way_round():
    _, ledger = build_two_models()
    high, low = ledger.sources[HIGH_BETA], ledger.sources[LOW_BETA]

    assert high.avg_beta == pytest.approx(2.0, abs=1e-6)
    assert low.avg_beta == pytest.approx(0.5, abs=1e-6)
    assert low.excess_expectancy > 0 > high.excess_expectancy
    assert list(ledger.report()["by_source"]) == [LOW_BETA, HIGH_BETA]
    assert ledger.worst_source == HIGH_BETA


def test_the_report_carries_the_beta_it_adjusted_by():
    _, ledger = build_two_models()
    row = ledger.report()["by_source"][HIGH_BETA]
    assert row["avg_beta"] == pytest.approx(2.0, abs=1e-3)
    assert "β" in "".join(ledger.summary_lines())


# ── KRX: the index is not a market factor ────────────────────────────────
def krx(ticker: str) -> Symbol:
    return Symbol(ticker, venue="kis", asset_class=AssetClass.EQUITY, quote_currency="KRW")


KODEX_200 = Symbol("069500", venue="kis", asset_class=AssetClass.ETF, quote_currency="KRW")
SAMSUNG = krx("005930")
PEERS = [krx(f"00{i}0" * 1) for i in range(1, 8)]


def krx_ctx(peer_returns: list[list[float]], target: list[float], now: int) -> Context:
    """A Korean book where the index ETF *is* the target — the concentration
    problem in its purest form: 삼성전자 and SK하이닉스 are over half of KOSPI 200."""
    ctx = make_ctx(end_of(now))
    push(ctx, SAMSUNG, walk(target))
    push(ctx, KODEX_200, walk(target))            # regressing a thing on itself
    for peer, returns in zip(PEERS, peer_returns):
        push(ctx, peer, walk(returns))
    ctx.universe = [SAMSUNG, *PEERS]
    return ctx


def test_a_korean_name_is_measured_against_its_peers_not_the_index():
    market = market_returns(200)
    ctx = krx_ctx([market] * len(PEERS), [3.0 * m for m in market], now=130)

    record = score_one(ctx, InsightLedger(benchmark=KODEX_200), SAMSUNG, at=130)

    assert record.reference.startswith("동일가중")
    # Against the index (which is itself) beta would be 1 and every residual 0.
    assert record.beta == pytest.approx(3.0, abs=0.05)
    assert record.excess_pct == pytest.approx(
        record.realised_pct - record.beta * record.benchmark_pct, abs=1e-12)


def test_the_peer_basket_leaves_the_name_itself_out():
    market = market_returns(200)
    ctx = krx_ctx([market] * len(PEERS), [3.0 * m for m in market], now=130)

    record = score_one(ctx, InsightLedger(benchmark=KODEX_200), SAMSUNG, at=130)

    peer_move = ctx.price(PEERS[0]) / walk(market)[130] - 1.0
    assert record.benchmark_pct == pytest.approx(peer_move, abs=1e-12)
    assert record.realised_pct != pytest.approx(peer_move)   # the name did something else


def test_a_limit_up_peer_does_not_drag_the_basket(monkeypatch):
    """The basket is an average, so one censored +30% print moves every name's
    reference — and every name's beta with it."""
    market = market_returns(200)
    limit_day = 100
    loud = list(market)
    loud[limit_day] = 0.2995                       # one peer hits 상한가
    peers = [loud] + [list(market) for _ in PEERS[1:]]
    target = [1.0 * m for m in market]

    ctx = krx_ctx(peers, target, now=130)
    clean = score_one(ctx, InsightLedger(benchmark=KODEX_200), SAMSUNG, at=130)
    assert clean.beta == pytest.approx(1.0, abs=0.02)

    monkeypatch.setattr(attribution, "is_limit_move", lambda ret, **kw: False)
    leaked = score_one(krx_ctx(peers, target, now=130),
                       InsightLedger(benchmark=KODEX_200), SAMSUNG, at=130)
    assert leaked.beta < clean.beta - 0.1


def test_too_few_peers_says_so_rather_than_regressing_on_the_index():
    """With no basket to form, the honest answer is the old unadjusted number —
    not a beta measured against an index the name dominates."""
    market = market_returns(200)
    ctx = krx_ctx([market] * len(PEERS), [3.0 * m for m in market], now=130)
    ctx.universe = [SAMSUNG, *PEERS[:2]]

    record = score_one(ctx, InsightLedger(benchmark=KODEX_200), SAMSUNG, at=130)

    assert record.beta == 1.0
    assert "미조정" in record.reference
    assert record.excess_pct == pytest.approx(record.realised_pct - record.benchmark_pct)


def test_is_krx_equity_does_not_catch_won_quoted_crypto():
    assert is_krx_equity(SAMSUNG)
    assert not is_krx_equity(KODEX_200)                       # an ETF is not a peer
    assert not is_krx_equity(Symbol("BTC/KRW", venue="upbit",
                                    asset_class=AssetClass.CRYPTO, quote_currency="KRW"))
    assert not is_krx_equity(NAME)
