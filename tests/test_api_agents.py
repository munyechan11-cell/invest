"""화면에서 에이전트를 켠다 — 그리고 어느 에이전트인지 되묻는다.

API 계층이 지켜야 하는 것은 두 가지입니다.

**되묻기.** 에이전트가 여럿인데 `agent_id` 없이 `close_all` 을 부르면 400 으로
되물어야 합니다. 조용히 하나만 정리하고 200 을 돌려주면, 사용자는 전부 정리된
줄 알고 화면을 닫고 나머지는 그대로 시장에 남습니다.

**에이전트마다 따로.** 성향도 한도도 실거래 확인도 전부 에이전트별입니다. 하나를
확인했다고 나머지가 열리면, 관찰용으로 넣은 에이전트가 진짜 주문을 내고 있다는
사실을 사용자가 모릅니다.

브로커 엔드포인트는 어디서도 부르지 않습니다 — synthetic 시세 + paper 브로커.
"""
from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from quant.api.server import create_app
from quant.config.schema import StrategyConfig
from quant.webapp import accounts as accounts_module

SECRET = "api-agents-test-secret-0123456789abcdefgh"
PASSWORD = "korea-invest-1"


def template(name):
    return {
        "name": name, "mode": "dry_run",
        "data": {"provider": "synthetic", "timeframe": "1d",
                 "calendar": "always_open", "warmup_bars": 30,
                 "params": {"seed": 1}},
        "universe": {"symbols": [{"ticker": "AAA", "venue": "SIM"}]},
        "alpha": [{"type": "ema_cross"}],
        "portfolio": {"starting_cash": 50000, "cash_reserve_pct": 0.0},
        "execution": {"min_order_notional": 1.0},
        "broker": {"type": "paper"},
    }


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(accounts_module, "_PBKDF2_ROUNDS", 1_000, raising=False)


@pytest.fixture
def client(tmp_path, monkeypatch):
    root = tmp_path / "templates"
    root.mkdir()
    for name in ("attack", "defend", "third"):
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


def spec(agent_id, config="attack", weight=0.5, mode="dry_run", **kw):
    return {"agent_id": agent_id, "label": f"{agent_id} 라벨",
            "config_path": config, "capital_weight": weight,
            "mode": mode, **kw}


def start_group(client, *specs):
    return client.post("/api/trader/group/start", json={"agents": list(specs)})


# ── 그룹 시작 ────────────────────────────────────────────────────────────
def test_a_group_of_two_starts_and_reports_both(client):
    r = start_group(client, spec("attack", "attack"), spec("defend", "defend"))
    assert r.status_code == 200, r.text

    body = r.json()
    assert [a["agent_id"] for a in body["agents"]] == ["attack", "defend"]
    # 통화는 설정에서 옵니다. 여기에 박아 두면 다른 통화 계좌의 잔고 조회가
    # 언제나 빈손이라 자본 배분이 전원 0 이 되고, 에이전트는 자기 몫이 없는
    # 줄 알고 아무것도 사지 않습니다 — 에러도 로그도 없이.
    assert sum(body["agents"][0]["allocated"].values()) == 25_000
    client.post("/api/trader/stop")


def test_the_status_endpoint_reports_the_group(client):
    start_group(client, spec("attack", "attack"), spec("defend", "defend"))
    try:
        body = client.get("/api/status").json()
        assert [a["agent_id"] for a in body["agents"]] == ["attack", "defend"]
        assert body["account"]["halted"] is False
    finally:
        client.post("/api/trader/stop")


def test_capital_weights_over_one_hundred_percent_are_refused(client):
    r = start_group(client, spec("attack", "attack", 0.7),
                    spec("defend", "defend", 0.7))
    assert r.status_code == 400
    assert "100%" in r.json()["detail"]


def test_more_than_four_agents_is_refused_by_the_schema(client):
    r = start_group(client, *[spec(f"a{i}", "attack", 0.2) for i in range(5)])
    assert r.status_code == 422


def test_duplicate_agent_ids_are_refused(client):
    r = start_group(client, spec("attack", "attack", 0.4),
                    spec("attack", "defend", 0.4))
    assert r.status_code == 400
    assert "겹칩니다" in r.json()["detail"]


