"""Toss OpenAPI v1.2.14 account contract, without touching a real account.

The official account surface has three easy-to-confuse shapes:

* ``GET /accounts`` returns a result *array*; its ``accountSeq`` (not accountNo)
  is the header required by every account-scoped endpoint.
* ``GET /holdings`` returns positions in ``items``.
* ``GET /buying-power`` returns ``cashBuyingPower`` for one requested currency.

Every HTTP interaction below uses a mock transport or fake client.  No token,
broker account, or order endpoint is contacted.
"""
from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from quant.api import server as api_server
from quant.brokerage import toss_broker as T
from quant.brokerage.base import BrokerageError
from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.engine import Engine, UnsafeShutdownError
from quant.core.events import EventBus, EventType
from quant.core.types import (
    Bar,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PortfolioTarget,
    Quote,
    RunMode,
    Symbol,
    utcnow,
)
from quant.execution.models import ImmediateExecution, LimitExecution
from quant.live.limits import TradingBudget

KRX = Symbol(
    "005930", venue="toss", quote_currency="KRW", tick_size=Decimal("100")
)
US = Symbol(
    "AAPL", venue="toss", quote_currency="USD",
    tick_size=Decimal("0.01"), lot_size=Decimal("1"),
)
ORDERED_AT = "2026-08-25T09:00:00+09:00"
FILLED_AT = "2026-08-25T09:01:00+09:00"


def _holdings(*, krw: str = "300000", usd: str | None = "25",
              items: list[dict] | None = None) -> dict:
    return {
        "totalPurchaseAmount": {"krw": "250000", "usd": "20"},
        "marketValue": {
            "amount": {"krw": krw, "usd": usd},
            "amountAfterCost": {"krw": krw, "usd": usd},
        },
        "profitLoss": {
            "amount": {"krw": "50000", "usd": "5"},
            "rate": "0.1",
        },
        "dailyProfitLoss": {
            "amount": {"krw": "1000", "usd": "1"},
            "rate": "0.01",
        },
        "items": items if items is not None else [
            {
                "symbol": "005930",
                "name": "삼성전자",
                "marketCountry": "KR",
                "currency": "KRW",
                "quantity": "2",
                "lastPrice": "150000",
                "averagePurchasePrice": "125000",
                "marketValue": {"amount": "300000"},
                "profitLoss": {"amount": "50000", "rate": "0.2"},
                "dailyProfitLoss": {"amount": "1000", "rate": "0.01"},
                "cost": {"commission": "0", "tax": "0"},
            }
        ],
    }


class FakeToss:
    def __init__(self, holdings: object, buying_power: dict[str, object] | None = None):
        self.holdings = holdings
        self.buying_power = buying_power or {
            "KRW": {"currency": "KRW", "cashBuyingPower": "420000"},
            "USD": {"currency": "USD", "cashBuyingPower": "12.5"},
        }
        self.calls: list[tuple[str, str, dict | None, bool]] = []

    async def request(self, method, path, *, params=None, json=None, account=False):
        self.calls.append((method, path, params, account))
        if path == T._FIELDS["holdings_path"]:
            return self.holdings
        if path == T._FIELDS["buying_power_path"]:
            return self.buying_power[str((params or {}).get("currency"))]
        raise AssertionError(f"unexpected fake Toss call: {method} {path}")

    async def close(self):
        pass


class FakeOrders:
    """Order-only fake. It records bodies and never opens a socket."""

    def __init__(self, replies: list[object] | None = None, *, open_orders=None):
        self.replies = list(replies or [])
        self.open_orders = [] if open_orders is None else open_orders
        self.calls: list[dict] = []

    async def request(self, method, path, *, params=None, json=None, account=False):
        self.calls.append({
            "method": method, "path": path, "params": params,
            "json": json, "account": account,
        })
        if method == "GET" and path == T._FIELDS["orders_path"]:
            return {
                "orders": self.open_orders, "nextCursor": None, "hasNext": False,
            }
        if method == "POST" and path.endswith("/cancel"):
            return {"orderId": "cancel-operation-1"}
        if not self.replies:
            raise AssertionError(f"no fake reply for {method} {path}")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    async def close(self):
        pass


def _order(
    *, side: OrderSide = OrderSide.BUY,
    quantity: str = "10",
    order_type: OrderType = OrderType.LIMIT,
) -> Order:
    return Order(
        KRX, side, Decimal(quantity), type=order_type,
        limit_price=70_000.0 if order_type is OrderType.LIMIT else None,
    )


def _detail(
    order: Order,
    *,
    status: str,
    filled: str,
    amount: str | None,
    commission: str | None = "0",
    tax: str | None = "0",
    average: str | None = None,
    filled_at: str | None = None,
) -> dict:
    if average is None and Decimal(filled) > 0 and amount is not None:
        average = str(Decimal(amount) / Decimal(filled))
    if filled_at is None and Decimal(filled) > 0:
        filled_at = FILLED_AT
    return {
        "orderId": order.broker_id,
        "symbol": order.symbol.ticker,
        "side": "BUY" if order.side is OrderSide.BUY else "SELL",
        "orderType": "LIMIT" if order.type is OrderType.LIMIT else "MARKET",
        "timeInForce": "DAY",
        "status": status,
        "price": str(Decimal(str(order.limit_price))) if order.limit_price else None,
        "quantity": str(order.quantity),
        "currency": order.symbol.quote_currency,
        "orderedAt": ORDERED_AT,
        "execution": {
            "filledQuantity": filled,
            "averageFilledPrice": average,
            "filledAmount": amount,
            "commission": commission,
            "tax": tax,
            "filledAt": filled_at,
            "settlementDate": None,
        },
    }


async def _order_broker(fake: FakeOrders, *, live: bool = True) -> T.TossBrokerage:
    broker = T.TossBrokerage(
        Portfolio(10_000_000, "KRW"),
        client_id="test-client", client_secret="test-secret",
        account_no="12345678901", live=live, reconcile_on_start=False,
        max_order_notional=1_000_000_000,
    )
    await broker.client.close()
    broker.client = fake
    broker.portfolio.mark(KRX, 70_000.0)
    return broker


async def _broker(fake: FakeToss, base_currency: str = "KRW") -> T.TossBrokerage:
    broker = T.TossBrokerage(
        Portfolio(800000, base_currency),
        client_id="test-client", client_secret="test-secret",
        account_no="12345678901", reconcile_on_start=False,
    )
    await broker.client.close()
    broker.client = fake
    return broker


async def _client(monkeypatch, account_no: str, handler) -> T._TossClient:
    async def fake_token(client_id, client_secret, timeout=20.0):
        return "test-token"

    monkeypatch.setattr(T, "toss_token", fake_token)
    client = T._TossClient(
        "test-client", "test-secret", account_no,
        requests_per_second=1_000_000,
    )
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


async def _order_broker_with_http_client(monkeypatch, handler) -> T.TossBrokerage:
    """Live order broker whose HTTP is a local MockTransport, never Toss."""
    broker = await _order_broker(FakeOrders())
    broker.client = await _client(monkeypatch, "12345678901", handler)
    # These tests exercise the side-effecting order request, not account lookup.
    # Pin the already-proven accountSeq so the only HTTP calls are POST /orders.
    broker.client._account_seq = "17"
    return broker


