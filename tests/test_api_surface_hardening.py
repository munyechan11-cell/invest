"""API 표면이 새거나 멈추는 다섯 자리.

여기서 고정하는 것은 "엔드포인트가 200 을 준다" 가 아닙니다. 가입한 사람
하나가 **다른 사람의 것에 닿거나, 다른 사람의 봇을 세울 수 있는가** 입니다.

* 공유 토큰(`QUANT_API_TOKEN`) 하나가 로그인을 건너뛰고 관리자 자리에 앉는가
* `/api/config` 가 운영자의 client_id 와 계좌번호를 가입자에게 건네는가
* 백테스트 한 번이 이벤트 루프를 붙잡아 모두의 봇을 멈추는가
* 자격증명 하나가 디스크를 얼마든지 먹을 수 있는가 — 그 디스크에는 모든
  사용자의 포지션과 체결 기록이 함께 있습니다
* 조회 한도 하나가 500 이 되는가

실제 브로커 엔드포인트는 어디서도 호출하지 않습니다 — 전부 synthetic 시세와
paper 브로커입니다.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from quant.api.server import (
    MAX_BACKTEST_DAYS,
    MAX_CONCURRENT_BACKTESTS,
    MAX_SECRET_LEN,
    _credential_fields,
    create_app,
)
from quant.config.schema import StrategyConfig
from quant.webapp import accounts as accounts_module
from quant.webapp.auth_api import SESSION_COOKIE

#: 이 파일의 async 헬퍼들은 평문 http 로 부릅니다. `__Host-` 는 Secure 없이는
#: 브라우저가 저장하지 않으므로 서버도 그때는 접두사 없는 이름을 봅니다.
_PLAIN_COOKIE = SESSION_COOKIE.replace("__Host-", "")

SECRET = "api-surface-hardening-secret-0123456789abcd"
TOKEN = "t" * 40
PASSWORD = "korea-invest-1"

#: 운영자의 값들. 응답 어디에 나타나도 실패입니다 — client_id 와 계좌번호는
#: 열쇠가 아니라 신원이고, 남에게 줄 이유는 열쇠와 똑같이 없습니다.
CLIENT_ID = "OPERATOR-TOSS-CLIENTID-CANARY"
ACCOUNT_NO = "OPERATOR-TOSS-ACCOUNTNO-CANARY"
CHAT_ID = "OPERATOR-TELEGRAM-CHAT-CANARY"

OPERATOR_TEMPLATE = {
    "name": "운영자템플릿",
    "mode": "dry_run",
    "data": {"provider": "toss", "timeframe": "1d", "calendar": "always_open",
             "warmup_bars": 60,
             "params": {"client_id": CLIENT_ID, "client_secret": "OPERATOR-SECRET"}},
    "universe": {"symbols": [{"ticker": "005930", "venue": "KRX",
                              "quote_currency": "KRW"}]},
    "alpha": [{"type": "ema_cross"}],
    "broker": {"type": "toss",
               "params": {"client_id": CLIENT_ID, "client_secret": "OPERATOR-SECRET",
                          "account_no": ACCOUNT_NO}},
    "notify": {"telegram_chat_id": CHAT_ID},
}

#: 백테스트가 CPU 를 실제로 오래 쓰도록 — 그래야 "루프를 붙잡는가" 를 잽니다.
SLOW_BACKTEST = {
    "name": "느린백테스트",
    "mode": "backtest",
    "data": {"provider": "synthetic", "timeframe": "1d", "calendar": "always_open",
             "warmup_bars": 60},
    "universe": {"symbols": [{"ticker": f"SIM{i}"} for i in range(1, 5)]},
    "alpha": [{"type": "ema_cross"}, {"type": "rsi_reversion"}],
    "broker": {"type": "paper"},
    "backtest": {"start": "2021-01-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"},
}


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    """이 파일이 재는 것은 해시 강도가 아닙니다 — 가입은 빠르면 됩니다."""
    monkeypatch.setattr(accounts_module, "_PBKDF2_ROUNDS", 1_000, raising=False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "templates"
    root.mkdir()
    (root / "slow.yaml").write_text(
        yaml.safe_dump(SLOW_BACKTEST, allow_unicode=True), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUANT_SECRET_KEY", SECRET)
    monkeypatch.setenv("QUANT_API_TOKEN", TOKEN)
    monkeypatch.setenv("QUANT_USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setenv("QUANT_USER_DATA", str(tmp_path / "userdata"))
    monkeypatch.setenv("QUANT_ENV_FILE", str(tmp_path / "env.test"))
    monkeypatch.setenv("QUANT_CONFIG_DIR", str(root))
    monkeypatch.setenv("QUANT_PROFILE_FILE", str(tmp_path / "profile.json"))
    return tmp_path


@pytest.fixture
def app(env):
    return create_app(StrategyConfig.model_validate(OPERATOR_TEMPLATE),
                      state_path=str(env / "state.db"))


@pytest.fixture
def client(app):
    with TestClient(app, base_url="https://desk.example") as c:
        yield c


def signup(client: TestClient, email: str) -> str:
    """가입하고 그 사람의 세션 쿠키만 돌려줍니다."""
    client.cookies.clear()
    r = client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    cookie = client.cookies.get(SESSION_COOKIE)
    client.cookies.clear()
    assert cookie
    return cookie


def as_user(client: TestClient, cookie: str) -> dict:
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, cookie)
    return {}


def as_token(client: TestClient) -> dict:
    """쿠키 없이 공유 토큰만 든 호출자 — 예전의 '기계' 경로."""
    client.cookies.clear()
    return {"Authorization": f"Bearer {TOKEN}"}


# ── (1) 공유 토큰은 아무도 인증하지 않는다 ──────────────────────────────
@pytest.mark.parametrize("path", ["/api/limits", "/api/setup", "/api/admin/users",
                                  "/api/status", "/api/config"])
def test_the_shared_token_reads_nothing_once_an_account_exists(client, path):
    signup(client, "admin@example.com")
    r = client.get(path, headers=as_token(client))
    assert r.status_code == 401, f"{path} → {r.status_code}: {r.text[:200]}"


@pytest.mark.parametrize("path,body", [
    ("/api/trader/start", {"config_path": "slow"}),
    ("/api/manual/close_all", None),
    ("/api/setup", {"values": {"KIS_APP_KEY": "ATTACKER-SWAPPED-KEY"}}),
    ("/api/backtest", {"config_path": "slow"}),
])
def test_the_shared_token_moves_nothing_once_an_account_exists(client, path, body):
    signup(client, "admin@example.com")
    r = client.post(path, json=body, headers=as_token(client))
    assert r.status_code == 401, f"{path} → {r.status_code}: {r.text[:200]}"


@pytest.mark.parametrize("method,path", [("get", "/api/setup"),
                                         ("get", "/api/admin/users"),
                                         ("post", "/api/trader/start"),
                                         ("post", "/api/manual/close_all")])
def test_a_token_in_the_query_string_is_not_a_seat(client, method, path):
    """접근 로그 한 줄이 관리자 자리가 되면 안 됩니다.

    리버스 프록시도 브라우저 히스토리도 요청 줄 전체를 남기고, `Referer` 는
    그것을 바깥으로 들고 나갑니다.
    """
    signup(client, "admin@example.com")
    client.cookies.clear()
    body = {"config_path": "slow"} if method == "post" else None
    r = client.request(method.upper(), f"{path}?token={TOKEN}", json=body)
    assert r.status_code == 401


def test_the_websocket_does_not_open_on_a_query_token_either(client):
    signup(client, "admin@example.com")
    client.cookies.clear()
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(f"/ws?token={TOKEN}"):
        pass


def test_the_signed_in_person_still_gets_their_own_desk(client):
    """토큰을 끊는 것이 로그인한 사람을 끊는 것이면 안 됩니다."""
    cookie = signup(client, "admin@example.com")
    r = client.get("/api/limits", headers=as_user(client, cookie))
    assert r.status_code == 200
    assert client.get("/api/admin/users").status_code == 200


def test_the_refusal_says_where_to_go(client):
    signup(client, "admin@example.com")
    r = client.get("/api/limits", headers=as_token(client))
    assert r.status_code == 401
    assert "가입" in r.json()["detail"] or "로그인" in r.json()["detail"]


# ── (1b) 계정이 없는 배포 — 1인용 경로는 남되, 닫히는 조건이 있다 ────────
@pytest.fixture
def solo(env, monkeypatch):
    """가입자가 없는 배포. 서버 주인과 계좌 주인이 같은 사람입니다."""
    monkeypatch.delenv("QUANT_SECRET_KEY", raising=False)
    with TestClient(create_app(StrategyConfig.model_validate(OPERATOR_TEMPLATE),
                               state_path=str(env / "state.db"))) as c:
        yield c


def test_an_app_without_accounts_serves_nobody(solo):
    """계정 없이 조립된 앱은 어떤 요청도 받지 않습니다.

    예전에는 이 상태에서 공유 토큰 하나가 전부를 열었습니다. 이제는 토큰이
    있든 없든 같은 답입니다 — 사람을 정하는 것은 세션 쿠키뿐이고, 계정을
    만들 수 없는 프로세스에는 앉을 자리가 없습니다.
    """
    assert solo.get("/api/limits").status_code in (401, 503)
    assert solo.get("/api/limits",
                    headers={"Authorization": f"Bearer {TOKEN}"}).status_code in (401, 503)


def test_a_url_token_opens_nothing(solo):
    """공유 토큰은 더 이상 존재하지 않습니다.

    값 하나가 자리를 열면 그것은 로그인이 아니라 로그인의 우회이고, 쿼리
    문자열에 실리면 프록시 접근 로그와 브라우저 히스토리에 그대로 남습니다.
    """
    assert solo.get(f"/api/setup?token={TOKEN}").status_code in (401, 503)
    assert solo.get("/api/setup",
                    headers={"Authorization": f"Bearer {TOKEN}"}).status_code in (401, 503)


def test_the_socket_authenticates_by_cookie_not_by_url(solo):
    """브라우저 WebSocket 은 헤더를 못 붙이지만 쿠키는 붙습니다.

    그래서 `?token=` 이 필요한 자리가 남아 있지 않습니다 — 로그인하지 않은
    소켓은 그냥 닫힙니다.
    """
    with pytest.raises(WebSocketDisconnect), solo.websocket_connect(f"/ws?token={TOKEN}"):
        pass


def test_the_single_operator_path_closes_the_moment_someone_registers(env, monkeypatch):
    """키가 사라졌다고 공유 토큰이 다시 전부를 열면 안 됩니다.

    `QUANT_SECRET_KEY` 가 빠지면 계정을 열 수 없어 1인용으로 되돌아가는데,
    가입자들의 데이터는 그대로 디스크에 남아 있습니다. 그 자리에서 토큰 하나가
    다시 모든 것을 여는 것은 로그인을 없애는 것과 같습니다.
    """
    with TestClient(create_app(None, state_path=str(env / "state.db")),
                    base_url="https://desk.example") as c:
        signup(c, "admin@example.com")

    monkeypatch.delenv("QUANT_SECRET_KEY", raising=False)
    with TestClient(create_app(None, state_path=str(env / "state.db")),
                    base_url="https://desk.example") as c:
        r = c.get("/api/limits", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 503
        assert "QUANT_SECRET_KEY" in r.json()["detail"]


# ── (2) /api/config 는 운영자의 신원을 건네지 않는다 ─────────────────────
def test_a_fresh_account_reads_no_operator_credential_from_config(client):
    stranger = signup(client, "stranger@evil.example")
    r = client.get("/api/config", headers=as_user(client, stranger))
    assert r.status_code == 200
    for value in (CLIENT_ID, ACCOUNT_NO, CHAT_ID, "OPERATOR-SECRET"):
        assert value not in r.text, f"{value} 가 그대로 나갔습니다"


def test_the_config_still_describes_the_strategy(client):
    """가리는 것과 없애는 것은 다릅니다 — 화면은 전략 이름과 종류를 봅니다."""
    stranger = signup(client, "stranger@evil.example")
    body = client.get("/api/config", headers=as_user(client, stranger)).json()
    assert body["name"] == "운영자템플릿"
    assert body["broker"]["type"] == "toss"
    # 운영자의 배선값은 가입자의 것이 아닙니다 — 가리는 게 아니라 비웁니다.
    assert body["broker"]["params"] == {}
    assert body["data"]["params"] == {}


def test_the_config_screen_never_returns_a_credential(client):
    """설정을 보여주되 값은 돌려주지 않습니다 — 이름만 남습니다."""
    body = client.get("/api/config").json()
    params = body.get("broker", {}).get("params", {})
    for field in ("client_id", "account_no", "app_key", "app_secret",
                  "api_key", "secret_key", "secret"):
        if field in params:
            assert params[field] == "***", field
    notify = body.get("notify") or {}
    if "telegram_chat_id" in notify:
        assert notify["telegram_chat_id"] == "***"


@pytest.mark.parametrize("broker,provider,expected", [
    ("kis", "kis", {"app_key", "app_secret", "account_no", "product_code"}),
    ("toss", "toss", {"client_id", "client_secret", "account_no"}),
    ("alpaca", "synthetic", {"api_key", "secret_key"}),
    ("ccxt", "ccxt", {"api_key", "secret"}),
])
def test_the_redaction_list_comes_from_the_wiring_table(broker, provider, expected):
    """이름을 손으로 적어두면 거래소가 하나 늘 때 조용히 어긋납니다.

    `client_id` 와 `account_no` 가 정확히 그렇게 빠져나갔습니다 — 이름에
    key/secret/token 이 없다는 이유로 통과했습니다.
    """
    from quant.webapp.registry import _targets

    cfg = StrategyConfig.model_validate({
        **OPERATOR_TEMPLATE,
        "data": {"provider": provider, "timeframe": "1d", "calendar": "always_open",
                 "warmup_bars": 60, "params": {"exchange": "binance"}},
        "broker": {"type": broker, "params": {"exchange": "binance"}},
    })
    fields = _credential_fields(cfg)
    assert expected <= fields
    declared = {arg for _params, wiring in _targets(cfg) for arg in wiring.args}
    assert declared <= fields, "배선표가 선언한 이름이 가려지지 않습니다"
    assert {"telegram_bot_token", "telegram_chat_id"} <= fields


# ── (3) 백테스트는 남의 봇을 세우지 않는다 ──────────────────────────────
async def _backtest_while_the_loop_watches(app, cookie: str, other: str) -> tuple:
    """백테스트를 돌리는 동안 0.25초 타이머가 제때 깨어나는지.

    모든 사용자의 봇이 이 루프 위의 태스크입니다. 타이머 하나가 20초 늦게
    깨어난다는 것은 그동안 아무의 봇도 한 틱을 못 돌았다는 뜻입니다.
    """
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://srv",
                                     timeout=120) as c:
            job = asyncio.create_task(
                c.post("/api/backtest", json={"config_path": "slow"},
                       cookies={_PLAIN_COOKIE: cookie}))
            started = time.monotonic()
            await asyncio.sleep(0.25)
            late = time.monotonic() - started - 0.25
            result = await job
            elapsed = time.monotonic() - started
            served = await c.get("/api/status", cookies={_PLAIN_COOKIE: other})
            return result, late, elapsed, served


def test_a_backtest_does_not_freeze_everyone_elses_bot(app, client):
    mine = signup(client, "carol@example.com")
    theirs = signup(client, "bob@example.com")
    result, late, elapsed, served = asyncio.run(
        _backtest_while_the_loop_watches(app, mine, theirs))

    assert result.status_code == 200, result.text[:300]
    assert served.status_code == 200
    # 루프 위에서 그대로 돌리면 이 타이머는 백테스트가 끝날 때까지 잠듭니다.
    assert late < max(0.5, elapsed / 3), (
        f"백테스트 {elapsed:.1f}초 동안 이벤트 루프가 {late:.1f}초 멈췄습니다")


async def _two_at_once(app, cookie: str) -> list[int]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://srv",
                                     timeout=120) as c:
            async def one():
                return await c.post("/api/backtest", json={"config_path": "slow"},
                                    cookies={_PLAIN_COOKIE: cookie})

            return sorted(r.status_code for r in await asyncio.gather(one(), one()))


def test_one_backtest_at_a_time_per_person(app, client):
    """클릭 한 번에 스레드 하나씩이면 상한이 없는 것과 같습니다."""
    mine = signup(client, "carol@example.com")
    assert asyncio.run(_two_at_once(app, mine)) == [200, 429]
    assert MAX_CONCURRENT_BACKTESTS >= 1


def test_a_backtest_window_is_bounded(client):
    mine = signup(client, "carol@example.com")
    r = client.post("/api/backtest",
                    json={"config_path": "slow", "start": "1900-01-01T00:00:00Z",
                          "end": "2026-01-01T00:00:00Z"},
                    headers=as_user(client, mine))
    assert r.status_code == 400
    assert str(MAX_BACKTEST_DAYS) in r.json()["detail"]


@pytest.mark.parametrize("body", [
    {"config_path": "slow", "start": "어제"},
    {"config_path": "slow", "start": "2026-01-01T00:00:00Z", "end": "2021-01-01T00:00:00Z"},
])
def test_a_bad_backtest_window_is_a_bad_request_not_a_crash(client, body):
    mine = signup(client, "carol@example.com")
    r = client.post("/api/backtest", json=body, headers=as_user(client, mine))
    assert r.status_code == 400


# ── (4) 자격증명에는 크기 상한이 있다 ───────────────────────────────────
def test_a_four_megabyte_credential_is_refused(client):
    """가입은 공짜고 저장할 이름은 스무 개 남짓입니다.

    상한이 없으면 계정 하나가 영구 디스크를 채우고, 그 디스크에는 모든
    사용자의 포지션과 체결 기록이 함께 삽니다.
    """
    mine = signup(client, "carol@example.com")
    r = client.post("/api/setup", json={"values": {"KIS_APP_KEY": "A" * 4_194_304}},
                    headers=as_user(client, mine))
    # 413(본문 자체가 큼) 이든 422(값이 김) 든, 저장되지 않는 것이 요점입니다.
    assert r.status_code in (413, 422), r.status_code
    assert client.get("/api/setup").json()["configured"] == {}


def test_the_cap_is_just_above_a_real_key(client):
    mine = signup(client, "carol@example.com")
    ok = client.post("/api/setup",
                     json={"values": {"KIS_APP_KEY": "A" * MAX_SECRET_LEN}},
                     headers=as_user(client, mine))
    assert ok.status_code == 200
    too_long = client.post("/api/setup",
                           json={"values": {"KIS_APP_SECRET": "A" * (MAX_SECRET_LEN + 1)}},
                           headers=as_user(client, mine))
    assert too_long.status_code == 422


def test_a_flood_of_keys_in_one_request_is_refused(client):
    mine = signup(client, "carol@example.com")
    r = client.post("/api/setup",
                    json={"values": {f"KEY_{i}": "x" for i in range(500)}},
                    headers=as_user(client, mine))
    assert r.status_code == 422


# ── (5) 조회 한도는 핸들러에 닿기 전에 걸린다 ───────────────────────────
@pytest.mark.parametrize("path", ["/api/equity", "/api/trades", "/api/events",
                                  "/api/desk"])
@pytest.mark.parametrize("limit", ["99999999999999999999", "-1", "0", "999999999"])
def test_a_limit_out_of_range_is_rejected_before_the_handler(client, path, limit):
    """`limit=<거대한 수>` 는 sqlite 안에서 터져 500 이 됐습니다.

    로그인한 아무나 누를 수 있는 크래시 경로였고, 음수는 조용히 빈 목록을
    돌려줬습니다 — 같은 누락의 다른 얼굴입니다.
    """
    mine = signup(client, "carol@example.com")
    r = client.get(f"{path}?limit={limit}", headers=as_user(client, mine))
    assert r.status_code == 422, f"{path}?limit={limit} → {r.status_code}"


@pytest.mark.parametrize("path,limit", [("/api/equity", 1200), ("/api/trades", 100),
                                        ("/api/events", 100), ("/api/desk", 1)])
def test_the_screen_still_gets_what_it_asks_for(client, path, limit):
    """대시보드가 실제로 보내는 값들 — 상한이 화면을 막으면 안 됩니다."""
    mine = signup(client, "carol@example.com")
    assert client.get(f"{path}?limit={limit}",
                      headers=as_user(client, mine)).status_code == 200
