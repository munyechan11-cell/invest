"""한 프로세스에 여러 사람이 앉았을 때, 남의 것에 닿는 길이 하나도 없는지.

여기서 확인하는 것은 "엔드포인트가 200 을 준다" 가 아닙니다. 그건 1인용일
때도 하던 일이고, 사고는 거기서 나지 않습니다. 확인하는 것은 하나뿐입니다 —
**A 가 B 의 것을 읽거나 움직일 수 있는가.**

그래서 새어나갈 수 있는 방향마다 테스트가 하나씩 있습니다. 자격증명(읽기),
하루 한도·성향(쓰기), 상태 파일(과거), 실행 중인 봇(현재), 이벤트 스트림
(관람), 그리고 프로세스 환경(전역). 마지막 것이 특히 중요합니다: `os.environ`
에 올라간 한투 키 하나는 같은 프로세스의 **모든** 봇이 읽으므로, 그건 유출이
아니라 계좌 공유입니다.

id 를 찍어서 남의 것에 닿는 길도 함께 막습니다. 가장 확실한 방법은 어떤
라우트도 사용자 id 를 받지 않는 것이라, 그것도 테스트로 고정합니다.

실제 브로커 엔드포인트는 어디서도 호출하지 않습니다. 봇을 실제로 띄우는
테스트는 전부 synthetic 시세 + paper 브로커이고, 키가 필요한 전략은 "키가
없어서 거절당하는" 경로로만 씁니다.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from quant.api.server import (
    ACCOUNT_KEYS,
    assert_ready_for_users,
    create_app,
    strategy_catalog,
)
from quant.core.types import UTC
from quant.live.credentials import OPERATOR_FIELDS, VENUES
from quant.live.state import StateStore
from quant.webapp import accounts as accounts_module
from quant.webapp.accounts import SecretKeyMissing
from quant.webapp.auth_api import SESSION_COOKIE

#: TestClient 는 쿠키 항아리를 웹소켓 핸드셰이크에 붙이지 않습니다 (브라우저는
#: 붙입니다). 그리고 그 핸드셰이크의 스킴은 ws 라 서버가 접두사 없는 이름을
#: 봅니다 — `__Host-` 는 Secure 없이는 브라우저가 저장하지 않기 때문입니다.
_WS_COOKIE = SESSION_COOKIE.replace("__Host-", "")


def ws_headers(cookie: str) -> dict:
    return {"cookie": f"{_WS_COOKIE}={cookie}"} if cookie else {}

SECRET = "multiuser-integration-secret-0123456789ab"
PASSWORD = "korea-invest-1"

#: A 의 값이 응답·파일·환경변수 어디에 나타나도 실패입니다.
A_KEYS = {"KIS_APP_KEY": "AAAA-app-key-aaaaaaaa",
          "KIS_APP_SECRET": "AAAA-app-secret-aaaaaaaa",
          "KIS_ACCOUNT_NO": "11112222"}
B_KEYS = {"KIS_APP_KEY": "BBBB-app-key-bbbbbbbb",
          "KIS_APP_SECRET": "BBBB-app-secret-bbbbbbbb",
          "KIS_ACCOUNT_NO": "33334444"}

#: 1인용 시절에 프로세스에 남던 것들. 하나라도 남으면 결과가 거짓이 됩니다.
_PROCESS_WIDE = (
    "QUANT_API_TOKEN", "QUANT_SECRET_KEY", "QUANT_USERS_DB", "QUANT_USER_DATA",
    "QUANT_ENV_FILE", "QUANT_CONFIG_DIR", "QUANT_PROFILE_FILE", "CORS_ORIGINS",
    "OPERATOR_NAME",
    "QUANT_LIMIT_DAILY_NOTIONAL", "QUANT_LIMIT_DAILY_ORDERS",
    "QUANT_LIMIT_DAILY_LOSS", "QUANT_LIMIT_DAILY_LOSS_PCT",
    *sorted(ACCOUNT_KEYS),
)


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    """PBKDF2 600,000회는 이 파일의 수십 번의 가입·로그인에서 유일한 비용입니다.

    진짜 비용은 accounts.py 의 테스트가 봅니다. 여기서 재고 싶은 것은 격리이지
    해시 강도가 아닙니다.
    """
    monkeypatch.setattr(accounts_module, "_PBKDF2_ROUNDS", 1_000)


@pytest.fixture(autouse=True)
def clean_process(tmp_path, monkeypatch):
    for name in _PROCESS_WIDE:
        monkeypatch.delenv(name, raising=False)
    # 저장소 루트의 investor_profile.json 과 .env 가 결과를 만들지 않도록.
    monkeypatch.chdir(tmp_path)


PAPER = {
    "name": "모의전략",
    "mode": "dry_run",
    "data": {"provider": "synthetic", "timeframe": "1d", "calendar": "always_open",
             "warmup_bars": 60},
    "universe": {"symbols": [{"ticker": "SIM1"}]},
    "alpha": [{"type": "ema_cross"}],
    "broker": {"type": "paper"},
}

KIS = {
    "name": "한투전략",
    "mode": "dry_run",
    "data": {"provider": "kis", "timeframe": "1d", "calendar": "always_open",
             "warmup_bars": 60},
    "universe": {"symbols": [{"ticker": "005930", "venue": "KRX",
                              "quote_currency": "KRW"}]},
    "alpha": [{"type": "ema_cross"}],
    "broker": {"type": "kis"},
}

LIVE = {
    "name": "실거래전략",
    "mode": "live",
    "data": {"provider": "synthetic", "timeframe": "1d", "calendar": "always_open",
             "warmup_bars": 60},
    "universe": {"symbols": [{"ticker": "AAA", "venue": "SIM"}]},
    "alpha": [{"type": "ema_cross"}],
    "broker": {"type": "alpaca", "live_trading_confirmed": True},
    "limits": {"max_daily_orders": 5},
}


@pytest.fixture
def templates(tmp_path):
    """서비스가 돌려주는 전략 목록. 사용자는 이름만 고를 수 있습니다."""
    root = tmp_path / "templates"
    root.mkdir()
    for name, body in (("paper", PAPER), ("kis", KIS), ("live", LIVE)):
        (root / f"{name}.yaml").write_text(
            yaml.safe_dump(body, allow_unicode=True), encoding="utf-8")
    # 목록 **밖에** 있는 설정. 경로를 지정할 수 있는지 확인하는 데 씁니다.
    outside = tmp_path / "secret_strategy.yaml"
    outside.write_text(yaml.safe_dump(PAPER, allow_unicode=True), encoding="utf-8")
    return root, outside


@pytest.fixture
def env(tmp_path, templates, monkeypatch):
    root, _outside = templates
    monkeypatch.setenv("QUANT_SECRET_KEY", SECRET)
    monkeypatch.setenv("QUANT_USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setenv("QUANT_USER_DATA", str(tmp_path / "userdata"))
    monkeypatch.setenv("QUANT_ENV_FILE", str(tmp_path / "env.test"))
    monkeypatch.setenv("QUANT_CONFIG_DIR", str(root))
    return tmp_path


@pytest.fixture
def app(env):
    return create_app(None, state_path=str(env / "state.db"))


@pytest.fixture
def client(app):
    # 컨텍스트 매니저여야 lifespan 이 돌고, 요청들이 **같은** 이벤트 루프를
    # 공유합니다. 봇은 그 루프 위의 태스크라 이게 없으면 첫 요청과 함께 죽습니다.
    with TestClient(app, base_url="https://desk.example") as c:
        yield c


# ── 한 사람의 브라우저 ──────────────────────────────────────────────────
class Caller:
    """한 사람. 요청마다 그 사람의 세션 쿠키만 붙습니다.

    쿠키 항아리를 매번 비우고 자기 것만 넣는 이유는, 테스트가 "마지막에
    로그인한 사람" 으로 조용히 넘어가면 격리 검사가 통째로 무의미해지기
    때문입니다.
    """

    def __init__(self, client: TestClient, cookie: str = "", token: str = "",
                 user: dict | None = None):
        self.client = client
        self.cookie = cookie
        self.token = token
        self.user = user or {}

    @property
    def id(self) -> int:
        return int(self.user["id"])

    def _seat(self) -> dict:
        self.client.cookies.clear()
        if self.cookie:
            self.client.cookies.set(SESSION_COOKIE, self.cookie)
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def get(self, path, **kw):
        return self.client.get(path, headers=self._seat(), **kw)

    def post(self, path, json=None, **kw):
        return self.client.post(path, json=json, headers=self._seat(), **kw)

    def patch(self, path, json=None, **kw):
        return self.client.patch(path, json=json, headers=self._seat(), **kw)

    def delete(self, path, **kw):
        return self.client.delete(path, headers=self._seat(), **kw)


def signup(client: TestClient, email: str, name: str = "", secrets: dict | None = None,
           password: str = PASSWORD) -> Caller:
    client.cookies.clear()
    r = client.post("/api/auth/register",
                    json={"email": email, "password": password, "display_name": name})
    assert r.status_code == 201, r.text
    caller = Caller(client, cookie=client.cookies.get(SESSION_COOKIE), user=r.json())
    if secrets:
        assert caller.post("/api/setup", json={"values": secrets}).status_code == 200
    client.cookies.clear()
    return caller


def two_users(client, a_secrets=None, b_secrets=None) -> tuple[Caller, Caller]:
    return (signup(client, "a@example.com", "에이", a_secrets),
            signup(client, "b@example.com", "비", b_secrets))


def until_running(caller: Caller, timeout: float = 25.0) -> dict:
    deadline = time.monotonic() + timeout
    status: dict = {}
    while time.monotonic() < deadline:
        status = caller.get("/api/status").json()
        if status.get("running"):
            return status
        if status.get("error"):
            raise AssertionError(f"봇이 죽었습니다: {status}")
        time.sleep(0.05)
    raise AssertionError(f"봇이 뜨지 않았습니다: {status}")


# ── 누구로 앉았는가 ─────────────────────────────────────────────────────
def test_the_first_signup_is_the_admin_and_the_next_is_not(client):
    a, b = two_users(client)
    assert a.user["is_admin"] is True
    assert b.user["is_admin"] is False


def test_each_caller_is_answered_as_themselves(client):
    """마지막에 가입한 사람이 아니라, 이 요청을 보낸 사람이어야 합니다."""
    a, b = two_users(client)
    assert a.get("/api/auth/me").json()["email"] == "a@example.com"
    assert b.get("/api/auth/me").json()["email"] == "b@example.com"
    assert a.get("/api/auth/me").json()["email"] == "a@example.com"


def _probe_paths(app) -> list[tuple[str, str]]:
    """인증이 필요한 라우트 전부 — (method, path)."""
    open_paths = {"/api/health", "/", "/openapi.json", "/docs",
                  "/docs/oauth2-redirect", "/redoc", "/ws"}
    out = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api") or path in open_paths:
            continue
        if path.startswith("/api/auth"):
            continue                      # 로그인 자체는 로그인 없이 되어야 합니다
        concrete = (path.replace("{ticker}", "SIM1").replace("{venue_id}", "kis")
                        .replace("{axis}", "R"))
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            out.append((method, concrete))
    return out


def test_every_route_that_touches_anything_refuses_an_anonymous_caller(client, app):
    """이 목록이 비면 테스트가 아무것도 안 한 것이므로 개수도 함께 봅니다."""
    probes = _probe_paths(app)
    assert len(probes) >= 25
    client.cookies.clear()
    for method, path in probes:
        r = client.request(method, path, json={})
        assert r.status_code == 401, f"{method} {path} → {r.status_code}"


def test_a_forged_session_cookie_is_not_a_session(client):
    a, _b = two_users(client)
    forged = Caller(client, cookie="a" * 43)
    assert forged.get("/api/setup").status_code == 401
    assert a.get("/api/setup").status_code == 200


def test_logging_out_ends_the_session_for_that_person_only(client):
    a, b = two_users(client)
    assert a.post("/api/auth/logout").status_code == 200
    assert a.get("/api/setup").status_code == 401
    assert b.get("/api/setup").status_code == 200


# ── 자격증명 ────────────────────────────────────────────────────────────
def test_one_users_credentials_are_invisible_to_another(client):
    a, b = two_users(client, a_secrets=A_KEYS, b_secrets=B_KEYS)

    a_view = a.get("/api/setup").json()
    b_view = b.get("/api/setup").json()
    assert a_view["configured"]["KIS_APP_KEY"] == A_KEYS["KIS_APP_KEY"][-4:]
    assert b_view["configured"]["KIS_APP_KEY"] == B_KEYS["KIS_APP_KEY"][-4:]
    # 짧은 비밀은 힌트조차 주지 않습니다 — 8자리 계좌번호에서 4자리는 절반입니다.
    assert a_view["configured"]["KIS_ACCOUNT_NO"] == ""
    # 힌트 4자리 말고는 어떤 조각도 넘어오지 않습니다.
    assert A_KEYS["KIS_APP_KEY"] not in json.dumps(b_view, ensure_ascii=False)
    assert B_KEYS["KIS_APP_KEY"] not in json.dumps(a_view, ensure_ascii=False)


def test_a_stored_credential_never_comes_back_through_any_route(client, app):
    a, _b = two_users(client, a_secrets=A_KEYS)
    a.post("/api/setup", json={"values": {"TELEGRAM_BOT_TOKEN": "AAAA-telegram-token"}})

    for method, path in _probe_paths(app):
        if method != "GET":
            continue
        body = a.get(path).text
        for value in (*A_KEYS.values(), "AAAA-telegram-token"):
            assert value not in body, f"{path} 가 {value[:6]}… 를 돌려줬습니다"


def test_no_credential_reaches_the_process_environment(client, tmp_path):
    """환경변수는 프로세스 전역입니다 — 여기 올라간 키는 모두의 키입니다."""
    a, _b = two_users(client, a_secrets=A_KEYS, b_secrets=B_KEYS)

    for name, value in {**A_KEYS, **B_KEYS}.items():
        assert os.environ.get(name) != value
        assert name not in os.environ or not os.environ[name]
    # 파일로도 새지 않습니다. 계정 DB 가 유일한 보관 장소입니다.
    env_file = tmp_path / "env.test"
    text = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    for value in {**A_KEYS, **B_KEYS}.values():
        assert value not in text


def test_a_leftover_env_file_is_taken_off_the_process_at_startup(env, monkeypatch):
    """1인용 시절의 `.env` 가 남아 있으면 그건 운영자 계좌의 공유 키입니다."""
    (env / "env.test").write_text("KIS_APP_KEY=leftover-operator-key\n", encoding="utf-8")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "leftover-deployment-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "leftover-telegram-token")

    create_app(None, state_path=str(env / "state.db"))

    assert not os.environ.get("KIS_APP_KEY")
    assert not os.environ.get("TOSS_CLIENT_SECRET")
    assert not os.environ.get("TELEGRAM_BOT_TOKEN")


def test_the_services_own_llm_key_is_left_where_it_is(env, monkeypatch):
    """경계가 어디인지 고정합니다 — 지우는 것은 주문을 낼 수 있는 값뿐입니다.

    LLM 키는 서비스가 자기 비용으로 제공하기로 한 값이고 주문을 낼 수 없습니다.
    같이 지우면 자기 키가 없는 사용자의 AI 데스크가 통째로 꺼집니다.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "service-owned-llm-key")
    create_app(None, state_path=str(env / "state.db"))
    assert os.environ.get("ANTHROPIC_API_KEY") == "service-owned-llm-key"


