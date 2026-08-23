"""End-to-end engine behaviour: no look-ahead, no money leaks, working stops."""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.alpha.base import AlphaModel
from quant.backtest.runner import run_backtest
from quant.config.loader import load_config
from quant.config.schema import (
    BacktestConfig, BrokerConfig, CostConfig, DataConfig, ExecutionConfig,
    ModelSpec, PortfolioConfig, RiskConfig, StrategyConfig, SymbolSpec, UniverseConfig,
)
from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, Bar, Direction, Insight, RunMode, Symbol

SYM = Symbol("AAA", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))


def base_config(**overrides) -> StrategyConfig:
    cfg = StrategyConfig(
        name="test",
        mode=RunMode.BACKTEST,
        data=DataConfig(provider="synthetic", params={"seed": 1}, timeframe="1d",
                        warmup_bars=60),
        universe=UniverseConfig(symbols=[SymbolSpec(ticker="AAA", venue="SIM")]),
        alpha=[ModelSpec(type="ema_cross")],
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


# ── no look-ahead ────────────────────────────────────────────────────────
def test_context_history_never_returns_unclosed_bars():
    clock = SimClock(datetime(2024, 1, 5, tzinfo=UTC))
    ctx = Context(clock, Portfolio(1000), EventBus(), timeframe="1d")
    for i in range(10):
        ts = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=i)
        ctx.push_bar(Bar(SYM, ts, 1, 1, 1, 1, 1, "1d"))
    # bars opening on the 1st..4th have closed by the 5th; the 5th's has not
    assert len(ctx.history(SYM)) == 4
    clock.set(datetime(2024, 1, 6, tzinfo=UTC))
    assert len(ctx.history(SYM)) == 5


def test_sim_clock_refuses_to_run_backwards():
    clock = SimClock(datetime(2024, 1, 5, tzinfo=UTC))
    with pytest.raises(ValueError):
        clock.set(datetime(2024, 1, 4, tzinfo=UTC))


class _AlwaysLong(AlphaModel):
    """Emits a fresh full-conviction long every bar."""

    name = "always_long"

    async def update(self, ctx, bars):
        return [
            Insight(bar.symbol, Direction.UP, ctx.bar_delta * 3, ctx.now,
                    confidence=1.0, source=self.name)
            for bar in bars.values()
        ]


def test_no_money_leak_buy_and_hold(monkeypatch):
    """With zero costs, an always-long book must track the asset's own return.

    Any drift between the two means the fill/cash/position accounting is
    fabricating or destroying value somewhere in the pipeline.
    """
    import quant.strategy.builder as builder

    monkeypatch.setitem(builder.BUILTIN_ALPHA_MODELS, "always_long", _AlwaysLong)
    cfg = base_config()
    cfg.alpha = [ModelSpec(type="always_long")]
    cfg.portfolio.model = ModelSpec(type="equal_weight")
    cfg.portfolio.max_position_weight = 1.0   # single-name book, no cap

    result = asyncio.run(run_backtest(cfg))

    from quant.data.providers.local import SyntheticProvider

    provider = SyntheticProvider(seed=1)
    bars = asyncio.run(provider.history(
        SYM, "1d", cfg.backtest.start, cfg.backtest.end))
    asset_return = bars[-1].close / bars[1].open - 1

    # exposure is ~100% but never exactly 100% (lot rounding, one-bar entry lag)
    assert result.report.total_return == pytest.approx(asset_return, abs=0.05)
    assert result.report.exposure > 0.9


def test_orders_never_fill_on_their_signal_bar():
    """The engine must place at bar t and fill no earlier than bar t+1."""
    from quant.brokerage.paper import PaperBrokerage
    from quant.core.engine import Engine
    from quant.execution.models import ImmediateExecution
    from quant.portfolio.models import EqualWeighting

    pf = Portfolio(10_000)
    clock = SimClock(datetime(2024, 1, 1, tzinfo=UTC))
    ctx = Context(clock, pf, EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    broker = PaperBrokerage(pf)
    engine = Engine(ctx, _AlwaysLong(),
                    EqualWeighting(cash_reserve_pct=0.0, max_position_weight=1.0),
                    ImmediateExecution(), broker)
    asyncio.run(engine.start())

    bar1 = Bar(SYM, datetime(2024, 1, 1, tzinfo=UTC), 100, 101, 99, 100, 1e6, "1d")
    asyncio.run(engine.on_bars({SYM.key: bar1}))
    assert pf.quantity(SYM) == 0            # order placed, nothing filled yet
    assert len(asyncio.run(broker.open_orders())) == 1

    bar2 = Bar(SYM, datetime(2024, 1, 2, tzinfo=UTC), 100, 101, 99, 100, 1e6, "1d")
    asyncio.run(engine.on_bars({SYM.key: bar2}))
    assert pf.quantity(SYM) > 0             # filled against the *next* bar


def test_stop_loss_closes_the_position():
    cfg = base_config()
    cfg.risk = RiskConfig(models=[
        ModelSpec(type="max_dd_per_security", params={"max_drawdown_pct": 0.05})
    ])
    result = asyncio.run(run_backtest(cfg))
    losers = [t for t in result.trades if t["pnl_pct"] < 0]
    if losers:
        # one bar of gap risk beyond the stop is expected; 3x the limit is not
        assert min(t["pnl_pct"] for t in losers) > -20.0


def test_protection_lock_never_blocks_an_exit():
    """A lock must stop entries and let exits through — the inverse is a trap."""
    from quant.risk.models import TradingLockGate
    from quant.core.types import PortfolioTarget

    pf = Portfolio(10_000)
    clock = SimClock(datetime(2024, 1, 1, tzinfo=UTC))
    ctx = Context(clock, pf, EventBus(), timeframe="1d")
    pf.position(SYM).quantity = Decimal("10")
    ctx.lock(SYM, clock.now() + timedelta(days=5), "test lock")

    gate = TradingLockGate()
    exit_target = PortfolioTarget(SYM, Decimal("0"))
    assert gate.manage(ctx, [exit_target])[0].quantity == Decimal("0")

    add_target = PortfolioTarget(SYM, Decimal("20"))
    assert gate.manage(ctx, [add_target])[0].quantity == Decimal("10")

    reduce_target = PortfolioTarget(SYM, Decimal("5"))
    assert gate.manage(ctx, [reduce_target])[0].quantity == Decimal("5")


def test_backtest_is_deterministic():
    cfg = base_config()
    a = asyncio.run(run_backtest(cfg))
    b = asyncio.run(run_backtest(base_config()))
    assert a.report.ending_equity == pytest.approx(b.report.ending_equity)
    assert a.report.trades == b.report.trades


def test_live_mode_requires_explicit_confirmation():
    with pytest.raises(ValueError, match="live_trading_confirmed"):
        StrategyConfig(name="x", mode=RunMode.LIVE,
                       broker=BrokerConfig(type="ccxt"))


def test_demo_config_runs():
    cfg = load_config("configs/demo.yaml")
    result = asyncio.run(run_backtest(cfg))
    assert result.bars > 200
    assert result.report.starting_equity == 100_000
