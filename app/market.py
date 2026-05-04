"""시세/지표 - 실시간 가격은 Finnhub, 캔들/지표는 Alpaca. 폴백: Yahoo Finance."""
from __future__ import annotations
import os
import logging
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

import httpx
import pandas as pd
import numpy as np
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

log = logging.getLogger("market")
FINNHUB = "https://finnhub.io/api/v1"
_alpaca: StockHistoricalDataClient | None = None


def market_of(symbol: str) -> str:
    """티커 형태로 시장 판별. 6자리 숫자=KR, 그 외=US."""
    s = (symbol or "").strip().upper()
    if s.isdigit() and len(s) == 6:
        return "KR"
    return "US"


def _alpaca_get() -> StockHistoricalDataClient:
    """Alpaca 클라이언트. 키 없으면 RuntimeError → 호출자가 Yahoo로 폴백."""
    global _alpaca
    if _alpaca is None:
        ak = os.environ.get("ALPACA_API_KEY", "").strip()
        sk = os.environ.get("ALPACA_SECRET_KEY", "").strip()
        if not ak or not sk:
            raise RuntimeError("ALPACA_API_KEY/ALPACA_SECRET_KEY 미설정")
        _alpaca = StockHistoricalDataClient(api_key=ak, secret_key=sk)
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
    """실시간 시세 — 시장별 다중 소스 폴백.

    KR: KIS → Yahoo (.KS/.KQ)
    US: Finnhub → Yahoo
    """
    if market_of(symbol) == "KR":
        try:
            from .market_kr import fetch_realtime_quote as _kr
            k = _kr(symbol)
        except Exception:
            from .market_kr_yahoo import fetch_realtime_quote as _yh
            k = _yh(symbol)
        return Quote(symbol=k.symbol, price=k.price, day_high=k.day_high,
                     day_low=k.day_low, day_open=k.day_open,
                     prev_close=k.prev_close, change_pct=k.change_pct, ts=k.ts)
    # US: Finnhub 시도 → 실패 시 Yahoo로 폴백
    try:
        return _fetch_us_quote(symbol)
    except Exception as e:
        log.warning(f"Finnhub 실패 ({symbol}): {e} → Yahoo Finance로 폴백")
        from .market_us_yahoo import fetch_realtime_quote as _yh
        u = _yh(symbol)
        return Quote(symbol=u.symbol, price=u.price, day_high=u.day_high,
                     day_low=u.day_low, day_open=u.day_open,
                     prev_close=u.prev_close, change_pct=u.change_pct, ts=u.ts)


def _fetch_us_quote(symbol: str) -> Quote:
    """Finnhub 실시간 quote (c=current, h=high, l=low, o=open, pc=prev close, t=ts).

    FINNHUB_API_KEY 미설정 시 RuntimeError → 호출자가 Yahoo로 폴백.
    """
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not key:
        raise RuntimeError("FINNHUB_API_KEY 미설정")
    with httpx.Client(timeout=10) as c:
        r = c.get(f"{FINNHUB}/quote",
                  params={"symbol": symbol.upper(), "token": key})
        r.raise_for_status()
        d = r.json()
    if not d or d.get("c") in (0, None):
        # 정규장 시작 직후 등 0 응답 시 prev_close로 폴백 (사용자 에러 방지)
        pc = float(d.get("pc") or 0)
        if pc > 0:
            return Quote(
                symbol=symbol.upper(), price=pc,
                day_high=pc, day_low=pc, day_open=pc,
                prev_close=pc, change_pct=0.0,
                ts=datetime.now(timezone.utc).isoformat(),
            )
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
    """실시간 시세 + 지표 + 거래량 분석. 한국/미국 자동 분기.

    US: Finnhub+Alpaca 시도 → 키 없거나 실패 시 Yahoo Finance로 폴백.
    KR: market_kr_yahoo 폴백 내장.
    """
    if market_of(symbol) == "KR":
        from .market_kr import get_snapshot_kr
        return get_snapshot_kr(symbol)
    symbol = symbol.upper()
    try:
        return _get_snapshot_us_finnhub_alpaca(symbol)
    except Exception as e:
        log.warning(f"Finnhub/Alpaca 실패 ({symbol}): {e} → Yahoo Finance로 폴백")
        from .market_us_yahoo import get_snapshot_us_yahoo
        return get_snapshot_us_yahoo(symbol)


def _get_snapshot_us_finnhub_alpaca(symbol: str) -> dict:
    """Finnhub(시세) + Alpaca(캔들) 기반 풀 스냅샷. 둘 다 키 필요."""
    quote = fetch_realtime_quote(symbol)

    cli = _alpaca_get()
    end = datetime.now(timezone.utc)

    # 분봉(최근 4일) - 일중 VWAP
    res = cli.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=end - timedelta(days=4), end=end,
        limit=2000,
        feed='iex',
    ))
    if not res.data or symbol not in res.data or len(res.data[symbol]) == 0:
        intraday5 = pd.DataFrame()
    else:
        intraday = res.df
        if isinstance(intraday.index, pd.MultiIndex):
            intraday = intraday.xs(symbol, level=0)
        intraday5 = intraday.resample("5min").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna()

    # 일봉(120일)
    res_daily = cli.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=end - timedelta(days=160), end=end,
        feed='iex',
    ))
    if not res_daily.data or symbol not in res_daily.data or len(res_daily.data[symbol]) == 0:
        raise RuntimeError(f"Daily data unavailable for {symbol}")
    
    daily = res_daily.df
    if isinstance(daily.index, pd.MultiIndex):
        daily = daily.xs(symbol, level=0)

    today_bars = intraday5
    if len(today_bars):
        last_date = today_bars.index[-1].date()
        today_bars = today_bars[today_bars.index.date == last_date]
    
    vwap = _vwap(today_bars) if len(today_bars) >= 5 else float(daily["close"].iloc[-1])

    rsi14 = _rsi(daily["close"], 14) if len(daily) >= 14 else 50.0
    macd, sig, hist = _macd(daily["close"]) if len(daily) >= 26 else (0, 0, 0)
    ma20, bb_up, bb_dn = _bbands(daily["close"]) if len(daily) >= 20 else (float(daily["close"].iloc[-1]), 0, 0)

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
