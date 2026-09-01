"""The `Context` object every model receives.

One read-mostly handle onto "the world as of now": time, the universe, bar
history, the portfolio, and a place to stash per-symbol state. Models never
reach for globals, which is what makes them composable and unit-testable.

Crucially, `history()` can only return bars strictly before `now` — the object
itself enforces the no-look-ahead rule so individual models cannot get it wrong.
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from quant.core.account import Portfolio
from quant.core.clock import Clock
from quant.core.events import EventBus
from quant.core.types import Bar, ClosedTrade, Quote, RunMode, Symbol, timeframe_delta

log = logging.getLogger("quant.context")

# Venue clocks can lead the application clock slightly. The same tolerance is
# used by mark selection and LiveTrader validation so an accepted quote cannot
# be discarded silently during sizing.
QUOTE_FUTURE_TOLERANCE = timedelta(seconds=5)


class Context:
    def __init__(
        self,
        clock: Clock,
        portfolio: Portfolio,
        bus: EventBus,
        timeframe: str = "1d",
        run_mode: RunMode = RunMode.BACKTEST,
        history_size: int = 800,
        params: dict[str, Any] | None = None,
    ):
        self.clock = clock
        self.portfolio = portfolio
        self.bus = bus
        self.timeframe = timeframe
        self.run_mode = run_mode
        self.params = params or {}
        self.universe: list[Symbol] = []
        self.history_size = history_size
        self._bars: dict[str, deque[Bar]] = defaultdict(lambda: deque(maxlen=history_size))
        self._quotes: dict[str, Quote] = {}
        self._state: dict[str, dict[str, Any]] = defaultdict(dict)
        self._locks: dict[str, tuple[datetime, str]] = {}
        self._pending: dict[str, Any] = {}
        self._pending_order_keys: set[str] = set()
        self._pinned: dict[str, str] = {}
        self.benchmark: Symbol | None = None
        # Engine wires the active brokerage here so the execution layer can
        # query narrow venue capabilities without hard-coding venue names.
        self.brokerage = None

    # ── time ─────────────────────────────────────────────────────────────
    @property
    def now(self) -> datetime:
        return self.clock.now()

    @property
    def bar_delta(self) -> timedelta:
        return timeframe_delta(self.timeframe)

    @property
    def is_live(self) -> bool:
        return self.run_mode is RunMode.LIVE

    # ── market data ──────────────────────────────────────────────────────
    def push_bar(self, bar: Bar) -> None:
        self._bars[bar.symbol.key].append(bar)

    def seed_history(self, symbol: Symbol, bars: Iterable[Bar]) -> None:
        buf = self._bars[symbol.key]
        buf.clear()
        buf.extend(bars)

    def history(self, symbol: Symbol, count: int | None = None) -> list[Bar]:
        """Closed bars for `symbol`, oldest first, never including the future."""
        now = self.now
        bars = [b for b in self._bars.get(symbol.key, ()) if b.end_ts <= now]
        return bars[-count:] if count else bars

    def latest(self, symbol: Symbol) -> Bar | None:
        bars = self.history(symbol, 1)
        return bars[0] if bars else None

    def closes(self, symbol: Symbol, count: int | None = None) -> list[float]:
        return [b.close for b in self.history(symbol, count)]

    def price(self, symbol: Symbol) -> float:
        """Best available mark without letting future/live data into backtests."""
        q = self.quote(symbol)
        if q is not None:
            try:
                age = self.now - q.ts
            except (TypeError, ValueError):
                age = None
            if (age is not None
                    and -QUOTE_FUTURE_TOLERANCE <= age < self.bar_delta):
                return q.mid
        bar = self.latest(symbol)
        return bar.close if bar else 0.0

    def set_quote(self, quote: Quote) -> None:
        self._quotes[quote.symbol.key] = quote

    def quote(self, symbol: Symbol) -> Quote | None:
        # Backtests are a closed-bar simulation. A quote can be left on a reused
        # Context by a caller, but it is neither historical evidence nor safe
        # fill input there. LiveTrader validates wall-clock freshness at each
        # decision/send boundary; keeping the raw live quote available lets that
        # validator explain whether it is missing, future, or stale.
        if self.run_mode is RunMode.BACKTEST:
            return None
        return self._quotes.get(symbol.key)

    def has_data(self, symbol: Symbol, minimum: int) -> bool:
        return len(self.history(symbol)) >= minimum

    # ── portfolio shortcuts ──────────────────────────────────────────────
    @property
    def equity(self) -> float:
        return self.portfolio.equity

    def position_qty(self, symbol: Symbol):
        return self.portfolio.quantity(symbol)

    # ── projected holdings ───────────────────────────────────────────────
    def set_pending(self, pending: dict[str, Decimal]) -> None:
        """Signed quantity currently resting in unfilled orders, per symbol."""
        self._pending = dict(pending)
        # Preserve keys even when opposite open orders net to zero. Quantity
        # alone cannot prove that a symbol has no live venue order.
        self._pending_order_keys = set(pending)

    def has_pending_order(self, symbol: Symbol) -> bool:
        return symbol.key in self._pending_order_keys

    def pending_quantity(self, symbol: Symbol) -> Decimal:

        return self._pending.get(symbol.key, Decimal("0"))

    def projected_quantity(self, symbol: Symbol) -> Decimal:
        """Position you will hold if every resting order fills.

        This — not the filled position — is what a target must be diffed
        against. Sizing off the filled quantity while an order rests means the
        same order goes out again every bar, and a limit that sits for three
        bars becomes three times the intended position.
        """
        return self.portfolio.quantity(symbol) + self.pending_quantity(symbol)

    def is_invested(self, symbol: Symbol) -> bool:
        return self.portfolio.quantity(symbol) != 0

    def recent_trades(self, symbol: Symbol | None = None, within: timedelta | None = None
                      ) -> list[ClosedTrade]:
        trades = self.portfolio.closed_trades
        if symbol is not None:
            trades = [t for t in trades if t.symbol.key == symbol.key]
        if within is not None:
            cutoff = self.now - within
            trades = [t for t in trades if t.exit_ts >= cutoff]
        return trades

    # ── per-symbol scratch state for models ──────────────────────────────
    def state(self, owner: str) -> dict[str, Any]:
        return self._state[owner]

    # ── operator pins ────────────────────────────────────────────────────
    def pin(self, symbol: Symbol, reason: str = "") -> None:
        """Take a symbol out of the strategy's hands.

        A manually opened position has no insight behind it, so the portfolio
        model would compute a target of zero and sell it straight back on the
        next bar — the operator's trade undone within a minute. Pinning tells
        the portfolio layer to leave it alone. Risk models still apply: a pin
        overrides the strategy's opinion, not the stop-loss.
        """
        self._pinned[symbol.key] = reason or "operator pinned"
        log.info("pinned %s — %s", symbol.ticker, self._pinned[symbol.key])

    def unpin(self, symbol: Symbol) -> None:
        if self._pinned.pop(symbol.key, None) is not None:
            log.info("unpinned %s — back under strategy control", symbol.ticker)

    def is_pinned(self, symbol: Symbol) -> bool:
        return symbol.key in self._pinned

    @property
    def pinned(self) -> dict[str, str]:
        return dict(self._pinned)

    # ── trading locks (freqtrade's pair locks) ───────────────────────────
    def lock(self, symbol: Symbol, until: datetime, reason: str) -> None:
        current = self._locks.get(symbol.key)
        if current is None or until > current[0]:
            self._locks[symbol.key] = (until, reason)
            log.info("locked %s until %s — %s", symbol.ticker, until.isoformat(), reason)

    def lock_all(self, until: datetime, reason: str) -> None:
        self._locks["*"] = (until, reason)

    def unlock(self, symbol: Symbol) -> None:
        self._locks.pop(symbol.key, None)

    def unlock_all(self) -> None:
        self._locks.clear()

    def is_locked(self, symbol: Symbol) -> tuple[bool, str]:
        now = self.now
        for key in ("*", symbol.key):
            entry = self._locks.get(key)
            if entry and entry[0] > now:
                return True, entry[1]
            if entry:
                self._locks.pop(key, None)
        return False, ""

    def export_locks(self) -> dict[str, tuple[datetime, str]]:
        """Raw lock map, for persistence across a restart."""
        now = self.now
        return {k: v for k, v in self._locks.items() if v[0] > now}

    def import_locks(self, locks: dict[str, tuple[datetime, str]]) -> None:
        self._locks.update(locks)

    def active_locks(self) -> dict[str, dict]:
        now = self.now
        return {
            k: {"until": v[0].isoformat(), "reason": v[1]}
            for k, v in self._locks.items()
            if v[0] > now
        }
