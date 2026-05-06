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


def build_alert_buttons(symbol: str, kind: str = "BUY") -> dict:
    """알림용 인라인 버튼.

    매수: 1주/5주/10주 + 스누즈
    매도: 25%/50%/100% (보유 비율) + 스누즈
    """
    buttons = []
    if kind in ("BUY", "TP", "DART", "SCREENER"):
        # 매수 수량 선택
        buttons.append([
            {"text": "💚 1주", "callback_data": f"buy:{symbol}:1"},
            {"text": "💚 5주", "callback_data": f"buy:{symbol}:5"},
            {"text": "💚 10주", "callback_data": f"buy:{symbol}:10"},
        ])
        buttons.append([
            {"text": "💤 1시간 스누즈", "callback_data": f"snooze:{symbol}:60"},
            {"text": "🔕 6시간 스누즈", "callback_data": f"snooze:{symbol}:360"},
        ])
    if kind in ("SL", "SELL"):
        # 매도는 보유 비율
        buttons.append([
            {"text": "🔴 25%", "callback_data": f"sellpct:{symbol}:25"},
            {"text": "🔴 50%", "callback_data": f"sellpct:{symbol}:50"},
            {"text": "🔴 전량", "callback_data": f"sellpct:{symbol}:100"},
        ])
        buttons.append([
            {"text": "💤 1시간 스누즈", "callback_data": f"snooze:{symbol}:60"},
        ])
    if not buttons:
        buttons.append([{"text": "💤 1시간 스누즈", "callback_data": f"snooze:{symbol}:60"}])
    return {"inline_keyboard": buttons}


async def send(chat_id: str | int, text: str, parse_mode: str = "HTML",
               reply_markup: dict | None = None) -> dict:
    """텔레그램 메시지 발송. {"ok": bool, "error": str|None} 반환."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN 미설정"}
    if not chat_id:
        return {"ok": False, "error": "chat_id 비어있음"}

    payload = {
        "chat_id": str(chat_id), "text": text,
        "parse_mode": parse_mode, "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(f"{API_BASE}/bot{token}/sendMessage", json=payload)
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


async def answer_callback(callback_query_id: str, text: str = "처리됨") -> bool:
    """인라인 버튼 클릭 응답 (토스트 표시)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(f"{API_BASE}/bot{token}/answerCallbackQuery", json={
                "callback_query_id": callback_query_id,
                "text": text[:200],
                "show_alert": False,
            })
            return r.status_code == 200
    except Exception:
        return False


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


def format_portfolio_added(symbol: str, name: str, entry_price: float,
                           shares: float, krw_invested: float,
                           current_price: float | None = None,
                           ana: dict | None = None,
                           is_kr: bool | None = None) -> str:
    """포트폴리오 담음 알림 — 매수 정보 + 현재 분석 결과 종합."""
    if is_kr is None:
        is_kr = symbol.isdigit() and len(symbol) == 6
    cur = "₩" if is_kr else "$"

    def p(v):
        return f"{cur}{int(v):,}" if is_kr else f"{cur}{v:.2f}"

    lines = [
        "<b>📥 포트폴리오 담김</b>",
        "",
        f"<b>{name}</b> ({symbol})",
        f"💰 매수가: <code>{p(entry_price)}</code>",
        f"📊 수량: <b>{shares:g}주</b>" if shares else "📊 수량: (입력 안 함)",
        f"💵 총 투입: <b>{p(krw_invested if not is_kr else krw_invested)}</b>",
    ]

    # 현재가 + 평가손익 (즉시)
    if current_price and current_price > 0 and entry_price > 0 and shares > 0:
        pnl = (current_price - entry_price) * shares
        pnl_pct = (current_price / entry_price - 1) * 100
        pnl_icon = "🟢" if pnl >= 0 else "🔴"
        sign = "+" if pnl >= 0 else ""
        lines.append(f"📈 현재가: <code>{p(current_price)}</code> {pnl_icon} {sign}{pnl_pct:.2f}%")

    # AI 분석 결과 (이미 있다면)
    if ana:
        ts = ana.get("sift_score")
        if ts:
            lines.append(
                f"🎯 SIFT Score: <b>{ts.get('score','?')}/100</b> "
                f"({ts.get('grade','?')}) {ts.get('label','')}"
            )
        pos = ana.get("position")
        if pos:
            emo = ana.get("position_emoji", "")
            lines.append(f"📌 추천 포지션: {emo} {pos}")
        target = ana.get("target_price")
        stop = ana.get("stop_price")
        if target:
            lines.append(f"🎯 목표가: <code>{p(float(target))}</code>")
        if stop:
            lines.append(f"🛑 손절가: <code>{p(float(stop))}</code>")
        hp = ana.get("holding_period")
        if hp:
            lines.append(f"⏱ 권장 보유: {hp}")
        ns = ana.get("news_summary")
        if ns:
            lines.append(f"\n💬 {ns[:200]}")

    lines.append("")
    lines.append("<i>이제 가격 변동 시 자동 알림 시작 (목표/손절 도달 시 즉시 푸시)</i>")
    if is_kr:
        lines.append(f"\n📱 <a href=\"https://tossinvest.com/stocks/A{symbol}\">토스에서 확인 →</a>")
    else:
        lines.append(f"\n📱 <a href=\"https://tossinvest.com/stocks/{symbol}.O\">토스 해외주식 →</a>")

    return "\n".join(lines)


