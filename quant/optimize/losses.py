"""Objective functions for parameter search.

Optimising raw return is how you get a strategy that made all its money in one
month of 2021. Every loss here is risk-aware, and most penalise low trade counts
— a "great" result from six trades is noise wearing a suit.

Convention: **lower is better** (these are losses, not scores).
"""
from __future__ import annotations

import math

from quant.backtest.runner import BacktestResult


def _penalise_thin(result: BacktestResult, minimum: int = 20) -> float:
    """Multiplier that punishes results with too few trades to mean anything."""
    n = result.report.trades
    if n >= minimum:
        return 1.0
    if n == 0:
        return 10.0
    return 1.0 + (minimum - n) / minimum * 2.0


def sharpe_loss(result: BacktestResult) -> float:
    return -result.report.sharpe * (1.0 / _penalise_thin(result))


def sortino_loss(result: BacktestResult) -> float:
    return -result.report.sortino * (1.0 / _penalise_thin(result))


def calmar_loss(result: BacktestResult) -> float:
    return -result.report.calmar * (1.0 / _penalise_thin(result))


def profit_loss(result: BacktestResult) -> float:
    return -result.report.total_return


def max_drawdown_loss(result: BacktestResult) -> float:
    r = result.report
    return -(r.total_return / max(r.max_drawdown, 0.01))


def deflated_sharpe_loss(result: BacktestResult) -> float:
    """Optimise the probability the Sharpe is real, not the Sharpe itself.

    This is the loss to use when running many trials — it has the multiple-
    testing correction built in, so the search is actively discouraged from
    chasing the luckiest parameter set.
    """
    return -result.report.deflated_sharpe * (1.0 / _penalise_thin(result))


def multi_metric_loss(result: BacktestResult) -> float:
    """Balanced objective: risk-adjusted return, drawdown, and consistency.

    Weighted so no single term can dominate — a strategy has to be decent on all
    three rather than spectacular on one.
    """
    r = result.report
    if r.trades == 0:
        return 100.0
    sharpe_term = -math.tanh(r.sharpe / 2.0)
    dd_term = min(r.max_drawdown / 0.25, 2.0)
    pf_term = -math.tanh(r.profit_factor - 1.0) if math.isfinite(r.profit_factor) else -1.0
    consistency = -math.tanh(r.win_rate * 2 - 0.8)
    turnover_term = min(r.turnover / 50.0, 1.0)
    return (2.0 * sharpe_term + 1.0 * dd_term + 1.0 * pf_term
            + 0.5 * consistency + 0.5 * turnover_term) * _penalise_thin(result)


LOSS_FUNCTIONS = {
    "sharpe": sharpe_loss,
    "sortino": sortino_loss,
    "calmar": calmar_loss,
    "profit": profit_loss,
    "max_drawdown": max_drawdown_loss,
    "deflated_sharpe": deflated_sharpe_loss,
    "multi_metric": multi_metric_loss,
}
