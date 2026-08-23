"""시장 캘린더 — trading sessions and holidays.

A live bot without one of these is a bug with a scheduler: it polls for candles
at 3am, computes signals from a stale tape, and sends orders into a closed
book. Every rejection looks like an API problem and none of them are.

Three calendars ship here:

  · `AlwaysOpen`   crypto — 24/7, no sessions, no holidays
  · `KrxCalendar`  KOSPI/KOSDAQ — 09:00–15:30 KST, with the lunar holidays
  · `UsEquity`     NYSE/NASDAQ — 09:30–16:00 ET, with early closes

Korea is the easy one to get exactly right: KST is a fixed UTC+9 with no
daylight saving, so the arithmetic needs no timezone database at all. The US
calendar does need one, and says so rather than guessing.

**Holiday tables go stale.** The Korean lunar holidays move every year and KRX
publishes them one year ahead, so the table below is explicitly dated and
`stale_after` will tell you when it stops being trustworthy instead of letting
a silent wrong answer through.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

from quant.core.types import UTC

log = logging.getLogger("quant.calendar")

KST = timezone(timedelta(hours=9))          # 한국은 서머타임이 없다 — 고정 오프셋


@dataclass(frozen=True)
class Session:
    """One continuous trading window, in the venue's local time."""

    open: time
    close: time
    label: str = "regular"

    def contains(self, local: time) -> bool:
        return self.open <= local < self.close


class MarketCalendar(ABC):
    """When is this venue actually tradable."""

    name = "calendar"
    tz: timezone | None = None
    #: date after which the holiday table is no longer trustworthy
    stale_after: date | None = None

    @abstractmethod
    def sessions_on(self, day: date) -> list[Session]:
        """Trading windows for `day`. Empty means the venue is shut."""

    def is_trading_day(self, day: date) -> bool:
        return bool(self.sessions_on(day))

    def local(self, moment: datetime) -> datetime:
        if self.tz is None:
            return moment
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(self.tz)

    def is_open(self, moment: datetime) -> bool:
        local = self.local(moment)
        return any(s.contains(local.time()) for s in self.sessions_on(local.date()))

    def next_open(self, moment: datetime, horizon_days: int = 14) -> datetime | None:
        """The next instant the venue opens, at or after `moment`.

        Returns None rather than guessing when nothing is found inside the
        horizon — a calendar that silently invents a session is worse than one
        that admits it does not know.
        """
        local = self.local(moment)
        for offset in range(horizon_days + 1):
            day = local.date() + timedelta(days=offset)
            for session in self.sessions_on(day):
                candidate = datetime.combine(day, session.open, tzinfo=self.tz or UTC)
                if candidate >= local:
                    return candidate.astimezone(UTC)
        log.warning("%s: no session found within %d days of %s",
                    self.name, horizon_days, moment.isoformat())
        return None

    def next_close(self, moment: datetime, horizon_days: int = 14) -> datetime | None:
        local = self.local(moment)
        for offset in range(horizon_days + 1):
            day = local.date() + timedelta(days=offset)
            for session in self.sessions_on(day):
                candidate = datetime.combine(day, session.close, tzinfo=self.tz or UTC)
                if candidate > local:
                    return candidate.astimezone(UTC)
        return None

    def minutes_until_open(self, moment: datetime) -> float:
        if self.is_open(moment):
            return 0.0
        nxt = self.next_open(moment)
        return (nxt - moment).total_seconds() / 60 if nxt else float("inf")

    def check_freshness(self, moment: datetime | None = None) -> str:
        """Warn when the holiday table has aged out. Returns '' when fine."""
        if self.stale_after is None:
            return ""
        today = (moment or datetime.now(UTC)).date()
        if today <= self.stale_after:
            return ""
        return (f"{self.name} 휴장일 표가 {self.stale_after.isoformat()} 까지만 "
                f"입력되어 있습니다 — 그 이후 날짜는 휴장일을 놓칠 수 있습니다. "
                f"quant/data/calendar.py 의 표를 갱신하세요.")


class AlwaysOpen(MarketCalendar):
    """Crypto. No sessions, no holidays, no mercy."""

    name = "always_open"

    def sessions_on(self, day: date) -> list[Session]:
        return [Session(time(0, 0), time(23, 59, 59, 999999), "24h")]

    def is_open(self, moment: datetime) -> bool:
        return True

    def next_open(self, moment: datetime, horizon_days: int = 14) -> datetime:
        return moment

    def minutes_until_open(self, moment: datetime) -> float:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 한국거래소 (KRX)
# ─────────────────────────────────────────────────────────────────────────────
#: 정규장. 동시호가(08:30–09:00, 15:20–15:30)는 정규장 안에 포함되어 있고,
#: 시간외 거래(15:40–18:00)는 유동성과 체결 방식이 달라 제외한다 — 시간외를
#: 정규장처럼 취급하면 백테스트의 체결 가정이 전부 어긋난다.
KRX_REGULAR = Session(time(9, 0), time(15, 30), "정규장")

