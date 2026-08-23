"""Simulated brokerage — the backtest engine's counterparty, and the dry-run's.

Sharing one implementation between backtest and dry run is what makes dry-run
results actually predictive: the fill logic, fee model and rejection rules a
paper session exercises are the exact ones the backtest used.

Orders rest until a *subsequent* bar can fill them. Nothing ever fills on the
bar that generated the signal — that single rule removes the majority of
accidental look-ahead.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from quant.brokerage.base import Brokerage, BrokerageError, InsufficientFunds
from quant.core.account import Portfolio
from quant.core.types import (
    Bar,
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Quote,
    RunMode,
    TimeInForce,
    utcnow,
)
from quant.execution.costs import (
    FeeModel,
    FillModel,
    PercentFeeModel,
    RealisticFillModel,
    SideAwareFeeModel,
    SlippageModel,
    SpreadPlusImpactSlippage,
)

log = logging.getLogger("quant.brokerage.paper")


class PaperBrokerage(Brokerage):
    name = "paper"

    def __init__(
        self,
        portfolio: Portfolio,
        fee_model: FeeModel | None = None,
        slippage_model: SlippageModel | None = None,
        fill_model: FillModel | None = None,
        allow_short: bool = False,
        margin_multiple: float = 1.0,
        run_mode: RunMode = RunMode.BACKTEST,
        order_expiry_bars: int = 5,
    ):
        self.portfolio = portfolio
        self.fees = fee_model or PercentFeeModel(taker_bps=10, maker_bps=2)
        self.slippage = slippage_model or SpreadPlusImpactSlippage()
        self.fill_model = fill_model or RealisticFillModel()
        self.allow_short = allow_short
        self.margin_multiple = margin_multiple
        self.run_mode = run_mode
        self.order_expiry_bars = order_expiry_bars
        self._open: dict[str, Order] = {}
        self._age: dict[str, int] = {}
        self.fills: list[Fill] = []
        self.rejections: list[dict] = []

    # ── order lifecycle ──────────────────────────────────────────────────
    async def submit(self, order: Order) -> Order:
        allowed, reason = self._budget_check(order)
        if not allowed:
            order.status = OrderStatus.REJECTED
            order.reject_reason = reason
            self.rejections.append({"order": order.id, "symbol": order.symbol.ticker,
                                    "reason": reason})
            return order
        try:
            self.validate(order)
            self._check_affordable(order)
        except BrokerageError as exc:
            order.status = OrderStatus.REJECTED
            order.reject_reason = str(exc)
            self.rejections.append({"order": order.id, "symbol": order.symbol.ticker,
                                    "reason": str(exc)})
            log.debug("rejected %s %s: %s", order.side.value, order.symbol.ticker, exc)
            return order
        order.status = OrderStatus.SUBMITTED
        order.broker_id = f"paper-{order.id}"
        self._open[order.id] = order
        self._age[order.id] = 0
        self._budget_record(order)
        return order

    async def cancel(self, order: Order) -> bool:
        existing = self._open.pop(order.id, None)
        self._age.pop(order.id, None)
        if existing is None:
            return False
        existing.status = OrderStatus.CANCELED
        return True

    async def open_orders(self) -> list[Order]:
        return list(self._open.values())

    async def positions(self):
        return {k: p.quantity for k, p in self.portfolio.positions.items() if not p.is_flat}

    async def balances(self):
        return {self.portfolio.base_currency: self.portfolio.cash}

    # ── the simulation step ──────────────────────────────────────────────
    def process_bar(self, bar: Bar, quote: Quote | None = None) -> list[Fill]:
        """Attempt to fill every resting order for this symbol against `bar`."""
        produced: list[Fill] = []
        for order in [o for o in self._open.values() if o.symbol.key == bar.symbol.key]:
            fill = self._try_fill(order, bar, quote)
            if fill is not None:
                produced.append(fill)
        self._expire(bar)
        return produced

    def _try_fill(self, order: Order, bar: Bar, quote: Quote | None) -> Fill | None:
        slip = self.slippage.slippage(order, bar, quote)
        price, is_maker = self.fill_model.fill_price(order, bar, quote, slip)
        if price is None or price <= 0:
            return None
        qty = self.fill_model.max_fillable(order, bar)
        qty = min(qty, order.remaining)
        if qty <= 0:
            return None

        fee_model = self.fees
        if isinstance(fee_model, SideAwareFeeModel):
            fee_model = fee_model.for_side(order.side)
        fee = fee_model.fee(order.symbol, qty, price, is_maker)

        if order.side is OrderSide.BUY:
            cost = float(qty) * price * float(order.symbol.multiplier) + fee
            if cost > self.portfolio.cash * self.margin_multiple + 1e-9:
                affordable = order.symbol.round_qty(
                    Decimal(str(max(0.0, self.portfolio.cash * self.margin_multiple - fee)
                                / (price * float(order.symbol.multiplier))))
                )
                if affordable <= 0:
                    self._reject(order, f"insufficient cash: need {cost:.2f}, "
                                        f"have {self.portfolio.cash:.2f}")
                    return None
                qty = affordable
                fee = fee_model.fee(order.symbol, qty, price, is_maker)

        fill = Fill(
            order_id=order.id, symbol=order.symbol, side=order.side, quantity=qty,
            price=price, fee=fee, ts=bar.end_ts,
            liquidity="maker" if is_maker else "taker", slippage=slip,
            tag=order.tag,
        )
        order.apply_fill(fill)
        self.fills.append(fill)
        if order.status is OrderStatus.FILLED:
            self._open.pop(order.id, None)
            self._age.pop(order.id, None)
        return fill

    def _reject(self, order: Order, reason: str) -> None:
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        self._open.pop(order.id, None)
        self._age.pop(order.id, None)
        self.rejections.append({"order": order.id, "symbol": order.symbol.ticker,
                                "reason": reason})

    def _expire(self, bar: Bar) -> None:
        for oid in [o for o in self._open if self._open[o].symbol.key == bar.symbol.key]:
            order = self._open[oid]
            self._age[oid] = self._age.get(oid, 0) + 1
            expired = (
                (order.tif is TimeInForce.DAY and self._age[oid] >= 1)
                or (order.tif in (TimeInForce.IOC, TimeInForce.FOK))
                or self._age[oid] >= self.order_expiry_bars
            )
            if expired:
                order.status = OrderStatus.EXPIRED
                self._open.pop(oid, None)
                self._age.pop(oid, None)

    def _check_affordable(self, order: Order) -> None:
        if order.side is OrderSide.SELL and not self.allow_short:
            held = self.portfolio.quantity(order.symbol)
            if order.quantity > held:
                raise InsufficientFunds(
                    f"short selling disabled: selling {order.quantity} with {held} held"
                )
        # A limit price on the wrong side of the market is a strategy bug, not a
        # patient order — surface it rather than letting it rest forever.
        if order.type is OrderType.LIMIT and order.limit_price is not None:
            mark = self.portfolio.position(order.symbol).last_price
            if mark > 0:
                if order.side is OrderSide.BUY and order.limit_price > mark * 1.5:
                    raise BrokerageError("buy limit >50% above mark — likely a bug")
                if order.side is OrderSide.SELL and order.limit_price < mark * 0.5:
                    raise BrokerageError("sell limit >50% below mark — likely a bug")