@pytest.mark.parametrize("key", ["HTTP_PROXY", "HTTPS_PROXY", "https_proxy",
                                 "ALL_PROXY", "PATH", "LD_PRELOAD", "PYTHONPATH",
                                 "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"])
def test_process_control_variables_are_still_refused(client, key):
    """저장 경로가 .env 에서 계정 DB 로 바뀌었다고 규칙이 헐거워지면 안 됩니다."""
    a, _b = two_users(client)
    body = a.post("/api/setup", json={"values": {
        "KIS_APP_KEY": "real-key-aaaa", key: "http://127.0.0.1:8899"}}).json()

    assert key in body["rejected"]
    assert key not in body["written"]
    assert os.environ.get(key) != "http://127.0.0.1:8899"
    # 한 줄 때문에 폼 전체를 잃지는 않습니다.
    assert body["written"] == ["KIS_APP_KEY"]


def test_a_newline_in_a_value_cannot_smuggle_a_second_key(client):
    a, _b = two_users(client)
    body = a.post("/api/setup", json={"values": {
        "KIS_APP_KEY": "AK\nHTTPS_PROXY=http://127.0.0.1:8899"}}).json()

    assert body["written"] == []
    assert "KIS_APP_KEY" in body["rejected"]
    assert os.environ.get("HTTPS_PROXY") is None


