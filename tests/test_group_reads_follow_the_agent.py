"""그룹이 도는 동안 **조회** 가 어느 에이전트를 보는가.

돈이 움직이는 경로는 에이전트가 둘 이상이면 되묻습니다(`AgentRequired`, 400).
조회는 되물을 수 없습니다 — 그래서 `trader("")` 가 None 일 때 `run_config("")`
는 **프로세스 기본 템플릿**(운영 배포에서는 데모)으로 물러섰고, 그룹이 도는
동안 봉·검색·심의·수급이 전부 데모 전략의 종목을 봤습니다. 봇은 토스 미국주식
을 사는데 차트는 "AAA 는 이 전략의 종목이 아닙니다" 를 띄우는 화면입니다.

여기서 고정하는 것:

  · `agent_id` 없는 조회는 전략 이름이 맞는 에이전트, 없으면 첫 에이전트를 본다
  · `/api/desk`·`/api/flow`·`/api/trader/sync` 가 `agent_id` 를 받는다
  · 화면은 수급·데스크 조회에 고른 에이전트를 붙인다
  · 돈이 움직이는 경로는 여전히 되묻는다 — 기본값이 그쪽으로 새지 않는다

프로세스 템플릿과 에이전트 템플릿의 종목을 일부러 다르게 둡니다. 같으면
어느 설정을 읽었는지 응답으로 구별할 수 없어, 고친 것을 되돌려도 통과합니다.
브로커 엔드포인트는 어디서도 부르지 않습니다 — synthetic 시세 + paper 브로커.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from quant.api.server import UserDesk, create_app
from quant.config.schema import StrategyConfig
from quant.webapp import accounts as accounts_module

sys.path.insert(0, str(Path(__file__).parent))

from test_api_agents import PASSWORD, SECRET, spec, start_group, template  # noqa: E402

HTML = Path("quant/api/static/index.html").read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))

#: 템플릿마다 다른 종목 — 응답이 어느 설정을 읽었는지 이것으로 갈립니다.
TICKERS = {"운영자": "AAA", "attack": "ATK", "defend": "DEF"}


def _template(name: str) -> dict:
    raw = template(f"{name}-strat" if name != "운영자" else name)
    raw["universe"] = {"symbols": [{"ticker": TICKERS[name], "venue": "SIM"}]}
    return raw


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(accounts_module, "_PBKDF2_ROUNDS", 1_000, raising=False)


@pytest.fixture
def client(tmp_path, monkeypatch):
    root = tmp_path / "templates"
    root.mkdir()
    for name in ("attack", "defend"):
        (root / f"{name}.yaml").write_text(
            yaml.safe_dump(_template(name), allow_unicode=True), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUANT_SECRET_KEY", SECRET)
    monkeypatch.setenv("QUANT_USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setenv("QUANT_USER_DATA", str(tmp_path / "userdata"))
    monkeypatch.setenv("QUANT_ENV_FILE", str(tmp_path / "env.test"))
    monkeypatch.setenv("QUANT_CONFIG_DIR", str(root))
    monkeypatch.delenv("QUANT_API_TOKEN", raising=False)

    app = create_app(StrategyConfig.model_validate(_template("운영자")),
                     state_path=str(tmp_path / "state.db"))
    with TestClient(app, base_url="https://desk.example") as c:
        r = c.post("/api/auth/register",
                   json={"email": "me@x.com", "password": PASSWORD})
        assert r.status_code == 201, r.text
        yield c


@pytest.fixture
def group(client):
    r = start_group(client, spec("attack", "attack"), spec("defend", "defend"))
    assert r.status_code == 200, r.text
    try:
        yield client
    finally:
        client.post("/api/trader/stop")


# ── 봉·검색은 도는 에이전트의 종목을 안다 ───────────────────────────────
def test_candles_read_the_running_agent_not_the_process_template(group):
    """ATK 는 첫 에이전트의 종목이고 프로세스 템플릿에는 없습니다."""
    r = group.get("/api/candles", params={"ticker": "ATK"})
    assert r.status_code == 200, r.text


def test_candles_follow_the_strategy_name_to_the_matching_agent(group):
    r = group.get("/api/candles", params={"ticker": "DEF", "strategy": "defend"})
    assert r.status_code == 200, r.text


def test_lookup_knows_the_running_agents_symbols(group):
    """검색은 "전략에 들어 있는 종목" 부터 찾습니다 — 그 전략이 데모면 봇이
    사는 종목을 이름으로 못 찾습니다."""
    found = {row["ticker"] for row in group.get(
        "/api/lookup", params={"q": "ATK"}).json()["results"]}
    assert "ATK" in found


# ── 수급·데스크·심의 ─────────────────────────────────────────────────────
def test_flow_does_not_claim_the_bot_is_off_while_a_group_runs(group):
    """"자동매매가 돌고 있지 않습니다" 는 돌고 있는 그룹 앞에서 거짓말입니다."""
    for params in ({}, {"agent_id": "defend"}):
        body = group.get("/api/flow", params=params).json()
        assert "돌고 있지 않습니다" not in body.get("message", ""), body


def test_desk_reads_go_to_the_chosen_or_first_agent(group, monkeypatch):
    asked: list[str] = []

    def desk_model(self, agent_id=""):
        asked.append(agent_id)
        return None

    monkeypatch.setattr(UserDesk, "desk_model", desk_model)
    group.get("/api/desk?limit=1")
    group.get("/api/desk", params={"limit": 1, "agent_id": "defend"})
    group.get("/api/desk/ATK", params={"agent_id": "defend"})
    assert asked == ["attack", "defend", "defend"]


def test_evaluate_deliberates_with_the_running_agents_strategy(group):
    """되돌리면 400 이 "'운영자' 전략에는 AI 데스크가 없습니다" 가 됩니다 —
    봇이 도는데 데모 전략으로 심의한다는 뜻입니다. 데스크가 없는 전략이라
    LLM 에 닿기 전에 끝납니다."""
    r = group.post("/api/evaluate", json={"ticker": "ATK"})
    assert r.status_code == 400 and "attack-strat" in r.json()["detail"], r.text
    r = group.post("/api/evaluate", json={"ticker": "DEF", "strategy": "defend"})
    assert r.status_code == 400 and "defend-strat" in r.json()["detail"], r.text


# ── 동기화는 이름을 받아야 되물은 보람이 있다 ────────────────────────────
def test_sync_accepts_the_agent_it_asked_for(group):
    r = group.post("/api/trader/sync")
    assert r.status_code == 400 and r.json()["code"] == "agent_required"
    r = group.post("/api/trader/sync", params={"agent_id": "attack"})
    assert r.status_code == 200, r.text


# ── 기본값이 돈 경로로 새지 않는다 ───────────────────────────────────────
@pytest.mark.parametrize("path", ["/api/manual/close_all", "/api/manual/pause",
                                  "/api/trader/sync"])
def test_money_paths_still_ask_which_agent(group, path):
    r = group.post(path)
    assert r.status_code == 400 and r.json()["code"] == "agent_required", r.text


# ── 화면은 고른 에이전트를 붙인다 ────────────────────────────────────────
def test_the_screen_sends_the_agent_with_flow_and_desk_reads():
    """맨손으로 부르면 서버가 첫 에이전트를 골라, 탭은 보수형인데 심의는
    공격형의 것이 재생됩니다."""
    m = re.search(r"async function refresh\([^)]*\) \{.*?\n\}", SCRIPT, re.S)
    assert m, "refresh 함수를 찾지 못했습니다"
    refresh = m.group(0)
    assert 'withAgent("/api/flow")' in refresh
    assert 'withAgent("/api/desk?limit=1")' in refresh
    assert 'api("/api/flow")' not in refresh
    assert 'api("/api/desk?limit=1")' not in refresh
