"""Quant — an event-driven algorithmic trading engine.

One pipeline drives backtesting, paper trading and live execution:

    universe → alpha (insights) → portfolio construction (targets)
             → risk (veto/shrink) → execution (orders) → brokerage

Quick start:

    from quant import load_config, run_backtest
    result = await run_backtest(load_config("configs/momentum.yaml"))
    result.print_summary()
"""
from __future__ import annotations

__version__ = "1.0.0"

from quant.config.loader import dump_config, load_config
from quant.config.schema import StrategyConfig
from quant.core.types import (
    AssetClass,
    Bar,
    Direction,
    Insight,
    Order,
    OrderSide,
    OrderType,
    PortfolioTarget,
    RunMode,
    Symbol,
)

__all__ = [
    "__version__",
    "load_config",
    "dump_config",
    "StrategyConfig",
    "AssetClass",
    "Bar",
    "Direction",
    "Insight",
    "Order",
    "OrderSide",
    "OrderType",
    "PortfolioTarget",
    "RunMode",
    "Symbol",
    "run_backtest",
    "build_engine",
]


def __getattr__(name: str):
    # Lazy so `import quant` stays cheap and does not pull in numpy/httpx.
    if name == "run_backtest":
        from quant.backtest.runner import run_backtest as fn
        return fn
    if name == "build_engine":
        from quant.strategy.builder import build_engine as fn
        return fn
    raise AttributeError(name)