@pytest.mark.parametrize("key", ["QUANT_API_TOKEN", "CORS_ORIGINS", "OPERATOR_NAME",
                                 "QUANT_LIMIT_DAILY_ORDERS", "QUANT_LIMIT_DAILY_LOSS"])
def test_service_wide_settings_cannot_be_set_from_an_account(client, key):
    """가입자 한 명이 배포 전체의 토큰이나 모두의 하루 한도를 정할 수는 없습니다."""
    a, _b = two_users(client)
    body = a.post("/api/setup", json={"values": {key: "9999"}}).json()

    assert body["written"] == []
    assert key in body["rejected"]
    assert os.environ.get(key) != "9999"


def test_the_account_screen_does_not_offer_the_operator_token_field(client):
    a, _b = two_users(client)
    offered = {f["env"] for f in a.get("/api/setup").json()["operator_fields"]}
    assert "QUANT_API_TOKEN" not in offered and "OPERATOR_NAME" not in offered
    # 계정에 속하는 것들은 그대로 남아 있어야 합니다. LLM 키는 하나만 —
    # 데스크 비용은 운영자가 내고, 자기 키를 넣고 싶은 사람에게만 제미나이를
    # 엽니다. 여러 공급자를 늘어놓으면 어느 것이 실제로 쓰이는지 알 수 없습니다.
    assert "GOOGLE_API_KEY" in offered and "TELEGRAM_BOT_TOKEN" in offered
    assert "ANTHROPIC_API_KEY" not in offered


