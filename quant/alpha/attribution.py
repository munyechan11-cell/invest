"""인사이트 채점과 알파 귀속 — which model is actually paying.

The engine measures the *portfolio* exhaustively — Sharpe, deflated Sharpe,
PSR, walk-forward efficiency — and until now measured the *alpha models* not at
all. That gap has a specific cost: when a composite strategy loses money you
cannot tell whether one model is bleeding and three are fine, or all four are
mediocre. Those call for opposite responses, and without attribution you are
guessing.

Borrowed from LEAN's insight scoring, with two deliberate differences:

* **Scored against the benchmark where there is one.** In a rising tape every
  long call looks right. Grading raw direction produces a hit rate that
  measures the market, not the model.
* **The scorer never sees an insight's own future.** An insight is queued when
  emitted and settled only once its period has fully elapsed, using prices the
  engine had already recorded. The ledger cannot flatter a model by peeking.

A model with a 45% hit rate and a positive expectancy is doing its job; one
with a 70% hit rate and a negative expectancy is quietly selling volatility.
Both numbers are here because either alone misleads.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from quant.core.context import Context
from quant.core.types import Direction, Insight, Symbol

log = logging.getLogger("quant.attribution")


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
    tag: str = ""

    def to_dict(self) -> dict:
        return {
            "source": self.source, "symbol": self.ticker,
            "direction": self.direction, "confidence": round(self.confidence, 3),
            "generated_at": self.generated_at.isoformat(),
            "realised_pct": round(self.realised_pct, 4),
            "benchmark_pct": round(self.benchmark_pct, 4),
            "excess_pct": round(self.excess_pct, 4),
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
            "information_ratio": round(self.information_ratio, 4),
            "avg_confidence": round(
                statistics.fmean(self.confidences), 4) if self.confidences else 0.0,
            "calibration_gap": round(self.calibration, 4),
        }

    def verdict(self) -> str:
        if self.scored < 20:
            return f"표본 {self.scored}건 — 판단하기에 부족"
        if self.excess_expectancy <= 0:
            return "벤치마크 대비 기여 없음 — 제거 후보"
        if self.calibration > 0.2:
            return "과신 — 확신도가 실제 적중률을 크게 웃돎"
        if self.information_ratio > 0.15:
            return "기여 중"
        return "미미한 기여"


class InsightLedger:
    """Queues every insight and settles it once its horizon has elapsed."""

    def __init__(self, benchmark: Symbol | None = None, max_pending: int = 5000,
                 max_scored: int = 20_000):
        self.benchmark = benchmark
        self.max_pending = max_pending
        self.max_scored = max_scored
        self._pending: list[tuple[Insight, float, float]] = []   # insight, entry, bench
        self.scored: list[ScoredInsight] = []
        self.sources: dict[str, SourceScore] = {}

    def record(self, ctx: Context, insights: list[Insight]) -> None:
        for ins in insights:
            if ins.direction is Direction.FLAT:
                continue                      # a veto makes no directional claim
            price = ctx.price(ins.symbol)
            if price <= 0:
                continue
            bench = ctx.price(self.benchmark) if self.benchmark is not None else 0.0
            self._pending.append((ins, price, bench))
        if len(self._pending) > self.max_pending:
            dropped = len(self._pending) - self.max_pending
            del self._pending[:dropped]
            log.debug("insight ledger dropped %d unsettled entries (cap)", dropped)

    def settle(self, ctx: Context) -> list[ScoredInsight]:
        """Score every queued insight whose period has fully elapsed."""
        now = ctx.now
        settled: list[ScoredInsight] = []
        still_open: list[tuple[Insight, float, float]] = []

        for ins, entry, bench_entry in self._pending:
            if now < ins.close_time:
                still_open.append((ins, entry, bench_entry))
                continue
            exit_price = ctx.price(ins.symbol)
            if exit_price <= 0 or entry <= 0:
                continue
            realised = (exit_price / entry - 1.0) * int(ins.direction)

            bench_move = 0.0
            if self.benchmark is not None and bench_entry > 0:
                bench_now = ctx.price(self.benchmark)
                if bench_now > 0:
                    bench_move = (bench_now / bench_entry - 1.0) * int(ins.direction)

            record = ScoredInsight(
                insight_id=ins.id, source=ins.source, ticker=ins.symbol.ticker,
                direction=int(ins.direction), confidence=ins.confidence,
                magnitude=ins.magnitude, generated_at=ins.generated_at,
                settled_at=now, entry_price=entry, exit_price=exit_price,
                realised_pct=realised, benchmark_pct=bench_move,
                excess_pct=realised - bench_move,
                correct=(realised - bench_move) > 0, tag=ins.tag,
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

        self._pending = still_open
        if len(self.scored) > self.max_scored:
            del self.scored[: len(self.scored) // 2]
        return settled

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
               f"{'초과':>8} {'IR':>6}  판정"]
        for s in rows:
            out.append(
                f"  {s.source:<{width}}  {s.scored:>5} {s.hit_rate:>5.1%} "
                f"{s.expectancy * 100:>+7.2f}% {s.excess_expectancy * 100:>+7.2f}% "
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
