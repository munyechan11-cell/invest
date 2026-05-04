"""기술 지표 공통 모듈 — RSI/MACD/Bollinger 일원화.

기존: market.py / market_kr.py / market_kr_yahoo.py / backtest.py 각각 따로 구현 (DRY 위반)
이제: 모두 이 모듈 사용.
"""
from __future__ import annotations


def rsi(close, period: int = 14) -> float:
    """Wilder's RSI. close: pd.Series."""
    import numpy as np
    delta = close.diff()
    up = delta.clip(lower=0).rolling(period).mean()
    dn = (-delta.clip(upper=0)).rolling(period).mean()
    rs = up / dn.replace(0, np.nan)
    return float((100 - 100 / (1 + rs)).iloc[-1])


def macd(close) -> tuple[float, float, float]:
    """MACD line / signal / histogram. close: pd.Series. Returns (macd, signal, hist)."""
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    line = ema12 - ema26
    sig = line.ewm(span=9, adjust=False).mean()
    return float(line.iloc[-1]), float(sig.iloc[-1]), float((line - sig).iloc[-1])


def bbands(close, period: int = 20, k: float = 2.0) -> tuple[float, float, float]:
    """Bollinger Bands (MA, upper, lower). close: pd.Series."""
    ma = close.rolling(period).mean()
    sd = close.rolling(period).std()
    return (
        float(ma.iloc[-1]),
        float((ma + k * sd).iloc[-1]),
        float((ma - k * sd).iloc[-1]),
    )


def vwap_session(df) -> float:
    """오늘 세션 VWAP. df: open/high/low/close/volume DataFrame."""
    pv = ((df["high"] + df["low"] + df["close"]) / 3) * df["volume"]
    return float(pv.cumsum().iloc[-1] / df["volume"].cumsum().iloc[-1])


def is_kr_symbol(symbol: str) -> bool:
    """KR 종목 판별 — 6자리 숫자."""
    return bool(symbol) and symbol.isdigit() and len(symbol) == 6
