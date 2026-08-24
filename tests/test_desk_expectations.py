"""심의를 기다릴 이유가 있는가.

여덟 전략 중 AI 데스크를 쓰는 것은 하나(`kr-desk-gemini`)뿐입니다. 나머지는
규칙 기반 신호로만 매매하므로 데스크 방은 영원히 비어 있는 것이 **정상**
입니다.

그런데 화면은 그 자리에 "대기 중 — 심의가 시작되면 여기에 발언이 그대로
남습니다" 를 띄웠습니다. 곧 뭔가 시작될 것처럼 읽히고, 실제로는 오지 않습니다.
기다릴 이유가 없는 것을 기다리게 만드는 화면입니다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from quant.api.server import strategy_catalog
from quant.config.loader import load_config

HTML = Path("quant/api/static/index.html").read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))


def _has_desk(cfg) -> bool:
    return any(m.type in ("desk", "council") for m in cfg.alpha)


def test_most_strategies_genuinely_have_no_desk():
    """이 테스트의 전제 — 데스크 없는 전략이 다수입니다."""
    without = []
    for name, path in strategy_catalog().items():
        try:
            cfg = load_config(str(path))
        except Exception:
            continue
        if not _has_desk(cfg):
            without.append(name)
    assert len(without) >= 5, f"데스크 없는 전략: {without}"


def test_the_screen_says_when_no_deliberation_is_coming():
    assert "function renderDeskAvailability" in SCRIPT
    body = re.search(r"function renderDeskAvailability\(\) \{(.*?)\n\}", SCRIPT, re.S).group(1)
    assert "데스크를 쓰지 않습니다" in body
    # 무엇을 골라야 볼 수 있는지도 말해야 합니다.
    assert "kr-desk-gemini" in body


def test_it_reacts_to_changing_the_strategy():
    """전략을 바꿨는데 옛 안내가 남으면 그것도 거짓말입니다."""
    onchange = re.search(r'\$\("#strategyPick"\)\.onchange = \(\) => \{(.*?)\n\};',
                         SCRIPT, re.S).group(1)
    assert "renderDeskAvailability" in onchange


def test_starting_a_bot_takes_you_to_where_it_happens():
    """시작을 누른 사람이 다음에 보고 싶은 것은 무슨 일이 일어나는가입니다."""
    assert "function scrollToDesk" in SCRIPT
    start = re.search(r'document\.getElementById\("runStart"\)\.onclick = async \(\) => \{(.*?)\n\};',
                      SCRIPT, re.S).group(1)
    assert "scrollToDesk()" in start


def test_the_demo_does_the_same():
    demo = SCRIPT[SCRIPT.index('$("#demoBtn")'):]
    assert "scrollToDesk()" in demo[:400]


def test_switching_strategy_clears_a_symbol_that_no_longer_exists():
    """옛 종목이 남으면 "AAPL 는 이 전략의 종목이 아닙니다" 를 계속 보게 됩니다.

    사용자가 고른 적도 없는 종목에 대해서.
    """
    body = re.search(r"function syncChartSymbols\(syms\) \{(.*?)\n\}", SCRIPT, re.S).group(1)
    assert "names.includes" in body
    assert 'sel.value = ""' in body


@pytest.mark.parametrize("room", ["analyst", "debate", "risk", "decision"])
def test_every_room_gets_the_message(room):
    body = re.search(r"function renderDeskAvailability\(\) \{(.*?)\n\}", SCRIPT, re.S).group(1)
    assert room in body