def test_the_byo_llm_field_appears_exactly_once(client):
    """같은 칸이 두 번 서면 화면은 어느 쪽이 진짜인지 말해 주지 못합니다.

    `OPERATOR_FIELDS` 에 Gemini 키를 넣으면서, 계정용 설명을 붙여 덧대는
    줄이 그대로 남아 두 번 나왔습니다. 목록을 조립하는 코드가 둘로 갈려
    있으면 언제든 다시 생깁니다.
    """
    a, _b = two_users(client)
    fields = a.get("/api/setup").json()["operator_fields"]
    envs = [f["env"] for f in fields]
    assert len(envs) == len(set(envs)), f"중복된 칸: {envs}"
    assert envs.count("GOOGLE_API_KEY") == 1
    # 그리고 그 칸의 설명은 계정용이어야 합니다 — 단일 운영자용 문구가 아니라.
    label = next(f["label"] for f in fields if f["env"] == "GOOGLE_API_KEY")
    assert "한도" in label, f"계정 화면인데 설명이 운영자용입니다: {label}"


def test_every_venue_field_the_setup_screen_shows_is_writable_by_a_user(client):
    """허용 목록이 화면과 어긋나면 사용자가 입력한 키가 조용히 사라집니다."""
    a, _b = two_users(client)
    advertised = [env for venue in VENUES for env, _, _ in venue.fields]
    advertised += [env for env, _, _ in OPERATOR_FIELDS
                   if env in {f["env"] for f in a.get("/api/setup").json()["operator_fields"]}]
    body = a.post("/api/setup",
                  json={"values": {env: "v-" + env for env in advertised}}).json()

    assert body["rejected"] == {}
    assert sorted(body["written"]) == sorted(advertised)


