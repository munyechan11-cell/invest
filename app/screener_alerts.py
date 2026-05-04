"""52주 신고가/신저가, 갭상승/갭하락 자동 발견 워커.

사용자 워치/포트폴리오 종목 + 시장 인기 종목에 대해 매일 1회 체크.
"""
from __future__ import annotations
import asyncio
import logging
import time
from datetime import datetime
import httpx

log = logging.getLogger("screener")

POLL_SEC = 3600  # 1시간 주기 (빈번 X)
_seen: dict[str, set[str]] = {}   # event_type → 알림된 symbols (날짜 단위)
_today_str = ""


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _reset_daily():
    global _today_str
    today = _today()
    if today != _today_str:
        _today_str = today
        _seen.clear()
        log.info(f"screener daily reset: {today}")


async def _fetch_yahoo_52w(symbol: str) -> dict | None:
    """Yahoo에서 52주 고/저 + 현재가."""
    suffixes = (".KS", ".KQ") if symbol.isdigit() and len(symbol) == 6 else ("",)
    try:
        async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "Mozilla/5.0"}) as c:
            for s in suffixes:
                ys = symbol + s
                r = await c.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{ys}",
                    params={"range": "1y", "interval": "1d"},
                )
                if r.status_code != 200:
                    continue
                data = r.json().get("chart", {}).get("result")
                if not data:
                    continue
                meta = data[0].get("meta", {})
                indicators = (data[0].get("indicators", {}).get("quote") or [{}])[0]
                highs = [h for h in (indicators.get("high") or []) if h is not None]
                lows = [l for l in (indicators.get("low") or []) if l is not None]
                opens = [o for o in (indicators.get("open") or []) if o is not None]
                closes = [c2 for c2 in (indicators.get("close") or []) if c2 is not None]
                if not highs or not lows:
                    continue
                price = float(meta.get("regularMarketPrice") or 0)
                prev_close = float(meta.get("previousClose") or meta.get("chartPreviousClose") or 0)
                day_open = float(meta.get("regularMarketOpen") or 0)
                return {
                    "price": price,
                    "prev_close": prev_close,
                    "day_open": day_open,
                    "high_52w": max(highs),
                    "low_52w": min(lows),
                    "high_52w_ago": (datetime.now() - datetime.fromtimestamp(
                        data[0].get("timestamp", [0])[highs.index(max(highs))] if max(highs) in highs else 0
                    )).days if data[0].get("timestamp") else 0,
                }
    except Exception as e:
        log.debug(f"yahoo 52w {symbol}: {e}")
    return None


def _detect_events(d: dict) -> list[tuple[str, str]]:
    """이벤트 감지 → [(type, message)] 반환."""
    events = []
    price = d.get("price", 0)
    pc = d.get("prev_close", 0)
    do = d.get("day_open", 0)
    hi52 = d.get("high_52w", 0)
    lo52 = d.get("low_52w", 0)

    if price <= 0:
        return events

    # 52주 신고가 (현재가가 52주 고점의 99.5% 이상)
    if hi52 > 0 and price >= hi52 * 0.995:
        events.append(("high_52w",
                       f"52주 신고가 돌파! 현재가 {price:.2f} (52w 고 {hi52:.2f})"))

    # 52주 신저가
    if lo52 > 0 and price <= lo52 * 1.005:
        events.append(("low_52w",
                       f"52주 신저가! 현재가 {price:.2f} (52w 저 {lo52:.2f})"))

    # 갭상승 (시가가 전일 종가 +3% 이상)
    if pc > 0 and do > 0 and do / pc - 1 >= 0.03:
        events.append(("gap_up",
                       f"갭상승 +{(do/pc-1)*100:.1f}% (전일 {pc:.2f} → 시가 {do:.2f})"))

    # 갭하락
    if pc > 0 and do > 0 and do / pc - 1 <= -0.03:
        events.append(("gap_down",
                       f"갭하락 {(do/pc-1)*100:.1f}% (전일 {pc:.2f} → 시가 {do:.2f})"))

    return events


async def _alert_event(symbol: str, event_type: str, message: str, broadcast):
    from app import telegram_alert
    from server import db

    icon_map = {"high_52w": "🚀", "low_52w": "💥", "gap_up": "🟢⬆️", "gap_down": "🔴⬇️"}
    icon = icon_map.get(event_type, "📢")

    # WebSocket
    await broadcast({"type": "alert", "symbol": symbol, "kind": "SCREENER",
                     "message": f"{icon} {message}"})

    # Telegram (워치/포트폴리오 사용자에게만)
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

    if not user_ids:
        return

    is_kr = symbol.isdigit() and len(symbol) == 6
    cur = "₩" if is_kr else "$"
    deeplink = (f"https://tossinvest.com/stocks/A{symbol}" if is_kr
                else f"https://tossinvest.com/stocks/{symbol}.O")
    msg = (
        f"<b>{icon} 시그널 — {symbol}</b>\n\n"
        f"<b>{message}</b>\n\n"
        f"📱 <a href=\"{deeplink}\">토스에서 확인 →</a>"
    )

    for uid in user_ids:
        chat_id = await db.get_telegram_chat_id(uid)
        if chat_id and telegram_alert.is_configured():
            await telegram_alert.send(chat_id, msg)


async def worker(broadcast):
    """52주 고/저 + 갭 자동 감지 워커."""
    log.info(f"screener worker started — every {POLL_SEC}s (1h)")
    while True:
        try:
            _reset_daily()
            from server import db
            symbols = set()
            for w in await db.list_all_watch():
                symbols.add(w["symbol"])
            for p in await db.list_all_portfolio():
                symbols.add(p["symbol"])

            if not symbols:
                await asyncio.sleep(POLL_SEC)
                continue

            for sym in symbols:
                d = await _fetch_yahoo_52w(sym)
                if not d:
                    await asyncio.sleep(0.3)
                    continue
                events = _detect_events(d)
                for ev_type, msg in events:
                    seen_set = _seen.setdefault(ev_type, set())
                    if sym in seen_set:
                        continue  # 오늘 이미 알림함
                    seen_set.add(sym)
                    await _alert_event(sym, ev_type, msg, broadcast)
                await asyncio.sleep(0.3)  # rate limit
        except Exception:
            log.exception("screener worker error")

        await asyncio.sleep(POLL_SEC)
