"""Data provider contract + registry.

A provider answers two questions and nothing else:
  1. What bars existed between A and B?  (`history` — backtests, warm-up)
  2. What is happening now?              (`latest_bars` / `stream` — live)

Keeping the interface this thin is what lets the same strategy run against a
CSV file, a crypto exchange, and a Korean broker without edits.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta

from quant.core.types import (
    Bar,
    Quote,
    Symbol,
    timeframe_delta,
    timeframe_seconds,
    utcnow,
)

log = logging.getLogger("quant.data")

_REGISTRY: dict[str, Callable[..., DataProvider]] = {}


def register_provider(name: str):
    def deco(cls):
        _REGISTRY[name.lower()] = cls
        return cls

    return deco


def create_provider(name: str, **kwargs) -> DataProvider:
    key = name.lower()
    if key not in _REGISTRY:
        raise KeyError(f"unknown data provider {name!r}; available: {sorted(_REGISTRY)}")
    return _REGISTRY[key](**kwargs)


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


class DataProvider(ABC):
    name = "base"
    supports_streaming = False

    @abstractmethod
    async def history(
        self, symbol: Symbol, timeframe: str, start: datetime, end: datetime
    ) -> list[Bar]:
        """Closed bars with open time in [start, end). Chronological, no gaps
        beyond what the venue itself had, and never a partially formed candle."""

    async def latest_bars(self, symbol: Symbol, timeframe: str, count: int = 1) -> list[Bar]:
        """Most recent `count` *closed* bars.

        일봉부터는 창을 넉넉히 잡습니다. 달력일과 거래일이 다르기 때문입니다 —
        일봉 3개를 달력 5일로 요청하면 설 연휴가 주말에 붙은 주에는 장이 한 번도
        안 서서 **빈 리스트**가 돌아옵니다. 그러면 라이브 루프는 그날 틱을
        통째로 건너뛰고, 하필 갭이 가장 큰 연휴 직후에 손절이 한 번도 평가되지
        않습니다.

        주말 몫으로 7/5, 연휴 몫으로 열흘을 더합니다. 넓게 잡아서 손해 보는
        것은 조회 한 번이 조금 무거워지는 것뿐이고, 좁게 잡아서 손해 보는 것은
        그날의 리스크 관리 전부입니다.
        """
        span = timeframe_delta(timeframe) * (count + 2)
        if timeframe_seconds(timeframe) >= 86400:
            span = span * 1.4 + timedelta(days=10)
        end = utcnow()
        bars = await self.history(symbol, timeframe, end - span, end)
        return bars[-count:]

    async def quote(self, symbol: Symbol) -> Quote | None:
        """Top of book. Providers without an L1 feed may return None; the
        execution layer then falls back to last trade price."""
        return None

    async def stream(
        self, symbols: list[Symbol], timeframe: str
    ) -> AsyncIterator[Bar]:  # pragma: no cover - overridden
        """Push closed bars as they form. Default: none (engine falls back to polling)."""
        if False:
            yield  # type: ignore[misc]
        raise NotImplementedError

    async def resolve(self, ticker: str) -> Symbol | None:
        """Turn a user-typed ticker into a fully specified Symbol."""
        return None

    async def describe(self, ticker: str) -> dict | None:
        """사람이 읽을 수 있는 종목 정보 — 이름, 현재가, 호가단위, 상하한가.

        `resolve` 는 엔진이 쓸 Symbol 을 만들고, 이건 화면이 보여줄 것을
        만듭니다. 종목코드만 띄우면 그게 무슨 회사인지 외운 사람만 고를 수
        있고, 잘못 고르면 다른 회사를 삽니다.

        지원하지 않는 프로바이더는 None 을 냅니다 — 그러면 화면이 "이 거래소
        에서는 검색을 지원하지 않습니다" 라고 말할 수 있습니다. 빈 목록을
        내면 "그런 종목이 없다" 는 뜻이 되어 버립니다.
        """
        return None

    async def describe_many(self, tickers: list[str]) -> dict[str, dict]:
        """여러 종목을 한 번에. 키는 티커, 값은 `describe` 와 같은 모양.

        기본 구현은 하나씩 도는 것뿐입니다. **다건 조회가 있는 거래소는 반드시
        덮어쓰세요** — 화면 하나가 종목 수만큼 호출을 내면 레이트 리밋에
        걸리고, 그러면 이름이 하나도 안 뜹니다. 이름이 가장 필요한 순간은
        종목이 많은 순간이라 하필 그때 전부 실패합니다.

        모르는 종목은 키 자체가 빠집니다. 빈 이름을 채워 넣으면 호출부가
        "찾았는데 이름이 없다" 와 "못 찾았다" 를 구분할 수 없습니다.
        """
        out: dict[str, dict] = {}
        for ticker in tickers:
            try:
                info = await self.describe(ticker)
            except Exception as exc:        # noqa: BLE001 — 한 종목이 실패해도
                # 나머지 이름은 나와야 합니다.
                log.debug("describe failed for %s: %s", ticker, exc)
                continue
            if info:
                out[str(info.get("ticker") or ticker)] = info
        return out

    async def close(self) -> None:
        return None


class CachingProvider(DataProvider):
    """Decorator that memoizes history windows in RAM.

    Backtests re-request overlapping windows constantly (warm-up, walk-forward
    folds, hyperopt trials); without this the same HTTP call runs thousands of
    times.
    """

    def __init__(self, inner: DataProvider, max_entries: int = 512):
        self.inner = inner
        self.name = f"cached:{inner.name}"
        self.supports_streaming = inner.supports_streaming
        self._cache: dict[tuple, list[Bar]] = {}
        self._order: list[tuple] = []
        self._max = max_entries
        self._locks: dict[tuple, asyncio.Lock] = {}

    def _key(self, symbol: Symbol, tf: str, start: datetime, end: datetime) -> tuple:
        return (symbol.key, tf, start.timestamp(), end.timestamp())

    async def history(self, symbol, timeframe, start, end) -> list[Bar]:
        key = self._key(symbol, timeframe, start, end)
        if key in self._cache:
            return self._cache[key]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._cache:                      # filled while we waited
                return self._cache[key]
            bars = await self.inner.history(symbol, timeframe, start, end)
            self._cache[key] = bars
            self._order.append(key)
            while len(self._order) > self._max:
                self._cache.pop(self._order.pop(0), None)
        self._locks.pop(key, None)
        return bars

    async def latest_bars(self, symbol, timeframe, count=1):
        return await self.inner.latest_bars(symbol, timeframe, count)

    async def quote(self, symbol):
        return await self.inner.quote(symbol)

    async def resolve(self, ticker):
        return await self.inner.resolve(ticker)

    async def describe(self, ticker):
        return await self.inner.describe(ticker)

    async def describe_many(self, tickers):
        # 안쪽으로 그대로 넘깁니다. 여기서 기본 구현을 타면 다건 조회를 가진
        # 프로바이더도 한 종목씩 부르게 됩니다.
        return await self.inner.describe_many(tickers)

    def stream(self, symbols, timeframe):
        return self.inner.stream(symbols, timeframe)

    async def close(self):
        await self.inner.close()


class CompositeProvider(DataProvider):
    """Routes each symbol to the first provider that can serve it.

    Real portfolios are cross-venue: Korean equities from KIS, US equities from
    Yahoo/Alpaca, crypto from ccxt. This keeps that routing out of strategies.
    """

    name = "composite"

    def __init__(self, providers: dict[str, DataProvider], default: str | None = None):
        self.providers = providers
        self.default = default or next(iter(providers))

    def _pick(self, symbol: Symbol) -> DataProvider:
        return self.providers.get(symbol.venue.lower(), self.providers[self.default])

    async def history(self, symbol, timeframe, start, end):
        return await self._pick(symbol).history(symbol, timeframe, start, end)

    async def latest_bars(self, symbol, timeframe, count=1):
        return await self._pick(symbol).latest_bars(symbol, timeframe, count)

    async def quote(self, symbol):
        return await self._pick(symbol).quote(symbol)

    async def resolve(self, ticker):
        for p in self.providers.values():
            sym = await p.resolve(ticker)
            if sym:
                return sym
        return None

    async def describe(self, ticker):
        for p in self.providers.values():
            found = await p.describe(ticker)
            if found:
                return found
        return None

    async def describe_many(self, tickers):
        """아직 못 찾은 것만 다음 거래소에 묻습니다.

        교차시장 포트폴리오에서는 국내 코드와 미국 티커가 한 목록에 섞여
        옵니다. 매번 전부를 모든 거래소에 물으면, 자기 시장이 아닌 종목까지
        포함한 요청이 통째로 거절되어 **맞는 것까지** 이름을 잃습니다.
        """
        out: dict[str, dict] = {}
        remaining = list(tickers)
        for p in self.providers.values():
            if not remaining:
                break
            found = await p.describe_many(remaining)
            out.update(found)
            seen = {str(k).strip().upper() for k in found}
            remaining = [t for t in remaining if str(t).strip().upper() not in seen]
        return out

    async def close(self):
        for p in self.providers.values():
            await p.close()


async def gather_history(
    provider: DataProvider,
    symbols: list[Symbol],
    timeframe: str,
    start: datetime,
    end: datetime,
    concurrency: int = 8,
) -> dict[str, list[Bar]]:
    """Fetch many symbols' history concurrently, bounded so we don't get
    rate-limited into a ban."""
    sem = asyncio.Semaphore(concurrency)

    async def one(sym: Symbol) -> tuple[str, list[Bar]]:
        async with sem:
            try:
                return sym.key, await provider.history(sym, timeframe, start, end)
            except Exception as exc:
                log.warning("history failed for %s: %s", sym, exc)
                return sym.key, []

    results = await asyncio.gather(*(one(s) for s in symbols))
    return dict(results)