def format_alert(symbol: str, kind: str, price: float, message: str,
                 sift_score: dict | None = None,
                 entry: float | None = None,
                 target: float | None = None,
                 stop: float | None = None,
                 is_kr: bool | None = None,
                 name: str | None = None,
                 confluence: dict | None = None) -> str:
    """알림 메시지 포맷 (HTML). 토스 앱 deeplink 포함.

    name: 종목 한글/영문명. 있으면 헤더에 '종목명 (티커)' 형식으로 표기.
    confluence: {score:int, tier:str, label:str, ...} — 5개 도트로 시각화.
    """
    if is_kr is None:
        is_kr = symbol.isdigit() and len(symbol) == 6
    cur = "₩" if is_kr else "$"

    def fmt_p(v):
        return f"{cur}{int(v):,}" if is_kr else f"{cur}{v:.2f}"

    icons = {"BUY": "💚 매수 신호", "TP": "🎯 익절 도달",
             "SL": "🛑 손절 이탈", "SELL": "🔴 매도 신호"}
    title = icons.get(kind, kind)
    sym_label = (
        f"{name} ({symbol})"
        if name and name.strip() and name.strip().upper() != symbol.upper()
        else symbol
    )

    lines = [
        f"<b>{title}</b> · <b>{sym_label}</b>",
        "",
        f"💰 현재가 <b>{fmt_p(price)}</b>",
    ]
    if entry:
        lines.append(f"📍 진입가: <code>{fmt_p(entry)}</code>")
    if target:
        lines.append(f"🎯 목표가: <code>{fmt_p(target)}</code>")
    if stop:
        lines.append(f"🛑 손절가: <code>{fmt_p(stop)}</code>")
    if sift_score:
        score = sift_score.get("score", 0)
        grade = sift_score.get("grade", "?")
        lines.append(f"📊 SIFT Score: <b>{score}/100</b> ({grade})")

    # Confluence 도트 시각화 (●●●●● vs ●●●○○) + 점수 + tier
    if confluence and isinstance(confluence, dict):
        cs = int(confluence.get("score") or 0)
        tier = confluence.get("tier") or ""
        dots = "●" * cs + "○" * (5 - cs)
        tier_emoji = "⭐" if tier == "high" else "✅" if cs >= 4 else "🟡" if cs >= 3 else "⚠️"
        lines.append(f"🎯 신뢰도: <code>{dots}</code> <b>{cs}/5</b> {tier_emoji}")

    lines += ["", f"💬 {message}"]

    # 토스 deeplink (한국주식만 toss 앱으로 연결, 미국주식은 토스 해외주식)
    if is_kr:
        lines += ["", f"📱 <a href=\"https://tossinvest.com/stocks/A{symbol}\">토스에서 매매하기 →</a>"]
    else:
        lines += ["", f"📱 <a href=\"https://tossinvest.com/stocks/{symbol}.O\">토스 해외주식 →</a>"]

    return "\n".join(lines)
