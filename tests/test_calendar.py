"""시장 캘린더.

The rule these tests protect: a live bot must not poll, signal, or order into a
closed book. Every failure here is a bot that trades at 3am on 설날 and reports
the rejections as API faults.
"""
from datetime import date, datetime, time, timedelta

import pytest

from quant.core.types import UTC
from quant.data.calendar import (
    KST,
    AlwaysOpen,
    KrxCalendar,
    calendar_for_venue,
    create_calendar,
)


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


# ── 휴장일 표에 없는 날이 들어가 있지 않은가 ─────────────────────────────
#
# 표에 잘못 들어간 날은 조용합니다. 그날 봇은 `_wait_for_market` 에서 자고,
# `_maintenance_cycle` 은 `market_open` 이 거짓이라 **손절 재평가와 수동 주문
# flush 를 건너뜁니다.** 사람이 누른 매도도 다음 개장까지 대기합니다. 그래서
# 이 검사는 "휴장일이 맞는가" 가 아니라 **"거래일을 휴장으로 적지 않았는가"**
# 를 봅니다 — 그쪽이 돈이 걸린 방향입니다.
#
# 규칙을 날짜로 고정합니다(관공서의 공휴일에 관한 규정 제3조):
#   · 현충일·신정은 대체공휴일이 없다
#   · 설날·추석 연휴는 **일요일** 겹침만 대체 사유다 (토요일은 아니다)
#   · 국경일·어린이날·부처님오신날·성탄절은 토·일 겹침 모두 대체 사유다

def test_memorial_day_has_no_substitute_holiday():
    """2026-06-06 현충일은 토요일. 다음 월요일은 **거래일**이다.

    현충일을 대체공휴일 대상으로 착각해 06-08 을 휴장으로 적어 두었었고,
    그날 코스피는 실제로 열렸습니다.
    """
    cal = KrxCalendar()
    assert cal.is_open(kst(2026, 6, 8, 11, 0)), "현충일에는 대체공휴일이 없다"


def test_chuseok_saturday_overlap_creates_no_substitute():
    """2026 추석 연휴는 목·금·토. 일요일과 겹치지 않으므로 대체가 없다.

    9/28(월)을 휴장으로 적어 두면 추석 직후 첫 거래일에 손절이 평가되지
    않습니다 — 연휴 뒤 갭이 가장 큰 날입니다.
    """
    cal = KrxCalendar()
    assert not cal.is_open(kst(2026, 9, 24, 11, 0))
    assert not cal.is_open(kst(2026, 9, 25, 11, 0))
    assert cal.is_open(kst(2026, 9, 28, 11, 0)), "설·추석은 일요일 겹침만 대체"


def test_lunar_new_year_sunday_overlap_creates_exactly_one_substitute():
    """2027 설 연휴는 토·일·월. 일요일 겹침 하나 → 대체 하루(화)뿐이다."""
    cal = KrxCalendar()
    assert not cal.is_open(kst(2027, 2, 8, 11, 0))     # 설날 다음날 (월)
    assert not cal.is_open(kst(2027, 2, 9, 11, 0))     # 대체공휴일 (화)
    assert cal.is_open(kst(2027, 2, 10, 11, 0)), "대체는 겹친 일요일 수만큼"


def test_national_holiday_weekend_overlap_does_create_a_substitute():
    """국경일은 현충일과 달리 토요일 겹침에도 대체가 붙는다 — 규칙의 반대편."""
    cal = KrxCalendar()
    assert not cal.is_open(kst(2026, 8, 17, 11, 0))    # 광복절(토) → 월 대체
    assert not cal.is_open(kst(2026, 10, 5, 11, 0))    # 개천절(토) → 월 대체


def test_constitution_day_is_a_holiday_again_from_2026():
    """제헌절이 공휴일로 되살아났고 KRX 도 휴장을 공표했습니다."""
    assert not KrxCalendar().is_open(kst(2026, 7, 17, 11, 0))


def test_the_published_2026_holiday_count_matches():
    """거래소가 공표한 2026 휴장일은 17일입니다.

    개수 하나가 표 전체의 오탈자를 잡습니다 — 날짜를 하나 잘못 넣으면
    다른 하나를 빼지 않는 한 개수가 어긋납니다.
    """
    from quant.data.calendar import KRX_HOLIDAYS

    assert len(KRX_HOLIDAYS[2026]) == 17
    assert len(set(KRX_HOLIDAYS[2026])) == 17, "중복된 날짜"


def test_us_early_close_only_when_the_eve_is_itself_a_session():
    """2027 독립기념일은 일요일이라 월요일이 휴장 — 그 전 금요일은 정규장이다.

    조기폐장을 잘못 적으면 오후 3시간 동안 손절이 평가되지 않습니다.
    """
    from zoneinfo import ZoneInfo

    from quant.data.calendar import UsEquityCalendar

    cal = UsEquityCalendar()
    et = ZoneInfo("America/New_York")
    assert not cal.is_open(datetime(2027, 7, 5, 11, 0, tzinfo=et))   # 대체 휴장
    assert cal.is_open(datetime(2027, 7, 2, 14, 0, tzinfo=et)), "조기폐장 아님"
    # 추수감사절 다음 날은 실제 조기폐장이라 13:00 이후가 닫혀 있어야 합니다.
    assert cal.is_open(datetime(2027, 11, 26, 12, 0, tzinfo=et))
    assert not cal.is_open(datetime(2027, 11, 26, 14, 0, tzinfo=et))
