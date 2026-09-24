"""답하지 못한 좌석은 판단이 아닙니다 — 화면·API·스크립트가 그렇게 말하는가.

좌석은 실패해도 대체값을 냅니다(분석가는 "판단 재료 부족·중립 0%", 트레이더는
"즉시 시장가", 리스크 토론은 "배율 50%"). 데스크가 멈추지 않게 하려는 것인데,
그 값을 읽는 쪽이 실패를 모르면 **실패가 정직한 판단으로 읽힙니다**:

* Jev 가 분석 단계만 막아도 말풍선 여덟 개가 "데이터 부족 — 판단 보류" 였고,
  좌석의 오류 문장은 화면 어디에도 없었습니다. 헤드가 답했으면 `/api/evaluate`
  는 투표 0석의 결정을 HTTP 200 으로 돌려줬습니다.
* 심의 도중 Jev 잔액이 떨어지면 데스크는 "기다려도 풀리지 않습니다" 를 적고
  꺼지는데, `/api/evaluate` 와 `desk_live_check.py` 는 그 이유를 읽지 않고
  "마감 시간을 넘겼거나 … 잠시 후 다시 시도하세요" 라고 했습니다.
* 데스크가 꺼진 이유 뒤에 화면은 언제나 "LLM 키와 한도를 확인하세요" 를 붙여,
  주소가 틀린 사람도 키를 보러 갔습니다.

네트워크는 나가지 않습니다(가짜 Jev). 화면 함수는 자바스크립트 엔진에 넣어
**실행** 합니다 — 엔진이 없으면 그 검사만 건너뜁니다.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from quant.alpha.desk import TradingDesk
from quant.alpha.llm_client import LLMConfig, LLMError, LLMUsage
from tests.test_a_broken_desk_is_not_a_hold import (  # noqa: F401 — 픽스처
    _jev_desk,
    _serve,
    client,
    fast_hashing,
)
from tests.test_desk import ScriptedLLM, make_ctx, run_desk
from tests.test_desk_wait_reason_behaviour import DESK, _engine
from tests.test_desk_wait_reason_behaviour import _run as run_wait_reason
from tests.test_jev import (
    PREFLIGHT_ANSWERS,
    FakeJev,
    desk_answers,
    is_preflight,
    seat_calls,
    tool_error,
)

HTML = Path("quant/api/static/index.html").read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))


def _fake_jev_desk(answer) -> tuple[TradingDesk, FakeJev]:
    fake = FakeJev(answer)
    return _jev_desk(fake), fake


def _analysts_throttled(name, args):
    """분석 단계(8석 동시)만 막히고 뒷좌석은 답하는 Jev."""
    if name == "jev_evaluate" and "stance" in args["questions"]:
        return tool_error("MCP error -32602: Invalid arguments for tool jev_evaluate")
    return desk_answers("bullish")(name, args)


def _broke(name, args):
    """시작 점검은 통과했고, 그 뒤 첫 좌석에서 잔액이 떨어진 Jev."""
    if is_preflight(args):
        return {"answers": PREFLIGHT_ANSWERS}
    return tool_error("Insufficient funds: top up your balance")


# ── 결정에 실패한 좌석 수가 실린다 ───────────────────────────────────────────
def test_a_decision_counts_the_seats_that_did_not_answer():
    desk = TradingDesk(ScriptedLLM(fail_seats=("analyst", "trade")), memory=False)
    run_desk(desk, make_ctx())
    out = desk.history[-1].to_dict()
    assert out["seat_failures"] == 8 + 1                     # 분석가 8 + 트레이더
    assert "simulated failure for analyst" in out["first_seat_error"]
    healthy = TradingDesk(ScriptedLLM(), memory=False)
    run_desk(healthy, make_ctx())
    assert healthy.history[-1].to_dict()["seat_failures"] == 0
    assert healthy.history[-1].to_dict()["first_seat_error"] == ""


def test_a_decision_carries_what_it_cost():
    """비용은 심의마다 계산돼 있었는데(`UsageTally`) API 로 나가지 않았습니다."""
    desk, _ = _fake_jev_desk(desk_answers("bullish"))
    run_desk(desk, make_ctx())
    out = desk.history[-1].to_dict()
    assert out["cost_usd"] == pytest.approx(16 * 677 / 1e6 * 0.042, abs=1e-6)
    assert out["cost_usd"] > 0


# ── /api/evaluate ────────────────────────────────────────────────────────────
def test_evaluate_refuses_a_decision_made_with_no_analyst(client, monkeypatch):  # noqa: F811 — 가져온 fixture
    """헤드는 답했지만 분석가는 한 석도 답하지 못했습니다 — 투표 0석의 결정."""
    desk, fake = _fake_jev_desk(_analysts_throttled)
    _serve(monkeypatch, desk)
    r = client.post("/api/evaluate", json={"ticker": "AAA", "strategy": "deskstrat"})
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "8석이 모두 실패" in detail and "jev 422" in detail
    # 헤드는 답했습니다 — 축약(합의 대체)이 아닌 결정이었는데도 거절합니다.
    assert [c for c in seat_calls(fake)
            if c["params"]["arguments"]["state"]["seat"] == "Head of Desk"]


def test_evaluate_says_the_desk_turned_off_mid_request(client, monkeypatch):  # noqa: F811 — 가져온 fixture
    """봇이 없으면 요청마다 새 데스크입니다(시작 점검 없음). 잔액이 떨어지면
    데스크는 이유를 적고 꺼지는데, 예전에는 그것을 읽지 않고 "잠시 후 다시
    시도하세요" 라고 했습니다 — 몇 번을 눌러도 같은 말이었습니다."""
    for _ in range(2):
        desk, _ = _fake_jev_desk(_broke)
        _serve(monkeypatch, desk)
        r = client.post("/api/evaluate", json={"ticker": "AAA", "strategy": "deskstrat"})
        assert r.status_code == 503, r.text
        detail = r.json()["detail"]
        assert detail.startswith("AI 데스크가 꺼져 있습니다")
        assert "Jev" in detail and "기다려도 풀리지 않습니다" in detail
        assert "잠시 후 다시 시도" not in detail and "마감" not in detail


