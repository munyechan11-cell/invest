"""Incremental (streaming) indicators.

Every indicator here is O(1) per update and *cannot* see the future — it only
ever receives bars in chronological order. That property is what makes a
backtest and a live session produce byte-identical signals, and it is why the
engine feeds strategies through these rather than recomputing a DataFrame each
bar (which is both slow and the classic source of look-ahead bugs).

Contract: `update(value) -> float | None`. `None` until warmed up; `is_ready`
tells you when the window has filled.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Iterable

from quant.core.types import Bar


class Indicator:
    """Base: named, warm-up aware, and history-recording for plotting."""

    def __init__(self, name: str, period: int, history: int = 0):
        if period < 1:
            raise ValueError("period must be >= 1")
        self.name = name
        self.period = period
        self.value: float | None = None
        self.samples = 0
        self._history: deque[float] = deque(maxlen=history) if history else deque(maxlen=1)

    @property
    def is_ready(self) -> bool:
        return self.value is not None and self.samples >= self.period

    def update(self, value: float) -> float | None:
        self.samples += 1
        self.value = self._compute(float(value))
        if self.value is not None:
            self._history.append(self.value)
        return self.value if self.is_ready else None

    def _compute(self, value: float) -> float | None:  # pragma: no cover - abstract
        raise NotImplementedError

    def prime(self, values: Iterable[float]) -> "Indicator":
        """Warm the indicator from historical values before going live."""
        for v in values:
            self.update(v)
        return self

    @property
    def previous(self) -> float | None:
        return self._history[-2] if len(self._history) >= 2 else None

    def __repr__(self) -> str:  # pragma: no cover - trivial
        v = f"{self.value:.6g}" if self.value is not None else "None"
        return f"<{self.name}({self.period})={v}{'' if self.is_ready else ' warming'}>"


class SMA(Indicator):
    def __init__(self, period: int, history: int = 4):
        super().__init__("SMA", period, history)
        self._win: deque[float] = deque(maxlen=period)
        self._sum = 0.0

    def _compute(self, value: float) -> float:
        if len(self._win) == self.period:
            self._sum -= self._win[0]
        self._win.append(value)
        self._sum += value
        return self._sum / len(self._win)


class EMA(Indicator):
    def __init__(self, period: int, history: int = 4):
        super().__init__("EMA", period, history)
        self.alpha = 2.0 / (period + 1.0)

    def _compute(self, value: float) -> float:
        if self.value is None:
            return value
        return self.value + self.alpha * (value - self.value)


class WilderMA(Indicator):
    """Wilder's smoothing (alpha = 1/n) — the one RSI/ATR/ADX are defined with."""

    def __init__(self, period: int, history: int = 4):
        super().__init__("RMA", period, history)
        self.alpha = 1.0 / period

    def _compute(self, value: float) -> float:
        if self.value is None:
            return value
        return self.value + self.alpha * (value - self.value)


class StdDev(Indicator):
    def __init__(self, period: int, history: int = 4):
        super().__init__("STD", period, history)
        self._win: deque[float] = deque(maxlen=period)

    def _compute(self, value: float) -> float:
        self._win.append(value)
        n = len(self._win)
        if n < 2:
            return 0.0
        mean = sum(self._win) / n
        var = sum((x - mean) ** 2 for x in self._win) / (n - 1)
        return math.sqrt(var)


class RSI(Indicator):
    """Wilder RSI. Returns 0..100."""

    def __init__(self, period: int = 14, history: int = 4):
        super().__init__("RSI", period, history)
        self._prev: float | None = None
        self._gain = WilderMA(period)
        self._loss = WilderMA(period)

    def _compute(self, value: float) -> float | None:
        if self._prev is None:
            self._prev = value
            return None
        delta = value - self._prev
        self._prev = value
        g = self._gain.update(max(delta, 0.0))
        l = self._loss.update(max(-delta, 0.0))
        if g is None or l is None:
            return None
        if l == 0:
            return 100.0
        rs = g / l
        return 100.0 - 100.0 / (1.0 + rs)

    @property
    def is_ready(self) -> bool:
        return self.value is not None and self.samples > self.period


