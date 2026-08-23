"""Alpaca adapter for US equities and crypto (REST, no SDK dependency)."""
from __future__ import annotations

import logging
import os
from decimal import Decimal

import httpx

from quant.brokerage.base import BrokerageError
from quant.brokerage.live_base import LiveBrokerage
from quant.core.types import Fill, Order, OrderSide, OrderType, TimeInForce, utcnow

log = logging.getLogger("quant.brokerage.alpaca")

PAPER_HOST = "https://paper-api.alpaca.markets"
LIVE_HOST = "https://api.alpaca.markets"


class AlpacaBrokerage(LiveBrokerage):
    name = "alpaca"

    def __init__(self, portfolio, api_key: str = "", secret_key: str = "", **kwargs):
        super().__init__(portfolio, **kwargs)
        self.api_key = api_key or os.environ.get("ALPACA_API_KEY", "")
        self.secret_key = secret_key or os.environ.get("ALPACA_SECRET_KEY", "")
        if not (self.api_key and self.secret_key):
            raise BrokerageError("ALPACA_API_KEY / ALPACA_SECRET_KEY are required")
        self.host = LIVE_HOST if self.live else PAPER_HOST
        self._client = httpx.AsyncClient(
            timeout=20,
            headers={"APCA-API-KEY-ID": self.api_key,
                     "APCA-API-SECRET-KEY": self.secret_key},
        )

    async def _venue_submit(self, order: Order) -> str:
        body = {
            "symbol": order.symbol.ticker,
            "qty": str(order.quantity),
            "side": order.side.value,
            "type": order.type.value.replace("_", "-"),
            "time_in_force": {"gtc": "gtc", "day": "day",
                              "ioc": "ioc", "fok": "fok"}[order.tif.value],
        }
        if order.limit_price is not None:
            body["limit_price"] = f"{order.limit_price:.2f}"
        if order.stop_price is not None:
            body["stop_price"] = f"{order.stop_price:.2f}"
        r = await self._client.post(f"{self.host}/v2/orders", json=body)
        if r.status_code >= 400:
            raise BrokerageError(f"alpaca {r.status_code}: {r.text[:300]}")
        return str(r.json().get("id"))

    async def _venue_cancel(self, order: Order) -> bool:
        r = await self._client.delete(f"{self.host}/v2/orders/{order.broker_id}")
        return r.status_code in (200, 204)

    async def _venue_open_orders(self):
        r = await self._client.get(f"{self.host}/v2/orders", params={"status": "open"})
        r.raise_for_status()
        return r.json()

    async def _venue_positions(self) -> dict[str, Decimal]:
        r = await self._client.get(f"{self.host}/v2/positions")
        r.raise_for_status()
        return {f"alpaca:{p['symbol']}": Decimal(p["qty"]) for p in r.json()}

    async def balances(self):
        r = await self._client.get(f"{self.host}/v2/account")
        r.raise_for_status()
        acct = r.json()
        return {"USD": float(acct.get("cash", 0)),
                "equity": float(acct.get("equity", 0)),
                "buying_power": float(acct.get("buying_power", 0))}

    async def poll_fills(self):
        if self.live:
            for order in list(self._orders.values()):
                # dry-run orders carry a synthetic id the venue knows nothing about
                if not order.status.is_open or not order.broker_id:
                    continue
                try:
                    r = await self._client.get(f"{self.host}/v2/orders/{order.broker_id}")
                    r.raise_for_status()
                except Exception as exc:
                    log.debug("order poll failed: %s", exc)
                    continue
                remote = r.json()
                newly = Decimal(str(remote.get("filled_qty") or 0)) - order.filled_qty
                if newly > 0:
                    fill = Fill(
                        order_id=order.id, symbol=order.symbol, side=order.side,
                        quantity=newly,
                        price=float(remote.get("filled_avg_price") or 0),
                        fee=0.0, ts=utcnow(),
                    )
                    order.apply_fill(fill)
                    self._pending_fills.append(fill)
        return await super().poll_fills()

    async def close(self):
        await self._client.aclose()
