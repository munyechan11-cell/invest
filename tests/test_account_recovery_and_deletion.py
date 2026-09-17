"""비밀번호 찾기와 계정 삭제.

**이 서비스에는 메일 발송기가 없습니다.** 그래서 흔한 "재설정 링크를 메일로"
는 만들 수 없고, 있는 척하면 비밀번호를 잊은 사람이 오지 않을 메일을
기다립니다. 대신 가입할 때 한 번 보여 주고 다시는 못 보는 **복구 코드** 를
줍니다 — 증권사 키와 같은 규칙입니다(`put_secret`).

계정 삭제는 되돌릴 수 없고, **돌고 있는 봇 위에서는 안 됩니다.** 실거래 봇이
도는 채로 계정이 사라지면 주문을 낸 주인이 없는 포지션이 증권사에 남고, 그
포지션의 손절은 이 프로세스 안에만 있었습니다.
"""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from quant.webapp import accounts as accounts_mod
from quant.webapp.accounts import AccountError, Accounts, User
from quant.webapp.auth_api import build_auth

SECRET = "k" * 48
GOOD = "hunter2-secret"
NEXT = "another1-secret"


@pytest.fixture(autouse=True)
def _fast_hashing(monkeypatch):
    monkeypatch.setattr(accounts_mod, "_PBKDF2_ROUNDS", 1_000)


@pytest.fixture
def accounts(tmp_path) -> Accounts:
    store = Accounts(tmp_path / "accounts.db", secret=SECRET)
    yield store
    store.close()


@pytest.fixture
def blocked() -> list[str]:
    """탈퇴 가드가 돌려줄 사유. 비우면 막지 않습니다."""
    return [""]


@pytest.fixture
def client(accounts, blocked) -> TestClient:
    auth = build_auth(accounts, deletion_guard=lambda user: blocked[0])
    app = FastAPI()
    app.include_router(auth.router)

    @app.get("/api/mine")
    async def mine(user: User = Depends(auth.current_user)):
        return {"email": user.email}

    return TestClient(app, base_url="https://desk.example")


def signup(client, email="a@example.com", password=GOOD):
    return client.post("/api/auth/register", json={
        "email": email, "password": password, "display_name": "나"})


# ── 가입할 때 코드를 준다 ────────────────────────────────────────────────
def test_register_returns_a_code_once(client):
    body = signup(client).json()
    assert body["recovery_code"], "가입 응답에 복구 코드가 없습니다"
    assert body["email"] == "a@example.com"


def test_the_code_is_never_retrievable_again(client, accounts):
    code = signup(client).json()["recovery_code"]
    # 저장된 쪽은 해시만 들고 있습니다 — DB 사본 하나가 모든 계정의 열쇠가
    # 되지 않아야 합니다.
    row = accounts.conn.execute("SELECT recovery_hash FROM users").fetchone()
    assert row["recovery_hash"] and code not in row["recovery_hash"]


# ── 그 코드로 비밀번호를 되찾는다 ────────────────────────────────────────
def reset(client, email, code, new=NEXT):
    return client.post("/api/auth/reset-password", json={
        "email": email, "recovery_code": code, "new_password": new})


def test_the_code_sets_a_new_password(client):
    code = signup(client).json()["recovery_code"]
    client.post("/api/auth/logout")
    assert reset(client, "a@example.com", code).status_code == 200
    assert client.post("/api/auth/login", json={
        "email": "a@example.com", "password": NEXT}).status_code == 200


def test_resetting_does_not_hand_out_a_session(client):
    """코드를 주운 사람이 곧바로 안에 들어와 있는 것보다, 한 걸음 더 걷는
    쪽이 낫습니다."""
    code = signup(client).json()["recovery_code"]
    client.post("/api/auth/logout")
    reset(client, "a@example.com", code)
    assert client.get("/api/mine").status_code == 401


def test_every_other_device_is_logged_out(client, accounts):
    code = signup(client).json()["recovery_code"]
    assert client.get("/api/mine").status_code == 200      # 이 기기도 세션이 있음
    reset(client, "a@example.com", code)
    assert client.get("/api/mine").status_code == 401


def test_a_used_code_does_not_work_twice(client):
    """한 번 쓴 코드가 계속 통하면 그건 비밀번호가 하나 더 있는 것과 같고,
    그쪽은 아무도 안 바꿉니다."""
    code = signup(client).json()["recovery_code"]
    assert reset(client, "a@example.com", code).status_code == 200
    assert reset(client, "a@example.com", code, "third111-secret").status_code == 400


@pytest.mark.parametrize("email,code", [
    ("a@example.com", "AAAAAA-BBBBBB-CCCCCC-DDDDDD"),   # 맞는 이메일, 틀린 코드
    ("nobody@example.com", "real"),                      # 없는 이메일
])
def test_a_wrong_attempt_does_not_say_which_half_was_wrong(client, email, code):
    """없는 이메일과 틀린 코드가 다른 답을 하면 이 창구가 곧 명부입니다."""
    real = signup(client).json()["recovery_code"]
    res = reset(client, email, real if code == "real" else code)
    assert res.status_code == 400
    assert res.json()["detail"] == "이메일 또는 복구 코드가 맞지 않습니다"


def test_a_weak_new_password_is_refused(client):
    code = signup(client).json()["recovery_code"]
    res = reset(client, "a@example.com", code, "short")
    assert res.status_code == 400 and "10자" in res.json()["detail"]


