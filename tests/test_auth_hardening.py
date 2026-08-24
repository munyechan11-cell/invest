"""계정이 없는 사람이 서비스 전체를 멈출 수 있는가 — 여기서 답은 "아니오" 입니다.

`test_auth_api.py` 가 정문의 모양(쿠키 속성, 구별되지 않는 실패, 시도 제한)을
고정한다면, 이 파일은 그 정문이 **부하와 악의** 아래에서도 같은 모양인지를
봅니다. 확인하는 것은 다섯 가지입니다.

* **해시는 이벤트 루프 밖에서 돈다.** 이 프로세스에는 모든 사용자의 봇이 함께
  삽니다. 가입 요청 여섯 개가 루프를 붙잡으면 그동안 어떤 봇도 봉을 처리하지
  못하고, 손절도 전량청산 버튼도 답하지 않습니다.
* **가입에도 한도가 있다.** 가입은 이 서비스에서 가장 비싼 무인증 쓰기입니다.
* **주소는 프록시가 말해줄 때만 믿는다.** 헤더를 돌려가며 주소별 한도를
  걸어나갈 수 있으면 그 한도는 없는 것과 같습니다.
* **모르는 사람이 주인을 잠글 수 없다.** 맞는 비밀번호는 언제나 들어옵니다.
* **세션 쿠키는 한 장뿐이다.** `__Host-` 접두사와, 두 장이면 거절하는 규칙.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from quant.webapp import accounts as accounts_mod
from quant.webapp.accounts import Accounts, User
from quant.webapp.auth_api import SESSION_COOKIE, LoginRateLimiter, build_auth

#: 평문 http 로 오는 요청에는 서버가 `__Host-` 를 떼고 냅니다 — 그 접두사는
#: Secure 를 요구해서, 붙인 채로 http 에 내면 브라우저가 버립니다.
_PLAIN_COOKIE = SESSION_COOKIE.replace("__Host-", "")

SECRET = "k" * 48
GOOD = "hunter2-secret"       # 10자 이상 + 숫자와 문자
WRONG = "totally-wrong9"


@pytest.fixture(autouse=True)
def _fast_hashing(monkeypatch):
    """반복 횟수는 accounts 의 관심사입니다. 여기서 재는 것은 어디서 도는가입니다."""
    monkeypatch.setattr(accounts_mod, "_PBKDF2_ROUNDS", 1_000)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("QUANT_COOKIE_SECURE", "QUANT_TRUSTED_PROXIES"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def accounts(tmp_path) -> Accounts:
    store = Accounts(tmp_path / "accounts.db", secret=SECRET)
    yield store
    store.close()


def build_app(accounts: Accounts, **limits) -> FastAPI:
    """인증 라우터 + 보호된 라우트 하나. limits 는 이 테스트가 쓸 한도."""
    auth = build_auth(accounts, limiter=LoginRateLimiter(**limits) if limits else None)
    app = FastAPI()
    app.include_router(auth.router)

    @app.get("/api/mine")
    async def mine(user: User = Depends(auth.current_user)):
        return {"email": user.email}

    return app


def register(client, email, password=GOOD, **kw):
    return client.post("/api/auth/register",
                       json={"email": email, "password": password, "display_name": ""}, **kw)


def login(client, email, password, **kw):
    return client.post("/api/auth/login", json={"email": email, "password": password}, **kw)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


# ── 해시는 루프 밖에서 ───────────────────────────────────────────────────
async def heartbeat_lag(app: FastAPI, requests, settle: float = 0.05):
    """요청들을 동시에 던지고, 그동안 봇의 심장이 멈춘 최대 시간을 잽니다.

    `requests` 는 `(client) -> coroutine` 목록입니다. 심장 태스크는
    `LiveTrader.run()` 의 자리입니다 — 같은 루프 위에 있고, 멈추면 손절이
    멈춥니다. 느린 것을 `time.sleep` 으로 흉내내는 이유는 PBKDF2 도 GIL 을
    놓기 때문입니다: 여기서 가르는 것은 해시가 핸들러 안에서 그대로 도는가,
    스레드로 나가는가 하나뿐입니다.
    """
    lag = {"max": 0.0}

    async def beat():
        while True:
            t0 = time.monotonic()
            await asyncio.sleep(0.01)
            lag["max"] = max(lag["max"], time.monotonic() - t0 - 0.01)

    task = asyncio.create_task(beat())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://desk") as client:
        await asyncio.sleep(settle)
        lag["max"] = 0.0
        started = time.monotonic()
        answers = await asyncio.gather(*[make(client) for make in requests])
        elapsed = time.monotonic() - started
    task.cancel()
    return answers, elapsed, lag["max"]


def slowly(monkeypatch, accounts: Accounts, name: str, seconds: float = 0.25):
    """`accounts.<name>` 을 느리게 만듭니다 — 해시 한 번의 비용 자리."""
    real = getattr(accounts, name)

    def slow(*args, **kwargs):
        time.sleep(seconds)
        return real(*args, **kwargs)

    monkeypatch.setattr(accounts, name, slow)


async def test_a_register_flood_does_not_stall_the_loop_the_bots_live_on(
        accounts, monkeypatch):
    """계정 없는 사람 여섯이 봇의 심장을 멈추면 안 됩니다."""
    slowly(monkeypatch, accounts, "register")
    app = build_app(accounts, per_register=100, per_address=1_000, per_global=10_000)

    answers, elapsed, lag = await heartbeat_lag(app, [
        (lambda c, i=i: c.post("/api/auth/register",
                               json={"email": f"flood{i}@example.com", "password": GOOD}))
        for i in range(6)])

    assert [r.status_code for r in answers] == [201] * 6
    # 루프 위에서 줄줄이 돌면 1.5초입니다. 스레드풀이면 한 번 값에 가깝습니다.
    assert elapsed < 1.0, f"가입 여섯 개가 {elapsed:.2f}초 동안 줄을 섰습니다"
    assert lag < 0.15, f"봇의 루프가 {lag * 1000:.0f}ms 멈췄습니다"


async def test_the_store_lock_is_not_a_second_door_into_the_loop(accounts, monkeypatch):
    """해시를 스레드로 보내는 것만으로는 부족합니다 — 저장소의 자물쇠가 남습니다.

    `Accounts.register` 는 해시를 자기 락 **안에서** 계산합니다. 그러면 루프
    위에 남은 `create_session` 한 줄이 그 락을 기다리며 루프를 그대로 세웁니다.
    실제로 처음 고친 뒤에도 루프가 6.5초 멈춘 것이 이 경로였습니다.
    """
    real = accounts.register

    def slow(*args, **kwargs):
        with accounts._lock:          # register 가 해시를 도는 동안의 그 락
            time.sleep(0.25)
        return real(*args, **kwargs)

    monkeypatch.setattr(accounts, "register", slow)
    app = build_app(accounts, per_register=100, per_address=1_000, per_global=10_000)

    answers, _elapsed, lag = await heartbeat_lag(app, [
        (lambda c, i=i: c.post("/api/auth/register",
                               json={"email": f"lock{i}@example.com", "password": GOOD}))
        for i in range(6)])

    assert [r.status_code for r in answers] == [201] * 6
    assert lag < 0.15, f"봇의 루프가 {lag * 1000:.0f}ms 멈췄습니다"


async def test_a_login_flood_does_not_stall_the_loop_either(accounts, monkeypatch):
    """없는 이메일도 해시를 씁니다(타이밍을 맞추려고) — 그래서 같은 무기입니다."""
    slowly(monkeypatch, accounts, "authenticate")
    app = build_app(accounts, per_email=100, per_address=1_000, per_global=10_000)

    answers, elapsed, lag = await heartbeat_lag(app, [
        (lambda c, i=i: c.post("/api/auth/login",
                               json={"email": f"ghost{i}@example.com", "password": WRONG}))
        for i in range(6)])

    assert [r.status_code for r in answers] == [401] * 6
    assert elapsed < 1.0
    assert lag < 0.15, f"봇의 루프가 {lag * 1000:.0f}ms 멈췄습니다"


async def test_changing_a_password_does_not_stall_the_loop(accounts, monkeypatch):
    """로그인한 사람의 요청도 마찬가지입니다 — 해시 두 번이라 오히려 비쌉니다."""
    app = build_app(accounts, per_register=100, per_address=1_000, per_global=10_000)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://desk") as client:
        r = await client.post("/api/auth/register",
                              json={"email": "a@example.com", "password": GOOD})
        cookie = r.cookies[_PLAIN_COOKIE]

    slowly(monkeypatch, accounts, "change_password")
    answers, _elapsed, lag = await heartbeat_lag(app, [
        (lambda c: c.post("/api/auth/password",
                          json={"current": GOOD, "new": "another1-secret"},
                          headers={"Cookie": f"{_PLAIN_COOKIE}={cookie}"}))])

    assert [r.status_code for r in answers] == [200]
    assert lag < 0.15, f"봇의 루프가 {lag * 1000:.0f}ms 멈췄습니다"


# ── 가입에도 한도 ────────────────────────────────────────────────────────
def test_registration_is_not_an_unmetered_write(accounts):
    """가입은 이 서비스에서 가장 비싼 무인증 요청입니다. 세지 않으면 안 됩니다."""
    client = TestClient(build_app(accounts))
    codes = [register(client, f"z{i}@example.com").status_code for i in range(12)]

    assert 429 in codes, f"연속 가입 12번이 모두 통과했습니다: {codes}"
    r = register(client, "z99@example.com")
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0
    assert "초 후" in r.json()["detail"]


def test_a_throttled_register_never_reaches_the_hash(accounts, monkeypatch):
    """거절이 우리에게도 비싸면 거절하는 의미가 없습니다."""
    client = TestClient(build_app(accounts, per_register=2, per_address=50,
                                  per_global=5_000))
    assert register(client, "a@example.com").status_code == 201
    assert register(client, "b@example.com").status_code == 201

    called = []
    monkeypatch.setattr(accounts, "register", lambda *a, **k: called.append(1))
    assert register(client, "c@example.com").status_code == 429
    assert called == [], "429 를 주면서 PBKDF2 를 돌렸습니다"


def test_one_person_signing_up_is_not_caught_by_it(accounts):
    """오타 두어 번 뒤 가입하는 사람은 막히지 않아야 합니다."""
    client = TestClient(build_app(accounts))
    assert register(client, "not-an-email").status_code == 400
    assert register(client, "a@example.com", password="short1").status_code == 400
    assert register(client, "a@example.com").status_code == 201


# ── 주소는 프록시가 말해줄 때만 ──────────────────────────────────────────
def test_rotating_x_forwarded_for_does_not_mint_fresh_buckets(accounts):
    """아무도 프록시로 지정하지 않았으면 그 헤더는 그냥 호출자가 친 글자입니다."""
    client = TestClient(build_app(accounts, per_email=5, per_address=6,
                                  per_register=100))
    codes = [login(client, f"ghost{i}@example.com", WRONG,
                   headers={"X-Forwarded-For": f"203.0.113.{i}"}).status_code
             for i in range(12)]

    assert 429 in codes, f"헤더를 돌려가며 주소 한도를 걸어나갔습니다: {codes}"


def test_the_header_is_honoured_from_a_peer_we_were_told_to_trust(accounts, monkeypatch):
    """배포에서는 진짜 프록시가 앞에 섭니다 — 그때는 헤더가 진짜 주소입니다."""
    monkeypatch.setenv("QUANT_TRUSTED_PROXIES", "10.0.0.7, testclient")
    client = TestClient(build_app(accounts, per_email=5, per_address=6,
                                  per_register=100))
    codes = [login(client, f"ghost{i}@example.com", WRONG,
                   headers={"X-Forwarded-For": f"203.0.113.{i}"}).status_code
             for i in range(12)]

    # 주소가 정말 다르므로 각자 자기 예산을 씁니다.
    assert codes == [401] * 12
    # 그리고 한 주소를 계속 쓰면 그 주소는 잠깁니다.
    same = [login(client, f"ghost9{i}@example.com", WRONG,
                  headers={"X-Forwarded-For": "203.0.113.200"}).status_code
            for i in range(8)]
    assert 429 in same


def test_a_global_ceiling_holds_even_when_the_bucketing_is_wrong():
    """버킷을 잘못 나눠도 "한도가 아예 없음" 이 되지는 않게."""
    clock = Clock()
    limiter = LoginRateLimiter(per_email=0, per_address=0, per_global=10,
                               window_s=60.0, clock=clock)
    for i in range(10):
        limiter.fail(f"u{i}@example.com", f"10.0.0.{i}")

    assert limiter.retry_after("fresh@example.com", "192.0.2.1") > 0
    assert limiter.register_retry_after("192.0.2.1") > 0
    # 그리고 영원히는 아닙니다 — 창이 지나면 다시 열립니다.
    clock.t += 61.0
    assert limiter.retry_after("fresh@example.com", "192.0.2.1") == 0.0


def test_registering_spends_the_same_address_budget_as_logging_in(accounts, monkeypatch):
    """엔드포인트를 바꾼다고 예산이 새로 생기지는 않습니다."""
    monkeypatch.setenv("QUANT_TRUSTED_PROXIES", "testclient")
    client = TestClient(build_app(accounts, per_email=50, per_address=4,
                                  per_register=50))
    here = {"X-Forwarded-For": "198.51.100.4"}
    for i in range(4):
        assert register(client, f"z{i}@example.com", headers=here).status_code == 201

    assert login(client, "z0@example.com", WRONG, headers=here).status_code == 429


# ── 모르는 사람이 주인을 잠글 수 없다 ────────────────────────────────────
def test_a_stranger_cannot_lock_the_owner_out_of_her_own_account(accounts, monkeypatch):
    """이메일만으로 잠그면, 남이 아무 데서나 다섯 번 틀려주는 것이 곧 퇴거입니다."""
    monkeypatch.setenv("QUANT_TRUSTED_PROXIES", "testclient")
    client = TestClient(build_app(accounts))
    assert register(client, "alice@example.com").status_code == 201
    client.cookies.clear()

    attacker = {"X-Forwarded-For": "198.51.100.9"}
    codes = [login(client, "alice@example.com", WRONG, headers=attacker).status_code
             for _ in range(8)]
    # 공격자 자신은 잠깁니다.
    assert codes[-1] == 429

    owner = {"X-Forwarded-For": "203.0.113.77"}
    r = login(client, "alice@example.com", GOOD, headers=owner)
    assert r.status_code == 200, r.text
    assert r.json()["email"] == "alice@example.com"


def test_the_attackers_own_address_stays_locked_even_with_the_right_password(
        accounts, monkeypatch):
    """대입을 멈추는 것이 목적이므로, 잠긴 주소에서는 맞는 값도 통과하지 않습니다."""
    monkeypatch.setenv("QUANT_TRUSTED_PROXIES", "testclient")
    client = TestClient(build_app(accounts))
    register(client, "alice@example.com")
    client.cookies.clear()

    seat = {"X-Forwarded-For": "198.51.100.9"}
    for _ in range(5):
        assert login(client, "alice@example.com", WRONG, headers=seat).status_code == 401
    assert login(client, "alice@example.com", GOOD, headers=seat).status_code == 429


def test_a_distributed_run_on_one_email_is_slowed_but_never_locked():
    """여러 주소에서 한 이메일을 두드리면 느려집니다 — 잠기지는 않습니다."""
    clock = Clock()
    limiter = LoginRateLimiter(per_email=5, per_address=100, window_s=60.0,
                               delay_s=2.0, clock=clock)
    for i in range(30):
        limiter.fail("alice@example.com", f"203.0.113.{i}")

    # 주인은 자기 주소에서 아무 지장 없이 들어옵니다.
    assert limiter.retry_after("alice@example.com", "198.51.100.1") == 0.0
    # 대신 **틀린** 시도의 답이 늦어집니다. 상한이 있어 연결을 붙잡지는 않습니다.
    assert 0.0 < limiter.delay_for("alice@example.com") <= 2.0
    # 그리고 성공 한 번이 그 계정의 지연을 지웁니다.
    limiter.succeed("alice@example.com", "198.51.100.1")
    assert limiter.delay_for("alice@example.com") == 0.0


# ── 가입 응답이 명부가 되지 않게 ─────────────────────────────────────────
def test_walking_a_list_through_register_is_throttled_and_recorded(accounts):
    """가입 응답은 여전히 "이미 가입" 을 말합니다. 그래서 그 창구를 좁힙니다.

    막는 것: 대량 조회. 시도마다 주소 예산을 쓰고 감사 로그에 남으므로, 후보
    목록을 훑으려면 주소가 몇 개씩 필요하고 훑은 흔적이 남습니다.
    막지 못하는 것: 이미 지목한 한 사람의 가입 여부. 가입 즉시 세션을 주는
    폼은 "빈 이메일" 과 "찬 이메일" 에 같은 답을 줄 수 없습니다 — 세션 자체가
    답이기 때문입니다. 그건 메일 확인으로만 닫히고, 이 프로젝트에는 메일을
    보내는 것이 없습니다.
    """
    client = TestClient(build_app(accounts))
    assert register(client, "alice@example.com").status_code == 201
    client.cookies.clear()

    codes = [register(client, "alice@example.com").status_code for _ in range(12)]
    assert codes[0] == 400
    assert 429 in codes, f"명부 조회가 끝까지 답을 받았습니다: {codes}"

    rows = accounts.conn.execute(
        "SELECT detail FROM audit WHERE action='register_taken_email'").fetchall()
    assert [r["detail"] for r in rows] == ["alice@example.com"] * codes.count(400)


# ── 세션 쿠키는 한 장 ────────────────────────────────────────────────────
def test_the_session_cookie_carries_the_host_prefix(accounts):
    """`__Host-` 는 옆 서브도메인이 같은 이름을 쓰지 못하게 하는 유일한 수단입니다."""
    assert SESSION_COOKIE.startswith("__Host-")
    client = TestClient(build_app(accounts), base_url="https://desk.example")
    header = register(client, "a@example.com").headers["set-cookie"]

    assert header.startswith(f"{SESSION_COOKIE}=")
    lowered = header.lower()
    assert "path=/" in lowered          # __Host- 가 요구하는 값
    assert "domain=" not in lowered     # 있으면 브라우저가 통째로 버립니다
    assert "; secure" in lowered        # 역시 __Host- 의 조건


def test_two_session_cookies_are_not_a_session(accounts):
    """정상 브라우저는 한 장만 보냅니다. 두 장은 누군가 끼워넣은 것입니다."""
    app = build_app(accounts)
    bob_client, carol_client = TestClient(app), TestClient(app)
    register(bob_client, "bob@example.com")
    register(carol_client, "carol@example.com")
    bob = bob_client.cookies[_PLAIN_COOKIE]
    carol = carol_client.cookies[_PLAIN_COOKIE]

    probe = TestClient(app)
    for first, second in ((bob, carol), (carol, bob), ("GARBAGE", bob), (bob, "GARBAGE")):
        r = probe.get("/api/mine", headers={
            "Cookie": f"{_PLAIN_COOKIE}={first}; {_PLAIN_COOKIE}={second}"})
        assert r.status_code == 401, f"{first[:6]}/{second[:6]} 가 세션이 됐습니다"

    # 한 장이면 그대로 동작합니다 — 세션을 못 쓰게 만든 게 아닙니다.
    r = probe.get("/api/mine", headers={"Cookie": f"{_PLAIN_COOKIE}={bob}"})
    assert r.json() == {"email": "bob@example.com"}


def test_logging_out_with_two_cookies_revokes_both(accounts):
    """어느 쪽이 내 것인지 모르는 상황이라면, 둘 다 끊는 것이 안전한 쪽입니다."""
    app = build_app(accounts)
    bob_client, carol_client = TestClient(app), TestClient(app)
    register(bob_client, "bob@example.com")
    register(carol_client, "carol@example.com")
    bob = bob_client.cookies[_PLAIN_COOKIE]
    carol = carol_client.cookies[_PLAIN_COOKIE]

    probe = TestClient(app)
    r = probe.post("/api/auth/logout", headers={
        "Cookie": f"{_PLAIN_COOKIE}={bob}; {_PLAIN_COOKIE}={carol}"})
    assert r.status_code == 200
    assert accounts.user_for_session(bob) is None
    assert accounts.user_for_session(carol) is None