#: KRX 휴장일. 음력 명절은 매년 바뀌므로 거래소 공표에 맞춰 갱신해야 한다.
#: 대체공휴일과 임시공휴일도 포함한다.
KRX_HOLIDAYS: dict[int, tuple[str, ...]] = {
    2024: (
        "01-01", "02-09", "02-10", "02-12", "03-01", "04-10", "05-01", "05-06",
        "05-15", "06-06", "08-15", "09-16", "09-17", "09-18", "10-01", "10-03",
        "10-09", "12-25", "12-31",
    ),
    2025: (
        "01-01", "01-27", "01-28", "01-29", "01-30", "03-03", "05-01", "05-05",
        "05-06", "06-03", "06-06", "08-15", "10-03", "10-06", "10-07", "10-08",
        "10-09", "12-25", "12-31",
    ),
    2026: (
        "01-01", "02-16", "02-17", "02-18", "03-02", "05-01", "05-05", "05-25",
        "06-03", "06-08", "08-17", "09-24", "09-25", "09-28", "10-05", "10-09",
        "12-25", "12-31",
    ),
    2027: (
        "01-01", "02-08", "02-09", "02-10", "03-01", "05-05", "05-13", "06-07",
        "08-16", "09-14", "09-15", "09-16", "10-04", "10-11", "12-25", "12-31",
    ),
}


class KrxCalendar(MarketCalendar):
    """KOSPI / KOSDAQ.

    KST is a fixed UTC+9 — Korea abolished daylight saving in 1988 — so this
    needs no timezone database and cannot drift with a stale tzdata package.
    """

    name = "krx"
    tz = KST
    stale_after = date(max(KRX_HOLIDAYS), 12, 31)

    def __init__(self, extra_holidays: Iterable[date] = (),
                 include_year_end: bool = True):
        self._holidays: set[date] = set()
        for year, days in KRX_HOLIDAYS.items():
            for md in days:
                month, day = md.split("-")
                self._holidays.add(date(year, int(month), int(day)))
        self._holidays.update(extra_holidays)
        # 연말 폐장일(12/31)은 위 표에 이미 들어 있다. 끄고 싶으면 여기서 제거.
        if not include_year_end:
            self._holidays = {d for d in self._holidays
                              if not (d.month == 12 and d.day == 31)}

    def sessions_on(self, day: date) -> list[Session]:
        if day.weekday() >= 5 or day in self._holidays:
            return []
        return [KRX_REGULAR]

    @property
    def holidays(self) -> set[date]:
        return set(self._holidays)


# ─────────────────────────────────────────────────────────────────────────────
# 미국 주식 (NYSE / NASDAQ)
# ─────────────────────────────────────────────────────────────────────────────
US_REGULAR = Session(time(9, 30), time(16, 0), "regular")
US_EARLY_CLOSE = Session(time(9, 30), time(13, 0), "early_close")

US_HOLIDAYS: dict[int, tuple[str, ...]] = {
    2024: ("01-01", "01-15", "02-19", "03-29", "05-27", "06-19", "07-04",
           "09-02", "11-28", "12-25"),
    2025: ("01-01", "01-09", "01-20", "02-17", "04-18", "05-26", "06-19",
           "07-04", "09-01", "11-27", "12-25"),
    2026: ("01-01", "01-19", "02-16", "04-03", "05-25", "06-19", "07-03",
           "09-07", "11-26", "12-25"),
    2027: ("01-01", "01-18", "02-15", "03-26", "05-31", "06-18", "07-05",
           "09-06", "11-25", "12-24"),
}
US_EARLY_CLOSES: dict[int, tuple[str, ...]] = {
    2024: ("07-03", "11-29", "12-24"),
    2025: ("07-03", "11-28", "12-24"),
    2026: ("11-27", "12-24"),
    2027: ("07-02", "11-26"),
}


class UsEquityCalendar(MarketCalendar):
    """NYSE / NASDAQ. Needs a timezone database for US daylight saving."""

    name = "us_equity"
    stale_after = date(max(US_HOLIDAYS), 12, 31)

    def __init__(self):
        try:
            from zoneinfo import ZoneInfo

            self.tz = ZoneInfo("America/New_York")
        except Exception as exc:      # pragma: no cover - platform dependent
            # Falling back to a fixed offset would be wrong for half the year,
            # which is worse than refusing.
            raise RuntimeError(
                "US equity hours need a timezone database (US observes daylight "
                f"saving): {exc}. Install `tzdata`."
            ) from exc
        self._holidays = {
            date(y, int(md.split("-")[0]), int(md.split("-")[1]))
            for y, days in US_HOLIDAYS.items() for md in days
        }
        self._early = {
            date(y, int(md.split("-")[0]), int(md.split("-")[1]))
            for y, days in US_EARLY_CLOSES.items() for md in days
        }

    def sessions_on(self, day: date) -> list[Session]:
        if day.weekday() >= 5 or day in self._holidays:
            return []
        return [US_EARLY_CLOSE if day in self._early else US_REGULAR]


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────
CALENDARS = {
    "always_open": AlwaysOpen,
    "24/7": AlwaysOpen,
    "crypto": AlwaysOpen,
    "krx": KrxCalendar,
    "kis": KrxCalendar,
    "kr": KrxCalendar,
    "us_equity": UsEquityCalendar,
    "nyse": UsEquityCalendar,
    "nasdaq": UsEquityCalendar,
    "alpaca": UsEquityCalendar,
}


def create_calendar(name: str) -> MarketCalendar:
    key = (name or "always_open").lower()
    if key not in CALENDARS:
        raise KeyError(f"unknown calendar {name!r}; available: {sorted(set(CALENDARS))}")
    return CALENDARS[key]()


def calendar_for_venue(venue: str, asset_class: str = "") -> MarketCalendar:
    """Best guess from the venue name, defaulting to 24/7.

    Defaulting to always-open is the safe direction: a wrong 24/7 calendar
    trades at odd hours and is obvious in the logs, while a wrongly-closed
    calendar silently does nothing and looks like the strategy having no
    opinions.
    """
    v = (venue or "").lower()
    if asset_class == "crypto":
        return AlwaysOpen()
    for key, cls in CALENDARS.items():
        if key in v:
            return cls()
    return AlwaysOpen()
