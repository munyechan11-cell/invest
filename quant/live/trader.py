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
from quant.core.engine import Engine, _insight_dict
from quant.core.events import Event, EventType
from quant.core.types import UTC, Bar, RunMode
from quant.data.provider import DataProvider, gather_history
from quant.live.notifier import TelegramNotifier
from quant.live.spend import SpendMeter
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
        meter: SpendMeter | None = None,
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
        # 이 봇이 쓴 LLM 을 누구 앞으로 다는가. 단일 사용자 배포에서는 셀
        # 사람이 없으므로 None 입니다.
        self.meter = meter
        # 휴장 중 심의 주기. 데스크 설정에서 읽고, 없으면 한 시간에 한 번입니다.
        # 매번 돌면 밤새 열일곱 번이라 비용이 봉당 심의보다 커집니다.
        spec = next((m for m in config.alpha if m.type in ("desk", "council")), None)
        minutes = float((spec.params.get("closed_cadence_minutes", 60) if spec else 0) or 0)
        self.closed_cadence_s = max(minutes, 5.0) * 60 if minutes else 0.0
        self._last_closed_deliberation = 0.0
        # 마지막 심의 시도가 어떻게 됐는지. 화면이 "계속 심의합니다" 라고 써
        # 놓고 아무것도 안 뜨면 그건 거짓말입니다 — 안 된 이유를 남겨야
        # 그 자리에 대신 쓸 말이 생깁니다.
        self.desk_note: str = ""
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
        # This must precede resume_run. Otherwise a different Toss strategy's
        # old clean run can be resumed without reaching start_run's account-wide
        # defense, bypassing both an unresolved run and its same-KST-day cooldown.
        # It performs SQLite reads only: no warm-up, broker connect, or order call.
        if cfg.mode is RunMode.LIVE and cfg.broker.type == "toss":
            self.state.assert_toss_account_start_allowed()
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
        if isinstance(self.engine.brokerage, LiveBrokerage):
            # This is a durable StateStore fact, not a guess from the cash value.
            # A first-ever real-account adoption may import existing holdings;
            # a resumed venue ledger must instead explain every quantity/cash
            # difference before the process is allowed to overwrite it.
            self.engine.brokerage.expect_restored_venue_truth(
                resumed and self.state.restored_venue_truth,
                reconciliation_required=(
                    resumed and self.state.restored_reconciliation_required
                ),
            )
        # Bind the ledger whether or not we resumed. A fresh run has to survive
        # its own first crash too, and a daily cap that a restart clears is not
        # a cap — it is a cap plus a reset button the failure mode presses.
        self.state.restore_budget(self.engine.budget, datetime.now(UTC))
        capital_source_before_connect = self.engine.ctx.portfolio.capital_source
        # Commit the crash quarantine before the broker can perform any live
        # activity. Only Engine.stop's verified no-open-order path reaches
        # ``stop_run`` and clears it again.
        if (isinstance(self.engine.brokerage, LiveBrokerage)
                and self.engine.brokerage.uses_venue_capital):
            self.state.mark_reconciliation_required()
        await self.engine.start()
        portfolio = self.engine.ctx.portfolio
        if (capital_source_before_connect != "venue"
                and portfolio.capital_source == "venue"):
            # A legacy run's day ledger may also carry the configured 800k as
            # its percentage-loss denominator. Correct it exactly once, when a
            # valid venue snapshot replaces the configured source; ordinary
            # restarts retain the day's original, already-real baseline.
            ledger = self.engine.budget.roll(datetime.now(UTC), portfolio.equity)
            ledger.starting_equity = portfolio.equity
            self.state.save_budget(self.engine.budget)
        # Persist the venue baseline before announcing the bot as running. If a
        # deploy lands before the first candle, the next process must still know
        # that configured starting_cash is not live account truth.
        self.state.snapshot_positions(portfolio)
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

        돌려받은 인사이트는 **장부에 넣되 주문은 내지 않습니다.** 인사이트는
        "이렇게 보인다" 이고, 그것이 주문이 되려면 다음 `on_bars` 에서
        포트폴리오 구성과 리스크를 한 번 더 거쳐야 합니다. 그 사이가 사람이
        "예약" 이라고 부르는 구간입니다 — 지금 판단하고, 장이 열리면 나갑니다.

        여기서 곧바로 매매까지 하면 "시작 버튼이 곧 시장가 주문" 이 되고,
        그건 사람이 누른 것과 다른 일입니다.
        """
        await self._deliberate_now("개장 전")

    async def _closed_market_deliberation(self) -> None:
        """장이 닫혀 있는 동안에도 주기적으로 심의한다.

        정규장이 닫혔다고 판단할 것이 없어지지는 않습니다 — 소수점 주문이나
        주간거래처럼 정규장 밖에서 도는 것도 있고, 무엇보다 "내일 무엇을 살
        것인가" 는 밤에 정하는 것입니다. 장이 열릴 때까지 화면이 비어 있으면
        사람은 봇이 고장 난 줄 압니다.

        비용이 드는 일이라 시계로 제한합니다. 매번 도는 것이 아니라
        `closed_cadence_minutes` 마다 한 번이고, 계량기가 거절하면 쉽니다.
        """
        if not self.closed_cadence_s:
            return
        now = time.monotonic()
        if now - self._last_closed_deliberation < self.closed_cadence_s:
            return
        self._last_closed_deliberation = now
        await self._deliberate_now("휴장 중")

    async def _deliberate_now(self, reason: str) -> None:
        """봉을 기다리지 않고 지금 한 번 심의한다. 주문은 내지 않는다.

        여기서 조용히 돌아가는 길이 여럿입니다 — 데스크가 없거나, 한도에
        걸렸거나, 볼 봉이 없거나. 그때마다 `desk_note` 에 이유를 남깁니다.
        화면이 "계속 심의합니다" 라고 써 놓고 아무것도 안 뜨면 그건 거짓말이고,
        사용자는 무엇을 고쳐야 할지 알 방법이 없습니다.
        """
        desk = self.desk()
        if desk is None:
            self.desk_note = "이 전략에는 AI 데스크가 없습니다"
            return
        # 이 심의는 사람이 ▶ 시작 을 누를 때마다 한 번씩 나갑니다. 껐다 켜기를
        # 반복하면 그만큼 반복되고, 데스크의 `cost_limit_usd` 는 봇을 새로
        # 세울 때마다 0 부터 다시 세므로 그것으로는 막히지 않습니다. 요금제
        # 한도를 여기서도 물어봅니다 — 다중 사용자 서비스에서 계량되지 않는
        # LLM 호출 경로는 결국 운영자 카드로 청구됩니다.
        if self.meter is not None:
            allowed, why = self.meter.allow()
            if not allowed:
                log.info("%s 심의 건너뜀 — %s", reason, why)
                self.desk_note = f"심의를 쉬는 중 — {why}"
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
            self.desk_note = ("볼 봉이 없습니다 — 유니버스가 비었거나 시세를 "
                              "아직 받지 못했습니다")
            return
        if desk.status().get("disabled_reason"):
            self.desk_note = desk.status()["disabled_reason"]
            return
        log.info("%s 심의 — 봉을 기다리지 않고 지금 상태로 한 번 봅니다", reason)
        before = desk.status()["llm_calls"], desk.estimated_cost_usd
        try:
            fresh = await desk.update(ctx, last)
            if fresh:
                # 장부에 넣습니다. 주문은 여기서 나가지 않습니다 — 다음
                # `on_bars` 가 포트폴리오 구성과 리스크를 거쳐 만들어 냅니다.
                # 그 사이가 사람이 "예약" 이라고 부르는 구간입니다.
                self.engine.insights.add(fresh)
                self.engine.ledger.record(ctx, fresh)
                self.desk_note = ""
                for ins in fresh:
                    await ctx.bus.publish(EventType.INSIGHT, _insight_dict(ins))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 여기서 죽으면 봇이 아예 안 뜹니다. 첫인상보다 돌아가는 쪽이
            # 중요하므로, 실패는 알리고 넘어갑니다.
            log.warning("%s 심의 실패: %s", reason, exc)
            self.desk_note = f"{reason} 심의 실패 — {exc}"
            await ctx.bus.publish(EventType.ERROR,
                                  {"error": f"{reason} 심의 실패: {exc}"})
        finally:
            # 실패했어도 부른 만큼은 청구됩니다. 성공만 계량하면 실패한 호출의
            # 비용이 아무 계정에도 잡히지 않습니다.
            after_calls = desk.status()["llm_calls"]
            if after_calls == before[0] and not self.desk_note:
                # 호출이 한 번도 안 나갔습니다. 데스크는 살아 있는데 이번
                # 봉에서는 아무 종목도 새로 볼 것이 없었다는 뜻입니다
                # (같은 봉은 한 번만 심의합니다).
                self.desk_note = ("이 봉은 이미 심의했습니다 — 다음 봉이 "
                                  "닫히면 다시 봅니다")
            if self.meter is not None:
                calls = max(0, after_calls - before[0])
                spent = max(0.0, desk.estimated_cost_usd - before[1])
                if calls:
                    self.meter.record(calls, spent)

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
        try:
            await self.start()
            if self._stop is not None and self._stop.is_set():
                # A signal during warm-up: start() has just set running = True, so
                # without this the stop would be forgotten until the next signal.
                self.running = False
            tf = self.config.data.timeframe
            while self.running:
                if await self._wait_for_market():
                    continue
                wake = next_candle_close(datetime.now(UTC), tf, lag=3.0)
                sleep_for = (wake - datetime.now(UTC)).total_seconds()
                if sleep_for > 0:
                    log.debug("sleeping %.1fs until %s", sleep_for, wake.isoformat())
                    # 봉 하나를 통째로 자면 그 사이 사람이 누른 매수·매도가
                    # 다음 봉까지 대기합니다. 일봉이면 내일이고, 그건 수동매매가
                    # 아닙니다. 짧게 끊어 자면서 대기열을 비웁니다.
                    if not await self._sleep_serving_manual(sleep_for):
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
            if self.started_at is None:
                await self._cleanup_failed_start()
            else:
                await self.shutdown()

    #: 수동 주문 대기열을 얼마나 자주 비우는가. 사람이 버튼을 누르고 이만큼은
    #: 기다릴 수 있지만, 그보다 길면 "안 눌렸나" 싶어 다시 누릅니다.
    MANUAL_FLUSH_S = 5.0

    async def _sleep_serving_manual(self, seconds: float) -> bool:
        """자되, 그 사이 수동 주문은 제때 내보낸다. 정지 신호가 오면 False."""
        left = seconds
        while left > 0:
            chunk = min(left, self.MANUAL_FLUSH_S)
            if not await self._sleep(chunk):
                return False
            left -= chunk
            if not self.running:
                return False
            try:
                sent = await self.engine.flush_manual()
                if sent:
                    log.info("수동 주문 %d건 즉시 제출", sent)
            except asyncio.CancelledError:
                raise
            except Exception as exc:      # noqa: BLE001 — 루프는 살립니다
                log.warning("수동 주문 제출 실패: %s", exc)
                await self.engine.ctx.bus.publish(
                    EventType.ERROR, {"error": f"수동 주문 제출 실패: {exc}"})
        return True

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
        # 장이 닫혀 있어도 판단할 것은 남아 있습니다 — "내일 무엇을 살 것인가"
        # 는 밤에 정하는 것이고, 그 결정이 장부에 있어야 개장하자마자 나갑니다.
        await self._closed_market_deliberation()
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
            fills_pre_settled = False
            if isinstance(self.engine.brokerage, LiveBrokerage):
                # One cumulative fill is visible through both the order and
                # holdings endpoints. Book the order fill first, then let venue
                # truth correct the final account. Reversing these two steps
                # applies the same trade twice (qty/cash), while polling again
                # inside on_bars would duplicate fee/trade events.
                await self.engine.settle_live_fills()
                fills_pre_settled = True
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
            await self.engine.on_bars(bars, settle=not fills_pre_settled)
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
    async def _cleanup_failed_start(self) -> None:
        """Release every resource acquired before startup became runnable.

        This path never cancels or submits orders.  It exists for failures in
        warm-up, account reconciliation, or broker connect, before normal
        ``shutdown`` is eligible to run.
        """
        if self._stopped:
            return
        self._stopped = True
        self.running = False
        log.warning("startup failed — releasing %s resources", self.config.name)
        for label, close in (
            ("broker", self.engine.brokerage.close),
            ("notifier", self.notifier.close),
            ("provider", self.provider.close),
        ):
            try:
                await close()
            except Exception:  # noqa: BLE001 — cleanup must continue
                log.exception("could not close %s after startup failure", label)
        try:
            # Do not stamp somebody else's active run if acquisition itself was
            # what failed. A fresh/resumed run we successfully claimed should
            # be visibly stopped before ownership is released.
            # Do not let a config/broker change become an escape hatch. The
            # marker describes the prior run, not the adapter selected for this
            # failed retry; only a fully started run that later passes Engine.stop
            # may clear it.
            preserve_quarantine = self.state.restored_reconciliation_required
            if self.state._owns and not preserve_quarantine:
                self.state.stop_run()
            elif self.state._owns:
                self.state.record_event(
                    "reconciliation_required",
                    {
                        "manual_verification_required": True,
                        "recovery": (
                            "토스 앱에서 당일 체결·미체결 주문과 실제 보유수량·현금, "
                            "일일 손실 한도를 대조한 뒤 기존 상태를 보관하고 새 실행으로 "
                            "초기화하세요"
                        ),
                    },
                )
        except Exception:  # noqa: BLE001 — ownership release still comes next
            log.exception("could not mark failed startup as stopped")
        finally:
            try:
                self.state.close()
            except Exception:  # noqa: BLE001 — best-effort final release
                log.exception("could not close state after startup failure")

    async def shutdown(self) -> None:
        if self._stopped or (not self.running and self.started_at is None):
            return
        # Set before the first await: a signal arriving mid-flush must not start
        # a second one against a half-closed state store.
        self._stopped = True
        self.running = False
        log.warning("shutting down %s", self.config.name)
        shutdown_error: BaseException | None = None
        try:
            await self.engine.stop()
        except BaseException as exc:  # cancellation/uncertainty is never a clean stop
            shutdown_error = exc
        finally:
            try:
                self.state.snapshot_positions(self.engine.ctx.portfolio)
                self.state.save_locks(self.engine.ctx.export_locks())
                self.state.save_pins(self.engine.ctx.pinned)
            except Exception as exc:  # noqa: BLE001 — still release every resource
                log.exception("could not persist the final live state")
                if shutdown_error is None:
                    shutdown_error = exc

            if shutdown_error is None:
                self.state.stop_run()
                try:
                    await self.notifier.send(
                        f"⏹ stopped {self.config.name}\n"
                        f"equity {self.engine.ctx.portfolio.equity:,.2f} "
                        f"({self.engine.ctx.portfolio.total_return:+.2%})"
                    )
                except Exception:  # noqa: BLE001 — notification is not settlement
                    log.exception("could not notify the clean shutdown")
            else:
                unsafe = (
                    "안전한 종료를 확인하지 못해 정상 종료로 기록하지 않았습니다. "
                    "토스 앱에서 미체결 주문과 당일 체결을 확인하고, 실제 보유수량과 "
                    "일일 손실 한도를 대조한 뒤 다시 시작하세요. "
                    f"원인: {shutdown_error}"
                )
                log.critical("%s", unsafe)
                try:
                    self.state.record_event(
                        "unsafe_shutdown",
                        {"error": str(shutdown_error)[:1000],
                         "manual_verification_required": True},
                    )
                except Exception:  # noqa: BLE001 — preserve the original failure
                    log.exception("could not persist the unsafe shutdown event")
                try:
                    await self.notifier.send(unsafe)
                except Exception:  # noqa: BLE001 — preserve the original failure
                    log.exception("could not notify the unsafe shutdown")

            for label, close in (
                ("notifier", self.notifier.close),
                ("provider", self.provider.close),
            ):
                try:
                    await close()
                except Exception:  # noqa: BLE001 — best-effort final release
                    log.exception("could not close %s during shutdown", label)
            try:
                self.state.close()
            except Exception:  # noqa: BLE001 — best-effort final release
                log.exception("could not close state during shutdown")
        if shutdown_error is not None:
            raise shutdown_error

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

    def _queued(self) -> list[dict]:
        """개장하면 검토될 판단들. 장이 열려 있으면 빈 목록입니다."""
        if self.calendar is None or self.calendar.is_open(datetime.now(UTC)):
            return []
        now = self.engine.ctx.now
        out = []
        for ins in self.engine.insights.active(now):
            out.append({
                "ticker": ins.symbol.ticker,
                "direction": getattr(ins.direction, "value", str(ins.direction)),
                "source": ins.source,
                "confidence": round(float(ins.confidence or 0), 3),
                "expires_at": ins.close_time.isoformat(),
            })
        return out

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
            # 장이 닫혀 있는 동안 내려진 판단. 주문은 아직 아니고, 개장하면
            # 포트폴리오 구성과 리스크를 거쳐 나갑니다 — 사람이 "예약" 이라고
            # 부르는 것이 이것입니다. 무엇이 나갈지 미리 보여야, 밤새 쌓인
            # 결정이 아침에 갑자기 체결되는 일이 없습니다.
            "queued": self._queued(),
            # 마지막 심의 시도가 왜 아무것도 못 남겼는지. 잘 돌았으면 빈 문자열.
            "desk_note": self.desk_note,
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
