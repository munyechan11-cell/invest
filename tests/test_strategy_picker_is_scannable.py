"""전략 고르는 칸은 눈으로 훑어져야 합니다.

열 줄이 전부 `이름 · 모드 — 연동 필요` 한 덩어리로 늘어서 있으면 어디를
봐야 할지 모릅니다. 고를 때 실제로 필요한 판단은 둘입니다 — **돈이
움직이나**, 그리고 **지금 쓸 수 있나**.

앞의 것을 묶음 제목으로 올리면 줄마다 붙던 꼬리가 사라지고, 실거래가 이름 옆
작은 글씨가 아니라 **통째로 다른 칸** 이 됩니다. 뒤의 것만 줄에 남깁니다.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "quant" / "api" / "static" / "index.html").read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))

STRATEGIES = [
    {"id": "demo", "label_ko": "연습 · 여러 신호 섞기", "mode": "backtest",
     "requires": []},
    {"id": "demo_flow", "label_ko": "연습 · 수급 신호", "mode": "backtest",
     "requires": []},
    {"id": "kr_desk_gemini", "label_ko": "국내 · AI 데스크 + 수급 (한투)",
     "mode": "live", "requires": ["KIS_APP_KEY"]},
    {"id": "kr_equity", "label_ko": "국내 스윙", "mode": "backtest",
     "requires": ["KIS_APP_KEY"]},
    {"id": "kr_toss", "label_ko": "국내 · 수급 추종 (토스)", "mode": "live",
     "requires": ["TOSS_CLIENT_ID"]},
    {"id": "paper_one", "label_ko": "모의 굴리기", "mode": "dry_run",
     "requires": ["TOSS_CLIENT_ID"]},
    {"id": "us_toss", "label_ko": "미국 · 상대강도 상위 (토스)", "mode": "live",
     "requires": ["TOSS_CLIENT_ID"]},
    {"id": "odd", "label_ko": "알 수 없는 모드", "mode": "something_new",
     "requires": []},
]


def _engine() -> str | None:
    jsc = ("/System/Library/Frameworks/JavaScriptCore.framework"
           "/Versions/A/Helpers/jsc")
    return shutil.which("node") or (jsc if Path(jsc).exists() else None)


def _fragment() -> str:
    """목록을 조립하는 부분만 떼어 냅니다 — 나머지는 네트워크와 DOM 입니다."""
    body = re.search(r"((?:async )?function loadStrategies\(\) \{.*?\n\})",
                     SCRIPT, re.S)
    assert body, "loadStrategies 를 찾지 못했습니다"
    text = body.group(0)
    start = text.index("  const GROUPS = [")
    end = text.index("  // 연동이 끝난 전략을 기본으로 고릅니다.")
    return text[start:end]


def render(linked: list[str], strategies=None) -> list[tuple[str, list[str]]]:
    """(묶음 제목, [줄]) 목록으로 실제 실행 결과를 돌려줍니다."""
    engine = _engine()
    source = f"""
function esc(s){{return String(s).replace(/[&<>"]/g,
  c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));}}
const stLabel = st => (st && (st.label_ko || st.name)) || "";
var linked = new Set({json.dumps(linked)});
var strategies = {json.dumps(strategies if strategies is not None else STRATEGIES,
                             ensure_ascii=False)};
var sel = {{innerHTML: ""}};
{_fragment()}
var write = (typeof console !== "undefined" && console.log) ? console.log : print;
write(JSON.stringify(sel.innerHTML));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(source)
        path = Path(handle.name)
    try:
        result = subprocess.run([engine, str(path)], capture_output=True,
                                text=True, timeout=30, check=True)
    finally:
        path.unlink(missing_ok=True)
    html = json.loads(result.stdout.strip().splitlines()[-1])
    out = []
    for group in re.findall(r'<optgroup label="(.*?)">(.*?)</optgroup>', html, re.S):
        title = group[0].replace("&amp;", "&")
        rows = [r.replace("&amp;", "&")
                for r in re.findall(r"<option[^>]*>(.*?)</option>", group[1], re.S)]
        out.append((title, rows))
    return out