def test_disconnecting_a_venue_only_touches_the_callers_own(client):
    a, b = two_users(client, a_secrets=A_KEYS, b_secrets=B_KEYS)

    out = a.post("/api/setup/disconnect/kis").json()
    assert sorted(out["removed"]) == sorted(A_KEYS)
    assert a.get("/api/setup").json()["configured"] == {}
    assert b.get("/api/setup").json()["configured"]["KIS_APP_KEY"] == \
        B_KEYS["KIS_APP_KEY"][-4:]


def test_a_blank_value_leaves_an_existing_credential_alone(client):
    a, _b = two_users(client, a_secrets=A_KEYS)
    body = a.post("/api/setup", json={"values": {"KIS_APP_KEY": ""}}).json()
    assert body["written"] == []
    assert a.get("/api/setup").json()["configured"]["KIS_APP_KEY"] == \
        A_KEYS["KIS_APP_KEY"][-4:]


# ── 하루 한도 ───────────────────────────────────────────────────────────
def test_daily_limits_belong_to_the_person_who_set_them(client):
    a, b = two_users(client)
    a.post("/api/limits", json={"max_daily_orders": 5, "max_daily_loss": 100_000})
    b.post("/api/limits", json={"max_daily_orders": 40})

    assert a.get("/api/limits").json()["configured"]["max_daily_orders"] == 5
    assert a.get("/api/limits").json()["configured"]["max_daily_loss"] == 100_000
    assert b.get("/api/limits").json()["configured"]["max_daily_orders"] == 40
    assert b.get("/api/limits").json()["configured"]["max_daily_loss"] == 0


def test_naming_someone_elses_id_in_the_body_changes_nothing(client):
    """id 는 세션에서만 옵니다. 본문에 적은 것은 이름 없는 필드일 뿐입니다."""
    a, b = two_users(client)
    b.post("/api/limits", json={"max_daily_orders": 40})

    body = a.post("/api/limits", json={"user_id": b.id, "id": b.id,
                                       "max_daily_orders": 5}).json()

    assert b.get("/api/limits").json()["configured"]["max_daily_orders"] == 40
    assert a.get("/api/limits").json()["configured"]["max_daily_orders"] == 5
    # 요청 모델이 모르는 필드는 레지스트리에 닿기도 전에 사라집니다 — "무시했다"
    # 고 보고할 것조차 없는 것이 가장 안전한 모양입니다.
    assert body["ignored"] == []


def test_a_user_id_in_the_query_string_changes_nothing(client):
    a, b = two_users(client)
    b.post("/api/limits", json={"max_daily_orders": 40})

    seen = a.get(f"/api/limits?user_id={b.id}&user={b.id}").json()
    assert seen["configured"]["max_daily_orders"] == 0


def test_limits_never_become_process_wide_again(client, tmp_path):
    """예전 `/api/limits` 는 `.env` 에 썼습니다 — 그러면 한 사람의 한도가 모두의 한도입니다."""
    a, b = two_users(client)
    a.post("/api/limits", json={"max_daily_orders": 5})

    for key in ("QUANT_LIMIT_DAILY_NOTIONAL", "QUANT_LIMIT_DAILY_ORDERS",
                "QUANT_LIMIT_DAILY_LOSS", "QUANT_LIMIT_DAILY_LOSS_PCT"):
        assert key not in os.environ
    assert not (tmp_path / "env.test").exists()
    assert b.get("/api/limits").json()["configured"]["max_daily_orders"] == 0


def test_partial_limit_updates_leave_the_others_alone(client):
    a, _b = two_users(client)
    a.post("/api/limits", json={"max_daily_notional": 50_000, "max_daily_orders": 20,
                                "max_daily_loss": 2_000, "max_daily_loss_pct": 0.03})
    body = a.post("/api/limits", json={"max_daily_orders": 25}).json()

    saved = a.get("/api/limits").json()["configured"]
    assert saved == {"max_daily_notional": 50_000, "max_daily_orders": 25,
                     "max_daily_loss": 2_000, "max_daily_loss_pct": 0.03}
    assert body["updated"] == ["max_daily_orders"] and body["removed"] == []


def test_an_explicit_zero_removes_a_cap_and_says_so(client):
    a, _b = two_users(client)
    a.post("/api/limits", json={"max_daily_loss": 2_000})
    body = a.post("/api/limits", json={"max_daily_loss": 0}).json()

    assert body["removed"] == ["max_daily_loss"]
    assert "해제" in body["note"]


