"""Shared machinery for real-money brokerage adapters.

Everything here exists because live trading fails in ways backtests cannot:
duplicate submissions after a timeout, positions that drift from local state,
a sizing bug that would have been harmless in simulation. The guard rails are
deliberately blunt and deliberately on by default.
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal

from quant.brokerage.base import Brokerage, BrokerageError
from quant.core.account import Portfolio
from quant.core.types import Fill, Order, OrderStatus, RunMode, Symbol, utcnow

log = logging.getLogger("quant.brokerage.live")


class LiveBrokerage(Brokerage):
    """Base for venue adapters. Subclasses implement the four `_venue_*` hooks."""

    def __init__(
        self,
        portfolio: Portfolio,
        live: bool = False,
        max_order_notional: float = 10_000.0,
        max_orders_per_minute: int = 20,
        reconcile_on_start: bool = True,
    ):
        self.portfolio = portfolio
        self.live = live
        self.run_mode = RunMode.LIVE if live else RunMode.DRY_RUN
        self.max_order_notional = max_order_notional
        self.max_orders_per_minute = max_orders_per_minute
        self.reconcile_on_start = reconcile_on_start
        self._orders: dict[str, Order] = {}
        self._recent_submits: list[float] = []
        #: (symbol, side, rounded qty) → timestamp, to stop a retry after a
        #: network timeout from becoming a second real position
        self._dedupe: dict[tuple, float] = {}
        self._pending_fills: list[Fill] = []
        self._lock = asyncio.Lock()

    # ── hooks ────────────────────────────────────────────────────────────
    async def _venue_submit(self, order: Order) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    async def _venue_cancel(self, order: Order) -> bool:  # pragma: no cover
        raise NotImplementedError

    async def _venue_open_orders(self) -> list[dict]:  # pragma: no cover
        raise NotImplementedError

    async def _venue_positions(self) -> dict[str, Decimal]:  # pragma: no cover
        raise NotImplementedError

    # ── guard rails ──────────────────────────────────────────────────────
    def _guard(self, order: Order) -> None:
        self.validate(order)
        price = order.limit_price or self.portfolio.position(order.symbol).last_price
        notional = abs(float(order.quantity)) * (price or 0) * float(order.symbol.multiplier)
        if price and notional > self.max_order_notional:
            raise BrokerageError(
                f"order notional {notional:,.2f} exceeds the "
                f"{self.max_order_notional:,.2f} per-order limit — refusing to send"
            )
        now = time.monotonic()
        self._recent_submits = [t for t in self._recent_submits if now - t < 60]
        if len(self._recent_submits) >= self.max_orders_per_minute:
            raise BrokerageError(
                f"rate limit: {self.max_orders_per_minute} orders/minute already sent. "
                "A strategy that wants more than this is almost certainly looping."
            )
        key = (order.symbol.key, order.side.value, str(order.quantity))
        last = self._dedupe.get(key)
        if last and now - last < 10:
            raise BrokerageError(
                "identical order sent less than 10s ago — suppressed as a probable duplicate"
            )

    async def submit(self, order: Order) -> Order:
        async with self._lock:
            try:
                self._guard(order)
            except BrokerageError as exc:
                order.status = OrderStatus.REJECTED
                order.reject_reason = str(exc)
                log.warning("blocked order for %s: %s", order.symbol.ticker, exc)
                return order

            if not self.live:
                # Dry run: everything except the network call, so the same code
                # path, guard rails and logging are exercised.
                order.status = OrderStatus.SUBMITTED
                order.broker_id = f"dry-{order.id}"
                order.meta["dry_run"] = True
                log.info("[DRY RUN] would send %s %s %s @ %s", order.side.value,
                         order.quantity, order.symbol.ticker, order.limit_price or "market")
                self._orders[order.id] = order
                return order

            try:
                broker_id = await self._venue_submit(order)
            except Exception as exc:
                order.status = OrderStatus.REJECTED
                order.reject_reason = f"venue rejected: {exc}"
                log.error("venue rejected %s: %s", order.symbol.ticker, exc)
                return order

            order.broker_id = broker_id
            order.status = OrderStatus.SUBMITTED
            order.updated_at = utcnow()
            self._orders[order.id] = order
            self._recent_submits.append(time.monotonic())
            self._dedupe[(order.symbol.key, order.side.value, str(order.quantity))] = \
                time.monotonic()
            log.info("sent %s %s %s (broker id %s)", order.side.value, order.quantity,
                     order.symbol.ticker, broker_id)
            return order

    async def cancel(self, order: Order) -> bool:
        if not self.live or order.broker_id is None:
            self._orders.pop(order.id, None)
            order.status = OrderStatus.CANCELED
            return True
        try:
            ok = await self._venue_cancel(order)
        except Exception as exc:
            log.warning("cancel failed for %s: %s", order.symbol.ticker, exc)
            return False
        if ok:
            order.status = OrderStatus.CANCELED
            self._orders.pop(order.id, None)
        return ok

    async def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status.is_open]

    async def poll_fills(self) -> list[Fill]:
        """Drain fills discovered since the last call. The engine calls this."""
        out, self._pending_fills = self._pending_fills, []
        return out

    async def sync(self) -> dict:
        """Reconcile local positions against the venue. The venue wins."""
        try:
            venue = await self._venue_positions()
        except Exception as exc:
            log.error("position sync failed: %s", exc)
            return {"ok": False, "error": str(exc)}

        drift: dict[str, dict] = {}
        for key, qty in venue.items():
            local = self.portfolio.positions.get(key)
            local_qty = local.quantity if local else Decimal("0")
            if local_qty != qty:
                drift[key] = {"local": float(local_qty), "venue": float(qty)}
                if local is not None:
                    local.quantity = qty
        for key, pos in self.portfolio.positions.items():
            if not pos.is_flat and key not in venue:
                drift[key] = {"local": float(pos.quantity), "venue": 0.0}
                pos.quantity = Decimal("0")
                pos.avg_price = 0.0
        if drift:
            log.warning("position drift corrected against the venue: %s", drift)
        return {"ok": True, "drift": drift, "venue_positions": {k: float(v)
                                                               for k, v in venue.items()}}

    async def connect(self) -> None:
        if self.reconcile_on_start:
            await self.sync()
        mode = "LIVE — real money" if self.live else "DRY RUN — no orders will be sent"
        log.warning("%s connected in %s mode", self.name, mode)
