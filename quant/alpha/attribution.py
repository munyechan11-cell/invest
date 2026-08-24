"""인사이트 채점과 알파 귀속 — which model is actually paying.

The engine measures the *portfolio* exhaustively — Sharpe, deflated Sharpe,
PSR, walk-forward efficiency — and until now measured the *alpha models* not at
all. That gap has a specific cost: when a composite strategy loses money you
cannot tell whether one model is bleeding and three are fine, or all four are
mediocre. Those call for opposite responses, and without attribution you are
guessing.

Borrowed from LEAN's insight scoring, with three deliberate differences:

* **Scored against the benchmark where there is one.** In a rising tape every
  long call looks right. Grading raw direction produces a hit rate that
  measures the market, not the model.
* **Scored against the benchmark *times beta*.** Subtracting the market move
  one-for-one prices every name as if it had a beta of one. It does not: a book
  that mixes 삼성전자 with a KOSDAQ 바이오 name over-credits the high-beta
  picks in a rising tape and over-punishes them in a falling one, so the
  ledger's verdict tracks the beta composition of what a model happens to pick
  rather than its skill. That bias does not wash out with sample size — it is
  the ranking itself that is wrong, and more data makes it more confidently
  wrong. Excess is therefore `realised − beta · reference`, with beta from a
  rolling market-model regression estimated on data available *before* the call
  was made, shrunk toward one (Vasicek).
* **The scorer never sees an insight's own future.** An insight is queued when
  emitted and settled only once its period has fully elapsed, using prices the
  engine had already recorded. The ledger cannot flatter a model by peeking.

On KRX the reference is not the configured index. 삼성전자 and SK하이닉스 are
together over half of KOSPI 200, so regressing either against a cap-weighted
index ETF regresses a thing on itself and calls the leftovers alpha. Korean
names are measured against an equal-weighted, leave-one-out basket of their
peers in the same universe instead; where too few peers exist to form one, the
ledger says so and leaves beta at one rather than inventing a number.

A model with a 45% hit rate and a positive expectancy is doing its job; one
with a 70% hit rate and a negative expectancy is quietly selling volatility.
Both numbers are here because either alone misleads.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime

from quant.core.context import Context
from quant.core.types import AssetClass, Bar, Direction, Insight, Symbol
from quant.indicators.streaming import KRX_PRICE_LIMIT, MarketModel, is_limit_move

log = logging.getLogger("quant.attribution")

#: Regression window for the beta behind the excess column, in bars.
BETA_WINDOW = 60

#: Bars are pulled from twice that span. Unaligned and limit-hit observations
#: are dropped rather than filled, so the window has to be allowed to fill
#: itself from further back — otherwise one 상한가 disables the adjustment for
#: a whole quarter.
BETA_LOOKBACK = 2

#: An equal-weighted basket with a name left out of it still has to be a
#: market. Below this many peers the KRX reference is not formed at all.
MIN_KRX_PEERS = 5


def is_krx_equity(symbol: Symbol) -> bool:
    """A 원화 주식 — the instrument the ±30% limit and the index concentration
    problem both apply to. KRW-quoted crypto is neither."""
    return symbol.asset_class is AssetClass.EQUITY and symbol.quote_currency.upper() == "KRW"


def _price(ctx: Context, symbol: Symbol, cache: dict[str, float]) -> float:
    """`ctx.price` scans the symbol's whole bar buffer, and a peer basket asks
    for every name in the universe once per insight. Price each name once per
    batch instead — within one batch the answer cannot change anyway."""
    price = cache.get(symbol.key)
    if price is None:
        price = cache[symbol.key] = ctx.price(symbol)
    return price


def _interval_returns(bars: list[Bar]) -> dict[tuple[datetime, datetime], float]:
    """Simple returns keyed by the interval each one spans.

    Keying by (previous close, this close) rather than by position is what lets
    several series be pooled without ever averaging a two-day return in with
    one-day ones: a name that missed a bar simply shares no key there.
    """
    out: dict[tuple[datetime, datetime], float] = {}
    for prev, bar in zip(bars, bars[1:]):
        if prev.close > 0 and bar.close > 0:
            out[(prev.end_ts, bar.end_ts)] = bar.close / prev.close - 1.0
    return out


def _aligned_returns(target: list[Bar], levels: dict[datetime, float]
                     ) -> list[tuple[float, float]]:
    """(target, reference) return pairs over identical intervals, oldest first.

    Only timestamps both legs priced survive, and each return is measured from
    the previous *surviving* timestamp, so the two legs always cover the same
    span. Nothing is forward-filled: a stale price contributes a zero return
    against a live market and drags beta toward zero on exactly the thin names
    whose beta matters most.
    """
    common = [(b.end_ts, b.close) for b in target if b.close > 0 and b.end_ts in levels]
    pairs: list[tuple[float, float]] = []
    for (prev_ts, prev_close), (ts, close) in zip(common, common[1:]):
        prev_ref, ref = levels[prev_ts], levels[ts]
        if prev_ref > 0:
            pairs.append((close / prev_close - 1.0, ref / prev_ref - 1.0))
    return pairs


@dataclass
class ScoredInsight:
    insight_id: str
    source: str
    ticker: str
    direction: int
    confidence: float
    magnitude: float | None
    generated_at: datetime
    settled_at: datetime
    entry_price: float
    exit_price: float
    realised_pct: float
    benchmark_pct: float
    excess_pct: float
    correct: bool
    beta: float = 1.0
    reference: str = ""
    tag: str = ""

    def to_dict(self) -> dict:
        return {
            "source": self.source, "symbol": self.ticker,
            "direction": self.direction, "confidence": round(self.confidence, 3),
            "generated_at": self.generated_at.isoformat(),
            "realised_pct": round(self.realised_pct, 4),
            "benchmark_pct": round(self.benchmark_pct, 4),
            "excess_pct": round(self.excess_pct, 4),
            "beta": round(self.beta, 3), "reference": self.reference,
            "correct": self.correct, "tag": self.tag[:120],
        }


@dataclass
class SourceScore:
    """What one alpha model's calls were worth."""

    source: str
    scored: int = 0
    correct: int = 0
    realised: list[float] = field(default_factory=list)
    excess: list[float] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)
    correctness: list[bool] = field(default_factory=list)
    betas: list[float] = field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        return self.correct / self.scored if self.scored else 0.0

    @property
    def expectancy(self) -> float:
        """Mean directional return per call — the number that actually matters."""
        return statistics.fmean(self.realised) if self.realised else 0.0

    @property
    def excess_expectancy(self) -> float:
        return statistics.fmean(self.excess) if self.excess else 0.0

    @property
    def avg_beta(self) -> float:
        """Market exposure the model was taking on, averaged over its calls.

        Two models with the same excess expectancy are not equally good if one
        of them got there at beta 1.6 — this is the column that says so.
        """
        return statistics.fmean(self.betas) if self.betas else 1.0

    @property
    def information_ratio(self) -> float:
        """Excess return per unit of its own dispersion."""
        if len(self.excess) < 3:
            return 0.0
        sd = statistics.pstdev(self.excess)
        return self.excess_expectancy / sd if sd > 1e-12 else 0.0

    @property
    def calibration(self) -> float:
        """Stated confidence minus realised hit rate.

        Near zero means the model's confidence can be trusted by the sizing
        layer. Strongly positive means it is overconfident and every position
        it drives is too large — a failure that looks like bad luck.
        """
        if not self.confidences:
            return 0.0
        return statistics.fmean(self.confidences) - self.hit_rate

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "scored": self.scored,
            "hit_rate": round(self.hit_rate, 4),
            "expectancy_pct": round(self.expectancy * 100, 4),
            "excess_expectancy_pct": round(self.excess_expectancy * 100, 4),
            "avg_beta": round(self.avg_beta, 3),
            "information_ratio": round(self.information_ratio, 4),
            "avg_confidence": round(
                statistics.fmean(self.confidences), 4) if self.confidences else 0.0,
            "calibration_gap": round(self.calibration, 4),
        }

    def verdict(self) -> str:
        if self.scored < 20:
            return f"표본 {self.scored}건 — 판단하기에 부족"
        if self.excess_expectancy <= 0:
            return "시장 위험(β) 조정 후 기여 없음 — 제거 후보"
        if self.calibration > 0.2:
            return "과신 — 확신도가 실제 적중률을 크게 웃돎"
        if self.information_ratio > 0.15:
            return "기여 중"
        return "미미한 기여"


