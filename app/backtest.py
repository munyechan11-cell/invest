"""백테스트 — 과거 60~90일 데이터로 우리 시그널의 실제 승률 검증.

룰 기반 매수 조건 (analyze_rules.py와 동일 방향성):
- RSI 30~55 (저평가에서 회복 중)
- MACD 히스토그램 > 0 (모멘텀 양전환)
- 종가 > MA20 (단기 추세 위)

매수 후 N일 보유 → 종가에 청산 → 수익률 기록.
"""
from __future__ import annotations
import logging
from datetime import datetime
import httpx

log = logging.getLogger("backtest")


def _fetch_history_kr(symbol: str, days: int = 100) -> list[dict]:
    """한국주식 일봉 — KIS 우선, 실패 시 Yahoo 폴백."""
    try:
        from app.market_kr import fetch_daily_candles
        rows = fetch_daily_candles(symbol, days=days)
        if rows:
            return rows
    except Exception:
        pass
    # Yahoo 폴백
    return _yahoo_history(symbol, suffixes=(".KS", ".KQ"), days=days)


def _fetch_history_us(symbol: str, days: int = 100) -> list[dict]:
    return _yahoo_history(symbol.upper(), suffixes=("",), days=days)


def _yahoo_history(symbol: str, suffixes: tuple, days: int) -> list[dict]:
    try:
        with httpx.Client(timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as c:
            for suffix in suffixes:
                ys = symbol + suffix
                r = c.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ys}",
                          params={"range": "3mo", "interval": "1d"})
                if r.status_code != 200:
                    continue
                results = r.json().get("chart", {}).get("result")
                if not results:
                    continue
                res = results[0]
                ts = res.get("timestamp") or []
                ind = (res.get("indicators", {}).get("quote") or [{}])[0]
                closes = ind.get("close") or []
                opens = ind.get("open") or []
                highs = ind.get("high") or []
                lows = ind.get("low") or []
                vols = ind.get("volume") or []
                rows = []
                for i in range(len(ts)):
                    if i >= len(closes) or closes[i] is None:
                        continue
                    rows.append({
                        "date": datetime.fromtimestamp(ts[i]).strftime("%Y-%m-%d"),
                        "open": float(opens[i] or closes[i]),
                        "high": float(highs[i] or closes[i]),
                        "low": float(lows[i] or closes[i]),
                        "close": float(closes[i]),
                        "volume": int(vols[i] or 0),
                    })
                if rows:
                    return rows[-days:]
    except Exception as e:
        log.warning(f"yahoo history fail {symbol}: {e}")
    return []


def backtest(symbol: str, hold_days: int = 3) -> dict:
    """N일 보유 전략 백테스트."""
    import pandas as pd
    import numpy as np

    is_kr = symbol.isdigit() and len(symbol) == 6
    candles = _fetch_history_kr(symbol) if is_kr else _fetch_history_us(symbol)

    if len(candles) < 40:
        return {"ok": False, "msg": f"데이터 부족 ({len(candles)}일) — 최소 40일 필요"}

    df = pd.DataFrame(candles)
    close = df["close"].astype(float)

    # 지표 계산
    delta = close.diff()
    up = delta.clip(lower=0).rolling(14).mean()
    dn = (-delta.clip(upper=0)).rolling(14).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    macd_hist = macd - macd.ewm(span=9, adjust=False).mean()

    ma20 = close.rolling(20).mean()

    trades = []
    for i in range(30, len(close) - hold_days - 1):
        r = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50
        h = float(macd_hist.iloc[i]) if not pd.isna(macd_hist.iloc[i]) else 0
        c = float(close.iloc[i])
        m = float(ma20.iloc[i]) if not pd.isna(ma20.iloc[i]) else c

        is_buy = (30 <= r <= 55) and h > 0 and c > m
        if not is_buy:
            continue

        entry = float(close.iloc[i + 1])
        exit_ = float(close.iloc[i + 1 + hold_days])
        ret_pct = (exit_ / entry - 1) * 100
        trades.append({
            "date": candles[i + 1]["date"],
            "entry": round(entry, 2),
            "exit": round(exit_, 2),
            "ret_pct": round(ret_pct, 2),
            "win": ret_pct > 0,
        })

    if not trades:
        return {
            "ok": True, "symbol": symbol, "period_days": len(close),
            "hold_days": hold_days, "total_trades": 0,
            "msg": "백테스트 기간 내 시그널 발생 없음 (조건 충족 안 됨)",
            "trades": [],
        }

    rets = [t["ret_pct"] for t in trades]
    wins = sum(1 for r in rets if r > 0)
    avg_ret = sum(rets) / len(rets)
    std_ret = float(pd.Series(rets).std() or 1)

    # B&H (단순 보유) 비교
    bh_pct = (float(close.iloc[-1]) / float(close.iloc[30]) - 1) * 100

    return {
        "ok": True,
        "symbol": symbol,
        "period_days": len(close),
        "hold_days": hold_days,
        "total_trades": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "win_rate": round(wins / len(trades) * 100, 1),
        "avg_return_pct": round(avg_ret, 2),
        "best_pct": round(max(rets), 2),
        "worst_pct": round(min(rets), 2),
        "cumulative_pct": round(sum(rets), 2),
        "sharpe_rough": round(avg_ret / std_ret, 2),
        "buy_and_hold_pct": round(bh_pct, 2),
        "recent_trades": trades[-10:],
    }
