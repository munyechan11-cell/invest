"""동적 유니버스 — what to trade, decided each cycle rather than hand-written.

Ported from Freqtrade's pairlist design, which gets one thing importantly
right: the universe is a **chain**, not a list. The first link *generates*
candidates (every market on the exchange, the top N by turnover, a fixed list),
and every subsequent link only ever *removes* them. That ordering is what makes
the chain safe to extend — a new filter can never smuggle an untradable
instrument into the book.

Why this matters more than it looks: most of the ways a strategy quietly loses
money are universe problems, not signal problems. A pair that listed three days
ago has no history for your indicators. A pair with an 80bp spread eats the
whole edge on entry. A pair that has not moved 2% in a month cannot pay for a
round trip. None of these show up as a bug; they show up as a slow bleed.

Every filter states *why* it dropped something, and the reasons are kept for
the log — a universe that shrinks silently is impossible to debug at 3am.
"""
from __future__ import annotations

import logging
import math
import random
import statistics
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from quant.core.context import Context
from quant.core.types import Bar, Symbol, periods_per_year, timeframe_delta
from quant.data.provider import DataProvider

log = logging.getLogger("quant.universe")


@dataclass
class SelectionReport:
    """What the chain did, and why. Kept for the operator, not for the engine."""

    candidates: int = 0
    selected: list[str] = field(default_factory=list)
    dropped: dict[str, list[str]] = field(default_factory=dict)   # filter -> tickers
    reasons: dict[str, str] = field(default_factory=dict)         # ticker -> reason
    elapsed_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "candidates": self.candidates,
            "selected": self.selected,
            "selected_count": len(self.selected),
            "dropped": {k: v for k, v in self.dropped.items() if v},
            "reasons": self.reasons,
            "elapsed_s": round(self.elapsed_s, 2),
        }

    def summary(self) -> str:
        drops = ", ".join(f"{k} -{len(v)}" for k, v in self.dropped.items() if v)
        return (f"universe {self.candidates} → {len(self.selected)}"
                + (f" ({drops})" if drops else ""))


# ─────────────────────────────────────────────────────────────────────────────
# Sources — the first link, which generates candidates
# ─────────────────────────────────────────────────────────────────────────────
class UniverseSource(ABC):
    name = "source"

    @abstractmethod
    async def candidates(self, ctx: Context, provider: DataProvider) -> list[Symbol]:
        ...


class StaticSource(UniverseSource):
    """The hand-written list. Still the right answer for a focused book."""

    name = "static"

    def __init__(self, symbols: list[Symbol]):
        self.symbols = list(symbols)

    async def candidates(self, ctx, provider):
        return list(self.symbols)


class ExchangeSource(UniverseSource):
    """Every market the venue lists, optionally narrowed by quote currency.

    Needs a provider that can enumerate its markets; providers that cannot
    return nothing rather than pretending, and the chain says so.
    """

    name = "exchange"

    def __init__(self, quote_currency: str = "", max_symbols: int = 500,
                 exclude: list[str] | None = None):
        self.quote = quote_currency.upper()
        self.max_symbols = max_symbols
        self.exclude = {e.upper() for e in (exclude or [])}

    async def candidates(self, ctx, provider):
        lister = getattr(provider, "available_symbols", None)
        if lister is None:
            log.warning("%s cannot enumerate markets — exchange source yields nothing",
                        provider.name)
            return []
        symbols = await lister()
        out = [
            s for s in symbols
            if (not self.quote or s.quote_currency.upper() == self.quote)
            and s.ticker.upper() not in self.exclude
        ]
        return out[: self.max_symbols]


# ─────────────────────────────────────────────────────────────────────────────
# Filters — every subsequent link, which may only remove
# ─────────────────────────────────────────────────────────────────────────────
class UniverseFilter(ABC):
    name = "filter"
    #: how many bars of history this filter needs to judge a symbol
    lookback = 0

    @abstractmethod
    async def apply(self, ctx: Context, symbols: list[Symbol],
                    report: SelectionReport) -> list[Symbol]:
        ...

    def _drop(self, report: SelectionReport, symbol: Symbol, reason: str) -> None:
        report.dropped.setdefault(self.name, []).append(symbol.ticker)
        report.reasons[symbol.ticker] = f"{self.name}: {reason}"


class _HistoryFilter(UniverseFilter):
    """Base for filters that judge from bar history already in the context."""

    def bars(self, ctx: Context, symbol: Symbol) -> list[Bar]:
        return ctx.history(symbol, self.lookback) if self.lookback else ctx.history(symbol)


