"""Crypto exchanges through ccxt — 100+ venues behind one interface.

Follows freqtrade's hard-won operational rules:
  * never trust the last candle (it is still forming) — drop it
  * page backwards from `since` because most venues cap `limit`
  * load markets once and use their precision/limits for lot & tick sizing
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal

from quant.core.aio import LazyLock
from quant.core.types import UTC, AssetClass, Bar, Quote, Symbol, timeframe_seconds
from quant.data.provider import DataProvider, register_provider

log = logging.getLogger("quant.data.ccxt")


@register_provider("ccxt")
class CcxtProvider(DataProvider):
    name = "ccxt"
    supports_streaming = False

    def __init__(
        self,
        exchange: str = "binance",
        api_key: str = "",
        secret: str = "",
        sandbox: bool = False,
        market_type: str = "spot",
        rate_limit_ms: int | None = None,
    ):
        try:
            import ccxt.async_support as ccxt_async
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "ccxt is required for crypto data: pip install 'ccxt>=4.4'"
            ) from exc
        if not hasattr(ccxt_async, exchange):
            raise ValueError(f"ccxt has no exchange {exchange!r}")
        opts = {
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {"defaultType": market_type},
        }
        if rate_limit_ms:
            opts["rateLimit"] = rate_limit_ms
        self.exchange_id = exchange
        self.ex = getattr(ccxt_async, exchange)(opts)
        if sandbox:
            self.ex.set_sandbox_mode(True)
        self._markets_loaded = False
        self._lock = LazyLock()

    async def _ensure_markets(self) -> None:
        if self._markets_loaded:
            return
        async with self._lock:
            if not self._markets_loaded:
                await self.ex.load_markets()
                self._markets_loaded = True

    async def resolve(self, ticker: str):
        await self._ensure_markets()
        t = ticker.upper().replace("-", "/")
        if t not in self.ex.markets and "/" not in t:
            t = f"{t}/USDT"
        market = self.ex.markets.get(t)
        if market is None:
            return None
        limits = market.get("limits") or {}
        precision = market.get("precision") or {}

        def step(p) -> Decimal:
            if p is None:
                return Decimal("0.00000001")
            # ccxt reports either a decimal-place count or a tick size
            return Decimal(str(p)) if p < 1 else Decimal(1).scaleb(-int(p))

        return Symbol(
            ticker=market["symbol"],
            venue=self.exchange_id,
            asset_class=AssetClass.CRYPTO,
            quote_currency=market.get("quote") or "USDT",
            lot_size=Decimal(str((limits.get("amount") or {}).get("min") or step(precision.get("amount")))),
            tick_size=step(precision.get("price")),
            min_notional=Decimal(str((limits.get("cost") or {}).get("min") or 0)),
        )

    async def history(self, symbol, timeframe, start, end):
        await self._ensure_markets()
        step_ms = timeframe_seconds(timeframe) * 1000
        since = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        out: list[Bar] = []
        seen: set[int] = set()
        while since < end_ms:
            try:
                chunk = await self.ex.fetch_ohlcv(symbol.ticker, timeframe, since=since, limit=1000)
            except Exception as exc:
                log.warning("%s ohlcv failed for %s: %s", self.exchange_id, symbol.ticker, exc)
                break
            if not chunk:
                break
            for ts, o, h, l, c, v in chunk:
                if ts in seen or ts >= end_ms:
                    continue
                # a candle is only trustworthy once its window has fully elapsed
                if ts + step_ms > now_ms:
                    continue
                seen.add(ts)
                out.append(
                    Bar(symbol, datetime.fromtimestamp(ts / 1000, tz=UTC),
                        float(o), float(h), float(l), float(c), float(v), timeframe)
                )
            advanced = chunk[-1][0] + step_ms
            if advanced <= since:          # venue refused to page forward
                break
            since = advanced
        out.sort(key=lambda b: b.ts)
        return out

    async def quote(self, symbol):
        try:
            t = await self.ex.fetch_ticker(symbol.ticker)
        except Exception:
            return None
        bid, ask = t.get("bid"), t.get("ask")
        last = t.get("last") or t.get("close")
        if bid is None or ask is None:
            if last is None:
                return None
            bid, ask = last * 0.9995, last * 1.0005
        return Quote(symbol, datetime.now(UTC), float(bid), float(ask),
                     float(t.get("bidVolume") or 0), float(t.get("askVolume") or 0))

    async def close(self):
        try:
            await self.ex.close()
        except Exception:
            pass
