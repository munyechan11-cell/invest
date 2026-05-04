"""Symbol → 종목명 변환 (알림에서 티커 + 한글명 함께 표시).

ocr_portfolio.py 의 KR_NAME_TO_CODE / KR_TO_US_TICKER 사전 역방향 사용.
사전에 없으면 Yahoo Finance longName/shortName 으로 fallback (24h 캐시).
"""
from __future__ import annotations
import time
import logging
import httpx

from app.ocr_portfolio import KR_NAME_TO_CODE, KR_TO_US_TICKER

log = logging.getLogger("symbol_names")

_CODE_TO_NAME: dict[str, str] = {}
for _name, _code in KR_NAME_TO_CODE.items():
    _CODE_TO_NAME.setdefault(_code, _name)
for _name, _ticker in KR_TO_US_TICKER.items():
    _CODE_TO_NAME.setdefault(_ticker, _name)

_YAHOO_CACHE: dict[str, tuple[str, float]] = {}
_TTL = 24 * 3600


def _fetch_yahoo_name(symbol: str) -> str:
    sym = (symbol or "").strip()
    if not sym:
        return ""
    is_kr = sym.isdigit() and len(sym) == 6
    candidates = [f"{sym}.KS", f"{sym}.KQ"] if is_kr else [sym.upper()]
    for ys in candidates:
        try:
            with httpx.Client(timeout=5, headers={"User-Agent": "Mozilla/5.0"}) as c:
                r = c.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{ys}",
                    params={"range": "1d", "interval": "1d"},
                )
            if r.status_code != 200:
                continue
            data = r.json()
            res = data.get("chart", {}).get("result")
            if not res:
                continue
            meta = res[0].get("meta", {})
            name = (meta.get("longName") or meta.get("shortName") or "").strip()
            if name:
                return name
        except Exception as e:
            log.debug(f"yahoo name fetch {ys} fail: {e}")
            continue
    return ""


def get_name(symbol: str) -> str:
    """symbol → 한글명 우선, 없으면 영문, 없으면 빈 문자열."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return ""
    if sym in _CODE_TO_NAME:
        return _CODE_TO_NAME[sym]
    now = time.time()
    cached = _YAHOO_CACHE.get(sym)
    if cached and cached[1] > now:
        return cached[0]
    name = _fetch_yahoo_name(sym)
    _YAHOO_CACHE[sym] = (name, now + _TTL)
    return name


def label(symbol: str) -> str:
    """알림용 라벨 — '종목명 (티커)' 또는 'symbol' (이름 못 찾으면)."""
    sym = (symbol or "").strip().upper()
    name = get_name(sym)
    if name and name.upper() != sym:
        return f"{name} ({sym})"
    return sym
