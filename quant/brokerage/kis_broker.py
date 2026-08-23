"""Korea Investment & Securities order adapter (domestic + overseas equities).

KIS separates *paper* (모의투자) and *live* into different hosts and different
transaction ids, so `live=False` is genuinely safe: it cannot reach the real
account even with production credentials.
"""
from __future__ import annotations

import logging
import os
from decimal import Decimal

import httpx

from quant.brokerage.base import BrokerageError
from quant.brokerage.live_base import LiveBrokerage
from quant.core.types import AssetClass, Fill, Order, OrderSide, OrderType, utcnow
from quant.data.providers.kis import kis_host, kis_token

log = logging.getLogger("quant.brokerage.kis")

# (buy, sell) transaction ids per environment
TR_DOMESTIC = {True: ("TTTC0802U", "TTTC0801U"), False: ("VTTC0802U", "VTTC0801U")}
TR_OVERSEAS = {True: ("TTTT1002U", "TTTT1006U"), False: ("VTTT1002U", "VTTT1001U")}
TR_BALANCE = {True: "TTTC8434R", False: "VTTC8434R"}


class KisBrokerage(LiveBrokerage):
    name = "kis"

    def __init__(self, portfolio, app_key: str = "", app_secret: str = "",
                 account_no: str = "", product_code: str = "01",
                 overseas_exchange: str = "NASD", **kwargs):
        super().__init__(portfolio, **kwargs)
        self.app_key = app_key or os.environ.get("KIS_APP_KEY", "")
        self.app_secret = app_secret or os.environ.get("KIS_APP_SECRET", "")
        self.account_no = account_no or os.environ.get("KIS_ACCOUNT_NO", "")
        self.product_code = product_code
        self.overseas_exchange = overseas_exchange
        self.paper = not self.live
        self._client = httpx.AsyncClient(timeout=20)
        missing = [n for n, v in (("KIS_APP_KEY", self.app_key),
                                  ("KIS_APP_SECRET", self.app_secret),
                                  ("KIS_ACCOUNT_NO", self.account_no)) if not v]
        if missing:
            raise BrokerageError(f"KIS brokerage needs {', '.join(missing)}")

    async def _headers(self, tr_id: str, body: dict | None = None) -> dict:
        token = await kis_token(self.app_key, self.app_secret, self.paper)
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        if body is not None and self.live:
            headers["hashkey"] = await self._hashkey(body)
        return headers

    async def _hashkey(self, body: dict) -> str:
        """KIS requires a tamper-proof hash on live orders."""
        r = await self._client.post(
            f"{kis_host(self.paper)}/uapi/hashkey", json=body,
            headers={"content-type": "application/json; charset=utf-8",
                     "appkey": self.app_key, "appsecret": self.app_secret},
        )
        r.raise_for_status()
        return r.json()["HASH"]

    async def _venue_submit(self, order: Order) -> str:
        domestic = order.symbol.quote_currency == "KRW"
        buy, sell = (TR_DOMESTIC if domestic else TR_OVERSEAS)[self.live]
        tr_id = buy if order.side is OrderSide.BUY else sell

        if domestic:
            path = "/uapi/domestic-stock/v1/trading/order-cash"
            body = {
                "CANO": self.account_no[:8],
                "ACNT_PRDT_CD": self.product_code,
                "PDNO": order.symbol.ticker,
                # 01 = market, 00 = limit
                "ORD_DVSN": "01" if order.type is OrderType.MARKET else "00",
                "ORD_QTY": str(int(order.quantity)),
                "ORD_UNPR": "0" if order.type is OrderType.MARKET
                            else str(int(order.limit_price or 0)),
            }
        else:
            if order.type is OrderType.MARKET:
                raise BrokerageError(
                    "KIS overseas orders must be limit orders — the API has no "
                    "market order type for foreign equities"
                )
            path = "/uapi/overseas-stock/v1/trading/order"
            body = {
                "CANO": self.account_no[:8],
                "ACNT_PRDT_CD": self.product_code,
                "OVRS_EXCG_CD": self.overseas_exchange,
                "PDNO": order.symbol.ticker,
                "ORD_QTY": str(int(order.quantity)),
                "OVRS_ORD_UNPR": f"{order.limit_price:.2f}",
                "ORD_SVR_DVSN_CD": "0",
                "ORD_DVSN": "00",
            }

        r = await self._client.post(f"{kis_host(self.paper)}{path}",
                                    headers=await self._headers(tr_id, body), json=body)
        r.raise_for_status()
        data = r.json()
        if str(data.get("rt_cd", "1")) != "0":
            raise BrokerageError(f"KIS order rejected: {data.get('msg1') or data}")
        return str((data.get("output") or {}).get("ODNO") or "")

    async def _venue_cancel(self, order: Order) -> bool:
        # KIS cancellation needs the original order's branch number, which the
        # submit response does not always carry; surfacing this honestly beats
        # silently pretending the cancel worked.
        raise BrokerageError(
            "KIS order cancellation is not implemented — cancel from the broker's "
            "own app or HTS. Prefer market/IOC orders so nothing rests."
        )

    async def _venue_open_orders(self):
        return []

    async def _venue_positions(self) -> dict[str, Decimal]:
        r = await self._client.get(
            f"{kis_host(self.paper)}/uapi/domestic-stock/v1/trading/inquire-balance",
            headers=await self._headers(TR_BALANCE[self.live]),
            params={
                "CANO": self.account_no[:8], "ACNT_PRDT_CD": self.product_code,
                "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
                "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
            },
        )
        r.raise_for_status()
        data = r.json()
        out: dict[str, Decimal] = {}
        for row in data.get("output1") or []:
            qty = Decimal(str(row.get("hldg_qty") or 0))
            if qty:
                out[f"kis:{row.get('pdno')}"] = qty
        return out

    async def close(self):
        await self._client.aclose()