# ── 투자 성향 ───────────────────────────────────────────────────────────
def test_investor_profiles_are_per_user(client):
    a, b = two_users(client)
    a.patch("/api/profile", json={"overrides": {"R": 0.8}})

    assert a.get("/api/profile").json()["overrides"] == {"R": 0.8}
    assert b.get("/api/profile").json()["overrides"] == {}


def test_a_leftover_profile_file_in_the_working_directory_belongs_to_nobody(
        client, tmp_path):
    """1인용 시절 파일 하나가 모두의 사이즈와 손절을 다시 정하면 안 됩니다."""
    (tmp_path / "investor_profile.json").write_text(
        json.dumps({"answers": {}, "overrides": {"R": -1.0}}), encoding="utf-8")
    a, _b = two_users(client)
    assert a.get("/api/profile").json()["overrides"] == {}


def test_clearing_an_override_only_clears_the_callers_own(client):
    a, b = two_users(client)
    a.patch("/api/profile", json={"overrides": {"R": 0.5}})
    b.patch("/api/profile", json={"overrides": {"R": -0.5}})

    a.delete("/api/profile/override/R")
    assert a.get("/api/profile").json()["overrides"] == {}
    assert b.get("/api/profile").json()["overrides"] == {"R": -0.5}


# ── 상태 파일 ───────────────────────────────────────────────────────────
def test_the_equity_curve_read_is_the_callers_own_state_file(client, app, env):
    """A 의 재시작이 B 의 과거를 복원하면, 두 사람의 손익이 한 장부에 섞입니다."""
    a, b = two_users(client)
    registry = app.state.registry
    assert registry.state_path(a.id) != registry.state_path(b.id)

    store = StateStore(registry.state_path(a.id))
    try:
        store.start_run("모의전략", "dry_run", 1_000_000.0)
        store.record_equity(datetime.now(UTC), 1_234_567.0, 1_000.0, 0.0)
    finally:
        store.close()

    # 프로세스 기본 설정이 있어야 resume_run 이 붙습니다 — 배포에서 `quant serve
    # configs/…` 로 뜨는 그 모양입니다.
    with TestClient(create_app(_paper_config(), state_path=str(env / "state.db")),
                    base_url="https://desk.example") as c2:
        a2 = Caller(c2, cookie=_login(c2, "a@example.com"))
        b2 = Caller(c2, cookie=_login(c2, "b@example.com"))
        assert len(a2.get("/api/equity").json()["points"]) == 1
        assert b2.get("/api/equity").json()["points"] == []
        assert b2.get("/api/trades").json()["trades"] == []


def _paper_config():
    from quant.config.schema import StrategyConfig

    return StrategyConfig.model_validate(PAPER)


def _login(client: TestClient, email: str) -> str:
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return client.cookies.get(SESSION_COOKIE)


# ── 실행 중인 봇 ────────────────────────────────────────────────────────
def test_two_people_run_their_own_bots_side_by_side(client):
    a, b = two_users(client)
    assert a.post("/api/trader/start", json={"config_path": "paper"}).status_code == 200
    assert b.post("/api/trader/start", json={"config_path": "paper"}).status_code == 200
    try:
        until_running(a)
        until_running(b)
        assert a.get("/api/health").json()["trader_running"] is True
        assert b.get("/api/health").json()["trader_running"] is True
    finally:
        a.post("/api/trader/stop")
        b.post("/api/trader/stop")


def test_one_persons_bot_is_invisible_and_untouchable_to_another(client):
    a, b = two_users(client)
    a.post("/api/trader/start", json={"config_path": "paper"})
    try:
        until_running(a)

        # B 는 자기 봇이 없습니다 — A 의 것이 보이지도, 잡히지도 않습니다.
        assert b.get("/api/status").json()["running"] is False
        assert b.get("/api/health").json()["trader_running"] is False
        assert b.get("/api/universe").json()["symbols"] == []
        assert b.get("/api/manual").json()["running"] is False

        for path in ("/api/manual/close_all", "/api/manual/pause",
                     "/api/manual/resume", "/api/limits/release",
                     "/api/trader/stop", "/api/trader/sync"):
            r = b.post(path, json={})
            assert r.status_code == 404, f"{path} → {r.status_code}"
        for path in ("/api/manual/buy", "/api/manual/sell", "/api/manual/close"):
            r = b.post(path, json={"ticker": "SIM1", "quantity": 1})
            assert r.status_code == 404, f"{path} → {r.status_code}"

        # A 의 봇은 아무 일도 겪지 않았습니다.
        mine = a.get("/api/manual").json()
        assert mine["running"] is True and mine["paused"] is False
        assert mine["pending"] == []
        assert a.get("/api/status").json()["running"] is True
    finally:
        a.post("/api/trader/stop")


def test_a_second_start_is_refused_for_that_person_only(client):
    a, b = two_users(client)
    a.post("/api/trader/start", json={"config_path": "paper"})
    try:
        until_running(a)
        again = a.post("/api/trader/start", json={"config_path": "paper"})
        assert again.status_code == 409
        assert again.json()["code"] == "already_running"
        # 남의 봇이 돌고 있다고 내가 못 뜨는 것은 아닙니다.
        assert b.post("/api/trader/start", json={"config_path": "paper"}).status_code == 200
        until_running(b)
    finally:
        a.post("/api/trader/stop")
        b.post("/api/trader/stop")


