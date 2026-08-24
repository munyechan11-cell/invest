"""탐색이 무엇을 고르는가.

손실 함수는 최적화가 무엇을 좋다고 부를지 정합니다. 여기가 뒤집히면 엔진은
고장 나지 않습니다 — 멀쩡히 돌면서 잘못된 파라미터를 골라 옵니다. 그리고
그 결과를 walk-forward 가 PASS 로 도장 찍습니다. 조용해서 더 위험합니다.

`multi_metric` 은 `quant optimize` 와 `quant walkforward` 둘 다의 기본값입니다.
"""
from __future__ import annotations

import pytest

from quant.backtest.metrics import PerformanceReport
from quant.backtest.runner import BacktestResult
from quant.optimize.losses import LOSS_FUNCTIONS

#: 벌점이 붙기 시작하는 거래 수. losses._thin_penalty 의 minimum 과 같습니다.
THIN, THICK = 6, 40


def result(*, trades: int, sharpe: float = 1.5, total_return: float = 0.35,
           max_drawdown: float = 0.10, win_rate: float = 0.55,
           profit_factor: float = 1.6, deflated_sharpe: float = 0.7,
           turnover: float = 12.0) -> BacktestResult:
    rep = PerformanceReport(
        trades=trades, sharpe=sharpe, sortino=sharpe * 1.3, calmar=sharpe * 0.8,
        total_return=total_return, max_drawdown=max_drawdown, win_rate=win_rate,
        profit_factor=profit_factor, deflated_sharpe=deflated_sharpe,
        turnover=turnover)
    # 장부도 같은 수만큼 채웁니다. `_thin_penalty` 는 성적표의 자리 수가 아니라
    # 장부 레코드 수를 세는데(그 이유는 losses._thin_penalty 참고), 여기서 한쪽만
    # 채우면 "거래 N회" 라는 이 헬퍼의 뜻이 두 갈래로 갈라집니다.
    return BacktestResult(config_name="t", report=rep, equity_curve=[], monthly={},
                          trades=[{}] * trades, engine_summary={})


@pytest.mark.parametrize("name", sorted(LOSS_FUNCTIONS))
def test_fewer_trades_is_never_rewarded(name):
    """같은 성적이면 거래가 적은 쪽이 더 좋아 보이면 안 됩니다.

    벌점을 곱셈으로 붙이면 정확히 이게 일어납니다 — 좋은 전략의 손실은
    음수라서, 1보다 큰 수를 곱하면 더 작아집니다. 벌점이 상이 됩니다.
    """
    loss = LOSS_FUNCTIONS[name]
    thin, thick = loss(result(trades=THIN)), loss(result(trades=THICK))
    assert thin > thick, (
        f"{name}: 거래 {THIN}회({thin:.4f})가 {THICK}회({thick:.4f})보다 "
        f"좋게 나옵니다 — 벌점이 상으로 뒤집혔습니다")


@pytest.mark.parametrize("name", sorted(LOSS_FUNCTIONS))
def test_no_trades_loses_to_everything(name):
    """거래가 없으면 고를 것이 없습니다. 어떤 실적보다도 나쁘게 나와야 합니다."""
    loss = LOSS_FUNCTIONS[name]
    none = loss(result(trades=0))
    awful = loss(result(trades=THICK, sharpe=-2.0, total_return=-0.6,
                        max_drawdown=0.8, win_rate=0.2, profit_factor=0.4,
                        deflated_sharpe=0.0))
    assert none > awful, f"{name}: 거래 0회가 손실 전략보다 좋게 나옵니다"


@pytest.mark.parametrize("name", sorted(LOSS_FUNCTIONS))
def test_a_better_strategy_gets_a_lower_loss(name):
    """벌점을 고치면서 방향 자체가 뒤집히지 않았는지."""
    loss = LOSS_FUNCTIONS[name]
    good = loss(result(trades=THICK, sharpe=2.2, total_return=0.60,
                       max_drawdown=0.08, win_rate=0.60, profit_factor=2.1,
                       deflated_sharpe=0.9))
    poor = loss(result(trades=THICK, sharpe=0.2, total_return=0.03,
                       max_drawdown=0.30, win_rate=0.44, profit_factor=1.05,
                       deflated_sharpe=0.1))
    assert good < poor, f"{name}: 좋은 전략이 나쁜 전략보다 높은 손실을 받습니다"


def test_the_penalty_actually_bites_at_the_default_loss():
    """multi_metric 은 두 최적화 명령의 기본값입니다 — 여기가 제일 중요합니다."""
    loss = LOSS_FUNCTIONS["multi_metric"]
    # 여섯 번 거래한 샤프 3.0 vs 마흔 번 거래한 샤프 1.5. 앞쪽은 노이즈입니다.
    fluke = loss(result(trades=THIN, sharpe=3.0, win_rate=0.83, profit_factor=4.0))
    solid = loss(result(trades=THICK, sharpe=1.5))
    assert solid < fluke, "여섯 번 거래한 요행이 마흔 번의 실적을 이깁니다"
