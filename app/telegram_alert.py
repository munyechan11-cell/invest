"""Telegram 봇 알림 — 휴대폰 즉시 푸시 (브라우저 닫혀있어도 도착).

셋업:
1. https://t.me/BotFather → /newbot → 봇 이름·username 설정 → TOKEN 발급
2. .env 에 TELEGRAM_BOT_TOKEN=... 등록
3. 사용자가 봇과 대화 시작 (/start 또는 아무 메시지)
4. /api/telegram/discover 호출 → 본인 chat_id 자동 발견
5. 프로필에 chat_id 등록 → 알림 수신 시작
"""
from __future__ import annotations
import os, logging
import httpx

log = logging.getLogger("telegram_alert")

API_BASE = "https://api.telegram.org"


def is_configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN"))


async def send(chat_id: str | int, text: str, parse_mode: str = "HTML") -> dict:
    """텔레그램 메시지 발송. {"ok": bool, "error": str|None} 반환."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN 미설정"}
    if not chat_id:
        return {"ok": False, "error": "chat_id 비어있음"}

    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(f"{API_BASE}/bot{token}/sendMessage", json={
                "chat_id": str(chat_id), "text": text,
                "parse_mode": parse_mode, "disable_web_page_preview": True,
            })
        if r.status_code != 200:
            err = r.json().get("description", r.text[:120]) if r.headers.get("content-type", "").startswith("application/json") else r.text[:120]
            log.warning(f"telegram send fail [{r.status_code}]: {err}")
            return {"ok": False, "error": f"HTTP {r.status_code}: {err}"}
        return {"ok": True, "error": None}
    except Exception as e:
        log.warning(f"telegram error: {e}")
        return {"ok": False, "error": str(e)}


async def get_me() -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return {}
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{API_BASE}/bot{token}/getMe")
            return r.json().get("result", {}) if r.status_code == 200 else {}
    except Exception:
        return {}


async def discover_chat_ids() -> list[dict]:
    """봇과 최근 대화한 사용자들의 chat_id 목록 — 본인 ID 찾기용."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return []
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{API_BASE}/bot{token}/getUpdates")
            if r.status_code != 200:
                return []
            updates = r.json().get("result", [])
    except Exception:
        return []

    seen: dict[int, dict] = {}
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or u.get("callback_query", {}).get("message")
        if not msg:
            continue
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid and cid not in seen:
            seen[cid] = {
                "chat_id": cid,
                "username": chat.get("username"),
                "first_name": chat.get("first_name"),
                "type": chat.get("type"),
            }
    return list(seen.values())


def format_alert(symbol: str, kind: str, price: float, message: str,
                 toss_score: dict | None = None,
                 entry: float | None = None,
                 target: float | None = None,
                 stop: float | None = None,
                 is_kr: bool | None = None) -> str:
    """알림 메시지 포맷 (HTML). 토스 앱 deeplink 포함."""
    if is_kr is None:
        is_kr = symbol.isdigit() and len(symbol) == 6
    cur = "₩" if is_kr else "$"

    def fmt_p(v):
        return f"{cur}{int(v):,}" if is_kr else f"{cur}{v:.2f}"

    icons = {"BUY": "💚 매수 신호", "TP": "🎯 익절 도달",
             "SL": "🛑 손절 이탈", "SELL": "🔴 매도 신호"}
    title = icons.get(kind, kind)

    lines = [
        f"<b>{title}</b> · <b>{symbol}</b>",
        "",
        f"💰 현재가 <b>{fmt_p(price)}</b>",
    ]
    if entry:
        lines.append(f"📍 진입가: <code>{fmt_p(entry)}</code>")
    if target:
        lines.append(f"🎯 목표가: <code>{fmt_p(target)}</code>")
    if stop:
        lines.append(f"🛑 손절가: <code>{fmt_p(stop)}</code>")
    if toss_score:
        score = toss_score.get("score", 0)
        grade = toss_score.get("grade", "?")
        lines.append(f"📊 TOSS Score: <b>{score}/100</b> ({grade})")

    lines += ["", f"💬 {message}"]

    # 토스 deeplink (한국주식만 toss 앱으로 연결, 미국주식은 토스 해외주식)
    if is_kr:
        lines += ["", f"📱 <a href=\"https://tossinvest.com/stocks/A{symbol}\">토스에서 매매하기 →</a>"]
    else:
        lines += ["", f"📱 <a href=\"https://tossinvest.com/stocks/{symbol}.O\">토스 해외주식 →</a>"]

    return "\n".join(lines)
