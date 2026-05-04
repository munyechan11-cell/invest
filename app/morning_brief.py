"""모닝 브리프 — 매일 평일 9:00 KST 자동 텔레그램 발송.

- TOP 5 매수 후보 (한+미 합산)
- 각 종목의 SIFT Score, 포지션, 변동률, 거래량 강도
- 토스 deeplink 포함
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime

from .market_hours import KST, is_weekend_kst
from .scanner import scan_universe
from . import telegram_alert

log = logging.getLogger("morning_brief")

_last_sent_date: str | None = None


async def generate_brief() -> str:
    """오늘의 TOP 5 + 시장 요약 HTML."""
    top = await scan_universe(market="BOTH", limit=5, min_score=50)

    now = datetime.now(KST)
    lines = [
        "<b>🌅 Sift Quant 모닝 브리프</b>",
        f"<i>{now.strftime('%Y-%m-%d %A %H:%M KST')}</i>",
    ]

    if not top:
        lines.append("\n📊 데이터 수집 실패 — 잠시 후 사이트에서 직접 확인하세요.")
        return "\n".join(lines)

    lines += ["", "<b>📈 오늘의 TOP 매수 후보:</b>"]

    for i, t in enumerate(top, 1):
        is_kr = t["market"] == "KR"
        cur = "₩" if is_kr else "$"
        price_fmt = f"{cur}{int(t['price']):,}" if is_kr else f"{cur}{t['price']:.2f}"
        chg_icon = "🟢" if t["change_pct"] >= 0 else "🔴"
        lines.append(
            f"\n<b>{i}. {t['name']}</b> ({t['symbol']})\n"
            f"   {t['position_emoji']} {t['position']} · SIFT <b>{t['sift_score']:.0f}</b> ({t['grade']})\n"
            f"   {price_fmt} {chg_icon} {t['change_pct']:+.2f}% · "
            f"RV {t['rv']}x · RSI {t['rsi']:.0f}"
        )

    lines += [
        "",
        "<i>💡 사이트에서 진입가/목표가/손절가 확인 후 토스에서 매매</i>",
        "<i>⚠️ 시그널 ≠ 매수 권유. 본인 판단 필수.</i>",
    ]
    return "\n".join(lines)


async def send_to_all() -> int:
    """모든 텔레그램 구독자에게 브리프 발송. 발송 성공 수 반환."""
    if not telegram_alert.is_configured():
        log.info("telegram not configured — brief skipped")
        return 0

    from server import db
    try:
        subs = await db.all_telegram_subscribers()
    except Exception as e:
        log.warning(f"db sub fetch fail: {e}")
        return 0

    if not subs:
        return 0

    text = await generate_brief()
    sent = 0
    for _uid, chat_id in subs:
        res = await telegram_alert.send(chat_id, text)
        if res.get("ok"):
            sent += 1
    log.info(f"morning brief sent to {sent}/{len(subs)}")
    return sent


async def daily_scheduler():
    """매일 평일 9:00~9:05 KST에 자동 발송. 중복 발송 방지."""
    global _last_sent_date
    log.info("morning brief scheduler started — every weekday 09:00 KST")
    while True:
        try:
            now = datetime.now(KST)
            today = now.strftime("%Y-%m-%d")

            if (not is_weekend_kst() and now.hour == 9 and now.minute < 5
                    and _last_sent_date != today):
                count = await send_to_all()
                _last_sent_date = today
                log.info(f"morning brief auto-sent at {now}: {count} subscribers")
        except Exception:
            log.exception("morning brief scheduler error")

        await asyncio.sleep(60)  # 1분마다 체크
