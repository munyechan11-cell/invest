"""성향 저장이 돌고 있는 봇의 한도를 **느슨하게** 만들면 안 된다.

시작할 때 도는 `apply_profile` 의 규칙은 "설정 파일에 명시된 값이 언제나
우선" 입니다. 그런데 실행 중 경로는 무조건 대입했고, 그 본문이 서버와
레지스트리 두 곳에 복제돼 있었습니다. 그래서 설문 한 번이 설정에 명시된 하루
손실 한도 1% 를 5% 로, 손절 상한 8% 를 30% 로 바꿨습니다 — 같은 성향이라도
언제 저장했느냐에 따라 봇이 다르게 돌았습니다.

런타임에서는 **조이는 방향만** 반영합니다. 조이는 변경은 사용자가 이미
받아들인 노출보다 커질 수 없습니다. 풀려면 설정 화면의 하루 한도로 가야 하고,
그쪽에는 감사 기록과 "0 은 한도 없음이 아니라 안 적었음" 규칙이 있습니다.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from quant.live.limits import TradingBudget
from quant.live.profile import InvestorProfile, apply_profile_to_engine
from quant.portfolio.models import EqualWeighting
from quant.risk.models import MaximumDrawdownPerSecurity

AGGRESSIVE = InvestorProfile(overrides={"R": 1.0, "H": 1.0, "E": 0.0, "C": 0.0})
DEFENSIVE = InvestorProfile(overrides={"R": -1.0, "H": 0.0, "E": 0.0, "C": 0.0})


def engine_with(*, loss_pct=0.01, orders=10, stop_pct=0.08, atr=3.0):
    stop = MaximumDrawdownPerSecurity(max_drawdown_pct=stop_pct, atr_multiple=atr)
    budget = TradingBudget(max_daily_loss_pct=loss_pct, max_daily_orders=orders)
    return SimpleNamespace(
        portfolio_model=EqualWeighting(),
        budget=budget,
        risk=SimpleNamespace(models=[stop]),
    ), budget, stop


def test_an_aggressive_profile_does_not_widen_an_explicit_daily_cap():
    engine, budget, stop = engine_with()

    out = apply_profile_to_engine(engine, AGGRESSIVE)

    assert budget.max_loss_pct == pytest.approx(0.01), "하루 손실 한도가 풀렸다"
    assert stop.limit == pytest.approx(0.08), "손절 상한이 넓어졌다"
    assert stop.atr_multiple == pytest.approx(3.0)
    assert out["loosened_blocked"], "무엇이 반영되지 않았는지 말해야 한다"


def test_a_defensive_profile_still_tightens():
    """조이는 방향은 그대로 반영되어야 합니다 — 안 그러면 기능이 죽습니다."""
    engine, budget, stop = engine_with(loss_pct=0.05, orders=200,
                                       stop_pct=0.30, atr=10.0)

    out = apply_profile_to_engine(engine, DEFENSIVE)

    assert budget.max_loss_pct < 0.05
    assert budget.max_orders < 200
    assert stop.limit < 0.30
    assert stop.atr_multiple < 10.0
    assert out["loosened_blocked"] == []


def test_sizing_still_follows_the_profile_in_both_directions():
    """사이징은 한도가 아닙니다 — 사용자가 방금 고른 값이 맞습니다."""
    engine, _budget, _stop = engine_with()
    before = engine.portfolio_model.max_position_weight

    apply_profile_to_engine(engine, AGGRESSIVE)
    aggressive = engine.portfolio_model.max_position_weight
    apply_profile_to_engine(engine, DEFENSIVE)

    assert aggressive != before or aggressive > 0
    assert engine.portfolio_model.max_position_weight < aggressive


def test_an_unlimited_cap_can_still_be_tightened():
    """한도가 아예 없던 봇(0)에는 성향이 처음으로 한도를 걸어 줍니다."""
    engine, budget, _stop = engine_with(loss_pct=0.0, orders=0)

    apply_profile_to_engine(engine, AGGRESSIVE)

    assert budget.max_loss_pct > 0
    assert budget.max_orders > 0


def test_the_kis_token_lock_does_not_need_a_loop_at_import_time():
    """모듈 로드가 이벤트 루프를 요구하면 봇 시작이 import 에서 죽습니다."""
    from quant.core.aio import LazyLock
    from quant.data.providers import kis

    assert isinstance(kis._TOKEN_LOCK, LazyLock)
