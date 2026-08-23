"""수동 개입 — the operator's override, while the bot keeps running.

An automated strategy you cannot interrupt is not a tool, it is a machine you
happen to own. Three things an operator genuinely needs mid-session:

  · **지금 사라 / 팔아라** — a discretionary trade the strategy did not ask for
  · **이거 정리해라** — close one position, or everything, now
  · **잠깐 멈춰** — stop opening anything new, without flattening the book

The interesting design question is what happens *after* a manual buy. The
strategy has no insight supporting that position, so on the next bar the
portfolio model would compute a target of zero and sell it straight back —
the operator's trade undone within a minute, which is worse than not offering
the feature at all.

So a manually opened position is **pinned**: the portfolio model is told to
leave it alone, and it stays the operator's position until they close it or
explicitly hand it back to the strategy. Risk models still apply — a pin
overrides the strategy's opinion, not the stop-loss.

Manual orders skip the alpha, portfolio and universe layers, because that is
the entire point. They do not skip the brokerage guard rails: lot rounding, the
per-order notional ceiling and the daily budget all still hold. The operator
can override the strategy; they cannot override the safety limits without
changing the config.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

from quant.core.context import Context
from quant.core.types import (
    Order, OrderSide, OrderType, Symbol, TimeInForce, new_id, utcnow,
)

log = logging.getLogger("quant.manual")

ManualAction = Literal["buy", "sell", "close", "close_all"]


@dataclass
class ManualRequest:
    action: ManualAction
    symbol: Symbol | None = None
    quantity: Decimal | None = None
    notional: float | None = None
    limit_price: float | None = None
    #: hand the resulting position back to the strategy instead of pinning it
    manage: bool = False
    note: str = ""
    requested_at: datetime = field(default_factory=utcnow)
    id: str = field(default_factory=lambda: new_id("man_"))
    status: str = "pending"
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "action": self.action,
            "symbol": self.symbol.ticker if self.symbol else None,
            "quantity": float(self.quantity) if self.quantity is not None else None,
            "notional": self.notional, "limit_price": self.limit_price,
            "manage": self.manage, "note": self.note,
            "requested_at": self.requested_at.isoformat(),
            "status": self.status, "detail": self.detail,
        }


class ManualControl:
    """Operator command queue, drained by the engine once per bar."""

    def __init__(self, max_history: int = 200):
        self.paused = False
        self.pause_reason = ""
        self._queue: list[ManualRequest] = []
        self.history: list[ManualRequest] = []
        self.max_history = max_history

    # ── pause ────────────────────────────────────────────────────────────
    def pause(self, reason: str = "operator paused") -> None:
        """Stop opening anything new. Exits, stops and manual orders continue.

        Deliberately not a kill switch: flattening a book because someone
        wanted a moment to think is its own kind of damage.
        """
        self.paused = True
        self.pause_reason = reason
        log.warning("자동매매 일시정지: %s (청산·손절·수동주문은 계속 동작)", reason)

    def resume(self) -> None:
        self.paused = False
        self.pause_reason = ""
        log.warning("자동매매 재개")

    # ── requests ─────────────────────────────────────────────────────────
    def buy(self, symbol: Symbol, quantity: Decimal | None = None,
            notional: float | None = None, limit_price: float | None = None,
            manage: bool = False, note: str = "") -> ManualRequest:
        return self._enqueue(ManualRequest("buy", symbol, quantity, notional,
                                           limit_price, manage, note))

    def sell(self, symbol: Symbol, quantity: Decimal | None = None,
             notional: float | None = None, limit_price: float | None = None,
             note: str = "") -> ManualRequest:
        return self._enqueue(ManualRequest("sell", symbol, quantity, notional,
                                           limit_price, note=note))

    def close(self, symbol: Symbol, note: str = "") -> ManualRequest:
        return self._enqueue(ManualRequest("close", symbol, note=note))

    def close_all(self, note: str = "") -> ManualRequest:
        return self._enqueue(ManualRequest("close_all", note=note))

    def _enqueue(self, request: ManualRequest) -> ManualRequest:
        self._queue.append(request)
        log.warning("수동 주문 접수: %s %s %s", request.action,
                    request.symbol.ticker if request.symbol else "",
                    request.note)
        return request

    def cancel(self, request_id: str) -> bool:
        for r in self._queue:
            if r.id == request_id:
                self._queue.remove(r)
                r.status = "cancelled"
                self._archive(r)
                return True
        return False

    @property
    def pending(self) -> list[ManualRequest]:
        return list(self._queue)

    def _archive(self, request: ManualRequest) -> None:
        self.history.append(request)
        if len(self.history) > self.max_history:
            del self.history[: self.max_history // 2]

    # ── execution ────────────────────────────────────────────────────────
    def build_orders(self, ctx: Context) -> list[Order]:
        """Turn queued requests into orders. Drains the queue."""
        queued, self._queue = self._queue, []
        orders: list[Order] = []

        for request in queued:
            try:
                built = self._build_one(ctx, request)
            except Exception as exc:
                request.status = "error"
                request.detail = str(exc)
                log.warning("수동 주문 실패 %s: %s", request.id, exc)
                self._archive(request)
                continue
            if not built:
                request.status = "skipped"
                request.detail = request.detail or "주문할 수량이 없습니다"
                self._archive(request)
                continue
            request.status = "submitted"
            orders.extend(built)
            self._archive(request)
        return orders

    def _build_one(self, ctx: Context, request: ManualRequest) -> list[Order]:
        if request.action == "close_all":
            return [self._exit_order(ctx, pos.symbol, request)
                    for pos in ctx.portfolio.open_positions]

        if request.symbol is None:
            raise ValueError("종목이 지정되지 않았습니다")
        symbol = request.symbol

        if request.action == "close":
            held = ctx.portfolio.quantity(symbol)
            if held == 0:
                request.detail = "보유 수량이 없습니다"
                return []
            return [self._exit_order(ctx, symbol, request)]

        price = request.limit_price or ctx.price(symbol)
        if price <= 0:
            raise ValueError(f"{symbol.ticker} 가격을 알 수 없습니다")

        qty = request.quantity
        if qty is None:
            if request.notional is None:
                raise ValueError("수량 또는 금액 중 하나는 지정해야 합니다")
            qty = symbol.round_qty(Decimal(str(request.notional / price)))
        else:
            qty = symbol.round_qty(qty)

        if qty <= 0:
            request.detail = "최소 주문 단위보다 작습니다"
            return []

        side = OrderSide.BUY if request.action == "buy" else OrderSide.SELL
        if side is OrderSide.SELL:
            held = ctx.portfolio.quantity(symbol)
            if qty > held:
                qty = symbol.round_qty(held)
                request.detail = f"보유 수량까지만 매도합니다 ({float(qty)})"
            if qty <= 0:
                request.detail = "보유 수량이 없습니다"
                return []

        if request.action == "buy" and not request.manage:
            # Pin it, or the portfolio model sells it back on the next bar.
            ctx.pin(symbol, f"수동 매수: {request.note}" or "수동 매수")

        order = Order(
            symbol=symbol, side=side, quantity=qty,
            type=OrderType.LIMIT if request.limit_price else OrderType.MARKET,
            limit_price=(float(symbol.round_price(request.limit_price, side))
                         if request.limit_price else None),
            tif=TimeInForce.DAY if request.limit_price else TimeInForce.GTC,
            tag=f"manual_{request.action}: {request.note}"[:180],
            source="manual",
            meta={"manual_request": request.id, "pinned": not request.manage},
        )
        return [order]

    @staticmethod
    def _exit_order(ctx: Context, symbol: Symbol, request: ManualRequest) -> Order:
        held = ctx.portfolio.quantity(symbol)
        ctx.unpin(symbol)          # closing hands the symbol back to the strategy
        return Order(
            symbol=symbol,
            side=OrderSide.SELL if held > 0 else OrderSide.BUY,
            quantity=abs(held), type=OrderType.MARKET,
            tag=f"manual_close: {request.note}"[:180], source="manual",
            meta={"manual_request": request.id},
        )

    # ── reporting ────────────────────────────────────────────────────────
    def status(self) -> dict:
        return {
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "pending": [r.to_dict() for r in self._queue],
            "recent": [r.to_dict() for r in self.history[-20:]],
        }