async def test_token_cache_is_isolated_when_two_clients_share_a_prefix(monkeypatch):
    T._TOKENS.clear()
    issued: list[tuple[str, str]] = []
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(
            part.split("=", 1) for part in request.content.decode("utf-8").split("&")
        )
        issued.append((form["client_id"], form["client_secret"]))
        return httpx.Response(200, json={
            "access_token": f"token-{len(issued)}", "expires_in": 3600,
        })

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self._client = real_async_client(transport=httpx.MockTransport(handler))

        async def __aenter__(self):
            return self._client

        async def __aexit__(self, *_args):
            await self._client.aclose()

    monkeypatch.setattr(T.httpx, "AsyncClient", FakeAsyncClient)
    prefix = "tsck_same-prefix"
    first = await T.toss_token(prefix + "-A", "tssk_secret-A")
    second = await T.toss_token(prefix + "-B", "tssk_secret-B")

    assert first == "token-1" and second == "token-2"
    assert len(issued) == 2, "다른 사용자가 첫 사용자의 bearer token을 재사용합니다"
    assert len(T._TOKENS) == 2
    assert all(isinstance(key, bytes) and b"tsck" not in key for key in T._TOKENS)


async def test_account_number_resolves_to_account_seq_and_is_cached(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == T._FIELDS["accounts_path"]:
            return httpx.Response(200, json={"result": [
                {"accountNo": "12345678901", "accountSeq": 17,
                 "accountType": "BROKERAGE"},
            ]})
        return httpx.Response(200, json={"result": {"items": []}})

    client = await _client(monkeypatch, "123-4567-8901", handler)
    try:
        await client.request("GET", T._FIELDS["holdings_path"], account=True)
        await client.request("GET", T._FIELDS["holdings_path"], account=True)
    finally:
        await client.close()

    assert [r.url.path for r in seen] == [
        T._FIELDS["accounts_path"],
        T._FIELDS["holdings_path"],
        T._FIELDS["holdings_path"],
    ]
    assert T._FIELDS["account_header"] not in seen[0].headers
    assert seen[1].headers[T._FIELDS["account_header"]] == "17"
    assert seen[2].headers[T._FIELDS["account_header"]] == "17"


async def test_a_stored_account_seq_is_supported_but_account_no_wins(monkeypatch):
    headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == T._FIELDS["accounts_path"]:
            return httpx.Response(200, json={"result": [
                # The configured value "1" matches this accountNo, so its seq
                # wins over the second row whose accountSeq also happens to be 1.
                {"accountNo": "1", "accountSeq": 7, "accountType": "BROKERAGE"},
                {"accountNo": "999", "accountSeq": 1, "accountType": "BROKERAGE"},
            ]})
        headers.append(request.headers[T._FIELDS["account_header"]])
        return httpx.Response(200, json={"result": {"items": []}})

    client = await _client(monkeypatch, "1", handler)
    try:
        await client.request("GET", T._FIELDS["holdings_path"], account=True)
    finally:
        await client.close()
    assert headers == ["7"]

    seq_headers: list[str] = []

    def seq_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == T._FIELDS["accounts_path"]:
            return httpx.Response(200, json={"result": [
                {"accountNo": "12345678901", "accountSeq": 17,
                 "accountType": "BROKERAGE"},
            ]})
        seq_headers.append(request.headers[T._FIELDS["account_header"]])
        return httpx.Response(200, json={"result": {"items": []}})

    client = await _client(monkeypatch, "17", seq_handler)
    try:
        await client.request("GET", T._FIELDS["holdings_path"], account=True)
    finally:
        await client.close()
    assert seq_headers == ["17"]


async def test_account_resolution_never_falls_back_to_the_first_account(monkeypatch):
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"result": [
            {"accountNo": "11111111111", "accountSeq": 1,
             "accountType": "BROKERAGE"},
        ]})

    client = await _client(monkeypatch, "99999999999", handler)
    try:
        with pytest.raises(BrokerageError, match="일치하는 활성 계좌가 없습니다"):
            await client.request("GET", T._FIELDS["holdings_path"], account=True)
    finally:
        await client.close()
    assert paths == [T._FIELDS["accounts_path"]]


async def test_accounts_result_must_be_the_official_array(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"accounts": [
            {"accountNo": "12345678901", "accountSeq": 1},
        ]}})

    client = await _client(monkeypatch, "12345678901", handler)
    try:
        with pytest.raises(BrokerageError, match="result 배열"):
            await client.request("GET", T._FIELDS["holdings_path"], account=True)
    finally:
        await client.close()


async def test_venue_capital_and_positions_share_one_official_holdings_snapshot():
    fake = FakeToss(_holdings())
    broker = await _broker(fake)

    capital = await broker._venue_capital()
    positions = await broker._venue_positions()
    costs = await broker._venue_costs()
    cached_cash = await broker._venue_cash()

    assert capital == {"currency": "KRW", "cash": 420000.0,
                       "holdings_value": 300000.0}
    assert positions == {"toss:005930": Decimal("2")}
    assert costs == {"toss:005930": 125000.0}
    assert cached_cash == 420000.0
    assert [call[1] for call in fake.calls].count(T._FIELDS["holdings_path"]) == 1
    assert [call[1] for call in fake.calls].count(T._FIELDS["buying_power_path"]) == 2


async def test_venue_capital_never_converts_or_adds_the_other_currency():
    fake = FakeToss(_holdings(krw="300000", usd="999999"))
    broker = await _broker(fake, "KRW")
    assert await broker._venue_capital() == {
        "currency": "KRW", "cash": 420000.0, "holdings_value": 300000.0,
    }
    buying_calls = [call for call in fake.calls
                    if call[1] == T._FIELDS["buying_power_path"]]
    assert [call[2] for call in buying_calls] == [
        {"currency": "KRW"}, {"currency": "KRW"},
    ]


async def test_positions_require_the_official_items_shape_instead_of_flattening():
    fake = FakeToss({
        "marketValue": {"amount": {"krw": "300000", "usd": None}},
        "holdings": [{"symbol": "005930", "quantity": "2"}],
    })
    broker = await _broker(fake)
    with pytest.raises(BrokerageError, match="items 배열"):
        await broker._venue_positions()


async def test_malformed_position_quantity_fails_loudly():
    data = _holdings(items=[{
        "symbol": "005930", "quantity": "not-a-number",
        "averagePurchasePrice": "125000",
    }])
    broker = await _broker(FakeToss(data))
    with pytest.raises(BrokerageError, match="quantity를 읽을 수 없습니다"):
        await broker._venue_positions()


async def test_account_overview_exposes_per_currency_investable_assets():
    broker = await _broker(FakeToss(_holdings(krw="300000", usd="25")))
    overview = await broker.account_overview()

    assert overview["cash"] is None
    assert overview["cash_buying_power"] == {"KRW": 420000.0, "USD": 12.5}
    assert overview["market_value"] == {"KRW": 300000.0, "USD": 25.0}
    assert overview["investable_assets"] == {"KRW": 720000.0, "USD": 37.5}
    assert overview["value_kind"] == "cash_buying_power_plus_holdings"


