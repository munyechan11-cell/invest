"""Portfolio construction — turning opinions into a target book.

This is where account size, leverage, concentration limits and cash reserves
live. Alpha models cannot reach in here, and this layer never second-guesses
direction — it only decides *how much*.

Every model returns absolute targets for the full union of (symbols with active
insights) ∪ (symbols currently held), so a symbol that stops being liked gets an
explicit zero target rather than being silently forgotten.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from decimal import Decimal

from quant.core.context import Context
from quant.core.types import Direction, Insight, PortfolioTarget, Symbol

log = logging.getLogger("quant.portfolio")


class PortfolioConstructionModel(ABC):
    name = "portfolio"

    def __init__(
        self,
        max_position_weight: float = 0.25,
        max_gross_leverage: float = 1.0,
        cash_reserve_pct: float = 0.02,
        allow_short: bool = False,
        min_trade_weight: float = 0.005,
    ):
        self.max_position_weight = max_position_weight
        self.max_gross_leverage = max_gross_leverage
        self.cash_reserve_pct = cash_reserve_pct
        self.allow_short = allow_short
        #: rebalances smaller than this fraction of equity are skipped — churning
        #: 0.1% of the book back and forth just donates the spread to the venue
        self.min_trade_weight = min_trade_weight

    @abstractmethod
    def weights(self, ctx: Context, insights: list[Insight]) -> dict[str, float]:
        """Return desired signed portfolio weights keyed by `Symbol.key`."""

    # ── shared pipeline ──────────────────────────────────────────────────
    def create_targets(self, ctx: Context, insights: list[Insight]) -> list[PortfolioTarget]:
        active = [i for i in insights if i.is_active(ctx.now)]
        raw = self.weights(ctx, active) if active else {}
        raw = self._apply_constraints(raw)

        symbols: dict[str, Symbol] = {i.symbol.key: i.symbol for i in active}
        for pos in ctx.portfolio.open_positions:
            symbols.setdefault(pos.symbol.key, pos.symbol)

        investable = ctx.equity * (1.0 - self.cash_reserve_pct)
        targets: list[PortfolioTarget] = []
        for key, symbol in symbols.items():
            if ctx.is_pinned(symbol):
                # The operator owns this one. Emitting no target at all (rather
                # than a zero) leaves the position untouched instead of closing it.
                continue
            weight = raw.get(key, 0.0)
            price = ctx.price(symbol)
            if price <= 0:
                continue
            current = ctx.portfolio.quantity(symbol)
            desired = Decimal("0") if weight == 0 else symbol.round_qty(
                Decimal(str(weight * investable / price))
            )
            # deadband: ignore rebalances too small to be worth the round trip
            delta_weight = abs(float(desired - current)) * price / max(ctx.equity, 1e-9)
            if desired != 0 and current != 0 and delta_weight < self.min_trade_weight:
                continue
            targets.append(PortfolioTarget(
                symbol, desired, tag=f"w={weight:+.4f}", source=self.name
            ))
        return targets

    def _apply_constraints(self, weights: dict[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for key, w in weights.items():
            if not self.allow_short and w < 0:
                w = 0.0
            w = max(-self.max_position_weight, min(self.max_position_weight, w))
            if abs(w) < 1e-6:
                w = 0.0
            out[key] = w
        gross = sum(abs(w) for w in out.values())
        if gross > self.max_gross_leverage and gross > 0:
            scale = self.max_gross_leverage / gross
            out = {k: v * scale for k, v in out.items()}
        return out

    # ── helper for subclasses ────────────────────────────────────────────
    @staticmethod
    def _net_scores(insights: list[Insight], ctx: Context) -> dict[str, tuple[Symbol, float]]:
        """Collapse many insights per symbol into one decayed, signed score.

        FLAT is treated as a hard veto rather than a vote, so a regime filter or
        a risk-committee veto reliably zeroes the position instead of being
        averaged away by two enthusiastic momentum models.
        """
        buckets: dict[str, list[Insight]] = {}
        for ins in insights:
            buckets.setdefault(ins.symbol.key, []).append(ins)

        out: dict[str, tuple[Symbol, float]] = {}
        for key, items in buckets.items():
            symbol = items[0].symbol
            if any(i.direction is Direction.FLAT for i in items):
                out[key] = (symbol, 0.0)
                continue
            num = den = 0.0
            for i in items:
                w = i.decayed_confidence(ctx.now) * (1.0 + abs(i.magnitude or 0.0))
                num += int(i.direction) * w
                den += w
            out[key] = (symbol, num / den if den > 0 else 0.0)
        return out
