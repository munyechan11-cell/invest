"""심의를 기다릴 이유가 있는가.

실매매 전략은 이제 전부 데스크를 거칩니다(`test_every_live_strategy_has_a_desk`).
데스크가 없는 것은 백테스트 전략뿐이고, 거기서는 **없는 것이 맞습니다** —
LLM 은 과거 날짜의 미래를 알아서 성적표를 거짓으로 좋게 만듭니다.

그런데 화면은 데스크 없는 전략에서도 "대기 중 — 심의가 시작되면 여기에 발언이
그대로 남습니다" 를 띄웠습니다. 곧 뭔가 시작될 것처럼 읽히고, 실제로는 오지
않습니다. 기다릴 이유가 없는 것을 기다리게 만드는 화면입니다.
"""
from __future__ import annotations

import re

import pytest

from quant.api.server import strategy_catalog
from quant.config.loader import load_config
from tests.screen import screen

HTML = screen()
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))


def _has_desk(cfg) -> bool:
    return any(m.type in ("desk", "council") for m in cfg.alpha)


def test_some_strategies_genuinely_have_no_desk():
    """이 파일의 전제 — 데스크가 영원히 안 도는 전략이 실제로 있습니다.

    전부 데스크를 쓰게 되면 아래 화면 검사들은 지킬 대상이 없어집니다.
    그때는 이 테스트가 먼저 실패해서 그 사실을 알려 줍니다.
    """
    without = []
    for name, path in strategy_catalog().items():
        try:
            cfg = load_config(str(path))
        except Exception:
            continue
        if not _has_desk(cfg):
            without.append(name)
    assert without, "데스크 없는 전략이 하나도 없습니다 — 이 파일의 검사가 무의미해집니다"


def test_the_screen_says_when_no_deliberation_is_coming():
    assert "function renderDeskAvailability" in SCRIPT
    assert "function deskWaitReason" in SCRIPT
    body = re.search(r"function deskWaitReason\(\) \{(.*?)\n\}", SCRIPT, re.S).group(1)
    assert 'kind: "nodesk"' in body, "데스크 없는 전략을 따로 구분하지 않습니다"
    assert "규칙 기반" in body, (
        "데스크가 없다는 사실만 말하고 그럼 무엇으로 매매하는지는 말하지 "
        "않습니다 — 사용자는 봇이 멈춘 줄 압니다.")


def test_waiting_forever_and_waiting_a_while_look_different():
    """"대기 중" 하나로 네 상황을 덮으면 사람은 고장을 기다림으로 읽습니다.

    데스크가 없는 전략, 아직 시작 안 함, 장이 닫힘, LLM 키가 거절당함 —
    앞의 셋은 기다리면 되고 마지막은 기다려도 영영 안 됩니다. 서버는 넷을
    구분할 재료를 이미 다 내보내고 있었고(`disabled_reason`,
    `market.minutes_to_open`, `universe`), 화면만 안 읽었습니다.
    """
    body = re.search(r"function deskWaitReason\(\) \{(.*?)\n\}", SCRIPT, re.S).group(1)
    # 영영 안 되는 쪽 — 서버가 만들어 둔 진짜 이유를 그대로 보여줘야 합니다.
    assert "disabled_reason" in body, "데스크가 왜 꺼졌는지 화면이 읽지 않습니다"
    # 기다리면 되는 쪽 — 얼마나 기다려야 하는지 말해야 합니다.
    assert "minutes_to_open" in body, "개장까지 얼마나 남았는지 말하지 않습니다"
    # 볼 종목이 없으면 그것도 기다림이 아닙니다.
    assert "universe" in body, "유니버스가 비었을 때를 구분하지 않습니다"
    # 넷이 같은 문구로 뭉치면 안 됩니다.
    kinds = set(re.findall(r'kind: "(\w+)"', body))
    assert len(kinds) >= 4, f"구분하는 상태가 {len(kinds)}가지뿐입니다: {kinds}"


def test_a_dead_desk_is_not_painted_as_a_patient_one():
    """기다려도 안 되는 상태는 눈에 다르게 보여야 합니다."""
    assert ".saybox.bad" in HTML, "'영영 안 됨' 을 나타낼 스타일이 없습니다"
    body = re.search(r"function renderDeskAvailability\(\) \{(.*?)\n\}", SCRIPT, re.S).group(1)
    assert 'r.kind === "broken"' in body, "고장 상태에 다른 표시를 붙이지 않습니다"


def test_it_does_not_name_one_strategy_as_the_only_desk():
    """"데스크가 있는 전략(kr-desk-gemini)을 고르세요" 는 이제 거짓입니다.

    데스크 전략이 하나뿐이던 시절의 문장인데, 그때도 토스만 연동한 사람에게는
    고를 수 없는 이름이었습니다. 화면이 특정 이름을 박아 두면 전략을 추가할
    때마다 그 문장이 조용히 낡습니다 — `kr-toss-desk` 를 만든 날 이미 낡았습니다.
    """
    reason = re.search(r"function deskWaitReason\(\) \{(.*?)\n\}", SCRIPT, re.S).group(1)
    for hard_coded in ("kr-desk-gemini", "kr-toss-desk", "us-toss-desk"):
        assert hard_coded not in reason, (
            f"안내가 {hard_coded} 를 이름으로 박아 두었습니다 — "
            "전략이 늘거나 줄면 그대로 틀린 말이 됩니다.")


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
