"""시장 운영 시간 체크 — KR/US 둘 다."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


def is_weekend_kst() -> bool:
    return datetime.now(KST).weekday() >= 5  # 5=토, 6=일


def kr_market_status() -> dict:
    """한국 장 (09:00~15:30 KST)."""
    now = datetime.now(KST)
    if is_weekend_kst():
        return {"is_open": False, "label": "주말 휴장", "next_open": _next_kr_open(now)}
    h, m = now.hour, now.minute
    minutes = h * 60 + m
    OPEN = 9 * 60          # 09:00
    CLOSE = 15 * 60 + 30   # 15:30
    if minutes < OPEN:
        return {"is_open": False, "label": "장 시작 전",
                "next_open": now.replace(hour=9, minute=0, second=0, microsecond=0).isoformat()}
    if minutes >= CLOSE:
        return {"is_open": False, "label": "장 마감",
                "next_open": _next_kr_open(now)}
    return {"is_open": True, "label": "정규장", "next_open": None}


def us_market_status() -> dict:
    """미국 장 (09:30~16:00 ET = KR 23:30~06:00 보통). 단순 추정."""
    # 미국 동부시간 (서머타임 무시 — 단순화)
    now_kst = datetime.now(KST)
    et = now_kst - timedelta(hours=14)  # 대략 ET (실제 13~14시간 차)
    if et.weekday() >= 5:
        return {"is_open": False, "label": "주말 휴장"}
    h, m = et.hour, et.minute
    minutes = h * 60 + m
    OPEN = 9 * 60 + 30
    CLOSE = 16 * 60
    if OPEN <= minutes < CLOSE:
        return {"is_open": True, "label": "정규장"}
    if 4 * 60 <= minutes < OPEN:
        return {"is_open": False, "label": "프리마켓"}
    if CLOSE <= minutes < 20 * 60:
        return {"is_open": False, "label": "애프터마켓"}
    return {"is_open": False, "label": "장 마감"}


def _next_kr_open(now: datetime) -> str:
    nxt = now + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt.replace(hour=9, minute=0, second=0, microsecond=0).isoformat()


def market_status_for(symbol: str) -> dict:
    is_kr = symbol.isdigit() and len(symbol) == 6
    return kr_market_status() if is_kr else us_market_status()
