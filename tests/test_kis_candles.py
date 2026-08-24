"""KIS 일봉 — 아직 안 끝난 오늘 봉을 확정봉으로 내주지 않는가.

KIS 의 일봉 응답에는 장중에도 "오늘" 행이 들어 있습니다. 그 행의 종가 자리는
확정 종가가 아니라 **그 순간의 현재가**라서, 09:30 에 읽은 값과 14:00 에 읽은
값이 다릅니다. 그걸 확정봉으로 받으면 같은 날짜의 봉이 하루 종일 모양을 바꾸고,
그 위에서 계산한 지표와 신호도 같이 흔들립니다. 백테스트에서는 절대 재현되지
않는 종류의 차이입니다.

여기서는 **구현식을 베끼지 않습니다.** 마감 시각을 다시 계산해서 대조하면
구현이 틀려도 같이 틀려 줄 뿐입니다. 대신 성질 셋만 봅니다.

  1. 한 번 돌려준 봉은 나중에 값이 바뀌지 않는다.
  2. 거래소가 확정한 종가는 감춰지지 않는다.
  3. 연휴 뒤 첫 거래일에도 확정봉이 빈 채로 오지 않는다.

(1) 만 보면 "전부 숨긴다" 가 통과하고, (2) 만 보면 "전부 내준다" 가 통과합니다.
(3) 은 앞선 수정이 실제로 밟았던 자리입니다 — 오늘 봉을 거르다가 연휴 직후
`latest_bars` 가 통째로 비어 그날 리스크 관리가 한 번도 안 돌았습니다.

네트워크는 부르지 않습니다. `_get` 을 KRX 캘린더로 만든 가짜 응답으로
바꿔치기하고, 시계는 고정합니다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from quant.core.types import UTC, Symbol
from quant.data import provider as P
from quant.data.calendar import KRX_REGULAR, KST, KrxCalendar
from quant.data.providers import kis as K

SYM = Symbol("005930", venue="kis", quote_currency="KRW", tick_size=Decimal("100"))

#: 오늘 행의 종가 자리에 장중 내내 들어 있는 값 — 확정 종가가 아닙니다.
INTRADAY = 71_000.0
#: 같은 날짜 행이 마감 뒤에 갖게 되는 값.
SETTLED = 77_000.0
#: 지난 거래일들. 값 자체는 아무 의미 없습니다.
PAST = 50_000.0


class Clock:
    """kis 모듈이 보는 시계. 테스트가 시각을 옮길 수 있게 한 겹 둡니다."""

    def __init__(self, at: datetime):
        self.at = at.astimezone(UTC)

    def now(self) -> datetime:
        return self.at

    def move_to(self, moment: datetime) -> None:
        self.at = moment.astimezone(UTC)


@pytest.fixture
def clock(monkeypatch):
    """코드가 시각을 읽는 자리를 **둘 다** 고정합니다.

    프로바이더는 `datetime.now` 로, `latest_bars` 는 `utcnow()` 로 지금을
    묻습니다. 한쪽만 고정하면 창은 진짜 오늘 것인데 응답은 고정된 날짜 것이
    되어, 테스트가 자기 배선을 검사하게 됩니다.
    """
    c = Clock(datetime(2026, 8, 24, 11, 0, tzinfo=KST))

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return c.now().astimezone(tz) if tz else c.now().replace(tzinfo=None)

    monkeypatch.setattr(K, "datetime", Frozen)
    monkeypatch.setattr(P, "utcnow", c.now)
    return c


class FakeKis(K.KisProvider):
    """네트워크 대신 KRX 캘린더에서 응답을 만듭니다.

    실제 응답의 성질 둘만 재현합니다: 행은 거래일에만 있고, **오늘 행은 장이
    열리는 순간부터 실려 오되 종가 자리가 마감 전까지 확정되지 않는다.**
    """

    def __init__(self, clock: Clock):
        super().__init__(app_key="test-key", app_secret="test-secret", paper=True)
        self._clock = clock
        self._cal = KrxCalendar()

    def _close_for(self, day: date, here: datetime) -> float | None:
        if day < here.date():
            return PAST
        if here.time() >= KRX_REGULAR.close:
            return SETTLED
        if here.time() >= KRX_REGULAR.open:
            return INTRADAY
        return None                     # 개장 전에는 오늘 행 자체가 없습니다

    async def _get(self, path, tr_id, params):
        here = self._clock.now().astimezone(KST)
        day = datetime.strptime(params["FID_INPUT_DATE_1"], "%Y%m%d").date()
        last = datetime.strptime(params["FID_INPUT_DATE_2"], "%Y%m%d").date()
        rows = []
        while day <= last:
            if self._cal.is_trading_day(day) and day <= here.date():
                close = self._close_for(day, here)
                if close is not None:
                    rows.append({
                        "stck_bsop_date": day.strftime("%Y%m%d"),
                        "stck_oprc": "70000", "stck_hgpr": "78000",
                        "stck_lwpr": "69000", "stck_clpr": str(close),
                        "acml_vol": "1000",
                    })
            day += timedelta(days=1)
        return {"rt_cd": "0", "output2": rows}


@pytest.fixture
async def provider(clock):
    p = FakeKis(clock)
    try:
        yield p
    finally:
        await p.close()


def by_date(bars) -> dict[str, float]:
    return {b.ts.date().isoformat(): b.close for b in bars}


async def test_a_bar_never_changes_after_it_has_been_handed_out(clock, provider):
    """장중에 받은 봉이 저녁에 다른 값이 되면, 그건 봉이 아니라 스냅샷입니다.

    같은 창을 11:00 과 16:00 에 두 번 부릅니다. 두 번 다 나온 날짜는 값이
    같아야 합니다. 새 날짜가 늘어나는 것은 정상이고(그 사이 장이 끝났으니),
    이미 준 날짜의 값이 바뀌는 것이 사고입니다.
    """
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 8, 25, tzinfo=UTC)

    clock.move_to(datetime(2026, 8, 24, 11, 0, tzinfo=KST))
    during = by_date(await provider.history(SYM, "1d", start, end))

    clock.move_to(datetime(2026, 8, 24, 16, 0, tzinfo=KST))
    after = by_date(await provider.history(SYM, "1d", start, end))

    changed = {d: (during[d], after[d]) for d in during
               if d in after and during[d] != after[d]}
    assert not changed, f"이미 내준 봉의 값이 바뀌었습니다: {changed}"
    assert set(during) <= set(after)


async def test_a_settled_close_is_not_hidden(clock, provider):
    """마감 뒤에는 그 날 봉이 확정 종가로 나와야 합니다.

    `ts + 봉길이 <= now` 로 판정하면 여기서 걸립니다 — KIS 의 `ts` 는 UTC
    시각이 아니라 KST 날짜 라벨이라, 마감이 다음 날 09:00 KST 로 계산되어
    확정된 당일 종가가 17시간 30분 동안 사라집니다.
    """
    clock.move_to(datetime(2026, 8, 24, 16, 0, tzinfo=KST))
    bars = by_date(await provider.history(SYM, "1d",
                                          datetime(2026, 8, 1, tzinfo=UTC),
                                          datetime(2026, 8, 25, tzinfo=UTC)))
    assert bars.get("2026-08-24") == SETTLED


async def test_intraday_still_serves_the_previous_closed_session(clock, provider):
    """오늘 봉을 거른다고 어제 봉까지 없어지면 안 됩니다.

    라이브 루프는 봉이 하나도 없으면 그 틱을 통째로 건너뜁니다 — 그날
    손절·트레일링·브로커 대조가 한 번도 평가되지 않습니다.
    """
    clock.move_to(datetime(2026, 8, 24, 11, 0, tzinfo=KST))
    bars = await provider.latest_bars(SYM, "1d", 3)
    assert bars, "장중에 확정봉이 하나도 오지 않았습니다"
    assert bars[-1].ts.date() == date(2026, 8, 21)      # 직전 금요일
    assert INTRADAY not in {b.close for b in bars}


@pytest.mark.parametrize("first_day", [
    date(2026, 2, 19),      # 설 연휴 02-16~18 뒤
    date(2026, 9, 29),      # 추석 09-24·25·28 뒤
    date(2027, 1, 4),       # 12-31 폐장 + 01-01 뒤
])
async def test_the_day_after_a_long_holiday_still_has_closed_bars(
        clock, provider, first_day):
    """연휴 뒤 첫 거래일 개장 직후 — 여기서 빈 리스트가 오면 그날이 통째로 빕니다.

    라이브 루프는 일봉에서 하루 한 번, 09:00 KST 에만 틱합니다. 그 한 번이
    빈 손으로 돌아오면 재시도가 없고, 하필 갭이 가장 큰 명절 직후에 보유
    포지션의 손절이 하루 동안 한 번도 평가되지 않습니다.
    """
    clock.move_to(datetime(first_day.year, first_day.month, first_day.day,
                           9, 0, 3, tzinfo=KST))
    bars = await provider.latest_bars(SYM, "1d", 3)
    assert bars, f"{first_day} 개장 직후 확정봉이 하나도 오지 않았습니다"
    assert all(b.ts.date() < first_day for b in bars)
    assert INTRADAY not in {b.close for b in bars}
