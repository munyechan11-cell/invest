"""Risk management — the last layer that can change a target before execution.

Risk models see the *proposed* book and may only ever reduce exposure: cut a
target, flatten a position, or veto an entry. They cannot open something the
alpha and portfolio layers did not ask for. That one-way constraint is what
makes a stack of risk models safe to compose in any order.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from decimal import Decimal

from quant.core.context import Context
from quant.core.types import PortfolioTarget

log = logging.getLogger("quant.risk")


class RiskManagementModel(ABC):
    name = "risk"

    @abstractmethod
    def manage(self, ctx: Context, targets: list[PortfolioTarget]) -> list[PortfolioTarget]:
        """Return the adjusted target book."""

    def on_trade_closed(self, ctx: Context, trade) -> None:
        return None

    # -- helper ----------------------------------------------------------
    @staticmethod
    def _flatten(target: PortfolioTarget, reason: str) -> PortfolioTarget:
        return PortfolioTarget(target.symbol, Decimal("0"), tag=reason, source="risk")


class CompositeRiskModel(RiskManagementModel):
    """Applies each model in order; the most restrictive result survives."""

    name = "composite_risk"

    def __init__(self, *models: RiskManagementModel):
        self.models = list(models)

    def manage(self, ctx, targets):
        for model in self.models:
            try:
                targets = model.manage(ctx, targets)
            except Exception:
                log.exception("risk model %s raised — leaving targets unchanged", model.name)
        return targets

    def on_trade_closed(self, ctx, trade):
        for m in self.models:
            m.on_trade_closed(ctx, trade)