async def test_zero_buying_power_is_a_real_zero_not_local_starting_cash():
    powers = {
        "KRW": {"currency": "KRW", "cashBuyingPower": "0"},
        "USD": {"currency": "USD", "cashBuyingPower": "0"},
    }
    broker = await _broker(FakeToss(_holdings(krw="0", usd=None, items=[]), powers))
    overview = await broker.account_overview()
    assert overview["cash_buying_power"] == {"KRW": 0.0, "USD": 0.0}
    assert overview["investable_assets"] == {"KRW": 0.0, "USD": 0.0}
    assert 800000 not in overview["investable_assets"].values()


@pytest.mark.parametrize("reply, message", [
    ({"currency": "KRW"}, "금액이 없습니다"),
    ({"currency": "KRW", "cashBuyingPower": "NaN"}, "0 이상 숫자가 아닙니다"),
    ({"currency": "USD", "cashBuyingPower": "420000"}, "요청과 다릅니다"),
])
async def test_broken_buying_power_never_falls_back_to_local_cash(reply, message):
    fake = FakeToss(_holdings(), {"KRW": reply})
    broker = await _broker(fake)
    with pytest.raises(BrokerageError, match=message):
        await broker._venue_capital()
    assert broker._capital_holdings is None
    assert broker._capital_cash is None


async def test_capital_snapshot_brackets_holdings_and_keeps_the_smaller_cash():
    class RacingCapital(FakeToss):
        def __init__(self):
            super().__init__(_holdings(krw="900", usd=None, items=[]))
            self.cash = iter(("100", "500"))  # a sell credits cash between reads

        async def request(self, method, path, *, params=None, json=None, account=False):
            self.calls.append((method, path, params, account))
            if path == T._FIELDS["buying_power_path"]:
                return {"currency": "KRW", "cashBuyingPower": next(self.cash)}
            if path == T._FIELDS["holdings_path"]:
                return self.holdings
            raise AssertionError(path)

    fake = RacingCapital()
    broker = await _broker(fake)
    capital = await broker._venue_capital()

    assert capital == {"currency": "KRW", "cash": 100.0, "holdings_value": 900.0}
    assert [call[1] for call in fake.calls] == [
        T._FIELDS["buying_power_path"],
        T._FIELDS["holdings_path"],
        T._FIELDS["buying_power_path"],
    ]
    assert capital["cash"] + capital["holdings_value"] == 1000.0


async def test_capital_snapshot_does_not_overstate_a_buy_between_reads():
    class RacingCapital(FakeToss):
        def __init__(self):
            super().__init__(_holdings(krw="500", usd=None, items=[]))
            self.cash = iter(("1000", "400"))

        async def request(self, method, path, *, params=None, json=None, account=False):
            self.calls.append((method, path, params, account))
            if path == T._FIELDS["buying_power_path"]:
                return {"currency": "KRW", "cashBuyingPower": next(self.cash)}
            if path == T._FIELDS["holdings_path"]:
                return self.holdings
            raise AssertionError(path)

    broker = await _broker(RacingCapital())
    assert await broker._venue_capital() == {
        "currency": "KRW", "cash": 400.0, "holdings_value": 500.0,
    }


async def test_account_overview_uses_the_same_bracketed_cash_snapshot():
    class RacingOverview(FakeToss):
        def __init__(self):
            super().__init__(_holdings(krw="300", usd="25", items=[]))
            self.values = {
                "KRW": iter(("420", "900")),
                "USD": iter(("12.5", "7.5")),
            }

        async def request(self, method, path, *, params=None, json=None, account=False):
            self.calls.append((method, path, params, account))
            if path == T._FIELDS["buying_power_path"]:
                currency = str(params["currency"])
                return {"currency": currency,
                        "cashBuyingPower": next(self.values[currency])}
            if path == T._FIELDS["holdings_path"]:
                return self.holdings
            raise AssertionError(path)

    fake = RacingOverview()
    broker = await _broker(fake)
    overview = await broker.account_overview()

    assert overview["cash_buying_power"] == {"KRW": 420.0, "USD": 7.5}
    assert overview["investable_assets"] == {"KRW": 720.0, "USD": 32.5}
    assert [call[1] for call in fake.calls] == [
        T._FIELDS["buying_power_path"], T._FIELDS["buying_power_path"],
        T._FIELDS["holdings_path"],
        T._FIELDS["buying_power_path"], T._FIELDS["buying_power_path"],
    ]


async def test_create_uses_decimal_strings_and_a_stable_client_order_id():
    fake = FakeOrders([
        {"orderId": "T-1", "clientOrderId": "ignored"},
        {"orderId": "T-1", "clientOrderId": "ignored"},
    ])
    broker = await _order_broker(fake)
    order = _order(quantity="10.000")

    assert await broker._venue_submit(order) == "T-1"
    assert await broker._venue_submit(order) == "T-1"

    first, second = (call["json"] for call in fake.calls)
    assert first == second
    assert first["quantity"] == "10" and isinstance(first["quantity"], str)
    assert first["price"] == "70000" and isinstance(first["price"], str)
    assert first["timeInForce"] == "DAY"
    assert first["clientOrderId"] == order.id
    assert broker._pending_fills == [], "create response is not an execution feed"


async def test_create_retries_a_transport_timeout_with_the_same_idempotency_key():
    request = httpx.Request("POST", "https://example.invalid/api/v1/orders")
    fake = FakeOrders([
        httpx.ReadTimeout("late response", request=request),
        {"orderId": "T-1", "clientOrderId": "ord-fixed"},
    ])
    broker = await _order_broker(fake)
    order = _order()
    order.id = "ord-fixed"

    assert await broker._venue_submit(order) == "T-1"
    assert len(fake.calls) == 2
    assert fake.calls[0]["json"] == fake.calls[1]["json"]
    assert fake.calls[0]["json"]["clientOrderId"] == "ord-fixed"


