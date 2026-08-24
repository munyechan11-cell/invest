"""첫 화면 안내.

처음 들어온 사람은 화면 절반이 비어 있는 것을 봅니다. 안내가 한 번 뜨는 것은
기능이지만, **매번** 뜨면 기능이 아니라 고장입니다. 그래서 여기서 검사하는 것은
"안내가 있는가"가 아니라 "봤다는 사실이 어디에 남는가" 입니다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

HTML = Path("quant/api/static/index.html").read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))


def test_the_tour_exists_in_the_markup():
    for element in ("tour", "tourVeil", "tourRing", "tourBox", "tourNext", "tourSkip"):
        assert f'id="{element}"' in HTML, f"{element} 가 없습니다"


@pytest.mark.parametrize("anchor", ["#setupBtn", "#runner", "#analystSeats",
                                    ".deck-r", "#meBtn"])
def test_every_step_points_at_something_that_exists(anchor):
    """안내가 가리키는 곳이 없으면 링만 사라지고 설명은 남습니다 — 최악의 조합."""
    assert anchor in SCRIPT, f"{anchor} 단계가 사라졌습니다"
    token = anchor.lstrip("#.")
    kind = 'id' if anchor.startswith("#") else 'class'
    assert re.search(rf'{kind}="[^"]*\b{re.escape(token)}\b', HTML), \
        f"{anchor} 를 가리키는데 그런 요소가 없습니다"


def test_seen_is_remembered_on_the_account_not_the_browser():
    """기기를 바꿔도 다시 뜨면 안 되고, 이 화면의 JS 는 저장소를 열지 않습니다."""
    assert "localStorage" not in SCRIPT
    assert "sessionStorage" not in SCRIPT
    assert "/api/auth/tour-seen" in SCRIPT


def test_a_step_that_points_at_nothing_on_screen_is_dropped():
    """좁은 화면에서는 시세 기둥이 display:none 입니다.

    그런 요소도 querySelector 는 찾아 주고, rect 는 0×0 입니다. 걸러내지
    않으면 링이 왼쪽 위 구석의 점이 되고, 설명은 화면에 없는 것을 설명합니다.
    """
    assert "function tourVisible" in SCRIPT
    assert "getClientRects().length" in SCRIPT
    assert "TOUR.filter(tourVisible)" in SCRIPT
    # 단계 수는 필터를 통과한 것만 세야 합니다.
    assert "TOUR.length" not in SCRIPT, "고정 길이를 아직 쓰고 있습니다"


def test_the_tour_holds_the_keyboard_while_it_is_up():
    """장막은 마우스만 막습니다.

    탭으로는 뒤쪽 버튼에 닿고, 거기서 엔터를 치면 설정 마법사가 장막
    **아래**에서 열립니다 — maybeTour() 가 막으려던 겹침이 그대로 생깁니다.
    """
    assert "el.inert = true" in SCRIPT          # 배경을 통째로 비활성
    assert 'ev.key === "Escape"' in SCRIPT      # 대화상자는 Esc 로 닫힙니다
    assert "function tourTrap" in SCRIPT        # 탭이 상자 안에서 돕니다
    assert "tourReturnFocus" in SCRIPT          # 끝나면 원래 자리로


def test_replaying_the_tour_does_not_re_announce_it():
    """? 안내로 다시 보는 경우에는 서버에 알릴 것이 없습니다."""
    body = re.search(r"function tourEnd\(\) \{(.*?)\n\}", SCRIPT, re.S).group(1)
    assert "if (me && me.tour_seen) return;" in body
    # POST 가 실패하면 로컬 표시도 세우지 않습니다 — 그래야 다음에 또 뜹니다.
    assert "r.ok" in body


def test_the_tour_yields_to_the_setup_wizard():
    """연동이 안 된 사람에게는 설정이 먼저입니다. 둘이 겹치면 둘 다 안 읽힙니다."""
    body = re.search(r"function maybeTour\(me\) \{(.*?)\n\}", SCRIPT, re.S).group(1)
    assert 'getElementById("setup")' in body and "return" in body


def test_the_account_carries_the_flag():
    from quant.webapp.accounts import User
    assert User.tour_seen is False or "tour_seen" in User.__dataclass_fields__