@dataclass
class _Pending:
    """One queued insight and everything it will be graded against."""

    insight: Insight
    entry: float
    #: Equal-weighted reference basket — one leg for a plain benchmark, many
    #: for the KRX peer basket. Entry prices are taken when the call was made.
    legs: list[tuple[Symbol, float]]
    #: Estimated on data that existed *before* the call. Using the window that
    #: contains the holding period would let the same bars set the adjustment
    #: and the thing being adjusted.
    beta: float
    reference: str


class InsightLedger:
    """Queues every insight and settles it once its horizon has elapsed."""

    def __init__(self, benchmark: Symbol | None = None, max_pending: int = 5000,
                 max_scored: int = 20_000, beta_window: int = BETA_WINDOW,
                 min_peers: int = MIN_KRX_PEERS):
        self.benchmark = benchmark
        self.max_pending = max_pending
        self.max_scored = max_scored
        self.beta_window = beta_window
        self.min_peers = min_peers
        self._pending: list[_Pending] = []
        self.scored: list[ScoredInsight] = []
        self.sources: dict[str, SourceScore] = {}
        self._peers: tuple[datetime, dict[str, dict], dict[tuple, list]] | None = None

    def record(self, ctx: Context, insights: list[Insight]) -> None:
        # One stamp for the whole batch: under a real clock `ctx.now` moves
        # between insights, and keying the regression cache off it would
        # re-estimate the same beta once per model that named the same symbol.
        stamp = ctx.now
        prices: dict[str, float] = {}
        by_symbol: dict[str, tuple[list[tuple[Symbol, float]], float, str]] = {}
        for ins in insights:
            if ins.direction is Direction.FLAT:
                continue                      # a veto makes no directional claim
            price = _price(ctx, ins.symbol, prices)
            if price <= 0:
                continue
            reference = by_symbol.get(ins.symbol.key)
            if reference is None:
                reference = by_symbol[ins.symbol.key] = self._reference(
                    ctx, ins.symbol, stamp, prices)
            legs, beta, label = reference
            self._pending.append(_Pending(ins, price, legs, beta, label))
        if len(self._pending) > self.max_pending:
            dropped = len(self._pending) - self.max_pending
            del self._pending[:dropped]
            log.debug("insight ledger dropped %d unsettled entries (cap)", dropped)

    def settle(self, ctx: Context) -> list[ScoredInsight]:
        """Score every queued insight whose period has fully elapsed."""
        now = ctx.now
        prices: dict[str, float] = {}
        settled: list[ScoredInsight] = []
        still_open: list[_Pending] = []

        for item in self._pending:
            ins, entry = item.insight, item.entry
            if now < ins.close_time:
                still_open.append(item)
                continue
            exit_price = _price(ctx, ins.symbol, prices)
            if exit_price <= 0 or entry <= 0:
                continue
            realised = (exit_price / entry - 1.0) * int(ins.direction)

            bench_move = self._reference_move(ctx, item.legs, prices) * int(ins.direction)
            # The market leg is what the position was *exposed* to, so it is the
            # leg beta scales. The regression's intercept is deliberately not
            # subtracted as well: alpha over the estimation window is the name's
            # own past drift, and removing it would charge a model for picking a
            # name that keeps working. With no reference `bench_move` is 0.0 and
            # excess is the raw return, which is what an unbenchmarked run says.
            excess = realised - item.beta * bench_move

            record = ScoredInsight(
                insight_id=ins.id, source=ins.source, ticker=ins.symbol.ticker,
                direction=int(ins.direction), confidence=ins.confidence,
                magnitude=ins.magnitude, generated_at=ins.generated_at,
                settled_at=now, entry_price=entry, exit_price=exit_price,
                realised_pct=realised, benchmark_pct=bench_move,
                excess_pct=excess, correct=excess > 0,
                beta=item.beta, reference=item.reference, tag=ins.tag,
            )
            settled.append(record)
            self.scored.append(record)

            score = self.sources.setdefault(ins.source, SourceScore(ins.source))
            score.scored += 1
            score.correct += int(record.correct)
            score.realised.append(realised)
            score.excess.append(record.excess_pct)
            score.confidences.append(ins.confidence)
            score.correctness.append(record.correct)
            score.betas.append(item.beta)

        self._pending = still_open
        if len(self.scored) > self.max_scored:
            del self.scored[: len(self.scored) // 2]
        return settled

    # ── what a call is measured against ──────────────────────────────────
    def _reference(self, ctx: Context, symbol: Symbol, stamp: datetime,
                   prices: dict[str, float]
                   ) -> tuple[list[tuple[Symbol, float]], float, str]:
        """Pick the reference series for one call and estimate its beta on it.

        Returning beta = 1.0 is the honest fallback, not a default: it says
        "not measured", and it reproduces the plain difference the ledger used
        to report, so an unmeasurable name is never silently re-scored.
        """
        if self.benchmark is None:
            return [], 1.0, ""     # no benchmark: excess is the raw return, as reported

        if is_krx_equity(symbol):
            legs = [(s, _price(ctx, s, prices)) for s in ctx.universe
                    if is_krx_equity(s) and s.key != symbol.key]
            legs = [(s, price) for s, price in legs if price > 0]
            if len(legs) >= self.min_peers:
                levels = self._krx_levels(ctx, symbol, stamp)
                return legs, self._beta(ctx, symbol, levels, KRX_PRICE_LIMIT), \
                    f"동일가중 국내 {len(legs)}종목"
            # Too few peers to leave one out, and the index is not a way out:
            # 삼성전자 and SK하이닉스 are over half of KOSPI 200, so a large cap
            # regressed on a cap-weighted index ETF is regressed on itself.
            price = _price(ctx, self.benchmark, prices)
            legs = [(self.benchmark, price)] if price > 0 else []
            return legs, 1.0, f"{self.benchmark.ticker} (β 미조정)" if legs else ""

        price = _price(ctx, self.benchmark, prices)
        if price <= 0:
            return [], 1.0, ""
        bars = ctx.history(self.benchmark, self.beta_window * BETA_LOOKBACK)
        levels = {b.end_ts: b.close for b in bars if b.close > 0}
        return [(self.benchmark, price)], self._beta(ctx, symbol, levels, None), \
            self.benchmark.ticker

    def _beta(self, ctx: Context, symbol: Symbol, levels: dict[datetime, float],
              price_limit: float | None) -> float:
        model = MarketModel(period=self.beta_window, price_limit=price_limit)
        history = ctx.history(symbol, self.beta_window * BETA_LOOKBACK)
        for target_ret, reference_ret in _aligned_returns(history, levels):
            model.observe(target_ret, reference_ret)
        return model.beta if model.is_ready else model.prior_beta

    def _krx_levels(self, ctx: Context, symbol: Symbol, stamp: datetime
                    ) -> dict[datetime, float]:
        """The equal-weighted, leave-one-out KRX reference as a level series.

        Equal weighting is what stops two names from *being* the factor;
        leaving the target out is what stops it from being regressed on itself.
        Limit-hit bars never enter the average — a censored return would drag
        the whole basket's move toward zero on the day it matters.
        """
        per_name, totals = self._peer_returns(ctx, stamp)
        own = per_name.get(symbol.key, {})
        levels: dict[datetime, float] = {}
        level, previous_end = 1.0, None

        for (start, end), (total, count) in sorted(totals.items()):
            mine = own.get((start, end))
            if mine is not None:
                total, count = total - mine, count - 1
            if count < self.min_peers:
                continue
            if previous_end is not None and start != previous_end:
                # A gap no basket priced. Restart the chain rather than let a
                # ratio be taken across a stretch the reference never covered.
                levels.clear()
                level = 1.0
            if not levels:
                levels[start] = level
            level *= 1.0 + total / count
            levels[end] = level
            previous_end = end
        return levels

    def _peer_returns(self, ctx: Context, stamp: datetime
                      ) -> tuple[dict[str, dict], dict[tuple, list]]:
        """Per-name interval returns and their cross-sectional totals.

        Cached per batch: leave-one-out is a subtraction from these totals, not
        a rebuild per name, or the ledger would be quadratic in the universe.
        """
        if self._peers is not None and self._peers[0] == stamp:
            return self._peers[1], self._peers[2]

        per_name: dict[str, dict] = {}
        totals: dict[tuple, list] = {}
        for sym in ctx.universe:
            if not is_krx_equity(sym):
                continue
            history = ctx.history(sym, self.beta_window * BETA_LOOKBACK)
            returns = {span: r for span, r in _interval_returns(history).items()
                       if not is_limit_move(r)}
            per_name[sym.key] = returns
            for span, r in returns.items():
                slot = totals.setdefault(span, [0.0, 0])
                slot[0] += r
                slot[1] += 1
        self._peers = (stamp, per_name, totals)
        return per_name, totals

    def _reference_move(self, ctx: Context, legs: list[tuple[Symbol, float]],
                        prices: dict[str, float]) -> float:
        """Equal-weighted return of the reference over the holding period."""
        moves = []
        for symbol, entry in legs:
            price = _price(ctx, symbol, prices)
            if entry > 0 and price > 0:
                moves.append(price / entry - 1.0)
        return statistics.fmean(moves) if moves else 0.0

    # ── reporting ────────────────────────────────────────────────────────
    def report(self) -> dict:
        return {
            "benchmark": self.benchmark.ticker if self.benchmark else None,
            "pending": len(self._pending),
            "scored": sum(s.scored for s in self.sources.values()),
            "by_source": {
                name: {**score.to_dict(), "verdict": score.verdict()}
                for name, score in sorted(
                    self.sources.items(),
                    key=lambda kv: kv[1].excess_expectancy, reverse=True)
            },
        }

    def summary_lines(self) -> list[str]:
        if not self.sources:
            return ["  (채점된 인사이트 없음 — 보유기간이 아직 끝나지 않았을 수 있음)"]
        rows = sorted(self.sources.values(), key=lambda s: s.excess_expectancy,
                      reverse=True)
        width = max(len(s.source) for s in rows)
        out = [f"  {'alpha':<{width}}  {'n':>5} {'적중':>6} {'기대값':>8} "
               f"{'β':>5} {'초과':>8} {'IR':>6}  판정"]
        for s in rows:
            out.append(
                f"  {s.source:<{width}}  {s.scored:>5} {s.hit_rate:>5.1%} "
                f"{s.expectancy * 100:>+7.2f}% {s.avg_beta:>5.2f} "
                f"{s.excess_expectancy * 100:>+7.2f}% "
                f"{s.information_ratio:>6.2f}  {s.verdict()}"
            )
        return out

    @property
    def worst_source(self) -> str | None:
        judged = [s for s in self.sources.values() if s.scored >= 20]
        if not judged:
            return None
        worst = min(judged, key=lambda s: s.excess_expectancy)
        return worst.source if worst.excess_expectancy < 0 else None
