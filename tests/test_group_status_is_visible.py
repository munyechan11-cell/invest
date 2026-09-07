"""그룹의 상태가 화면에 **보이는가** — 정지, 죽음, 첫 폴링.

세 가지 조용한 고장입니다.

  · 계좌 합계 불변식이 어긋나 게이트웨이가 그룹 전체를 멈추면 모든 주문이
    거절되는데, 서버가 `status.account.halt_reason` 에 실어 보내는 그 사유를
    화면이 읽지 않았습니다. 머리말은 "실거래" 그대로였습니다.
  · 그룹이 죽어도 `/api/health` 에 `last_error` 가 실리지 않았습니다 — 그룹
    상태에는 최상위 `strategy` 가 없고, 사유는 에이전트 행에 있습니다.
  · 에이전트 하나짜리 그룹의 첫 폴링이 "에이전트 '' 는 이 그룹에 없습니다" 였습니다
    — `default_agent_id` 가 둘 미만이면 "" 를 돌려주는데, 그 빈 이름을 모르는
    이름처럼 거절했습니다.

화면 쪽은 함수를 **실행** 합니다. 문자열이 있는지만 보면 고친 것을 되돌려도
리터럴이 죽은 코드에 남아 있는 한 통과합니다.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from quant.api.server import _focus_group_status, _last_stop, create_app
from quant.config.schema import StrategyConfig
from quant.webapp import accounts as accounts_module
from quant.webapp.registry import UserRegistry

sys.path.insert(0, str(Path(__file__).parent))

from test_api_agents import PASSWORD, SECRET, spec, start_group, template  # noqa: E402


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(accounts_module, "_PBKDF2_ROUNDS", 1_000, raising=False)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """`test_api_agents` 와 같은 준비 — 템플릿 둘, 계정 하나, paper 브로커."""
    root = tmp_path / "templates"
    root.mkdir()
    for name in ("attack", "defend"):
        (root / f"{name}.yaml").write_text(
            yaml.safe_dump(template(f"{name}-strat"), allow_unicode=True),
            encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUANT_SECRET_KEY", SECRET)
    monkeypatch.setenv("QUANT_USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setenv("QUANT_USER_DATA", str(tmp_path / "userdata"))
    monkeypatch.setenv("QUANT_ENV_FILE", str(tmp_path / "env.test"))
    monkeypatch.setenv("QUANT_CONFIG_DIR", str(root))
    monkeypatch.delenv("QUANT_API_TOKEN", raising=False)

    app = create_app(StrategyConfig.model_validate(template("운영자")),
                     state_path=str(tmp_path / "state.db"))
    with TestClient(app, base_url="https://desk.example") as c:
        r = c.post("/api/auth/register",
                   json={"email": "me@x.com", "password": PASSWORD})
        assert r.status_code == 201, r.text
        yield c


HTML = Path("quant/api/static/index.html").read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))

_ENGINES = [
    (shutil.which("node"), []),
    ("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc", []),
]


def _engine():
    for path, args in _ENGINES:
        if path and Path(path).exists():
            return path, args
    return None


def _whole_fn(name: str) -> str:
    match = re.search(
        rf"((?:async )?function {name}\([^)]*\) \{{.*?\n\}})", SCRIPT, re.S)
    assert match, f"{name} 함수를 찾지 못했습니다"
    return match.group(1)


def _group_status(*, alive: bool, halted: bool = False, reason: str = "",
                  errors: dict[str, str] | None = None, top_level: bool = True) -> dict:
    """`GroupTrader.status()` 의 모양을 손으로 만듭니다.

    `top_level=False` 는 그룹이 최상위 `error`·`stopped_at` 를 아직 답지 않던
    옛 모양입니다 — 그때도 에이전트 행의 사유는 화면에 닿아야 합니다.
    """
    errors = errors or {}
    rows = [{"agent_id": a, "label": f"{a} 라벨", "mode": "live",
             "strategy": f"{a}-strat", "running": alive,
             "error": errors.get(a, "")} for a in ("attack", "defend")]
    body = {"running": alive, "mode": "live",
            "account": {"halted": halted, "halt_reason": reason, "halt_drift": {}},
            "agents": rows}
    if not alive and top_level:
        body["error"] = "; ".join(f"{k}: {v}" for k, v in errors.items())
        body["stopped_at"] = "2026-09-04T00:31:00+00:00"
    return body


# ── 죽은 그룹은 /api/health 에 사유를 남긴다 ─────────────────────────────
def test_last_stop_reads_the_single_bot_shape():
    error, strategy, stopped = _last_stop({
        "running": False, "strategy": "kr-toss", "error": "시세를 받지 못했습니다",
        "stopped_at": "2026-09-04T00:31:00+00:00"})
    assert (error, strategy, stopped) == (
        "시세를 받지 못했습니다", "kr-toss", "2026-09-04T00:31:00+00:00")


def test_last_stop_reads_the_group_shape_including_the_agents_strategies():
    st = _group_status(alive=False, errors={"attack": "워밍업 실패"})
    error, strategy, stopped = _last_stop(st)
    assert "워밍업 실패" in error
    assert "attack-strat" in strategy and "defend-strat" in strategy
    assert stopped == st["stopped_at"]


def test_last_stop_falls_back_to_the_agent_rows_when_the_group_has_no_top_level_error():
    st = _group_status(alive=False, top_level=False,
                       errors={"attack": "취소됨", "defend": "인증 거절"})
    error, _, _ = _last_stop(st)
    assert "defend" in error and "인증 거절" in error
    # "취소됨" 은 사람이 멈춘 흔적입니다 — 정지 버튼 뒤에 붉게 뜨면 안 됩니다.
    assert "취소됨" not in error
    only_cancelled = _group_status(alive=False, top_level=False,
                                   errors={"attack": "취소됨"})
    assert _last_stop(only_cancelled)[0] == ""


def test_a_running_group_without_errors_is_not_a_stop():
    assert _last_stop(_group_status(alive=True)) == ("", "attack-strat, defend-strat", None)


@pytest.mark.parametrize("top_level", [True, False])
def test_health_surfaces_why_a_group_died(client, monkeypatch, top_level):
    """봇이 죽으면 화면은 `health.last_error` 로 "봇이 멈췄습니다" 를 띄웁니다.
    그룹에는 그 값이 한 번도 실리지 않았습니다."""
    dead = _group_status(alive=False, top_level=top_level,
                         errors={"attack": "시세를 받지 못해 시작할 수 없습니다"})
    monkeypatch.setattr(UserRegistry, "status", lambda self, uid: dead)
    body = client.get("/api/health").json()
    assert body["trader_running"] is False
    assert "시세를 받지 못해" in (body.get("last_error") or ""), body
    assert "attack-strat" in (body.get("last_strategy") or ""), body
    if top_level:
        assert body["stopped_at"] == dead["stopped_at"]


# ── 첫 폴링 — 빈 이름은 "아무거나", 모르는 이름만 거절 ──────────────────
def test_an_empty_agent_id_means_the_first_agent():
    body = _focus_group_status(_group_status(alive=True), "")
    assert body["agent_id"] == "attack"
    assert "없습니다" not in body.get("message", "")


def test_an_unknown_agent_id_is_still_refused():
    body = _focus_group_status(_group_status(alive=True), "ghost")
    assert body["agent_id"] is None and "ghost" in body["message"]


def test_the_first_poll_of_a_single_agent_group_shows_its_book(client):
    """에이전트가 하나면 `default_agent_id` 는 "" 입니다 — 그 빈 이름이 거절되면
    첫 폴링부터 "에이전트 '' 는 이 그룹에 없습니다" 가 뜹니다."""
    r = start_group(client, spec("solo", "attack", 1.0))
    assert r.status_code == 200, r.text
    try:
        body = client.get("/api/status").json()
        assert body["agent_id"] == "solo", body.get("message")
        assert "portfolio" in body
        assert "없습니다" not in body.get("message", "")
    finally:
        client.post("/api/trader/stop")


# ── 계좌 정지는 화면에 보인다 ────────────────────────────────────────────
def test_the_screen_reads_the_accounts_halt_reason():
    summary = _whole_fn("renderRunSummary")
    assert "account.halt_reason" in summary
    assert "account.halted" in summary
    assert 'note("#runMsg"' in summary


@pytest.mark.skipif(_engine() is None, reason="JavaScript 엔진이 없습니다")
def test_a_halted_group_is_said_out_loud_once_per_reason():
    """정지 사유는 한 번만 — 폴링마다 다시 띄우면 읽히지 않습니다. 사유가
    바뀌면 다시, 풀리면 풀렸다고, 그룹이 멈추면 조용히."""
    path, args = _engine()
    prelude = r"""
