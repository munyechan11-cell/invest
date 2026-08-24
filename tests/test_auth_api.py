"""로그인 표면이 서비스의 정문입니다 — 여기가 뚫리면 나머지는 볼 것도 없습니다.

이 파일이 고정하는 것.

* **쿠키 속성.** HttpOnly, Path=/, https 면 Secure, 그리고 SameSite=Lax.
  마지막 것이 없으면 사용자가 열어둔 아무 페이지나 로그인된 세션으로 이쪽에
  주문을 넣을 수 있습니다. 이 대시보드에는 전량청산 버튼이 있습니다.
* **없는 이메일과 틀린 비밀번호가 구별되지 않는다.** 구별되면 로그인 폼이
  가입자 명부가 되고, 그 명부가 자격증명 대입의 절반입니다.
* **시도 제한.** 증권사 키를 들고 있는 서비스의 로그인 폼은 스터핑의 표적입니다.
* **비밀번호 변경은 다른 기기를 모두 끊는다.** 안 끊으면 바꾼 의미가 없습니다.
"""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from quant.webapp import accounts as accounts_mod
from quant.webapp.accounts import Accounts, User
from quant.webapp.auth_api import (
    BAD_LOGIN,
    SESSION_COOKIE,
    LoginRateLimiter,
    build_auth,
    build_auth_router,
    public_user,
)

SECRET = "k" * 48
GOOD = "hunter2-secret"       # 10자 이상 + 숫자와 문자
OTHER = "another1-secret"


@pytest.fixture(autouse=True)
def _fast_hashing(monkeypatch):
    """PBKDF2 60만 회는 로그인 한 번의 값입니다. 이 파일은 로그인을 수십 번
    합니다 — 반복 횟수 자체는 accounts 의 관심사이므로 여기서는 낮춥니다."""
    monkeypatch.setattr(accounts_mod, "_PBKDF2_ROUNDS", 1_000)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("QUANT_COOKIE_SECURE", raising=False)


@pytest.fixture
def accounts(tmp_path) -> Accounts:
    store = Accounts(tmp_path / "accounts.db", secret=SECRET)
    yield store
    store.close()


@pytest.fixture
def auth(accounts):
    return build_auth(accounts)


@pytest.fixture
def app(auth) -> FastAPI:
    """인증 라우터 + 다른 라우터가 의존성을 쓰는 모습 그대로."""
    app = FastAPI()
    app.include_router(auth.router)

    @app.get("/api/mine")
    async def mine(user: User = Depends(auth.current_user)):
        return {"email": user.email}

    @app.get("/api/admin")
    async def admin(user: User = Depends(auth.require_admin)):
        return {"email": user.email}

    @app.get("/api/maybe")
    async def maybe(user: User | None = Depends(auth.optional_user)):
        return {"signed_in": user is not None}

    return app


@pytest.fixture
def client(app) -> TestClient:
    """배포와 같은 https 로 부릅니다.

    `__Host-` 접두사는 `Secure` 없이는 브라우저가 **저장 자체를 거부**하므로,
    평문 http 로 검사하면 배포에서 실제로 나가는 쿠키가 아니라 개발용 대체
    이름을 검사하게 됩니다.
    """
    return TestClient(app, base_url="https://desk.example")


@pytest.fixture
def plain_client(app) -> TestClient:
    """평문 http — 로컬 개발에서 쓰이는 경로."""
    return TestClient(app, base_url="http://localhost")


def signup(client, email="a@example.com", password=GOOD, name="첫 사용자"):
    return client.post("/api/auth/register", json={
        "email": email, "password": password, "display_name": name})


def login(client, email="a@example.com", password=GOOD, **kw):
    return client.post("/api/auth/login", json={"email": email, "password": password}, **kw)


# ── 가입 ─────────────────────────────────────────────────────────────────
def test_register_returns_the_user_and_signs_them_in(client):
    r = signup(client)
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == "a@example.com"
    assert body["display_name"] == "첫 사용자"
    # 아무도 없는 서비스의 첫 사용자는 관리자입니다.
    assert body["is_admin"] is True
    # 이어지는 요청이 바로 인증됩니다 — 가입 후 로그인을 또 시키지 않습니다.
    assert client.get("/api/auth/me").json()["email"] == "a@example.com"


