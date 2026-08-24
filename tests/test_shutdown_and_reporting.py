"""Two things that used to quietly do nothing: stopping, and reporting.

A stop signal that only sets a flag leaves the loop asleep until the next
candle — up to a full day on 1d — so every `docker stop` is really a SIGKILL
mid-cycle. And a report whose '초과'(excess) column is measured against a
benchmark that was never loaded is not an excess column at all.
"""
import asyncio
import os
import signal
import sqlite3
import time
from datetime import datetime, timedelta

import pytest

from quant.backtest.runner import run_backtest
from quant.config.schema import (
    BacktestConfig,
    BrokerConfig,
    CostConfig,
    DataConfig,
    ExecutionConfig,
    FlowConfig,
    ModelSpec,
    PortfolioConfig,
    RiskConfig,
    StrategyConfig,
    SymbolSpec,
    UniverseConfig,
)
from quant.core.clock import next_candle_close
from quant.core.types import UTC, RunMode
from quant.data.providers.local import SyntheticProvider
from quant.live.trader import LiveTrader

TICKERS = ["AAA", "BBB", "CCC"]


def backtest_config(**overrides) -> StrategyConfig:
    cfg = StrategyConfig(
        name="lifecycle-test",
        mode=RunMode.BACKTEST,
        data=DataConfig(provider="synthetic", params={"seed": 3}, timeframe="1d",
                        warmup_bars=60, cache=False),
        universe=UniverseConfig(
            symbols=[SymbolSpec(ticker=t, venue="SIM") for t in TICKERS]),
        alpha=[ModelSpec(type="ema_cross"), ModelSpec(type="donchian_breakout")],
        portfolio=PortfolioConfig(starting_cash=100_000, max_gross_leverage=1.0,
                                  cash_reserve_pct=0.0, min_trade_weight=0.0),
        risk=RiskConfig(),
        execution=ExecutionConfig(min_order_notional=1.0),
        costs=CostConfig(preset="zero_cost"),
        broker=BrokerConfig(type="paper"),
        backtest=BacktestConfig(
            start=datetime(2022, 1, 1, tzinfo=UTC), end=datetime(2023, 1, 1, tzinfo=UTC)
        ),
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def live_config() -> StrategyConfig:
    cfg = backtest_config()
    cfg.mode = RunMode.DRY_RUN
    cfg.data.warmup_bars = 60
    return cfg


def make_trader(tmp_path, name="s.db") -> LiveTrader:
    return LiveTrader(live_config(), state_path=str(tmp_path / name))


def stopped_at(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("select stopped_at from runs order by id desc").fetchone()[0]
    finally:
        conn.close()


# ── (a) a stop must interrupt the wait, not queue behind it ───────────────
def test_sleep_returns_the_moment_a_stop_is_requested(tmp_path):
    """The bug: `await asyncio.sleep(sleep_for)` cannot be interrupted."""
    trader = make_trader(tmp_path)
    trader.running = True

    async def scenario():
        started = time.monotonic()
        task = asyncio.ensure_future(trader._sleep(50_000))   # ~13.9h, as measured
        await asyncio.sleep(0.05)
        trader.request_stop()
        elapsed_fully = await asyncio.wait_for(task, timeout=5)
        return elapsed_fully, time.monotonic() - started

    elapsed_fully, waited = asyncio.run(scenario())
    assert elapsed_fully is False        # interrupted, not slept through
    assert waited < 2


def test_sleep_also_wakes_when_only_the_running_flag_is_cleared(tmp_path):
    """`POST /api/trader/stop` clears the flag and nothing else."""
    trader = make_trader(tmp_path)
    trader.running = True
    trader.stop_poll_s = 0.05

    async def scenario():
        started = time.monotonic()
        task = asyncio.ensure_future(trader._sleep(50_000))
        await asyncio.sleep(0.05)
        trader.running = False
        await asyncio.wait_for(task, timeout=5)
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 2


def test_sleep_that_is_never_interrupted_reports_that_it_elapsed(tmp_path):
    trader = make_trader(tmp_path)
    trader.running = True
    assert asyncio.run(trader._sleep(0.05)) is True


def test_sigterm_stops_the_loop_and_closes_the_run(tmp_path):
    """The operator's repro: SIGTERM, then `docker stop`'s 10 second grace."""
    remaining = (next_candle_close(datetime.now(UTC), "1d", lag=3.0)
                 - datetime.now(UTC)).total_seconds()
    if remaining < 60:
        pytest.skip("within a minute of the daily candle close — no wait to interrupt")

    db = tmp_path / "sigterm.db"
    trader = LiveTrader(live_config(), state_path=str(db))

    async def scenario():
        task = asyncio.ensure_future(trader.run())
        trader.install_signal_handlers()
        for _ in range(200):                       # let warm-up finish
            await asyncio.sleep(0.05)
            if trader.running and trader.last_bar_ts is not None:
                break
        assert trader.running, "trader never started"
        started = time.monotonic()
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, timeout=20)
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 10            # docker's grace period
    assert not trader.running
    assert stopped_at(db) is not None, "run left open — the state flush was skipped"


def test_a_stop_during_warmup_is_not_forgotten(tmp_path):
    """start() sets running = True, which used to undo a stop asked for first."""
    trader = make_trader(tmp_path, "warmup.db")
    ticks = []

    async def _tick():
        ticks.append(1)

    trader._tick = _tick

    async def scenario():
        task = asyncio.ensure_future(trader.run())
        await asyncio.sleep(0)                     # run() has only reached warm-up
        trader.request_stop()
        await asyncio.wait_for(task, timeout=20)

    asyncio.run(scenario())
    assert ticks == []
    assert stopped_at(tmp_path / "warmup.db") is not None


def test_shutdown_is_idempotent(tmp_path):
    """run()'s finally and an external stop must not both flush a closed store."""
    trader = make_trader(tmp_path, "twice.db")

    async def scenario():
        await trader.start()
        await trader.shutdown()
        await trader.shutdown()                    # would hit a closed sqlite handle

    asyncio.run(scenario())
    assert stopped_at(tmp_path / "twice.db") is not None


def test_status_reports_stopping_before_the_flush_finishes(tmp_path):
    trader = make_trader(tmp_path, "status.db")

    async def scenario():
        await trader.start()
        trader.request_stop()
        mid = trader.status()
        await trader.shutdown()
        return mid, trader.status()

    mid, after = asyncio.run(scenario())
    assert mid["running"] is False and mid["stopping"] is True
    assert after["stopping"] is False


# ── (b) universe.benchmark must be loaded, or said not to be ─────────────
def _attribution(result) -> dict:
    return result.engine_summary["attribution"]


def test_excess_is_measured_against_the_benchmark_not_zero():
    plain = asyncio.run(run_backtest(backtest_config()))
    cfg = backtest_config()
    cfg.universe.benchmark = "ZZZ"
    with_bench = asyncio.run(run_backtest(cfg))

    flat = _attribution(plain)["by_source"]
    assert flat, "no scored insights — the test config stopped producing signals"
    for name, score in flat.items():
        assert score["excess_expectancy_pct"] == score["expectancy_pct"], name

    scored = _attribution(with_bench)["by_source"]
    assert _attribution(with_bench)["benchmark"] == "ZZZ"
    assert any(s["excess_expectancy_pct"] != s["expectancy_pct"] for s in scored.values())


def test_the_benchmark_is_priced_but_never_traded():
    """The only workaround — putting it in `symbols` — traded it 21 times."""
    plain = asyncio.run(run_backtest(backtest_config()))
    cfg = backtest_config()
    cfg.universe.benchmark = "ZZZ"
    with_bench = asyncio.run(run_backtest(cfg))

    assert not any(t["symbol"] == "ZZZ" for t in with_bench.trades)
    # Identical books: pricing the benchmark must not perturb the strategy.
    assert [t["symbol"] for t in with_bench.trades] == [t["symbol"] for t in plain.trades]
    assert with_bench.report.ending_equity == pytest.approx(plain.report.ending_equity)


class _Benchmarkless(SyntheticProvider):
    """Prices everything except the benchmark, like a venue that has no SPY."""

    async def history(self, symbol, timeframe, start, end):
        if symbol.ticker == "ZZZ":
            return []
        return await super().history(symbol, timeframe, start, end)


def test_an_unloadable_benchmark_is_reported_instead_of_scoring_against_zero():
    cfg = backtest_config()
    cfg.universe.benchmark = "ZZZ"
    result = asyncio.run(run_backtest(cfg, provider=_Benchmarkless(seed=3)))

    hits = [w for w in result.warnings if "ZZZ" in w]
    assert hits, f"silent fake excess column; warnings={result.warnings}"
    assert "초과" in hits[0]
    for name, score in _attribution(result)["by_source"].items():
        assert score["excess_expectancy_pct"] == score["expectancy_pct"], name


def test_the_live_path_prices_the_benchmark_too(tmp_path):
    """The dashboard's attribution panel had the identical silent hole."""
    cfg = live_config()
    cfg.universe.benchmark = "ZZZ"
    trader = LiveTrader(cfg, state_path=str(tmp_path / "bench.db"))
    ctx = trader.engine.ctx

    asyncio.run(trader.warmup())
    assert ctx.price(ctx.benchmark) > 0
    assert ctx.benchmark.key not in {s.key for s in ctx.universe}

    # and it keeps being priced, or the excess column goes stale after a day
    trader._seen = {k: ts - timedelta(days=5) for k, ts in trader._seen.items()}
    fetched = asyncio.run(trader._fetch_new_bars())
    assert ctx.benchmark.key in fetched


def test_the_worst_alpha_warning_does_not_claim_a_benchmark_it_never_had():
    """It said 'negative benchmark-excess expectancy' with no benchmark at all."""
    result = asyncio.run(run_backtest(backtest_config()))
    for w in result.warnings:
        assert "benchmark-excess" not in w, w


# ── (c) a missing 수급 source must not pass for a 수급 backtest ──────────
def flow_config(provider: str, alpha: list[ModelSpec]) -> StrategyConfig:
    cfg = backtest_config()
    cfg.flow = FlowConfig(provider=provider,
                          params={"seed": 5} if provider == "synthetic" else {})
    cfg.alpha = alpha
    cfg.backtest.end = datetime(2022, 7, 1, tzinfo=UTC)
    return cfg


def _flow_warnings(result) -> list[str]:
    return [w for w in result.warnings if "수급" in w]


def test_omitting_the_flow_source_warns_when_a_flow_alpha_is_configured():
    # The price alpha is the point: it keeps trading, so the report looks
    # healthy while both 수급 models sit there emitting nothing.
    cfg = flow_config("none", [ModelSpec(type="ema_cross"),
                               ModelSpec(type="investor_flow"),
                               ModelSpec(type="retail_contrarian")])
    result = asyncio.run(run_backtest(cfg))

    hits = _flow_warnings(result)
    assert hits, f"README promises a warning here; warnings={result.warnings}"
    assert "investor_flow" in hits[0] and "retail_contrarian" in hits[0]


def test_a_configured_flow_source_produces_no_such_warning():
    cfg = flow_config("synthetic", [ModelSpec(type="investor_flow")])
    result = asyncio.run(run_backtest(cfg))
    assert _flow_warnings(result) == []


def test_a_strategy_that_never_reads_flow_stays_quiet():
    """Otherwise the warning appears on every config and stops being read."""
    result = asyncio.run(run_backtest(flow_config("none", [ModelSpec(type="ema_cross")])))
    assert _flow_warnings(result) == []