async def test_create_retries_http_500_with_the_same_idempotency_key(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(500, json={
                "error": {"code": "internal-error", "message": "temporary"},
            })
        return httpx.Response(200, json={
            "result": {"orderId": "T-1", "clientOrderId": "ord-fixed"},
        })

    broker = await _order_broker_with_http_client(monkeypatch, handler)
    order = _order()
    order.id = "ord-fixed"
    try:
        assert await broker._venue_submit(order) == "T-1"
    finally:
        await broker.client.close()

    assert len(seen) == 2
    assert seen[0].content == seen[1].content
    assert broker.fill_channel_ok


async def test_two_http_500_order_responses_lock_the_fill_channel(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500, json={
            "error": {"code": "internal-error", "message": "temporary"},
        })

    broker = await _order_broker_with_http_client(monkeypatch, handler)
    try:
        with pytest.raises(BrokerageError, match="두 번 받지 못했습니다"):
            await broker._venue_submit(_order())
    finally:
        await broker.client.close()

    assert len(seen) == 2
    assert seen[0].content == seen[1].content
    assert not broker.fill_channel_ok


async def test_malformed_2xx_order_response_is_retried_with_the_same_key():
    fake = FakeOrders([
        {"unexpected": "no order id"},
        {"orderId": "T-1"},
    ])
    broker = await _order_broker(fake)
    order = _order()

    assert await broker._venue_submit(order) == "T-1"
    assert len(fake.calls) == 2
    assert fake.calls[0]["json"] == fake.calls[1]["json"]
    assert broker.fill_channel_ok


async def test_two_malformed_2xx_order_responses_lock_the_fill_channel():
    fake = FakeOrders([[], {}])
    broker = await _order_broker(fake)

    with pytest.raises(BrokerageError, match="두 번 받지 못했습니다"):
        await broker._venue_submit(_order())

    assert len(fake.calls) == 2
    assert not broker.fill_channel_ok


async def test_invalid_json_2xx_order_response_is_retried(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                200, content=b"{", headers={"content-type": "application/json"},
            )
        return httpx.Response(200, json={"result": {"orderId": "T-1"}})

    broker = await _order_broker_with_http_client(monkeypatch, handler)
    try:
        assert await broker._venue_submit(_order()) == "T-1"
    finally:
        await broker.client.close()

    assert len(seen) == 2
    assert seen[0].content == seen[1].content


@pytest.mark.parametrize("status_code", [400, 401, 403, 422, 429])
async def test_documented_4xx_order_rejection_is_not_retried_or_locked(
    monkeypatch, status_code,
):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status_code, json={
            "error": {"code": "rejected", "message": "not accepted"},
        })

    broker = await _order_broker_with_http_client(monkeypatch, handler)
    try:
        with pytest.raises(BrokerageError, match=f"실패 \\({status_code}\\)"):
            await broker._venue_submit(_order())
    finally:
        await broker.client.close()

    assert len(seen) == 1
    assert broker.fill_channel_ok


async def test_two_unknown_submit_timeouts_lock_out_the_next_same_process_order():
    request = httpx.Request("POST", "https://example.invalid/api/v1/orders")
    fake = FakeOrders([
        httpx.ReadTimeout("first timeout", request=request),
        httpx.ReadTimeout("second timeout", request=request),
    ])
    broker = await _order_broker(fake)
    position = broker.portfolio.position(KRX)
    position.quantity = Decimal("20")
    position.avg_price = 70_000.0
    first = _order(side=OrderSide.SELL)

    submitted = await broker.submit(first)
    second = await broker.submit(_order(side=OrderSide.SELL))

    assert submitted.status is OrderStatus.REJECTED
    assert second.status is OrderStatus.REJECTED
    assert "체결 조회 채널이 끊겼습니다" in second.reject_reason
    assert [call["method"] for call in fake.calls] == ["GET", "POST", "POST"]
    assert len([call for call in fake.calls if call["method"] == "POST"]) == 2, (
        "the second local order must not reach Toss"
    )
    assert not broker.fill_channel_ok


async def test_create_canonicalizes_an_unsafe_local_id_without_losing_stability():
    fake = FakeOrders([
        {"orderId": "T-1"}, {"orderId": "T-1"},
    ])
    broker = await _order_broker(fake)
    order = _order()
    order.id = "사용자 입력/" + "x" * 80

    await broker._venue_submit(order)
    await broker._venue_submit(order)

    keys = [call["json"]["clientOrderId"] for call in fake.calls]
    assert keys[0] == keys[1]
    assert len(keys[0]) <= 36
    assert keys[0].startswith("q_")
    assert all(ch.isalnum() or ch in "-_" for ch in keys[0])


async def test_create_rejects_an_empty_local_id_before_http():
    fake = FakeOrders()
    broker = await _order_broker(fake)
    order = _order()
    order.id = ""

    with pytest.raises(BrokerageError, match="로컬 order id가 없어"):
        await broker._venue_submit(order)

    assert fake.calls == []


@pytest.mark.parametrize("order_type", [OrderType.STOP, OrderType.STOP_LIMIT])
async def test_create_rejects_unsupported_order_types_before_http(order_type):
    fake = FakeOrders()
    broker = await _order_broker(fake)

    with pytest.raises(BrokerageError, match="MARKET 또는 LIMIT"):
        await broker._venue_submit(_order(order_type=order_type))

    assert fake.calls == []


@pytest.mark.parametrize("quantity", ["0", "-1", "NaN"])
async def test_create_rejects_nonpositive_or_nonfinite_quantity_before_http(quantity):
    fake = FakeOrders()
    broker = await _order_broker(fake)

    with pytest.raises(BrokerageError, match="0보다 큰 유한한"):
        await broker._venue_submit(_order(quantity=quantity))

    assert fake.calls == []


async def test_create_rejects_a_long_only_oversell_before_http():
    fake = FakeOrders()
    broker = await _order_broker(fake)
    broker.portfolio.position(KRX).quantity = Decimal("5")

    with pytest.raises(BrokerageError, match="매도 수량 6.*보유 수량 5"):
        await broker._venue_submit(
            _order(side=OrderSide.SELL, quantity="6")
        )

    assert fake.calls == []


async def test_create_allows_an_exact_long_only_exit():
    fake = FakeOrders([{"orderId": "T-1"}])
    broker = await _order_broker(fake)
    broker.portfolio.position(KRX).quantity = Decimal("5")

    assert await broker._venue_submit(
        _order(side=OrderSide.SELL, quantity="5")
    ) == "T-1"
    assert fake.calls[0]["json"]["side"] == "SELL"
    assert fake.calls[0]["json"]["quantity"] == "5"


@pytest.mark.parametrize(
    ("side", "quantity"),
    [
        (OrderSide.BUY, "1"),
        (OrderSide.SELL, "1"),
        (OrderSide.SELL, "0.5"),
    ],
)
async def test_us_market_quantity_order_uses_the_official_decimal_body(side, quantity):
    fake = FakeOrders([{"orderId": "T-1"}])
    broker = await _order_broker(fake)
    broker.portfolio.position(US).quantity = Decimal("2")
    order = Order(US, side, Decimal(quantity), type=OrderType.MARKET)

    assert await broker._venue_submit(order) == "T-1"

    body = fake.calls[0]["json"]
    assert body["symbol"] == "AAPL"
    assert body["side"] == side.value.upper()
    assert body["orderType"] == "MARKET"
    assert body["quantity"] == quantity
    assert "price" not in body


async def test_us_market_exit_reaches_toss_instead_of_being_trapped():
    fake = FakeOrders([{"orderId": "T-1"}])
    broker = await _order_broker(fake)
    position = broker.portfolio.position(US)
    position.quantity = Decimal("1")
    position.avg_price = 180.0
    broker.portfolio.mark(US, 185.0)
    order = Order(US, OrderSide.SELL, Decimal("1"), type=OrderType.MARKET)

    submitted = await broker.submit(order)

    assert submitted.status is OrderStatus.SUBMITTED
    assert fake.calls[-1]["path"] == T._FIELDS["orders_path"]
    assert fake.calls[-1]["json"]["orderType"] == "MARKET"
    assert "price" not in fake.calls[-1]["json"]