class MACD(Indicator):
    """Returns the MACD line; `.signal` and `.histogram` expose the rest."""

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9, history: int = 4):
        super().__init__("MACD", slow + signal, history)
        self._fast, self._slow = EMA(fast), EMA(slow)
        self._signal = EMA(signal)
        self.signal: float | None = None
        self.histogram: float | None = None
        self._prev_hist: float | None = None

    def _compute(self, value: float) -> float | None:
        f, s = self._fast.update(value), self._slow.update(value)
        if f is None or s is None:
            return None
        macd = f - s
        self._prev_hist = self.histogram
        self.signal = self._signal.update(macd)
        self.histogram = macd - self.signal if self.signal is not None else None
        return macd

    @property
    def crossed_up(self) -> bool:
        return (
            self.histogram is not None
            and self._prev_hist is not None
            and self._prev_hist <= 0 < self.histogram
        )

    @property
    def crossed_down(self) -> bool:
        return (
            self.histogram is not None
            and self._prev_hist is not None
            and self._prev_hist >= 0 > self.histogram
        )


class BollingerBands(Indicator):
    """Returns the middle band; `.upper`, `.lower`, `.width`, `.percent_b`."""

    def __init__(self, period: int = 20, k: float = 2.0, history: int = 4):
        super().__init__("BB", period, history)
        self.k = k
        self._sma, self._std = SMA(period), StdDev(period)
        self.upper = self.lower = self.width = self.percent_b = None

    def _compute(self, value: float) -> float | None:
        mid, sd = self._sma.update(value), self._std.update(value)
        if mid is None or sd is None:
            return None
        self.upper, self.lower = mid + self.k * sd, mid - self.k * sd
        self.width = (self.upper - self.lower) / mid if mid else 0.0
        span = self.upper - self.lower
        self.percent_b = (value - self.lower) / span if span > 0 else 0.5
        return mid


class BarIndicator(Indicator):
    """Base for indicators that need the full OHLC bar, not just close."""

    def update_bar(self, bar: Bar) -> float | None:
        self.samples += 1
        self.value = self._compute_bar(bar)
        if self.value is not None:
            self._history.append(self.value)
        return self.value if self.is_ready else None

    def update(self, value: float) -> float | None:  # pragma: no cover - guard
        raise TypeError(f"{self.name} requires update_bar(bar)")

    def _compute_bar(self, bar: Bar) -> float | None:  # pragma: no cover - abstract
        raise NotImplementedError

    def prime_bars(self, bars: Iterable[Bar]) -> "BarIndicator":
        for b in bars:
            self.update_bar(b)
        return self


class ATR(BarIndicator):
    """Average True Range — the workhorse for volatility-scaled stops and sizing."""

    def __init__(self, period: int = 14, history: int = 4):
        super().__init__("ATR", period, history)
        self._rma = WilderMA(period)
        self._prev_close: float | None = None

    def _compute_bar(self, bar: Bar) -> float | None:
        if self._prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self._prev_close),
                abs(bar.low - self._prev_close),
            )
        self._prev_close = bar.close
        return self._rma.update(tr)

    def percent_of(self, price: float) -> float:
        return (self.value / price) if (self.value and price > 0) else 0.0


class ADX(BarIndicator):
    """Wilder ADX with `.plus_di` / `.minus_di` — a cheap trend/chop classifier."""

    def __init__(self, period: int = 14, history: int = 4):
        super().__init__("ADX", period * 2, history)
        self.period = period * 2
        self._n = period
        self._tr, self._pdm, self._mdm = WilderMA(period), WilderMA(period), WilderMA(period)
        self._dx = WilderMA(period)
        self._prev: Bar | None = None
        self.plus_di = self.minus_di = None

    def _compute_bar(self, bar: Bar) -> float | None:
        prev, self._prev = self._prev, bar
        if prev is None:
            return None
        up, down = bar.high - prev.high, prev.low - bar.low
        pdm = up if (up > down and up > 0) else 0.0
        mdm = down if (down > up and down > 0) else 0.0
        tr = max(bar.high - bar.low, abs(bar.high - prev.close), abs(bar.low - prev.close))
        atr = self._tr.update(tr)
        p, m = self._pdm.update(pdm), self._mdm.update(mdm)
        if not atr or p is None or m is None:
            return None
        self.plus_di = 100.0 * p / atr
        self.minus_di = 100.0 * m / atr
        denom = self.plus_di + self.minus_di
        dx = 100.0 * abs(self.plus_di - self.minus_di) / denom if denom > 0 else 0.0
        return self._dx.update(dx)


