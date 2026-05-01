"""한국 시장 — 한국투자증권(KIS) Open API + Yahoo Finance 폴백.

KIS 우선 시도 (외국인/기관/개인 수급 + 실시간 시세 제공).
KIS 키 미설정/실패 시 Yahoo Finance로 자동 폴백 — 사이트 절대 안 죽음.
"""
from __future__ import annotations
import os, time, logging
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

import httpx

log = logging.getLogger("market_kr")

_KIS_REAL = "https://openapi.koreainvestment.com:9443"
_KIS_MOCK = "https://openapivts.koreainvestment.com:29443"
_token: dict = {"value": None, "exp": 0}


def _base() -> str:
    return _KIS_MOCK if os.environ.get("KIS_PAPER", "true").lower() == "true" else _KIS_REAL


def _token_get() -> str:
    app_key = os.environ.get("KIS_APP_KEY")
    app_secret = os.environ.get("KIS_APP_SECRET")
    if not app_key or not app_secret:
        raise RuntimeError("한국 주식 분석을 위해 KIS_APP_KEY와 KIS_APP_SECRET 환경변수 설정이 필요합니다.")
        
    if _token["value"] and _token["exp"] > time.time() + 60:
        return _token["value"]
    r = httpx.post(f"{_base()}/oauth2/tokenP", json={
        "grant_type": "client_credentials",
        "appkey": app_key,
        "appsecret": app_secret,
    }, timeout=15)
    r.raise_for_status()
    d = r.json()
    _token["value"] = d["access_token"]
    _token["exp"] = time.time() + int(d.get("expires_in", 21600))
    return _token["value"]


def _headers(tr_id: str) -> dict:
    return {
        "authorization": f"Bearer {_token_get()}",
        "appkey": os.environ.get("KIS_APP_KEY", ""),
        "appsecret": os.environ.get("KIS_APP_SECRET", ""),
        "tr_id": tr_id,
        "content-type": "application/json; charset=utf-8",
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


def fetch_realtime_quote(symbol: str) -> KRQuote:
    """실시간 현재가."""
    r = httpx.get(
        f"{_base()}/uapi/domestic-stock/v1/quotations/inquire-price",
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        headers=_headers("FHKST01010100"),
        timeout=10,
    )
    r.raise_for_status()
    d = r.json()
    if d.get("rt_cd") != "0":
        raise RuntimeError(f"KIS quote err: {d.get('msg1')}")
    o = d["output"]
    price = float(o["stck_prpr"])
    pc = float(o["stck_sdpr"])
    return KRQuote(
        symbol=symbol,
        price=price,
        day_high=float(o["stck_hgpr"]),
        day_low=float(o["stck_lwpr"]),
        day_open=float(o["stck_oprc"]),
        prev_close=pc,
        change_pct=(price / pc - 1) * 100 if pc else 0.0,
        volume=int(o["acml_vol"]),
        ts=datetime.now(timezone.utc).isoformat(),
    )


def fetch_investor_flow(symbol: str) -> dict:
    """투자자별 매매동향 — 한국시장 고유: 외국인/기관/개인 순매수."""
    r = httpx.get(
        f"{_base()}/uapi/domestic-stock/v1/quotations/inquire-investor",
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        headers=_headers("FHKST01010900"),
        timeout=10,
    )
    if r.status_code != 200:
        return {}
    d = r.json()
    rows = d.get("output", []) or []
    if not rows:
        return {}
    today = rows[0]
    return {
        "date": today.get("stck_bsop_date"),
        "foreign_net_qty": int(today.get("frgn_ntby_qty", 0) or 0),
        "institutional_net_qty": int(today.get("orgn_ntby_qty", 0) or 0),
        "retail_net_qty": int(today.get("prsn_ntby_qty", 0) or 0),
    }


def fetch_daily_candles(symbol: str, days: int = 100) -> list[dict]:
    """일봉 — RSI/MACD 등 지표 계산용."""
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
    r = httpx.get(
        f"{_base()}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        params={
            "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": start, "FID_INPUT_DATE_2": end,
            "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "1",
        },
        headers=_headers("FHKST03010100"),
        timeout=15,
    )
    if r.status_code != 200:
        return []
    d = r.json()
    rows = d.get("output2", []) or []
    return [{
        "date": x.get("stck_bsop_date"),
        "open": float(x.get("stck_oprc", 0)),
        "high": float(x.get("stck_hgpr", 0)),
        "low": float(x.get("stck_lwpr", 0)),
        "close": float(x.get("stck_clpr", 0)),
        "volume": int(x.get("acml_vol", 0)),
    } for x in rows if x.get("stck_clpr")][::-1][-days:]


def get_snapshot_kr(symbol: str) -> dict:
    """한국주식 스냅샷. KIS 우선 → 실패 시 Yahoo Finance로 자동 폴백."""
    import pandas as pd, numpy as np

    # ── KIS 시도
    try:
        quote = fetch_realtime_quote(symbol)
        candles = fetch_daily_candles(symbol)
        flow = fetch_investor_flow(symbol)
    except Exception as e:
        log.warning(f"KIS 실패 ({symbol}): {e} → Yahoo Finance로 폴백")
        from .market_kr_yahoo import get_snapshot_kr_yahoo
        return get_snapshot_kr_yahoo(symbol)

    if len(candles) < 30:
        # 지표 계산 불가 — 기본값
        return {
            "quote": {**quote.__dict__, "today_volume": quote.volume,
                      "avg_volume_20d": 0, "relative_volume": 0.0},
            "indicators": {},
            "recent_closes": [c["close"] for c in candles[-10:]],
            "flow_kr": flow,
        }

    df = pd.DataFrame(candles)
    close = df["close"]
    delta = close.diff()
    up = delta.clip(lower=0).rolling(14).mean()
    dn = (-delta.clip(upper=0)).rolling(14).mean()
    rs = up / dn.replace(0, np.nan)
    rsi = float((100 - 100 / (1 + rs)).iloc[-1])

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    sig = macd.ewm(span=9, adjust=False).mean()

    ma20 = close.rolling(20).mean()
    sd20 = close.rolling(20).std()
    bb_up = ma20 + 2 * sd20
    bb_dn = ma20 - 2 * sd20

    avg_vol = float(df["volume"].tail(20).mean())
    rv = quote.volume / avg_vol if avg_vol else 0.0

    return {
        "quote": {**quote.__dict__,
                  "today_volume": quote.volume,
                  "avg_volume_20d": int(avg_vol),
                  "relative_volume": round(rv, 2)},
        "indicators": {
            "rsi14": round(rsi, 2),
            "macd": round(float(macd.iloc[-1]), 3),
            "macd_signal": round(float(sig.iloc[-1]), 3),
            "macd_hist": round(float((macd - sig).iloc[-1]), 3),
            "vwap_today": round(quote.price, 2),  # 일봉 기반엔 VWAP 의미 약함
            "ma20": round(float(ma20.iloc[-1]), 2),
            "bb_upper": round(float(bb_up.iloc[-1]), 2),
            "bb_lower": round(float(bb_dn.iloc[-1]), 2),
            "above_vwap": True,
        },
        "recent_closes": [round(float(x), 2) for x in close.tail(10).tolist()],
        "flow_kr": flow,
    }
