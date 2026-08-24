"""거래소가 페이지를 어떻게 잘라 주는가.

거래소마다 한 번에 주는 봉 수의 상한이 다릅니다. 바이낸스는 1000, 업비트는
200 입니다. 큰 값을 박아 두면 상한이 낮은 거래소에서 **첫 요청부터** 거절당하고,
예외 처리가 경고 한 줄을 남기고 멈춰서 **봉 0개**로 끝납니다. 그 위에서
백테스트가 조용히 돕니다 — 빈 시계열은 오류가 아니라 "거래 기회가 없었다" 로
보이기 때문에 아무도 알아채지 못합니다.

더 나쁜 경우가 있습니다. `since` 를 창의 시작이 아니라 **끝**으로 해석하는
거래소에서는 페이지마다 창의 마지막 구간만 돌아옵니다. 봉은 정상처럼 보이는데
사이가 몇 달씩 비어 있고, 200일 이동평균이 실제로는 몇 년을 덮습니다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

UTC = timezone.utc


@pytest.fixture
def provider(monkeypatch):
    """ccxt 없이 프로바이더 껍데기만 세웁니다 — 실제 거래소를 부르지 않습니다."""
    from quant.data.providers.ccxt_provider import CcxtProvider
    p = CcxtProvider.__new__(CcxtProvider)
    p.exchange_id = "testex"
    p._markets_loaded = True
    p._page_limit = 1000
    p.ex = None
    return p


class FakeExchange:
    """상한을 넘는 limit 을 거절하고, 그 이하는 연속 봉을 주는 거래소."""

    def __init__(self, cap: int, start_ms: int, step_ms: int, total: int):
        self.cap, self.start_ms, self.step_ms, self.total = cap, start_ms, step_ms, total
        self.calls: list[int] = []

    async def fetch_ohlcv(self, ticker, timeframe, since=None, limit=None):
        self.calls.append(limit)
        if limit and limit > self.cap:
            raise ValueError(f"limit {limit} exceeds {self.cap}")
        out = []
        ts = max(since or self.start_ms, self.start_ms)
        last = self.start_ms + self.step_ms * self.total
        while ts < last and len(out) < (limit or self.cap):
            out.append([ts, 100.0, 101.0, 99.0, 100.0, 5.0])
            ts += self.step_ms
        return out


async def _history(provider, ex, days=40):
    from quant.core.types import Symbol
    provider.ex = ex
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return await provider.history(Symbol("BTC/KRW", venue="testex"), "1h",
                                  start, start + timedelta(days=days))


@pytest.mark.asyncio
async def test_a_low_page_cap_does_not_end_the_download(provider):
    """상한 200 인 거래소에서 봉 0개로 끝나면 안 됩니다."""
    start = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
    ex = FakeExchange(cap=200, start_ms=start, step_ms=3_600_000, total=500)
    bars = await _history(provider, ex)
    assert len(bars) > 400, f"봉 {len(bars)}개 — 상한에 걸려 멈췄습니다"
    # 큰 값으로 시작해 줄여 가며 자기 상한을 찾아야 합니다.
    assert ex.calls[0] == 1000 and min(ex.calls) <= 200


@pytest.mark.asyncio
async def test_the_learned_page_size_is_remembered(provider):
    """한 번 배운 상한을 다음 종목에도 써야 합니다 — 매번 거절부터 시작하면 낭비입니다."""
    start = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
    ex = FakeExchange(cap=200, start_ms=start, step_ms=3_600_000, total=300)
    await _history(provider, ex)
    learned = provider._page_limit
    assert learned <= 200
    ex2 = FakeExchange(cap=200, start_ms=start, step_ms=3_600_000, total=300)
    await _history(provider, ex2)
    assert ex2.calls[0] == learned, "배운 상한을 잊고 다시 1000 부터 시작합니다"


@pytest.mark.asyncio
async def test_a_gapped_series_is_reported(provider, caplog):
    """구멍을 고칠 수는 없지만 조용히 지나가서는 안 됩니다."""

    class WindowEnd(FakeExchange):
        """`since` 를 창의 **끝**으로 해석하는 거래소.

        업비트가 이렇습니다. ccxt 가 `to = since + step*limit` 로 뒤집어 보내고,
        거래소는 그 시각 **이전** 의 봉을 자기 상한(200)만큼 돌려줍니다. 그래서
        요청한 창이 200칸보다 넓으면 앞쪽이 통째로 빠지고, 다음 페이지는 받은
        마지막 봉부터 다시 나아가므로 그 구멍이 영구히 남습니다.
        """

        HARD_CAP = 200

        async def fetch_ohlcv(self, ticker, timeframe, since=None, limit=None):
            self.calls.append(limit)
            want = limit or self.HARD_CAP
            end = (since or self.start_ms) + self.step_ms * want
            end = min(end, self.start_ms + self.step_ms * self.total)
            first = max(self.start_ms, end - self.step_ms * self.HARD_CAP)
            out, ts = [], first
            while ts < end:
                out.append([ts, 100.0, 101.0, 99.0, 100.0, 5.0])
                ts += self.step_ms
            return out

    start = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
    ex = WindowEnd(cap=100000, start_ms=start, step_ms=3_600_000, total=2000)
    with caplog.at_level(logging.WARNING):
        bars = await _history(provider, ex, days=83)
    # 실제로 구멍이 생겼는지 먼저 확인합니다 — 안 생겼으면 이 테스트가
    # 검사하는 것이 아무것도 없습니다.
    gaps = sum(1 for a, b in zip(bars, bars[1:])
               if (b.ts - a.ts).total_seconds() > 3600 * 3)
    assert gaps, "재현 자체가 실패했습니다 — 구멍이 안 생겼습니다"
    assert any("구멍" in r.message for r in caplog.records), \
        "구멍 난 시계열을 아무 말 없이 돌려줍니다"


@pytest.mark.asyncio
async def test_a_clean_series_says_nothing(provider, caplog):
    """정상 시계열에 경고를 띄우면 다음부터 아무도 안 읽습니다."""
    start = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
    ex = FakeExchange(cap=1000, start_ms=start, step_ms=3_600_000, total=600)
    with caplog.at_level(logging.WARNING):
        await _history(provider, ex, days=40)
    assert not any("구멍" in r.message for r in caplog.records)
