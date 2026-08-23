"""Telegram notifications for a running bot.

Failure is always non-fatal: a notifier that can take the trading loop down is
worse than no notifier. Everything is best-effort, rate-limited, and logged.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time

import httpx

from quant.core.aio import LazyLock
from quant.core.events import Event, EventType

log = logging.getLogger("quant.live.notifier")


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str,
                 on_events: list[str] | None = None,
                 min_interval_s: float = 1.0, max_per_hour: int = 60):
        self.token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
        self.on_events = set(on_events or [])
        self.min_interval = min_interval_s
        self.max_per_hour = max_per_hour
        self._last_sent = 0.0
        self._hour_bucket: list[float] = []
        self._client = httpx.AsyncClient(timeout=10)
        self._lock = LazyLock()

    async def handle(self, event: Event) -> None:
        if not self.enabled or event.type.value not in self.on_events:
            return
        text = self._format(event)
        if text:
            await self.send(text)

    async def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        async with self._lock:
            now = time.time()
            self._hour_bucket = [t for t in self._hour_bucket if now - t < 3600]
            if len(self._hour_bucket) >= self.max_per_hour:
                log.debug("telegram hourly cap reached — dropping message")
                return False
            wait = self.min_interval - (now - self._last_sent)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                r = await self._client.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat_id, "text": text[:4000],
                          "parse_mode": "HTML", "disable_web_page_preview": True},
                )
                ok = r.status_code == 200
                if not ok:
                    log.warning("telegram send failed: %s %s", r.status_code, r.text[:200])
            except Exception as exc:
                log.warning("telegram send failed: %s", exc)
                ok = False
            self._last_sent = time.time()
            self._hour_bucket.append(self._last_sent)
            return ok

    @staticmethod
    def _format(event: Event) -> str:
        p = event.payload or {}
        e = html.escape
        if event.type is EventType.ORDER_FILLED:
            arrow = "🟢" if p.get("side") == "buy" else "🔴"
            return (f"{arrow} <b>FILL</b> {e(str(p.get('symbol')))} "
                    f"{e(str(p.get('side'))).upper()} {p.get('quantity')} @ "
                    f"{p.get('price')}\nfee {p.get('fee')} · "
                    f"slip {p.get('slippage_bps')}bps")
        if event.type is EventType.TRADE_CLOSED:
            pnl = p.get("pnl_pct", 0)
            mark = "✅" if pnl > 0 else "❌"
            return (f"{mark} <b>CLOSED</b> {e(str(p.get('symbol')))} "
                    f"{pnl:+.2f}% ({p.get('pnl')})\n"
                    f"held {p.get('duration_hours')}h · {e(str(p.get('exit_tag') or ''))}")
        if event.type is EventType.PROTECTION:
            return (f"🛡 <b>PROTECTION</b> {e(str(p.get('protection')))} on "
                    f"{e(str(p.get('symbol')))}\n{e(str(p.get('reason')))}")
        if event.type is EventType.ORDER_REJECTED:
            return (f"⚠️ <b>REJECTED</b> {e(str(p.get('symbol')))}: "
                    f"{e(str(p.get('reject_reason') or p.get('reason')))}")
        if event.type is EventType.ERROR:
            return f"🔥 <b>ERROR</b>\n{e(str(p)[:600])}"
        if event.type is EventType.STATE:
            return f"ℹ️ <b>{e(str(p.get('state','')).upper())}</b> {e(str(p.get('mode','')))}"
        return ""

    async def close(self) -> None:
        await self._client.aclose()
