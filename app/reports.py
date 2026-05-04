"""일일/주간 성과 리포트 — 텔레그램 자동 발송."""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta

from .market_hours import KST, is_weekend_kst
from . import telegram_alert

log = logging.getLogger("reports")
_last_daily_date: str | None = None
_last_weekly_date: str | None = None


async def generate_daily_report(user_id: int) -> str:
    """오늘의 모의 트레이드 + 가격 알림 + 실현 손익 요약."""
    from server import db
    user = await db.get_user_by_id(user_id)
    name = user.get("display_name") or user.get("username") or "사용자"
    now = datetime.now(KST)

    mock_stats = await db.mock_trade_stats(user_id)
    realized = await db.realized_pnl_summary(user_id)

    # 오늘 발생한 알림 수
    today_alerts = []
    try:
        all_alerts = await db.recent_alerts(limit=200)
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        today_alerts = [a for a in all_alerts if a.get("created_at", 0) >= cutoff]
    except Exception:
        pass

    lines = [
        f"<b>📊 일일 리포트 — {now.strftime('%m/%d')}</b>",
        f"<i>{name}님</i>",
        "",
        "<b>오늘의 활동:</b>",
        f"• 알림: {len(today_alerts)}건",
        f"• 모의 시그널 누적: {mock_stats['total_signals']}건 (진행중 {mock_stats['open']})",
        "",
    ]

    if mock_stats["closed"] > 0:
        lines += [
            "<b>📈 모의 시그널 검증:</b>",
            f"• 승률: <b>{mock_stats['win_rate']}%</b> ({mock_stats['wins']}승 {mock_stats['losses']}패)",
            f"• 평균 수익률: <b>{mock_stats['avg_return_pct']:+.2f}%</b>",
            f"• 누적: <b>{mock_stats['cumulative_pct']:+.2f}%</b>",
            f"• 베스트/워스트: {mock_stats['best_pct']:+.2f}% / {mock_stats['worst_pct']:+.2f}%",
            "",
        ]

    if realized["trades"] > 0:
        sign = "+" if realized["total_pnl"] >= 0 else ""
        lines += [
            "<b>💰 실현 손익 (전체 기간):</b>",
            f"• 거래 {realized['trades']}건 (승률 {realized['win_rate']}%)",
            f"• 누적 손익: <b>{sign}{realized['total_pnl']:,.0f}</b>",
            f"• 평균 수익률: {realized['avg_pnl_pct']:+.2f}%",
            "",
        ]

    lines.append("<i>💡 시그널 신뢰도가 4주 60%+ 도달 시 실전 전환 검토</i>")
    return "\n".join(lines)


async def generate_weekly_report(user_id: int) -> str:
    """일요일 발송용 주간 성과."""
    from server import db
    user = await db.get_user_by_id(user_id)
    name = user.get("display_name") or user.get("username") or "사용자"
    now = datetime.now(KST)
    week_ago = now - timedelta(days=7)

    realized_week = [
        r for r in await db.list_realized_pnl(user_id, days=7)
    ]

    lines = [
        f"<b>📅 주간 리포트 — {week_ago.strftime('%m/%d')}~{now.strftime('%m/%d')}</b>",
        f"<i>{name}님</i>",
        "",
    ]

    if realized_week:
        wins = sum(1 for r in realized_week if r["pnl_pct"] > 0)
        total_pnl = sum(r["pnl"] for r in realized_week)
        avg = sum(r["pnl_pct"] for r in realized_week) / len(realized_week)
        sign = "+" if total_pnl >= 0 else ""
        lines += [
            "<b>이번 주 실현 손익:</b>",
            f"• {len(realized_week)}건 거래 (승률 {wins/len(realized_week)*100:.0f}%)",
            f"• 손익: <b>{sign}{total_pnl:,.0f}</b>",
            f"• 평균 수익률: {avg:+.2f}%",
            "",
            "<b>거래 내역:</b>",
        ]
        for r in realized_week[:10]:
            sign = "+" if r["pnl_pct"] >= 0 else ""
            dt = datetime.fromtimestamp(r["closed_at"]).strftime("%m/%d")
            lines.append(f"  {dt} {r['symbol']}: {sign}{r['pnl_pct']:.2f}% ({r['reason']})")
    else:
        lines.append("이번 주 실현 거래 없음.")

    return "\n".join(lines)


async def send_daily_to_all():
    if not telegram_alert.is_configured():
        return 0
    from server import db
    subs = await db.all_telegram_subscribers()
    sent = 0
    for uid, chat_id in subs:
        try:
            text = await generate_daily_report(uid)
            res = await telegram_alert.send(chat_id, text)
            if res.get("ok"):
                sent += 1
        except Exception as e:
            log.warning(f"daily report uid={uid}: {e}")
    log.info(f"daily reports sent: {sent}/{len(subs)}")
    return sent


async def send_weekly_to_all():
    if not telegram_alert.is_configured():
        return 0
    from server import db
    subs = await db.all_telegram_subscribers()
    sent = 0
    for uid, chat_id in subs:
        try:
            text = await generate_weekly_report(uid)
            res = await telegram_alert.send(chat_id, text)
            if res.get("ok"):
                sent += 1
        except Exception as e:
            log.warning(f"weekly report uid={uid}: {e}")
    log.info(f"weekly reports sent: {sent}/{len(subs)}")
    return sent


async def daily_scheduler():
    """매일 평일 16:00 KST 일일 리포트 + 일요일 18:00 주간 리포트."""
    global _last_daily_date, _last_weekly_date
    log.info("reports scheduler started — daily 16:00 / weekly Sun 18:00 KST")
    while True:
        try:
            now = datetime.now(KST)
            today = now.strftime("%Y-%m-%d")

            # 평일 16:00~16:05 일일 리포트
            if (not is_weekend_kst() and now.hour == 16 and now.minute < 5
                    and _last_daily_date != today):
                await send_daily_to_all()
                _last_daily_date = today

            # 일요일 18:00~18:05 주간 리포트
            if (now.weekday() == 6 and now.hour == 18 and now.minute < 5
                    and _last_weekly_date != today):
                await send_weekly_to_all()
                _last_weekly_date = today
        except Exception:
            log.exception("reports scheduler error")

        await asyncio.sleep(60)
