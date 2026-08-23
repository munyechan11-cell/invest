"""The live trading loop.

Sleeps until each candle closes, fetches the newly settled bars, and hands them
to the same `Engine.on_bars` a backtest uses. Everything that is different about
live trading — retries, reconnects, position reconciliation, persistence,
graceful shutdown — lives here so the strategy layer stays identical.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
from datetime import datetime, timedelta
from typing import Callable

from quant.brokerage.live_base import LiveBrokerage
from quant.config.schema import StrategyConfig
from quant.core.clock import RealClock, next_candle_close
from quant.core.engine import Engine
from quant.core.events import Event, EventType
from quant.core.types import UTC, Bar, RunMode, Symbol, timeframe_delta
from quant.data.provider import DataProvider, gather_history
from quant.live.notifier import TelegramNotifier
from quant.live.state import StateStore
from quant.strategy.builder import build_engine

log = logging.getLogger("quant.live")


class LiveTrader:
    def __init__(
        self,
        config: StrategyConfig,
        state_path: str = "quant_state.db",
        resume: bool = True,
        max_consecutive_errors: int = 10,
    ):
        if config.mode is RunMode.BACKTEST:
            raise ValueError("LiveTrader needs mode: dry_run or live")
        self.config = config
        self.engine: Engine
        self.provider: DataProvider
        self.engine, self.provider = build_engine(config, clock=RealClock())
        self.calendar = getattr(self.engine, "calendar", None)
        self.state = StateStore(state_path)
        self.resume = resume
        self.max_errors = max_consecutive_errors
        self.notifier = TelegramNotifier(
            config.notify.telegram_bot_token, config.notify.telegram_chat_id,
            config.notify.on_events,
        )
        self.running = False
        self.errors = 0
        self.last_bar_ts: datetime | None = None
        self.started_at: datetime | None = None
        self._seen: dict[str, datetime] = {}
        self._announced_closed = False

    # ── wiring ───────────────────────────────────────────────────────────
    def _attach_observers(self) -> None:
        bus = self.engine.ctx.bus

        async def persist(event: Event) -> None:
            payload = event.payload or {}
            if event.type is EventType.ORDER_FILLED:
                self.state.record_event("order_filled", payload)
            elif event.type is EventType.TRADE_CLOSED:
                self.state.record_event("trade_closed", payload)
            elif event.type is EventType.EQUITY:
                self.state.record_equity(
                    datetime.now(UTC), payload.get("equity", 0.0),
                    payload.get("cash", 0.0), payload.get("drawdown_pct", 0.0) / 100,
                )
                self.state.snapshot_positions(self.engine.ctx.portfolio)
            elif event.type in (EventType.PROTECTION, EventType.ORDER_REJECTED,
                                EventType.RISK_ACTION, EventType.ERROR):
                self.state.record_event(event.type.value, payload)

        bus.on(None, persist)
        bus.on(None, self.notifier.handle)

    # ── startup ──────────────────────────────────────────────────────────
    async def warmup(self) -> None:
        ctx = self.engine.ctx
        symbols = list(ctx.universe)
        if not symbols:
            raise ValueError("universe is empty")
        end = datetime.now(UTC)
        start = end - self.config.warmup_delta
        log.info("warming up %d symbols with %d bars of %s history",
                 len(symbols), self.config.data.warmup_bars, self.config.data.timeframe)
        series = await gather_history(self.provider, symbols,
                                      self.config.data.timeframe, start, end)
        usable = []
        for sym in symbols:
            bars = series.get(sym.key, [])
            if len(bars) < 10:
                log.warning("%s returned only %d warm-up bars — excluded from the universe",
                            sym.ticker, len(bars))
                continue
            ctx.seed_history(sym, bars)
            ctx.portfolio.mark(sym, bars[-1].close)
            self._seen[sym.key] = bars[-1].ts
            usable.append(sym)
        if not usable:
            raise RuntimeError("no symbol produced usable warm-up data")
        self.engine.set_universe(usable)

        flow_feed = getattr(self.engine, "flow_feed", None)
        if flow_feed is not None:
            added = await flow_feed.refresh(usable, force=True)
            if flow_feed.has_data:
                log.info("수급 데이터 %d 세션 적재 완료", added)
            elif flow_feed.failures:
                log.warning("수급 데이터 없음: %s", flow_feed.failures)

        # Push the clock past the last warm-up bar so ctx.history() sees it.
        self.last_bar_ts = max(self._seen.values())

    async def start(self) -> None:
        cfg = self.config
        if self.resume and self.state.resume_run(cfg.name, cfg.mode.value):
            restored = self.state.restore_positions(
                self.engine.ctx.portfolio,
                {s.key: s for s in self.engine.ctx.universe},
            )
            log.info("resumed run %s with %d positions", self.state.run_id, restored)
        else:
            self.state.start_run(cfg.name, cfg.mode.value, cfg.portfolio.starting_cash,
                                 cfg.model_dump_json())

        self._attach_observers()
        await self.warmup()
        await self.engine.start()
        self.started_at = datetime.now(UTC)
        self.running = True

        if self.calendar is not None:
            stale = self.calendar.check_freshness()
            if stale:
                log.warning(stale)
                await self.notifier.send("⚠️ " + stale)

        banner = (f"{'🔴 LIVE' if cfg.mode is RunMode.LIVE else '🧪 DRY RUN'} "
                  f"{cfg.name} · {len(self.engine.ctx.universe)} symbols · "
                  f"{cfg.data.timeframe} · {getattr(self.calendar, 'name', 'no calendar')}")
        log.warning(banner)
        await self.notifier.send(banner)

    # ── the loop ─────────────────────────────────────────────────────────
    async def run(self) -> None:
        await self.start()
        tf = self.config.data.timeframe
        try:
            while self.running:
                if await self._wait_for_market():
                    continue
                wake = next_candle_close(datetime.now(UTC), tf, lag=3.0)
                sleep_for = (wake - datetime.now(UTC)).total_seconds()
                if sleep_for > 0:
                    log.debug("sleeping %.1fs until %s", sleep_for, wake.isoformat())
                    await asyncio.sleep(sleep_for)
                if not self.running:
                    break
                await self._tick()
        finally:
            await self.shutdown()

    async def _wait_for_market(self) -> bool:
        """Sleep until the venue opens. Returns True if we slept.

        Polling a closed book is not harmless: every fetch returns the same
        stale candle, the strategy recomputes the same signal, and every order
        comes back rejected for a reason that looks like an API fault.
        """
        if self.calendar is None or self.calendar.is_open(datetime.now(UTC)):
            return False
        nxt = self.calendar.next_open(datetime.now(UTC))
        if nxt is None:
            log.error("%s: no upcoming session found — check the holiday table",
                      self.calendar.name)
            await asyncio.sleep(3600)
            return True
        wait_s = max((nxt - datetime.now(UTC)).total_seconds(), 0)
        if not self._announced_closed:
            log.info("%s 휴장 — %s 개장까지 %.1f시간 대기",
                     self.calendar.name, nxt.isoformat(), wait_s / 3600)
            self._announced_closed = True
        # Wake periodically rather than sleeping for hours in one go, so a stop
        # signal is honoured promptly and a clock jump cannot strand the loop.
        await asyncio.sleep(min(wait_s + 2, 300))
        if self.calendar.is_open(datetime.now(UTC)):
            self._announced_closed = False
            log.info("%s 개장", self.calendar.name)
        return True

    async def _refresh_universe(self) -> None:
        """Re-run the universe chain and warm any newly admitted symbol.

        A symbol added without history would spend its first N bars emitting
        signals from a half-warm indicator, which is indistinguishable from a
        strategy that has started behaving badly.
        """
        selector = getattr(self.engine, "universe_selector", None)
        if selector is None or not selector.filters:
            return
        if not any(f.name != "held" for f in selector.filters):
            return
        if not selector.due():
            return

        ctx = self.engine.ctx
        chosen = await selector.select(ctx, self.provider)
        fresh = [s for s in chosen if not ctx.history(s)]
        if fresh:
            end = datetime.now(UTC)
            start = end - self.config.warmup_delta
            series = await gather_history(self.provider, fresh,
                                          self.config.data.timeframe, start, end)
            for sym in fresh:
                bars = series.get(sym.key, [])
                if bars:
                    ctx.seed_history(sym, bars)
                    self._seen[sym.key] = bars[-1].ts
                    ctx.portfolio.mark(sym, bars[-1].close)
                else:
                    log.warning("%s admitted to the universe but returned no history",
                                sym.ticker)
        self.engine.set_universe(chosen)

    async def _tick(self) -> None:
        try:
            await self._refresh_universe()
            bars = await self._fetch_new_bars()
            if not bars:
                log.debug("no new closed bars this cycle")
                return
            await self.engine.on_bars(bars)
            self.last_bar_ts = max(b.ts for b in bars.values())
            self.errors = 0

            if isinstance(self.engine.brokerage, LiveBrokerage):
                # Cheap insurance: reconcile every cycle so drift is caught in
                # minutes rather than after a bad restart.
                await self.engine.brokerage.sync()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.errors += 1
            log.exception("tick failed (%d/%d consecutive)", self.errors, self.max_errors)
            await self.engine.ctx.bus.publish(
                EventType.ERROR, {"error": str(exc), "consecutive": self.errors}
            )
            if self.errors >= self.max_errors:
                log.critical("too many consecutive failures — stopping")
                await self.notifier.send(
                    f"🔥 stopping {self.config.name}: {self.errors} consecutive errors"
                )
                self.running = False

    async def _fetch_new_bars(self) -> dict[str, Bar]:
        """Return only bars we have not already processed."""
        tf = self.config.data.timeframe
        out: dict[str, Bar] = {}
        watched = list(self.engine.ctx.universe)
        seen = {s.key for s in watched}
        for pos in self.engine.ctx.portfolio.open_positions:
            if pos.symbol.key not in seen:
                watched.append(pos.symbol)       # never stop pricing what we hold
        results = await asyncio.gather(
            *(self.provider.latest_bars(s, tf, 3) for s in watched),
            return_exceptions=True,
        )
        for symbol, result in zip(watched, results):
            if isinstance(result, BaseException):
                log.warning("data fetch failed for %s: %s", symbol.ticker, result)
                continue
            for bar in result:
                last = self._seen.get(symbol.key)
                if last is not None and bar.ts <= last:
                    continue
                self._seen[symbol.key] = bar.ts
                out[symbol.key] = bar        # keep the newest per symbol
        return out

    # ── shutdown ─────────────────────────────────────────────────────────
    async def shutdown(self) -> None:
        if not self.running and self.started_at is None:
            return
        self.running = False
        log.warning("shutting down %s", self.config.name)
        try:
            await self.engine.stop()
        finally:
            self.state.snapshot_positions(self.engine.ctx.portfolio)
            self.state.stop_run()
            await self.notifier.send(
                f"⏹ stopped {self.config.name}\n"
                f"equity {self.engine.ctx.portfolio.equity:,.2f} "
                f"({self.engine.ctx.portfolio.total_return:+.2%})"
            )
            await self.notifier.close()
            await self.provider.close()
            self.state.close()

    def install_signal_handlers(self) -> None:
        """Ctrl-C and SIGTERM stop *cleanly* — open positions are left alone but
        state is flushed, which is what you want on a container redeploy."""
        loop = asyncio.get_running_loop()

        def stop() -> None:
            log.warning("signal received — finishing the current cycle then stopping")
            self.running = False

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop)
            except NotImplementedError:      # Windows
                signal.signal(sig, lambda *_: stop())

    def desk(self):
        """The TradingDesk instance if one is configured, else None."""
        alpha = self.engine.alpha
        models = getattr(alpha, "models", [alpha])
        for m in models:
            if getattr(m, "name", "") == "desk":
                return m
        return None

    def status(self) -> dict:
        pf = self.engine.ctx.portfolio
        desk = self.desk()
        return {
            "strategy": self.config.name,
            "mode": self.config.mode.value,
            "running": self.running,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_bar": self.last_bar_ts.isoformat() if self.last_bar_ts else None,
            "consecutive_errors": self.errors,
            "universe": [s.ticker for s in self.engine.ctx.universe],
            "market": {
                "calendar": getattr(self.calendar, "name", None),
                "open": self.calendar.is_open(datetime.now(UTC)) if self.calendar else None,
                "minutes_to_open": round(
                    self.calendar.minutes_until_open(datetime.now(UTC)), 1
                ) if self.calendar else None,
                "stale_warning": self.calendar.check_freshness() if self.calendar else "",
            },
            "engine": self.engine.summary(),
            "portfolio": pf.snapshot(),
            "desk": desk.status() if desk is not None else None,
        }


async def run_live(config: StrategyConfig, state_path: str = "quant_state.db") -> None:
    trader = LiveTrader(config, state_path)
    trader.install_signal_handlers()
    await trader.run()
