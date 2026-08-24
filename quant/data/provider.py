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
from datetime import datetime

from quant.core.types import Bar, Quote, Symbol, timeframe_delta, utcnow

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
        """Most recent `count` *closed* bars."""
        span = timeframe_delta(timeframe) * (count + 2)
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
    return {k: v for k, v in results}