def test_the_second_user_is_not_an_admin(client, accounts):
    signup(client)
    r = signup(client, email="b@example.com", name="둘째")
    assert r.status_code == 201
    assert r.json()["is_admin"] is False


def test_register_never_echoes_the_password(client):
    body = signup(client).text
    assert GOOD not in body
    assert "password" not in body


@pytest.mark.parametrize("email,password,fragment", [
    ("not-an-email", GOOD, "이메일"),
    ("c@example.com", "short1", "10자"),
    ("c@example.com", "1234567890", "숫자와 문자"),
])
def test_register_says_why_it_refused(client, email, password, fragment):
    r = client.post("/api/auth/register",
                    json={"email": email, "password": password, "display_name": ""})
    assert r.status_code == 400
    assert fragment in r.json()["detail"]


def test_a_taken_email_is_refused(client):
    signup(client)
    r = signup(client)
    assert r.status_code == 400
    assert "이미 가입" in r.json()["detail"]


def test_an_enormous_password_is_rejected_before_it_is_hashed(client):
    """길이 상한이 없으면 폼 한 번이 곧 CPU 무료 이용권입니다."""
    r = client.post("/api/auth/register", json={
        "email": "big@example.com", "password": "a1" * 500_000, "display_name": ""})
    assert r.status_code == 422


# ── 쿠키 ─────────────────────────────────────────────────────────────────
def cookie_header(response) -> str:
    """속성 비교용으로 소문자화한 헤더. 이름 비교에는 쓰지 마세요."""
    return response.headers["set-cookie"].lower()


def raw_cookie_header(response) -> str:
    """대소문자 그대로. `__Host-` 는 접두사가 정확히 일치할 때만 효력이 있습니다."""
    return response.headers["set-cookie"]


def test_over_plain_http_the_name_drops_the_prefix_so_login_works(plain_client):
    """`__Host-` 를 http 에서 고집하면 로그인이 성공했다고 답한 뒤 아무 일도
    일어나지 않습니다 — 브라우저가 쿠키를 버리고, 이유는 어디에도 안 보입니다.

    http 에서는 접두사가 줄 보호가 애초에 없으므로 이름을 낮춥니다. 보호를
    포기하는 것이 아니라, 없는 보호를 이유로 서비스를 못 쓰게 만들지 않는 것입니다.
    """
    header = raw_cookie_header(signup(plain_client))
    assert header.startswith("quant_session=")
    assert "secure" not in header.lower()
    # 그래도 세션은 실제로 동작해야 합니다.
    assert plain_client.get("/api/auth/me").status_code == 200


def test_over_https_the_prefix_is_kept(client):
    header = raw_cookie_header(signup(client))
    assert header.startswith(f"{SESSION_COOKIE}=")
    assert "secure" in header.lower()


def test_the_session_cookie_is_httponly_lax_and_rooted(client):
    sent = signup(client)
    # 이름은 대소문자를 지켜서 봅니다 — 브라우저는 `__host-` 를 특별 취급하지
    # 않으므로, 소문자로 나가면 접두사가 주는 보호가 통째로 사라집니다.
    assert raw_cookie_header(sent).startswith(f"{SESSION_COOKIE}=")
    header = cookie_header(sent)
    assert "httponly" in header          # 스크립트가 세션을 읽지 못하게
    assert "samesite=lax" in header      # 남의 사이트에서 온 POST 에 붙지 않게
    assert "path=/" in header            # 대시보드 전체에서 같은 세션


def test_samesite_is_present_at_all(client):
    """이 한 줄이 없으면 evil.example 의 폼이 사용자의 세션으로 주문을 넣습니다."""
    assert "samesite=" in cookie_header(signup(client))


def test_plain_http_does_not_get_a_secure_cookie(plain_client):
    # Secure 를 붙이면 http 로는 쿠키가 아예 오가지 않습니다. 로컬 개발이
    # 조용히 로그인 불가가 되면 안 됩니다.
    assert "; secure" not in cookie_header(signup(plain_client))


def test_https_gets_a_secure_cookie(app):
    secure_client = TestClient(app, base_url="https://desk.example")
    assert "; secure" in cookie_header(signup(secure_client))


