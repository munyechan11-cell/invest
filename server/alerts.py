"""실시간 가격 폴링 + 알림 트리거."""
from __future__ import annotations
import asyncio, time, logging
from app.market import fetch_realtime_quote
from . import db
from .sizing import shares_for, split_plan

log = logging.getLogger("alerts")

POLL_SEC = 6           # 워치리스트당 폴링 주기
COOLDOWN_SEC = 300     # 같은 알림 재발송 방지
_last_sent: dict[tuple[str, str], float] = {}


def _cool(symbol: str, kind: str) -> bool:
    k = (symbol, kind); now = time.time()
    if now - _last_sent.get(k, 0) < COOLDOWN_SEC:
        return True
    _last_sent[k] = now
    return False


async def _evaluate(item: dict, plan: dict, broadcast) -> None:
    sym = item["symbol"]
    try:
        q = await asyncio.to_thread(fetch_realtime_quote, sym)
    except Exception as e:
        log.warning("quote fail %s: %s", sym, e)
        return
    price = q.price

    # 항상 가격 틱 송출
    await broadcast({"type": "tick", "symbol": sym, "price": price,
                     "change_pct": q.change_pct, "ts": q.ts})

    pos = (plan.get("position") or "").strip()
    target = plan.get("target_price")
    sr_label = plan.get("reentry_or_stop_label")
    sr_price = plan.get("reentry_or_stop_price")
    capital = item["capital"]; risk_pct = item["risk_pct"]

    # 매수 조건: 분할매수/적극매수에서, 재진입가 부근 또는 그 아래
    if pos in ("분할 매수", "적극 매수") and sr_price:
        if price <= float(sr_price) * 1.003:  # 0.3% 안쪽이면 트리거
            if not _cool(sym, "BUY"):
                size = shares_for(capital, risk_pct, price, float(sr_price) * 0.97)
                splits = split_plan(size["shares"])
                msg = (f"💚 지금 {pos}! ${price:.2f} 도달 — "
                       f"권장 {size['shares']}주 (분할: {splits}), "
                       f"투입 ${size['notional']:.0f}, 최대손실 ${size['max_loss']:.0f}")
                await db.add_alert(sym, "BUY", msg, price)
                await broadcast({"type": "alert", "symbol": sym, "kind": "BUY",
                                 "message": msg, "price": price})

    # 익절: 목표가 도달
    if target and price >= float(target):
        if not _cool(sym, "TP"):
            msg = f"🎯 목표가 도달! ${price:.2f} ≥ ${target} — 매도/익절 권장"
            await db.add_alert(sym, "TP", msg, price)
            await broadcast({"type": "alert", "symbol": sym, "kind": "TP",
                             "message": msg, "price": price})

    # 손절: 손절가 이탈 (매수계열에서만)
    if pos in ("분할 매수", "적극 매수") and sr_label == "손절가" and sr_price and price <= float(sr_price):
        if not _cool(sym, "SL"):
            msg = f"🛑 손절선 이탈! ${price:.2f} ≤ ${sr_price} — 즉시 매도"
            await db.add_alert(sym, "SL", msg, price)
            await broadcast({"type": "alert", "symbol": sym, "kind": "SL",
                             "message": msg, "price": price})

    # 매도 권고에서 강한 추가 하락 → 추가 매도 알림
    if pos in ("분할 매도", "적극 매도") and target and price >= float(target):
        if not _cool(sym, "SELL"):
            msg = f"🔴 매도 신호 가격대 도달! ${price:.2f} — {pos}"
            await db.add_alert(sym, "SELL", msg, price)
            await broadcast({"type": "alert", "symbol": sym, "kind": "SELL",
                             "message": msg, "price": price})


async def worker(broadcast):
    """단일 background task. 워치리스트 순회하며 폴링."""
    log.info("alert worker started")
    while True:
        try:
            items = await db.list_watch()
            plans = await db.all_plans()
            for it in items:
                plan = plans.get(it["symbol"])
                if not plan:
                    continue
                await _evaluate(it, plan, broadcast)
                await asyncio.sleep(0.3)  # rate limit cushion
        except Exception:
            log.exception("worker loop error")
        await asyncio.sleep(POLL_SEC)
