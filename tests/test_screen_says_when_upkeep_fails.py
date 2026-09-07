"""화면이 **지금 상태** 를 말하는가 — 배지, 점검 실패, 실제 한도.

세 가지 조용한 거짓말입니다.

  · 봇이 멈춰도 머리말 `#mode` 배지가 "실거래" 그대로였습니다. 바로 아래
    안전 라벨은 "자동매매 정지" 라, 화면이 두 말을 동시에 했습니다.
  · 3초 유지 주기가 실패하면 **손절이 평가되지 않는데**, 봇은 살아 있으므로
    라벨은 "실행 중" 이고 화면 어디에도 그 사실이 없었습니다.
  · 실거래 시작 확인 창의 하루 한도가 템플릿 원값이라, 사용자가 설정 화면에
    저장한 값과 달랐습니다 — 그 창은 "얼마까지 잃어도 되는가" 를 읽으라고
    있는 자리입니다.

화면 쪽은 함수를 **실행** 합니다. 문자열만 대조하면 고친 것을 되돌려도
리터럴이 죽은 코드에 남아 있는 한 통과합니다.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from quant.api.server import create_app
from quant.config.schema import StrategyConfig
from quant.webapp import accounts as accounts_module

sys.path.insert(0, str(Path(__file__).parent))

from test_api_agents import PASSWORD, SECRET, template  # noqa: E402
from test_group_status_is_visible import _engine, _whole_fn  # noqa: E402


def run_js(harness: str):
    path, args = _engine()
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(harness)
        js = fh.name
    try:
        proc = subprocess.run([path, *args, js], capture_output=True,
                              text=True, timeout=60)
    finally:
        Path(js).unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout.strip().splitlines()[-1])


PRELUDE = r"""
var notes = [];
var els = {};
function $(sel) {
  if (!els[sel]) els[sel] = {textContent: "", dataset: {}, className: "",
                             innerHTML: "", querySelector: function () { return null; }};
  return els[sel];
}
var document = {body: {classList: {toggle: function () {}}},
                getElementById: function (id) { return $("#" + id); }};
function shownStrategy() {
  return {id: "us-toss", name: "us-toss", mode: "live", limits: {}};
}
function stLabel(st) { return st.name; }
function fmtNum(v) { return String(v == null ? "—" : v); }
function esc(s) { return String(s); }
function note(el, text, kind) { notes.push({el: el, text: text, kind: kind}); }
function renderMini() {}
function setBotBookSource() { return "장부"; }
function renderHud() {}
var MODE_BADGE = {live: "실거래", dry_run: "모의매매",
                  backtest: "과거검증", offline: "미가동"};
var lastHaltReason = "";
var botState = null;
"""


# ── 배지는 봇이 멈추면 함께 내려간다 ─────────────────────────────────────
@pytest.mark.skipif(_engine() is None, reason="JavaScript 엔진이 없습니다")
def test_the_mode_badge_comes_down_when_the_bot_stops():
    driver = r"""
$("#mode").textContent = "실거래";
$("#mode").className = "mode px";
$("#strategy").textContent = "미국 · AI 데스크";
clearBotBook("봇이 실행 중이 아닙니다");
console.log(JSON.stringify({mode: $("#mode").textContent,
                            cls: $("#mode").className,
                            strategy: $("#strategy").textContent}));
"""
    out = run_js(PRELUDE + _whole_fn("clearBotBook") + "\n" + driver)

    assert out["mode"] == "미가동", "멈춘 봇이 계속 '실거래' 라고 말한다"
    assert "backtest" in out["cls"], "붉은 실거래 배지가 그대로다"
    assert out["strategy"] == "—"


# ── 점검 주기 실패가 화면에 보인다 ───────────────────────────────────────
@pytest.mark.skipif(_engine() is None, reason="JavaScript 엔진이 없습니다")
def test_a_failing_upkeep_cycle_is_said_out_loud():
    driver = r"""
