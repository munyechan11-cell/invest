"""Clocks. The engine only ever asks a clock for `now()` and for the next
scheduled wake-up, which is what lets one engine drive both a backtest (a
`SimClock` fast-forwarding through history) and a live session (a `RealClock`
sleeping until the next candle close)."""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime, timedelta

from quant.core.types import UTC, timeframe_delta


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime: ...

    @abstractmethod
    async def sleep_until(self, ts: datetime) -> None: ...

    @property
    def is_simulated(self) -> bool:
        return False


class SimClock(Clock):
    """Deterministic clock advanced explicitly by the backtest loop."""

    def __init__(self, start: datetime):
        self._now = start if start.tzinfo else start.replace(tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, ts: datetime) -> None:
        ts = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
        # Time never runs backwards — a data source emitting out-of-order bars
        # would otherwise silently reintroduce look-ahead.
        if ts < self._now:
            raise ValueError(f"clock moved backwards: {self._now} -> {ts}")
        self._now = ts

    async def sleep_until(self, ts: datetime) -> None:
        self.set(ts)

    @property
    def is_simulated(self) -> bool:
        return True


class RealClock(Clock):
    def __init__(self, offset: timedelta = timedelta(0)):
        self.offset = offset  # measured drift vs the venue's server time

    def now(self) -> datetime:
        return datetime.now(UTC) + self.offset

    async def sleep_until(self, ts: datetime) -> None:
        delay = (ts - self.now()).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)


def next_candle_close(now: datetime, timeframe: str, lag: float = 2.0) -> datetime:
    """Wall-clock instant at which the candle covering `now` is settled.

    `lag` seconds of padding covers exchange-side aggregation delay — polling at
    the exact boundary reliably returns the *previous* candle on most venues.
    """
    delta = timeframe_delta(timeframe)
    secs = int(delta.total_seconds())
    epoch = int(now.timestamp())
    boundary = epoch - epoch % secs + secs
    return datetime.fromtimestamp(boundary + lag, tz=UTC)
