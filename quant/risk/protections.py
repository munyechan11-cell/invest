"""Protections — circuit breakers that lock trading after bad outcomes.

Borrowed wholesale from freqtrade, because they encode the operational lesson
that a strategy's *worst* behaviour is what determines whether you can actually
run it: a model that is right on average but loses eight trades in a row will be
switched off by its operator long before the average arrives.

Protections do not modify targets directly. They set locks on the context, and
`TradingLockGate` (a risk model) enforces them. Keeping them separate means a
protection can never accidentally prevent an exit.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime

from quant.core.context import Context
from quant.core.types import ClosedTrade, Symbol, one_line_error

log = logging.getLogger("quant.protections")


class Protection(ABC):
    name = "protection"
    #: True = lock only the offending symbol; False = lock the whole book
    per_symbol = True

    def __init__(self, lookback_bars: int = 60, stop_bars: int = 12):
        self.lookback_bars = lookback_bars
        self.stop_bars = stop_bars

    @abstractmethod
    def check(self, ctx: Context, symbol: Symbol | None) -> tuple[bool, str]:
        """Return (should_lock, reason)."""

    def apply(self, ctx: Context) -> list[dict]:
        """Evaluate and set locks. Returns whatever it triggered, for the log."""
        events: list[dict] = []
        if self.per_symbol:
            candidates = {t.symbol.key: t.symbol for t in self._recent(ctx, None)}
            for sym in candidates.values():
                triggered, reason = self.check(ctx, sym)
                if triggered:
                    until = self.lock_until(ctx, sym)
                    ctx.lock(sym, until, f"{self.name}: {reason}")
                    events.append({"protection": self.name, "symbol": sym.ticker,
                                   "reason": reason, "until": until.isoformat()})
        else:
            triggered, reason = self.check(ctx, None)
            if triggered:
                until = self.lock_until(ctx, None)
                ctx.lock_all(until, f"{self.name}: {reason}")
                events.append({"protection": self.name, "symbol": "*",
                               "reason": reason, "until": until.isoformat()})
        return events

    def lock_until(self, ctx: Context, symbol: Symbol | None) -> datetime:
        """When a lock set on this bar should expire.

        Measured from now, because most protections read the state of the book
        *as of this bar* and the wait starts here. A protection whose window is
        anchored to a past event has to override this: `apply` runs every bar,
        and re-deriving the expiry from `ctx.now` while the condition still
        holds walks the lock forward one bar at a time instead of letting it
        run out.
        """
        return ctx.now + ctx.bar_delta * self.stop_bars

    def _recent(self, ctx: Context, symbol: Symbol | None) -> list[ClosedTrade]:
        return ctx.recent_trades(symbol, within=ctx.bar_delta * self.lookback_bars)


class StoplossGuard(Protection):
    """Lock after N stop-outs inside the lookback window.

    Repeated stops are the signature of a regime the strategy does not
    understand; stepping aside for a while is cheaper than paying tuition on
    every bar.
    """

    name = "stoploss_guard"

    def __init__(self, lookback_bars: int = 60, trade_limit: int = 4,
                 stop_bars: int = 12, only_per_symbol: bool = False,
                 required_profit: float = 0.0, stops_only: bool = True):
        super().__init__(lookback_bars, stop_bars)
        self.trade_limit = trade_limit
        self.per_symbol = only_per_symbol
        self.required_profit = required_profit
        #: Count only risk-forced exits. A run of small losses closed by the
        #: strategy's own signal is ordinary; a run of *stop-outs* means the
        #: setup is misreading the regime, which is what this guard is for.
        self.stops_only = stops_only

    def check(self, ctx, symbol):
        recent = self._recent(ctx, symbol)
        # The two settings answer different questions and compose: `stops_only`
        # picks which kind of exit counts, `required_profit` how bad it has to
        # be. Applying the threshold in both branches also fixes a guard that
        # was too eager — `was_stopped_out` counts a *trailing* stop, so an exit
        # that locked in a gain was evidence against the strategy. With the
        # shipped `trade_limit: 3`, a good run of trailing stops was enough to
        # halt a book that was working.
        hits = [t for t in recent if t.pnl_pct < self.required_profit]
        label = "losing trades"
        if self.stops_only:
            hits = [t for t in hits if t.was_stopped_out]
            label = "stop-outs"
        if len(hits) >= self.trade_limit:
            reasons = {t.exit_reason for t in hits}
            return True, (
                f"{len(hits)} {label} in {self.lookback_bars} bars "
                f"(limit {self.trade_limit}) — {', '.join(sorted(reasons))}"
            )
        return False, ""


class CooldownPeriod(Protection):
    """Bar re-entry for a few bars after any exit.

    Prevents the classic failure where a strategy stops out and immediately
    re-buys the same bar's noise, paying two spreads for nothing.
    """

    name = "cooldown"
    per_symbol = True

    def __init__(self, stop_bars: int = 3, only_after_loss: bool = False):
        super().__init__(lookback_bars=stop_bars + 1, stop_bars=stop_bars)
        self.only_after_loss = only_after_loss

    def _last_exit(self, ctx, symbol) -> ClosedTrade | None:
        # Only a real exit starts a cooldown. Scaling out realises PnL and is
        # recorded as a trade, but the strategy still holds the name — treating
        # a trim as an exit locks it out of a position it is in the middle of
        # managing.
        trades = [t for t in self._recent(ctx, symbol) if t.closes_position]
        if not trades:
            return None
        return max(trades, key=lambda t: t.exit_ts)

    def check(self, ctx, symbol):
        last = self._last_exit(ctx, symbol)
        if last is None:
            return False, ""
        if self.only_after_loss and last.pnl >= 0:
            return False, ""
        elapsed = (ctx.now - last.exit_ts) / ctx.bar_delta
        if elapsed < self.stop_bars:
            return True, f"cooling down {self.stop_bars - elapsed:.0f} more bars after exit"
        return False, ""

    def lock_until(self, ctx, symbol):
        # Anchored to the exit, not to now. This is a fixed wait after leaving a
        # name, and `check` keeps returning True for every bar of that wait — so
        # an expiry measured from `ctx.now` would be rewritten one bar later on
        # each pass and hold the symbol for roughly twice `stop_bars`.
        last = self._last_exit(ctx, symbol)
        if last is None:
            return super().lock_until(ctx, symbol)
        return last.exit_ts + ctx.bar_delta * self.stop_bars


class LowProfitPairs(Protection):
    """Stop trading instruments whose recent aggregate P&L is negative.

    Different from `StoplossGuard`: a symbol can bleed steadily without ever
    tripping a stop count, and death by a thousand cuts is still death.
    """

    name = "low_profit"
    per_symbol = True

    def __init__(self, lookback_bars: int = 120, stop_bars: int = 24,
                 min_trades: int = 3, required_profit: float = 0.0):
        super().__init__(lookback_bars, stop_bars)
        self.min_trades = min_trades
        self.required_profit = required_profit

    def check(self, ctx, symbol):
        trades = self._recent(ctx, symbol)
        if len(trades) < self.min_trades:
            return False, ""
        # Weighted by the capital each trade actually put at risk. Adding raw
        # percentage returns answers no question anyone asked: a 40% loss on a
        # starter position and a 40% gain on a full one sum to zero and were
        # not remotely a wash. Keeping the result a fraction also keeps
        # `required_profit` readable — 0.0 is break-even, -0.02 is "down 2%".
        deployed = sum(float(t.quantity) * t.entry_price for t in trades)
        if deployed <= 0:
            return False, ""
        total = sum(t.pnl for t in trades) / deployed
        if total < self.required_profit:
            return True, (f"{len(trades)} trades netting {total:+.2%} on capital "
                          f"over the window")
        return False, ""


class MaxDrawdownProtection(Protection):
    """Global halt when the equity curve drops too far inside the window."""

    name = "max_drawdown"
    per_symbol = False

    def __init__(self, lookback_bars: int = 120, max_drawdown: float = 0.12,
                 stop_bars: int = 24, min_trades: int = 5):
        super().__init__(lookback_bars, stop_bars)
        self.max_drawdown = abs(max_drawdown)
        self.min_trades = min_trades

    def check(self, ctx, symbol):
        curve = ctx.portfolio.equity_curve[-self.lookback_bars:]
        if len(curve) < 5 or len(ctx.portfolio.closed_trades) < self.min_trades:
            return False, ""
        peak, dd = curve[0].equity, 0.0
        for point in curve:
            peak = max(peak, point.equity)
            if peak > 0:
                dd = max(dd, 1.0 - point.equity / peak)
        if dd >= self.max_drawdown:
            return True, f"window drawdown {dd:.2%} >= {self.max_drawdown:.2%}"
        return False, ""


class ProtectionManager:
    """Runs every protection once per bar and reports what fired.

    A protection that raises is not a protection that cleared. One that keeps
    raising is not a protection at all — and this repository has already paid
    for that distinction once: a signal generator lost its API quota, failed
    every cycle for three days, logged each failure and quietly fell back. The
    logs were all there. Nobody read them for three days.

    So a failure is reported like any other event — it reaches the operator's
    notifier on the same channel a firing protection does — and a protection
    that has failed on `broken_after_bars` consecutive bars stops new entries
    across the book. Not on the first one: a single raise is more likely a data
    glitch than a broken breaker, and halting a working book on a hiccup is its
    own kind of loss.

    Other protections keep running throughout. One that crashes must not take
    the working ones down with it.
    """

    def __init__(self, *protections: Protection, broken_after_bars: int = 3):
        self.protections = list(protections)
        self.broken_after_bars = broken_after_bars
        self._failing: dict[str, int] = {}

    def apply(self, ctx: Context) -> list[dict]:
        events: list[dict] = []
        for p in self.protections:
            try:
                fired = p.apply(ctx)
            except Exception as exc:
                events.append(self._failed(ctx, p, exc))
            else:
                events.extend(fired)
                if self._failing.pop(p.name, 0):
                    log.info("보호장치 %s 이(가) 다시 동작합니다", p.name)
        for e in events:
            # A crash is already logged with its traceback in `_failed`; saying
            # "fired" about it here would mislead whoever greps this file
            # during an incident.
            if not e.get("failed"):
                log.info("protection fired: %s", e)
        return events

    def _failed(self, ctx: Context, p: Protection, exc: Exception) -> dict:
        """Record one failure, and halt if this protection is simply broken.

        The halt is left to expire on its own once the protection recovers.
        Lifting it here would mean `unlock_all`, which clears every lock in the
        book including ones other protections set for their own reasons.
        """
        bars = self._failing[p.name] = self._failing.get(p.name, 0) + 1
        log.exception("보호장치 %s 이(가) 예외로 멈췄습니다 (%d봉 연속)", p.name, bars)
        reason = (f"보호장치 {p.name} 이(가) {bars}봉 연속 실패했습니다 "
                  f"— {one_line_error(exc)}")
        event = {"protection": p.name, "symbol": "*", "failed": True,
                 "bars": bars, "reason": reason}
        if bars >= self.broken_after_bars:
            until = ctx.now + ctx.bar_delta * p.stop_bars
            halt = f"{reason} — 안전장치가 없는 상태라 신규 진입을 멈춥니다"
            ctx.lock_all(until, halt)
            event["reason"] = halt
            event["until"] = until.isoformat()
        return event


BUILTIN_PROTECTIONS = {
    "stoploss_guard": StoplossGuard,
    "cooldown": CooldownPeriod,
    "low_profit": LowProfitPairs,
    "max_drawdown": MaxDrawdownProtection,
}
