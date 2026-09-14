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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from quant.core.context import Context
from quant.core.types import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Symbol,
    TimeInForce,
    new_id,
    utcnow,
)

log = logging.getLogger("quant.manual")

ManualAction = Literal["buy", "sell", "close", "close_all"]

# A market buy is approved against the quote visible at that moment, not as a
# standing reservation to buy at any future price. Exits remain durable; only
# exposure-increasing, unpriced intent expires.
MARKET_BUY_MAX_QUEUE_AGE = timedelta(seconds=30)


class _ManualOrderDeferred(RuntimeError):
    """The request remains queued until venue order state becomes unambiguous."""


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

    @property
    def _side(self) -> OrderSide | None:
        if self.action == "buy":
            return OrderSide.BUY
        if self.action == "sell":
            return OrderSide.SELL
        return None                      # close/close_all 은 항상 시장가입니다

    def effective(self) -> tuple[float | None, float | None, str]:
        """브로커가 **실제로 받게 될** (수량, 지정가)와 그 이유.

        `_build_one` 은 보내기 직전에 수량을 lot 격자로, 지정가를 호가 격자로
        스냅합니다(`round_qty`·`round_price`). 접수한 원값을 그대로 화면에
        띄우면, 이 줄은 "이걸 정말 낼 것인가" 를 묻는 자리인데 **실행되지 않을
        숫자로** 답하게 됩니다.

        실측(000660, 호가 사다리): 1000.7주를 71,234 에 매도 접수하면 실제
        주문은 `1000주 @ 71,300` 입니다. 운영자는 71,234 에 걸린 줄 알고
        기다리는데 호가가 71,300 을 찍고 돌아서면, 본인은 청산됐다고 믿지만
        주문은 그대로 남아 있습니다.

        **알 수 없는 것은 손대지 않습니다.** 금액(`notional`)으로 낸 주문의
        수량은 발주 시점 시세로 정해지고, 매도가 보유 수량까지 줄어드는 것은
        그때의 장부가 정합니다 — 둘 다 여기서는 알 수 없으므로 `None` 과
        `detail` 로 남깁니다. 모르는 자리에 지어낸 숫자를 넣는 것이 원래
        문제였습니다.
        """
        qty = float(self.quantity) if self.quantity is not None else None
        price = self.limit_price
        if self.symbol is None:
            return qty, price, ""
        notes: list[str] = []
        if self.quantity is not None:
            snapped = float(self.symbol.round_qty(self.quantity))
            if snapped != qty:
                notes.append(f"수량 {qty:g} → {snapped:g} (최소 주문 단위)")
            qty = snapped
        side = self._side
        if price is not None and side is not None:
            snapped_price = float(self.symbol.round_price(price, side))
            if snapped_price != price:
                notes.append(f"지정가 {price:,g} → {snapped_price:,g} (호가단위)")
            price = snapped_price
        return qty, price, " · ".join(notes)

    def to_dict(self) -> dict:
        quantity, limit_price, adjusted = self.effective()
        return {
            "id": self.id, "action": self.action,
            "symbol": self.symbol.ticker if self.symbol else None,
            # 접수한 값이 아니라 **브로커가 받게 될 값** 입니다. 다르면
            # `adjusted` 가 어디서 얼마나 움직였는지 말합니다.
            "quantity": quantity,
            "notional": self.notional, "limit_price": limit_price,
            "requested_quantity": (float(self.quantity)
                                   if self.quantity is not None else None),
            "requested_limit_price": self.limit_price,
            "adjusted": adjusted,
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

    def mark_pending(self, detail: str) -> None:
        for request in self._queue:
            request.detail = detail

    def pending_close_keys(self, ctx: Context) -> set[str]:
        keys = {
            request.symbol.key
            for request in self._queue
            if request.action == "close" and request.symbol is not None
        }
        if any(request.action == "close_all" for request in self._queue):
            keys.update(pos.symbol.key for pos in ctx.portfolio.open_positions)
        return keys

    def pending_symbol_keys(self, ctx: Context) -> set[str]:
        keys = {
            request.symbol.key
            for request in self._queue
            if request.symbol is not None
        }
        if any(request.action == "close_all" for request in self._queue):
            keys.update(symbol.key for symbol in ctx.universe)
            keys.update(pos.symbol.key for pos in ctx.portfolio.open_positions)
        return keys

    def _archive(self, request: ManualRequest) -> None:
        self.history.append(request)
        if len(self.history) > self.max_history:
            del self.history[: self.max_history // 2]

    def record_submission(self, order: Order, *, reducing: bool) -> None:
        """Reconcile a broker result back to its operator request.

        Building an order is not submission. In particular, a definitive
        closed-session rejection of an exit must stay queued for the next open
        instead of disappearing behind a misleading ``submitted`` label.
        Transport uncertainty is deliberately not auto-retried here: without a
        venue-wide idempotency contract that could duplicate a real order.
        """
        request_id = str(order.meta.get("manual_request") or "")
        if not request_id or order.status is not OrderStatus.REJECTED:
            return
        request = next(
            (item for item in reversed(self.history) if item.id == request_id),
            None,
        )
        if request is None:
            return
        reason = str(order.reject_reason or "증권사가 주문을 거부했습니다")
        request.status = "rejected"
        request.detail = reason
        retryable_exit = reducing and request.action in {
            "sell", "close", "close_all",
        } and self._closed_session_rejection(reason)
        if not retryable_exit or any(item.id == request_id for item in self._queue):
            return
        retry = replace(
            request,
            status="pending",
            detail=(
                "장이 열리면 보유·미체결을 다시 확인한 뒤 자동으로 재시도합니다: "
                + reason
            ),
        )
        self._queue.append(retry)

    @staticmethod
    def _closed_session_rejection(reason: str) -> bool:
        normalized = reason.casefold()
        return any(marker in normalized for marker in (
            "정규장이 닫", "장이 닫", "장 마감", "거래시간이 아닙니다",
            "market closed", "market is closed", "outside market hours",
            "session closed",
        ))

    # ── execution ────────────────────────────────────────────────────────
    def build_orders(
        self,
        ctx: Context,
        reserved_order_keys: set[str] | None = None,
        blocked_increase_keys: set[str] | None = None,
    ) -> list[Order]:
        """Turn queued requests into orders. Drains the queue."""
        queued, self._queue = self._queue, []
        orders: list[Order] = []
        # Venue pending state is sampled before this synchronous drain. Orders
        # built earlier in the same drain are not in that sample yet, so track
        # them locally. Otherwise `sell(6)` followed by `close()` can submit
        # SELL 6 + SELL 10 against a long 10 position.
        batch_order_keys: set[str] = set(reserved_order_keys or ())

        for request in queued:
            try:
                built = self._build_one(
                    ctx,
                    request,
                    batch_order_keys,
                    blocked_increase_keys or set(),
                )
            except _ManualOrderDeferred as exc:
                request.status = "pending"
                request.detail = str(exc)
                self._queue.append(request)
                continue
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
            batch_order_keys.update(order.symbol.key for order in built)
            self._archive(request)
        return orders

    def _build_one(
        self,
        ctx: Context,
        request: ManualRequest,
        batch_order_keys: set[str] | None = None,
        blocked_increase_keys: set[str] | None = None,
    ) -> list[Order]:
        busy_keys = batch_order_keys if batch_order_keys is not None else set()
        blocked_keys = (
            blocked_increase_keys if blocked_increase_keys is not None else set()
        )

        if request.action == "close_all":
            busy = [
                pos.symbol.ticker for pos in ctx.portfolio.open_positions
                if (ctx.has_pending_order(pos.symbol)
                    or pos.symbol.key in busy_keys)
            ]
            if busy:
                raise _ManualOrderDeferred(
                    "기존 미체결 주문 정산 후 전체 청산을 자동으로 다시 시도합니다: "
                    + ", ".join(sorted(busy))
                )
            return [self._exit_order(ctx, pos.symbol, request)
                    for pos in ctx.portfolio.open_positions]

        if request.symbol is None:
            raise ValueError("종목이 지정되지 않았습니다")
        symbol = request.symbol
        if request.action == "buy" and request.limit_price is None:
            try:
                age = ctx.now - request.requested_at
            except (TypeError, ValueError):
                age = MARKET_BUY_MAX_QUEUE_AGE + timedelta(seconds=1)
            if age > MARKET_BUY_MAX_QUEUE_AGE:
                request.detail = (
                    "시장가 매수 검토 시세가 30초 지나 만료되었습니다 — "
                    "최신 시세를 확인하고 다시 검토하세요"
                )
                return []
        if ctx.has_pending_order(symbol) or symbol.key in busy_keys:
            raise _ManualOrderDeferred(
                f"{symbol.ticker} 기존 미체결 주문 정산 후 자동으로 다시 시도합니다"
            )
        if request.action == "buy" and symbol.key in blocked_keys:
            raise _ManualOrderDeferred(
                f"{symbol.ticker} 리스크 축소가 완료될 때까지 신규 매수를 보류합니다"
            )

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
            ctx.pin(symbol, f"수동 매수: {request.note}"
                    if (request.note or "").strip() else "수동 매수")

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
