"""계정과 자격증명 보관.

남의 증권사 키를 맡아두는 코드라 여기서는 "동작한다"로 부족합니다.
새어나갈 수 있는 경로마다 테스트가 하나씩 있어야 합니다.
"""
import sqlite3

import pytest

from quant.webapp import (
    AccountError,
    Accounts,
    SecretKeyMissing,
    hash_password,
    password_problem,
    verify_password,
)

SECRET = "k" * 48


@pytest.fixture
def accounts(tmp_path):
    return Accounts(tmp_path / "users.db", secret=SECRET)


# ── 시작 조건 ───────────────────────────────────────────────────────────
def test_it_refuses_to_start_without_a_secret_key(tmp_path, monkeypatch):
    """평문으로 남의 키를 받아두는 상태로 뜨느니 뜨지 않는 편이 낫습니다."""
    monkeypatch.delenv("QUANT_SECRET_KEY", raising=False)
    with pytest.raises(SecretKeyMissing) as exc:
        Accounts(tmp_path / "u.db")
    assert "QUANT_SECRET_KEY" in str(exc.value)


def test_a_short_secret_key_is_refused(tmp_path):
    with pytest.raises(SecretKeyMissing):
        Accounts(tmp_path / "u.db", secret="tooshort")


# ── 비밀번호 ────────────────────────────────────────────────────────────
def test_a_password_hash_is_salted_and_not_reversible():
    a, b = hash_password("same-password-1"), hash_password("same-password-1")
    assert a != b, "같은 비밀번호가 같은 해시를 내면 소금이 없는 것입니다"
    assert "same-password-1" not in a
    assert verify_password("same-password-1", a)
    assert not verify_password("same-password-2", a)


def test_a_corrupt_hash_does_not_raise():
    """DB 가 손상돼도 로그인 시도가 500 이 되면 안 됩니다."""
    assert not verify_password("x", "not-a-hash")
    assert not verify_password("x", "md5$1$aa$bb")


@pytest.mark.parametrize("bad", ["short", "12345678901", "abcdefghijk"])
def test_weak_passwords_are_named(bad):
    assert password_problem(bad)


def test_a_reasonable_password_passes():
    assert password_problem("correct-horse-9") == ""


# ── 가입 ────────────────────────────────────────────────────────────────
def test_the_first_user_is_the_admin_and_the_second_is_not(accounts):
    first = accounts.register("a@example.com", "correct-horse-9")
    second = accounts.register("b@example.com", "correct-horse-9")
    assert first.is_admin and not second.is_admin


def test_the_same_email_cannot_register_twice(accounts):
    accounts.register("a@example.com", "correct-horse-9")
    with pytest.raises(AccountError):
        accounts.register("A@Example.com", "correct-horse-9")


# ── 세션 ────────────────────────────────────────────────────────────────
def test_a_session_resolves_and_revokes(accounts):
    u = accounts.register("a@example.com", "correct-horse-9")
    token = accounts.create_session(u.id)
    assert accounts.user_for_session(token).id == u.id
    accounts.revoke(token)
    assert accounts.user_for_session(token) is None


def test_session_tokens_are_stored_hashed(accounts, tmp_path):
    """DB 사본 한 장이 곧 모든 사람의 로그인 상태가 되면 안 됩니다."""
    u = accounts.register("a@example.com", "correct-horse-9")
    token = accounts.create_session(u.id)
    raw = sqlite3.connect(tmp_path / "users.db").execute(
        "SELECT token_hash FROM sessions").fetchone()[0]
    assert token not in raw


def test_changing_the_password_ends_other_sessions(accounts):
    """비밀번호를 바꾸는 이유의 절반은 누가 봤을지 모른다는 것입니다."""
    u = accounts.register("a@example.com", "correct-horse-9")
    stale = accounts.create_session(u.id)
    accounts.change_password(u.id, "correct-horse-9", "brand-new-pass-7")
    assert accounts.user_for_session(stale) is None
    assert accounts.authenticate("a@example.com", "brand-new-pass-7")


def test_the_wrong_current_password_cannot_change_it(accounts):
    u = accounts.register("a@example.com", "correct-horse-9")
    with pytest.raises(AccountError):
        accounts.change_password(u.id, "wrong", "brand-new-pass-7")


# ── 자격증명 ────────────────────────────────────────────────────────────
def test_a_saved_secret_is_never_handed_back(accounts):
    u = accounts.register("a@example.com", "correct-horse-9")
    accounts.put_secret(u.id, "KIS_APP_KEY", "PSabcdefghijklmnopqrABCD")
    shown = accounts.configured(u.id)
    assert shown == {"KIS_APP_KEY": "ABCD"}, "화면에 끝 4자리보다 많이 갔습니다"
    assert accounts.secrets_for(u.id)["KIS_APP_KEY"] == "PSabcdefghijklmnopqrABCD"


def test_secrets_are_encrypted_on_disk(accounts, tmp_path):
    u = accounts.register("a@example.com", "correct-horse-9")
    accounts.put_secret(u.id, "KIS_APP_SECRET", "SUPER-SECRET-VALUE-1234")
    blob = sqlite3.connect(tmp_path / "users.db").execute(
        "SELECT ciphertext FROM user_secrets").fetchone()[0]
    assert b"SUPER-SECRET-VALUE-1234" not in bytes(blob)


def test_one_user_cannot_see_anothers_secrets(accounts):
    a = accounts.register("a@example.com", "correct-horse-9")
    b = accounts.register("b@example.com", "correct-horse-9")
    accounts.put_secret(a.id, "KIS_APP_KEY", "AAAA-key-value-1111")
    assert accounts.secrets_for(b.id) == {}
    assert accounts.configured(b.id) == {}


def test_a_newline_in_a_value_is_refused(accounts):
    """줄바꿈 하나가 저장 파일에서는 두 번째 키가 됩니다."""
    u = accounts.register("a@example.com", "correct-horse-9")
    with pytest.raises(AccountError):
        accounts.put_secret(u.id, "KIS_APP_KEY", "value\nQUANT_SECRET_KEY=stolen")


def test_a_wrong_secret_key_yields_nothing_rather_than_garbage(tmp_path):
    """키를 잃어버렸을 때 조용히 빈 값으로 매매하러 가면 안 됩니다."""
    a = Accounts(tmp_path / "u.db", secret=SECRET)
    u = a.register("a@example.com", "correct-horse-9")
    a.put_secret(u.id, "KIS_APP_KEY", "value-1234")
    a.close()

    other = Accounts(tmp_path / "u.db", secret="d" * 48)
    assert other.secrets_for(u.id) == {}
    assert other.configured(u.id) == {"KIS_APP_KEY": "1234"}


def test_the_audit_log_records_the_name_but_not_the_value(accounts):
    u = accounts.register("a@example.com", "correct-horse-9")
    accounts.put_secret(u.id, "KIS_APP_KEY", "SECRET-VALUE-9999")
    entries = accounts.history(u.id)
    assert any(e["action"] == "secret_saved" for e in entries)
    assert not any("SECRET-VALUE-9999" in str(e) for e in entries)
