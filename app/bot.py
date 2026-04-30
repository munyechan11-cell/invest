"""Telegram 봇 - 티커 → 퀀트 리포트."""
from __future__ import annotations
import os, asyncio, logging
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from app.market import get_snapshot
from app.news import fetch_news, fetch_profile, fetch_market_flow
from app.analyze import analyze
from app.trade import place

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
log = logging.getLogger("bot")


def fmt(symbol: str, snap: dict, ana: dict, profile: dict) -> str:
    q = snap["quote"]
    name = profile.get("name") or symbol
    emo = ana.get("position_emoji", "•")
    label = ana.get("reentry_or_stop_label", "재진입/손절가")

    lines = [
        f"### 📊 {symbol} 실시간 퀀트 분석 리포트",
        f"_{name}_",
        "",
        f"- *현재가:* ${q['price']:.2f}  ({q['change_pct']:+.2f}%)",
        f"- *금일 변동폭:* 최저 ${q['day_low']:.2f} / 최고 ${q['day_high']:.2f}",
        f"- *현재 포지션:* {emo} *{ana.get('position','관망')}*  (신뢰도 {ana.get('confidence','?')}%)",
        "",
        "*1. 포지션 선정 근거 (기술 및 펀더멘털 분석)*",
        f"{ana.get('rationale','')}",
        "",
        "*2. 트레이딩 액션 플랜*",
        f"- *목표가(Target Price):* ${ana.get('target_price','-')}",
        f"- *{label}:* ${ana.get('reentry_or_stop_price','-')}",
        f"- *권장 보유 기간:* {ana.get('holding_period','-')} — {ana.get('holding_period_reason','')}",
        "",
        "*3. 시장 수급 동향 (US Market Data)*",
        f"- *기관(Institutional):* {ana.get('flow_institutional','-')} — {ana.get('flow_institutional_reason','')}",
        f"- *개인(Retail):* {ana.get('flow_retail','-')}",
        f"- *특이사항:* {ana.get('flow_special','특이사항 없음')}",
        "",
        "*4. 실시간 마켓 컨텍스트*",
        f"{ana.get('market_context','')}",
        "",
        f"📉 [차트 확인하기](https://www.tradingview.com/symbols/{symbol}/)",
        f"_쿼트 시각: {q['ts']}_",
    ]
    return "\n".join(lines)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "티커를 보내세요 (예: TSLA, AAPL, NVDA)\n"
        "/buy TSLA 1  · 페이퍼 매수\n"
        "/sell TSLA 1 · 페이퍼 매도"
    )


async def handle_ticker(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().upper()
    if not text or len(text) > 6 or not text.isalpha():
        await update.message.reply_text("티커 1~5자 알파벳만 보내주세요. 예: TSLA")
        return
    msg = await update.message.reply_text(f"⏳ {text} 실시간 분석 중…")
    try:
        snap = await asyncio.to_thread(get_snapshot, text)
        news, profile, flow = await asyncio.gather(
            fetch_news(text), fetch_profile(text), fetch_market_flow(text)
        )
        ana = await asyncio.to_thread(analyze, text, snap, news, flow, profile)
        await msg.edit_text(
            fmt(text, snap, ana, profile),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.exception("analyze failed")
        await msg.edit_text(f"⚠️ 실패: {e}")


async def cmd_trade(update: Update, ctx: ContextTypes.DEFAULT_TYPE, side: str):
    args = ctx.args or []
    if len(args) < 2:
        await update.message.reply_text(f"사용법: /{side} TSLA 1")
        return
    sym, qty = args[0].upper(), int(args[1])
    res = await asyncio.to_thread(place, sym, side, qty)
    await update.message.reply_text(f"{side.upper()} {sym} x{qty} → {res}")


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("buy", lambda u, c: cmd_trade(u, c, "buy")))
    app.add_handler(CommandHandler("sell", lambda u, c: cmd_trade(u, c, "sell")))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ticker))
    log.info("Bot starting…")
    app.run_polling()


if __name__ == "__main__":
    main()