@pytest.mark.parametrize(
    ("held", "quantity"),
    [
        ("0.5", "0.5"),
        ("1.5", "0.5"),
    ],
)
async def test_live_submit_accepts_exact_or_lower_fractional_us_market_sell(
    held, quantity,
):
    fake = FakeOrders([{"orderId": "T-1"}])
    broker = await _order_broker(fake)
    position = broker.portfolio.position(US)
    position.quantity = Decimal(held)
    position.avg_price = 180.0
    broker.portfolio.mark(US, 185.0)

    submitted = await broker.submit(Order(
        US, OrderSide.SELL, Decimal(quantity), type=OrderType.MARKET,
    ))

    assert submitted.status is OrderStatus.SUBMITTED
    assert fake.calls == [
        {
            "method": "GET",
            "path": T._FIELDS["orders_path"],
            "params": {"status": "OPEN"},
            "json": None,
            "account": True,
        },
        {
            "method": "POST",
            "path": T._FIELDS["orders_path"],
            "params": None,
            "json": {
                "clientOrderId": submitted.id,
                "symbol": "AAPL",
                "side": "SELL",
                "quantity": quantity,
                "orderType": "MARKET",
                "timeInForce": "DAY",
            },
            "account": True,
        },
    ]


async def test_strategy_target_zero_sends_fractional_us_market_sell_to_toss():
    fake = FakeOrders([{"orderId": "T-1"}])
    broker = await _order_broker(fake)
    position = broker.portfolio.position(US)
    position.quantity = Decimal("0.5")
    position.avg_price = 180.0

    now = utcnow()
    ctx = Context(
        SimClock(now), broker.portfolio, EventBus(),
        timeframe="1d", run_mode=RunMode.LIVE,
    )

    async def no_insights(_ctx, _bars):
        return []

    alpha = SimpleNamespace(update=no_insights)
    portfolio_model = SimpleNamespace(create_targets=lambda _ctx, _insights: [
        PortfolioTarget(US, Decimal("0"), tag="strategy flat"),
    ])
    engine = Engine(
        ctx, alpha, portfolio_model, ImmediateExecution(min_order_notional=1), broker,
    )
    bar = Bar(
        US, now - timedelta(days=1), 184.0, 186.0, 183.0, 185.0, 1_000_000, "1d",
    )

    await engine.on_bars({US.key: bar}, settle=False)

    assert len(engine.orders) == 1
    assert engine.orders[0].status is OrderStatus.SUBMITTED
    assert engine.orders[0].side is OrderSide.SELL
    assert engine.orders[0].quantity == Decimal("0.5")
    assert engine.orders[0].type is OrderType.MARKET
    assert fake.calls[-1]["json"]["quantity"] == "0.5"
    assert fake.calls[-1]["json"]["orderType"] == "MARKET"
    assert "price" not in fake.calls[-1]["json"]


async def test_limit_execution_forces_fractional_target_flat_to_market():
    fake = FakeOrders([{"orderId": "T-1"}])
    broker = await _order_broker(fake)
    position = broker.portfolio.position(US)
    position.quantity = Decimal("0.5")
    position.avg_price = 180.0
    broker.portfolio.mark(US, 185.0)
    now = utcnow()
    ctx = Context(SimClock(now), broker.portfolio, EventBus(), run_mode=RunMode.LIVE)
    ctx.set_quote(Quote(US, now, bid=184.9, ask=185.1))
    execution = LimitExecution(
        offset_bps=5, urgent_after_bars=3, min_order_notional=1,
    )
    Engine(ctx, SimpleNamespace(), SimpleNamespace(), execution, broker)

    orders = execution.execute(ctx, [PortfolioTarget(US, Decimal("0"))])
    assert len(orders) == 1
    assert orders[0].quantity == Decimal("0.5")
    assert orders[0].type is OrderType.MARKET
    assert orders[0].limit_price is None

    submitted = await broker.submit(orders[0])

    assert submitted.status is OrderStatus.SUBMITTED
    assert fake.calls[-1]["json"]["orderType"] == "MARKET"
    assert "price" not in fake.calls[-1]["json"]


async def test_manual_close_fractional_us_position_reaches_toss():
    fake = FakeOrders([{"orderId": "T-1"}])
    broker = await _order_broker(fake)
    position = broker.portfolio.position(US)
    position.quantity = Decimal("0.5")
    position.avg_price = 180.0
    broker.portfolio.mark(US, 185.0)
    now = utcnow()
    ctx = Context(
        SimClock(now), broker.portfolio, EventBus(), run_mode=RunMode.LIVE,
    )
    ctx.set_quote(Quote(US, now, bid=184.9, ask=185.1))
    engine = Engine(
        ctx, SimpleNamespace(), SimpleNamespace(),
        ImmediateExecution(min_order_notional=1), broker,
    )
    request = engine.manual.close(US)

    assert await engine.flush_manual() == 1

    assert request.status == "submitted"
    assert len(engine.orders) == 1
    assert engine.orders[0].status is OrderStatus.SUBMITTED
    assert engine.orders[0].quantity == Decimal("0.5")
    assert engine.orders[0].type is OrderType.MARKET
    assert fake.calls[-1]["json"]["quantity"] == "0.5"
    assert fake.calls[-1]["json"]["orderType"] == "MARKET"
    assert "price" not in fake.calls[-1]["json"]


async def test_manual_fractional_sell_quantity_is_explicitly_skipped_use_close():
    fake = FakeOrders()
    broker = await _order_broker(fake)
    position = broker.portfolio.position(US)
    position.quantity = Decimal("1.5")
    position.avg_price = 180.0
    broker.portfolio.mark(US, 185.0)
    now = utcnow()
    ctx = Context(
        SimClock(now), broker.portfolio, EventBus(), run_mode=RunMode.LIVE,
    )
    ctx.set_quote(Quote(US, now, bid=184.9, ask=185.1))
    engine = Engine(
        ctx, SimpleNamespace(), SimpleNamespace(),
        ImmediateExecution(min_order_notional=1), broker,
    )
    request = engine.manual.sell(US, quantity=Decimal("0.5"))

    assert await engine.flush_manual() == 0

    assert request.status == "skipped"
    assert request.detail == "최소 주문 단위보다 작습니다"
    assert engine.orders == []
    assert fake.calls == []


@pytest.mark.parametrize(
    ("side", "order_type", "quantity", "limit_price", "held", "reason"),
    [
        (
            OrderSide.BUY, OrderType.MARKET, "0.5", None, "0",
            "미국 주식 MARKET SELL만 지원",
        ),
        (
            OrderSide.SELL, OrderType.LIMIT, "0.5", 185.0, "1",
            "미국 주식 MARKET SELL만 지원",
        ),
        (
            OrderSide.SELL, OrderType.MARKET, "0.1234567", None, "1",
            "소수점 6자리까지만 지원",
        ),
        (
            OrderSide.SELL, OrderType.MARKET, "0.6", None, "0.5",
            "보유 수량 0.5.*넘습니다",
        ),
    ],
)
async def test_live_submit_rejects_unsupported_or_oversold_fractional_us_orders(
    side, order_type, quantity, limit_price, held, reason,
):
    fake = FakeOrders()
    broker = await _order_broker(fake)
    position = broker.portfolio.position(US)
    position.quantity = Decimal(held)
    position.avg_price = 180.0
    broker.portfolio.mark(US, 185.0)

    async def ready_sync():
        broker._capital_ready = True
        broker._venue_buying_power = 1_000_000.0
        return {}

    broker.sync = ready_sync
    submitted = await broker.submit(Order(
        US, side, Decimal(quantity), type=order_type, limit_price=limit_price,
    ))

    assert submitted.status is OrderStatus.REJECTED
    assert submitted.reject_reason
    assert re.search(reason, submitted.reject_reason)
    assert fake.calls == []