def test_evaluate_meters_this_requests_jev_calls(client, monkeypatch):  # noqa: F811 — 가져온 fixture
    """계량은 소스에 `record_spend` 가 있는지로만 검사되고 있었습니다 — 값을
    0 으로 바꿔도 모든 테스트가 통과했습니다. 실제로 적히는지 봅니다."""
    desk, fake = _fake_jev_desk(desk_answers("bullish"))
    _serve(monkeypatch, desk)
    r = client.post("/api/evaluate", json={"ticker": "AAA", "strategy": "deskstrat"})
    assert r.status_code == 200, r.text
    assert len(seat_calls(fake)) == 16
    app = client.app
    user = app.state.accounts.by_email("me@x.com")
    today = app.state.registry.usage.today(user.id)
    assert today["deliberations"] == 1 and today["llm_calls"] == 16
    assert today["cost_usd"] > 0
    assert r.json()["metered"]["llm_calls"] == 16
    assert r.json()["cost_usd"] > 0 and r.json()["seat_failures"] == 0


# ── desk_live_check.py ───────────────────────────────────────────────────────
def test_the_live_check_says_the_desk_turned_off_mid_run(monkeypatch):
    from tests.test_desk_live_check_reports_failures import run_script

    code, out, _ = run_script(monkeypatch, _broke)
    assert code == 2, out
    assert "심의 도중 데스크가 꺼졌습니다" in out
    assert "기다려도 풀리지 않습니다" in out
    assert "마감시간 초과 또는 데이터 부족" not in out


# ── 화면 ─────────────────────────────────────────────────────────────────────
pytestmark_js = pytest.mark.skipif(_engine() is None, reason="자바스크립트 엔진이 없습니다")