pytestmark = pytest.mark.skipif(_engine() is None,
                                reason="JavaScript 엔진이 없습니다")


# ── 묶음이 판단을 대신합니다 ─────────────────────────────────────────────
def test_the_list_is_grouped_by_what_is_at_stake():
    titles = [t for t, _ in render(["TOSS_CLIENT_ID"])]
    assert any("과거 검증" in t for t in titles)
    assert any("모의투자" in t for t in titles)
    assert any("실거래" in t for t in titles)


def test_the_safe_groups_come_first():
    """실거래에 닿으려면 목록을 끝까지 내려가야 하고, 그 이동 자체가 한 번
    더 묻는 일이 됩니다."""
    titles = [t for t, _ in render(["TOSS_CLIENT_ID"])]
    live = next(i for i, t in enumerate(titles) if "실거래" in t)
    assert live == max(i for i, t in enumerate(titles) if "기타" not in t)


def test_a_row_no_longer_repeats_its_mode():
    """묶음 제목이 말하는 것을 줄마다 다시 쓰면, 그게 열 줄을 한 덩어리로
    만든 원인입니다."""
    for _title, rows in render(["TOSS_CLIENT_ID"]):
        for row in rows:
            assert "과거 검증" not in row and "실거래" not in row
            assert "모의투자" not in row


def test_live_is_a_whole_section_not_a_suffix():
    live = next(rows for title, rows in render(["TOSS_CLIENT_ID"])
                if "실거래" in title)
    assert "국내 · 수급 추종 (토스)" in live
    assert "연습 · 여러 신호 섞기" not in live


# ── 지금 쓸 수 있는 것이 위로 ────────────────────────────────────────────
def test_ready_strategies_sort_above_the_ones_needing_a_link():
    """묶음 안에서까지 섞여 있으면 "지금 고를 수 있는 것" 을 다시 눈으로
    골라내야 합니다."""
    for _title, rows in render(["TOSS_CLIENT_ID"]):
        needs = [i for i, r in enumerate(rows) if "연동 필요" in r]
        ready = [i for i, r in enumerate(rows) if "연동 필요" not in r]
        assert not (needs and ready) or max(ready) < min(needs)


def test_the_servers_order_survives_inside_a_group():
    """가나다순으로 다시 세우면 연습·국내·미국으로 묶여 오던 순서가
    흩어집니다 — 목록의 뜻을 화면이 지웁니다."""
    backtest = next(rows for title, rows in render([]) if "과거 검증" in title)
    assert backtest[:2] == ["연습 · 여러 신호 섞기", "연습 · 수급 신호"]


def test_linking_a_broker_moves_its_strategies_up():
    without = next(rows for t, rows in render([]) if "실거래" in t)
    with_toss = next(rows for t, rows in render(["TOSS_CLIENT_ID"]) if "실거래" in t)
    assert "연동 필요" in without[0]
    assert "연동 필요" not in with_toss[0]


# ── 아무것도 조용히 사라지지 않습니다 ────────────────────────────────────
def test_every_strategy_still_appears_exactly_once():
    rows = [r for _t, group in render(["TOSS_CLIENT_ID"]) for r in group]
    assert len(rows) == len(STRATEGIES)


def test_an_unknown_mode_lands_in_its_own_group_rather_than_vanishing():
    """조용히 빠지는 것이 잘못 분류되는 것보다 나쁩니다."""
    other = next(rows for title, rows in render([]) if "기타" in title)
    assert other == ["알 수 없는 모드"]


def test_an_empty_mode_produces_no_empty_heading():
    only_live = [s for s in STRATEGIES if s["mode"] == "live"]
    titles = [t for t, _ in render(["TOSS_CLIENT_ID"], only_live)]
    assert titles == [t for t in titles if "실거래" in t]