class AgeFilter(_HistoryFilter):
    """Drop instruments without enough history to warm an indicator.

    The most common cause of a strategy's first live trades being nonsense: a
    freshly listed pair whose 200-period average is computed from 12 bars.
    """

    name = "age"

    def __init__(self, min_bars: int = 200):
        self.min_bars = min_bars
        self.lookback = min_bars + 5

    async def apply(self, ctx, symbols, report):
        out = []
        for s in symbols:
            n = len(ctx.history(s))
            if n < self.min_bars:
                self._drop(report, s, f"{n} bars < {self.min_bars} required")
            else:
                out.append(s)
        return out


class VolumeFilter(_HistoryFilter):
    """Rank by traded value and keep the top N above a floor.

    Value, not share count: 10m shares of a ₩1,200 stock is a different market
    from 10m shares of a ₩800,000 one.
    """

    name = "volume"

    def __init__(self, lookback_bars: int = 20, top_n: int = 0,
                 min_value: float = 0.0):
        self.lookback = lookback_bars
        self.top_n = top_n
        self.min_value = min_value

    async def apply(self, ctx, symbols, report):
        scored: list[tuple[float, Symbol]] = []
        for s in symbols:
            bars = self.bars(ctx, s)
            if not bars:
                self._drop(report, s, "no history")
                continue
            value = statistics.fmean([b.close * b.volume for b in bars])
            if value < self.min_value:
                self._drop(report, s, f"turnover {value:,.0f} < {self.min_value:,.0f}")
                continue
            scored.append((value, s))
        scored.sort(key=lambda x: x[0], reverse=True)
        if self.top_n:
            for _, s in scored[self.top_n:]:
                self._drop(report, s, f"outside top {self.top_n} by turnover")
            scored = scored[: self.top_n]
        return [s for _, s in scored]


class PriceFilter(_HistoryFilter):
    """Drop instruments whose tick size is coarse relative to their price.

    A ₩1,000 stock on a ₩1 tick has 10bp of granularity — a limit order can
    only ever be placed on a grid that costs more than many strategies make.
    """

    name = "price"

    def __init__(self, min_price: float = 0.0, max_price: float = 0.0,
                 max_tick_pct: float = 0.0):
        self.min_price = min_price
        self.max_price = max_price
        self.max_tick_pct = max_tick_pct
        self.lookback = 1

    async def apply(self, ctx, symbols, report):
        out = []
        for s in symbols:
            price = ctx.price(s)
            if price <= 0:
                self._drop(report, s, "no price")
                continue
            if self.min_price and price < self.min_price:
                self._drop(report, s, f"price {price:,.4f} < {self.min_price:,.4f}")
                continue
            if self.max_price and price > self.max_price:
                self._drop(report, s, f"price {price:,.4f} > {self.max_price:,.4f}")
                continue
            if self.max_tick_pct:
                tick_pct = float(s.tick_size) / price
                if tick_pct > self.max_tick_pct:
                    self._drop(report, s,
                               f"tick {tick_pct:.3%} coarser than {self.max_tick_pct:.3%}")
                    continue
            out.append(s)
        return out


class SpreadFilter(UniverseFilter):
    """Drop instruments whose quoted spread eats the expected edge.

    Uses the live quote when there is one and falls back to the bar's own
    high-low range as a proxy, which is crude but directionally right.
    """

    name = "spread"

    def __init__(self, max_spread_pct: float = 0.005):
        self.max_spread = max_spread_pct
        self.lookback = 1

    async def apply(self, ctx, symbols, report):
        out = []
        for s in symbols:
            quote = ctx.quote(s)
            if quote is not None and math.isfinite(quote.spread_pct):
                spread = quote.spread_pct
            else:
                bar = ctx.latest(s)
                if bar is None or bar.close <= 0:
                    self._drop(report, s, "no quote or bar")
                    continue
                spread = (bar.high - bar.low) / bar.close * 0.25   # rough proxy
            if spread > self.max_spread:
                self._drop(report, s, f"spread {spread:.3%} > {self.max_spread:.3%}")
                continue
            out.append(s)
        return out


