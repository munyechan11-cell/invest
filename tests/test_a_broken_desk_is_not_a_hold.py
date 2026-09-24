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
