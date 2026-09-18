"""화면의 "관망" 이 **판단인지 시간 초과인지** 구별되어야 합니다.

    관망 · 확신 14% — 토론·결정 단계가 마감 내 완료되지 않아 분석가 합의로 대체

이건 데스크가 "사지 말자" 고 결정한 것이 아닙니다. **마감 안에 끝나지
못해** 분석가 합의로 물러선 것이고, 그때 확신은 깎이고 사이즈는 절반으로
묶입니다. 화면에서는 둘 다 똑같이 "관망" 으로 보이지만, 사람이 해야 할
일은 정반대입니다 — 하나는 기다리는 것이고 하나는 마감을 늘리는 것입니다.

그리고 한 봉에 2종목만 심의하면, 그 둘이 관망일 때 나머지는 그 봉에서 한
번도 검토되지 않습니다. 화면에는 "관망" 하나만 남고, 사람은 봇이 멈춘
것으로 읽습니다.
"""
from __future__ import annotations

import pytest

from quant.cli import _load
from tests.conftest import shipped_configs

DESK_CONFIGS = [p for p in shipped_configs()
                if any(spec.type == "desk" for spec in _load(p).alpha)]


def desk_params(path):
    return next(s.params for s in _load(path).alpha if s.type == "desk")


@pytest.mark.parametrize("path", DESK_CONFIGS)
def test_every_candidate_gets_looked_at_each_cycle(path):
    """후보보다 적게 보면, 안 본 종목은 그 봉에서 기회가 없었습니다."""
    config = _load(path)
    params = desk_params(path)
    candidates = len(config.universe.symbols)
    assert params.get("max_symbols_per_run", 4) >= min(candidates, 4), (
        f"{path}: 후보 {candidates}종목 중 "
        f"{params.get('max_symbols_per_run')}종목만 심의합니다")


@pytest.mark.parametrize("path", DESK_CONFIGS)
def test_the_deadline_leaves_room_for_the_whole_desk(path):
    """19번 호출이 줄을 서는 무료 티어에서 120초는 모자랍니다. 모자라면
    데스크는 결정하지 않고 **대체** 합니다 — 그리고 그건 "관망" 으로
    보입니다."""
    params = desk_params(path)
    deadline = float(params.get("deadline_s", 120))
    assert deadline >= 180, (
        f"{path}: 마감 {deadline:.0f}초 — 토론·결정이 못 끝나면 분석가 "
        "합의로 대체되고, 화면에는 판단과 구별되지 않는 '관망' 이 남습니다")


@pytest.mark.parametrize("path", DESK_CONFIGS)
def test_the_deadline_still_fits_inside_one_bar(path):
    """마감이 한 봉보다 길면 다음 봉이 오는데도 심의가 안 끝납니다."""
    config = _load(path)
    bar_seconds = {"1d": 86400, "1w": 604800, "1h": 3600}.get(
        config.data.timeframe, 86400)
    assert float(desk_params(path).get("deadline_s", 120)) < bar_seconds / 4


def test_a_degraded_verdict_says_so_on_the_screen():
    """대체된 판단에는 '축약' 이 붙습니다. 안 붙으면 시간 초과가 판단으로
    읽힙니다."""
    from pathlib import Path

    html = Path("quant/api/static/index.html").read_text(encoding="utf-8")
    assert "d.degraded" in html and "축약" in html


def test_the_fallback_caps_its_own_conviction():
    """끝내지 못한 심의가 100% 확신을 말하면 안 됩니다."""
    import inspect

    from quant.alpha.desk import TradingDesk

    src = inspect.getsource(TradingDesk)
    assert '"conviction": min(abs(consensus), 0.7)' in src
    assert 'risk["position_scale"] = min(' in src
