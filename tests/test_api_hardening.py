"""제어 API 가 주인에게 겨눠지지 않도록.

세 가지를 고정합니다.

* **`/api/setup` 은 설정 화면이 가진 키만 쓴다.** 예전에는 대문자면 무엇이든
  `.env` 에 들어갔고, `HTTPS_PROXY` 한 줄이면 다음 브로커 호출이 공격자를
  거쳐 나가면서 KIS 키가 그대로 넘어갔습니다.
* **`/api/trader/start` 는 CLI 와 같은 조건을 요구한다.** 대시보드가 실거래로
  가는 더 쉬운 길이면 안 됩니다.
* **`/api/limits` 는 부분 수정이다.** 주문 건수만 올리려던 한 번의 호출이
  손실 한도를 조용히 해제하면 안 됩니다.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

from quant.api.server import create_app
from quant.live.credentials import (
    OPERATOR_FIELDS, VENUES, CredentialStore, WRITABLE_KEYS, rejection_reason,
)
from quant.live.limits import TradingBudget

#: 자격증명이 아니라 프로세스를 조종하는 변수들 — 소문자 변형 포함.
PROCESS_CONTROL = [
    "HTTP_PROXY", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "NO_PROXY",
    "PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
    "PYTHONPATH", "PYTHONSTARTUP", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
]


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """격리된 `.env`. 진짜 자격증명 파일은 절대 건드리지 않습니다."""
    path = tmp_path / "env.test"
    monkeypatch.setenv("QUANT_ENV_FILE", str(path))
    monkeypatch.delenv("QUANT_API_TOKEN", raising=False)
    for key in ("QUANT_LIMIT_DAILY_NOTIONAL", "QUANT_LIMIT_DAILY_ORDERS",
                "QUANT_LIMIT_DAILY_LOSS", "QUANT_LIMIT_DAILY_LOSS_PCT"):
        monkeypatch.delenv(key, raising=False)
    before = dict(os.environ)
    yield path
    # 서버는 저장한 값을 os.environ 에도 올립니다. monkeypatch 는 그건 모릅니다.
    os.environ.clear()
    os.environ.update(before)


@pytest.fixture
def client(env_file, tmp_path):
    return TestClient(create_app(None, state_path=str(tmp_path / "state.db")))


def env_keys(path) -> dict:
    if not path.exists():
        return {}
    return {line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#") and "=" in line}


# ── (a) 설정 저장은 허용 목록 안에서만 ────────────────────────────────────
@pytest.mark.parametrize("key", PROCESS_CONTROL)
def test_process_control_variables_never_reach_the_env_file(client, env_file, key):
    """감사에서 확인된 공격 그대로: 인증 없이 프록시를 심고 자격증명을 가로챈다."""
    body = client.post("/api/setup", json={"values": {
        "KIS_APP_KEY": "real-key", key: "http://127.0.0.1:8899",
    }}).json()

    assert key in body["rejected"]
    assert key not in body["written"]
    assert key not in env_keys(env_file)
    assert os.environ.get(key) != "http://127.0.0.1:8899"
    # 나머지 값은 그대로 저장됩니다 — 한 줄 때문에 폼 전체를 잃지 않습니다.
    assert env_keys(env_file)["KIS_APP_KEY"] == "real-key"


def test_an_unknown_key_is_refused_even_when_it_looks_harmless(client, env_file):
    body = client.post("/api/setup", json={"values": {"MY_OWN_SETTING": "1"}}).json()
    assert body["written"] == []
    assert "MY_OWN_SETTING" in body["rejected"]
    assert env_keys(env_file) == {}


def test_rejections_are_reported_to_the_caller(client):
    """조용히 버리면 호출자는 저장된 줄 압니다."""
    body = client.post("/api/setup", json={"values": {"HTTPS_PROXY": "http://x"}}).json()
    assert body["rejected"]["HTTPS_PROXY"]
    assert "HTTPS_PROXY" in body["note"]


def test_every_field_the_setup_screen_shows_is_writable(client, env_file):
    """허용 목록이 화면과 어긋나면 운영자가 입력한 키가 조용히 사라집니다."""
    advertised = [env for venue in VENUES for env, _, _ in venue.fields] + \
                 [env for env, _, _ in OPERATOR_FIELDS]
    body = client.post("/api/setup",
                       json={"values": {env: "v-" + env for env in advertised}}).json()
    assert body["rejected"] == {}
    assert sorted(body["written"]) == sorted(advertised)


def test_the_limits_endpoint_keys_are_writable():
    """`/api/limits` 도 같은 저장소를 씁니다 — 여기서 막히면 한도가 저장되지 않습니다."""
    for key in ("QUANT_LIMIT_DAILY_NOTIONAL", "QUANT_LIMIT_DAILY_ORDERS",
                "QUANT_LIMIT_DAILY_LOSS", "QUANT_LIMIT_DAILY_LOSS_PCT"):
        assert key in WRITABLE_KEYS
        assert rejection_reason(key) == ""


@pytest.mark.parametrize("key", PROCESS_CONTROL)
def test_the_store_itself_refuses_process_control(tmp_path, key):
    """엔드포인트가 아니라 저장소가 막습니다 — 다른 호출자가 생겨도 같습니다."""
    store = CredentialStore(tmp_path / "env.test")
    report = store.update({key: "x"})
    assert report.written == []
    assert key in report.rejected
    assert not (tmp_path / "env.test").exists()


def test_a_newline_in_a_value_cannot_smuggle_a_second_key(client, env_file):
    """한 줄에 한 키. 값 안의 줄바꿈은 허용 목록을 값 쪽에서 우회하는 길입니다."""
    body = client.post("/api/setup", json={"values": {
        "KIS_APP_KEY": "AK\nHTTPS_PROXY=http://127.0.0.1:8899",
    }}).json()

    assert body["written"] == []
    assert "KIS_APP_KEY" in body["rejected"]
    assert "HTTPS_PROXY" not in env_keys(env_file)
    assert os.environ.get("HTTPS_PROXY") is None


def test_a_blank_value_still_leaves_an_existing_credential_alone(tmp_path):
    store = CredentialStore(tmp_path / "env.test")
    store.update({"KIS_APP_KEY": "original"})
    report = store.update({"KIS_APP_KEY": ""})
    assert report.written == []
    assert env_keys(tmp_path / "env.test")["KIS_APP_KEY"] == "original"


# ── (b) 실거래 시작 조건은 CLI 와 같아야 한다 ─────────────────────────────
LIVE_YAML = """
name: live-probe
mode: {mode}
data: {{provider: synthetic, timeframe: 1d}}
universe:
  symbols:
    - {{ticker: AAA, venue: SIM}}