def test_a_proxy_reporting_https_gets_a_secure_cookie(client):
    """배포에서는 TLS 를 프록시가 끊습니다 — 앱에는 http 로 도착합니다."""
    r = client.post("/api/auth/register",
                    json={"email": "p@example.com", "password": GOOD, "display_name": ""},
                    headers={"x-forwarded-proto": "https"})
    assert "; secure" in cookie_header(r)


def test_a_forged_header_cannot_take_secure_away(app):
    """헤더는 Secure 를 붙일 수만 있고 뗄 수는 없어야 합니다."""
    secure_client = TestClient(app, base_url="https://desk.example")
    r = secure_client.post("/api/auth/register",
                           json={"email": "q@example.com", "password": GOOD,
                                 "display_name": ""},
                           headers={"x-forwarded-proto": "http"})
    assert "; secure" in cookie_header(r)


def test_the_cookie_value_is_an_opaque_token(client, accounts):
    signup(client)
    value = client.cookies[SESSION_COOKIE]
    assert len(value) >= 32
    # 사용자 id 나 이메일을 그대로 담으면 남의 세션을 손으로 적어낼 수 있습니다.
    assert "a@example.com" not in value
    assert value not in ("1", "user-1")
    # 서버는 토큰을 해시해서 보관합니다 — DB 사본이 곧 로그인이면 안 됩니다.
    stored = accounts.conn.execute("SELECT token_hash FROM sessions").fetchone()
    assert stored["token_hash"] != value


# ── 로그인 ───────────────────────────────────────────────────────────────
def test_login_issues_a_working_session(client):
    signup(client)
    client.cookies.clear()
    assert client.get("/api/auth/me").status_code == 401

    r = login(client)
    assert r.status_code == 200
    assert r.json()["email"] == "a@example.com"
    assert client.get("/api/auth/me").status_code == 200


def test_login_is_case_insensitive_about_the_email(client):
    signup(client)
    client.cookies.clear()
    assert login(client, email="A@Example.com").status_code == 200


def test_a_wrong_password_and_an_unknown_email_look_identical(client):
    signup(client)
    client.cookies.clear()

    wrong = login(client, password="totally-wrong9")
    unknown = login(client, email="nobody@example.com")

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert wrong.json()["detail"] == BAD_LOGIN
    # 이메일이 응답에 되비쳐 나오면 그것만으로도 구별이 됩니다.
    assert "nobody@example.com" not in unknown.text


def test_the_audit_log_knows_which_of_the_two_it_was(client, accounts):
    signup(client)
    client.cookies.clear()
    login(client, password="totally-wrong9")
    login(client, email="nobody@example.com")

    rows = accounts.conn.execute(
        "SELECT user_id, action, detail FROM audit ORDER BY id").fetchall()
    actions = [r["action"] for r in rows]
    assert "login_failed" in actions              # 있는 계정, 틀린 비밀번호
    assert "login_failed_unknown_email" in actions  # 없는 계정
    # 감사 로그에도 비밀번호는 남지 않습니다.
    assert not any("wrong9" in (r["detail"] or "") for r in rows)


def test_a_failed_login_leaves_no_session(client, accounts):
    signup(client)
    client.cookies.clear()
    r = login(client, password="totally-wrong9")
    assert SESSION_COOKIE not in r.cookies
    assert client.get("/api/auth/me").status_code == 401


# ── 로그아웃 ─────────────────────────────────────────────────────────────
def test_logout_kills_the_session_on_the_server_too(client, accounts):
    signup(client)
    token = client.cookies[SESSION_COOKIE]

    assert client.post("/api/auth/logout").json() == {"ok": True}
    assert client.get("/api/auth/me").status_code == 401
    # 쿠키를 지우는 것만으로는 부족합니다 — 토큰 사본을 가진 쪽이 남습니다.
    assert accounts.user_for_session(token) is None


def test_logout_without_a_session_is_still_fine(client):
    assert client.post("/api/auth/logout").status_code == 200


def test_logging_out_one_browser_leaves_the_other_alone(app, accounts):
    """세션 하나를 끊는 것이지 계정을 끊는 것이 아닙니다."""
    laptop, phone = TestClient(app), TestClient(app)
    signup(laptop)
    login(phone)

    laptop.post("/api/auth/logout")
    assert laptop.get("/api/auth/me").status_code == 401
    assert phone.get("/api/auth/me").status_code == 200


