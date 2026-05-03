"""주요 시장 지수 — KOSPI/KOSDAQ/S&P500/NASDAQ. Bloomberg-style ticker bar용."""
from __future__ import annotations
import asyncio
import logging
import time
import httpx

log = logging.getLogger("indices")

INDICES = [
    ("^KS11", "KOSPI", "KR"),
    ("^KQ11", "KOSDAQ", "KR"),
    ("^GSPC", "S&P 500", "US"),
    ("^IXIC", "NASDAQ", "US"),
    ("^DJI", "Dow Jones", "US"),
]

_cache: dict = {"value": None, "ts": 0}
_TTL = 30  # 30초 캐시 (지수는 자주 안 변함)


async def _fetch_one(symbol: str, name: str, market: str) -> dict | None:
    try:
        async with httpx.AsyncClient(
            timeout=5, headers={"User-Agent": "Mozilla/5.0"}
        ) as c:
            r = await c.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={"range": "1d", "interval": "5m"},
            )
            if r.status_code != 200:
                return None
            data = r.json().get("chart", {}).get("result")
            if not data:
                return None
            meta = data[0].get("meta", {})
            price = float(meta.get("regularMarketPrice") or 0)
            prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0)
            if price <= 0 or prev <= 0:
                return None
            change = price - prev
            change_pct = (price / prev - 1) * 100
            return {
                "symbol": symbol,
                "name": name,
                "market": market,
                "price": round(price, 2),
                "change": round(change, 2),
                "change_pct": round(change_pct, 2),
                "is_open": meta.get("instrumentType") == "INDEX"
                          and meta.get("regularMarketTime", 0) > 0,
            }
    except Exception as e:
        log.debug(f"index {symbol} fail: {e}")
        return None


async def fetch_indices() -> list[dict]:
    """모든 주요 지수 병렬 fetch — 30초 캐시."""
    now = time.time()
    if _cache["value"] and now - _cache["ts"] < _TTL:
        return _cache["value"]

    results = await asyncio.gather(*[_fetch_one(s, n, m) for s, n, m in INDICES])
    valid = [r for r in results if r]
    _cache["value"] = valid
    _cache["ts"] = now
    return valid