broker:
  type: {broker}
  live_trading_confirmed: {confirmed}
{limits}
"""

LIMITS_BLOCK = "limits:\n  max_daily_orders: 5\n"


def write_config(tmp_path, name="c.yaml", *, mode="live", broker="alpaca",
                 confirmed="true", limits=LIMITS_BLOCK):
    path = tmp_path / name
    path.write_text(LIVE_YAML.format(mode=mode, broker=broker,
                                     confirmed=confirmed, limits=limits),
                    encoding="utf-8")
    return str(path)


class _Bus:
    def on(self, *_a, **_k):
        pass


class _Ctx:
    def __init__(self):
        self.bus = _Bus()


class _Engine:
    def __init__(self):
        self.ctx = _Ctx()
        self.budget = TradingBudget()


class _ConfigStub:
    class mode:
        value = "dry_run"


class FakeLiveTrader:
    """브로커에 닿지 않는 대역. 실주문은 테스트에서 절대 내지 않습니다."""

    def __init__(self, config, state_path):
        self.config, self.state_path = config, state_path
        self.engine = _Engine()
        self.running = True

    async def run(self):
        await asyncio.sleep(0)

    def status(self):
        return {"running": True, "mode": self.config.mode.value}


@pytest.fixture
def no_real_broker(monkeypatch):
    import quant.live.trader as trader_module

    monkeypatch.setattr(trader_module, "LiveTrader", FakeLiveTrader)


def started(client):
    return bool(client.get("/api/health").json()["trader_running"])


def test_live_is_refused_when_the_config_file_does_not_ask_for_it(
        client, tmp_path, no_real_broker):
    """CLI 는 mode 가 live 가 아닌 설정으로는 실거래를 거부합니다. API 도 같아야 합니다."""
    path = write_config(tmp_path, mode="dry_run")
    r = client.post("/api/trader/start",
                    json={"config_path": path, "mode": "live", "confirm": "live-probe"})
    assert r.status_code == 400
    assert "mode" in r.json()["detail"]
    assert not started(client)


def test_live_without_a_daily_cap_is_refused(client, tmp_path, no_real_broker):
    """감사 재현: 하루 한도 없는 실거래가 POST 한 번으로 떴습니다."""
    path = write_config(tmp_path, limits="")
    r = client.post("/api/trader/start",
                    json={"config_path": path, "mode": "live", "confirm": "live-probe"})
    assert r.status_code == 400
    assert "limits" in r.json()["detail"]
    assert not started(client)


def test_live_with_a_paper_broker_is_refused(client, tmp_path, no_real_broker):
    path = write_config(tmp_path, broker="paper")
    r = client.post("/api/trader/start",
                    json={"config_path": path, "mode": "live", "confirm": "live-probe"})
    assert r.status_code == 400
    assert "paper" in r.json()["detail"]
    assert not started(client)


def test_live_without_live_trading_confirmed_is_refused(client, tmp_path, no_real_broker):
    path = write_config(tmp_path, confirmed="false")
    r = client.post("/api/trader/start",
                    json={"config_path": path, "mode": "live", "confirm": "live-probe"})
    assert r.status_code == 400
    assert "live_trading_confirmed" in r.json()["detail"]
    assert not started(client)


@pytest.mark.parametrize("confirm", ["", "yes", "Live-Probe", "live-probe2"])
def test_live_needs_the_strategy_name_typed_back(client, tmp_path, no_real_broker,
                                                 confirm):
    """CLI 가 콘솔에서 받는 확인과 같은 잠금."""
    path = write_config(tmp_path)
    r = client.post("/api/trader/start",
                    json={"config_path": path, "mode": "live", "confirm": confirm})
    assert r.status_code == 400
    assert "live-probe" in r.json()["detail"]
    assert not started(client)


def test_a_fully_confirmed_live_config_still_starts(client, tmp_path, no_real_broker):
    """잠금이 영영 닫혀 있으면 그것도 버그입니다."""
    path = write_config(tmp_path)
    r = client.post("/api/trader/start",
                    json={"config_path": path, "mode": "live", "confirm": "live-probe"})
    assert r.status_code == 200, r.text
    assert r.json() == {"started": True, "strategy": "live-probe", "mode": "live"}


def test_dry_run_still_starts_without_a_confirmation(client, tmp_path, no_real_broker):
    path = write_config(tmp_path, mode="dry_run", broker="paper", confirmed="false",
                        limits="")
    r = client.post("/api/trader/start", json={"config_path": path})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "dry_run"


def test_a_missing_config_is_a_bad_request_not_a_crash(client, tmp_path):
    r = client.post("/api/trader/start",
                    json={"config_path": str(tmp_path / "nope.yaml")})
    assert r.status_code == 400


# ── (c) 한도는 부분 수정 ──────────────────────────────────────────────────
@pytest.fixture
def running(client, tmp_path):
    """예산이 붙은 봇이 돌고 있는 상태."""
    trader = FakeLiveTrader(_ConfigStub(), str(tmp_path / "state.db"))
    client.app.state.quant.trader = trader
    return trader


ALL_FOUR = {"max_daily_notional": 50000, "max_daily_orders": 20,
            "max_daily_loss": 2000, "max_daily_loss_pct": 0.03}


def test_sending_one_cap_leaves_the_others_alone(client, running, env_file):
    """감사 재현: {max_daily_orders: 25} 하나에 나머지 세 한도가 사라졌습니다."""
    client.post("/api/limits", json=ALL_FOUR)
    body = client.post("/api/limits", json={"max_daily_orders": 25}).json()

    budget = running.engine.budget
    assert budget.max_orders == 25
    assert budget.max_notional == 50000
    assert budget.max_loss == 2000
    assert budget.max_loss_pct == 0.03
    assert body["updated"] == ["max_daily_orders"]
    assert body["removed"] == []

    live = client.get("/api/limits").json()
    assert live["notional"]["limit"] == 50000
    assert live["loss"]["limit"] == 2000
    assert live["orders"]["limit"] == 25


def test_the_persisted_file_matches_what_is_running(client, running, env_file):
    """재시작 뒤에 한도가 되살아나면 사고가 스스로 증거를 지웁니다."""
    client.post("/api/limits", json=ALL_FOUR)
    client.post("/api/limits", json={"max_daily_orders": 25})

    stored = env_keys(env_file)
    assert stored["QUANT_LIMIT_DAILY_ORDERS"] == "25"
    assert stored["QUANT_LIMIT_DAILY_NOTIONAL"] == "50000.0"
    assert stored["QUANT_LIMIT_DAILY_LOSS"] == "2000.0"
    assert stored["QUANT_LIMIT_DAILY_LOSS_PCT"] == "0.03"


def test_an_explicit_zero_removes_a_cap_and_says_so(client, running, env_file):
    client.post("/api/limits", json=ALL_FOUR)
    body = client.post("/api/limits", json={"max_daily_loss": 0}).json()

    assert running.engine.budget.max_loss == 0
    assert running.engine.budget.max_notional == 50000
    assert body["removed"] == ["max_daily_loss"]
    assert "해제" in body["note"]
    # 살아 있는 값과 저장된 값이 갈라지면 다음 아침에 아무도 진실을 못 봅니다.
    assert "QUANT_LIMIT_DAILY_LOSS" not in env_keys(env_file)


def test_zeroing_a_cap_that_was_never_set_is_not_reported_as_a_release(
        client, running, env_file):
    body = client.post("/api/limits", json={"max_daily_loss": 0}).json()
    assert body["removed"] == []


def test_partial_updates_work_before_a_bot_is_running(client, env_file):
    client.post("/api/limits", json=ALL_FOUR)
    body = client.post("/api/limits", json={"max_daily_orders": 25}).json()

    assert body["applied_now"] is None
    stored = env_keys(env_file)
    assert stored["QUANT_LIMIT_DAILY_ORDERS"] == "25"
    assert stored["QUANT_LIMIT_DAILY_NOTIONAL"] == "50000.0"
    configured = client.get("/api/limits").json()["configured"]
    assert configured["max_daily_notional"] == 50000
    assert configured["max_daily_loss"] == 2000


def test_a_loss_cap_is_stored_as_a_magnitude(client, running, env_file):
    """음수로 적어도 같은 뜻입니다 — 부호 때문에 한도가 꺼지면 안 됩니다."""
    client.post("/api/limits", json={"max_daily_loss": -2000})
    assert running.engine.budget.max_loss == 2000
    assert env_keys(env_file)["QUANT_LIMIT_DAILY_LOSS"] == "2000.0"
