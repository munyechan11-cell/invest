"""Naver Finance — KR 종목 시세 fallback (KIS / Yahoo 둘 다 실패 시 last resort).

비공식 endpoint. 응답 schema 변경 위험 있어 try/except 광범위.
일봉 캔들은 제공 X — quote level 만. indicators 계산 어려움.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import httpx

log = logging.getLogger("market_kr_naver")

NAVER_API = "https://m.stock.naver.com/api/stock"
HDR = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "Referer": "https://m.stock.naver.com/",
    "Accept": "application/json, text/plain, */*",
}


@dataclass
class NaverQuote:
    symbol: str
    price: float
    day_high: float
    day_low: float
    day_open: float
    prev_close: float
    change_pct: float
    volume: int
    ts: str


def _to_num(s, default: float = 0.0) -> float:
    if s is None:
        return default
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return float(str(s).replace(",", "").strip() or default)
    except (ValueError, TypeError):
        return default


def fetch_naver_quote(code: str) -> NaverQuote | None:
    """Naver Finance에서 KR 6자리 종목 시세 조회. 실패 시 None.

    Yahoo가 차단된 환경에서 Yahoo와 독립된 한국 source로 last resort.
    """
    code = (code or "").strip()
    if not (code.isdigit() and len(code) == 6):
        return None

    try:
        with httpx.Client(timeout=8, headers=HDR) as c:
            r = c.get(f"{NAVER_API}/{code}/basic")
            if r.status_code != 200:
                log.info(f"naver basic HTTP {r.status_code} for {code}")
                return None
            d = r.json()
    except Exception as e:
        log.info(f"naver fetch error {code}: {e}")
        return None

    try:
        price = _to_num(d.get("closePrice"))
        if price <= 0:
            return None

        diff = _to_num(d.get("compareToPreviousClosePrice"))
        ratio = _to_num(d.get("fluctuationsRatio"))

        # 등락 부호 — code 값으로 판별 (3/4/5 = 하락 계열)
        cmp_obj = d.get("compareToPreviousPrice") or {}
        is_down = str(cmp_obj.get("code", "")).strip() in ("3", "4", "5")
        if is_down:
            diff = -abs(diff)
            ratio = -abs(ratio)

        prev_close = price - diff if diff else price
        # 분기별 응답 스키마 다른 경우 대비 — 다양한 키 fallback
        day_open = _to_num(d.get("openPrice") or d.get("marketValue"))
        day_high = _to_num(d.get("highPrice"))
        day_low = _to_num(d.get("lowPrice"))
        volume = int(_to_num(d.get("accumulatedTradingVolume") or d.get("totalTradingVolume")))

        if day_open <= 0:
            day_open = price
        if day_high <= 0:
            day_high = price
        if day_low <= 0:
            day_low = price

        return NaverQuote(
            symbol=code,
            price=price,
            day_high=day_high,
            day_low=day_low,
            day_open=day_open,
            prev_close=prev_close if prev_close > 0 else price,
            change_pct=ratio,
            volume=volume,
            ts=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as e:
        log.warning(f"naver parse error {code}: {e}")
        return None


def get_snapshot_kr_naver(code: str) -> dict | None:
    """Quote level snapshot — indicators 없는 최소 형태.

    Yahoo / KIS 모두 fail 한 last resort. analyze()가 빈 indicators 에도
    돌아가도록 graceful degradation.
    """
    q = fetch_naver_quote(code)
    if q is None:
        return None
    return {
        "quote": {
            "symbol": q.symbol, "price": q.price,
            "day_high": q.day_high, "day_low": q.day_low,
            "day_open": q.day_open, "prev_close": q.prev_close,
            "change_pct": q.change_pct, "volume": q.volume, "ts": q.ts,
            "today_volume": q.volume, "avg_volume_20d": 0, "relative_volume": 0.0,
        },
        "indicators": {},
        "recent_closes": [q.price],  # 최소 한 점이라도 — analyze() 가 빈 list 대비
        "flow_kr": {},
        "_source": "naver",
        "_partial": True,  # 부분 데이터 — 호출자에게 주의 신호
    }
