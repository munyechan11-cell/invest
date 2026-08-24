"""Crypto venue adapter via ccxt."""
from __future__ import annotations

import logging
from decimal import Decimal

from quant.brokerage.live_base import LiveBrokerage
from quant.core.types import Fill, Order, OrderType, Symbol, utcnow

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
        return await super().poll_fills()

    async def close(self):
        try:
            await self.ex.close()
        except Exception:
            pass
