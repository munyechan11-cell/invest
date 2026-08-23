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
from datetime import timedelta

from quant.core.context import Context
from quant.core.types import ClosedTrade, Symbol

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
        until = ctx.now + ctx.bar_delta * self.stop_bars
        if self.per_symbol:
            candidates = {t.symbol.key: t.symbol for t in self._recent(ctx, None)}
            for sym in candidates.values():
                triggered, reason = self.check(ctx, sym)
                if triggered:
                    ctx.lock(sym, until, f"{self.name}: {reason}")
                    events.append({"protection": self.name, "symbol": sym.ticker,
                                   "reason": reason, "until": until.isoformat()})
        else:
            triggered, reason = self.check(ctx, None)
            if triggered:
                ctx.lock_all(until, f"{self.name}: {reason}")
                events.append({"protection": self.name, "symbol": "*",
                               "reason": reason, "until": until.isoformat()})
        return events

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
        if self.stops_only:
            hits = [t for t in recent if t.was_stopped_out]
            label = "stop-outs"
        else:
            hits = [t for t in recent if t.pnl_pct < self.required_profit]
            label = "losing trades"
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

    def check(self, ctx, symbol):
        trades = self._recent(ctx, symbol)
        if not trades:
            return False, ""
        last = max(trades, key=lambda t: t.exit_ts)
        if self.only_after_loss and last.pnl >= 0:
            return False, ""
        elapsed = (ctx.now - last.exit_ts) / ctx.bar_delta
        if elapsed < self.stop_bars:
            return True, f"cooling down {self.stop_bars - elapsed:.0f} more bars after exit"
        return False, ""


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
        total = sum(t.pnl_pct for t in trades)
        if total < self.required_profit:
            return True, f"{len(trades)} trades netting {total:+.2%} over the window"
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
    """Runs every protection once per bar and reports what fired."""

    def __init__(self, *protections: Protection):
        self.protections = list(protections)

    def apply(self, ctx: Context) -> list[dict]:
        events: list[dict] = []
        for p in self.protections:
            try:
                events.extend(p.apply(ctx))
            except Exception:
                log.exception("protection %s raised", p.name)
        for e in events:
            log.info("protection fired: %s", e)
        return events


BUILTIN_PROTECTIONS = {
    "stoploss_guard": StoplossGuard,
    "cooldown": CooldownPeriod,
    "low_profit": LowProfitPairs,
    "max_drawdown": MaxDrawdownProtection,
}