# ── 비밀번호 변경 ────────────────────────────────────────────────────────
def test_changing_a_password_needs_the_current_one(client):
    signup(client)
    r = client.post("/api/auth/password", json={"current": "nope12345", "new": OTHER})
    assert r.status_code == 400
    assert "현재 비밀번호" in r.json()["detail"]


def test_a_weak_new_password_is_refused_and_the_old_one_survives(client, app):
    signup(client)
    r = client.post("/api/auth/password", json={"current": GOOD, "new": "123456"})
    assert r.status_code == 400

    fresh = TestClient(app)
    assert login(fresh).status_code == 200


def test_changing_a_password_logs_out_the_other_devices_but_not_this_one(app):
    laptop, phone = TestClient(app), TestClient(app)
    signup(laptop)
    login(phone)

    r = laptop.post("/api/auth/password", json={"current": GOOD, "new": OTHER})
    assert r.status_code == 200
    # 바꾼 사람은 계속 씁니다.
    assert laptop.get("/api/auth/me").status_code == 200
    # 누가 봤을지 몰라서 바꾸는 것입니다 — 다른 기기는 끊깁니다.
    assert phone.get("/api/auth/me").status_code == 401

    after = TestClient(app)
    assert login(after, password=GOOD).status_code == 401
    assert login(after, password=OTHER).status_code == 200


def test_the_alternate_field_names_work_too(client):
    """화면이 current_password / new_password 로 보내도 받습니다."""
    signup(client)
    r = client.post("/api/auth/password",
                    json={"current_password": GOOD, "new_password": OTHER})
    assert r.status_code == 200


def test_changing_a_password_requires_being_signed_in(client):
    signup(client)
    client.cookies.clear()
    r = client.post("/api/auth/password", json={"current": GOOD, "new": OTHER})
    assert r.status_code == 401


# ── 의존성 ───────────────────────────────────────────────────────────────
def test_a_protected_route_401s_without_a_session(client):
    assert client.get("/api/mine").status_code == 401
    assert "로그인" in client.get("/api/mine").json()["detail"]


def test_a_protected_route_sees_the_signed_in_user(client):
    signup(client)
    assert client.get("/api/mine").json() == {"email": "a@example.com"}


def test_a_forged_cookie_is_not_a_session(client):
    signup(client)
    client.cookies.clear()
    # 토큰은 서버가 발급한 것만 세션입니다. 형태만 맞는 문자열은 아닙니다.
    client.cookies.set(SESSION_COOKIE, "x" * 43)
    assert client.get("/api/mine").status_code == 401


def test_an_expired_session_is_refused(client, accounts):
    signup(client)
    accounts.conn.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'")
    accounts.conn.commit()
    assert client.get("/api/mine").status_code == 401


def test_admin_only_routes(app):
    boss, member = TestClient(app), TestClient(app)
    signup(boss)                                     # 첫 사용자 = 관리자
    signup(member, email="b@example.com", name="둘째")

    assert TestClient(app).get("/api/admin").status_code == 401   # 익명
    assert member.get("/api/admin").status_code == 403            # 로그인했지만 일반
    assert boss.get("/api/admin").status_code == 200


def test_optional_user_does_not_demand_a_session(client):
    assert client.get("/api/maybe").json() == {"signed_in": False}
    signup(client)
    assert client.get("/api/maybe").json() == {"signed_in": True}


def test_one_users_session_never_resolves_to_another(app, accounts):
    a, b = TestClient(app), TestClient(app)
    signup(a)
    signup(b, email="b@example.com", name="둘째")
    assert a.get("/api/mine").json()["email"] == "a@example.com"
    assert b.get("/api/mine").json()["email"] == "b@example.com"


# ── 시도 제한 ────────────────────────────────────────────────────────────
def test_repeated_failures_on_one_email_get_locked_out(client):
    signup(client)
    client.cookies.clear()
    for _ in range(5):
        assert login(client, password="totally-wrong9").status_code == 401

    r = login(client, password="totally-wrong9")
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0
    assert "초 후" in r.json()["detail"]
    # 잠긴 동안에는 맞는 비밀번호도 통과하지 않습니다 — 그래야 대입이 멈춥니다.
    assert login(client).status_code == 429


