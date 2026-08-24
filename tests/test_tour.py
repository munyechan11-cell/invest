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


def test_the_tour_yields_to_the_setup_wizard():
    """연동이 안 된 사람에게는 설정이 먼저입니다. 둘이 겹치면 둘 다 안 읽힙니다."""
    body = re.search(r"function maybeTour\(me\) \{(.*?)\n\}", SCRIPT, re.S).group(1)
    assert 'getElementById("setup")' in body and "return" in body


def test_the_account_carries_the_flag():
    from quant.webapp.accounts import User
    assert User.tour_seen is False or "tour_seen" in User.__dataclass_fields__
