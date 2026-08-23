"""Investor supply/demand flow — 수급 데이터.

In Korean equities the single most-watched non-price signal is who is buying:
외국인 (foreign), 기관 (institutions, itself several sub-desks), 개인 (retail),
and 프로그램 (index/basket program trades). The distinction matters because the
groups have persistently different holding periods — foreign and institutional
net buying tends to persist over days to weeks, retail net buying tends to be
the other side of it.

This module models that as a first-class data type alongside OHLCV, because a
strategy that can see price but not flow is blind to half of what moves a
Korean mid-cap.

Everything here obeys the same no-look-ahead rule as bars: a flow record is
stamped with the session it describes, and `Context` will not hand it to a
model until that session has closed.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from quant.core.aio import LazySemaphore
from quant.core.types import UTC, Symbol

log = logging.getLogger("quant.data.flow")


@dataclass(frozen=True)
class InvestorFlow:
    """One session's net buying by investor group, for one instrument.

    Quantities are signed net share counts (buy − sell); values are signed net
    notional in the instrument's quote currency. Both are kept because they
    answer different questions: quantity tells you accumulation, value tells you
    conviction — 10,000 shares of a ₩800,000 stock is a very different message
    from 10,000 shares of a ₩1,200 one.
    """

    symbol: Symbol
    ts: datetime
    foreign_qty: float = 0.0
    institution_qty: float = 0.0
    retail_qty: float = 0.0
    program_qty: float = 0.0
    foreign_value: float = 0.0
    institution_value: float = 0.0
    retail_value: float = 0.0
    program_value: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    #: sub-desk breakdown when the venue provides it (연기금, 투신, 사모, …)
    institution_detail: dict = field(default_factory=dict)

    @property
    def smart_money_qty(self) -> float:
        """Foreign + institutional, i.e. everything that is not retail."""
        return self.foreign_qty + self.institution_qty

    @property
    def smart_money_value(self) -> float:
        return self.foreign_value + self.institution_value

    @property
    def participation(self) -> float:
        """Net smart-money volume as a fraction of the session's total volume.

        Scale-free, so it is comparable across a ₩600tn large cap and a small
        cap — unlike raw share counts, which are not.
        """
        return self.smart_money_qty / self.volume if self.volume > 0 else 0.0

    @property
    def is_accumulation(self) -> bool:
        """Foreign and institutions on the same side, against retail."""
        return (
            self.foreign_qty > 0 and self.institution_qty > 0 and self.retail_qty < 0
        )

    @property
    def is_distribution(self) -> bool:
        return (
            self.foreign_qty < 0 and self.institution_qty < 0 and self.retail_qty > 0
        )

    def to_dict(self) -> dict:
        return {
            "date": self.ts.date().isoformat(),
            "foreign_qty": round(self.foreign_qty),
            "institution_qty": round(self.institution_qty),
            "retail_qty": round(self.retail_qty),
            "program_qty": round(self.program_qty),
            "foreign_value": round(self.foreign_value),
            "institution_value": round(self.institution_value),
            "retail_value": round(self.retail_value),
            "close": self.close,
            "volume": round(self.volume),
            "participation_pct": round(self.participation * 100, 3),
            "pattern": ("accumulation" if self.is_accumulation
                        else "distribution" if self.is_distribution else "mixed"),
            "institution_detail": self.institution_detail or None,
        }


@dataclass(frozen=True)
class FlowSummary:
    """Rolling statistics over a window of `InvestorFlow` records.

    This is what models and agents actually reason about — a single day's net
    buy is noise, a 20-day accumulation streak is a position being built.
    """

    symbol: Symbol
    window: int
    foreign_net_qty: float
    institution_net_qty: float
    retail_net_qty: float
    foreign_net_value: float
    institution_net_value: float
    smart_money_net_value: float
    foreign_streak: int          # consecutive sessions, signed
    institution_streak: int
    avg_participation: float
    participation_zscore: float
    accumulation_days: int
    distribution_days: int
    turnover_ratio: float        # net smart-money volume / total volume
    price_change_pct: float
    divergence: str              # how flow relates to price

    def to_dict(self) -> dict:
        return {
            "window": self.window,
            "foreign_net_qty": round(self.foreign_net_qty),
            "institution_net_qty": round(self.institution_net_qty),
            "retail_net_qty": round(self.retail_net_qty),
            "foreign_net_value": round(self.foreign_net_value),
            "institution_net_value": round(self.institution_net_value),
            "smart_money_net_value": round(self.smart_money_net_value),
            "foreign_streak": self.foreign_streak,
            "institution_streak": self.institution_streak,
            "avg_participation_pct": round(self.avg_participation * 100, 3),
            "participation_zscore": round(self.participation_zscore, 2),
            "accumulation_days": self.accumulation_days,
            "distribution_days": self.distribution_days,
            "turnover_ratio_pct": round(self.turnover_ratio * 100, 3),
            "price_change_pct": round(self.price_change_pct * 100, 2),
            "divergence": self.divergence,
        }


def summarize(symbol: Symbol, flows: list[InvestorFlow], window: int = 20) -> FlowSummary | None:
    """Collapse a flow window into the handful of numbers that carry signal."""
    if not flows:
        return None
    recent = flows[-window:]
    if len(recent) < 2:
        return None

    import statistics

    f_qty = sum(f.foreign_qty for f in recent)
    i_qty = sum(f.institution_qty for f in recent)
    r_qty = sum(f.retail_qty for f in recent)
    f_val = sum(f.foreign_value for f in recent)
    i_val = sum(f.institution_value for f in recent)
    total_volume = sum(f.volume for f in recent)

    def streak(getter) -> int:
        """Consecutive same-sign sessions ending at the most recent one."""
        sign = 0
        count = 0
        for f in reversed(recent):
            v = getter(f)
            s = (v > 0) - (v < 0)
            if s == 0:
                break
            if sign == 0:
                sign = s
            elif s != sign:
                break
            count += 1
        return count * sign

    participations = [f.participation for f in recent]
    avg_part = statistics.fmean(participations)
    # z-score against the full history so "unusually heavy buying" is measured
    # against this instrument's own normal, not an absolute threshold
    baseline = [f.participation for f in flows]
    sd = statistics.pstdev(baseline) if len(baseline) > 2 else 0.0
    z = ((participations[-1] - statistics.fmean(baseline)) / sd) if sd > 1e-9 else 0.0

    first_close = next((f.close for f in recent if f.close > 0), 0.0)
    price_change = (recent[-1].close / first_close - 1.0) if first_close > 0 else 0.0
    smart_value = f_val + i_val

    if smart_value > 0 and price_change < -0.01:
        divergence = "bullish_divergence"      # they are buying weakness
    elif smart_value < 0 and price_change > 0.01:
        divergence = "bearish_divergence"      # they are selling strength
    elif smart_value > 0 and price_change > 0:
        divergence = "confirmed_uptrend"
    elif smart_value < 0 and price_change < 0:
        divergence = "confirmed_downtrend"
    else:
        divergence = "neutral"

    return FlowSummary(
        symbol=symbol,
        window=len(recent),
        foreign_net_qty=f_qty,
        institution_net_qty=i_qty,
        retail_net_qty=r_qty,
        foreign_net_value=f_val,
        institution_net_value=i_val,
        smart_money_net_value=smart_value,
        foreign_streak=streak(lambda f: f.foreign_qty),
        institution_streak=streak(lambda f: f.institution_qty),
        avg_participation=avg_part,
        participation_zscore=z,
        accumulation_days=sum(1 for f in recent if f.is_accumulation),
        distribution_days=sum(1 for f in recent if f.is_distribution),
        turnover_ratio=(f_qty + i_qty) / total_volume if total_volume > 0 else 0.0,
        price_change_pct=price_change,
        divergence=divergence,
    )


_FLOW_REGISTRY: dict = {}


def register_flow_provider(name: str):
    def deco(cls):
        _FLOW_REGISTRY[name.lower()] = cls
        return cls

    return deco


def create_flow_provider(name: str, **kwargs) -> "FlowProvider":
    key = (name or "none").lower()
    if key in ("none", "null", ""):
        return NullFlowProvider()
    if key not in _FLOW_REGISTRY:
        raise KeyError(
            f"unknown flow provider {name!r}; available: {sorted(_FLOW_REGISTRY) + ['none']}"
        )
    return _FLOW_REGISTRY[key](**kwargs)


def available_flow_providers() -> list[str]:
    return sorted(_FLOW_REGISTRY) + ["none"]


class FlowProvider(ABC):
    """Source of investor-flow records. Not every venue has one."""

    name = "flow"

    @abstractmethod
    async def flows(self, symbol: Symbol, start: datetime, end: datetime
                    ) -> list[InvestorFlow]:
        """Sessions in [start, end), chronological."""

    async def latest(self, symbol: Symbol, count: int = 20) -> list[InvestorFlow]:
        end = datetime.now(UTC)
        # generous window: sessions are sparse (weekends, holidays)
        start = end - timedelta(days=count * 2 + 20)
        return (await self.flows(symbol, start, end))[-count:]

    async def close(self) -> None:
        return None


class NullFlowProvider(FlowProvider):
    """Used where no flow source exists. Returns nothing, loudly once.

    Explicitly better than leaving flow models unconfigured: a strategy that
    *thinks* it is reading flow but silently gets zeros would produce confident
    nonsense.
    """

    name = "none"

    def __init__(self, reason: str = "no investor-flow source for this venue"):
        self.reason = reason
        self._warned = False

    async def flows(self, symbol, start, end):
        if not self._warned:
            log.warning("investor flow unavailable: %s", self.reason)
            self._warned = True
        return []


class FlowFeed:
    """Shared, de-duplicated access to flow data for every model that wants it.

    One instance is handed to both the rule-based flow alpha and the LLM desk,
    so a bar costs one fetch rather than one per consumer.
    """

    def __init__(self, provider: FlowProvider, history: int = 120,
                 refresh_every_bars: int = 1, concurrency: int = 4,
                 live: bool = False):
        self.provider = provider
        self.history = history
        self.refresh_every = max(refresh_every_bars, 1)
        #: Only a live session polls for new sessions. A backtest is served
        #: entirely from `backfill`, because `refresh` asks the provider for
        #: "the latest" relative to *wall clock* — which in a backtest is both
        #: ruinously slow and a look-ahead path straight into the future.
        self.live = live
        self._store: dict[str, list[InvestorFlow]] = {}
        self._bar_count = 0
        self._sem = LazySemaphore(concurrency)
        self.failures: dict[str, str] = {}

    def seed(self, symbol: Symbol, flows: Iterable[InvestorFlow]) -> None:
        self._store[symbol.key] = sorted(flows, key=lambda f: f.ts)

    def get(self, symbol: Symbol, now: datetime | None = None) -> list[InvestorFlow]:
        """Stored flows, filtered to sessions that had already closed by `now`."""
        items = self._store.get(symbol.key, [])
        if now is None:
            return list(items)
        return [f for f in items if f.ts <= now]

    def summary(self, symbol: Symbol, now: datetime | None = None,
                window: int = 20) -> FlowSummary | None:
        return summarize(symbol, self.get(symbol, now), window)

    async def refresh(self, symbols: list[Symbol], force: bool = False) -> int:
        """Pull the newest sessions. Returns how many new records landed.

        A no-op outside a live session — see `self.live`.
        """
        if not self.live:
            return 0
        self._bar_count += 1
        if not force and (self._bar_count - 1) % self.refresh_every != 0:
            return 0

        async def one(sym: Symbol) -> tuple[str, list[InvestorFlow]]:
            async with self._sem:
                try:
                    return sym.key, await self.provider.latest(sym, self.history)
                except Exception as exc:
                    self.failures[sym.key] = str(exc)
                    log.warning("flow fetch failed for %s: %s", sym.ticker, exc)
                    return sym.key, []

        results = await asyncio.gather(*(one(s) for s in symbols))
        added = 0
        for key, flows in results:
            if not flows:
                continue
            existing = {f.ts: f for f in self._store.get(key, [])}
            before = len(existing)
            for f in flows:
                existing[f.ts] = f
            merged = [existing[k] for k in sorted(existing)]
            self._store[key] = merged[-self.history * 2:]
            added += len(existing) - before
        return added

    async def backfill(self, symbols: list[Symbol], start: datetime,
                       end: datetime) -> None:
        """Load a historical window once, for backtests."""
        async def one(sym: Symbol) -> None:
            async with self._sem:
                try:
                    self.seed(sym, await self.provider.flows(sym, start, end))
                except Exception as exc:
                    self.failures[sym.key] = str(exc)
                    log.warning("flow backfill failed for %s: %s", sym.ticker, exc)

        await asyncio.gather(*(one(s) for s in symbols))

    @property
    def has_data(self) -> bool:
        return any(self._store.values())
