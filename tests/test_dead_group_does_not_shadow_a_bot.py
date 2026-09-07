"""멈춘 그룹이 살아 있는 단일 봇을 가리면 안 된다.

`stop_group` 은 죽은 에이전트의 사유를 화면이 읽을 수 있게 그룹 객체를 남겨
둡니다. 그 뒤 같은 사람이 단일 봇을 시작하면 `registry.status()` 가 그룹을
먼저 보고 `running: False` 와 죽은 `agents` 배열을 답했습니다.

화면에서 벌어지는 일:
 · `/api/health` 는 `trader_running: true`, `/api/status` 는 `running: false`
   → 뒤에 온 쪽이 이겨 **■정지 버튼이 사라집니다.**
 · `adoptAgents` 가 죽은 에이전트 id 를 `activeAgent` 로 잡고, `withAgent()` 가
   그 이름을 **모든 돈 경로** 에 붙입니다 → 매도·청산·일시정지가 전부 404.

즉 실거래 봇이 도는데 화면에서 멈추거나 포지션을 줄일 수단이 하나도 남지
않습니다.
"""
from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from quant.api.server import create_app
from quant.config.schema import StrategyConfig
from quant.webapp import accounts as accounts_module

from .test_api_agents import PASSWORD, SECRET, spec, start_group, template


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(accounts_module, "_PBKDF2_ROUNDS", 1_000, raising=False)


@pytest.fixture
def client(tmp_path, monkeypatch):
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
        assert c.post("/api/auth/register",
                      json={"email": "me@x.com", "password": PASSWORD}
                      ).status_code == 201
        yield c


def group_then_single(client):
    assert start_group(client, spec("attack", "attack", 0.5),
                       spec("defend", "defend", 0.5)).status_code == 200
    assert client.post("/api/trader/stop").status_code == 200
    started = client.post("/api/trader/start", json={"config_path": "attack"})
    assert started.status_code == 200, started.text
    return started


def test_the_running_bot_is_what_status_reports(client):
    try:
        group_then_single(client)
        status = client.get("/api/status").json()

        assert status["running"] is True, "죽은 그룹이 살아 있는 봇을 가렸다"
        assert "portfolio" in status
    finally:
        client.post("/api/trader/stop")


def test_health_and_status_agree(client):
    """두 응답이 어긋나면 화면이 정지 버튼을 감춥니다."""
    try:
        group_then_single(client)
        health = client.get("/api/health").json()
        status = client.get("/api/status").json()

        assert health["trader_running"] == status["running"]
    finally:
        client.post("/api/trader/stop")


def test_the_screen_is_not_handed_dead_agent_ids(client):
    """`agents` 가 남아 있으면 화면이 그 이름을 모든 돈 경로에 붙입니다."""
    try:
        group_then_single(client)
        status = client.get("/api/status").json()

        assert not status.get("agents"), "죽은 에이전트 목록이 화면으로 나갔다"
    finally:
        client.post("/api/trader/stop")


def test_the_bot_can_still_be_stopped(client):
    """이 결함의 실제 피해 — 멈출 손잡이가 남아 있는가."""
    group_then_single(client)

    stopped = client.post("/api/trader/stop")

    assert stopped.status_code == 200, stopped.text


def test_a_live_group_still_wins_over_a_dead_bot(client):
    """반대 방향은 그대로여야 합니다 — 돌고 있는 그룹이 우선입니다."""
    try:
        assert client.post("/api/trader/start",
                           json={"config_path": "attack"}).status_code == 200
        assert client.post("/api/trader/stop").status_code == 200
        assert start_group(client, spec("attack", "attack", 0.5),
                           spec("defend", "defend", 0.5)).status_code == 200

        status = client.get("/api/status").json()

        assert [a["agent_id"] for a in status["agents"]] == ["attack", "defend"]
    finally:
        client.post("/api/trader/stop")
