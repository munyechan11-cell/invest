"""토스증권 Open API 어댑터 — 국내 + 미국 주식.

Toss opened a retail Open API in 2026: OAuth 2.0 client-credentials, one REST
surface covering KRX and US equities, quotes through orders.

**두 가지를 먼저 알아야 합니다.**

1. **모의투자 환경이 없습니다.** KIS는 실계좌와 완전히 분리된 모의투자 호스트를
   주지만 토스는 주지 않습니다. 공식 안내조차 "실 환경에서 소액으로 테스트"라고
   말합니다. 즉 토스에서는 이 엔진의 `dry_run` 모드 — 실시간 시세를 읽되 주문은
   내지 않고 체결을 시뮬레이션 — 가 편의 기능이 아니라 **유일한 안전망**입니다.
   그래서 `live=False`인 동안 이 어댑터는 주문 경로를 아예 밟지 않습니다.

2. **필드명은 검증이 필요합니다.** 아래 엔드포인트와 요청 필드는 공개 가이드
   문서를 근거로 구성했고, 공식 레퍼런스로 대조하지 못했습니다. 틀릴 수 있는
   부분을 `_FIELDS` 한 곳에 모아 두었으니, 실거래 전 반드시 공식 문서와 맞춰
   보고 필요하면 그 딕셔너리만 고치면 됩니다. 추측한 값을 사실처럼 숨겨 두는
   것보다 이렇게 드러내는 편이 낫습니다.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from datetime import datetime
from decimal import Decimal

import httpx

from quant.brokerage.base import BrokerageError
from quant.brokerage.live_base import LiveBrokerage
from quant.core.aio import LazyLock
from quant.core.types import (
    UTC,
    AssetClass,
    Bar,
    Fill,
    Order,
    OrderSide,
    OrderType,
    Quote,
    Symbol,
    timeframe_seconds,
    utcnow,
)
from quant.data.provider import DataProvider, register_provider

log = logging.getLogger("quant.toss")

HOST = "https://openapi.tossinvest.com"

#: 공식 문서와 대조가 필요한 부분. 실거래 전 여기만 확인하면 됩니다.
_FIELDS = {
    "token_path": "/oauth2/token",
    "price_path": "/v1/market/price",
    "candles_path": "/v1/market/candles",
    "orderbook_path": "/v1/market/orderbook",
    "accounts_path": "/v1/accounts",
    "holdings_path": "/v1/accounts/holdings",
    "orders_path": "/v1/orders",
    "account_header": "X-Tossinvest-Account",
    # 주문 요청 본문
    "order_symbol": "symbol",
    "order_side": "side",
    "order_qty": "quantity",
    "order_type": "orderType",
    "order_price": "price",
    "side_buy": "BUY",
    "side_sell": "SELL",
    "type_market": "MARKET",
    "type_limit": "LIMIT",
    # 응답
    "order_id": "orderId",
    "filled_qty": "filledQuantity",
    "avg_price": "averagePrice",
}

_TOKENS: dict[str, tuple[str, float]] = {}
_TOKEN_LOCK = LazyLock()


async def toss_token(client_id: str, client_secret: str,
                     timeout: float = 20.0) -> str:
    """Fetch-or-reuse an OAuth 2.0 access token (client_credentials, Basic auth)."""
    cache_key = client_id[:10]
    cached = _TOKENS.get(cache_key)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    async with _TOKEN_LOCK:
        cached = _TOKENS.get(cache_key)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{HOST}{_FIELDS['token_path']}",
                headers={"Authorization": f"Basic {basic}",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials"},
            )
            r.raise_for_status()
            data = r.json()
        token = data.get("access_token")
        if not token:
            raise BrokerageError(f"토스 토큰 응답에 access_token 없음: {str(data)[:200]}")
        _TOKENS[cache_key] = (token, time.time() + int(data.get("expires_in", 3600)))
        return token


class _TossClient:
    """Shared HTTP surface for the data provider and the brokerage."""

    def __init__(self, client_id: str, client_secret: str, account_no: str = "",
                 timeout: float = 20.0, requests_per_second: float = 8.0):
        self.client_id = client_id or os.environ.get("TOSS_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("TOSS_CLIENT_SECRET", "")
        self.account_no = account_no or os.environ.get("TOSS_ACCOUNT_NO", "")
        if not (self.client_id and self.client_secret):
            raise BrokerageError("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 가 필요합니다")
        self._http = httpx.AsyncClient(timeout=timeout)
        self._gap = 1.0 / requests_per_second
        self._next_at = 0.0
        self._lock = LazyLock()

    async def _headers(self, with_account: bool = False) -> dict:
        token = await toss_token(self.client_id, self.client_secret)
        headers = {"Authorization": f"Bearer {token}",
                   "Content-Type": "application/json"}
        if with_account:
            if not self.account_no:
                raise BrokerageError("TOSS_ACCOUNT_NO 가 필요합니다")
            headers[_FIELDS["account_header"]] = self.account_no
        return headers

    async def request(self, method: str, path: str, *, params: dict | None = None,
                      json: dict | None = None, account: bool = False) -> dict:
        async with self._lock:
            wait = self._next_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_at = time.monotonic() + self._gap
        r = await self._http.request(
            method, f"{HOST}{path}", params=params, json=json,
            headers=await self._headers(account),
        )
        if r.status_code >= 400:
            raise BrokerageError(f"토스 {method} {path} → {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    async def close(self) -> None:
        await self._http.aclose()


@register_provider("toss")
class TossProvider(DataProvider):
    """국내·미국 주식 시세."""

    name = "toss"

    _INTERVAL = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60",
                 "1d": "D", "1w": "W"}

    def __init__(self, client_id: str = "", client_secret: str = "",
                 account_no: str = "", **kwargs):
        self.client = _TossClient(client_id, client_secret, account_no, **kwargs)

    async def history(self, symbol, timeframe, start, end):
        interval = self._INTERVAL.get(timeframe)
        if interval is None:
            raise ValueError(
                f"토스는 {sorted(self._INTERVAL)} 주기만 제공합니다 (요청: {timeframe})")
        data = await self.client.request(
            "GET", _FIELDS["candles_path"],
            params={"symbol": symbol.ticker, "interval": interval,
                    "from": start.strftime("%Y%m%d"), "to": end.strftime("%Y%m%d")},
        )
        rows = data.get("candles") or data.get("data") or []
        _step = timeframe_seconds(timeframe)
        now = datetime.now(UTC)
        bars: list[Bar] = []
        for row in rows:
            ts = _parse_ts(row.get("timestamp") or row.get("dateTime") or row.get("date"))
            if ts is None or not (start <= ts < end):
                continue
            try:
                bar = Bar(symbol, ts, float(row["open"]), float(row["high"]),
                          float(row["low"]), float(row["close"]),
                          float(row.get("volume") or 0), timeframe)
            except (KeyError, TypeError, ValueError):
                continue
            # never hand back a candle that is still forming
            if bar.end_ts > now:
                continue
            bars.append(bar)
        bars.sort(key=lambda b: b.ts)
        return bars

    async def quote(self, symbol):
        try:
            data = await self.client.request(
                "GET", _FIELDS["orderbook_path"], params={"symbol": symbol.ticker})
        except Exception as exc:
            log.debug("토스 호가 조회 실패 %s: %s", symbol.ticker, exc)
            return None
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        if not bids or not asks:
            return None
        try:
            return Quote(symbol, utcnow(), float(bids[0]["price"]),
                         float(asks[0]["price"]),
                         float(bids[0].get("quantity") or 0),
                         float(asks[0].get("quantity") or 0))
        except (KeyError, TypeError, ValueError):
            return None

    async def resolve(self, ticker: str):
        code = ticker.strip().upper()
        try:
            data = await self.client.request(
                "GET", _FIELDS["price_path"], params={"symbol": code})
        except Exception:
            return None
        if not data:
            return None
        krx = code.isdigit() and len(code) == 6
        from quant.data.providers.kis import korean_tick_size

        price = float(data.get("price") or data.get("currentPrice") or 0)
        return Symbol(
            code, venue="toss", asset_class=AssetClass.EQUITY,
            quote_currency="KRW" if krx else "USD",
            lot_size=Decimal("1"),
            tick_size=korean_tick_size(price) if krx else Decimal("0.01"),
        )

    async def close(self):
        await self.client.close()


class TossBrokerage(LiveBrokerage):
    """토스증권 주문.

    `live=False` 인 동안 주문 경로는 아예 실행되지 않습니다 — 토스에는 모의투자
    호스트가 없어서, dry-run이 곧 "네트워크에 아무것도 보내지 않는다"를 의미해야
    하기 때문입니다.
    """

    name = "toss"

    def __init__(self, portfolio, client_id: str = "", client_secret: str = "",
                 account_no: str = "", **kwargs):
        super().__init__(portfolio, **kwargs)
        self.client = _TossClient(client_id, client_secret, account_no)
        if self.live:
            log.warning(
                "토스증권 실거래 모드 — 모의투자 환경이 없으므로 모든 주문이 실계좌로 나갑니다. "
                "일일 한도(limits)와 주문 한도(max_order_notional)를 반드시 확인하세요."
            )

    async def _venue_submit(self, order: Order) -> str:
        if order.type is OrderType.MARKET and order.symbol.quote_currency != "KRW":
            raise BrokerageError("해외 주식은 지정가만 지원합니다 (시장가 불가)")
        body = {
            _FIELDS["order_symbol"]: order.symbol.ticker,
            _FIELDS["order_side"]: (_FIELDS["side_buy"] if order.side is OrderSide.BUY
                                    else _FIELDS["side_sell"]),
            _FIELDS["order_qty"]: int(order.quantity),
            _FIELDS["order_type"]: (_FIELDS["type_limit"] if order.type is OrderType.LIMIT
                                    else _FIELDS["type_market"]),
        }
        if order.type is OrderType.LIMIT:
            body[_FIELDS["order_price"]] = float(order.limit_price or 0)

        data = await self.client.request("POST", _FIELDS["orders_path"],
                                         json=body, account=True)
        broker_id = str(data.get(_FIELDS["order_id"]) or data.get("id") or "")
        if not broker_id:
            raise BrokerageError(f"토스 주문 응답에 주문번호 없음: {str(data)[:200]}")

        filled = float(data.get(_FIELDS["filled_qty"]) or 0)
        if filled > 0:
            self._pending_fills.append(Fill(
                order_id=order.id, symbol=order.symbol, side=order.side,
                quantity=Decimal(str(filled)),
                price=float(data.get(_FIELDS["avg_price"])
                            or order.limit_price or 0),
                fee=0.0, ts=utcnow(), tag=order.tag,
            ))
        return broker_id

    async def _venue_cancel(self, order: Order) -> bool:
        await self.client.request(
            "DELETE", f"{_FIELDS['orders_path']}/{order.broker_id}", account=True)
        return True

    async def _venue_open_orders(self):
        data = await self.client.request("GET", _FIELDS["orders_path"],
                                         params={"status": "OPEN"}, account=True)
        return data.get("orders") or data.get("data") or []

    async def _venue_positions(self) -> dict[str, Decimal]:
        data = await self.client.request("GET", _FIELDS["holdings_path"], account=True)
        out: dict[str, Decimal] = {}
        for row in (data.get("holdings") or data.get("data") or []):
            code = row.get("symbol") or row.get("code")
            qty = row.get("quantity") or row.get("balance") or 0
            if code and qty:
                out[f"toss:{code}"] = Decimal(str(qty))
        return out

    async def poll_fills(self):
        if self.live:
            for order in list(self._orders.values()):
                if not order.status.is_open or not order.broker_id:
                    continue
                try:
                    remote = await self.client.request(
                        "GET", f"{_FIELDS['orders_path']}/{order.broker_id}",
                        account=True)
                except Exception as exc:
                    log.debug("토스 주문 조회 실패 %s: %s", order.broker_id, exc)
                    continue
                newly = Decimal(str(remote.get(_FIELDS["filled_qty"]) or 0)) \
                    - order.filled_qty
                if newly > 0:
                    fill = Fill(
                        order_id=order.id, symbol=order.symbol, side=order.side,
                        quantity=newly,
                        price=float(remote.get(_FIELDS["avg_price"]) or 0),
                        fee=0.0, ts=utcnow(), tag=order.tag,
                    )
                    order.apply_fill(fill)
                    self._pending_fills.append(fill)
        return await super().poll_fills()

    async def close(self):
        await self.client.close()


def _parse_ts(raw) -> datetime | None:
    if raw is None:
        return None
    text = str(raw).strip()
    try:
        if text.isdigit():
            if len(text) == 8:                       # YYYYMMDD
                return datetime.strptime(text, "%Y%m%d").replace(tzinfo=UTC)
            value = float(text)
            if value > 1e11:                         # milliseconds
                value /= 1000.0
            return datetime.fromtimestamp(value, tz=UTC)
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (ValueError, OSError):
        return None
