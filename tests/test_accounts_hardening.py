"""디스크 위의 저장소 — 파일 권한, 값 크기, 그리고 사용량 계량.

`test_accounts.py` 는 "값이 새어나가는 경로"를 봅니다. 여기서는 그 값이
**어디에 어떻게 놓여 있는가**를 봅니다. 0600 인 DB 옆에 0644 인 사본이
있으면 잠근 것이 아니고, 크기 상한이 화면에만 있으면 화면이 하나 더 생기는
순간 없는 것과 같습니다.
"""
import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quant.alpha.llm_client import price_for
from quant.webapp import AccountError, Accounts
from quant.webapp.usage import PLANS, UsageStore

SECRET = "k" * 48
KST = timezone(timedelta(hours=9))

MODEL = "gemini-3.7-flash"
ONE = {"model": MODEL, "llm_calls": 16,
       "input_tokens": 53_289, "output_tokens": 6_571}


@pytest.fixture
def lax_umask():
    """서비스가 물려받는 흔한 umask. 022 여야 이 검사가 의미를 가집니다."""
    previous = os.umask(0o022)
    yield
    os.umask(previous)


@pytest.fixture
def accounts(tmp_path, lax_umask):
    return Accounts(tmp_path / "users.db", secret=SECRET)


def modes(db: Path) -> dict[str, int]:
    """DB 와 사이드카의 권한. 없는 파일은 빠집니다."""
    out = {}
    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{db}{suffix}")
        if p.exists():
            out[suffix or "db"] = stat.S_IMODE(p.stat().st_mode)
    return out


# ── 파일 권한 ───────────────────────────────────────────────────────────
def test_the_wal_sidecar_is_no_more_readable_than_the_db(accounts, tmp_path):
    """0600 인 DB 옆의 0644 사본에는 같은 행이 들어 있습니다."""
    u = accounts.register("a@example.com", "correct-horse-9")
    accounts.put_secret(u.id, "KIS_APP_SECRET", "SUPER-SECRET-VALUE-1234")

    found = modes(tmp_path / "users.db")
    assert "-wal" in found, "WAL 이 켜져 있지 않으면 이 검사가 아무것도 안 봅니다"
    assert all(m & 0o077 == 0 for m in found.values()), found


def test_the_sidecars_come_back_private_after_sqlite_recreates_them(tmp_path, lax_umask):
    """사이드카는 지워졌다 다시 생깁니다. 한 번의 chmod 로는 부족합니다."""
    db = tmp_path / "users.db"
    first = Accounts(db, secret=SECRET)
    u = first.register("a@example.com", "correct-horse-9")
    first.put_secret(u.id, "KIS_APP_KEY", "PSabcdefghijklmnopqrABCD")
    first.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)

    again = Accounts(db, secret=SECRET)
    again.put_secret(u.id, "KIS_APP_SECRET", "another-value-5678")
    found = modes(db)
    assert "-wal" in found
    assert all(m & 0o077 == 0 for m in found.values()), found
    again.close()


def test_a_db_left_open_by_an_older_build_is_repaired_on_start(tmp_path, lax_umask):
    """이미 배포돼 0644 로 굴러가던 파일도 다음 기동에 닫혀야 합니다."""
    db = tmp_path / "users.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{db}{suffix}")
        if p.exists():
            os.chmod(p, 0o644)

    store = Accounts(db, secret=SECRET)
    assert all(m & 0o077 == 0 for m in modes(db).values()), modes(db)
    store.close()


def test_the_usage_db_is_private_too(tmp_path, lax_umask):
    """누가 얼마를 쓰는지도 옆 계정이 읽을 것은 아닙니다."""
    db = tmp_path / "usage.db"
    store = UsageStore(db)
    store.record(1, **ONE)
    assert all(m & 0o077 == 0 for m in modes(db).values()), modes(db)
    store.close()


# ── 값 크기 ─────────────────────────────────────────────────────────────
def test_the_store_itself_refuses_an_absurd_value(accounts):
    """API 상한은 두 번째 방어선이어야지 유일한 방어선이면 안 됩니다."""
    u = accounts.register("a@example.com", "correct-horse-9")
    with pytest.raises(AccountError) as exc:
        accounts.put_secret(u.id, "KIS_APP_SECRET", "A" * 200_000)
    assert "너무 깁니다" in str(exc.value)
    assert accounts.configured(u.id) == {}, "거절해 놓고 저장했습니다"


