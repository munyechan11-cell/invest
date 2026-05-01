"""한국 주식 무료 폴백 — Yahoo Finance (KIS 키가 없거나 실패할 때).

장점: 키 불필요, 즉시 사용 가능.
단점: 외국인/기관/개인 수급 미제공 (KIS만 가능). 시세 15분 지연 가능.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from dataclasses import dataclass

import httpx
import pandas as pd
import numpy as np

log = logging.getLogger("market_kr_yahoo")

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart"
HDR = {
    "User-Agent": "Mozilla/5.0 (compatible; Toss/1.0)",
    "Accept": "application/json",
}


@dataclass
class KRQuote:
    symbol: str
    price: float
    day_high: float
    day_low: float
    day_open: float
    prev_close: float
    change_pct: float
    volume: int
    ts: str


def _yahoo_symbol(symbol: str) -> str:
    """6자리 코드를 야후 형식으로. 코스피·코스닥 모두 .KS·.KQ 시도 — 잡히는 쪽 사용."""
    return f"{symbol}.KS"


def _try_fetch(yahoo_sym: str, range_: str = "1mo", interval: str = "1d") -> dict | None:
    try:
        with httpx.Client(timeout=10, headers=HDR) as c:
            r = c.get(f"{YAHOO}/{yahoo_sym}",
                      params={"range": range_, "interval": interval, "includePrePost": "false"})
            if r.status_code != 200:
                return None
            data = r.json()
            res = data.get("chart", {}).get("result")
            if not res:
                return None
            return res[0]
    except Exception as e:
        log.debug(f"yahoo fetch failed {yahoo_sym}: {e}")
        return None


def _resolve(symbol: str) -> tuple[str, dict]:
    """KS → KQ 순서로 시도. (성공한_심볼, 데이터)."""
    for suffix in (".KS", ".KQ"):
        ys = f"{symbol}{suffix}"
        data = _try_fetch(ys)
        if data:
            return ys, data
    raise RuntimeError(f"Yahoo Finance에서 {symbol} 종목을 찾지 못함 (코스피·코스닥 둘 다 실패)")


def fetch_realtime_quote(symbol: str) -> KRQuote:
    ys, data = _resolve(symbol)
    meta = data.get("meta", {})
    price = float(meta.get("regularMarketPrice") or 0)
    pc = float(meta.get("previousClose") or meta.get("chartPreviousClose") or 0)
    if price <= 0 or pc <= 0:
        raise RuntimeError(f"Yahoo 시세 비정상: price={price}, prev={pc}")

    return KRQuote(
        symbol=symbol,
        price=price,
        day_high=float(meta.get("regularMarketDayHigh") or price),
        day_low=float(meta.get("regularMarketDayLow") or price),
        day_open=float(meta.get("regularMarketOpen") or price),
        prev_close=pc,
        change_pct=(price / pc - 1) * 100,
        volume=int(meta.get("regularMarketVolume") or 0),
        ts=datetime.now(timezone.utc).isoformat(),
    )


def get_snapshot_kr_yahoo(symbol: str) -> dict:
    """Yahoo 기반 한국주식 스냅샷 (수급 데이터 없음)."""
    ys, daily_data = _resolve(symbol)

    # 일봉 데이터 파싱
    ts_list = daily_data.get("timestamp") or []
    indicators = daily_data.get("indicators", {})
    quote_data = (indicators.get("quote") or [{}])[0]
    closes = [c for c in (quote_data.get("close") or []) if c is not None]
    opens = [c for c in (quote_data.get("open") or []) if c is not None]
    highs = [c for c in (quote_data.get("high") or []) if c is not None]
    lows = [c for c in (quote_data.get("low") or []) if c is not None]
    volumes = [c for c in (quote_data.get("volume") or []) if c is not None]

    # 같은 길이로 자르기 (None 빠진 경우 대응)
    n = min(len(closes), len(opens), len(highs), len(lows), len(volumes))
    if n < 5:
        # 데이터 너무 적음
        meta = daily_data.get("meta", {})
        price = float(meta.get("regularMarketPrice") or 0)
        return {
            "quote": {
                "symbol": symbol, "price": price,
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
            "flow_kr": {},
            "_source": "yahoo",
        }

    df = pd.DataFrame({
        "open": opens[-n:], "high": highs[-n:], "low": lows[-n:],
        "close": closes[-n:], "volume": volumes[-n:],
    })

    quote = fetch_realtime_quote(symbol)

    close = df["close"]
    delta = close.diff()
    up = delta.clip(lower=0).rolling(14).mean()
    dn = (-delta.clip(upper=0)).rolling(14).mean()
    rs = up / dn.replace(0, np.nan)
    rsi = float((100 - 100 / (1 + rs)).iloc[-1]) if len(close) >= 15 else 50.0

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    sig = macd.ewm(span=9, adjust=False).mean()

    ma20 = close.rolling(20).mean()
    sd20 = close.rolling(20).std()
    bb_up = ma20 + 2 * sd20
    bb_dn = ma20 - 2 * sd20

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
            "vwap_today": round(quote.price, 2),
            "ma20": round(last(ma20, quote.price), 2),
            "bb_upper": round(last(bb_up, quote.price * 1.05), 2),
            "bb_lower": round(last(bb_dn, quote.price * 0.95), 2),
            "above_vwap": True,
        },
        "recent_closes": [round(float(x), 2) for x in close.tail(10).tolist()],
        "flow_kr": {},  # Yahoo는 외국인/기관 수급 미제공
        "_source": "yahoo",
    }
