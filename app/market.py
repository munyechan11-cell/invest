"""시세/지표 - 실시간 가격은 Finnhub, 캔들/지표는 Alpaca."""
from __future__ import annotations
import os
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

import httpx
import pandas as pd
import numpy as np
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

FINNHUB = "https://finnhub.io/api/v1"
_alpaca: StockHistoricalDataClient | None = None


def market_of(symbol: str) -> str:
    """티커 형태로 시장 판별. 6자리 숫자=KR, 그 외=US."""
    s = (symbol or "").strip().upper()
    if s.isdigit() and len(s) == 6:
        return "KR"
    return "US"


def _alpaca_get() -> StockHistoricalDataClient:
    global _alpaca
    if _alpaca is None:
        _alpaca = StockHistoricalDataClient(
            api_key=os.environ["ALPACA_API_KEY"],
            secret_key=os.environ["ALPACA_SECRET_KEY"],
        )
    return _alpaca


@dataclass
class Quote:
    symbol: str
    price: float
    day_high: float
    day_low: float
    day_open: float
    prev_close: float
    change_pct: float
    ts: str  # 시세 타임스탬프


def _rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    up = delta.clip(lower=0).rolling(period).mean()
    down = (-delta.clip(upper=0)).rolling(period).mean()
    rs = up / down.replace(0, np.nan)
    return float((100 - 100 / (1 + rs)).iloc[-1])


def _macd(close: pd.Series) -> tuple[float, float, float]:
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    return float(macd.iloc[-1]), float(signal.iloc[-1]), float(hist.iloc[-1])


def _vwap(df: pd.DataFrame) -> float:
    pv = ((df["high"] + df["low"] + df["close"]) / 3) * df["volume"]
    return float(pv.cumsum().iloc[-1] / df["volume"].cumsum().iloc[-1])


def _bbands(close: pd.Series, period: int = 20, k: float = 2.0):
    ma = close.rolling(period).mean()
    sd = close.rolling(period).std()
    return float(ma.iloc[-1]), float((ma + k * sd).iloc[-1]), float((ma - k * sd).iloc[-1])


def fetch_realtime_quote(symbol: str) -> Quote:
    """미국주식 실시간 — KR 티커는 자동으로 KIS로 위임."""
    if market_of(symbol) == "KR":
        from .market_kr import fetch_realtime_quote as _kr
        k = _kr(symbol)
        return Quote(symbol=k.symbol, price=k.price, day_high=k.day_high,
                     day_low=k.day_low, day_open=k.day_open,
                     prev_close=k.prev_close, change_pct=k.change_pct, ts=k.ts)
    return _fetch_us_quote(symbol)


def _fetch_us_quote(symbol: str) -> Quote:
    """Finnhub 실시간 quote (c=current, h=high, l=low, o=open, pc=prev close, t=ts)."""
    key = os.environ["FINNHUB_API_KEY"]
    with httpx.Client(timeout=10) as c:
        r = c.get(f"{FINNHUB}/quote",
                  params={"symbol": symbol.upper(), "token": key})
        r.raise_for_status()
        d = r.json()
    if not d or d.get("c") in (0, None):
        raise RuntimeError(f"Finnhub quote unavailable for {symbol}")
    price = float(d["c"]); pc = float(d["pc"])
    return Quote(
        symbol=symbol.upper(),
        price=price,
        day_high=float(d["h"]),
        day_low=float(d["l"]),
        day_open=float(d["o"]),
        prev_close=pc,
        change_pct=(price / pc - 1) * 100 if pc else 0.0,
        ts=datetime.fromtimestamp(int(d.get("t", 0)), tz=timezone.utc).isoformat(),
    )


def get_snapshot(symbol: str) -> dict:
    """실시간 시세 + 지표 + 거래량 분석. 한국/미국 자동 분기."""
    if market_of(symbol) == "KR":
        from .market_kr import get_snapshot_kr
        return get_snapshot_kr(symbol)
    symbol = symbol.upper()
    quote = fetch_realtime_quote(symbol)

    cli = _alpaca_get()
    end = datetime.now(timezone.utc)

    # 분봉(최근 4일) - 일중 VWAP
    intraday = cli.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=end - timedelta(days=4), end=end,
        limit=2000,
    )).df
    if isinstance(intraday.index, pd.MultiIndex):
        intraday = intraday.xs(symbol, level=0)
    intraday5 = intraday.resample("5min").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()

    # 일봉(120일)
    daily = cli.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=end - timedelta(days=160), end=end,
    )).df
    if isinstance(daily.index, pd.MultiIndex):
        daily = daily.xs(symbol, level=0)

    today_bars = intraday5
    if len(today_bars):
        last_date = today_bars.index[-1].date()
        today_bars = today_bars[today_bars.index.date == last_date]
    vwap = _vwap(today_bars) if len(today_bars) >= 5 else float(daily["close"].iloc[-1])

    rsi14 = _rsi(daily["close"], 14)
    macd, sig, hist = _macd(daily["close"])
    ma20, bb_up, bb_dn = _bbands(daily["close"])

    today_volume = int(today_bars["volume"].sum()) if len(today_bars) else int(daily["volume"].iloc[-1])
    avg_vol_20d = float(daily["volume"].tail(20).mean())
    rel_volume = today_volume / avg_vol_20d if avg_vol_20d else 0.0

    return {
        "quote": {
            **quote.__dict__,
            "today_volume": today_volume,
            "avg_volume_20d": int(avg_vol_20d),
            "relative_volume": round(rel_volume, 2),
        },
        "indicators": {
            "rsi14": round(rsi14, 2),
            "macd": round(macd, 3),
            "macd_signal": round(sig, 3),
            "macd_hist": round(hist, 3),
            "vwap_today": round(vwap, 2),
            "ma20": round(ma20, 2),
            "bb_upper": round(bb_up, 2),
            "bb_lower": round(bb_dn, 2),
            "above_vwap": quote.price > vwap,
        },
        "recent_closes": [round(float(x), 2) for x in daily["close"].tail(10).tolist()],
    }