var out = [];
function snap() {
  return {label: $("#runSafetyLabel").textContent,
          text: $("#runSafetyText").textContent,
          state: $("#runSafety").dataset.state};
}
botState = {running: true, mode: "live", maintenance_error: ""};
renderRunSummary(true); out.push(snap());
botState = {running: true, mode: "live",
            maintenance_error: "봉 사이 손절 실패: toss 체결 조회 504"};
renderRunSummary(true); out.push(snap());
botState = {running: true, mode: "live", maintenance_error: ""};
renderRunSummary(true); out.push(snap());
console.log(JSON.stringify(out));
"""
    healthy, broken, recovered = run_js(
        PRELUDE + _whole_fn("renderRunSummary") + "\n" + driver)

    assert healthy["state"] == "live"
    assert "504" not in healthy["text"]

    assert "504" in broken["text"], "점검 실패가 화면 어디에도 없다"
    assert "손절" in broken["text"]
    assert broken["state"] == "error"

    assert "504" not in recovered["text"], "회복했는데 계속 경고한다"
    assert recovered["state"] == "live"


@pytest.mark.skipif(_engine() is None, reason="JavaScript 엔진이 없습니다")
def test_a_stale_halt_reason_does_not_land_on_a_freshly_started_bot():
    """정지된 봇을 멈추고 새로 시작하면, 옛 응답의 사유가 붙으면 안 됩니다.

    시작 핸들러는 `setRunning(true)` 를 먼저 부르고 `botState` 는 다음 폴링에서야
    갱신됩니다. 그 사이 옛 `account.halted` 를 읽으면 시작 확인 문구가 곧바로
    붉은 "계좌 대조 실패" 로 덮입니다.
    """
    driver = r"""
botState = {running: false, mode: "live",
            account: {halted: true, halt_reason: "옛 사유 — 이미 멈춘 그룹"}};
renderRunSummary(true);
console.log(JSON.stringify({notes: notes,
                            label: $("#runSafetyLabel").textContent}));
"""
    out = run_js(PRELUDE + _whole_fn("renderRunSummary") + "\n" + driver)

    assert out["notes"] == [], "새로 시작한 봇에 옛 정지 사유가 붙었다"
    assert "정지됨" not in out["label"]


# ── 확인 창의 한도는 실제로 걸릴 값이어야 한다 ───────────────────────────
@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(accounts_module, "_PBKDF2_ROUNDS", 1_000, raising=False)


@pytest.fixture
def client(tmp_path, monkeypatch):
    root = tmp_path / "templates"
    root.mkdir()
    body = template("attack-strat")
    body["limits"] = {"max_daily_notional": 500_000, "max_daily_orders": 10,
                      "max_daily_loss": 50_000}
    (root / "attack.yaml").write_text(
        yaml.safe_dump(body, allow_unicode=True), encoding="utf-8")
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
        assert c.post("/api/auth/register",
                      json={"email": "me@x.com", "password": PASSWORD}
                      ).status_code == 201
        yield c


def listed(client, strategy_id="attack"):
    rows = client.get("/api/strategies").json()["strategies"]
    return next(r for r in rows if r["id"] == strategy_id)


def test_the_listed_limits_start_from_the_template(client):
    assert listed(client)["limits"]["daily_loss"] == pytest.approx(50_000)


def test_a_saved_limit_is_what_the_confirmation_will_show(client):
    """저장한 한도가 실제로 걸리는 값입니다 — 확인 창도 그것을 보여야 합니다."""
    saved = client.post("/api/limits", json={"max_daily_loss": 10_000})
    assert saved.status_code == 200, saved.text

    limits = listed(client)["limits"]

    assert limits["daily_loss"] == pytest.approx(10_000), (
        "확인 창이 템플릿 원값을 보여 준다 — 봇은 저장값으로 돕니다")
    # 손대지 않은 항목은 템플릿 값 그대로여야 합니다.
    assert limits["daily_orders"] == 10
