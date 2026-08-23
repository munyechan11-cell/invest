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

from quant.core.types import Fill, Order, RunMode, Symbol

log = logging.getLogger("quant.brokerage")


class BrokerageError(RuntimeError):
    pass


class InsufficientFunds(BrokerageError):
    pass


class Brokerage(ABC):
    name = "brokerage"
    run_mode = RunMode.BACKTEST

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
