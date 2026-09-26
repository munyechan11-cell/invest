"""고장 난 데스크의 "관망" 은 판단이 아닙니다.

Jev 키가 거절되면(또는 Jev 가 내려가면) 16석이 전부 실패하고, 헤드도 실패해
분석가 합의로 물러섭니다. 분석가가 한 석도 답하지 못했으니 그 합의는 빈 값 —
**확신 0% 의 관망** 입니다. 그런데 화면에는 이렇게 떴습니다:

    관망 · 확신 0% — 토론·결정 단계가 마감 내 완료되지 않아 분석가 합의로 대체

마감은 아무 상관이 없었습니다. 키가 거절된 것입니다. `/api/evaluate` 는 이 값을
HTTP 200 으로 돌려줬고, 이미 꺼진 데스크(시작 점검에서 키가 거절된)로도 16번을
더 불렀습니다. 종목 심의 창은 `degraded` 를 그리지도 않았습니다.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from quant.alpha.desk import TradingDesk
from quant.alpha.llm_client import LLMConfig, LLMError
from quant.api.server import create_app
from quant.config.schema import StrategyConfig
from quant.webapp import accounts as accounts_module
from quant.webapp.registry import UserRegistry
from tests.test_desk import ScriptedLLM, make_ctx, run_desk
from tests.test_jev import no_backoff  # noqa: F401 — 픽스처

sys.path.insert(0, str(Path(__file__).parent))

from test_api_agents import PASSWORD, SECRET, template  # noqa: E402

HTML = Path("quant/api/static/index.html").read_text(encoding="utf-8")


# ── 합의로 물러선 이유 ───────────────────────────────────────────────────────
def test_a_failed_call_is_not_blamed_on_the_deadline():
    desk = TradingDesk(ScriptedLLM(fail_seats=("head",)), memory=False)
    run_desk(desk, make_ctx())
    decision = desk.history[-1]
    assert decision.degraded
    assert "마감" not in decision.rationale
    assert "호출 실패" in decision.rationale and "simulated failure" in decision.rationale


def test_a_real_timeout_still_says_deadline():
    desk = TradingDesk(ScriptedLLM(), memory=False)
    head, _ = desk._consensus_fallback({}, {}, asyncio.TimeoutError())
    assert "마감" in head["rationale"]


# ── /api/evaluate ────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(accounts_module, "_PBKDF2_ROUNDS", 1_000, raising=False)


def _desk_template() -> dict:
    raw = template("desk-strat")
    raw["data"]["warmup_bars"] = 120                 # 심의에는 60봉 이상이 필요합니다
    raw["alpha"] = [{"type": "desk",
                     "params": {"llm": {"provider": "jev", "api_key": "test"}}}]
    return raw


@pytest.fixture
def client(tmp_path, monkeypatch):
    root = tmp_path / "templates"
    root.mkdir()
    (root / "deskstrat.yaml").write_text(
        yaml.safe_dump(_desk_template(), allow_unicode=True), encoding="utf-8")
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
        r = c.post("/api/auth/register", json={"email": "me@x.com", "password": PASSWORD})
        assert r.status_code == 201, r.text
        yield c


def _jev_desk(handler) -> TradingDesk:
    desk = TradingDesk(LLMConfig(provider="jev", api_key="test"), debate_rounds=1,
                       risk_debate_rounds=1, memory=False)
    desk.client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return desk


def _serve(monkeypatch, desk):
    monkeypatch.setattr(UserRegistry, "desk_for", lambda self, uid, cfg: (desk, False))


def _rejected(posts):
    def handler(request):
        posts.append(request)
        return httpx.Response(403, json={"error": "invalid_token"})
    return handler


def test_a_disabled_desk_answers_503_with_its_reason_and_calls_nothing(client, monkeypatch):
    posts: list = []
    desk = _jev_desk(_rejected(posts))
    asyncio.run(desk.on_start(make_ctx()))                  # 시작 점검에서 꺼짐
    assert desk.status()["enabled"] is False
    posts.clear()
    _serve(monkeypatch, desk)

    r = client.post("/api/evaluate", json={"ticker": "AAA", "strategy": "deskstrat"})
    assert r.status_code == 503, r.text
    assert "꺼져 있습니다" in r.json()["detail"] and "Jev" in r.json()["detail"]
    assert posts == []                                      # 한 번도 부르지 않았다


def test_a_desk_whose_every_analyst_failed_answers_503_not_a_hold(client, monkeypatch):
    """시작 점검을 안 거친 새 데스크(`desk_for`)에 거절된 키 — 모든 좌석이 실패."""
    posts: list = []
    _serve(monkeypatch, _jev_desk(_rejected(posts)))

    r = client.post("/api/evaluate", json={"ticker": "AAA", "strategy": "deskstrat"})
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "Jev" in detail and "8석" in detail and "jev 403" in detail
    assert "관망" not in detail and "마감" not in detail
    assert posts                                            # 불러 봤고, 실패를 말한다


def test_the_ask_panel_shows_a_degraded_deliberation():
    m = re.search(r"function renderAsk\(d, ticker\) \{(.*?)\n\}", HTML, re.S)
    assert m, "renderAsk 가 없습니다"
    assert "d.degraded" in m.group(1) and "축약" in m.group(1)


def test_the_fallback_reason_reaches_the_llm_error_text():
    """`LLMError` 로 물러설 때 원인 문장이 근거에 실립니다(한 줄로, 주소 없이)."""
    desk = TradingDesk(ScriptedLLM(), memory=False)
    head, _ = desk._consensus_fallback(
        {}, {}, LLMError("jev 503: upstream down https://x.test/a?k=secret"))
    assert "jev 503" in head["rationale"] and "secret" not in head["rationale"]


# ── 4차 점검 ────────────────────────────────────────────────────────────────
def test_evaluate_closes_the_desk_it_built_for_the_request(client, monkeypatch):
    """봇이 없으면 요청마다 새 데스크(새 클라이언트·연결 풀)를 세웁니다. 예전에는
    닫지 않아, 요청마다 연결 여럿이 순환 참조 수거 때까지 열려 있었습니다."""
    from tests.test_jev import FakeJev, desk_answers

    desk = _jev_desk(FakeJev(desk_answers("bullish")))
    _serve(monkeypatch, desk)
    r = client.post("/api/evaluate", json={"ticker": "AAA", "strategy": "deskstrat"})
    assert r.status_code == 200, r.text
    assert desk.client._client.is_closed

    failing = _jev_desk(_rejected([]))                      # 실패한 요청도 닫습니다
    _serve(monkeypatch, failing)
    r = client.post("/api/evaluate", json={"ticker": "AAA", "strategy": "deskstrat"})
    assert r.status_code == 503, r.text
    assert failing.client._client.is_closed


@pytest.mark.parametrize("value", [65, "0.65x"])
def test_a_mistyped_desk_knob_is_not_reported_as_a_missing_key(client, monkeypatch, value):
    """`llm.extra.undecided_below` 를 65(백분율)로 적으면 데스크를 세울 때 거절됩니다.
    봇 시작은 그 `LLMError` 를 "쓸 수 있는 Jev 키가 없습니다" 로 적고 원문을 버려,
    사람이 멀쩡한 키를 확인하러 갔습니다."""
    import os

    raw = _desk_template()
    raw["alpha"] = [{"type": "desk", "params": {"llm": {
        "provider": "jev", "api_key": "test", "extra": {"undecided_below": value}}}}]
    root = Path(os.environ["QUANT_CONFIG_DIR"])
    (root / "deskknob.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True),
                                         encoding="utf-8")
    r = client.post("/api/trader/start", json={"config_path": "deskknob"})
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "키가 없습니다" not in detail, detail
    assert "undecided_below" in detail and "키가 없어서가 아닙니다" in detail


def test_a_missing_key_is_still_reported_as_a_missing_key(client, monkeypatch):
    import os

    monkeypatch.delenv("JEV_API_KEY", raising=False)
    raw = _desk_template()
    raw["alpha"] = [{"type": "desk", "params": {"llm": {"provider": "jev"}}}]
    root = Path(os.environ["QUANT_CONFIG_DIR"])
    (root / "desknokey.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True),
                                          encoding="utf-8")
    r = client.post("/api/trader/start", json={"config_path": "desknokey"})
    assert r.status_code == 503, r.text
    assert "쓸 수 있는 Jev 키가 없습니다" in r.json()["detail"]


def test_an_outage_is_not_reported_as_already_deliberated(no_backoff):  # noqa: F811 — 가져온 fixture
    """Jev 가 전부 503 이면 응답이 온 호출이 없어 LLM 호출 수가 그대로입니다.
    그걸 "새로 볼 것이 없었다" 로 읽어, 모든 좌석이 실패한 심의가 "이 봉은 이미
    심의했습니다" 로 적혔습니다."""
    import types

    from quant.live.trader import LiveTrader
    from tests.test_jev import PREFLIGHT_ANSWERS, FakeJev, is_preflight

    def answer(name, args):
        if is_preflight(args):
            return {"answers": PREFLIGHT_ANSWERS}
        return httpx.Response(503, text="Service Unavailable")

    desk = _jev_desk(FakeJev(answer))
    ctx = make_ctx()
    stub = types.SimpleNamespace(
        desk=lambda: desk, desk_note="",
        engine=types.SimpleNamespace(
            ctx=ctx, insights=types.SimpleNamespace(add=lambda fresh: None),
            ledger=types.SimpleNamespace(record=lambda c, fresh: None)))

    async def go():
        await desk.on_start(ctx)
        await LiveTrader._deliberate_now(stub, "개장 전")

    asyncio.run(go())
    assert desk.status()["enabled"] is True
    assert "이미 심의했습니다" not in stub.desk_note, stub.desk_note
    assert stub.desk_note.startswith("개장 전 심의 — AI 호출이 실패했습니다"), stub.desk_note
    assert "jev 503" in stub.desk_note
