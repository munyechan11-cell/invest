"""다중검정 보정이 실제로 무언가를 말하는가.

Deflated Sharpe 는 "파라미터 200개를 돌려봤고 그중 제일 좋은 게 샤프 2.1"
이라는 문장에 대한 정직한 답입니다. 무편향 후보 200개 중 최댓값의 기댓값은
0이 아니고, 그만큼을 빼는 것이 이 지표입니다.

그러려면 후보들이 서로 얼마나 흩어져 있는지를 알아야 합니다. 그 값을
가정하면 지표가 조용히 죽습니다 — 예외도 경고도 없이, 그냥 모든 전략이
0점을 받습니다. 0점은 "과최적화입니다" 처럼 보이기 때문에 더 나쁩니다.
"""
from __future__ import annotations

import math

import pytest

from quant.backtest.metrics import deflated_sharpe
from quant.optimize.hyperopt import _trial_sharpe_variance

PPY = 252
N = 756          # 일봉 3년


def annual(sr: float) -> float:
    return sr / math.sqrt(PPY)


def test_a_good_strategy_is_not_pinned_at_zero():
    """예전 기본값(1.0)은 기준선을 연 샤프 49로 만들었습니다.

    그 아래에서는 존재할 수 있는 모든 전략이 정확히 0.000000 을 받습니다.
    지표가 아니라 상수였습니다.
    """
    dsr = deflated_sharpe(annual(2.0), N, 0.0, 3.0, trials=200)
    assert dsr > 0.5, f"연 샤프 2.0 이 {dsr:.6f} — 기준선이 여전히 비현실적입니다"


def test_the_old_default_is_what_killed_it():
    """되돌아가지 않았는지 확인하는 회귀 표시."""
    dead = deflated_sharpe(annual(2.0), N, 0.0, 3.0, trials=200,
                           variance_of_trials=1.0)
    assert dead < 1e-6, "이 값이 0이 아니면 이 테스트의 전제가 바뀐 것입니다"


def test_more_trials_is_a_higher_bar():
    """더 많이 뒤졌으면 같은 샤프도 덜 믿어야 합니다 — 이 지표의 존재 이유."""
    few = deflated_sharpe(annual(1.6), N, 0.0, 3.0, trials=5)
    many = deflated_sharpe(annual(1.6), N, 0.0, 3.0, trials=2000)
    assert many < few, "시행을 400배 늘렸는데 기준선이 오르지 않습니다"


def test_a_wider_trial_spread_is_a_higher_bar():
    """후보들이 넓게 흩어져 있을수록 최댓값은 우연히도 높습니다."""
    tight = deflated_sharpe(annual(1.6), N, 0.0, 3.0, trials=200,
                            variance_of_trials=0.0002)
    wide = deflated_sharpe(annual(1.6), N, 0.0, 3.0, trials=200,
                           variance_of_trials=0.002)
    assert wide < tight


def test_one_trial_is_just_the_probabilistic_sharpe():
    """한 번만 돌렸으면 뺄 것이 없습니다."""
    from quant.backtest.metrics import probabilistic_sharpe
    assert deflated_sharpe(annual(1.5), N, 0.0, 3.0, trials=1) == pytest.approx(
        probabilistic_sharpe(annual(1.5), N, 0.0, 3.0, 0.0))


def test_the_variance_comes_from_the_trials_actually_run():
    history = [{"report": {"sharpe": s}} for s in (1.2, 0.8, 2.1, -0.4, 1.5, 0.2)]
    v = _trial_sharpe_variance(history, "1d")
    assert v is not None and 0 < v < 0.1
    # 연 단위 분산을 그대로 쓰면 안 됩니다 — 샤프는 1기간 단위로 넘깁니다.
    import statistics
    assert v == pytest.approx(
        statistics.variance([s / math.sqrt(PPY) for s in (1.2, 0.8, 2.1, -0.4, 1.5, 0.2)]))


@pytest.mark.parametrize("history", [
    [],
    [{"report": {"sharpe": 1.0}}],
    [{"report": None}, {"report": None}],
])
def test_too_few_trials_to_measure_says_so(history):
    """하나로는 분산이 정의되지 않습니다. 0 을 지어내지 않고 None 을 냅니다."""
    assert _trial_sharpe_variance(history, "1d") is None
