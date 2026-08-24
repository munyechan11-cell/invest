"""Crypto exchanges through ccxt — 100+ venues behind one interface.

Follows freqtrade's hard-won operational rules:
  * never trust the last candle (it is still forming) — drop it
  * page backwards from `since` because most venues cap `limit`
  * load markets once and use their precision/limits for lot & tick sizing
"""
from __future__ import annotations

import contextlib
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
        #: 이 거래소가 받아 주는 페이지 크기. 거절당하면 줄이고 기억합니다 —
        #: 바이낸스는 1000, 업비트는 200 이 상한입니다.
        self._page_limit = 1000

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
        # 페이지 크기는 거래소마다 다릅니다. 바이낸스는 1000 을 받고 업비트는
        # 200 이 상한입니다. 큰 값을 박아 두면 상한이 낮은 거래소에서 **첫
        # 요청부터** 거절당하고, 아래 except 가 경고 한 줄을 남기고 break 해서
        # 봉 0개로 끝납니다 — 그 위에서 백테스트가 조용히 돕니다.
        #
        # 그래서 큰 값으로 시작해 거절당하면 반으로 줄입니다. 큰 거래소는
        # 그대로 빠르고, 작은 거래소는 두세 번 만에 자기 상한을 찾습니다.
        limit = self._page_limit
        while since < end_ms:
            try:
                chunk = await self.ex.fetch_ohlcv(symbol.ticker, timeframe,
                                                  since=since, limit=limit)
            except Exception as exc:
                if limit > 100:
                    limit //= 2
                    self._page_limit = limit
                    log.info("%s: 페이지 크기를 %d 로 줄입니다 (%s)",
                             self.exchange_id, limit, exc)
                    continue
                log.warning("%s ohlcv failed for %s: %s", self.exchange_id, symbol.ticker, exc)
                break
            if not chunk:
                break
            for ts, o, h, low, c, v in chunk:
                if ts in seen or ts >= end_ms:
                    continue
                # a candle is only trustworthy once its window has fully elapsed
                if ts + step_ms > now_ms:
                    continue
                seen.add(ts)
                out.append(
                    Bar(symbol, datetime.fromtimestamp(ts / 1000, tz=UTC),
                        float(o), float(h), float(low), float(c), float(v), timeframe)
                )
            advanced = chunk[-1][0] + step_ms
            if advanced <= since:          # venue refused to page forward
                break
            since = advanced
        out.sort(key=lambda b: b.ts)
        self._warn_on_gaps(symbol, timeframe, out, step_ms)
        return out

    def _warn_on_gaps(self, symbol, timeframe: str, bars: list[Bar],
                      step_ms: int) -> None:
        """받은 시계열에 구멍이 있으면 말합니다.

        거래소가 `since` 를 창의 시작이 아니라 **끝**으로 해석하면(업비트가
        그렇습니다) 페이지마다 창의 마지막 구간만 돌아옵니다. 그러면 봉은
        정상처럼 보이는데 사이가 몇 달씩 비어 있고, 지표는 그 건너뛴 봉들을
        연속봉으로 계산합니다 — 200일 이동평균이 실제로는 몇 년을 덮습니다.
        연율화도 같이 틀어집니다.

        고치지는 못합니다(거래소의 의미론이라서). 다만 조용히 지나가지는
        않습니다 — 구멍 난 시계열 위에서 나온 백테스트를 믿는 것이 이 종류의
        결함이 실제로 돈을 잃는 방식입니다.
        """
        if len(bars) < 3:
            return
        step = step_ms / 1000.0
        gaps = 0
        worst = 0.0
        for prev, cur in zip(bars, bars[1:]):
            delta = (cur.ts - prev.ts).total_seconds()
            # 주말·휴장은 정상입니다. 한 칸의 3배를 넘는 것만 셉니다.
            if delta > step * 3:
                gaps += 1
                worst = max(worst, delta)
        if gaps:
            log.warning(
                "%s %s %s: 봉 사이에 구멍 %d곳, 최대 %.1f일 — 거래소가 페이지를 "
                "예상과 다르게 잘라 주고 있습니다. 이 시계열 위의 지표와 "
                "연율화는 믿을 수 없습니다.",
                self.exchange_id, symbol.ticker, timeframe, gaps, worst / 86400)

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
        # 닫는 중에 터지는 것은 아무것도 바꾸지 못합니다 — 이미 끝내는 길입니다.
        with contextlib.suppress(Exception):
            await self.ex.close()