def test_the_event_stream_is_not_shared(client, app):
    """남의 체결과 평가액이 내 화면에 흐르는 것도 격리가 깨진 것입니다."""
    a, b = two_users(client)
    app.state.quant.hub_for(a.id).ring.append(
        {"type": "order_filled", "ts": "2026-01-01T00:00:00+00:00",
         "source": "engine", "payload": {"ticker": "SIM1", "quantity": 3}})

    assert len(a.get("/api/events").json()["events"]) == 1
    assert b.get("/api/events").json()["events"] == []


def test_a_websocket_needs_a_session(client):
    a, _b = two_users(client)
    client.cookies.clear()
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/ws") as ws:
        ws.receive_text()
    assert exc.value.code == 4401

    with client.websocket_connect("/ws", headers=ws_headers(a.cookie)):
        pass


def test_a_socket_joins_only_its_own_fan_out(client, app):
    """소켓이 어느 링에 붙는지가 곧 누구의 체결을 보게 되는지입니다."""
    a, b = two_users(client)
    hubs = app.state.quant
    hubs.hub_for(a.id).ring.append(
        {"type": "order_filled", "ts": "2026-01-01T00:00:00+00:00",
         "source": "engine", "payload": {"ticker": "SIM1"}})

    client.cookies.clear()
    with client.websocket_connect("/ws", headers=ws_headers(a.cookie)) as ws:
        # 붙자마자 자기 링을 재생받습니다.
        assert json.loads(ws.receive_text())["payload"]["ticker"] == "SIM1"

    with client.websocket_connect("/ws", headers=ws_headers(b.cookie)):
        # B 는 B 의 링에만 앉아 있습니다 — A 의 다음 체결은 오지 않습니다.
        assert len(hubs.hub_for(b.id).clients) == 1
        assert hubs.hub_for(a.id).clients == set()


# ── 시작 전 점검 ────────────────────────────────────────────────────────
def test_starting_without_credentials_names_what_is_missing(client):
    a, _b = two_users(client)
    r = a.post("/api/trader/start", json={"config_path": "kis"})

    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "credentials_missing"
    assert {item["name"] for item in body["missing"]} >= {"KIS_APP_KEY", "KIS_APP_SECRET"}
    # 무엇이 필요한지는 말해도, 남의 값이나 내 값이 문장에 섞이지는 않습니다.
    assert "app-key" not in r.text


def test_credentials_registered_by_one_person_do_not_start_anothers_bot(client):
    a, b = two_users(client, a_secrets=A_KEYS)
    assert a.get("/api/setup").json()["configured"].get("KIS_APP_KEY")
    r = b.post("/api/trader/start", json={"config_path": "kis"})
    assert r.status_code == 400 and r.json()["code"] == "credentials_missing"


def test_a_user_cannot_name_a_file_on_the_server(client, templates):
    """설정 경로를 받으면 그건 설정 선택이 아니라 파일 열람입니다."""
    _root, outside = templates
    a, _b = two_users(client)
    for path in (str(outside), "../secret_strategy", "/etc/passwd",
                 "../../etc/passwd.yaml"):
        r = a.post("/api/trader/start", json={"config_path": path})
        assert r.status_code == 400, path
        assert "템플릿" in r.json()["detail"]


def test_the_catalog_is_what_a_user_may_start(client, templates):
    a, _b = two_users(client)
    listed = {s["id"] for s in a.get("/api/strategies").json()["strategies"]}
    assert listed == set(strategy_catalog())
    assert "kis" in listed and "secret_strategy" not in listed


def test_live_still_needs_the_strategy_name_typed_back_by_this_person(client):
    a, _b = two_users(client)
    r = a.post("/api/trader/start",
               json={"config_path": "live", "mode": "live", "confirm": "wrong"})
    assert r.status_code == 400
    assert "실거래전략" in r.json()["detail"]
    assert a.get("/api/status").json()["running"] is False


def test_a_backtest_cannot_carry_an_arbitrary_config(client):
    a, _b = two_users(client)
    r = a.post("/api/backtest", json={"config": PAPER})
    assert r.status_code == 400
    assert "템플릿" in r.json()["detail"]


# ── 관리자와 1인용 토큰 ─────────────────────────────────────────────────
def test_only_an_admin_reads_the_user_list(client):
    a, b = two_users(client)
    assert a.get("/api/admin/users").status_code == 200
    assert b.get("/api/admin/users").status_code == 403


def test_the_user_list_carries_no_credentials(client):
    a, _b = two_users(client, a_secrets=A_KEYS, b_secrets=B_KEYS)
    text = a.get("/api/admin/users").text
    for value in {**A_KEYS, **B_KEYS}.values():
        assert value not in text
    assert "password" not in text and "hash" not in text


