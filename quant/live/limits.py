"""일일 거래 한도 — the budget the bot cannot talk its way past.

Every other limit in this engine is a *strategy* limit: position weight,
leverage, drawdown. They all assume the strategy is behaving. This one assumes
it is not.

A bug in a signal, a bad config, a venue returning stale prices — the failure
mode is always the same shape: the bot trades far more than a human would in a
day, and nobody notices until the statement arrives. The budget is deliberately
dumb and sits below everything else, at the brokerage, where it sees the actual
orders rather than the intentions.

Three independent caps, because they fail differently:

  · **거래대금** caps churn. Hit first by a signal oscillating on noise.
  · **주문 건수** caps loops. Hit first by a genuine bug — a retry storm, a
    state machine that never advances.
  · **손실** caps the damage. Hit first when the strategy is simply wrong today.

Any one of them tripping stops *new* exposure. None of them ever blocks an
exit: a limit that traps you in a losing position is not a safety feature.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from quant.core.types import UTC, Fill, Order

log = logging.getLogger("quant.limits")


@dataclass
class DayLedger:
    """One trading day's usage."""

    day: date
    notional: float = 0.0
    orders: int = 0
    realized_pnl: float = 0.0
    fees: float = 0.0
    starting_equity: float = 0.0
    blocked: int = 0
    #: operator waived today's caps. Resets with the day, never persists.
    released: bool = False

    def to_dict(self) -> dict:
        return {
            "date": self.day.isoformat(),
            "released": self.released,
            "notional": round(self.notional, 2),
            "orders": self.orders,
            "realized_pnl": round(self.realized_pnl, 2),
            "fees": round(self.fees, 2),
            "blocked": self.blocked,
        }