var notes = [];
var els = {};
function $(sel) {
  if (!els[sel]) els[sel] = {textContent: "", dataset: {}, className: ""};
  return els[sel];
}
var document = {body: {classList: {toggle: function () {}}}};
function shownStrategy() {
  return {id: "us-toss", name: "us-toss", mode: "live", limits: {}};
}
function stLabel(st) { return st.name; }
function fmtNum(v) { return String(v == null ? "—" : v); }
function note(el, text, kind) { notes.push({el: el, text: text, kind: kind}); }
var lastHaltReason = "";
var botState = null;
"""
    driver = r"""
function snap() {
  return {label: $("#runSafetyLabel").textContent,
          text: $("#runSafetyText").textContent,
          state: $("#runSafety").dataset.state,
          rail: $("#railAuto").textContent,
          notes: notes.slice()};
}
var out = [];
botState = {running: true, mode: "live", account: {halted: false, halt_reason: ""}};
renderRunSummary(true); out.push(snap());
botState = {running: true, mode: "live",
            account: {halted: true, halt_reason: "슬리브 원장과 증권사 잔고가 다릅니다 — X"}};
renderRunSummary(true); renderRunSummary(true); out.push(snap());
botState.account = {halted: true, halt_reason: "미귀속 수량이 음수입니다"};
renderRunSummary(true); out.push(snap());
botState.account = {halted: false, halt_reason: ""};
renderRunSummary(true); out.push(snap());
botState = {running: false, mode: "live",
            account: {halted: true, halt_reason: "옛 사유"}};
