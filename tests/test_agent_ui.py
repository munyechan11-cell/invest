"""화면이 "어느 에이전트인가" 를 잃지 않는지 고정합니다.

한 계좌에서 성향이 다른 봇을 넷까지 굴릴 때, 화면의 조작 하나하나에 "누구에게"
가 붙어야 합니다. 붙지 않으면 서버가 400 으로 되묻는데 — 그건 안전이지만 —
붙었는데 **엉뚱한 에이전트에 붙는 것** 은 안전이 아닙니다. 공격형을 정리하려다
보수형을 비우는 것은 탭 하나 잘못 눌러서 일어납니다.

그래서 여기서 고정하는 것은 세 가지입니다:

  · 돈이 움직이는 모든 호출이 `withAgent(...)` 를 지나는가
  · 주문 확인창이 **어느 에이전트인지** 말하는가
  · 자본 비중 합이 100% 를 넘으면 시작 버튼이 막히는가
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "quant" / "api" / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "quant" / "api" / "static" / "app.css").read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))

#: 계좌의 돈을 움직이거나 봇의 행동을 바꾸는 경로. 전부 에이전트에 귀속돼야
#: 합니다 — 그렇지 않으면 서버가 되묻고, 화면은 이유 없이 막힌 것처럼 보입니다.
AGENT_SCOPED = [
    "/api/manual/buy", "/api/manual/sell", "/api/manual/close",
    "/api/manual/close_all", "/api/manual/pause", "/api/manual/resume",
    "/api/manual/unpin/", "/api/manual", "/api/limits", "/api/profile",
]


def _whole_fn(name: str) -> str:
    match = re.search(
        rf"((?:async )?function {name}\([^)]*\) \{{.*?\n\}})", SCRIPT, re.S)
    assert match, f"{name} 함수를 찾지 못했습니다"
    return match.group(1)


# ── 모든 조작이 에이전트에 귀속되는가 ────────────────────────────────────
def _statement_around(index: int) -> str:
    """이 위치를 감싸는 문장. 앞의 `;`/`{`/`}` 부터 뒤의 `;` 까지.

    `withAgent(...)` 가 따옴표 바로 앞에 오지 않는 경우가 있습니다 — 삼항식
    안에 들어가거나(`withAgent(paused ? "…resume" : "…pause")`) 줄이 바뀌거나.
    문장 전체를 보면 그 둘을 오탐하지 않습니다.
    """
    # 줄바꿈은 경계로 쓰지 않습니다 — 호출이 여러 줄에 걸치는 경우가 있고,
    # 줄에서 끊으면 앞줄의 `withAgent(` 를 놓쳐 오탐합니다.
    start = max(SCRIPT.rfind(ch, 0, index) for ch in ";{}")
    end = SCRIPT.find(";", index)
    return SCRIPT[start + 1:end if end != -1 else index + 200]


@pytest.mark.parametrize("path", AGENT_SCOPED)
def test_every_agent_scoped_call_goes_through_with_agent(path):
    """`api("/api/limits")` 처럼 맨손으로 부르는 자리가 하나라도 남으면, 그
    조작은 그룹에서 400 으로 튕기거나 엉뚱한 대상에 적용됩니다."""
    bare = []
    for m in re.finditer(rf'["\']{re.escape(path)}["\']', SCRIPT):
        statement = _statement_around(m.start())
        # `withAccount` 는 "일부러 계좌 전체" 라는 뜻입니다 — ⚙설정의 하루
        # 한도와 초기 성향 진단. 맨손 호출과 구별해야 합니다.
        if "withAgent" not in statement and "withAccount" not in statement:
            bare.append(statement.strip()[:90])
    assert not bare, f"{path} 를 withAgent/withAccount 없이 부르는 자리: {bare}"


def test_the_setup_sheet_edits_the_account_cap_not_an_agents():
    """그룹이 도는 동안 ⚙설정의 하루 한도가 활성 에이전트의 파일에 적히면,
    계좌 마스터 한도를 바꿀 길이 화면 어디에도 없습니다."""
    load = _whole_fn("loadLimits")
    assert 'withAccount("/api/limits")' in load
    assert 'withAgent("/api/limits")' not in load
    # 저장도 같은 범위여야 합니다 — 읽는 곳과 쓰는 곳이 다르면 사용자는
    # 자기가 본 값을 고쳤다고 믿습니다.
    assert 'post(withAccount("/api/limits")' in SCRIPT
    # 에이전트 탭을 눌러도 설정 시트의 계좌 한도 칸이 에이전트 값으로
    # 덮이지 않아야 합니다.
    tabs = _whole_fn("renderAgentTabs")
    assert "loadLimits()" not in tabs


def test_with_agent_appends_nothing_when_no_agent_is_chosen():
    """그룹을 쓰지 않는 사람의 요청 경로는 정확히 그대로여야 합니다."""
    fn = _whole_fn("withAgent")
    assert "if (!activeAgent) return path;" in fn
    assert "encodeURIComponent(activeAgent)" in fn


def test_with_agent_keeps_an_existing_query_string():
    fn = _whole_fn("withAgent")
    assert 'path.includes("?") ? "&" : "?"' in fn


# ── 확인창이 대상을 말하는가 ─────────────────────────────────────────────
def test_the_order_review_names_the_agent_and_says_the_others_stay():
    """전체 청산을 누르는 사람이 확인해야 하는 것은 "무엇을" 만이 아니라
    "누구의 것을" 입니다."""
    review = _whole_fn("confirmOrderReview")
    assert "agents.length > 1" in review
    assert "에이전트: ${agentLabel(activeAgent)}" in review
    assert "나머지 ${agents.length - 1}개는 그대로" in review


def test_the_review_uses_the_agents_own_mode_not_the_accounts():
    """계좌 등급은 가장 위험한 에이전트를 따릅니다. 관찰용 에이전트에 넣는
    주문에까지 "실거래" 라고 써 붙이면 그 경고가 곧 무의미해집니다."""
    review = _whole_fn("confirmOrderReview")
    assert "a.agent_id === activeAgent" in review
    assert "mine ? mine.mode : status.mode" in review


# ── 자본 비중 ────────────────────────────────────────────────────────────
def test_the_start_button_is_blocked_when_the_weights_exceed_the_account():
    fn = _whole_fn("renderWeightSum")
    assert "start.disabled = over" in fn
    assert "100% 를 넘을 수 없습니다" in fn


def test_the_leftover_share_is_named_as_cash():
    """합계가 100% 미만인 것은 오류가 아니라 선택입니다 — 남는 몫은 현금."""
    assert "남는 ${100 - total}% 는 현금" in _whole_fn("renderWeightSum")


def test_a_hand_typed_weight_is_never_rebalanced_away():
    """적어 둔 숫자가 다음 "+ 추가" 한 번에 사라지면 도와주는 것이 아닙니다."""
    rebalance = _whole_fn("rebalanceAgents")
    assert "r => !r.touched" in rebalance
    assert "agentRows[i].touched = true;" in _whole_fn("renderAgentRows")


# ── 실거래 확인은 에이전트마다 ───────────────────────────────────────────
def test_each_live_agent_is_confirmed_separately():
    """하나를 확인했다고 나머지가 열리면, 관찰용으로 넣은 에이전트가 진짜
    주문을 내고 있다는 사실을 사용자가 모른 채로 하루를 보냅니다."""
    start = SCRIPT[SCRIPT.index('document.getElementById("agentStart")'):]
    start = start[:start.index("\n};")]
    assert "for (const row of agentRows)" in start
    assert 'row.mode === "live"' in start
    assert "spec.confirm = typed.trim();" in start


def test_a_live_agent_row_is_visually_marked():
    """넷 중 하나만 진짜 돈이면, 그 하나가 나머지와 같은 회색으로 보이는 것이
    이 화면에서 가장 위험합니다."""
    assert '.agentrow[data-live="1"]' in CSS
    assert "var(--red)" in CSS[CSS.index('.agentrow[data-live="1"]'):][:120]


def test_a_stopped_agent_tab_does_not_look_alive():
    """멈춘 에이전트를 살아 있는 것과 같은 모양으로 두면, 사용자는 그것이 아직
    시장에 있다고 믿습니다."""
    assert '.agtab[data-stopped="1"]' in CSS


# ── 그룹과 단일 봇은 배타적 ──────────────────────────────────────────────
def test_the_group_builder_is_hidden_while_anything_runs():
    """계좌가 하나이므로 둘이 동시에 돌 수 없습니다. 눌러도 409 로 거절당할
    버튼을 사용자가 찾아다니게 두지 않습니다."""
    fn = _whole_fn("runnerState")
    assert "setup.hidden = running" in fn


def test_a_vanished_agent_is_not_kept_as_the_target():
    """없어진 이름을 계속 붙여 보내면 서버가 거절하고, 사용자는 자기가 무엇을
    잘못했는지 알 수 없습니다."""
    fn = _whole_fn("adoptAgents")
    assert "!agents.includes(activeAgent)) activeAgent = \"\"" in fn


# ── 실제로 도는가 ────────────────────────────────────────────────────────
@pytest.mark.skipif(not shutil.which("node"), reason="node 없음")
def test_the_weight_maths_actually_balances():
    """산수는 읽어서 맞는지 알기 어렵습니다 — 돌려 봅니다."""
    harness = """
    let agentRows = [];
    const AGENT_MAX = 4;
    let strategies = [{id: "a", name: "a"}, {id: "b", name: "b"}];
    function missingFor() { return []; }
    function renderAgentRows() {}
    function defaultAgentId(i) { return "a" + (i + 1); }
    %s
    const out = [];
    addAgentRow(); out.push(agentRows.map(r => r.capital_weight));
    addAgentRow(); out.push(agentRows.map(r => r.capital_weight));
    addAgentRow(); out.push(agentRows.map(r => r.capital_weight));
    // 손으로 70%% 를 적으면 나머지가 남은 몫을 나눠 갖는다
    agentRows[0].capital_weight = 0.7; agentRows[0].touched = true;
    rebalanceAgents(); out.push(agentRows.map(r => r.capital_weight));
    console.log(JSON.stringify(out));
    """ % (_whole_fn("addAgentRow") + "\n" + _whole_fn("rebalanceAgents"))

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(harness)
        path = fh.name
    result = subprocess.run(["node", path], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    import json
    one, two, three, typed = json.loads(result.stdout)

    assert one == [1.0], "혼자면 계좌 전부"
    assert two == [0.5, 0.5], "둘이면 반반"
    assert sum(three) <= 1.0 + 1e-9, f"셋의 합이 계좌를 넘었습니다: {three}"
    assert typed[0] == 0.7
    assert sum(typed) <= 1.0 + 1e-9, f"손으로 적은 뒤 합이 넘었습니다: {typed}"
