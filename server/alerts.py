"""실시간 가격 폴링 + 알림 트리거."""
from __future__ import annotations
import asyncio, time, logging
from app.market import fetch_realtime_quote, market_of
from server import db
from server.sizing import shares_for, split_plan

log = logging.getLogger("alerts")

POLL_SEC = 6           # 워치리스트당 폴링 주기
COOLDOWN_SEC = 300     # 같은 알림 재발송 방지
_last_sent: dict[tuple[str, str], float] = {}


def _fmt(symbol: str, price: float) -> str:
    """KR이면 ₩123,456, US면 $123.45 형식."""
    if market_of(symbol) == "KR":
        return f"₩{int(price):,}"
    return f"${price:.2f}"


def _cool(symbol: str, kind: str) -> bool:
    k = (symbol, kind); now = time.time()
    if now - _last_sent.get(k, 0) < COOLDOWN_SEC:
        return True
    _last_sent[k] = now
    return False


async def _evaluate_item(item: dict, plan: dict, quote: any, broadcast) -> None:
    sym = item["symbol"]
    price = quote.price
    user_id = item.get("user_id", 0)

    pos = (plan.get("position") or "").strip()
    target = plan.get("target_price")
    # 새 스키마(stop_price/entry_price) + 구 스키마(reentry_or_stop_price) 모두 호환
    stop_price = plan.get("stop_price") or plan.get("reentry_or_stop_price")
    entry_price = plan.get("entry_price") or stop_price
    capital = item["capital"]; risk_pct = item["risk_pct"]

    pf = _fmt(sym, price)
    fmt_stop = _fmt(sym, float(stop_price)) if stop_price else ""
    fmt_tp = _fmt(sym, float(target)) if target else ""
    fmt_entry = _fmt(sym, float(entry_price)) if entry_price else ""

    is_buy = pos in ("분할 매수", "적극 매수")
    is_sell = pos in ("분할 매도", "적극 매도")

    # 매수 신호: 매수 포지션에서 진입가 부근 (±0.3%) 도달
    if is_buy and entry_price:
        if abs(price - float(entry_price)) / float(entry_price) <= 0.003:
            if not _cool(sym, f"BUY_{user_id}"):
                stop_for_size = float(stop_price) if stop_price else price * 0.97
                size = shares_for(capital, risk_pct, price, stop_for_size)
                splits = split_plan(size["shares"])
                msg = (f"💚 지금 {pos}! {pf} (진입가 {fmt_entry} 도달) — "
                       f"권장 {size['shares']}주 (분할: {splits}), "
                       f"투입 {_fmt(sym, size['notional'])}, 최대손실 {_fmt(sym, size['max_loss'])}")
                await db.add_alert(sym, "BUY", msg, price)
                await broadcast({"type": "alert", "symbol": sym, "kind": "BUY",
                                 "message": msg, "price": price, "user_id": user_id})

    # 익절(매수): 목표가 ≥ 도달
    if is_buy and target and price >= float(target):
        if not _cool(sym, f"TP_{user_id}"):
            msg = f"🎯 목표가 도달! {pf} ≥ {fmt_tp} — 매도/익절 권장"
            await db.add_alert(sym, "TP", msg, price)
            await broadcast({"type": "alert", "symbol": sym, "kind": "TP",
                             "message": msg, "price": price, "user_id": user_id})

    # 손절(매수): 손절가 ≤ 이탈
    if is_buy and stop_price and price <= float(stop_price):
        if not _cool(sym, f"SL_{user_id}"):
            msg = f"🛑 손절선 이탈! {pf} ≤ {fmt_stop} — 즉시 매도"
            await db.add_alert(sym, "SL", msg, price)
            await broadcast({"type": "alert", "symbol": sym, "kind": "SL",
                             "message": msg, "price": price, "user_id": user_id})

    # 매도 익절: 매도 포지션에서 목표가(하락) 이하로 도달
    if is_sell and target and price <= float(target):
        if not _cool(sym, f"TP_{user_id}"):
            msg = f"🎯 매도 목표가 도달! {pf} ≤ {fmt_tp} — 환매/청산 권장"
            await db.add_alert(sym, "TP", msg, price)
            await broadcast({"type": "alert", "symbol": sym, "kind": "TP",
                             "message": msg, "price": price, "user_id": user_id})

    # 매도 신호: 매도 포지션에서 진입가 부근 도달
    if is_sell and entry_price:
        if abs(price - float(entry_price)) / float(entry_price) <= 0.003:
            if not _cool(sym, f"SELL_{user_id}"):
                msg = f"🔴 매도 진입가 도달! {pf} — {pos}"
                await db.add_alert(sym, "SELL", msg, price)
                await broadcast({"type": "alert", "symbol": sym, "kind": "SELL",
                                 "message": msg, "price": price, "user_id": user_id})


async def worker(broadcast):
    """최적화된 병렬 폴링 워커. 세마포어를 사용하여 속도와 안정성 동시 확보."""
    log.info("alert worker started (concurrent mode)")
    sem = asyncio.Semaphore(5) # 동시 실행 5개로 제한 (Rate Limit 고려)

    async def poll_one(sym, items_group, plan):
        async with sem:
            try:
                q = await asyncio.to_thread(fetch_realtime_quote, sym)
                # 틱 송출
                await broadcast({"type": "tick", "symbol": sym, "price": q.price,
                                 "change_pct": q.change_pct, "ts": q.ts})
                
                # 각 유저별 조건 평가
                for it in items_group:
                    await _evaluate_item(it, plan, q, broadcast)
            except Exception as e:
                log.warning(f"polling error {sym}: {e}")

    while True:
        try:
            items = await db.list_all_watch()
            plans = await db.all_plans()
            
            # 심볼별로 유저 그룹화
            from collections import defaultdict
            grouped = defaultdict(list)
            for it in items:
                grouped[it["symbol"]].append(it)

            tasks = []
            for sym, user_items in grouped.items():
                if sym in plans:
                    tasks.append(poll_one(sym, user_items, plans[sym]))
            
            if tasks:
                await asyncio.gather(*tasks)
            
        except Exception:
            log.exception("worker loop error")
        await asyncio.sleep(POLL_SEC)
