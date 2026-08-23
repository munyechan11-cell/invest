"""시장 캘린더.

The rule these tests protect: a live bot must not poll, signal, or order into a
closed book. Every failure here is a bot that trades at 3am on 설날 and reports
the rejections as API faults.
"""
from datetime import date, datetime, time, timedelta, timezone

import pytest

from quant.data.calendar import (
    KST, AlwaysOpen, KrxCalendar, MarketCalendar, calendar_for_venue, create_calendar,
)
from quant.core.types import UTC


def kst(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=KST)


# ── crypto ───────────────────────────────────────────────────────────────
def test_crypto_is_always_open():
    cal = AlwaysOpen()
    assert cal.is_open(datetime(2026, 1, 1, 3, 0, tzinfo=UTC))
    assert cal.minutes_until_open(datetime(2026, 1, 1, 3, 0, tzinfo=UTC)) == 0.0


# ── KRX hours ────────────────────────────────────────────────────────────
def test_krx_regular_session_bounds():
    cal = KrxCalendar()
    assert not cal.is_open(kst(2026, 3, 10, 8, 59))
    assert cal.is_open(kst(2026, 3, 10, 9, 0))
    assert cal.is_open(kst(2026, 3, 10, 15, 29))
    assert not cal.is_open(kst(2026, 3, 10, 15, 30))     # close is exclusive
    assert not cal.is_open(kst(2026, 3, 10, 18, 0))


def test_krx_is_closed_at_the_weekend():
    cal = KrxCalendar()
    assert not cal.is_open(kst(2026, 3, 14, 11, 0))      # Saturday
    assert not cal.is_open(kst(2026, 3, 15, 11, 0))      # Sunday
    assert cal.is_open(kst(2026, 3, 16, 11, 0))          # Monday


def test_krx_lunar_new_year_is_closed():
    """설날 — the holiday a fixed weekday rule would get wrong every year."""
    cal = KrxCalendar()
    for day in (16, 17, 18):
        assert not cal.is_open(kst(2026, 2, day, 11, 0)), f"2026-02-{day} should be 휴장"
    assert cal.is_open(kst(2026, 2, 19, 11, 0))


def test_krx_chuseok_is_closed():
    cal = KrxCalendar()
    for day in (24, 25):
        assert not cal.is_open(kst(2026, 9, day, 11, 0))


def test_krx_year_end_close():
    cal = KrxCalendar()
    assert not cal.is_open(kst(2026, 12, 31, 11, 0))


def test_krx_uses_a_fixed_offset_so_no_tz_database_is_needed():
    """KST has had no daylight saving since 1988; the arithmetic must not drift."""
    cal = KrxCalendar()
    assert cal.tz.utcoffset(None) == timedelta(hours=9)
    # 00:30 UTC == 09:30 KST — inside the session
    assert cal.is_open(datetime(2026, 3, 10, 0, 30, tzinfo=UTC))
    # 23:00 UTC == 08:00 KST next day — outside it
    assert not cal.is_open(datetime(2026, 3, 9, 23, 0, tzinfo=UTC))


# ── next_open ────────────────────────────────────────────────────────────
def test_next_open_skips_the_weekend():
    cal = KrxCalendar()
    nxt = cal.next_open(kst(2026, 3, 13, 16, 0))         # Friday after close
    assert nxt.astimezone(KST).date() == date(2026, 3, 16)
    assert nxt.astimezone(KST).time() == time(9, 0)


def test_next_open_skips_a_holiday_block():
    cal = KrxCalendar()
    nxt = cal.next_open(kst(2026, 2, 16, 10, 0))         # inside 설날
    assert nxt.astimezone(KST).date() == date(2026, 2, 19)


def test_next_open_returns_now_during_a_session():
    cal = KrxCalendar()
    moment = kst(2026, 3, 10, 11, 0)
    assert cal.next_open(moment).astimezone(KST).date() == date(2026, 3, 11)
    assert cal.is_open(moment)


def test_minutes_until_open_is_zero_when_open():
    assert KrxCalendar().minutes_until_open(kst(2026, 3, 10, 11, 0)) == 0.0


def test_minutes_until_open_across_a_holiday():
    cal = KrxCalendar()
    minutes = cal.minutes_until_open(kst(2026, 2, 16, 10, 0))
    assert 60 * 24 * 2 < minutes < 60 * 24 * 4


# ── staleness ────────────────────────────────────────────────────────────
def test_the_holiday_table_admits_when_it_has_aged_out():
    """A silently wrong holiday table is the worst possible outcome, so the
    calendar states its own expiry rather than guessing past it."""
    cal = KrxCalendar()
    assert cal.check_freshness(datetime(2026, 6, 1, tzinfo=UTC)) == ""
    warning = cal.check_freshness(datetime(2099, 1, 1, tzinfo=UTC))
    assert warning and "갱신" in warning


# ── resolution ───────────────────────────────────────────────────────────
def test_venue_inference():
    assert isinstance(calendar_for_venue("kis"), KrxCalendar)
    assert isinstance(calendar_for_venue("binance", "crypto"), AlwaysOpen)
    # unknown venue defaults to always-open: trading at odd hours is visible in
    # the logs, a wrongly-closed calendar just silently does nothing
    assert isinstance(calendar_for_venue("some-new-venue"), AlwaysOpen)


def test_explicit_names_resolve():
    assert isinstance(create_calendar("krx"), KrxCalendar)
    assert isinstance(create_calendar("crypto"), AlwaysOpen)
    with pytest.raises(KeyError):
        create_calendar("nonexistent")