def test_a_real_sized_credential_still_fits(accounts):
    """한투 앱 시크릿이 실측 180자 남짓입니다. 상한이 이것을 막으면 안 됩니다."""
    u = accounts.register("a@example.com", "correct-horse-9")
    accounts.put_secret(u.id, "KIS_APP_SECRET", "S" * 180 + "9999")
    assert accounts.configured(u.id) == {"KIS_APP_SECRET": "9999"}


def test_an_absurd_name_is_refused_as_well(accounts):
    """이름도 값과 같은 행에 들어갑니다."""
    u = accounts.register("a@example.com", "correct-horse-9")
    with pytest.raises(AccountError):
        accounts.put_secret(u.id, "K" * 5_000, "value-1234")
    with pytest.raises(AccountError):
        accounts.put_secret(u.id, "   ", "value-1234")
    assert accounts.configured(u.id) == {}


# ── 사용량 계량 ─────────────────────────────────────────────────────────
@pytest.fixture
def usage(tmp_path):
    store = UsageStore(tmp_path / "usage.db")
    yield store
    store.close()


def test_the_recorded_cost_is_the_price_list_times_the_tokens(usage):
    """계량이 요금표와 어긋나면 상한이 엉뚱한 금액에서 걸립니다."""
    pin, pout = price_for(MODEL)
    want = ONE["input_tokens"] / 1e6 * pin + ONE["output_tokens"] / 1e6 * pout
    assert usage.record(1, **ONE) == pytest.approx(want)
    assert usage.month(1)["cost_usd"] == pytest.approx(want, abs=1e-4)


def test_a_kst_clock_does_not_get_the_nine_hours_added_twice(usage):
    """KST 로 온 시각에 또 9시간을 더하면 하루 경계가 오후 3시로 밀립니다."""
    # 같은 순간을 두 시계로: 2026-08-24 20:00 KST = 2026-08-24 11:00 UTC.
    # 어느 쪽으로 적어도 한국 날짜는 8월 24일 하나여야 합니다.
    as_kst = datetime(2026, 8, 24, 20, 0, tzinfo=KST)
    as_utc = datetime(2026, 8, 24, 11, 0, tzinfo=timezone.utc)
    usage.record(1, now=as_kst, **ONE)
    assert usage.today(1, now=as_utc)["deliberations"] == 1
    assert usage.today(1, now=as_kst)["deliberations"] == 1


def test_a_kst_evening_still_belongs_to_that_korean_day(usage):
    """저녁 9시(KST)는 UTC 로는 이미 정오지만, 사용자에게는 아직 오늘입니다."""
    morning = datetime(2026, 8, 24, 9, 30, tzinfo=KST)
    evening = datetime(2026, 8, 24, 21, 0, tzinfo=KST)
    usage.record(1, now=morning, **ONE)
    usage.record(1, now=evening, **ONE)
    assert usage.today(1, now=evening)["deliberations"] == 2
    # 자정을 넘기면 비어야 합니다.
    assert usage.today(1, now=evening + timedelta(hours=4))["deliberations"] == 0


def test_a_byo_key_user_is_not_told_they_are_blocked(usage):
    """운영자 부담으로 한도를 쓴 뒤 자기 키를 넣은 사람이 여기로 옵니다."""
    for _ in range(PLANS["free"].daily_deliberations):
        usage.record(1, **ONE)
    assert not usage.allow(1, "free")[0]
    assert usage.allow(1, "free", own_key=True)[0]

    mine = usage.status(1, "free", own_key=True)
    assert mine["allowed"], "집행은 통과시키면서 화면은 막혔다고 말합니다"
    assert mine["reason"] == ""
    assert not usage.status(1, "free")["allowed"]


def test_a_byo_key_users_spend_never_lands_on_the_operator(usage):
    for _ in range(50):
        usage.record(1, own_key=True, **ONE)
    assert usage.operator_month()["cost_usd"] == 0.0
    assert usage.leaderboard() == []
    assert usage.month(1)["cost_usd"] == 0.0
