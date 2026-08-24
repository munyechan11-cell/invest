"""Objective functions for parameter search.

Optimising raw return is how you get a strategy that made all its money in one
month of 2021. Every loss here is risk-aware, and most penalise low trade counts
— a "great" result from six trades is noise wearing a suit.

Convention: **lower is better** (these are losses, not scores).
"""
from __future__ import annotations

import math

from quant.backtest.runner import BacktestResult

#: 거래가 하나도 없으면 최적화할 대상이 없습니다. 눈금과 무관하게 밀어냅니다.
_NO_TRADES = 1e6


def _thin_penalty(result: BacktestResult, scale: float, minimum: int = 20) -> float:
    """거래 수가 모자란 결과에 **더할** 벌점. 0 이면 벌점 없음.

    곱셈이 아니라 덧셈이어야 합니다. 손실은 낮을수록 좋은 값이라 좋은 전략에서
    음수가 되는데, 음수에 1보다 큰 수를 곱하면 더 작아집니다 — 벌점이 그대로
    상으로 뒤집힙니다. 여섯 번 거래하고 얻은 샤프 3 이 스무 번 거래한 샤프 3
    보다 좋다고 말하는 목적함수가 됩니다. 정확히 이 함수가 걸러내려던 것입니다.

    `multi_metric` 은 `quant optimize` 와 `quant walkforward` **둘 다의** 기본
    손실입니다. 그래서 이 엔진이 지금까지 내놓은 walk-forward PASS 판정은 전부
    뒤집힌 목표 아래에서 골라진 것이고, 다시 돌려야 합니다.

    `scale` 은 그 손실 함수의 눈금입니다 — 샤프는 O(1) 이고 수익률은 무제한이라
    상수 하나로 둘 다 맞출 수 없습니다.
    """
    n = result.report.trades
    if n == 0:
        return _NO_TRADES
    if n >= minimum:
        return 0.0
    return scale * (minimum - n) / minimum


def sharpe_loss(result: BacktestResult) -> float:
    return -result.report.sharpe + _thin_penalty(result, 1.0)


def sortino_loss(result: BacktestResult) -> float:
    return -result.report.sortino + _thin_penalty(result, 1.0)


def calmar_loss(result: BacktestResult) -> float:
    return -result.report.calmar + _thin_penalty(result, 1.0)


def profit_loss(result: BacktestResult) -> float:
    return -result.report.total_return + _thin_penalty(result, 0.5)


def max_drawdown_loss(result: BacktestResult) -> float:
    r = result.report
    return -(r.total_return / max(r.max_drawdown, 0.01)) + _thin_penalty(result, 1.0)


def deflated_sharpe_loss(result: BacktestResult) -> float:
    """Optimise the probability the Sharpe is real, not the Sharpe itself.

    This is the loss to use when running many trials — it has the multiple-
    testing correction built in, so the search is actively discouraged from
    chasing the luckiest parameter set.
    """
    return -result.report.deflated_sharpe + _thin_penalty(result, 1.0)


def multi_metric_loss(result: BacktestResult) -> float:
    """Balanced objective: risk-adjusted return, drawdown, and consistency.

    Weighted so no single term can dominate — a strategy has to be decent on all
    three rather than spectacular on one.
    """
    r = result.report
    if r.trades == 0:
        return _NO_TRADES
    sharpe_term = -math.tanh(r.sharpe / 2.0)
    dd_term = min(r.max_drawdown / 0.25, 2.0)
    pf_term = -math.tanh(r.profit_factor - 1.0) if math.isfinite(r.profit_factor) else -1.0
    consistency = -math.tanh(r.win_rate * 2 - 0.8)
    turnover_term = min(r.turnover / 50.0, 1.0)
    return (2.0 * sharpe_term + 1.0 * dd_term + 1.0 * pf_term
            + 0.5 * consistency + 0.5 * turnover_term) + _thin_penalty(result, 2.0)


LOSS_FUNCTIONS = {
    "sharpe": sharpe_loss,
    "sortino": sortino_loss,
    "calmar": calmar_loss,
    "profit": profit_loss,
    "max_drawdown": max_drawdown_loss,
    "deflated_sharpe": deflated_sharpe_loss,
    "multi_metric": multi_metric_loss,
}