class VolatilityFilter(_HistoryFilter):
    """Keep instruments inside a volatility band.

    Both ends matter. Below the floor there is no move to capture and costs
    dominate; above the ceiling position sizing becomes guesswork and stops get
    hit by noise.
    """

    name = "volatility"

    def __init__(self, lookback_bars: int = 60, min_annual_vol: float = 0.10,
                 max_annual_vol: float = 2.0):
        self.lookback = lookback_bars
        self.min_vol = min_annual_vol
        self.max_vol = max_annual_vol

    async def apply(self, ctx, symbols, report):
        out = []
        ppy = periods_per_year(ctx.timeframe)
        for s in symbols:
            closes = [b.close for b in self.bars(ctx, s)]
            rets = [math.log(b / a) for a, b in zip(closes, closes[1:])
                    if a > 0 and b > 0]
            if len(rets) < 10:
                self._drop(report, s, "not enough returns to measure volatility")
                continue
            vol = statistics.pstdev(rets) * math.sqrt(ppy)
            if vol < self.min_vol:
                self._drop(report, s, f"vol {vol:.1%} below {self.min_vol:.1%} floor")
            elif vol > self.max_vol:
                self._drop(report, s, f"vol {vol:.1%} above {self.max_vol:.1%} ceiling")
            else:
                out.append(s)
        return out


class RangeStabilityFilter(_HistoryFilter):
    """Drop instruments that simply have not moved.

    Freqtrade's insight: a pair whose whole recent range is smaller than a
    round trip's cost cannot be profitable regardless of how good the signal
    is. This is a different question from volatility — a name can have high
    variance and still go nowhere.
    """

    name = "range_stability"

    def __init__(self, lookback_bars: int = 30, min_range_pct: float = 0.03):
        self.lookback = lookback_bars
        self.min_range = min_range_pct

    async def apply(self, ctx, symbols, report):
        out = []
        for s in symbols:
            bars = self.bars(ctx, s)
            if len(bars) < 5:
                self._drop(report, s, "not enough bars")
                continue
            low = min(b.low for b in bars)
            high = max(b.high for b in bars)
            rng = (high - low) / low if low > 0 else 0.0
            if rng < self.min_range:
                self._drop(report, s, f"{self.lookback}-bar range {rng:.2%} "
                                      f"< {self.min_range:.2%}")
            else:
                out.append(s)
        return out


class CorrelationFilter(_HistoryFilter):
    """Keep the universe from becoming one bet wearing several tickers.

    Greedy: walks the candidates in their incoming order (so an upstream rank
    is respected) and drops any name too correlated with one already kept.
    """

    name = "correlation"

    def __init__(self, lookback_bars: int = 90, max_correlation: float = 0.9):
        self.lookback = lookback_bars
        self.max_corr = max_correlation

    @staticmethod
    def _returns(bars: list[Bar]) -> list[float]:
        closes = [b.close for b in bars]
        return [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]

    @staticmethod
    def _corr(xs: list[float], ys: list[float]) -> float | None:
        n = min(len(xs), len(ys))
        if n < 20:
            return None
        xs, ys = xs[-n:], ys[-n:]
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        sy = math.sqrt(sum((y - my) ** 2 for y in ys))
        if sx <= 0 or sy <= 0:
            return None
        return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)

    async def apply(self, ctx, symbols, report):
        kept: list[tuple[Symbol, list[float]]] = []
        for s in symbols:
            rets = self._returns(self.bars(ctx, s))
            clash = None
            for other, other_rets in kept:
                c = self._corr(rets, other_rets)
                if c is not None and abs(c) > self.max_corr:
                    clash = (other, c)
                    break
            if clash is not None:
                self._drop(report, s,
                           f"corr {clash[1]:+.2f} with {clash[0].ticker}")
            else:
                kept.append((s, rets))
        return [s for s, _ in kept]


class PerformanceFilter(UniverseFilter):
    """Rank by the strategy's own realised P&L on each instrument.

    Genuinely double-edged, and worth saying so: it compounds a real edge, and
    it also chases luck on a short sample. `min_trades` is the guard — below
    it, a symbol is left alone rather than promoted or demoted on noise.
    """

    name = "performance"

    def __init__(self, lookback_bars: int = 500, min_trades: int = 4,
                 drop_worst: int = 0, min_profit_pct: float | None = None):
        self.lookback_bars = lookback_bars
        self.min_trades = min_trades
        self.drop_worst = drop_worst
        self.min_profit = min_profit_pct

    async def apply(self, ctx, symbols, report):
        window = ctx.bar_delta * self.lookback_bars
        scored: list[tuple[float, int, Symbol]] = []
        for s in symbols:
            trades = ctx.recent_trades(s, within=window)
            total = sum(t.pnl_pct for t in trades)
            scored.append((total, len(trades), s))

        out = []
        for total, n, s in scored:
            if n < self.min_trades:
                out.append(s)                        # too few trades to judge
                continue
            if self.min_profit is not None and total < self.min_profit:
                self._drop(report, s, f"{n} trades netting {total:+.2%}")
                continue
            out.append(s)

        if self.drop_worst:
            judged = [(t, n, s) for t, n, s in scored
                      if n >= self.min_trades and s in out]
            judged.sort(key=lambda x: x[0])
            for total, n, s in judged[: self.drop_worst]:
                self._drop(report, s, f"worst performer: {total:+.2%} over {n} trades")
                out.remove(s)
        return out