async def test_cancel_uses_the_official_post_cancel_path():
    order = _order()
    order.broker_id = "T-1"
    fake = FakeOrders([_detail(
        order, status="CANCELED", filled="0", amount=None,
        commission=None, tax=None,
    )])
    broker = await _order_broker(fake)

    assert await broker._venue_cancel(order) is True
    assert fake.calls[0] == {
        "method": "POST",
        "path": "/api/v1/orders/T-1/cancel",
        "params": None,
        "json": {},
        "account": True,
    }
    assert fake.calls[1]["method"] == "GET"
    assert fake.calls[1]["path"] == "/api/v1/orders/T-1"


async def test_cancel_waits_for_pending_cancel_to_become_canceled():
    order = _order()
    order.broker_id = "T-1"
    order.status = OrderStatus.SUBMITTED
    fake = FakeOrders([
        _detail(order, status="PENDING_CANCEL", filled="0", amount=None,
                commission=None, tax=None),
        _detail(order, status="CANCELED", filled="0", amount=None,
                commission=None, tax=None),
    ])
    broker = await _order_broker(fake)
    broker._orders[order.id] = order

    assert await broker.cancel(order) is True

    assert order.status is OrderStatus.CANCELED
    assert order.id not in broker._orders
    assert [call["method"] for call in fake.calls] == ["POST", "GET", "GET"]


async def test_cancel_confirmation_allows_five_one_second_rate_gaps(monkeypatch):
    clock = {"now": 0.0}

    class OneSecondRateGapFake(FakeOrders):
        async def request(self, method, path, *, params=None, json=None, account=False):
            clock["now"] += 1.0
            return await super().request(
                method, path, params=params, json=json, account=account,
            )

    monkeypatch.setattr(T.time, "monotonic", lambda: clock["now"])
    order = _order()
    order.broker_id = "T-1"
    order.status = OrderStatus.SUBMITTED
    pending = _detail(
        order, status="PENDING_CANCEL", filled="0", amount=None,
        commission=None, tax=None,
    )
    canceled = _detail(
        order, status="CANCELED", filled="0", amount=None,
        commission=None, tax=None,
    )
    fake = OneSecondRateGapFake([
        *[{**pending, "execution": dict(pending["execution"])} for _ in range(4)],
        canceled,
    ])
    broker = await _order_broker(fake)
    broker._orders[order.id] = order

    assert await broker.cancel(order) is True

    assert order.status is OrderStatus.CANCELED
    assert [call["method"] for call in fake.calls] == ["POST"] + ["GET"] * 5
    assert clock["now"] == 6.0


async def test_cancel_waits_for_pending_cancel_to_become_filled_and_keeps_deltas():
    order = _order()
    order.broker_id = "T-1"
    order.status = OrderStatus.SUBMITTED
    fake = FakeOrders([
        _detail(
            order, status="PENDING_CANCEL", filled="3", amount="210000",
            commission="30", tax="0", average="70000",
        ),
        _detail(
            order, status="FILLED", filled="10", amount="710000",
            commission="100", tax="1400", average="71000",
            filled_at="2026-08-25T09:02:00+09:00",
        ),
    ])
    broker = await _order_broker(fake)
    broker._orders[order.id] = order

    assert await broker.cancel(order) is True
    fills = await broker.poll_fills()

    assert order.status is OrderStatus.FILLED
    assert order.id not in broker._orders
    assert [fill.quantity for fill in fills] == [Decimal("3"), Decimal("7")]
    assert sum(fill.quantity for fill in fills) == Decimal("10")
    assert sum(fill.fee for fill in fills) == 1500.0


async def test_cancel_rejected_status_never_orphans_the_original_order():
    order = _order()
    order.broker_id = "T-1"
    order.status = OrderStatus.SUBMITTED
    fake = FakeOrders([_detail(
        order, status="CANCEL_REJECTED", filled="0", amount=None,
        commission=None, tax=None,
    )])
    broker = await _order_broker(fake)
    broker._orders[order.id] = order

    assert await broker.cancel(order) is False

    assert order.status is OrderStatus.SUBMITTED
    assert broker._orders[order.id] is order
    assert not broker.fill_channel_ok


async def test_cancel_detail_timeout_never_removes_the_original_order():
    order = _order()
    order.broker_id = "T-1"
    order.status = OrderStatus.SUBMITTED
    request = httpx.Request("GET", "https://example.invalid/api/v1/orders/T-1")
    fake = FakeOrders([httpx.ReadTimeout("detail timeout", request=request)])
    broker = await _order_broker(fake)
    broker._orders[order.id] = order

    assert await broker.cancel(order) is False

    assert order.status is OrderStatus.SUBMITTED
    assert broker._orders[order.id] is order
    assert not broker.fill_channel_ok


async def test_shutdown_books_a_verified_cancel_race_fill_when_later_polls_fail():
    """A later outage cannot erase a partial fill already proven during cancel."""
    request = httpx.Request("GET", "https://example.invalid/api/v1/orders/T-1")
    order = _order()
    order.broker_id = "T-1"
    order.status = OrderStatus.SUBMITTED
    lifecycle: list[str] = []

    class ClosingOrders(FakeOrders):
        async def close(self):
            lifecycle.append("close")

    fake = ClosingOrders([
        _detail(
            order, status="PENDING_CANCEL", filled="3", amount="210000",
            commission="30", tax="0", average="70000",
        ),
        httpx.ReadTimeout("cancel detail failed", request=request),
        httpx.ReadTimeout("shutdown cancel detail failed", request=request),
        httpx.ReadTimeout("final fill poll failed", request=request),
    ])
    broker = await _order_broker(fake)
    broker._orders[order.id] = order

    # The first detail proves three fills, but the next detail cannot prove the
    # terminal status. The order and its cached fill must both remain tracked.
    assert await broker.cancel(order) is False
    assert order.filled_qty == Decimal("3")
    assert broker.portfolio.quantity(KRX) == 0
    assert len(broker._pending_fills) == 1

    clock = SimClock(utcnow())
    bus = EventBus()
    bus.on(EventType.ORDER_FILLED, lambda _event: lifecycle.append("booked"))
    ctx = Context(clock, broker.portfolio, bus)
    budget = TradingBudget(clock=clock)
    engine = Engine.__new__(Engine)
    engine.ctx = ctx
    engine.brokerage = broker
    engine.budget = budget
    engine.risk = SimpleNamespace(on_trade_closed=lambda *_args: None)
    engine._started = True

    # Engine.stop retries cancel, then performs its final poll. Both lookups
    # fail, so the error must propagate; the network-free cached fill is still
    # booked before the broker/state boundary closes.
    with pytest.raises(UnsafeShutdownError, match="상세 조회/해석 실패"):
        await engine.stop()

    assert broker.portfolio.quantity(KRX) == Decimal("3")
    assert broker.portfolio.cash == pytest.approx(10_000_000 - 210_000 - 30)
    assert broker.portfolio.total_fees == 30.0
    assert budget.today is not None and budget.today.fees == 30.0
    assert broker._pending_fills == []
    assert lifecycle == ["booked", "close"]
    assert [call["method"] for call in fake.calls] == [
        "POST", "GET", "GET", "GET", "GET", "GET",
    ]
    assert engine._started is False


