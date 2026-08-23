"""Execution — diffing targets against reality and emitting orders.

The execution model's only job is *how* to get from the current book to the
target book. It never questions the target. Splitting it out means switching
from "cross the spread immediately" to "work the order over an hour" is a
one-line config change with no strategy edits.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from decimal import Decimal

from quant.core.context import Context
from quant.core.types import Order, OrderSide, OrderType, PortfolioTarget, TimeInForce

log = logging.getLogger("quant.execution")


class ExecutionModel(ABC):
    name = "execution"

    def __init__(self, min_order_notional: float = 1.0):
        self.min_order_notional = min_order_notional

    @abstractmethod
    def execute(self, ctx: Context, targets: list[PortfolioTarget]) -> list[Order]: ...

    # -- shared helpers --------------------------------------------------
    def _deltas(self, ctx: Context, targets: list[PortfolioTarget]
                ) -> list[tuple[PortfolioTarget, Decimal, float]]:
        """(target, signed delta quantity, price) for every target worth acting on."""
        out = []
        for t in targets:
            price = ctx.price(t.symbol)
            if price <= 0:
                continue
            current = ctx.portfolio.quantity(t.symbol)
            delta = t.symbol.round_qty(t.quantity - current)
            if delta == 0:
                continue

            # A minimum-notional floor is a cost heuristic for *entries*. Applying
            # it to an exit is how a stop-loss silently fails: the target says
            # flat, the order is never sent, and the position rides to zero. Any
            # target that reduces the position is therefore exempt.
            reducing = abs(t.quantity) < abs(current) or (
                current != 0 and (t.quantity > 0) != (current > 0)
            )
            if not reducing:
                notional = abs(float(delta)) * price * float(t.symbol.multiplier)
                floor = max(self.min_order_notional, float(t.symbol.min_notional))
                if notional < floor:
                    # Below the venue minimum the order would be rejected anyway;
                    # below our own floor it is not worth the fee.
                    continue
            out.append((t, delta, price))
        return out

    def _order(self, target: PortfolioTarget, delta: Decimal, order_type=OrderType.MARKET,
               limit_price: float | None = None, tif=TimeInForce.GTC,
               tag_suffix: str = "") -> Order:
        side = OrderSide.BUY if delta > 0 else OrderSide.SELL
        return Order(
            symbol=target.symbol,
            side=side,
            quantity=abs(delta),
            type=order_type,
            limit_price=(
                float(target.symbol.round_price(limit_price, side))
                if limit_price is not None else None
            ),
            tif=tif,
            tag=f"{target.tag}{tag_suffix}",
            source=self.name,
            meta={"target_qty": float(target.quantity), "model": target.source},
        )