class ShuffleFilter(UniverseFilter):
    """Randomise the order.

    Not decoration: when a downstream cap keeps the first N, a stable ordering
    means the same names are always favoured, and a backtest of that is a
    backtest of the ordering as much as the strategy.
    """

    name = "shuffle"

    def __init__(self, seed: int = 0):
        self.seed = seed
        self._round = 0

    async def apply(self, ctx, symbols, report):
        out = list(symbols)
        random.Random(f"{self.seed}:{self._round}").shuffle(out)
        self._round += 1
        return out


class LimitFilter(UniverseFilter):
    """Hard cap on universe size — the last link, usually."""

    name = "limit"

    def __init__(self, max_symbols: int = 20, offset: int = 0):
        self.max_symbols = max_symbols
        self.offset = offset

    async def apply(self, ctx, symbols, report):
        kept = symbols[self.offset: self.offset + self.max_symbols]
        for s in symbols:
            if s not in kept:
                self._drop(report, s, f"outside positions {self.offset}"
                                      f"..{self.offset + self.max_symbols}")
        return kept


class HeldPositionFilter(UniverseFilter):
    """Force anything currently held back into the universe.

    Placed last, always. Dropping a symbol you hold means no model ever emits
    an exit for it and the position becomes permanent — the single most
    dangerous failure mode a dynamic universe has.
    """

    name = "held"

    async def apply(self, ctx, symbols, report):
        keys = {s.key for s in symbols}
        out = list(symbols)
        for pos in ctx.portfolio.open_positions:
            if pos.symbol.key not in keys:
                out.append(pos.symbol)
                report.reasons[pos.symbol.ticker] = "held: re-added so it can be exited"
        return out


# ─────────────────────────────────────────────────────────────────────────────
# The chain
# ─────────────────────────────────────────────────────────────────────────────
class UniverseSelector:
    """One source, then filters, then a mandatory held-position re-add."""

    def __init__(self, source: UniverseSource, filters: list[UniverseFilter] | None = None,
                 refresh_every_bars: int = 24, warmup_bars: int = 250):
        self.source = source
        self.filters = list(filters or [])
        if not any(isinstance(f, HeldPositionFilter) for f in self.filters):
            self.filters.append(HeldPositionFilter())
        self.refresh_every = max(refresh_every_bars, 1)
        self.warmup_bars = warmup_bars
        self._bar_count = 0
        self.last_report: SelectionReport | None = None

    @property
    def required_history(self) -> int:
        return max([self.warmup_bars, *(f.lookback for f in self.filters)] or [0])

    def due(self) -> bool:
        due = (self._bar_count % self.refresh_every) == 0
        self._bar_count += 1
        return due

    async def select(self, ctx: Context, provider: DataProvider) -> list[Symbol]:
        import time as _time

        started = _time.monotonic()
        report = SelectionReport()
        symbols = await self.source.candidates(ctx, provider)
        report.candidates = len(symbols)

        for f in self.filters:
            before = len(symbols)
            try:
                symbols = await f.apply(ctx, symbols, report)
            except Exception:
                # A broken filter must not empty the book.
                log.exception("universe filter %s raised — skipped", f.name)
                continue
            log.debug("universe filter %s: %d → %d", f.name, before, len(symbols))

        report.selected = [s.ticker for s in symbols]
        report.elapsed_s = _time.monotonic() - started
        self.last_report = report
        log.info("%s", report.summary())
        return symbols


BUILTIN_UNIVERSE_SOURCES = {
    "static": StaticSource,
    "exchange": ExchangeSource,
}

BUILTIN_UNIVERSE_FILTERS = {
    "age": AgeFilter,
    "volume": VolumeFilter,
    "price": PriceFilter,
    "spread": SpreadFilter,
    "volatility": VolatilityFilter,
    "range_stability": RangeStabilityFilter,
    "correlation": CorrelationFilter,
    "performance": PerformanceFilter,
    "shuffle": ShuffleFilter,
    "limit": LimitFilter,
    "held": HeldPositionFilter,
}