async def test_cancel_pending_without_terminal_confirmation_stays_tracked():
    order = _order()
    order.broker_id = "T-1"
    order.status = OrderStatus.SUBMITTED
    pending = _detail(
        order, status="PENDING_CANCEL", filled="0", amount=None,
        commission=None, tax=None,
    )
    fake = FakeOrders([
        {**pending, "execution": dict(pending["execution"])} for _ in range(5)
    ])
    broker = await _order_broker(fake)
    broker._orders[order.id] = order

    assert await broker.cancel(order) is False

    assert order.status is OrderStatus.SUBMITTED
    assert broker._orders[order.id] is order
    assert order.meta["toss_cancel_operation_id"] == "cancel-operation-1"


async def test_restart_fails_closed_on_an_unowned_remote_open_order():
    foreign = {"orderId": "REMOTE-1", "symbol": "005930", "status": "PENDING"}
    fake = FakeOrders(open_orders=[foreign])
    broker = await _order_broker(fake)

    with pytest.raises(BrokerageError, match="자동 복원·취소하지 않습니다"):
        await broker.connect()

    assert len(fake.calls) == 1
    assert fake.calls[0]["method"] == "GET"
    assert not any(call["method"] == "POST" for call in fake.calls)
    assert broker._orders == {}, "foreign app orders must never become local cancel targets"


async def test_runtime_app_order_blocks_the_next_bot_order_before_post():
    fake = FakeOrders(open_orders=[])
    broker = await _order_broker(fake)

    # This is the state immediately after connect: the account was clean and
    # its capital snapshot was already proven.  The user then creates an order
    # in the Toss app while the process remains alive.
    await broker._assert_owned_remote_open_orders()
    fake.open_orders = [{
        "orderId": "APP-OPEN-1", "symbol": KRX.ticker,
        "side": "BUY", "orderType": "LIMIT", "timeInForce": "DAY",
        "status": "PENDING", "price": "70000", "quantity": "1",
        "currency": "KRW", "orderedAt": ORDERED_AT,
        "execution": {
            "filledQuantity": "0", "averageFilledPrice": None,
            "filledAmount": None, "commission": None, "tax": None,
            "filledAt": None, "settlementDate": None,
        },
    }]
    broker._capital_ready = True
    broker._venue_buying_power = 10_000_000.0

    async def ready_sync():
        broker._capital_ready = True
        broker._venue_buying_power = 10_000_000.0
        return {"ok": True, "capital_ready": True}

    broker.sync = ready_sync
    submitted = await broker.submit(_order(quantity="1"))

    assert submitted.status is OrderStatus.REJECTED
    assert "소유권을 확인할 수 없는 미체결 주문" in submitted.reject_reason
    assert not broker.account_ready
    assert [call["method"] for call in fake.calls] == ["GET", "GET"]
    assert all(call["path"] == T._FIELDS["orders_path"] for call in fake.calls)
    assert not any(call["method"] == "POST" for call in fake.calls)
    assert broker._orders == {}, "foreign app orders must never become local cancel targets"


async def test_restart_fails_closed_when_open_order_pagination_is_not_official():
    class BrokenOpenOrders(FakeOrders):
        async def request(self, method, path, *, params=None, json=None, account=False):
            self.calls.append({
                "method": method, "path": path, "params": params,
                "json": json, "account": account,
            })
            return {"orders": [], "nextCursor": "unexpected", "hasNext": True}

    broker = await _order_broker(BrokenOpenOrders())
    with pytest.raises(BrokerageError, match="pagination"):
        await broker.connect()


async def test_connect_rechecks_for_an_app_order_created_during_capital_sync():
    class AppearingOrder(FakeOrders):
        def __init__(self):
            super().__init__()
            self.open_reads = 0

        async def request(self, method, path, *, params=None, json=None, account=False):
            self.calls.append({
                "method": method, "path": path, "params": params,
                "json": json, "account": account,
            })
            if method == "GET" and path == T._FIELDS["orders_path"]:
                self.open_reads += 1
                rows = [] if self.open_reads == 1 else [{"orderId": "APP-1"}]
                return {"orders": rows, "nextCursor": None, "hasNext": False}
            if path == T._FIELDS["buying_power_path"]:
                return {"currency": "KRW", "cashBuyingPower": "420000"}
            if path == T._FIELDS["holdings_path"]:
                return _holdings(krw="0", usd=None, items=[])
            raise AssertionError(path)

    fake = AppearingOrder()
    broker = await _order_broker(fake)
    broker.reconcile_on_start = True

    with pytest.raises(BrokerageError, match="미체결 주문이 1건"):
        await broker.connect()

    assert fake.open_reads == 2
    assert not broker.account_ready


async def test_read_only_account_broker_does_not_apply_the_live_open_order_gate():
    fake = FakeOrders(open_orders=[{"orderId": "APP-ORDER"}])
    broker = await _order_broker(fake, live=False)

    await broker.connect()

    assert fake.calls == [], "a read-only account view must not inspect or cancel app orders"


async def test_nested_execution_cumulative_deltas_are_booked_exactly_once():
    order = _order()
    order.broker_id = "T-1"
    order.status = OrderStatus.SUBMITTED
    first = _detail(
        order, status="PARTIAL_FILLED", filled="4", amount="280000",
        commission="40", tax="0", average="70000",
    )
    duplicate = dict(first)
    duplicate["execution"] = dict(first["execution"])
    final = _detail(
        order, status="FILLED", filled="10", amount="710000",
        commission="100", tax="1400", average="71000",
        filled_at="2026-08-25T09:02:00+09:00",
    )
    fake = FakeOrders([first, duplicate, final])
    broker = await _order_broker(fake)
    broker._orders[order.id] = order

    one = await broker.poll_fills()
    two = await broker.poll_fills()
    three = await broker.poll_fills()

    assert [(fill.quantity, fill.price, fill.fee) for fill in one] == [
        (Decimal("4"), 70_000.0, 40.0),
    ]
    assert two == []
    assert len(three) == 1
    assert three[0].quantity == Decimal("6")
    assert three[0].price == pytest.approx(430_000 / 6)
    assert three[0].fee == 1460.0
    assert order.filled_qty == Decimal("10")
    assert order.fees == 1500.0
    assert order.status is OrderStatus.FILLED


async def test_a_partial_fill_is_booked_before_terminal_cancel_status():
    order = _order()
    order.broker_id = "T-1"
    order.status = OrderStatus.SUBMITTED
    fake = FakeOrders([_detail(
        order, status="CANCELED", filled="3", amount="210000",
        commission="30", tax="0", average="70000",
    )])
    broker = await _order_broker(fake)
    broker._orders[order.id] = order

    fills = await broker.poll_fills()

    assert [fill.quantity for fill in fills] == [Decimal("3")]
    assert order.filled_qty == Decimal("3")
    assert order.status is OrderStatus.CANCELED


