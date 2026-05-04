"""미국주식 무료 폴백 — Yahoo Finance (Finnhub/Alpaca 키 없거나 실패할 때).

장점: 키 불필요, 즉시 사용 가능. 시세·일봉·주요 지표 모두 가능.
단점: 실시간성 약간 지연 (~15분), pre/post market 데이터 없음.

market.py에서 Finnhub 또는 Alpaca 호출 실패 시 자동 폴백됨.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from dataclasses import dataclass

import httpx
import pandas as pd
import numpy as np

log = logging.getLogger("market_us_yahoo")

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart"
HDR = {
    "User-Agent": "Mozilla/5.0 (compatible; Sift/1.0)",
    "Accept": "application/json",
}


@dataclass
class USQuote:
    symbol: str
    price: float
    day_high: float
    day_low: float
    day_open: float
    prev_close: float
    change_pct: float
    volume: int
    ts: str


def _fetch(symbol: str, range_: str = "6mo", interval: str = "1d") -> dict:
    """Yahoo chart API 호출. 실패 시 RuntimeError."""
    try:
        with httpx.Client(timeout=10, headers=HDR) as c:
            r = c.get(
                f"{YAHOO}/{symbol.upper()}",
                params={"range": range_, "interval": interval, "includePrePost": "false"},
            )
            if r.status_code != 200:
                raise RuntimeError(f"Yahoo HTTP {r.status_code} for {symbol}")
            data = r.json()
        res = data.get("chart", {}).get("result")
        if not res:
            err = data.get("chart", {}).get("error") or {}
            raise RuntimeError(f"Yahoo no result for {symbol}: {err.get('description', '')}")
        return res[0]
    except httpx.HTTPError as e:
        raise RuntimeError(f"Yahoo network error for {symbol}: {e}")


def fetch_realtime_quote(symbol: str) -> USQuote:
    """실시간 시세 — 일봉 endpoint의 meta가 가장 정확."""
    data = _fetch(symbol, range_="1d", interval="1m")
    meta = data.get("meta", {})
    price = float(meta.get("regularMarketPrice") or 0)
    pc = float(meta.get("previousClose") or meta.get("chartPreviousClose") or 0)
    if price <= 0:
        # 장 시작 직후 등 — pc로 폴백
        price = pc
    if price <= 0 or pc <= 0:
        raise RuntimeError(f"Yahoo 시세 비정상 ({symbol}): price={price}, prev={pc}")

    return USQuote(
        symbol=symbol.upper(),
        price=price,
        day_high=float(meta.get("regularMarketDayHigh") or price),
        day_low=float(meta.get("regularMarketDayLow") or price),
        day_open=float(meta.get("regularMarketOpen") or price),
        prev_close=pc,
        change_pct=(price / pc - 1) * 100 if pc else 0.0,
        volume=int(meta.get("regularMarketVolume") or 0),
        ts=datetime.now(timezone.utc).isoformat(),
    )


def get_snapshot_us_yahoo(symbol: str) -> dict:
    """Yahoo 기반 미국주식 스냅샷. 일봉 + 지표 + 거래량 분석."""
    daily_data = _fetch(symbol, range_="6mo", interval="1d")

    # 일봉 파싱
    indicators_data = daily_data.get("indicators", {})
    quote_data = (indicators_data.get("quote") or [{}])[0]
    closes = [c for c in (quote_data.get("close") or []) if c is not None]
    opens = [c for c in (quote_data.get("open") or []) if c is not None]
    highs = [c for c in (quote_data.get("high") or []) if c is not None]
    lows = [c for c in (quote_data.get("low") or []) if c is not None]
    volumes = [c for c in (quote_data.get("volume") or []) if c is not None]

    n = min(len(closes), len(opens), len(highs), len(lows), len(volumes))
    if n < 5:
        meta = daily_data.get("meta", {})
        price = float(meta.get("regularMarketPrice") or meta.get("previousClose") or 0)
        if price <= 0:
            raise RuntimeError(f"Yahoo {symbol}: 데이터 너무 적고 시세도 없음")
        return {
            "quote": {
                "symbol": symbol.upper(), "price": price,
                "day_high": float(meta.get("regularMarketDayHigh") or price),
                "day_low": float(meta.get("regularMarketDayLow") or price),
                "day_open": float(meta.get("regularMarketOpen") or price),
                "prev_close": float(meta.get("previousClose") or price),
                "change_pct": 0.0,
                "volume": int(meta.get("regularMarketVolume") or 0),
                "ts": datetime.now(timezone.utc).isoformat(),
                "today_volume": int(meta.get("regularMarketVolume") or 0),
                "avg_volume_20d": 0,
                "relative_volume": 0.0,
            },
            "indicators": {},
            "recent_closes": [round(c, 2) for c in closes[-10:]],
            "_source": "yahoo",
        }

    df = pd.DataFrame({
        "open": opens[-n:], "high": highs[-n:], "low": lows[-n:],
        "close": closes[-n:], "volume": volumes[-n:],
    })

    quote = fetch_realtime_quote(symbol)
    close = df["close"]

    # RSI
    delta = close.diff()
    up = delta.clip(lower=0).rolling(14).mean()
    dn = (-delta.clip(upper=0)).rolling(14).mean()
    rs = up / dn.replace(0, np.nan)
    rsi = float((100 - 100 / (1 + rs)).iloc[-1]) if len(close) >= 15 else 50.0

    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    sig = macd.ewm(span=9, adjust=False).mean()

    # Bollinger
    ma20 = close.rolling(20).mean()
    sd20 = close.rolling(20).std()
    bb_up = ma20 + 2 * sd20
    bb_dn = ma20 - 2 * sd20

    # 거래량
    avg_vol = float(df["volume"].tail(20).mean()) if len(df) >= 20 else float(df["volume"].mean())
    rv = quote.volume / avg_vol if avg_vol else 0.0

    last = lambda s, default: float(s.iloc[-1]) if not pd.isna(s.iloc[-1]) else default

    return {
        "quote": {
            **quote.__dict__,
            "today_volume": quote.volume,
            "avg_volume_20d": int(avg_vol or 0),
            "relative_volume": round(rv, 2),
        },
        "indicators": {
            "rsi14": round(rsi, 2) if not pd.isna(rsi) else 50.0,
            "macd": round(last(macd, 0.0), 3),
            "macd_signal": round(last(sig, 0.0), 3),
            "macd_hist": round(last(macd - sig, 0.0), 3),
            "vwap_today": round(quote.price, 2),  # Yahoo는 일중 분봉 따로 안 받아 quote로 근사
            "ma20": round(last(ma20, quote.price), 2),
            "bb_upper": round(last(bb_up, quote.price * 1.05), 2),
            "bb_lower": round(last(bb_dn, quote.price * 0.95), 2),
            "above_vwap": True,
        },
        "recent_closes": [round(float(x), 2) for x in close.tail(10).tolist()],
        "_source": "yahoo",
    }
