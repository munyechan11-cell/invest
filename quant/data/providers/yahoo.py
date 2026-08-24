"""Yahoo Finance chart API — no key, global equity/ETF/FX/crypto coverage.

Used as the default free source for equities and as a cross-check against
broker feeds. Rate limits are undocumented and enforced by IP, so requests are
serialized through a small token bucket.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

import httpx

from quant.core.aio import LazyLock
from quant.core.types import UTC, AssetClass, Bar, Quote, Symbol, timeframe_seconds
from quant.data.provider import DataProvider, register_provider

log = logging.getLogger("quant.data.yahoo")

_INTERVAL = {
    "1m": "1m", "2m": "2m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "60m": "1h", "1d": "1d", "1w": "1wk",
}
# Yahoo silently truncates intraday history; knowing the real ceiling turns a
# confusing empty backtest into an explicit warning.
_MAX_DAYS = {"1m": 7, "2m": 60, "5m": 60, "15m": 60, "30m": 60, "1h": 730}


class _RateLimiter:
    def __init__(self, per_second: float):
        self._interval = 1.0 / per_second
        self._next = 0.0
        self._lock = LazyLock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._next:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self._interval


@register_provider("yahoo")
class YahooProvider(DataProvider):
    name = "yahoo"

    BASE = "https://query1.finance.yahoo.com"

    def __init__(self, requests_per_second: float = 4.0, timeout: float = 20.0):
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; quant-engine/1.0)"},
            follow_redirects=True,
        )
        self._limiter = _RateLimiter(requests_per_second)

    async def _get(self, path: str, params: dict) -> dict:
        last_exc: Exception | None = None
        for attempt in range(4):
            await self._limiter.wait()
            try:
                r = await self._client.get(f"{self.BASE}{path}", params=params)
                if r.status_code == 429:
                    await asyncio.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as exc:              # network flake / 5xx
                last_exc = exc
                await asyncio.sleep(0.5 * (2 ** attempt))
        raise RuntimeError(f"yahoo request failed: {path} ({last_exc})")

    async def history(self, symbol, timeframe, start, end):
        interval = _INTERVAL.get(timeframe)
        if interval is None:
            raise ValueError(f"yahoo does not serve timeframe {timeframe!r}")
        span_days = (end - start).days
        cap = _MAX_DAYS.get(timeframe)
        if cap and span_days > cap:
            log.warning(
                "yahoo caps %s history at ~%dd; requested %dd for %s — result will be short",
                timeframe, cap, span_days, symbol.ticker,
            )
        data = await self._get(
            f"/v8/finance/chart/{symbol.ticker}",
            {
                "period1": int(start.timestamp()),
                "period2": int(end.timestamp()),
                "interval": interval,
                "includePrePost": "false",
                "events": "div,split",
            },
        )
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return []
        node = result[0]
        stamps = node.get("timestamp") or []
        quote = ((node.get("indicators") or {}).get("quote") or [{}])[0]
        # Prefer split/dividend-adjusted closes; unadjusted series fabricate
        # gap-downs that look exactly like a crash to a momentum model.
        adj = ((node.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose")
        step = timeframe_seconds(timeframe)
        bars: list[Bar] = []
        for i, ts in enumerate(stamps):
            o, h, low, c = (quote.get(k, [None] * len(stamps))[i]
                            for k in ("open", "high", "low", "close"))
            if None in (o, h, low, c):
                continue
            factor = (adj[i] / c) if (adj and adj[i] and c) else 1.0
            dt = datetime.fromtimestamp(ts - ts % step, tz=UTC)
            if not (start <= dt < end):
                continue
            v = (quote.get("volume") or [0] * len(stamps))[i] or 0
            bars.append(
                Bar(symbol, dt, o * factor, h * factor, low * factor, c * factor,
                    float(v), timeframe)
            )
        bars.sort(key=lambda b: b.ts)
        # Drop the still-forming last candle — acting on it is look-ahead.
        if bars and bars[-1].end_ts > datetime.now(UTC):
            bars.pop()
        return bars

    async def quote(self, symbol):
        try:
            data = await self._get(
                f"/v8/finance/chart/{symbol.ticker}", {"interval": "1m", "range": "1d"}
            )
            meta = ((data.get("chart") or {}).get("result") or [{}])[0].get("meta") or {}
            price = meta.get("regularMarketPrice")
            if not price:
                return None
            bid = meta.get("bid") or price * 0.9995
            ask = meta.get("ask") or price * 1.0005
            return Quote(symbol, datetime.now(UTC), float(bid), float(ask))
        except Exception:
            return None

    async def resolve(self, ticker: str):
        t = ticker.strip().upper()
        try:
            data = await self._get("/v1/finance/search", {"q": t, "quotesCount": 5})
        except Exception:
            return None
        for q in data.get("quotes", []):
            if not q.get("symbol"):
                continue
            qtype = (q.get("quoteType") or "").upper()
            asset = {
                "ETF": AssetClass.ETF,
                "CRYPTOCURRENCY": AssetClass.CRYPTO,
                "CURRENCY": AssetClass.FOREX,
                "FUTURE": AssetClass.FUTURE,
            }.get(qtype, AssetClass.EQUITY)
            return Symbol(
                q["symbol"], venue="yahoo", asset_class=asset,
                quote_currency=q.get("currency") or "USD",
            )
        return None

    async def close(self):
        await self._client.aclose()
