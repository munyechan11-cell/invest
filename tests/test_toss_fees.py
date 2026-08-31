"""토스 체결에 실제 비용이 붙는가.

토스는 체결 응답에 수수료를 실어 주지 않는 자리가 있고, 어댑터는 그 자리에서
오랫동안 `fee=0.0` 을 그대로 `Fill` 에 넣었습니다. 회계층은 0 을 "모른다" 가
아니라 **"공짜"** 로 믿습니다 — 그래서 국내 매도마다 20bp(2026년 증권거래세)가
장부에서 통째로 사라졌고, 회전율이 높은 전략일수록 실거래 곡선이 실제보다
좋아 보였습니다.

여기서 검사하는 것은 **성질** 이지 계산식이 아닙니다. 요율표를 베껴 오면
구현이 틀려도 같이 틀린 채 통과하므로:

* 체결에 비용이 붙는가 (0원이 아닌가)
* 매도가 매수보다 비싼가, 그 차이가 **설정에 적힌** 세율과 맞는가
* 설정(`costs.sell_tax_bps`)이 실거래 어댑터까지 닿는가
* 단가를 모르는 체결을 "수수료 0원" 으로 장부에 넣지 않는가

네트워크는 쓰지 않습니다 — `client` 를 가짜로 갈아 끼웁니다.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from quant.brokerage.toss_broker import TossBrokerage
from quant.config.schema import (
    BrokerConfig,
    CostConfig,
    ModelSpec,
    RunMode,
    StrategyConfig,
)
from quant.core.account import Portfolio
from quant.core.types import UTC, Order, OrderSide, OrderStatus, OrderType, Symbol
from quant.strategy.builder import build_brokerage, build_costs

KRX = Symbol("005930", venue="toss", quote_currency="KRW", tick_size=Decimal("100"))
US = Symbol("AAPL", venue="toss", quote_currency="USD", tick_size=Decimal("0.01"))
PRICE = 70_000.0
QTY = Decimal("10")
NOTIONAL = float(QTY) * PRICE
TS = datetime(2026, 7, 1, 4, 0, tzinfo=UTC)

CREDS = {"client_id": "test-id", "client_secret": "test-secret", "account_no": "1234"}


class FakeToss:
    """`_TossClient` 자리에 들어가는 가짜. 소켓을 열지 않습니다."""

    def __init__(self, reply: dict):
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    async def request(self, method, path, *, params=None, json=None, account=False):
        self.calls.append((method, path))
        # Live Toss orders now fail closed until an official account-capital
        # snapshot has succeeded.  Fee tests are not account tests, so give the
        # pre-submit reconciliation a valid empty account instead of letting the
        # order-response fixture masquerade as /holdings.
        if path == "/api/v1/holdings":
            return {
                "marketValue": {"amount": {"krw": "0", "usd": None}},
                "items": [],
            }
        if path == "/api/v1/buying-power":
            currency = str((params or {}).get("currency") or "KRW")
            return {"currency": currency, "cashBuyingPower": "10000000"}
        if method == "GET" and path == "/api/v1/orders":
            return {"orders": [], "nextCursor": None, "hasNext": False}
        return self.reply

    async def close(self):
        pass


def _brokerage(fee_model=None, reply: dict | None = None, *, live: bool = True):
    broker = TossBrokerage(Portfolio(10_000_000, "KRW"), live=live,
                           fee_model=fee_model, reconcile_on_start=False,
                           max_order_notional=1e12, **CREDS)
    broker.client = FakeToss(reply or {})
    return broker


def _order(side: OrderSide, symbol: Symbol = KRX, qty: Decimal = QTY) -> Order:
    order = Order(symbol, side, qty, type=OrderType.LIMIT, limit_price=PRICE)
    order.broker_id = "T-1"
    order.status = OrderStatus.SUBMITTED
    return order


def _kr_fee_model(sell_tax_bps: float | None = None):
    """설정 파일이 만드는 것과 **같은 경로** 로 비용 모델을 만듭니다."""
    cfg = StrategyConfig(name="t", alpha=[ModelSpec(type="ema_cross")],
                         costs=CostConfig(preset="kr_equity",
                                          sell_tax_bps=sell_tax_bps))
    return build_costs(cfg)[0]


async def _poll_once(broker: TossBrokerage, order: Order, remote: dict):
    # Keep the individual cost assertions compact while exercising the official
    # v1.2.14 Order.execution shape.  The adapter consumes cumulative amount and
    # cost fields, never a top-level averagePrice.
    if "execution" not in remote:
        filled = Decimal(str(remote.get("filledQuantity") or 0))
        average = remote.get("averagePrice")
        price = Decimal(str(average)) if average is not None else None
        amount = filled * price if price is not None else None
        fee = (broker._fill_fee(order, filled, float(price), TS)
               if filled > 0 and price is not None else None)
        remote = {
            "orderId": order.broker_id,
            "symbol": order.symbol.ticker,
            "side": "BUY" if order.side is OrderSide.BUY else "SELL",
            "orderType": "LIMIT" if order.type is OrderType.LIMIT else "MARKET",
            "timeInForce": "DAY",
            "status": "FILLED" if filled == order.quantity else "PARTIAL_FILLED",
            "price": str(order.limit_price) if order.limit_price is not None else None,
            "quantity": str(order.quantity),
            "currency": order.symbol.quote_currency,
            "orderedAt": "2026-07-01T12:59:00+09:00",
            "execution": {
                "filledQuantity": str(filled),
                "averageFilledPrice": str(price) if price is not None else None,
                "filledAmount": str(amount) if amount is not None else None,
                "commission": str(fee) if fee is not None else None,
                "tax": "0" if fee is not None else None,
                "filledAt": "2026-07-01T13:00:00+09:00" if filled else None,
                "settlementDate": None,
            },
        }
    broker._orders[order.id] = order
    broker.client = FakeToss(remote)
    return await broker.poll_fills()


# ── 비용이 붙는가 ────────────────────────────────────────────────────────
async def test_a_toss_fill_is_not_free():
    """가장 단순한 성질 하나 — 체결에 돈이 든다."""
    broker = _brokerage(_kr_fee_model())
    order = _order(OrderSide.BUY)
    fills = await _poll_once(broker, order, {"filledQuantity": "10",
                                             "averagePrice": "70000"})
    assert len(fills) == 1
    assert fills[0].fee > 0, "체결이 공짜로 기록됐습니다"


async def test_the_us_side_is_not_free_either():
    """미국 주식도 같은 코드 경로를 씁니다 — 통화만 다르다고 공짜가 되지 않습니다."""
    cfg = StrategyConfig(name="t", alpha=[ModelSpec(type="ema_cross")],
                         costs=CostConfig(preset="us_equity"))
    broker = _brokerage(build_costs(cfg)[0])
    order = _order(OrderSide.BUY, symbol=US)
    fills = await _poll_once(broker, order, {"filledQuantity": "10",
                                             "averagePrice": "190.5"})
    assert fills[0].fee > 0


async def test_a_bigger_fill_costs_more():
    """비용이 체결 규모를 따라가는가. 상수를 박아 두면 이게 깨집니다."""
    broker = _brokerage(_kr_fee_model())
    small = broker._fill_fee(_order(OrderSide.BUY, qty=Decimal("10")),
                             Decimal("10"), PRICE, TS)
    big = broker._fill_fee(_order(OrderSide.BUY, qty=Decimal("40")),
                           Decimal("40"), PRICE, TS)
    assert big > small


# ── 매도 거래세 ──────────────────────────────────────────────────────────
async def test_the_domestic_sell_side_carries_the_transaction_tax():
    """국내 매도에는 증권거래세가 붙습니다 — 매수보다 반드시 비쌉니다.

    차이를 **설정에 적힌** 세율로 검산합니다. 구현이 참고하는 요율표를 그대로
    베껴 오면, 그 표를 잘못 읽어도 테스트가 같이 틀려서 통과합니다.
    """
    broker = _brokerage(_kr_fee_model(sell_tax_bps=20.0))
    buy = await _poll_once(broker, _order(OrderSide.BUY),
                           {"filledQuantity": "10", "averagePrice": "70000"})
    sell = await _poll_once(broker, _order(OrderSide.SELL),
                            {"filledQuantity": "10", "averagePrice": "70000"})
    assert sell[0].fee > buy[0].fee
    assert sell[0].fee - buy[0].fee == pytest.approx(NOTIONAL * 20.0 / 10_000.0)


async def test_the_configured_cost_model_reaches_the_toss_adapter():
    """`costs.sell_tax_bps` 가 실거래 어댑터까지 닿는가.

    이 줄이 끊기면 어댑터는 조용히 프리셋 기본값으로 물러섭니다 — 사람이
    적어 넣은 세율이 백테스트에서만 적용되고 실거래에서는 무시되는데, 화면에는
    아무 표시도 나지 않습니다. 그래서 객체 동일성과 **실제 계산 결과** 를 둘 다
    봅니다: 33bp 는 저장소 어디에도 없는 값이라, 프리셋으로 물러서면 틀립니다.
    """
    cfg = StrategyConfig(
        name="t", mode=RunMode.DRY_RUN, alpha=[ModelSpec(type="ema_cross")],
        costs=CostConfig(preset="kr_equity", sell_tax_bps=33.0),
        broker=BrokerConfig(type="toss", params=dict(CREDS)),
    )
    fee, slippage, fill = build_costs(cfg)
    broker = build_brokerage(cfg, Portfolio(10_000_000, "KRW"), fee, slippage, fill)

    assert isinstance(broker, TossBrokerage)
    assert broker.fees is fee, "build_costs 가 만든 모델이 어댑터에 닿지 않습니다"
    sell = broker._fill_fee(_order(OrderSide.SELL), QTY, PRICE, TS)
    buy = broker._fill_fee(_order(OrderSide.BUY), QTY, PRICE, TS)
    assert sell - buy == pytest.approx(NOTIONAL * 33.0 / 10_000.0)


# ── 단가를 모르는 체결 ───────────────────────────────────────────────────
async def test_a_fill_with_no_price_is_not_booked_as_a_free_one():
    """단가 0 에 요율을 곱하면 0원입니다 — 그건 "공짜" 가 아니라 "모른다" 입니다.

    처음에는 이 경우를 통째로 건너뛰었습니다. "다음 폴링이 같은 수량을 다시
    본다" 는 전제였는데, **취소 경로에는 다음 폴링이 없습니다** — `cancel()` 은
    체결을 훑은 직후 주문을 목록에서 빼고, 세션을 닫을 때는 열린 주문을 전부
    취소합니다. 그래서 거래소에 체결로 남은 것이 여기서는 사라졌고, 손절도
    사이징도 하루 한도도 걸리지 않는 포지션이 생겼습니다.

    공식 execution 이 누적 금액과 평균가를 둘 다 잃었다면 지정가로 청구액을
    지어내지 않고 채널을 잠급니다. 취소 경로도 이를 미체결 확인으로 오인하면
    안 됩니다.
    """
    broker = _brokerage(_kr_fee_model())
    order = _order(OrderSide.BUY)          # 지정가 주문
    with pytest.raises(Exception, match="filledAmount"):
        await _poll_once(broker, order, {"filledQuantity": "10",
                                         "averagePrice": None})
    assert order.filled_qty == 0
    assert not broker.fill_channel_ok


async def test_a_fill_with_no_price_at_all_is_not_given_an_invented_fee():
    """지정가마저 없으면(시장가) 수수료를 계산할 근거가 없습니다.

    시장가도 누적 금액이 없으면 정확한 체결 delta를 만들 수 없습니다. 주문을
    공짜로 장부화하지 않고 다음 주문을 fail-closed 합니다.
    """
    broker = _brokerage(_kr_fee_model())
    order = Order(KRX, OrderSide.BUY, QTY, type=OrderType.MARKET)
    order.broker_id = "T-1"
    order.status = OrderStatus.SUBMITTED
    with pytest.raises(Exception, match="filledAmount"):
        await _poll_once(broker, order, {"filledQuantity": "10",
                                         "averagePrice": None})
    assert order.filled_qty == 0
    assert not broker.fill_channel_ok


async def test_an_unpriced_market_fill_is_not_booked_on_the_submit_path_either():
    """주문 응답 경로에도 같은 규칙이 걸리는가.

    지정가는 `limit_price` 로 물러설 수 있어 이 가드가 발동하지 않습니다.
    단가를 정말로 모르는 것은 **시장가** 주문이고, 그게 새 자리를 여는 주문
    이라 원가 0 으로 장부에 들어가면 손절도 사이징도 그 위에서 계산됩니다.
    """
    broker = _brokerage(_kr_fee_model(), {"orderId": "T-1", "filledQuantity": "10"})
    order = Order(KRX, OrderSide.BUY, QTY, type=OrderType.MARKET)
    assert await broker._venue_submit(order) == "T-1"
    assert broker._pending_fills == []


async def test_create_response_never_books_a_fill_but_detail_does():
    """OrderResponse has no execution; only the detail response may book it."""
    fee_model = _kr_fee_model(sell_tax_bps=20.0)
    submitted = _brokerage(fee_model, {"orderId": "T-1", "filledQuantity": "10",
                                       "averagePrice": "70000"})
    order = _order(OrderSide.SELL)
    # The adapter now rejects long-only oversells before HTTP. This test is
    # about create-vs-detail fill accounting, so give the sell a real holding.
    submitted.portfolio.position(order.symbol).quantity = order.quantity
    await submitted._venue_submit(order)
    from_submit = await submitted.poll_fills()
    assert from_submit == []

    polled = _brokerage(fee_model)
    from_poll = await _poll_once(polled, order,
                                 {"filledQuantity": "10", "averagePrice": "70000"})
    assert len(from_poll) == 1
    assert from_poll[0].fee > 0


async def test_a_fully_filled_order_still_reaches_filled():
    """비용을 붙이느라 주문 수명주기를 망가뜨리지 않았는가.

    체결 booking 을 `_venue_submit` 안에서 `order.apply_fill` 로 하면 곧이어
    `LiveBrokerage.submit()` 이 상태를 SUBMITTED 로 덮고, 그 뒤 폴링은
    `newly = 0` 이라 영영 FILLED 로 닫지 못합니다. 그렇게 남은 좀비 주문은
    실행모델의 미체결 목록에서 빠지지 않아 그 종목의 다음 주문을 시장가로
    승격시킵니다 — 되찾으려던 거래세와 같은 자릿수를 스프레드로 도로 냅니다.

    여기서 보는 것은 **수명주기뿐** 입니다. 이 입력(주문 응답이 체결을 실어
    오는 경우)에서는 같은 수량이 두 경로에 각각 잡히는 별개의 기존 결함이
    있는데, 그건 이 변경이 손대지 않은 자리라 여기서 주장하지 않습니다.
    참고로 토스 공식 스펙의 `POST /api/v1/orders` 응답(`OrderResponse`)에는
    `orderId` 밖에 없어, 실제 API 에서는 이 경로 자체가 돌지 않습니다.
    """
    broker = _brokerage(_kr_fee_model(), {"orderId": "T-1", "filledQuantity": "10",
                                          "averagePrice": "70000"})
    order = _order(OrderSide.BUY)
    order.status = OrderStatus.NEW
    await broker.submit(order)
    await _poll_once(broker, order, {"filledQuantity": "10",
                                     "averagePrice": "70000"})
    assert order.status is OrderStatus.FILLED
    assert await broker.open_orders() == []