def _grab(pattern: str) -> str:
    m = re.search(pattern, SCRIPT, re.S)
    assert m, f"화면 코드에서 찾지 못했습니다: {pattern[:40]}"
    return m.group(0)


def _fn(name: str, is_async: bool = False) -> str:
    prefix = "async function" if is_async else "function"
    return _grab(rf"\n{prefix} {name}\([^)]*\) \{{.*?\n\}}")


def _const(name: str) -> str:
    return _grab(rf"\nconst {name} = .*?;\n")


def _node(src: str):
    path, args = _engine()
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write("var write = (typeof console !== 'undefined' && console.log) "
                 "? console.log : print;\n" + src)
        js = fh.name
    try:
        proc = subprocess.run([path, *args, js], capture_output=True, text=True,
                              timeout=60)
        assert proc.returncode == 0, proc.stderr or proc.stdout
        return json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        Path(js).unlink(missing_ok=True)


def _decision(**override) -> dict:
    desk = TradingDesk(ScriptedLLM(**override.pop("llm", {})), memory=False)
    run_desk(desk, make_ctx())
    out = desk.history[-1].to_dict()
    out.pop("brief", None)
    out.update(override)
    return out


@pytestmark_js
def test_the_floor_says_a_failed_seat_failed():
    """실패한 분석가가 "데이터 부족 — 판단 보류" 로, 실패한 트레이더가 "즉시
    시장가" 로 그려지던 자리. 오류가 있으면 그것부터 말합니다."""
    d = _decision(llm={"fail_seats": ("analyst", "trade", "risk_debate")})
    src = "\n".join([
        "var said = [];",
        "function say(key, text, tone) { said.push([key, text, tone]); }",
        "function hush() {} function resetFloor() {} function syncPlayBar() {}",
        "function renderVerdict() {}",
        "function sleep() { return Promise.resolve(); }",
        "function symLabel(s) { return s; } function nameOf() { return ''; }",
        "var node = {textContent: '', classList: {add: function(){}, remove: function(){}}};",
        "var $ = function () { return node; };",
        "var playing = false, playGeneration = 0, playSpeed = 1, lastPlayed = null;",
        _const("ANALYSTS").replace("const ", "var ", 1),
        _const("TONE"), _const("STANCE_KO"), _const("ACTION_STYLE"), _const("actionKo"),
        _grab(r"\nconst ENTRY_STYLE_KO = \{.*?\};\n"),
        _fn("seatFailure"), _fn("playDeliberation", is_async=True),
        f"playDeliberation({json.dumps(d, ensure_ascii=False)}, {{}})"
        ".then(function () { write(JSON.stringify(said)); });",
    ])
    said = {key: (text, tone) for key, text, tone in _node(src)}
    for key in ("technical", "flow", "quant", "trader", "risk_aggressive",
                "risk_conservative"):
        text, tone = said[key]
        assert text.startswith("응답 실패 — ") and "simulated failure" in text, (key, text)
        assert tone == "bad", key
    assert "데이터 부족" not in said["technical"][0]
    assert "시장가" not in said["trader"][0] and "배율" not in said["risk_aggressive"][0]
    # 답한 좌석은 예전 그대로입니다.
    assert said["bull"][0] == "scripted argument"
    assert said["research_manager"][0].startswith("[매수]")