def test_changing_the_case_of_the_email_does_not_reset_the_counter(client):
    signup(client)
    client.cookies.clear()
    for _ in range(5):
        login(client, password="totally-wrong9")
    assert login(client, email="A@EXAMPLE.COM", password="totally-wrong9").status_code == 429


def test_locking_one_email_does_not_lock_another(client):
    signup(client)
    signup(client, email="b@example.com", name="둘째")
    client.cookies.clear()
    for _ in range(5):
        login(client, password="totally-wrong9")

    assert login(client).status_code == 429
    # 옆 사람 계정은 멀쩡해야 합니다.
    assert login(client, email="b@example.com").status_code == 200


def test_one_address_walking_many_emails_gets_locked_out(app, accounts):
    """스터핑은 한 계정을 두드리지 않고 명부를 훑습니다 — 주소별 한도가 잡습니다."""
    stuffer = TestClient(app)
    for i in range(20):
        assert stuffer.post("/api/auth/login", json={
            "email": f"victim{i}@example.com", "password": "guess12345"}).status_code == 401
    r = stuffer.post("/api/auth/login",
                     json={"email": "victim99@example.com", "password": "guess12345"})
    assert r.status_code == 429


def test_a_successful_login_forgives_the_email(client):
    """오타 몇 번 뒤 제대로 친 사람은 계속 쓸 수 있어야 합니다."""
    signup(client)
    client.cookies.clear()
    for _ in range(4):
        login(client, password="totally-wrong9")
    assert login(client).status_code == 200

    client.cookies.clear()
    for _ in range(4):
        assert login(client, password="totally-wrong9").status_code == 401


def test_the_limiter_is_per_router_not_global(accounts, app):
    """한 인스턴스에서 잠갔다고 다른 인스턴스가 잠기면 테스트도 배포도 꼬입니다."""
    client = TestClient(app)
    signup(client)
    client.cookies.clear()
    for _ in range(6):
        login(client, password="totally-wrong9")

    other = TestClient(_mount(build_auth(accounts)))
    assert login(other).status_code == 200


def _mount(auth) -> FastAPI:
    app = FastAPI()
    app.include_router(auth.router)
    return app


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_the_window_slides():
    """영원히 잠그지 않습니다 — 창이 지나면 다시 열립니다."""
    clock = Clock()
    limiter = LoginRateLimiter(per_email=3, per_address=100, window_s=60.0, clock=clock)
    for _ in range(3):
        limiter.fail("a@example.com", "1.2.3.4")

    assert limiter.retry_after("a@example.com", "1.2.3.4") == pytest.approx(60.0)
    clock.t += 59.0
    assert limiter.retry_after("a@example.com", "1.2.3.4") == pytest.approx(1.0)
    clock.t += 2.0
    assert limiter.retry_after("a@example.com", "1.2.3.4") == 0.0


def test_the_limiter_does_not_grow_without_bound():
    """무작위 이메일 수만 개가 곧 메모리 무료 이용권이면 안 됩니다."""
    clock = Clock()
    limiter = LoginRateLimiter(window_s=60.0, clock=clock)
    for i in range(5_000):
        limiter.fail(f"u{i}@example.com", f"10.0.0.{i % 256}")
    clock.t += 61.0
    limiter.fail("late@example.com", "10.0.0.1")
    assert len(limiter._fails) < 100


def test_an_unknown_address_still_counts_by_email():
    """client 가 없는 ASGI 호출에서도 이메일 버킷은 살아 있어야 합니다."""
    limiter = LoginRateLimiter(per_email=2, per_address=5)
    limiter.fail("a@example.com", "")
    limiter.fail("a@example.com", "")
    assert limiter.retry_after("a@example.com", "") > 0


# ── 내보내는 이름 ────────────────────────────────────────────────────────
def test_public_user_carries_nothing_secret(accounts):
    user = accounts.register("x@example.com", GOOD, "이름")
    accounts.put_secret(user.id, "KIS_APP_KEY", "super-secret-key-value")
    body = public_user(user)
    assert set(body) == {"id", "email", "display_name", "is_admin", "created_at"}
    assert "super-secret-key-value" not in str(body)
    assert "password" not in str(body).lower()


def test_build_auth_router_mounts_on_its_own(accounts):
    app = FastAPI()
    app.include_router(build_auth_router(accounts))
    client = TestClient(app)
    assert signup(client).status_code == 201
