"""Brokerage abstraction.

The engine talks to exactly one of these. A backtest gets `PaperBrokerage` with
a `SimClock`; a dry run gets `PaperBrokerage` with a `RealClock` and live data;
production gets a real venue adapter. No other part of the engine changes.

Adapters are responsible for one thing the engine cannot do for them:
reconciling reality. `sync()` is authoritative — if the venue says you hold 3
shares and local state says 4, the venue is right.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from decimal import Decimal

from quant.core.types import Order, OrderSide, OrderType, RunMode, Symbol

log = logging.getLogger("quant.brokerage")


class BrokerageError(RuntimeError):
    pass


class InsufficientFunds(BrokerageError):
    pass


class Brokerage(ABC):
    name = "brokerage"
    run_mode = RunMode.BACKTEST
    #: daily turnover / order-count / loss caps. Deliberately enforced here,
    #: at the last layer before the venue, where the actual orders are visible
    #: rather than the strategy's intentions.
    budget = None
    portfolio = None

    def _reduces_position(self, order: Order) -> bool:
        if self.portfolio is None:
            return False
        held = self.portfolio.quantity(order.symbol)
        if held == 0 or order.quantity <= 0 or order.quantity > abs(held):
            return False
        return (held > 0) == (order.side is OrderSide.SELL)

    def exact_flatten_order_type(
        self,
        symbol: Symbol,
        current_quantity: Decimal,
        target_quantity: Decimal,
    ) -> OrderType | None:
        """Venue-only escape hatch for an off-grid exact-to-flat exit.

        Lot grids normally apply symmetrically. A venue may explicitly allow a
        narrower liquidation route (for example, fractional US MARKET SELL)
        without allowing fractional entries. Execution consults this capability
        only when the target is exactly flat; ordinary sizing still uses the
        Symbol lot size.
        """
        return None

    def _budget_check(self, order: Order) -> tuple[bool, str]:
        if self.budget is None or not self.budget.configured:
            return True, ""
        price = order.limit_price or 0.0
        if price <= 0 and self.portfolio is not None:
            price = self.portfolio.position(order.symbol).last_price
        equity = self.portfolio.equity if self.portfolio is not None else 0.0
        return self.budget.check(order, price, self._reduces_position(order),
                                 equity=equity)

    def _budget_record(self, order: Order) -> None:
        if self.budget is None or not self.budget.configured:
            return
        price = order.limit_price or 0.0
        if price <= 0 and self.portfolio is not None:
            price = self.portfolio.position(order.symbol).last_price
        self.budget.record_order(order, price)

    @abstractmethod
    async def submit(self, order: Order) -> Order:
        """Send an order. Mutates and returns it with broker id / status set."""

    @abstractmethod
    async def cancel(self, order: Order) -> bool: ...

    @abstractmethod
    async def open_orders(self) -> list[Order]: ...

    async def positions(self) -> dict[str, Decimal]:
        """Venue-reported positions keyed by `Symbol.key`."""
        return {}

    async def balances(self) -> dict[str, float]:
        return {}

    async def sync(self) -> dict:
        """Reconcile local state against the venue. Returns a diff report."""
        return {}

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    # -- safety ----------------------------------------------------------
    def validate(self, order: Order) -> None:
        """Raise before anything leaves the process if an order is malformed."""
        if order.quantity <= 0:
            raise BrokerageError(f"non-positive quantity: {order.quantity}")
        sym: Symbol = order.symbol
        if sym.lot_size > 0 and sym.round_qty(order.quantity) != order.quantity:
            raise BrokerageError(
                f"{sym} quantity {order.quantity} is off the {sym.lot_size} lot grid"
            )
        if order.type.value.startswith("limit") and order.limit_price is None:
            raise BrokerageError("limit order without a limit price")
