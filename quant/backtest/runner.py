"""Backtest driver.

Loads history once, merges every symbol into a single chronologically ordered
stream of bar batches, and feeds the engine. The warm-up window is loaded
*before* the test window and pushed through `seed_history` rather than through
the pipeline, so indicators start warm without the strategy being credited (or
blamed) for trades in a period it should not have traded.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from quant.backtest.metrics import PerformanceReport, analyze, monthly_returns
from quant.config.schema import StrategyConfig
from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.engine import Engine, _trade_dict
from quant.core.events import EventBus
from quant.core.types import UTC, Bar, RunMode, Symbol, timeframe_delta
from quant.data.flow import NullFlowProvider
from quant.data.provider import DataProvider, gather_history
from quant.strategy.builder import build_engine

log = logging.getLogger("quant.backtest")


@dataclass
class BacktestResult:
    config_name: str
    report: PerformanceReport
    equity_curve: list[tuple[str, float]]
    monthly: dict[str, float]
    trades: list[dict]
    engine_summary: dict
    bars: int = 0
    elapsed_s: float = 0.0
    warnings: list[str] = field(default_factory=list)
    attribution_lines: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "config": self.config_name,
            "report": self.report.to_dict(),
            "equity_curve": self.equity_curve,
            "monthly_returns": self.monthly,
            "trades": self.trades,
            "engine": self.engine_summary,
            "bars": self.bars,
            "elapsed_s": round(self.elapsed_s, 2),
            "warnings": self.warnings,
        }

    def print_summary(self) -> None:
        print(f"\n{'=' * 68}\n  {self.config_name}\n{'=' * 68}")
        for line in self.report.summary_lines():
            print(f"  {line}")
        if self.attribution_lines:
            print("\n  알파별 기여도")
            for line in self.attribution_lines:
                print(line)
        if self.warnings:
            print("\n  Warnings:")
            for w in self.warnings:
                print(f"    ! {w}")
        print(f"{'=' * 68}\n")


def merge_bars(series: dict[str, list[Bar]]) -> list[tuple[datetime, dict[str, Bar]]]:
    """Group bars from many symbols into per-timestamp batches.

    Batching (rather than interleaving one symbol at a time) is what allows
    cross-sectional models to see the whole universe at each point in time.
    """
    buckets: dict[datetime, dict[str, Bar]] = {}
    for key, bars in series.items():
        for bar in bars:
            buckets.setdefault(bar.ts, {})[key] = bar
    return [(ts, buckets[ts]) for ts in sorted(buckets)]


async def run_backtest(
    config: StrategyConfig,
    provider: DataProvider | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> BacktestResult:
    started = time.monotonic()
    warnings: list[str] = []

    end = config.backtest.end or datetime.now(UTC)
    start = config.backtest.start or (end - timedelta(days=730))
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    if start >= end:
        raise ValueError(f"backtest start {start} is not before end {end}")

    warmup_start = start - config.warmup_delta
    portfolio = Portfolio(config.portfolio.starting_cash, config.portfolio.base_currency)
    bus = EventBus()
    engine, built_provider = build_engine(
        config, clock=SimClock(warmup_start), bus=bus, portfolio=portfolio
    )
    provider = provider or built_provider
    ctx = engine.ctx

    symbols: list[Symbol] = list(ctx.universe)
    if not symbols:
        raise ValueError("universe is empty — nothing to backtest")

    log.info("loading %d symbols from %s (%s → %s, warm-up from %s)",
             len(symbols), provider.name, start.date(), end.date(), warmup_start.date())
    series = await gather_history(provider, symbols, config.data.timeframe, warmup_start, end)

    tradable: list[Symbol] = []
    warm_counts: dict[str, int] = {}
    for sym in symbols:
        bars = series.get(sym.key, [])
        in_window = [b for b in bars if b.ts >= start]
        if len(in_window) < 2:
            warnings.append(f"{sym.ticker}: only {len(in_window)} bars in the test window — dropped")
            continue
        warmup_bars = [b for b in bars if b.ts < start]
        tradable.append(sym)
        warm_counts[sym.ticker] = len(warmup_bars)
        ctx.seed_history(sym, warmup_bars)
    if not tradable:
        raise ValueError("no symbol had usable data in the requested window")
    engine.set_universe(tradable)

    flow_feed = getattr(engine, "flow_feed", None)
    if flow_feed is not None and not isinstance(flow_feed.provider, NullFlowProvider):
        await flow_feed.backfill(tradable, warmup_start, end)
        if not flow_feed.has_data:
            warnings.append(
                "flow provider returned no 수급 data — flow-based models will stay silent"
            )

    # Counted from the raw series, not ctx.history() — the sim clock still sits
    # at warmup_start here, so history() would correctly report nothing yet.
    thin = [t for t, n in warm_counts.items() if n < config.data.warmup_bars * 0.5]
    if thin:
        warnings.append(
            f"thin warm-up for {', '.join(thin[:6])}"
            f"{'…' if len(thin) > 6 else ''} — early signals may be unreliable"
        )

    stream = merge_bars({
        s.key: [b for b in series.get(s.key, []) if start <= b.ts < end] for s in tradable
    })
    if not stream:
        raise ValueError("no bars inside the backtest window")

    selector = getattr(engine, "universe_selector", None)
    dynamic = bool(selector and selector.filters and
                   any(f.name != "held" for f in selector.filters))
    if dynamic:
        log.info("dynamic universe: %d candidates, refresh every %d bars",
                 len(tradable), selector.refresh_every)

    await engine.start()
    total = len(stream)
    for i, (_, batch) in enumerate(stream):
        # Selection runs before the bars are processed, so the models see the
        # universe that was decided from data available *before* this bar.
        if dynamic and selector.due():
            engine.set_universe(await selector.select(ctx, provider))
        await engine.on_bars(batch)
        if on_progress and (i % 200 == 0 or i == total - 1):
            on_progress(i + 1, total)

    # Mark-to-market close: value the book at the final bar rather than leaving
    # open positions unpriced, which would flatter or punish the result at random.
    last_ts = stream[-1][0] + timeframe_delta(config.data.timeframe)
    if hasattr(ctx.clock, "set"):
        ctx.clock.set(last_ts)
    portfolio.record_equity(last_ts)
    await engine.stop()

    report = analyze(portfolio, config.data.timeframe,
                     config.backtest.risk_free_rate, config.backtest.trials)
    if report.trades == 0:
        warnings.append("zero closed trades — check warm-up length and signal thresholds")
    if report.turnover > 50:
        warnings.append(
            f"turnover {report.turnover:.0f}x/yr is very high; results are dominated "
            "by the cost model, not by the signal"
        )
    if config.costs.preset == "zero_cost":
        warnings.append("zero-cost preset in use — this result is not achievable")

    engine_summary = engine.summary()
    attribution_lines = engine.ledger.summary_lines()
    worst = engine.ledger.worst_source
    if worst:
        warnings.append(
            f"alpha '{worst}' has a negative benchmark-excess expectancy — "
            f"the composite may be better off without it"
        )
    if selector is not None and selector.last_report is not None:
        engine_summary["universe"] = selector.last_report.to_dict()
        if not selector.last_report.selected:
            warnings.append("universe selection ended with zero symbols — "
                            "check the filter chain")

    return BacktestResult(
        config_name=config.name,
        report=report,
        equity_curve=[(p.ts.isoformat(), round(p.equity, 2))
                      for p in portfolio.equity_curve],
        monthly=monthly_returns(portfolio),
        trades=[_trade_dict(t) for t in portfolio.closed_trades],
        engine_summary=engine_summary,
        bars=total,
        elapsed_s=time.monotonic() - started,
        warnings=warnings,
        attribution_lines=attribution_lines,
    )
