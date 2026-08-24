"""제어 API 가 주인에게 겨눠지지 않도록.

이 파일은 **로그인한 사용자**로 요청합니다. 예전에는 계정 없이 열려 있어서
같은 검사를 인증 없이 통과했는데, 그 상태 자체가 결함이었습니다.

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

from quant.api.server import ACCOUNT_OPERATOR_FIELDS, create_app
from quant.live.credentials import (
    VENUES,
    WRITABLE_KEYS,
    CredentialStore,
    rejection_reason,
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
def client(env_file, tmp_path, monkeypatch):
    """가입하고 로그인까지 마친 클라이언트.

    이 API 에는 익명 자리가 없습니다. 세션 쿠키가 사람을 정하는 유일한
    수단이므로, 검사도 사람이 된 뒤부터 시작해야 실제와 같습니다.
    """
    monkeypatch.setenv("QUANT_SECRET_KEY", "t" * 48)
    monkeypatch.setenv("QUANT_USERS_DB", str(tmp_path / "users.db"))
    c = TestClient(create_app(None, state_path=str(tmp_path / "state.db")))
    r = c.post("/api/auth/register", json={
        "email": "operator@example.com", "password": "correct-horse-9",
        "display_name": "운영자"})
    assert r.status_code == 201, r.text
    return c


def stored(client) -> dict:
    """이 사용자에게 저장된 자격증명 이름 → 끝 4자리.

    값은 어떤 경로로도 돌아오지 않으므로 이름만 확인할 수 있습니다.
    """
    body = client.get("/api/setup").json()
    return body.get("configured", body.get("state", {}).get("configured", {})) or {}


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
    assert key not in stored(client)
    # 프로세스 환경에도 절대 올라가지 않습니다 — 거기 올리면 같은 서버의
    # 다른 사용자 봇이 그것을 읽습니다.
    assert os.environ.get(key) != "http://127.0.0.1:8899"
    # 나머지 값은 그대로 저장됩니다 — 한 줄 때문에 폼 전체를 잃지 않습니다.
    assert "KIS_APP_KEY" in stored(client)


def test_an_unknown_key_is_refused_even_when_it_looks_harmless(client, env_file):
    body = client.post("/api/setup", json={"values": {"MY_OWN_SETTING": "1"}}).json()
    assert body["written"] == []
    assert "MY_OWN_SETTING" in body["rejected"]
    assert stored(client) == {}


def test_rejections_are_reported_to_the_caller(client):
    """조용히 버리면 호출자는 저장된 줄 압니다."""
    body = client.post("/api/setup", json={"values": {"HTTPS_PROXY": "http://x"}}).json()
    assert body["rejected"]["HTTPS_PROXY"]
    assert "HTTPS_PROXY" in body["note"]


def test_every_field_the_setup_screen_shows_is_writable(client, env_file):
    """허용 목록이 화면과 어긋나면 운영자가 입력한 키가 조용히 사라집니다."""
    # 계정 화면이 실제로 보여주는 것만. 운영자 이름과 대시보드 토큰은 계정이
    # 대신하므로 화면에서 빠졌고, 하루 한도는 /api/limits 가 따로 받습니다.
    advertised = [env for venue in VENUES for env, _, _ in venue.fields] + \
                 [env for env, _, _ in ACCOUNT_OPERATOR_FIELDS]
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
    """템플릿을 하나 놓고 **이름**을 돌려줍니다.

    API 는 서버 경로를 받지 않습니다 — 가입자가 경로를 지정할 수 있으면 그것은
    전략 선택이 아니라 파일 열람입니다. 그래서 테스트도 이름으로 부릅니다.
    """
    root = tmp_path / "templates"
    root.mkdir(exist_ok=True)
    path = root / name
    path.write_text(LIVE_YAML.format(mode=mode, broker=broker,
                                     confirmed=confirmed, limits=limits),
                    encoding="utf-8")
    os.environ["QUANT_CONFIG_DIR"] = str(root)
    return path.stem


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

    def __init__(self, config, state_path, *, profile_path=None, **kwargs):
        # 실물 `LiveTrader` 는 사용자별 성향 파일 경로를 받습니다. 대역이 그것을
        # 안 받으면, 시그니처가 갈라졌다는 사실이 테스트 실패로만 드러납니다.
        self.config, self.state_path = config, state_path
        self.profile_path = profile_path
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


@pytest.fixture
def linked(client):
    """브로커가 연결된 상태.

    자격증명 없이 봇을 띄우려 하면 서비스가 거부합니다 — 그것도 잠금 중
    하나라서, 다른 잠금을 시험하려면 먼저 이걸 통과해야 합니다.
    """
    r = client.post("/api/setup", json={"values": {
        "ALPACA_API_KEY": "test-key-aaaa", "ALPACA_SECRET_KEY": "test-secret-bbbb"}})
    assert r.status_code == 200, r.text
    return client


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


def test_a_fully_confirmed_live_config_still_starts(linked, tmp_path, no_real_broker):
    """잠금이 영영 닫혀 있으면 그것도 버그입니다."""
    client, path = linked, write_config(tmp_path)
    r = client.post("/api/trader/start",
                    json={"config_path": path, "mode": "live", "confirm": "live-probe"})
    assert r.status_code == 200, r.text
    assert r.json() == {"started": True, "strategy": "live-probe", "mode": "live"}


def test_dry_run_still_starts_without_a_confirmation(client, tmp_path, no_real_broker):
    """paper 브로커는 아무 계좌에도 닿지 않으므로 자격증명 없이 떠야 합니다."""
    path = write_config(tmp_path, mode="dry_run", broker="paper", confirmed="false",
                        limits="")
    r = client.post("/api/trader/start", json={"config_path": path})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "dry_run"


def test_a_missing_config_is_a_bad_request_not_a_crash(client, tmp_path):
    r = client.post("/api/trader/start",
                    json={"config_path": str(tmp_path / "nope.yaml")})
    assert r.status_code == 400


def test_a_bot_without_credentials_is_refused_before_it_reaches_a_broker(
        client, tmp_path, no_real_broker):
    """연결도 안 한 채 봇이 뜨면, 실패는 브로커 앞에서 납니다 — 그때는 늦습니다."""
    path = write_config(tmp_path, mode="dry_run", confirmed="false", limits="")
    r = client.post("/api/trader/start", json={"config_path": path})
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "credentials_missing"
    # 무엇이 없는지 화면이 그대로 안내할 수 있어야 합니다.
    assert [m["name"] for m in body["missing"]] == ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"]


# ── (c) 한도는 부분 수정 ──────────────────────────────────────────────────
#
# 한도는 이제 사용자의 것입니다. 프로세스 하나에 하나가 아니라 계정마다 하나이고,
# `.env` 가 아니라 그 사용자의 저장소에 남습니다. 지켜야 할 성질은 그대로입니다:
# 한 필드만 보낸 호출이 나머지 한도를 조용히 지우면 안 되고, 저장된 값과 돌고
# 있는 값이 갈라지면 안 됩니다.

ALL_FOUR = {"max_daily_notional": 50000, "max_daily_orders": 20,
            "max_daily_loss": 2000, "max_daily_loss_pct": 0.03}


def caps(client) -> dict:
    return client.get("/api/limits").json()["configured"]


def test_sending_one_cap_leaves_the_others_alone(client):
    """감사 재현: {max_daily_orders: 25} 하나에 나머지 세 한도가 사라졌습니다."""
    client.post("/api/limits", json=ALL_FOUR)
    body = client.post("/api/limits", json={"max_daily_orders": 25}).json()

    assert body["updated"] == ["max_daily_orders"]
    assert body["removed"] == []
    assert caps(client) == {"max_daily_notional": 50000, "max_daily_orders": 25,
                            "max_daily_loss": 2000, "max_daily_loss_pct": 0.03}


def test_an_explicit_zero_removes_a_cap_and_says_so(client):
    """0 은 "한도 없음" 입니다 — 그런데 조용히 그러면 안 됩니다."""
    client.post("/api/limits", json=ALL_FOUR)
    body = client.post("/api/limits", json={"max_daily_loss": 0}).json()

    assert body["removed"] == ["max_daily_loss"]
    assert "해제" in body["note"]
    assert caps(client)["max_daily_loss"] == 0
    assert caps(client)["max_daily_notional"] == 50000


def test_zeroing_a_cap_that_was_never_set_is_not_reported_as_a_release(client):
    body = client.post("/api/limits", json={"max_daily_loss": 0}).json()
    assert body["removed"] == []


def test_a_loss_cap_is_stored_as_a_magnitude(client):
    """음수로 적어도 같은 뜻입니다 — 부호 때문에 한도가 꺼지면 안 됩니다."""
    client.post("/api/limits", json={"max_daily_loss": -2000})
    assert caps(client)["max_daily_loss"] == 2000


def test_the_caps_survive_a_restart(client, tmp_path, monkeypatch):
    """재시작 뒤에 한도가 되살아나면 사고가 스스로 증거를 지웁니다."""
    client.post("/api/limits", json=ALL_FOUR)
    cookie = client.cookies

    fresh = TestClient(create_app(None, state_path=str(tmp_path / "state.db")))
    fresh.cookies = cookie
    assert fresh.get("/api/limits").json()["configured"] == {
        "max_daily_notional": 50000, "max_daily_orders": 20,
        "max_daily_loss": 2000, "max_daily_loss_pct": 0.03}


def test_one_users_caps_are_not_anothers(client, tmp_path):
    """한도는 사람의 것입니다. 남의 한도가 내 봇을 묶으면 안 됩니다."""
    client.post("/api/limits", json=ALL_FOUR)

    other = TestClient(client.app)
    other.post("/api/auth/register", json={
        "email": "second@example.com", "password": "correct-horse-9",
        "display_name": "둘째"})
    assert other.get("/api/limits").json()["configured"]["max_daily_orders"] == 0