def test_a_second_group_is_refused_while_one_runs(client):
    start_group(client, spec("attack", "attack"), spec("defend", "defend"))
    try:
        r = start_group(client, spec("third", "third", 1.0))
        assert r.status_code == 409
        assert r.json()["code"] == "already_running"
    finally:
        client.post("/api/trader/stop")


# ── 어느 에이전트인지 되묻는다 ───────────────────────────────────────────
def test_close_all_without_an_agent_asks_which_one(client):
    """조용히 하나만 정리하고 200 을 돌려주면, 사용자는 전부 정리된 줄 알고
    화면을 닫습니다."""
    start_group(client, spec("attack", "attack"), spec("defend", "defend"))
    try:
        r = client.post("/api/manual/close_all")
        assert r.status_code == 400
        body = r.json()
        assert body["code"] == "agent_required"
        assert set(body["agents"]) == {"attack", "defend"}
    finally:
        client.post("/api/trader/stop")


def test_naming_the_agent_lets_the_order_through(client):
    start_group(client, spec("attack", "attack"), spec("defend", "defend"))
    try:
        r = client.post("/api/manual/close_all", params={"agent_id": "attack"})
        assert r.status_code == 200, r.text
        assert "queued" in r.json()
    finally:
        client.post("/api/trader/stop")


def test_pause_also_asks(client):
    start_group(client, spec("attack", "attack"), spec("defend", "defend"))
    try:
        assert client.post("/api/manual/pause").status_code == 400
        assert client.post(
            "/api/manual/pause", params={"agent_id": "defend"}
        ).status_code == 200
    finally:
        client.post("/api/trader/stop")


def test_a_single_agent_group_does_not_ask(client):
    """답이 하나뿐인 질문은 사용자에게 일만 늘립니다."""
    start_group(client, spec("solo", "attack", 1.0))
    try:
        assert client.post("/api/manual/pause").status_code == 200
    finally:
        client.post("/api/trader/stop")


# ── 성향과 한도는 에이전트마다 ───────────────────────────────────────────
def test_profiles_are_saved_per_agent(client):
    a = client.patch("/api/profile", params={"agent_id": "attack"},
                     json={"overrides": {"R": 1.0}})
    d = client.patch("/api/profile", params={"agent_id": "defend"},
                     json={"overrides": {"R": -1.0}})
    assert a.status_code == 200 and d.status_code == 200

    assert client.get("/api/profile", params={"agent_id": "attack"}
                      ).json()["axes"][0]["value"] == 1.0
    assert client.get("/api/profile", params={"agent_id": "defend"}
                      ).json()["axes"][0]["value"] == -1.0


def test_limits_are_saved_per_agent(client):
    client.post("/api/limits", params={"agent_id": "attack"},
                json={"max_daily_orders": 60})
    client.post("/api/limits", params={"agent_id": "defend"},
                json={"max_daily_orders": 5})

    attack = client.get("/api/limits", params={"agent_id": "attack"}).json()
    defend = client.get("/api/limits", params={"agent_id": "defend"}).json()
    assert attack["configured"]["max_daily_orders"] == 60
    assert defend["configured"]["max_daily_orders"] == 5


def test_the_account_level_settings_are_untouched_by_agents(client):
    """그룹을 쓰지 않는 사람의 설정은 그 자리에 그대로 남습니다."""
    client.post("/api/limits", json={"max_daily_orders": 12})
    client.post("/api/limits", params={"agent_id": "attack"},
                json={"max_daily_orders": 99})

    assert client.get("/api/limits").json()["configured"]["max_daily_orders"] == 12


def test_a_saved_agent_profile_reaches_the_running_engine(client):
    """저장만 되고 봇에 안 닿으면 화면이 거짓말을 합니다."""
    client.patch("/api/profile", params={"agent_id": "attack"},
                 json={"overrides": {"R": 1.0}})
    client.patch("/api/profile", params={"agent_id": "defend"},
                 json={"overrides": {"R": -1.0}})
    start_group(client, spec("attack", "attack"), spec("defend", "defend"))
    try:
        attack = client.get("/api/limits", params={"agent_id": "attack"}).json()
        defend = client.get("/api/limits", params={"agent_id": "defend"}).json()
        # 성향의 R 축이 하루 손실 한도를 정합니다 — 공격형이 더 큽니다.
        assert attack["loss"]["limit_pct"] != defend["loss"]["limit_pct"]
    finally:
        client.post("/api/trader/stop")