@pytest.fixture
def token_app(env, monkeypatch):
    monkeypatch.setenv("QUANT_API_TOKEN", "t" * 40)
    with TestClient(create_app(None, state_path=str(env / "state.db")),
                    base_url="https://desk.example") as c:
        yield c


def test_the_shared_token_opens_nothing_once_people_have_accounts(token_app):
    """공용 토큰은 사람을 대신하지 않습니다.

    한 개의 값이 관리자 자리를 열면 그것은 로그인이 아니라 로그인의 우회입니다.
    게다가 그 값은 주소창과 프록시 접근 로그를 타고 서비스 밖으로 흐릅니다 —
    로그 한 줄을 본 사람이 남의 계좌를 청산할 수 있게 됩니다. 가입자가 하나라도
    있으면 세션 쿠키만이 사람을 정합니다.
    """
    admin, _other = two_users(token_app)
    admin.post("/api/limits", json={"max_daily_orders": 7})

    machine = Caller(token_app, token="t" * 40)
    assert machine.get("/api/limits").status_code == 401
    assert machine.get("/api/admin/users").status_code == 401
    assert machine.post("/api/manual/close_all").status_code == 401


def test_a_session_cookie_is_the_only_thing_that_seats_you(token_app):
    """토큰을 함께 보내도 결과가 달라지지 않아야 합니다."""
    _admin, other = two_users(token_app)
    both = Caller(token_app, cookie=other.cookie, token="t" * 40)
    assert both.get("/api/auth/me").json()["email"] == "b@example.com"
    # 관리자가 아닌 사람이 토큰을 들었다고 관리자가 되지 않습니다.
    assert both.get("/api/admin/users").status_code == 403


def test_a_wrong_token_is_not_a_seat(token_app):
    two_users(token_app)
    assert Caller(token_app, token="wrong").get("/api/limits").status_code == 401


def test_the_token_alone_says_to_sign_up_first(token_app):
    """아직 아무도 없으면 토큰이 대신할 사람도 없습니다."""
    r = Caller(token_app, token="t" * 40).get("/api/limits")
    assert r.status_code == 401
    assert "가입" in r.json()["detail"]


# ── 뜨지 말아야 할 상태 ─────────────────────────────────────────────────
def test_the_service_refuses_to_start_without_an_encryption_key(monkeypatch):
    monkeypatch.delenv("QUANT_SECRET_KEY", raising=False)
    with pytest.raises(SecretKeyMissing) as exc:
        assert_ready_for_users()
    assert "QUANT_SECRET_KEY" in str(exc.value)


@pytest.mark.parametrize("value", ["", "   ", "short-key", "x" * 31])
def test_a_blank_or_short_encryption_key_does_not_count(monkeypatch, value):
    monkeypatch.setenv("QUANT_SECRET_KEY", value)
    with pytest.raises(SecretKeyMissing):
        assert_ready_for_users()


def test_an_encryption_key_lets_it_through(monkeypatch):
    monkeypatch.setenv("QUANT_SECRET_KEY", SECRET)
    assert_ready_for_users()


def test_user_files_land_beside_the_state_db_by_default(tmp_path, monkeypatch):
    """호스팅은 영구 디스크를 하나만 붙입니다 — 셋이 흩어지면 하나만 살아남습니다.

    작업 디렉터리에 남은 포지션 기록은 재배포와 함께 사라지고, 봇은 자기가
    무엇을 들고 있었는지 모르는 채로 다시 뜹니다.
    """
    from pathlib import Path

    monkeypatch.setenv("QUANT_SECRET_KEY", SECRET)
    disk = tmp_path / "var-data"
    disk.mkdir()

    app = create_app(None, state_path=str(disk / "quant_state.db"))
    with TestClient(app, base_url="https://desk.example") as c:
        a = signup(c, "a@example.com")

    assert (disk / "quant_users.db").exists()
    assert Path(app.state.registry.state_path(a.id)).parent.parent == disk / "users"


def test_quant_serve_refuses_without_an_encryption_key(monkeypatch, capsys):
    """`assert_safe_to_bind` 와 같은 자리에서, 같은 방식으로 끝냅니다."""
    from quant.cli import main

    monkeypatch.delenv("QUANT_SECRET_KEY", raising=False)
    monkeypatch.setenv("QUANT_API_TOKEN", "t" * 40)
    assert main(["serve", "--host", "127.0.0.1"]) == 2
    assert "QUANT_SECRET_KEY" in capsys.readouterr().err


# ── id 를 찍어 넣을 자리가 없다 ─────────────────────────────────────────
def test_no_route_takes_a_user_id(app):
    """가장 확실한 접근 제어는 남의 id 를 적을 칸이 아예 없는 것입니다."""
    for route in app.routes:
        for name in getattr(route, "param_convertors", {}):
            assert "user" not in name.lower(), f"{route.path} 가 {name} 를 받습니다"
            assert name not in ("id", "uid", "account"), route.path