@pytest.mark.parametrize(
    ("remote_status", "local_status"),
    [
        ("CANCELED", OrderStatus.CANCELED),
        ("REJECTED", OrderStatus.REJECTED),
    ],
)
async def test_periodic_terminal_detail_releases_truth_guards_only_after_fresh_sync(
    remote_status,
    local_status,
):
    order = _order()
    order.broker_id = "T-1"
    terminal = _detail(
        order, status=remote_status, filled="0", amount=None,
        commission=None, tax=None,
    )
    fake = FakeOrders([{"orderId": "T-1"}, terminal])
    broker = await _order_broker(fake)
    real_sync = broker.sync

    async def pre_submit_sync():
        return {"ok": True, "capital_ready": True}

    broker.sync = pre_submit_sync
    broker._capital_ready = True
    broker._venue_buying_power = broker.portfolio.cash
    order.broker_id = None
    assert (await broker.submit(order)).status is OrderStatus.SUBMITTED
    broker.sync = real_sync

    assert order.id in broker._capital_order_checkpoints
    assert order.id in broker._capital_reservations
    assert await broker.poll_fills() == []
    assert order.status is local_status
    assert order.id in broker._capital_terminal_observed

    async def capital():
        return {
            "currency": "KRW",
            "cash": broker.portfolio.cash,
            "holdings_value": 0.0,
        }

    async def positions():
        return {}

    async def costs():
        return {}

    broker._venue_capital = capital
    broker._venue_positions = positions
    broker._venue_costs = costs
    report = await broker.sync()

    assert report["ok"] and report["capital_ready"]
    assert order.id not in broker._capital_order_checkpoints
    assert order.id not in broker._capital_reservations
    assert order.id not in broker._capital_terminal_observed


async def test_decreasing_cumulative_amount_fails_without_a_duplicate_fill():
    order = _order()
    order.broker_id = "T-1"
    order.status = OrderStatus.SUBMITTED
    first = _detail(
        order, status="PARTIAL_FILLED", filled="4", amount="280000",
        commission="40", tax="0", average="70000",
    )
    regressed = _detail(
        order, status="PARTIAL_FILLED", filled="4", amount="279999",
        commission="40", tax="0", average="69999.75",
    )
    broker = await _order_broker(FakeOrders([first, regressed]))
    broker._orders[order.id] = order

    assert len(await broker.poll_fills()) == 1
    with pytest.raises(BrokerageError, match="누적 체결 금액이 이전 조회보다 줄었습니다"):
        await broker.poll_fills()

    assert order.filled_qty == Decimal("4")
    assert not broker.fill_channel_ok


@pytest.mark.parametrize(
    ("status", "message"),
    [
        ("CANCEL_REJECTED", "작업 레코드를 원주문 상태로 연결할 수 없습니다"),
        ("REPLACE_REJECTED", "작업 레코드를 원주문 상태로 연결할 수 없습니다"),
        ("REPLACED", "후속 orderId를 추적할 수 없습니다"),
    ],
)
async def test_operation_and_replaced_statuses_fail_closed(status, message):
    order = _order()
    order.broker_id = "T-1"
    order.status = OrderStatus.SUBMITTED
    detail = _detail(
        order, status=status, filled="0", amount=None,
        commission=None, tax=None, average=None,
    )
    broker = await _order_broker(FakeOrders([detail]))
    broker._orders[order.id] = order

    with pytest.raises(BrokerageError, match=message):
        await broker.poll_fills()

    assert order.status is OrderStatus.SUBMITTED
    assert not broker.fill_channel_ok


async def test_malformed_legacy_detail_fails_closed_without_orphaning_reservation():
    order = _order()
    malformed = {
        "orderId": "T-1", "filledQuantity": "0", "averagePrice": None,
    }
    fake = FakeOrders([{"orderId": "T-1"}, malformed])
    broker = await _order_broker(fake)
    real_sync = broker.sync

    async def pre_submit_sync():
        return {"ok": True, "capital_ready": True}

    broker.sync = pre_submit_sync
    broker._capital_ready = True
    broker._venue_buying_power = broker.portfolio.cash
    assert (await broker.submit(order)).status is OrderStatus.SUBMITTED
    broker.sync = real_sync
    assert order.id in broker._capital_order_checkpoints
    assert order.id in broker._capital_reservations

    assert await broker.cancel(order) is False

    assert order.status is OrderStatus.SUBMITTED
    assert broker._orders[order.id] is order
    assert order.id in broker._capital_order_checkpoints
    assert order.id in broker._capital_reservations
    assert order.id not in broker._capital_terminal_observed
    assert not broker.fill_channel_ok
    assert "취소 후 원주문 상태를 확인하지 못했습니다" in broker.fill_channel_error


async def test_setup_verification_checks_account_truth_before_reporting_success(
    monkeypatch,
):
    async def token(*_args, **_kwargs):
        return "token"

    async def overview(_self):
        return {
            "cash_buying_power": {"KRW": 426_319.0},
            "market_value": {"KRW": 0.0},
        }

    async def quote(_self, symbol):
        return Quote(symbol, utcnow(), 70_000.0, 70_100.0)

    async def close(_self):
        return None

    monkeypatch.setattr(T, "toss_token", token)
    monkeypatch.setattr(T.TossBrokerage, "account_overview", overview)
    monkeypatch.setattr(T.TossBrokerage, "close", close)
    monkeypatch.setattr(T.TossProvider, "quote", quote)
    monkeypatch.setattr(T.TossProvider, "close", close)

    result = await api_server._verify_toss({
        "TOSS_CLIENT_ID": "test-client",
        "TOSS_CLIENT_SECRET": "test-secret",
        "TOSS_ACCOUNT_NO": "12345678901",
    })

    assert result["ok"] is True
    assert [step["step"] for step in result["steps"]] == [
        "OAuth 토큰 발급", "실계좌 식별·잔고 조회", "현재가 조회 (삼성전자)",
    ]


async def test_setup_verification_rejects_a_token_that_cannot_read_the_account(
    monkeypatch,
):
    quote_called = False

    async def token(*_args, **_kwargs):
        return "token"

    async def overview(_self):
        raise BrokerageError("account unavailable")

    async def quote(_self, symbol):
        nonlocal quote_called
        quote_called = True
        return Quote(symbol, utcnow(), 70_000.0, 70_100.0)

    async def close(_self):
        return None

    monkeypatch.setattr(T, "toss_token", token)
    monkeypatch.setattr(T.TossBrokerage, "account_overview", overview)
    monkeypatch.setattr(T.TossBrokerage, "close", close)
    monkeypatch.setattr(T.TossProvider, "quote", quote)
    monkeypatch.setattr(T.TossProvider, "close", close)

    result = await api_server._verify_toss({
        "TOSS_CLIENT_ID": "test-client",
        "TOSS_CLIENT_SECRET": "test-secret",
        "TOSS_ACCOUNT_NO": "12345678901",
    })

    assert result["ok"] is False
    assert result["steps"][-1]["step"] == "실계좌 식별·잔고 조회"
    assert quote_called is False, "계좌 truth 실패 뒤 검증 성공 경로를 계속 탑니다"