@pytestmark_js
def test_the_verdict_and_the_ask_panel_count_failed_seats_and_show_the_cost():
    d = _decision(llm={"fail_seats": ("analyst",)}, cost_usd=0.0013,
                  metered={"llm_calls": 16, "cost_usd": 0.0013, "billed_to": "service"})
    src = "\n".join([
        "var box = {innerHTML: '', className: '', hidden: true,"
        " classList: {add: function(){}, remove: function(){}}};",
        "var $ = function () { return box; };",
        "function followDesk() {} function symLabel(s) { return s; }",
        "function nameOf() { return ''; } var botState = null;",
        _const("esc"), _const("ACTION_STYLE"), _grab(r"\nconst ACT_KO = \{.*?\};\n"),
        _grab(r"\nconst pct2 = .*?;\n"),
        _fn("nothingToSell"), _fn("renderVerdict"), _fn("renderAsk"),
        f"var d = {json.dumps(d, ensure_ascii=False)};",
        "renderVerdict(d, false); var verdict = box.innerHTML;",
        "renderAsk(d, 'AAA'); write(JSON.stringify([verdict, box.innerHTML]));",
    ])
    verdict, ask = _node(src)
    assert "좌석 8곳 실패" in verdict
    assert "AI 호출" in verdict and "$0.0013" in verdict
    assert "좌석 8곳이 답하지 못했습니다" in ask and "simulated failure" in ask


@pytestmark_js
def test_a_disabled_desk_shows_its_own_reason_without_a_key_hint():
    """주소가 틀린 사람에게 "LLM 키와 한도를 확인하세요" 는 틀린 길입니다."""
    reason = "Jev 주소를 찾을 수 없습니다 — 설정의 llm.base_url 을 확인하세요"
    [got] = run_wait_reason([{
        "strategies": [DESK], "picked": "kr_toss_desk", "running": True,
        "deskState": {"disabled_reason": reason, "deliberations": []},
        "botState": {"universe": ["005930"],
                     "market": {"calendar": "krx", "open": True, "minutes_to_open": 0}},
    }])
    assert got["kind"] == "broken" and got["line"] == reason


def test_every_disabled_reason_carries_its_own_next_step():
    """화면이 뒤에 붙이던 안내를 뺐으므로, 서버의 이유마다 할 일이 있어야 합니다."""
    class Fail:
        def __init__(self, provider, exc):
            self.usage = LLMUsage()
            self.config = LLMConfig(provider=provider, api_key="x")
            self.exc = exc

        async def complete(self, *a, **k):
            raise self.exc

    for provider, exc in (("google", LLMError("google 500: internal")),
                          ("jev", LLMError("jev 406: Not Acceptable")),
                          ("google", RuntimeError("boom"))):
        desk = TradingDesk(Fail(provider, exc), memory=False)
        asyncio.run(desk.on_start(make_ctx()))
        reason = desk.status()["disabled_reason"]
        assert "확인하세요" in reason, reason


# ── 데모는 Jev 데스크가 내는 모양이다 ────────────────────────────────────────
def test_the_demo_shows_what_the_shipped_desk_actually_produces():
    """'어떻게 도는지 보기' 가 호출 19회·토론 2라운드·서술형 논거를 보여 줬습니다.
    출하 데스크(Jev)는 16회, 1라운드, 확률을 적은 정해진 문장입니다."""
    demo = _grab(r"\nconst DEMO = \{.*?\n\};\n")
    assert "llm_calls: 16" in demo and "llm_calls: 19" not in demo
    assert "round: 2" not in demo and demo.count("round: 1") == 2   # 토론·리스크 1라운드
    assert "cost_usd: 0.0013" in demo
    texts = re.findall(r'(?:argument|reasoning|rationale|execution_note): "([^"]*)"', demo)
    assert len(texts) >= 6
    for text in texts:
        assert text.endswith("(Jev 확률 판정)"), text
    assert "체결 가능성:" in demo                             # 미시구조 좌석의 본업


def test_cost_notes_use_the_jev_price():
    server = Path("quant/api/server.py").read_text(encoding="utf-8")
    usage = Path("quant/webapp/usage.py").read_text(encoding="utf-8")
    evaluate = server[server.index('@app.post("/api/evaluate")'):]
    evaluate = evaluate[:evaluate.index('"""', evaluate.index('"""') + 3)]
    assert "$0.0013" in evaluate and "심의 한 번이 약 $0.06" not in evaluate
    assert "$0.0013" in usage and "심의 한 번이 약 $0.06 입니다" not in usage
