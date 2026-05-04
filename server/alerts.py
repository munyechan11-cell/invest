"""실시간 가격 폴링 + 알림 트리거 (브라우저·Telegram·모의 트레이드 추적)."""
from __future__ import annotations
import asyncio, time, logging
from app.market import fetch_realtime_quote, market_of
from app import telegram_alert
from app.symbol_names import get_name as _symbol_name
from server import db
from server.sizing import shares_for, split_plan

log = logging.getLogger("alerts")

POLL_SEC = 5           # 실시간 폴링 주기 (포트폴리오 자동 업데이트 5초)
COOLDOWN_SEC = 300     # 같은 알림 재발송 방지
_MAX_COOL_ENTRIES = 1000  # 메모리 누수 방지 한계
_last_sent: dict[tuple[str, str], float] = {}


def _gc_cool():
    """_last_sent 정리 — 만료된 항목 제거 + 한계 초과 시 절반 비우기."""
    now = time.time()
    # 만료된 것 모두 제거 (TTL × 3 지난 것)
    expired = [k for k, ts in _last_sent.items() if now - ts > COOLDOWN_SEC * 3]
    for k in expired:
        _last_sent.pop(k, None)
    # 그래도 너무 많으면 오래된 절반
    if len(_last_sent) > _MAX_COOL_ENTRIES:
        sorted_items = sorted(_last_sent.items(), key=lambda x: x[1])
        for k, _ in sorted_items[: len(_last_sent) // 2]:
            _last_sent.pop(k, None)


async def _push_telegram(symbol: str, kind: str, price: float, message: str,
                         plan: dict | None = None, name: str | None = None):
    """알림을 Telegram으로 푸시 — 등록된 사용자별 설정 적용 (min_score, snooze, inline 버튼)."""
    if not telegram_alert.is_configured():
        return
    try:
        subs = await db.all_telegram_subscribers()
    except Exception:
        return
    if not subs:
        return

    is_kr = symbol.isdigit() and len(symbol) == 6
    sift_score = (plan or {}).get("sift_score") if plan else None
    text = telegram_alert.format_alert(
        symbol, kind, price, message,
        sift_score=sift_score,
        entry=(plan or {}).get("entry_price"),
        target=(plan or {}).get("target_price"),
        stop=(plan or {}).get("stop_price"),
        is_kr=is_kr,
        name=name,
    )

    score_val = (sift_score or {}).get("score", 0) if isinstance(sift_score, dict) else 0
    for user_id, chat_id in subs:
        try:
            settings = await db.get_user_settings(user_id)
            # 사용자 min_score 미달 → 알림 스킵
            if settings.get("telegram_min_score", 0) > score_val:
                continue
            # 스누즈 중이면 스킵
            if await db.is_snoozed(user_id, symbol):
                continue
            # 인라인 버튼 옵션
            buttons = (telegram_alert.build_alert_buttons(symbol, kind)
                       if settings.get("enable_inline_actions", 1) else None)
            await telegram_alert.send(chat_id, text, reply_markup=buttons)
        except Exception as e:
            log.warning(f"telegram push uid={user_id}: {e}")


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

    # 종목명 — 매핑 사전(즉시) → Yahoo 캐시(첫 호출 1회 fetch). 없으면 빈 문자열.
    name = _symbol_name(sym)
    sym_label = f"{name} ({sym})" if name and name.upper() != sym.upper() else sym

    is_buy = pos in ("분할 매수", "적극 매수")
    is_sell = pos in ("분할 매도", "적극 매도")

    # 매수 신호: 매수 포지션에서 진입가 부근 (±0.3%) 도달
    if is_buy and entry_price:
        if abs(price - float(entry_price)) / float(entry_price) <= 0.003:
            if not _cool(sym, f"BUY_{user_id}"):
                stop_for_size = float(stop_price) if stop_price else price * 0.97
                size = shares_for(capital, risk_pct, price, stop_for_size) if capital > 0 else {"shares": 0, "notional": 0, "max_loss": 0}
                splits = split_plan(size["shares"]) if capital > 0 else []
                if capital > 0:
                    msg = (f"💚 {sym_label} · 지금 {pos}! {pf} (진입가 {fmt_entry} 도달) — "
                           f"권장 {size['shares']}주 (분할: {splits}), "
                           f"투입 {_fmt(sym, size['notional'])}, 최대손실 {_fmt(sym, size['max_loss'])}")
                else:
                    msg = f"💚 {sym_label} · 지금 {pos}! {pf} (진입가 {fmt_entry} 도달) — 토스증권에서 매수 진행"
                await db.add_alert(sym, "BUY", msg, price)
                await broadcast({"type": "alert", "symbol": sym, "name": name, "kind": "BUY",
                                 "message": msg, "price": price, "user_id": user_id})
                await _push_telegram(sym, "BUY", price, msg, plan, name=name)
                # 모의 트레이드 자동 시작 — 추후 TP/SL 도달 시 자동 청산
                try:
                    await db.open_mock_trade_for_all(sym, "buy", price,
                                                    float(target) if target else None,
                                                    float(stop_price) if stop_price else None)
                except Exception as e:
                    log.warning(f"mock_trade open fail: {e}")

    # 익절(매수): 목표가 ≥ 도달
    if is_buy and target and price >= float(target):
        if not _cool(sym, f"TP_{user_id}"):
            msg = f"🎯 {sym_label} · 목표가 도달! {pf} ≥ {fmt_tp} — 매도/익절 권장"
            await db.add_alert(sym, "TP", msg, price)
            await broadcast({"type": "alert", "symbol": sym, "name": name, "kind": "TP",
                             "message": msg, "price": price, "user_id": user_id})
            await _push_telegram(sym, "TP", price, msg, plan, name=name)

    # 손절(매수): 손절가 ≤ 이탈
    if is_buy and stop_price and price <= float(stop_price):
        if not _cool(sym, f"SL_{user_id}"):
            msg = f"🛑 {sym_label} · 손절선 이탈! {pf} ≤ {fmt_stop} — 즉시 매도"
            await db.add_alert(sym, "SL", msg, price)
            await broadcast({"type": "alert", "symbol": sym, "name": name, "kind": "SL",
                             "message": msg, "price": price, "user_id": user_id})
            await _push_telegram(sym, "SL", price, msg, plan, name=name)

            # 자동 손절매: settings.auto_stoploss=1이면 보유 전량 매도
            try:
                settings = await db.get_user_settings(user_id)
                if settings.get("auto_stoploss") == 1:
                    port_items = [p for p in await db.list_portfolio(user_id) if p["symbol"] == sym]
                    held = sum(float(p.get("shares") or 0) for p in port_items)
                    if held >= 1:
                        from app.trade_kis import auto_order
                        sell_qty = int(held)
                        result = await asyncio.to_thread(auto_order, sym, "sell", sell_qty, price)
                        log.info(f"AUTO_SL user={user_id} {sym} qty={sell_qty} → {result.get('ok')}")
                        if result.get("ok") and port_items:
                            try:
                                await db.record_realized_pnl(
                                    user_id, sym, sell_qty,
                                    float(port_items[0]["entry_price"]), price,
                                    fee=0, reason="auto_stoploss"
                                )
                            except Exception:
                                pass
                            # 텔레그램 추가 알림
                            chat = await db.get_telegram_chat_id(user_id)
                            if chat and telegram_alert.is_configured():
                                await telegram_alert.send(
                                    chat,
                                    f"🤖 <b>자동 손절 실행</b>\n\n"
                                    f"{sym} {sell_qty}주 매도 @ {pf}\n"
                                    f"모드: <b>{result.get('mode', 'PAPER')}</b>"
                                )
            except Exception as e:
                log.warning(f"auto_stoploss fail {sym}: {e}")

    # 매도 익절: 매도 포지션에서 목표가(하락) 이하로 도달
    if is_sell and target and price <= float(target):
        if not _cool(sym, f"TP_{user_id}"):
            msg = f"🎯 {sym_label} · 매도 목표가 도달! {pf} ≤ {fmt_tp} — 환매/청산 권장"
            await db.add_alert(sym, "TP", msg, price)
            await broadcast({"type": "alert", "symbol": sym, "name": name, "kind": "TP",
                             "message": msg, "price": price, "user_id": user_id})
            await _push_telegram(sym, "TP", price, msg, plan, name=name)

    # 매도 신호: 매도 포지션에서 진입가 부근 도달
    if is_sell and entry_price:
        if abs(price - float(entry_price)) / float(entry_price) <= 0.003:
            if not _cool(sym, f"SELL_{user_id}"):
                msg = f"🔴 {sym_label} · 매도 진입가 도달! {pf} — {pos}"
                await db.add_alert(sym, "SELL", msg, price)
                await broadcast({"type": "alert", "symbol": sym, "name": name, "kind": "SELL",
                                 "message": msg, "price": price, "user_id": user_id})
                await _push_telegram(sym, "SELL", price, msg, plan, name=name)


async def _check_price_alerts(symbol: str, price: float, broadcast):
    """사용자 지정 가격 알림 체크 — 도달 시 1회 발송 후 비활성화."""
    try:
        alerts = await db.list_active_price_alerts()
    except Exception:
        return
    for a in alerts:
        if a["symbol"] != symbol:
            continue
        target = float(a["target_price"])
        cond = a.get("condition", ">=")
        hit = (cond == ">=" and price >= target) or \
              (cond == "<=" and price <= target) or \
              (cond == "==" and abs(price - target) / target < 0.001)
        if not hit:
            continue
        # 발송 후 비활성화 (1회성)
        await db.trigger_price_alert(a["id"])
        pf = _fmt(symbol, price)
        ft = _fmt(symbol, target)
        note = a.get("note") or ""
        name = _symbol_name(symbol)
        sym_label = f"{name} ({symbol})" if name and name.upper() != symbol.upper() else symbol
        msg = f"🔔 사용자 알림! {sym_label} {pf} {cond} {ft}" + (f" — {note}" if note else "")
        await db.add_alert(symbol, "CUSTOM", msg, price)
        await broadcast({"type": "alert", "symbol": symbol, "name": name, "kind": "CUSTOM",
                         "message": msg, "price": price, "user_id": a["user_id"]})
        # 텔레그램
        chat_id = await db.get_telegram_chat_id(a["user_id"])
        if chat_id and telegram_alert.is_configured():
            tmsg = (
                f"<b>🔔 가격 알림 도달</b>\n\n"
                f"<b>{sym_label}</b>\n"
                f"현재가: <b>{pf}</b>\n"
                f"설정가: <code>{cond} {ft}</code>\n"
            )
            if note:
                tmsg += f"메모: <i>{note}</i>\n"
            await telegram_alert.send(chat_id, tmsg)


async def _check_mock_trades(symbol: str, price: float):
    """오픈된 모의 트레이드의 TP/SL 도달 자동 청산."""
    try:
        opens = await db.list_open_mock_trades()
    except Exception:
        return
    for t in opens:
        if t["symbol"] != symbol:
            continue
        side = t["side"]
        target = t.get("target_price")
        stop = t.get("stop_price")
        try:
            if side == "buy":
                if target and price >= float(target):
                    await db.close_mock_trade(t["id"], price, "TP")
                elif stop and price <= float(stop):
                    await db.close_mock_trade(t["id"], price, "SL")
            else:  # sell
                if target and price <= float(target):
                    await db.close_mock_trade(t["id"], price, "TP")
                elif stop and price >= float(stop):
                    await db.close_mock_trade(t["id"], price, "SL")
        except Exception as e:
            log.warning(f"mock close fail {t['id']}: {e}")


async def worker(broadcast):
    """병렬 폴링 워커 — 워치리스트 + 포트폴리오 합집합을 5초마다 갱신."""
    log.info(f"alert worker started — polling every {POLL_SEC}s (watchlist + portfolio)")
    sem = asyncio.Semaphore(5)  # Finnhub/KIS 레이트 리밋 보호

    from collections import defaultdict

    async def poll_one(sym: str, watch_items: list[dict], port_items: list[dict], plan: dict | None):
        async with sem:
            try:
                q = await asyncio.to_thread(fetch_realtime_quote, sym)
            except Exception as e:
                log.warning(f"polling error {sym}: {e}")
                return

            # ── 모든 구독자에게 가격 틱 송출 (포트폴리오·워치리스트 모두 사용)
            await broadcast({
                "type": "tick", "symbol": sym,
                "price": q.price, "change_pct": q.change_pct, "ts": q.ts,
                "in_portfolio": bool(port_items),
                "in_watchlist": bool(watch_items),
            })

            # ── 오픈된 모의 트레이드 자동 청산 체크
            await _check_mock_trades(sym, q.price)

            # ── 사용자 지정 가격 알림 체크
            await _check_price_alerts(sym, q.price, broadcast)

            # ── 포트폴리오 보유종목별 평가손익 계산 + 송출
            for p in port_items:
                entry = float(p.get("entry_price") or 0)
                shares = float(p.get("shares") or 0)
                if entry > 0 and shares > 0:
                    pnl = (q.price - entry) * shares
                    pnl_pct = (q.price / entry - 1) * 100
                    await broadcast({
                        "type": "portfolio_pnl",
                        "user_id": p.get("user_id"),
                        "portfolio_id": p.get("id"),
                        "symbol": sym,
                        "current_price": q.price,
                        "entry_price": entry,
                        "shares": shares,
                        "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "ts": q.ts,
                    })

            # ── 알림 평가: 워치리스트 + 포트폴리오 둘 다 (이전엔 워치만)
            if plan:
                # 워치리스트: 분석 시 입력한 capital/risk_pct 사용
                for it in watch_items:
                    await _evaluate_item(it, plan, q, broadcast)

                # 포트폴리오: 보유종목용 합성 item으로 평가
                #  - capital은 실제 매수금액(krw_invested or shares*entry)
                #  - 같은 user에 같은 symbol이 워치+포트 둘 다 있어도 _cool 쿨다운으로 중복 방지
                already_alerted_users = {it.get("user_id") for it in watch_items}
                for p in port_items:
                    uid = p.get("user_id")
                    if uid in already_alerted_users:
                        continue  # 워치에서 이미 알림 발송됨 (중복 회피)
                    entry = float(p.get("entry_price") or 0)
                    shares = float(p.get("shares") or 0)
                    notional = entry * shares if entry and shares else float(p.get("krw_invested") or 0)
                    synthetic = {
                        "symbol": p["symbol"],
                        "user_id": uid,
                        "capital": notional or 1000,  # 비어있어도 알림은 발송
                        "risk_pct": 1.0,
                        "_source": "portfolio",
                    }
                    await _evaluate_item(synthetic, plan, q, broadcast)

    loop_count = 0
    while True:
        try:
            # 매 60회(=5분)마다 cooldown 메모리 정리
            loop_count += 1
            if loop_count % 60 == 0:
                _gc_cool()

            # 한국·미국 둘 다 휴장이면 1분만 쉬고 가벼운 폴링
            from app.market_hours import kr_market_status, us_market_status
            kr_open = kr_market_status().get("is_open")
            us_open = us_market_status().get("is_open")
            if not kr_open and not us_open:
                # 둘 다 닫혀있으면 60초마다만 폴링 (5초 → 60초)
                if loop_count % 12 != 0:
                    await asyncio.sleep(POLL_SEC)
                    continue

            watch = await db.list_all_watch()
            port = await db.list_all_portfolio()
            plans = await db.all_plans()

            wl_grouped: dict[str, list] = defaultdict(list)
            for it in watch:
                wl_grouped[it["symbol"]].append(it)

            port_grouped: dict[str, list] = defaultdict(list)
            for it in port:
                port_grouped[it["symbol"]].append(it)

            all_symbols = set(wl_grouped.keys()) | set(port_grouped.keys())

            if all_symbols:
                tasks = [poll_one(sym, wl_grouped.get(sym, []),
                                  port_grouped.get(sym, []), plans.get(sym))
                         for sym in all_symbols]
                await asyncio.gather(*tasks)

        except Exception:
            log.exception("worker loop error")
        await asyncio.sleep(POLL_SEC)
