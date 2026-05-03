"""DART 공시 자동 모니터링 — 한국주식 필수.

워치리스트/포트폴리오의 KR 종목들에 대해 10분마다 새 공시 체크 → 텔레그램 알림.

DART OpenAPI: https://opendart.fss.or.kr
"""
from __future__ import annotations
import asyncio
import logging
import os
import time
import httpx

log = logging.getLogger("dart_watcher")

POLL_SEC = 600   # 10분마다 체크 (DART API 한도 보호)
_seen: dict[str, set[str]] = {}   # symbol → 알림 발송된 rcept_no 집합


def _is_kr(symbol: str) -> bool:
    return symbol.isdigit() and len(symbol) == 6


async def _fetch_dart_filings(symbol: str, days: int = 1) -> list[dict]:
    """최근 N일 공시 조회 (DART OpenAPI)."""
    key = os.environ.get("DART_API_KEY")
    if not key:
        return []
    from datetime import datetime, timedelta
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://opendart.fss.or.kr/api/list.json", params={
                "crtfc_key": key, "stock_code": symbol,
                "bgn_de": start, "end_de": end,
                "page_count": 20,
            })
            if r.status_code != 200:
                return []
            data = r.json()
            if data.get("status") != "000":
                return []
            return data.get("list") or []
    except Exception as e:
        log.warning(f"DART {symbol}: {e}")
        return []


def _classify_filing(report_name: str) -> tuple[str, str]:
    """공시명 → (중요도 이모지, 카테고리)."""
    name = report_name or ""
    # 호재성
    if any(k in name for k in ("실적", "영업", "이익", "수주", "계약")):
        return "🟢", "실적/수주"
    if any(k in name for k in ("자기주식취득", "취득결정", "투자")):
        return "🟢", "자사주/투자"
    if any(k in name for k in ("배당", "주주환원")):
        return "🟢", "배당/주주환원"
    # 악재성
    if any(k in name for k in ("매도", "처분", "감자", "자본감소")):
        return "🔴", "매도/감자"
    if any(k in name for k in ("소송", "제재", "벌금")):
        return "🔴", "법률리스크"
    if any(k in name for k in ("정정", "철회")):
        return "⚠️", "정정공시"
    # 중립
    if any(k in name for k in ("증자", "전환사채", "신주인수권")):
        return "🟡", "증자/CB"
    if "지분" in name or "변동" in name:
        return "🟡", "지분변동"
    return "📄", "기타"


async def _alert_filing(symbol: str, name: str, filing: dict, broadcast):
    from app import telegram_alert
    from server import db

    icon, category = _classify_filing(filing.get("report_nm", ""))
    rcept_no = filing.get("rcept_no", "")
    rcept_dt = filing.get("rcept_dt", "")
    submitter = filing.get("flr_nm", "")
    url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

    msg = (
        f"<b>{icon} 신규 공시 — {name} ({symbol})</b>\n"
        f"\n"
        f"📋 <b>{filing.get('report_nm', '제목 없음')}</b>\n"
        f"🏷 카테고리: {category}\n"
        f"📅 접수: {rcept_dt}\n"
        f"🏢 제출: {submitter}\n"
        f"\n"
        f"🔗 <a href=\"{url}\">DART에서 원본 보기</a>"
    )

    # WebSocket 알림 (브라우저)
    await broadcast({
        "type": "alert", "symbol": symbol, "kind": "DART",
        "message": f"{icon} 공시: {filing.get('report_nm', '')[:50]} · {category}",
    })

    # 텔레그램: 워치/포트폴리오에 이 종목 가진 모든 사용자
    user_ids = set()
    try:
        for w in await db.list_all_watch():
            if w["symbol"] == symbol:
                user_ids.add(w["user_id"])
        for p in await db.list_all_portfolio():
            if p["symbol"] == symbol:
                user_ids.add(p["user_id"])
    except Exception:
        return

    for uid in user_ids:
        chat_id = await db.get_telegram_chat_id(uid)
        if chat_id and telegram_alert.is_configured():
            await telegram_alert.send(chat_id, msg)


async def worker(broadcast):
    """DART 공시 자동 폴링 워커."""
    log.info(f"DART watcher started — polling every {POLL_SEC}s")
    if not os.environ.get("DART_API_KEY"):
        log.info("DART_API_KEY 미설정 — 워커 비활성")
        return

    from server import db

    # 첫 실행 시 지금 시점까지의 공시는 '본 것'으로 간주 (스팸 방지)
    bootstrap = True

    while True:
        try:
            # 워치리스트 + 포트폴리오의 KR 종목들 수집
            symbols: dict[str, str] = {}  # symbol → name (best effort)
            for w in await db.list_all_watch():
                if _is_kr(w["symbol"]):
                    symbols[w["symbol"]] = w["symbol"]
            for p in await db.list_all_portfolio():
                if _is_kr(p["symbol"]):
                    symbols[p["symbol"]] = p["symbol"]

            if not symbols:
                await asyncio.sleep(POLL_SEC)
                continue

            for sym, name in symbols.items():
                seen = _seen.setdefault(sym, set())
                filings = await _fetch_dart_filings(sym, days=1)
                # 종목명 보강 (첫 공시에서)
                if filings and filings[0].get("corp_name"):
                    name = filings[0]["corp_name"]

                for f in filings:
                    rno = f.get("rcept_no", "")
                    if not rno or rno in seen:
                        continue
                    seen.add(rno)
                    if not bootstrap:
                        await _alert_filing(sym, name, f, broadcast)
                # rate limit cushion
                await asyncio.sleep(0.3)

            bootstrap = False
        except Exception:
            log.exception("dart watcher error")

        await asyncio.sleep(POLL_SEC)
