"""The live trading loop.

Sleeps until each candle closes, fetches the newly settled bars, and hands them
to the same `Engine.on_bars` a backtest uses. Everything that is different about
live trading — retries, reconnects, position reconciliation, persistence,
graceful shutdown — lives here so the strategy layer stays identical.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from datetime import datetime

from quant.brokerage.live_base import LiveBrokerage
from quant.config.schema import StrategyConfig
from quant.core.clock import RealClock, next_candle_close
from quant.core.engine import Engine
from quant.core.events import Event, EventType
from quant.core.types import UTC, Bar, RunMode
from quant.data.provider import DataProvider, gather_history
from quant.live.notifier import TelegramNotifier
from quant.live.state import StateStore
from quant.strategy.builder import build_engine

log = logging.getLogger("quant.live")


class LiveTrader:
    #: How often a sleeping loop re-checks `running`. The stop *event* wakes it
    #: instantly, but `/api/trader/stop` only clears the flag, so the poll is
    #: what makes that path prompt too.
    stop_poll_s = 1.0

    def __init__(
        self,
        config: StrategyConfig,
        state_path: str = "quant_state.db",
        resume: bool = True,
        max_consecutive_errors: int = 10,
        profile_path: str | None = None,
    ):
        if config.mode is RunMode.BACKTEST:
            raise ValueError("LiveTrader needs mode: dry_run or live")
        from quant.strategy.builder import apply_investor_profile

        # The saved investor profile fills in anything the config left at its
        # defaults — sizing, stops, daily caps — before the engine is built.
        config = apply_investor_profile(config, profile_path)
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
        # Built on the running loop rather than here: a trader is constructed
        # outside async context (CLI, API) and an Event binds to a loop eagerly.
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._stopped = False

    # ── wiring ───────────────────────────────────────────────────────────
    def _attach_observers(self) -> None:
        bus = self.engine.ctx.bus

        async def persist(event: Event) -> None:
            payload = event.payload or {}
            if event.type is EventType.ORDER_FILLED:
                self.state.record_event("order_filled", payload)
            elif event.type is EventType.TRADE_CLOSED:
                self.state.record_event("trade_closed", payload)
                # 이벤트만 남기면 매매 기록과 기간별 실현손익이 영원히 빕니다 —
                # 그쪽은 events 가 아니라 trades 테이블을 읽습니다.
                self.state.record_closed_trade(payload)
            elif event.type is EventType.EQUITY:
                self.state.record_equity(
                    datetime.now(UTC), payload.get("equity", 0.0),
                    payload.get("cash", 0.0), payload.get("drawdown_pct", 0.0) / 100,
                )
                self.state.snapshot_positions(self.engine.ctx.portfolio)
                self.state.save_locks(self.engine.ctx.export_locks())
                self.state.save_pins(self.engine.ctx.pinned)
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
        # The benchmark is warmed like everything else but stays out of the
        # universe: unpriced, the attribution panel's '초과' column is the raw
        # return wearing a benchmark's name.
        bench = ctx.benchmark
        to_fetch = list(symbols)
        if bench is not None and bench.key not in {s.key for s in symbols}:
            to_fetch.append(bench)
        series = await gather_history(self.provider, to_fetch,
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
            # 사용자가 읽는 문장입니다. 영어 한 줄이면 "왜 안 되는지 모르겠다"
            # 로 끝나고, 실제로 그렇게 끝났습니다.
            names = ", ".join(s.ticker for s in symbols[:6])
            raise RuntimeError(
                f"시세를 받지 못해 시작할 수 없습니다 ({names}). "
                f"증권사 키가 맞는지, 그 계좌로 시세 조회 권한이 있는지 "
                f"확인하세요. 장 시간이 아니거나 거래소가 응답하지 않을 때도 "
                f"이렇게 됩니다 — 잠시 후 다시 시도해 보세요.")
        self.engine.set_universe(usable)

        if bench is not None and bench.key not in {s.key for s in usable}:
            bars = series.get(bench.key, [])
            if len(bars) < 10:
                log.warning("벤치마크 %s 데이터 없음 — 리포트의 '초과'는 벤치마크 대비가 "
                            "아니라 원수익률입니다", bench.ticker)
            else:
                ctx.seed_history(bench, bars)
                ctx.portfolio.mark(bench, bars[-1].close)
                self._seen[bench.key] = bars[-1].ts

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
        resumed = self.resume and self.state.resume_run(cfg.name, cfg.mode.value)
        if not resumed:
            self.state.start_run(cfg.name, cfg.mode.value, cfg.portfolio.starting_cash,
                                 cfg.model_dump_json())

        self._attach_observers()
        await self.warmup()

        # Restore *after* warm-up, so every symbol in the stored book is known
        # and a position in a name that has since left the universe is still
        # reported rather than silently dropped.
        symbols = {s.key: s for s in self.engine.ctx.universe}
        if resumed:
            restored = self.state.restore_positions(self.engine.ctx.portfolio, symbols)
            self.engine.ctx.import_locks(self.state.restore_locks(datetime.now(UTC)))
            pinned = self.state.restore_pins(self.engine.ctx, symbols)
            log.info("run %s 복원: 포지션 %d건, 핀 %d건", self.state.run_id,
                     restored, pinned)
        # Bind the ledger whether or not we resumed. A fresh run has to survive
        # its own first crash too, and a daily cap that a restart clears is not
        # a cap — it is a cap plus a reset button the failure mode presses.
        self.state.restore_budget(self.engine.budget, datetime.now(UTC))
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
        await self._opening_deliberation()

    async def _opening_deliberation(self) -> None:
        """시작하자마자 데스크를 한 번 돌린다 — 봉 하나를 더 기다리지 않고.

        일봉 전략에서 "다음 봉" 은 다음 장 마감입니다. 시작 버튼을 누른
        사람에게 그건 사실상 "내일 다시 오세요" 이고, 그동안 화면에는 "대기
        중" 만 뜹니다. 열 분쯤 보고 나면 고장으로 읽는 것이 당연합니다 —
        실제로 그렇게 읽혔습니다.

        기다릴 이유가 없습니다. 워밍업이 끝났으니 지표는 이미 데워졌고 마지막
        봉도 손에 있습니다. 데스크가 지금 무엇을 보는지는 **지금** 말할 수
        있습니다.

        돌려받은 인사이트는 **버립니다.** 주문은 봉이 닫힐 때 나가야 합니다 —
        여기서 매매까지 시작하면 "시작 버튼이 곧 시장가 주문" 이 되고, 그건
        사람이 누른 것과 다른 일입니다. 발언만 화면에 올립니다(그 발행은
        `desk.update()` 가 스스로 버스에 실어 보냅니다).
        """
        desk = self.desk()
        if desk is None:
            return
        ctx = self.engine.ctx
        # 워밍업이 남긴 마지막 봉. 없으면 심의할 재료가 없는 것이고, 그건
        # 유니버스가 비었다는 뜻이라 여기서 할 말이 없습니다.
        last = {}
        for symbol in ctx.universe:
            history = ctx.history(symbol)
            if history:
                last[symbol.key] = history[-1]
        if not last:
            return
        log.info("개장 전 심의 — 봉을 기다리지 않고 지금 상태로 한 번 봅니다")
        try:
            await desk.update(ctx, last)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 여기서 죽으면 봇이 아예 안 뜹니다. 첫인상보다 돌아가는 쪽이
            # 중요하므로, 실패는 알리고 넘어갑니다.
            log.warning("개장 전 심의 실패: %s", exc)
            await ctx.bus.publish(EventType.ERROR,
                                  {"error": f"개장 전 심의 실패: {exc}"})

    # ── stopping ─────────────────────────────────────────────────────────
    @property
    def stopping(self) -> bool:
        """A stop has been asked for but the flush has not finished yet."""
        return not self.running and self.started_at is not None and not self._stopped

    def _stop_event(self) -> asyncio.Event:
        if self._stop is None:
            self._stop = asyncio.Event()
        return self._stop

    def request_stop(self) -> None:
        """Finish the current cycle and exit. Safe from a signal handler."""
        self.running = False
        self._stop_event().set()

    async def _sleep(self, seconds: float) -> bool:
        """Sleep unless a stop arrives. Returns False if one did.

        The wait is sliced instead of one long timer because that timer is what
        a stop has to cut through: `docker stop` allows 10s before SIGKILL, and
        on a 1d timeframe a bare sleep parks the loop for up to a full day —
        so every redeploy would land mid-cycle with the state flush skipped.
        """
        stop = self._stop_event()
        deadline = time.monotonic() + seconds
        while self.running and not stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            try:
                await asyncio.wait_for(stop.wait(),
                                       timeout=min(remaining, self.stop_poll_s))
            except asyncio.TimeoutError:
                continue
        return False

    # ── the loop ─────────────────────────────────────────────────────────
    async def run(self) -> None:
        self._task = asyncio.current_task()
        await self.start()
        if self._stop is not None and self._stop.is_set():
            # A signal during warm-up: start() has just set running = True, so
            # without this the stop would be forgotten until the next signal.
            self.running = False
        tf = self.config.data.timeframe
        try:
            while self.running:
                if await self._wait_for_market():
                    continue
                wake = next_candle_close(datetime.now(UTC), tf, lag=3.0)
                sleep_for = (wake - datetime.now(UTC)).total_seconds()
                if sleep_for > 0:
                    log.debug("sleeping %.1fs until %s", sleep_for, wake.isoformat())
                    if not await self._sleep(sleep_for):
                        break
                if not self.running:
                    break
                await self._tick()
        except asyncio.CancelledError:
            # A second signal, or the API shutting the loop down. Swallowed on
            # purpose: the flush below is the reason we were cancelled at all.
            log.warning("cancelled mid-cycle — flushing state and exiting")
            self.running = False
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
            await self._sleep(3600)
            return True
        wait_s = max((nxt - datetime.now(UTC)).total_seconds(), 0)
        if not self._announced_closed:
            log.info("%s 휴장 — %s 개장까지 %.1f시간 대기",
                     self.calendar.name, nxt.isoformat(), wait_s / 3600)
            self._announced_closed = True
        # Wake periodically rather than sleeping for hours in one go, so a stop
        # signal is honoured promptly and a clock jump cannot strand the loop.
        if not await self._sleep(min(wait_s + 2, 300)):
            return True
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
            if isinstance(self.engine.brokerage, LiveBrokerage):
                # Cheap insurance: reconcile every cycle so drift is caught in
                # minutes rather than after a bad restart.
                #
                # **봉이 없어도** 맞춰 봅니다. 대조는 봉이 아니라 증권사 쪽
                # 장부를 읽는 일이라 새 봉과 아무 상관이 없고, 정작 대조가
                # 필요한 날은 조용한 날입니다 — 재시작으로 상태가 어긋났거나
                # 시세가 안 올 때. 아래 조기 반환 뒤에 두었더니 그런 날에만
                # 골라서 건너뛰었습니다.
                await self.engine.brokerage.sync()
            if not bars:
                log.debug("no new closed bars this cycle")
                return
            await self.engine.on_bars(bars)
            self.last_bar_ts = max(b.ts for b in bars.values())
            self.errors = 0
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
                seen.add(pos.symbol.key)
        bench = self.engine.ctx.benchmark
        if bench is not None and bench.key not in seen and self._seen.get(bench.key):
            watched.append(bench)                # priced every cycle, never traded
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
        if self._stopped or (not self.running and self.started_at is None):
            return
        # Set before the first await: a signal arriving mid-flush must not start
        # a second one against a half-closed state store.
        self._stopped = True
        self.running = False
        log.warning("shutting down %s", self.config.name)
        try:
            await self.engine.stop()
        finally:
            self.state.snapshot_positions(self.engine.ctx.portfolio)
            self.state.save_locks(self.engine.ctx.export_locks())
            self.state.save_pins(self.engine.ctx.pinned)
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
            if self._stop is not None and self._stop.is_set():
                # Asked twice: stop waiting on whatever the cycle is stuck in
                # (a hung fetch, a broker that never answers). run() catches the
                # cancellation so the state flush still happens.
                log.warning("second signal — cancelling the current cycle")
                if self._task is not None:
                    self._task.cancel()
                return
            log.warning("signal received — finishing the current cycle then stopping")
            self.request_stop()

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
            "stopping": self.stopping,
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
