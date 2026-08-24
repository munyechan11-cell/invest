"""화면 요소의 id 는 유일해야 합니다.

`id="strategy"` 가 헤더의 전략 이름 표시와 새 전략 선택 상자 양쪽에 붙어
있었습니다. `getElementById` 는 앞의 것을 돌려주므로, 시작 버튼은 `<span>`
에서 `.value` 를 읽어 undefined 를 서버로 보냈습니다 — 연동을 다 마친
사용자에게 아무 일도 일어나지 않는, 이유가 화면에 없는 실패였습니다.
"""
import re
from collections import Counter
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "quant/api/static/index.html"


@pytest.fixture(scope="module")
def html() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_every_id_in_the_markup_is_unique(html):
    body = html[:html.index("<script")]
    ids = re.findall(r'\bid="([^"]+)"', body)
    dupes = {k: n for k, n in Counter(ids).items() if n > 1}
    assert not dupes, f"중복된 id: {dupes}"


def test_every_id_the_script_reaches_for_exists(html):
    body = html[:html.index("<script")]
    present = set(re.findall(r'\bid="([^"]+)"', body))
    script = html[html.index("<script"):]
    wanted = set(re.findall(r'getElementById\("([^"]+)"\)', script))
    wanted |= set(re.findall(r'\$\("#([A-Za-z][\w-]*)"\)', script))
    # 스크립트가 만들어 넣는 것들은 마크업에 없어도 됩니다.
    created = set(re.findall(r'id="([^"${}]+)"', script))
    missing = sorted(wanted - present - created)
    assert not missing, f"마크업에 없는 id 를 참조합니다: {missing}"


def test_the_strategy_picker_is_a_select_not_a_label(html):
    """값을 읽어야 하는 요소가 표시용 span 이면 조용히 undefined 가 나갑니다."""
    assert '<select id="strategyPick"' in html
    assert '<span class="tab on px" id="strategy">' in html
