"""한국+미국 휴장일 캘린더.

KRX/NYSE 공식 휴장일을 정적 데이터로 보유.
매년 1월에 갱신 권장. (자동 fetch 가능하지만 의존성 추가 회피)
"""
from __future__ import annotations
from datetime import date

# 한국 KRX 휴장일 (설/추석/대체공휴일/임시공휴일 포함)
# 출처: KRX 공식 영업일 캘린더
KR_HOLIDAYS_2026 = {
    "2026-01-01",  # 신정
    "2026-02-16", "2026-02-17", "2026-02-18",  # 설 연휴
    "2026-03-02",  # 삼일절(대체)
    "2026-05-05",  # 어린이날
    "2026-05-25",  # 부처님오신날 (대체)
    "2026-06-03",  # 지방선거
    "2026-06-15",  # 현충일(대체)
    "2026-08-17",  # 광복절(대체)
    "2026-09-24", "2026-09-25", "2026-09-26",  # 추석 연휴
    "2026-10-05",  # 개천절(대체)
    "2026-10-09",  # 한글날
    "2026-12-25",  # 성탄절
    "2026-12-31",  # 연말 휴장
}

KR_HOLIDAYS_2027 = {
    "2027-01-01",
    "2027-02-08", "2027-02-09", "2027-02-10",
    "2027-03-01",
    "2027-05-05",
    "2027-05-13",
    "2027-06-07",
    "2027-08-16",
    "2027-09-15", "2027-09-16", "2027-09-17",
    "2027-10-04",
    "2027-10-11",
    "2027-12-31",
}

# 미국 NYSE 휴장일
US_HOLIDAYS_2026 = {
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # MLK Day
    "2026-02-16",  # Presidents Day
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day (관측)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving
    "2026-12-25",  # Christmas
}

US_HOLIDAYS_2027 = {
    "2027-01-01",
    "2027-01-18",
    "2027-02-15",
    "2027-03-26",
    "2027-05-31",
    "2027-06-18",
    "2027-07-05",
    "2027-09-06",
    "2027-11-25",
    "2027-12-24",
}

KR_HOLIDAYS = KR_HOLIDAYS_2026 | KR_HOLIDAYS_2027
US_HOLIDAYS = US_HOLIDAYS_2026 | US_HOLIDAYS_2027


def is_kr_holiday(d: date | str) -> bool:
    if isinstance(d, date):
        d = d.isoformat()
    return d in KR_HOLIDAYS


def is_us_holiday(d: date | str) -> bool:
    if isinstance(d, date):
        d = d.isoformat()
    return d in US_HOLIDAYS


def next_kr_business_day(start: date) -> date:
    from datetime import timedelta
    d = start + timedelta(days=1)
    while d.weekday() >= 5 or is_kr_holiday(d):
        d += timedelta(days=1)
    return d


def next_us_business_day(start: date) -> date:
    from datetime import timedelta
    d = start + timedelta(days=1)
    while d.weekday() >= 5 or is_us_holiday(d):
        d += timedelta(days=1)
    return d