class Donchian(BarIndicator):
    """Donchian channel — returns the midline; `.upper` / `.lower` for breakouts."""

    def __init__(self, period: int = 20, history: int = 4):
        super().__init__("DONCHIAN", period, history)
        self._highs: deque[float] = deque(maxlen=period)
        self._lows: deque[float] = deque(maxlen=period)
        self.upper = self.lower = None

    def _compute_bar(self, bar: Bar) -> float | None:
        # Exclude the current bar from the channel so a breakout test against
        # `upper` is a real breakout and not "the bar broke its own high".
        self.upper = max(self._highs) if self._highs else None
        self.lower = min(self._lows) if self._lows else None
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        if self.upper is None or self.lower is None:
            return None
        return (self.upper + self.lower) / 2.0


class Keltner(BarIndicator):
    """Keltner channel. Paired with Bollinger it gives the classic squeeze."""

    def __init__(self, period: int = 20, atr_period: int = 10, k: float = 1.5, history: int = 4):
        super().__init__("KELTNER", max(period, atr_period), history)
        self._ema = EMA(period)
        self._atr = ATR(atr_period)
        self.k = k
        self.upper = self.lower = None

    def _compute_bar(self, bar: Bar) -> float | None:
        mid = self._ema.update(bar.close)
        self._atr.update_bar(bar)
        atr = self._atr.value
        if mid is None or atr is None:
            return None
        self.upper, self.lower = mid + self.k * atr, mid - self.k * atr
        return mid


class RollingReturn(Indicator):
    """Return over `period` bars — the raw material for momentum ranking."""

    def __init__(self, period: int = 20, history: int = 4):
        super().__init__("ROC", period, history)
        self._win: deque[float] = deque(maxlen=period + 1)

    def _compute(self, value: float) -> float | None:
        self._win.append(value)
        if len(self._win) <= self.period or self._win[0] <= 0:
            return None
        return value / self._win[0] - 1.0


class RollingVolatility(Indicator):
    """Annualized stdev of log returns — used for volatility targeting."""

    def __init__(self, period: int = 20, periods_per_year: float = 252.0, history: int = 4):
        super().__init__("VOL", period, history)
        self._prev: float | None = None
        self._std = StdDev(period)
        self._ppy = periods_per_year

    def _compute(self, value: float) -> float | None:
        prev, self._prev = self._prev, value
        if prev is None or prev <= 0 or value <= 0:
            return None
        sd = self._std.update(math.log(value / prev))
        return sd * math.sqrt(self._ppy) if sd is not None else None


class VWAP(BarIndicator):
    """Session VWAP; `reset()` at the session boundary."""

    def __init__(self, history: int = 4):
        super().__init__("VWAP", 1, history)
        self._pv = 0.0
        self._v = 0.0

    def _compute_bar(self, bar: Bar) -> float | None:
        self._pv += bar.typical * bar.volume
        self._v += bar.volume
        return self._pv / self._v if self._v > 0 else bar.close

    def reset(self) -> None:
        self._pv = self._v = 0.0
        self.value = None
        self.samples = 0


class IndicatorSet:
    """A named bundle of indicators fed from one bar stream.

    Strategies declare their indicators once; the engine primes them from
    history on start and then pushes each closed bar exactly once.
    """

    def __init__(self, **indicators: Indicator):
        self._items: dict[str, Indicator] = dict(indicators)

    def add(self, name: str, indicator: Indicator) -> Indicator:
        self._items[name] = indicator
        return indicator

    def __getattr__(self, name: str) -> Indicator:
        try:
            return self.__dict__["_items"][name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __getitem__(self, name: str) -> Indicator:
        return self._items[name]

    def __contains__(self, name: str) -> bool:
        return name in self._items

    def update(self, bar: Bar) -> None:
        for ind in self._items.values():
            if isinstance(ind, BarIndicator):
                ind.update_bar(bar)
            else:
                ind.update(bar.close)

    def prime(self, bars: Iterable[Bar]) -> "IndicatorSet":
        for bar in bars:
            self.update(bar)
        return self

    @property
    def is_ready(self) -> bool:
        return all(i.is_ready for i in self._items.values())

    @property
    def warmup(self) -> int:
        return max((i.period for i in self._items.values()), default=0)

    def values(self) -> dict[str, float | None]:
        return {k: v.value for k, v in self._items.items()}