renderRunSummary(false); out.push(snap());
console.log(JSON.stringify(out));
"""
    harness = prelude + _whole_fn("renderRunSummary") + "\n" + driver
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(harness)
        js = fh.name
    try:
        proc = subprocess.run([path, *args, js], capture_output=True, text=True,
                              timeout=60)
    finally:
        Path(js).unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    fine, halted, changed, released, stopped = json.loads(proc.stdout.strip().splitlines()[-1])

    assert fine["label"] == "실거래 자동매매 실행 중" and fine["notes"] == []
    assert fine["state"] == "live"

    # 정지: 표식·본문·레일이 전부 바뀌고, 사유는 두 번 그려도 한 번만 말한다.
    assert "정지됨" in halted["label"] and "실거래" in halted["label"]
    assert "슬리브 원장과 증권사 잔고가 다릅니다" in halted["text"]
    assert halted["state"] == "error" and "정지됨" in halted["rail"]
    assert len(halted["notes"]) == 1
    assert halted["notes"][0]["kind"] == "err"
    assert "슬리브 원장과 증권사 잔고가 다릅니다" in halted["notes"][0]["text"]

    # 사유가 바뀌면 다시 말한다.
    assert len(changed["notes"]) == 2
    assert "미귀속 수량이 음수입니다" in changed["notes"][1]["text"]

    # 돌고 있는데 사유가 사라졌으면 풀린 것이다 — 조용히 지우지 않는다.
    assert len(released["notes"]) == 3 and released["notes"][2]["kind"] == "ok"
    assert released["label"] == "실거래 자동매매 실행 중"

    # 멈춘 그룹의 옛 사유는 "지금 정지됨" 이 아니고, 풀렸다고도 하지 않는다.
    assert stopped["label"] == "자동매매 정지" and stopped["state"] == "off"
    assert len(stopped["notes"]) == 3
