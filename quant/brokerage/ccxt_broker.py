"""Crypto venue adapter via ccxt."""
from __future__ import annotations

import contextlib
import logging
from decimal import Decimal

from quant.brokerage.live_base import LiveBrokerage
from quant.core.types import Fill, Order, OrderStatus, OrderType, Symbol, utcnow

log = logging.getLogger("quant.brokerage.ccxt")


class CcxtBrokerage(LiveBrokerage):
    name = "ccxt"

    def __init__(self, portfolio, exchange: str = "binance", api_key: str = "",
                 secret: str = "", password: str = "", sandbox: bool = True,
                 market_type: str = "spot", **kwargs):
        super().__init__(portfolio, **kwargs)
        try:
            import ccxt.async_support as ccxt_async
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("pip install 'ccxt>=4.4' for crypto trading") from exc
        self.exchange_id = exchange
        self.ex = getattr(ccxt_async, exchange)({
            "apiKey": api_key, "secret": secret, "password": password,
            "enableRateLimit": True, "options": {"defaultType": market_type},
        })
        if sandbox:
            self.ex.set_sandbox_mode(True)
        elif self.live:
            log.warning("%s sandbox is OFF and live=True — orders hit the real book",
                        exchange)
        self._symbols: dict[str, Symbol] = {}

    async def _venue_submit(self, order: Order) -> str:
        self._symbols[order.symbol.ticker] = order.symbol
        params = {}
        if order.reduce_only:
            params["reduceOnly"] = True
        self._enforce_submission_guard(order)
        result = await self.ex.create_order(
            symbol=order.symbol.ticker,
            type="limit" if order.type is OrderType.LIMIT else "market",
            side=order.side.value,
            amount=float(order.quantity),
            price=order.limit_price if order.type is OrderType.LIMIT else None,
            params=params,
        )
        # Market orders usually come back already filled; book it immediately so
        # the engine's position state is right on the very next bar.
        filled = float(result.get("filled") or 0)
        if filled > 0:
            self._pending_fills.append(Fill(
                order_id=order.id, symbol=order.symbol, side=order.side,
                quantity=Decimal(str(filled)),
                price=float(result.get("average") or result.get("price") or 0),
                fee=float((result.get("fee") or {}).get("cost") or 0),
                ts=utcnow(),
                liquidity="taker" if order.type is OrderType.MARKET else "maker",
            ))
        return str(result.get("id") or "")

    async def _venue_cancel(self, order: Order) -> bool:
        await self.ex.cancel_order(order.broker_id, order.symbol.ticker)
        return True

    async def _venue_open_orders(self) -> list[dict]:
        return await self.ex.fetch_open_orders()

    async def _venue_positions(self) -> dict[str, Decimal]:
        balance = await self.ex.fetch_balance()
        out: dict[str, Decimal] = {}
        for asset, amount in (balance.get("total") or {}).items():
            if not amount:
                continue
            for key, sym in self._symbols.items():
                if key.split("/")[0] == asset:
                    out[sym.key] = Decimal(str(amount))
        return out

    async def poll_fills(self):
        """Also pick up fills of resting limit orders."""
        if self.live:
            for order in list(self._orders.values()):
                if not order.status.is_open or not order.broker_id:
                    continue
                try:
                    remote = await self.ex.fetch_order(order.broker_id, order.symbol.ticker)
                except Exception as exc:
                    log.debug("fetch_order failed for %s: %s", order.broker_id, exc)
                    continue
                newly = Decimal(str(remote.get("filled") or 0)) - order.filled_qty
                if newly > 0:
                    self._pending_fills.append(Fill(
                        order_id=order.id, symbol=order.symbol, side=order.side,
                        quantity=newly,
                        price=float(remote.get("average") or remote.get("price") or 0),
                        fee=float((remote.get("fee") or {}).get("cost") or 0),
                        ts=utcnow(), liquidity="maker",
                    ))
                    order.apply_fill(self._pending_fills[-1])
                # 거래소가 이미 끝낸 주문을 로컬에서 열린 채로 두면 `projected
                # quantity` 가 없는 주문을 계속 세고, 실행 모델은 그 종목에
                # 아무것도 새로 내보내지 않습니다 — 죽은 주문 하나가 그 종목의
                # 매매를 조용히 멈춥니다. ccxt 는 상태를 문자열로 줍니다.
                status = str(remote.get("status") or "").lower()
                if order.status.is_open and status in ("canceled", "cancelled",
                                                       "expired", "rejected"):
                    order.status = (OrderStatus.FILLED
                                    if order.filled_qty >= order.quantity
                                    else OrderStatus.CANCELED)
                    order.updated_at = utcnow()
                    log.info("거래소가 주문 %s 를 %s 로 끝냈습니다 — 로컬에서도 "
                             "닫습니다", order.broker_id, status)
        return await super().poll_fills()

    async def close(self):
        # 닫는 중에 터지는 것은 아무것도 바꾸지 못합니다 — 이미 끝내는 길입니다.
        with contextlib.suppress(Exception):
            await self.ex.close()