class TradingBudget:
    """Per-day caps on turnover, order count and loss.

    `timezone_offset_hours` decides when "today" rolls over. It defaults to
    Korean market time because a budget that resets at UTC midnight would reset
    in the middle of the KRX session — halfway through the very day it is
    supposed to be bounding.
    """

    def __init__(
        self,
        max_daily_notional: float = 0.0,
        max_daily_orders: int = 0,
        max_daily_loss: float = 0.0,
        max_daily_loss_pct: float = 0.0,
        timezone_offset_hours: float = 9.0,
        halt_until_next_day: bool = True,
        clock=None,
    ):
        numeric_limits = (
            max_daily_notional, max_daily_orders, max_daily_loss,
            max_daily_loss_pct, timezone_offset_hours,
        )
        if not all(math.isfinite(float(value)) for value in numeric_limits):
            raise ValueError("일일 한도와 시간대는 유한한 숫자여야 합니다")
        if abs(float(timezone_offset_hours)) > 24:
            raise ValueError("일일 한도 시간대는 UTC-24..UTC+24 범위여야 합니다")
        self.max_notional = max_daily_notional
        self.max_orders = max_daily_orders
        self.max_loss = abs(max_daily_loss)
        self.max_loss_pct = abs(max_daily_loss_pct)
        self.tz_offset = timedelta(hours=timezone_offset_hours)
        self.halt_until_next_day = halt_until_next_day
        #: The engine's clock. Without it every call that omits `now` reads the
        #: wall clock, and in a backtest that is a different day from the bar
        #: being simulated — so the ledger flips between the simulated day and
        #: today's date on alternate calls and resets its counters each time.
        #: A daily cap that resets every bar is not a daily cap.
        self.clock = clock
        self.today: DayLedger | None = None
        self.history: list[DayLedger] = []
        self._halted_reason = ""
        #: durable store, once one is bound — see `bind_store`.
        self._store = None

    # ── day boundary ─────────────────────────────────────────────────────
    def _now(self) -> datetime:
        return self.clock.now() if self.clock is not None else datetime.now(UTC)

    def local_day(self, now: datetime | None = None) -> date:
        return ((now or self._now()) + self.tz_offset).date()

    def roll(self, now: datetime | None = None, equity: float = 0.0) -> DayLedger:
        day = self.local_day(now)
        if self.today is None:
            self.today = DayLedger(day, starting_equity=equity)
        elif self.today.day != day:
            self.history.append(self.today)
            if len(self.history) > 400:
                del self.history[:200]
            log.info("일일 한도 초기화: %s → %s (전일 거래대금 %.0f, 주문 %d건, 손익 %+.0f)",
                     self.today.day, day, self.today.notional, self.today.orders,
                     self.today.realized_pnl)
            self.today = DayLedger(day, starting_equity=equity)
            self._halted_reason = ""
            self._persist()
        elif equity and not self.today.starting_equity:
            self.today.starting_equity = equity
        return self.today

    # ── the check ────────────────────────────────────────────────────────
    def check(self, order: Order, price: float, is_reducing: bool,
              now: datetime | None = None, equity: float = 0.0) -> tuple[bool, str]:
        """(allowed, reason). Reducing orders are always allowed."""
        ledger = self.roll(now, equity)

        # An exit is never blocked. A cap that traps you in a losing position
        # has stopped being a safety feature.
        if is_reducing:
            return True, ""

        if ledger.released:
            # The operator explicitly waived today's caps. Clearing only the
            # halt flag would not work — the counters are still over the line,
            # so the very next check would re-halt and the release button would
            # appear to do nothing.
            return True, ""

        if self._halted_reason:
            return False, self._halted_reason

        notional = abs(float(order.quantity)) * price * float(order.symbol.multiplier)

        if self.max_orders and ledger.orders >= self.max_orders:
            return self._halt(f"일일 주문 건수 한도 {self.max_orders}건 도달")
        if self.max_notional and ledger.notional + notional > self.max_notional:
            return False, (
                f"일일 거래대금 한도 초과: 이미 {ledger.notional:,.0f} 사용, "
                f"이 주문 {notional:,.0f}, 한도 {self.max_notional:,.0f}"
            )
        if self.max_loss and ledger.realized_pnl <= -self.max_loss:
            return self._halt(
                f"일일 손실 한도 도달: {ledger.realized_pnl:+,.0f} (한도 -{self.max_loss:,.0f})")
        if self.max_loss_pct and ledger.starting_equity > 0:
            loss_pct = -ledger.realized_pnl / ledger.starting_equity
            if loss_pct >= self.max_loss_pct:
                return self._halt(
                    f"일일 손실 한도 도달: {loss_pct:.2%} (한도 {self.max_loss_pct:.2%})")
        return True, ""

    def _halt(self, reason: str) -> tuple[bool, str]:
        if self.halt_until_next_day and not self._halted_reason:
            self._halted_reason = reason + " — 다음 거래일까지 신규 진입 중단"
            log.warning(self._halted_reason)
        if self.today is not None:
            self.today.blocked += 1
        self._persist()
        return False, self._halted_reason or reason

    # ── bookkeeping ──────────────────────────────────────────────────────
    def record_order(self, order: Order, price: float,
                     now: datetime | None = None) -> None:
        ledger = self.roll(now)
        ledger.orders += 1
        ledger.notional += abs(float(order.quantity)) * price * float(
            order.symbol.multiplier)
        self._persist()

    def record_fill(self, fill: Fill, now: datetime | None = None) -> None:
        ledger = self.roll(now)
        ledger.fees += fill.fee
        self._persist()

    def record_trade(self, pnl: float, now: datetime | None = None) -> None:
        self.roll(now).realized_pnl += pnl
        self._persist()

    def blocked_count(self) -> int:
        return self.today.blocked if self.today else 0

    # ── reporting ────────────────────────────────────────────────────────
    @property
    def halted(self) -> bool:
        return bool(self._halted_reason)

    def release(self) -> None:
        """Operator override — waives today's caps entirely.

        Deliberately all-or-nothing for the day rather than a partial top-up:
        any "grant a bit more" rule would be an arbitrary number pretending to
        be a policy. The safeguards that remain are that it is explicit,
        logged, and gone tomorrow.
        """
        # Mark the ledger the checks are actually using. Calling roll() with
        # wall-clock time here would silently start a *new* day whenever the
        # engine's clock differs from the machine's — which both resets the
        # counters and releases a day nobody was trading.
        ledger = self.today or self.roll()
        ledger.released = True
        log.warning("운영자가 %s 의 일일 한도를 해제했습니다 (사유: %s). "
                    "내일 자동으로 복구됩니다.",
                    ledger.day, self._halted_reason or "중단 없음")
        self._halted_reason = ""
        self._persist()

    # ── durability ───────────────────────────────────────────────────────
    def bind_store(self, store) -> None:
        """Write the ledger through to `store` on every change.

        Binding rather than snapshotting on a timer is deliberate: an order can
        go out between two snapshots, and a ledger that is one order behind at
        the moment of the crash is exactly the order a restart would let
        through twice.
        """
        self._store = store

    def _persist(self) -> None:
        if self._store is None:
            return
        store = self._store
        try:
            store.save_budget(self)
        except Exception:
            # Never let a bookkeeping failure stop the trading path, but say so
            # loudly. The store keeps a sticky uncertainty marker, so even if a
            # later write recovers, shutdown cannot call this a clean run.
            mark_failed = getattr(
                store, "mark_accounting_persistence_failed", None,
            )
            if mark_failed is not None:
                mark_failed()
            self._store = None
            log.exception(
                "일일 한도 저장 실패 — 이번 실행은 정상 종료로 표시하지 않고 "
                "수동 계좌 대조가 필요한 격리 상태로 남깁니다"
            )

    def to_state(self) -> dict:
        """Today's ledger as a plain dict, for the state DB."""
        if self.today is None:
            return {}
        return {
            "day": self.today.day.isoformat(),
            "notional": self.today.notional,
            "orders": self.today.orders,
            "realized_pnl": self.today.realized_pnl,
            "fees": self.today.fees,
            "starting_equity": self.today.starting_equity,
            "blocked": self.today.blocked,
            "halt_reason": self._halted_reason,
            "tz_offset_hours": self.tz_offset.total_seconds() / 3600,
        }

    def load_state(self, state: dict, now: datetime | None = None) -> bool:
        """Adopt a stored ledger if it is still today's. Returns whether it was.

        Call this once, before trading resumes. The day guard is the whole
        point: yesterday's usage must not bound today, and today's must not be
        forgotten just because the process died. `released` is deliberately not
        carried over — an operator waiving the caps is a decision about a
        running bot, not a standing setting, so a restart re-halts and asks
        again. That is the safe direction to be wrong in.
        """
        if not state:
            return False
        stored_tz = float(state.get("tz_offset_hours") or 0.0)
        if abs(stored_tz - self.tz_offset.total_seconds() / 3600) > 1e-9:
            log.warning("일일 한도 복원 취소: 저장 시점의 시간대(UTC%+g)가 현재 설정(UTC%+g)과 "
                        "다릅니다 — '오늘'의 범위가 달라져 그대로 쓸 수 없습니다",
                        stored_tz, self.tz_offset.total_seconds() / 3600)
            return False
        try:
            day = date.fromisoformat(str(state["day"]))
        except (KeyError, TypeError, ValueError):
            log.warning("일일 한도 복원 취소: 저장된 날짜를 읽을 수 없습니다 (%r)",
                        state.get("day"))
            return False
        today = self.local_day(now)
        if day != today:
            log.info("저장된 일일 한도는 %s 자 — 오늘(%s)은 새 한도로 시작합니다", day, today)
            return False

        self.today = DayLedger(
            day,
            notional=float(state.get("notional") or 0.0),
            orders=int(state.get("orders") or 0),
            realized_pnl=float(state.get("realized_pnl") or 0.0),
            fees=float(state.get("fees") or 0.0),
            starting_equity=float(state.get("starting_equity") or 0.0),
            blocked=int(state.get("blocked") or 0),
        )
        self._halted_reason = state.get("halt_reason") or ""
        if self._halted_reason:
            log.warning("복원: 일일 한도 중단 상태 — %s", self._halted_reason)
        log.info("복원: %s 일일 사용량 (거래대금 %.0f, 주문 %d건, 손익 %+.0f)",
                 day, self.today.notional, self.today.orders, self.today.realized_pnl)
        return True

    def status(self, now: datetime | None = None) -> dict:
        ledger = self.roll(now)
        def pct(used, cap):
            return round(used / cap * 100, 1) if cap else None
        return {
            "date": ledger.day.isoformat(),
            "halted": self.halted,
            "released": ledger.released,
            "halt_reason": self._halted_reason,
            "notional": {"used": round(ledger.notional, 2),
                         "limit": self.max_notional or None,
                         "used_pct": pct(ledger.notional, self.max_notional)},
            "orders": {"used": ledger.orders, "limit": self.max_orders or None,
                       "used_pct": pct(ledger.orders, self.max_orders)},
            "loss": {"realized_pnl": round(ledger.realized_pnl, 2),
                     "limit": self.max_loss or None,
                     "limit_pct": self.max_loss_pct or None},
            "blocked_orders": ledger.blocked,
            "fees": round(ledger.fees, 2),
        }

    @property
    def configured(self) -> bool:
        return bool(self.max_notional or self.max_orders or self.max_loss
                    or self.max_loss_pct)
