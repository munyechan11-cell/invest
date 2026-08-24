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
        #: 오늘 같은 계정의 *다른* run 이 남긴 중단 — `adopt_halt` 참고.
        #: `_halted_reason` 과 일부러 자리를 나눠 둡니다. `to_state()` 가
        #: 내보내는 것은 `_halted_reason` 뿐이라, 이 run 의 원장 행에는 이 run 이
        #: 스스로 만든 사유만 남습니다. 한 자리에 합치면 이어받은 사유가 run 마다
        #: 복제되고, 그러면 실제로 한도를 넘긴 run 을 해제해도 사본들이 계정을
        #: 계속 막습니다.
        self._carried_halt = ""
        #: 이어받은 중단이 걸린 거래일. 원장과 따로 들고 있어야 하는 이유는
        #: `roll()` 의 주석 참고.
        self._carried_halt_day: date | None = None
        #: durable store, once one is bound — see `bind_store`.
        self._store = None

    # ── day boundary ─────────────────────────────────────────────────────
    def _now(self) -> datetime:
        return self.clock.now() if self.clock is not None else datetime.now(UTC)

    def local_day(self, now: datetime | None = None) -> date:
        return ((now or self._now()) + self.tz_offset).date()

    def roll(self, now: datetime | None = None, equity: float = 0.0) -> DayLedger:
        day = self.local_day(now)
        # 이어받은 중단은 그것이 걸린 거래일에만 유효합니다. 아래 날짜 전환
        # 분기와 따로 확인해야 하는 이유: 거래가 한 건도 없으면 `self.today` 는
        # 계속 None 이라 그 분기를 지나가지 않습니다. 그러면 장 마감 뒤 켜 둔
        # 봇이 다음 날 아침 첫 주문에서 어제 중단에 막힙니다.
        if self._carried_halt and day != self._carried_halt_day:
            self._carried_halt = ""
            self._carried_halt_day = None
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

        in_force = self._halted_reason or self._carried_halt
        if in_force:
            return False, in_force

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
        return bool(self._halted_reason or self._carried_halt)

    def adopt_halt(self, source_strategy: str,
                   now: datetime | None = None) -> bool:
        """오늘 같은 계정의 다른 전략이 이미 중단됐다는 사실만 이어받습니다.

        `resume_run(전략, 모드)` 이 전략 이름으로 run 을 찾기 때문에, 전략만
        바꿔 켜면 원장이 빈 새 run 이 열립니다. 그래서 "다음 거래일까지 신규
        진입 중단" 이 다음 거래일이 아니라 **다음 전략까지만** 유지됐습니다 —
        손실 중단을 무력화하는 데 재배포조차 필요 없고, 목록에서 다른 전략을
        고르기만 하면 됐습니다.

        이어받는 것은 중단 사실 하나뿐입니다. 거래대금·주문 건수·손익·
        `starting_equity` 는 일부러 가져오지 않습니다 — 상태 파일 하나에 kis·
        토스·바이낸스처럼 통화도 계좌도 다른 run 이 섞여 있어 합산이 회계상
        의미가 없고, 특히 `starting_equity` 를 물려받는 순간 비율 손실 한도의
        분모가 남의 자산이 됩니다.

        언제 이게 안 통하는가:

        · 시간대(`timezone_offset_hours`)를 바꿔 켜면 이어받지 않습니다.
          '오늘' 이 가리키는 구간이 달라져 같은 날이라고 말할 수 없기
          때문이고, `load_state` 도 같은 이유로 복원을 거부합니다.
        · 한도 **직전까지** 쓰고 전략을 바꾸면 남은 여유는 그대로 다시
          얻습니다. 이어받는 것은 중단이지 사용량이 아닙니다.
        """
        if not self.halt_until_next_day:
            # 이 봇은 "걸린 주문만 막고 하루를 잠그지는 말라" 고 명시했습니다
            # (`_halt()` 이 같은 플래그를 지킵니다). 자기 힘으로는 만들 수 없는
            # 래치를 남의 run 이 대신 걸어 줄 수는 없습니다.
            return False
        if not self.configured:
            # 일일 한도를 하나도 걸지 않은 봇 — 이 안전장치를 쓰지 않겠다는
            # 뜻이므로, 남의 중단을 근거로 세울 자리가 없습니다.
            return False
        if self.halted:
            return False
        self._carried_halt_day = self.local_day(now)
        self._carried_halt = (
            f"오늘 이 계정의 다른 전략({source_strategy})이 일일 한도로 "
            "중단되었습니다 — 다음 거래일까지 신규 진입 중단"
        )
        # 원본 사유 문자열은 일부러 옮겨 적지 않습니다. 거기에는 다른 계좌의,
        # 다른 통화의 금액이 박혀 있어서 이 봇의 화면에 그대로 띄우면 자기
        # 원장(손익 0)과 정면으로 모순되는 숫자가 됩니다.
        log.warning(self._carried_halt)
        return True

    def release(self) -> None:
        """Operator override — waives today's caps entirely.

        Deliberately all-or-nothing for the day rather than a partial top-up:
        any "grant a bit more" rule would be an arbitrary number pretending to
        be a policy. The safeguards that remain are that it is explicit,
        logged, and gone tomorrow.
        """
        if self._carried_halt and not self._halted_reason:
            # 이 봇 자신은 아무 한도도 넘기지 않았습니다. 넘긴 것은 오늘 같은
            # 계정의 다른 전략이고, 여기서 지워야 하는 것도 그 사실 하나뿐입니다.
            # `released` 까지 세우면 이 봇의 거래대금·건수·손실 한도가 하루 종일
            # 함께 풀립니다 — 운영자가 누른 버튼이 뜻한 것보다 훨씬 넓습니다.
            log.warning("운영자가 이어받은 중단을 해제했습니다 (%s). "
                        "이 봇 자신의 일일 한도는 그대로입니다.", self._carried_halt)
            self._carried_halt = ""
            self._carried_halt_day = None
            self._forget_stored_halts()
            return
        # Mark the ledger the checks are actually using. Calling roll() with
        # wall-clock time here would silently start a *new* day whenever the
        # engine's clock differs from the machine's — which both resets the
        # counters and releases a day nobody was trading.
        ledger = self.today or self.roll()
        ledger.released = True
        log.warning("운영자가 %s 의 일일 한도를 해제했습니다 (사유: %s). "
                    "내일 자동으로 복구됩니다.",
                    ledger.day, self._halted_reason or "중단 없음")
        was_halted = bool(self._halted_reason)
        self._halted_reason = ""
        self._persist()
        if was_halted:
            # 중단이 걸려 있지 않았다면 지울 것도 없습니다. 조건 없이 부르면
            # 아무 봇에서나 해제를 누르는 것이 계정의 중단 기록을 조용히
            # 치우는 버튼이 됩니다.
            self._forget_stored_halts()

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
        try:
            self._store.save_budget(self)
        except Exception:
            # Never let a bookkeeping failure stop the trading path, but say so
            # loudly: from here the caps only hold until the next restart.
            self._store = None
            log.exception("일일 한도 저장 실패 — 재시작하면 오늘 사용량이 초기화됩니다")

    def _forget_stored_halts(self) -> None:
        """해제된 중단 사유를 원장에서도 지웁니다 — 해제가 이어받기의 역연산이 되도록.

        메모리에서만 지우면, 재시작한 다음 run 이 같은 행을 다시 읽어 또
        이어받습니다. 운영자에게는 봇을 껐다 켤 때마다 해제가 되돌아오는 것으로
        보이고, 사유를 남긴 run 은 화면에 보이지도 않아 손댈 방법이 없습니다.

        지우는 것은 사유 문자열뿐이고 사용량 숫자는 그대로 둡니다. 그래서 실제로
        한도를 넘긴 run 은 다시 켜면 자기 원장으로 곧바로 다시 중단됩니다 —
        사라지는 것은 "오늘 누가 넘었다" 는 표시이지 넘었다는 사실이 아닙니다.
        """
        forget = getattr(self._store, "release_day_halts", None)
        if forget is None:
            return
        try:
            forget(self)
        except Exception:
            log.exception("중단 해제 기록 실패 — 재시작하면 오늘 중단이 되살아납니다")

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
            "halt_reason": self._halted_reason or self._carried_halt,
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
