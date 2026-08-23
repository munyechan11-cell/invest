"""Seeded synthetic investor flow, so the desk and the flow alpha are runnable
and testable with no KIS credentials.

The generator deliberately embeds the structure the models look for — multi-day
accumulation runs where foreign and institutional net buying line up against
retail — because a purely random flow series would make the tests vacuous. It
is a fixture, not evidence: nothing here says the real signal exists.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from quant.core.types import UTC, Symbol
from quant.data.flow import FlowProvider, InvestorFlow, register_flow_provider


@register_flow_provider("synthetic")
class SyntheticFlowProvider(FlowProvider):
    name = "synthetic_flow"
    EPOCH = datetime(2015, 1, 1, tzinfo=UTC)

    def __init__(self, seed: int = 11, avg_volume: float = 1_000_000,
                 price: float = 50_000, regime_days: int = 12):
        self.seed = seed
        self.avg_volume = avg_volume
        self.price = price
        self.regime_days = regime_days
        self._paths: dict[str, list[InvestorFlow]] = {}

    def _path(self, symbol: Symbol, until: datetime) -> list[InvestorFlow]:
        cached = self._paths.get(symbol.key)
        if cached and cached[-1].ts >= until:
            return cached
        rng = random.Random(f"{self.seed}:{symbol.key}")
        out: list[InvestorFlow] = []
        ts = self.EPOCH
        regime = 0
        price = self.price
        while ts <= until:
            if ts.weekday() < 5:                       # weekdays only
                if len(out) % self.regime_days == 0:
                    regime = rng.choice([1, 1, 0, -1, -1])
                volume = max(abs(rng.gauss(self.avg_volume, self.avg_volume * 0.3)), 1000)
                # smart money takes one side, retail is mechanically the other
                smart = regime * volume * abs(rng.gauss(0.03, 0.02))
                foreign = smart * rng.uniform(0.4, 0.8)
                institution = smart - foreign
                retail = -smart + rng.gauss(0, volume * 0.005)
                price = max(price * (1 + regime * 0.002 + rng.gauss(0, 0.012)), 100)
                out.append(InvestorFlow(
                    symbol=symbol, ts=ts,
                    foreign_qty=foreign, institution_qty=institution, retail_qty=retail,
                    foreign_value=foreign * price, institution_value=institution * price,
                    retail_value=retail * price,
                    program_qty=smart * rng.uniform(0.2, 0.5),
                    program_value=smart * rng.uniform(0.2, 0.5) * price,
                    close=price, volume=volume,
                ))
            ts += timedelta(days=1)
        self._paths[symbol.key] = out
        return out

    async def flows(self, symbol, start, end):
        return [f for f in self._path(symbol, end) if start <= f.ts < end]
