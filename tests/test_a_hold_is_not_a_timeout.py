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


# ── 후보가 좁으면 관망 하나가 "오늘은 끝" 이 됩니다 ─────────────────────
#
# 후보 4종목에 한 봉 심의 4종목이면, 그 넷이 관망일 때 그 봉에는 아무 일도
# 없습니다. 화면에서 그건 봇이 멈춘 것과 구별되지 않습니다. 후보를 넓히면
# 관망 하나가 곧 끝이 되지 않고, 매 봉 다시 매겨지는 선택이 다른 종목을
# 올려 줍니다.

KIS_PAPER = ["configs/kr_kis_paper.yaml", "configs/us_kis_paper.yaml"]


@pytest.mark.parametrize("path", KIS_PAPER)
def test_the_practice_universe_is_wide_enough_to_keep_looking(path):
    config = _load(path)
    assert len(config.universe.symbols) >= 20, (
        f"{path}: 후보 {len(config.universe.symbols)}종목 — 관망 몇 개로 "
        "그 봉이 끝납니다")


@pytest.mark.parametrize("path", KIS_PAPER)
def test_the_filter_does_not_undo_the_wider_universe(path):
    """후보를 20개 적어 놓고 `limit` 이 4로 자르면 아무것도 안 바뀝니다."""
    config = _load(path)
    limits = [f.params.get("max_symbols") for f in config.universe.filters
              if f.type == "limit"]
    for cap in limits:
        assert cap is None or cap >= 12, f"{path}: limit {cap} 이 후보를 다시 좁힙니다"


@pytest.mark.parametrize("path", KIS_PAPER)
def test_the_korean_ladder_is_on_for_every_new_name(path):
    """새로 넣은 종목이 사다리를 안 켜면 그 종목만 조용히 거절당합니다."""
    config = _load(path)
    for spec in config.universe.symbols:
        if spec.quote_currency.upper() == "KRW":
            assert spec.tick_ladder == "krx", f"{path}: {spec.ticker}"


@pytest.mark.parametrize("path", KIS_PAPER)
def test_no_duplicate_candidates(path):
    tickers = [s.ticker for s in _load(path).universe.symbols]
    assert len(tickers) == len(set(tickers))


# ── 넓게 보려다 느려지는 것 ──────────────────────────────────────────────
#
# 로그: 종목당 호출 **80번**. 16석이면 19번이면 됩니다. 나머지는 전부 429
# 재시도입니다 — 네 종목을 한꺼번에 던지면 같은 순간에 76번이 나가고, 무료
# 티어는 그 대부분을 튕겨 냅니다. 튕긴 호출은 백오프만큼 기다렸다 다시
# 나가므로, 넓게 보려던 것이 **느려지고 비싸집니다.**

@pytest.mark.parametrize("path", DESK_CONFIGS)
def test_symbols_are_queued_not_all_fired_at_once(path):
    params = desk_params(path)
    concurrent = params.get("concurrent_symbols", params.get("max_symbols_per_run", 4))
    assert concurrent <= 2, (
        f"{path}: {concurrent}종목을 동시에 심의합니다 — 같은 순간의 호출이 "
        "한도를 넘으면 재시도로 되돌아옵니다")


def test_the_desk_actually_queues_them():
    """설정만 있고 코드가 안 지키면 아무것도 안 바뀝니다."""
    import inspect

    from quant.alpha.desk import TradingDesk

    src = inspect.getsource(TradingDesk.update)
    assert "Semaphore(self.concurrent_symbols)" in src


def test_one_is_the_floor_not_zero():
    """0 이면 아무 종목도 심의하지 못합니다."""
    from quant.alpha.desk import TradingDesk
    from tests.test_desk import ScriptedLLM

    assert TradingDesk(ScriptedLLM(), concurrent_symbols=0, memory=False
                       ).concurrent_symbols == 1