# ── 에이전트 목록 ────────────────────────────────────────────────────────
def test_the_agents_endpoint_lists_what_is_running(client):
    assert client.get("/api/agents").json() == {
        "running": [], "max_agents": 4, "agents": []}

    start_group(client, spec("attack", "attack"), spec("defend", "defend"))
    try:
        body = client.get("/api/agents").json()
        assert body["running"] == ["attack", "defend"]
        assert [a["agent_id"] for a in body["agents"]] == ["attack", "defend"]
        assert "profile" in body["agents"][0]
        assert "limits" in body["agents"][0]
    finally:
        client.post("/api/trader/stop")


# ── 실거래 확인은 에이전트마다 ───────────────────────────────────────────
def test_a_live_agent_without_its_own_confirmation_is_refused(client):
    """하나를 확인했다고 나머지가 열리면, 관찰용으로 넣은 에이전트가 진짜
    주문을 내고 있다는 사실을 사용자가 모릅니다."""
    r = start_group(client, spec("attack", "attack", mode="live"))
    assert r.status_code >= 400
    assert client.get("/api/agents").json()["running"] == []


# ── 단일 봇 경로는 그대로 ────────────────────────────────────────────────
def test_the_old_single_bot_start_still_works(client):
    r = client.post("/api/trader/start",
                    json={"config_path": "attack", "mode": "dry_run"})
    assert r.status_code == 200, r.text
    try:
        body = client.get("/api/status").json()
        assert "agents" not in body
    finally:
        client.post("/api/trader/stop")


def test_health_reports_a_group_as_running(client):
    """`/api/health` 가 2개 이상 그룹을 "안 돈다" 로 보고하면, 감시가 실거래
    그룹을 죽은 것으로 읽습니다."""
    start_group(client, spec("attack", "attack"), spec("defend", "defend"))
    try:
        body = client.get("/api/health").json()
        assert body.get("trader_running") is True
    finally:
        client.post("/api/trader/stop")


def test_reads_without_an_agent_id_follow_the_strategy_name(client):
    """`agent_id` 없이 부르는 조회는 그룹이 돌 때 **전략 이름이 맞는 에이전트**
    를 봅니다. 아무것도 고르지 않으면 프로세스 기본 템플릿으로 물러서서, 옛
    단일 봇의 run 을 이 그룹의 자산 곡선인 양 보여줬습니다."""
    r = start_group(client, spec("attack", "attack"), spec("defend", "defend"))
    assert r.status_code == 200, r.text

    r = client.get("/api/equity?strategy=defend")
    assert r.status_code == 200, r.text
    assert r.json()["agent_id"] == "defend"

    r = client.get("/api/equity")
    assert r.status_code == 200, r.text
    assert r.json()["agent_id"] == "attack", "고르지 않으면 첫 에이전트"

    r = client.get("/api/equity?agent_id=defend&strategy=attack")
    assert r.status_code == 200, r.text
    assert r.json()["agent_id"] == "defend", "명시한 에이전트가 이름보다 앞선다"


def test_status_carries_the_focused_agents_book(client):
    """머리말 표식·자산 요약·전략 이름은 최상위 `portfolio`/`strategy` 를 읽습니다.
    그룹 응답에 그것이 없으면 그룹이 도는 내내 "미가동" 으로 보입니다."""
    r = start_group(client, spec("attack", "attack"), spec("defend", "defend"))
    assert r.status_code == 200, r.text

    body = client.get("/api/status?agent_id=defend").json()
    assert body["agent_id"] == "defend"
    assert body["strategy"] == "defend-strat"
    assert "portfolio" in body and "cash" in body["portfolio"]
    assert body["agent_mode"] == "dry_run"
    # 그룹의 키는 그대로 — 계좌 등급, 계좌 요약, 에이전트 목록.
    assert body["mode"] == "dry_run" and body["running"] is True
    assert "account" in body and [a["agent_id"] for a in body["agents"]] == ["attack", "defend"]

    body = client.get("/api/status").json()
    assert body["agent_id"] == "attack", "고르지 않으면 첫 에이전트"
    assert "portfolio" in body