def test_guessing_the_code_is_rate_limited_like_a_login(client):
    """여기만 열어 두면 복구 코드가 제한 없이 추측당하는 두 번째
    비밀번호가 됩니다."""
    signup(client)
    codes = [f"AAAAAA-BBBBBB-CCCCCC-{n:06d}" for n in range(12)]
    statuses = [reset(client, "a@example.com", c).status_code for c in codes]
    assert 429 in statuses, f"제한 없이 {len(codes)}번 시도됐습니다: {statuses}"


# ── 로그인한 뒤 새 코드 받기 ─────────────────────────────────────────────
def test_a_new_code_needs_the_password(client):
    """자리를 비운 사이 열린 브라우저 하나가 곧 재설정 수단이 되면 안 됩니다."""
    signup(client)
    assert client.post("/api/auth/recovery-code",
                       json={"current_password": "wrong-one-1"}).status_code == 400
    res = client.post("/api/auth/recovery-code", json={"current_password": GOOD})
    assert res.status_code == 200 and res.json()["code"]


def test_issuing_a_new_code_kills_the_old_one(client):
    """재발급은 "예전 것이 샜을지도 모른다" 는 뜻이기도 합니다."""
    old = signup(client).json()["recovery_code"]
    client.post("/api/auth/recovery-code", json={"current_password": GOOD})
    client.post("/api/auth/logout")
    assert reset(client, "a@example.com", old).status_code == 400


def test_an_anonymous_caller_cannot_mint_one(client):
    signup(client)
    client.post("/api/auth/logout")
    assert client.post("/api/auth/recovery-code",
                       json={"current_password": GOOD}).status_code == 401


# ── 콘솔이 마지막 길 ─────────────────────────────────────────────────────
def test_the_console_can_reset_without_either_secret(accounts):
    """비밀번호도 복구 코드도 잃은 계정의 마지막 길. 유일한 인증은 "이 서버
    에서 이 프로세스를 실행할 수 있다" 이고, 그래서 CLI 에만 있습니다."""
    user = accounts.register("a@example.com", GOOD)
    token = accounts.create_session(user.id)
    accounts.set_password(user.id, NEXT)
    assert accounts.authenticate("a@example.com", NEXT)
    assert accounts.user_for_session(token) is None, "세션도 함께 끊겨야 합니다"


def test_the_console_reset_still_checks_the_password_rules(accounts):
    user = accounts.register("a@example.com", GOOD)
    with pytest.raises(AccountError, match="10자"):
        accounts.set_password(user.id, "short")


# ── 탈퇴 ─────────────────────────────────────────────────────────────────
def delete(client, password=GOOD, email="a@example.com"):
    return client.post("/api/auth/delete-account", json={
        "current_password": password, "confirm_email": email})


def test_deleting_takes_the_account_and_its_broker_keys(client, accounts):
    user_id = signup(client).json()["id"]
    accounts.put_secret(user_id, "KIS_APP_KEY", "secret-value")
    assert delete(client).status_code == 200
    assert accounts.count() == 0
    assert accounts.configured(user_id) == {}
    assert client.get("/api/mine").status_code == 401


def test_the_email_has_to_be_typed_out(client, accounts):
    """브라우저가 채워 주는 비밀번호만으로는 "눌렀다" 와 "지우려고 했다" 를
    구분하지 못합니다 — 실거래 확인이 전략 이름을 받는 것과 같은 이유입니다."""
    signup(client)
    res = delete(client, email="")
    assert res.status_code == 400 and "이메일" in res.json()["detail"]
    assert accounts.count() == 1


def test_a_wrong_password_keeps_the_account(client, accounts):
    signup(client)
    assert delete(client, password="not-the-one-1").status_code == 400
    assert accounts.count() == 1


def test_a_running_bot_blocks_deletion(client, accounts, blocked):
    """실거래 봇이 도는 채로 지우면 주인 없는 주문과 포지션이 남습니다."""
    signup(client)
    blocked[0] = "자동매매가 돌고 있습니다 — 먼저 정지한 뒤 탈퇴하세요."
    res = delete(client)
    assert res.status_code == 409
    assert "정지" in res.json()["detail"]
    assert accounts.count() == 1, "막혔는데 지워졌습니다"


def test_the_bot_check_comes_before_the_password_check(client, accounts, blocked):
    """비밀번호를 맞게 적었는지와, 지금 지워도 되는지는 다른 질문입니다.
    틀린 비밀번호로 400 을 받으면 사람은 비밀번호만 고쳐서 다시 누릅니다."""
    signup(client)
    blocked[0] = "자동매매가 돌고 있습니다"
    assert delete(client, password="wrong-one-1").status_code == 409


def test_an_anonymous_caller_cannot_delete(client, accounts):
    signup(client)
    client.post("/api/auth/logout")
    assert delete(client).status_code == 401
    assert accounts.count() == 1


def test_the_audit_trail_survives_but_loses_the_email(client, accounts):
    """무엇이 있었는지는 운영자의 보안 기록입니다 — 탈퇴로 지워져야 하는
    것은 계정이지 사고 기록이 아닙니다. 대신 이메일 자리는 비웁니다."""
    user_id = signup(client).json()["id"]
    delete(client)
    rows = accounts.conn.execute(
        "SELECT action, detail FROM audit WHERE user_id=?", (user_id,)).fetchall()
    assert [r["action"] for r in rows].count("account_deleted") == 1
    assert all(r["detail"] == "" for r in rows if r["action"] != "account_deleted")
