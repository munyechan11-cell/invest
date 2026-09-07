"""토스가 `execution.commission` / `execution.tax` 를 `null` 로 보내면 체결이 공짜였다.

주문 상세의 비용 필드는 nullable 입니다. 어댑터는 `null` 을 0 으로 읽어
`fee = commission_delta + tax_delta = 0` 으로 장부화했습니다. 회계층은 0 을
"모른다" 가 아니라 **"공짜"** 로 믿습니다 — 그래서 토스가 비용을 비워 보내는
동안은 일일 손실 한도가 수수료만큼 늦게 걸리고, 실현손익과 현금 장부가 매
체결마다 실제보다 좋아 보였습니다. `_fill_fee` (설정의 비용 모델 추정)는
있었지만 토스 경로에서는 아무도 부르지 않았습니다.

여기서 검사하는 성질:

* `null` 비용 체결이 공짜로 장부화되지 않고, **설정의 비용 모델** 이 말하는
  금액이 붙는가 (매도엔 설정에 적힌 거래세율만큼 더)
* 증권사가 명시적으로 준 `"0"` 은 그대로 0 인가 (매수엔 거래세가 없고, 무료
  수수료 이벤트도 있습니다)
* 한 번 추정으로 간 주문은 뒤 체결분도 추정으로 가는가 — 뒤늦게 들어온 누적
  비용을 델타로 더 얹으면 같은 돈이 두 번 잡힙니다
* 비용이 매번 `null` 이어도 수량·금액이 줄어드는 응답은 여전히 거부하는가
* 실계좌 현금 증명이 추정 오차 몇 원을 "장부에 없는 체결" 로 오인해 다음
  매수를 영영 막지 않는가 — 그러면서 한 주가 통째로 빠진 현금은 여전히
  잡아내는가

네트워크는 쓰지 않습니다 — `client` 를 가짜로 갈아 끼웁니다.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from quant.brokerage.base import BrokerageError
from quant.brokerage.live_base import FEE_ESTIMATED_META, FEE_ESTIMATED_TOTAL_META
from quant.brokerage.toss_broker import TossBrokerage
from quant.config.schema import CostConfig, ModelSpec, StrategyConfig
from quant.core.account import Portfolio
from quant.core.types import UTC, Order, OrderSide, OrderStatus, OrderType, Symbol
from quant.strategy.builder import build_costs

KRX = Symbol("005930", venue="toss", quote_currency="KRW", tick_size=Decimal("100"))
PRICE = 70_000.0
QTY = Decimal("10")
FILLED_AT_UTC = datetime(2026, 7, 1, 4, 0, tzinfo=UTC)   # == 13:00 KST below
CREDS = {"client_id": "test-id", "client_secret": "test-secret", "account_no": "1234"}
SELL_TAX_BPS = 20.0


class FakeToss:
    """`_TossClient` 자리에 들어가는 가짜. 상태를 바꿔 가며 같은 인스턴스를 씁니다."""

    def __init__(self, *, cash: str = "10000000", items: list[dict] | None = None):
        self.cash = Decimal(cash)
        self.items = list(items or [])
        self.detail: dict | None = None
        self.calls: list[tuple[str, str]] = []

    async def request(self, method, path, *, params=None, json=None, account=False):
        self.calls.append((method, path))
        if path == "/api/v1/holdings":
            total = sum(
                Decimal(str(item["marketValue"]["amount"]))
                for item in self.items if item["currency"] == "KRW"
            )
            return {
                "marketValue": {"amount": {"krw": str(total), "usd": None}},
                "items": list(self.items),
            }
        if path == "/api/v1/buying-power":
            currency = str((params or {}).get("currency") or "KRW")
            return {"currency": currency, "cashBuyingPower": str(self.cash)}
        if method == "GET" and path == "/api/v1/orders":
            return {"orders": [], "nextCursor": None, "hasNext": False}
        if method == "POST" and path == "/api/v1/orders":
            return {"orderId": "T-1"}
        if method == "GET" and path == "/api/v1/orders/T-1":
            assert self.detail is not None, "테스트가 주문 상세를 준비하지 않았습니다"
            return self.detail
        raise AssertionError(f"unexpected fake Toss call: {method} {path}")

    async def close(self):
        pass


def _fee_model(sell_tax_bps: float = SELL_TAX_BPS):
    """설정 파일이 만드는 것과 **같은 경로** 로 비용 모델을 만듭니다."""
    cfg = StrategyConfig(name="t", alpha=[ModelSpec(type="ema_cross")],
                         costs=CostConfig(preset="kr_equity",
                                          sell_tax_bps=sell_tax_bps))
    return build_costs(cfg)[0]


def _expected_fee(model, order: Order, qty: Decimal, price: float) -> float:
    """설정 모델이 이 체결분에 매길 금액 — 어댑터의 `_fill_fee` 를 거치지 않습니다."""
    side_model = model.for_side(order.side)
    return side_model.fee(order.symbol, qty, price,
                          order.type is OrderType.LIMIT, FILLED_AT_UTC)


def _brokerage(fee_model=None, *, portfolio: Portfolio | None = None) -> TossBrokerage:
    broker = TossBrokerage(portfolio or Portfolio(10_000_000, "KRW"), live=True,
                           fee_model=fee_model or _fee_model(),
                           reconcile_on_start=False, max_order_notional=1e12,
                           **CREDS)
    broker.client = FakeToss()
    return broker


def _order(side: OrderSide = OrderSide.BUY, qty: Decimal = QTY) -> Order:
    order = Order(KRX, side, qty, type=OrderType.LIMIT, limit_price=PRICE)
    order.broker_id = "T-1"
    order.status = OrderStatus.SUBMITTED
    return order


def _detail(order: Order, *, filled: str, price: float = PRICE,
            commission: str | None, tax: str | None,
            amount: str | None = None, status: str | None = None) -> dict:
    """공식 v1.2.14 Order 상세 응답 하나. 비용 둘은 그대로 실어 보냅니다."""
    filled_qty = Decimal(filled)
    if status is None:
        status = ("FILLED" if filled_qty == order.quantity
                  else "PARTIAL_FILLED" if filled_qty else "PENDING")
    return {
        "orderId": order.broker_id,
        "symbol": order.symbol.ticker,
        "side": "BUY" if order.side is OrderSide.BUY else "SELL",
        "orderType": "LIMIT" if order.type is OrderType.LIMIT else "MARKET",
        "timeInForce": "DAY",
        "status": status,
        "price": str(order.limit_price) if order.limit_price is not None else None,
        "quantity": str(order.quantity),
        "currency": order.symbol.quote_currency,
        "orderedAt": "2026-07-01T12:59:00+09:00",
        "execution": {
            "filledQuantity": str(filled_qty),
            "averageFilledPrice": str(price) if filled_qty else None,
            "filledAmount": (amount if amount is not None
                             else str(filled_qty * Decimal(str(price))) if filled_qty
                             else None),
            "commission": commission,
            "tax": tax,
            "filledAt": "2026-07-01T13:00:00+09:00" if filled_qty else None,
            "settlementDate": None,
        },
    }


async def _poll(broker: TossBrokerage, order: Order, detail: dict):
    broker._orders[order.id] = order
    broker.client.detail = detail
    return await broker.poll_fills()


# ── null 은 공짜가 아니다 ───────────────────────────────────────────────
async def test_a_fill_with_null_commission_and_tax_is_not_booked_as_free():
    model = _fee_model()
    broker = _brokerage(model)
    order = _order(OrderSide.BUY)

    fills = await _poll(broker, order, _detail(order, filled="10",
                                               commission=None, tax=None))

    assert len(fills) == 1
    assert fills[0].fee > 0, "비용을 모르는 체결이 공짜로 기록됐습니다"
    assert fills[0].fee == pytest.approx(_expected_fee(model, order, QTY, PRICE))
    assert order.meta.get(FEE_ESTIMATED_META) is True
    assert float(Decimal(order.meta[FEE_ESTIMATED_TOTAL_META])) == pytest.approx(fills[0].fee)


async def test_a_null_cost_sell_carries_the_configured_transaction_tax():
    """추정도 매도엔 거래세를 얹어야 합니다 — 설정에 적힌 세율로 검산합니다."""
    broker = _brokerage(_fee_model(sell_tax_bps=SELL_TAX_BPS))
    buy = await _poll(broker, _order(OrderSide.BUY),
                      _detail(_order(OrderSide.BUY), filled="10",
                              commission=None, tax=None))
    sell_order = _order(OrderSide.SELL)
    sell = await _poll(broker, sell_order,
                       _detail(sell_order, filled="10", commission=None, tax=None))

    notional = float(QTY) * PRICE
    assert sell[0].fee > buy[0].fee
    assert sell[0].fee - buy[0].fee == pytest.approx(notional * SELL_TAX_BPS / 10_000.0)


async def test_one_null_cost_field_is_enough_to_fall_back_to_the_estimate():
    """수수료는 왔는데 세금이 `null` 이어도 그 체결분은 통째로 추정입니다.

    반만 믿으면 반만 틀립니다 — 어느 쪽이 빠졌는지 화면에는 안 보입니다.
    """
    model = _fee_model()
    broker = _brokerage(model)
    order = _order(OrderSide.SELL)

    fills = await _poll(broker, order, _detail(order, filled="10",
                                               commission="105", tax=None))

    assert fills[0].fee == pytest.approx(_expected_fee(model, order, QTY, PRICE))
    assert order.meta.get(FEE_ESTIMATED_META) is True


async def test_an_explicit_zero_from_the_venue_stays_zero():
    """`"0"` 은 모름이 아니라 실제 0원입니다 — 추정으로 덮어쓰면 안 됩니다."""
    broker = _brokerage()
    order = _order(OrderSide.BUY)

    fills = await _poll(broker, order, _detail(order, filled="10",
                                               commission="0", tax="0"))

    assert fills[0].fee == 0.0
    assert FEE_ESTIMATED_META not in order.meta


async def test_the_venue_number_wins_whenever_the_venue_gives_one():
    """추정은 대체 경로입니다. 실제 청구액이 오면 그 값이어야 합니다."""
    broker = _brokerage()
    order = _order(OrderSide.SELL)

    fills = await _poll(broker, order, _detail(order, filled="10",
                                               commission="777", tax="1400"))

    assert fills[0].fee == pytest.approx(777 + 1400)
    assert FEE_ESTIMATED_META not in order.meta


# ── 한 주문 안에서 추정과 실제를 섞지 않는다 ─────────────────────────────
async def test_once_estimated_an_order_keeps_estimating_its_later_slices():
    """앞 4주는 추정, 뒤 6주는 뒤늦게 온 누적 비용의 델타 — 이렇게 섞으면
    그 델타에 앞 4주의 실제 비용이 들어 있어 같은 돈이 두 번 잡힙니다."""
    model = _fee_model()
    broker = _brokerage(model)
    order = _order(OrderSide.BUY)

    first = await _poll(broker, order, _detail(order, filled="4",
                                               commission=None, tax=None))
    second = await _poll(broker, order, _detail(order, filled="10",
                                                commission="999999", tax="0"))

    assert first[0].quantity == Decimal("4")
    assert second[0].quantity == Decimal("6")
    assert first[0].fee == pytest.approx(_expected_fee(model, order, Decimal("4"), PRICE))
    assert second[0].fee == pytest.approx(_expected_fee(model, order, Decimal("6"), PRICE))
    assert order.fees == pytest.approx(first[0].fee + second[0].fee)
    assert order.status is OrderStatus.FILLED
    assert float(Decimal(order.meta[FEE_ESTIMATED_TOTAL_META])) == pytest.approx(order.fees)


async def test_a_venue_cost_that_goes_missing_mid_order_does_not_double_book():
    """앞 슬라이스는 실제 비용, 뒤 슬라이스에서 `null` — 뒤 슬라이스만 추정하고
    그 뒤로는 실제 값이 다시 와도 델타로 더 얹지 않습니다."""
    model = _fee_model()
    broker = _brokerage(model)
    order = _order(OrderSide.BUY)

    first = await _poll(broker, order, _detail(order, filled="4",
                                               commission="40", tax="0"))
    second = await _poll(broker, order, _detail(order, filled="10",
                                                commission=None, tax=None))
    # 체결은 이미 끝났는데 상태 전이가 늦어 한 번 더 조회됐고, 이번엔 누적
    # 비용이 실려 왔습니다. 새 체결이 없으니 아무것도 더 잡히면 안 됩니다.
    third = await _poll(broker, order, _detail(order, filled="10",
                                               commission="100", tax="0"))

    assert first[0].fee == pytest.approx(40.0)
    assert second[0].fee == pytest.approx(_expected_fee(model, order, Decimal("6"), PRICE))
    assert third == []
    assert order.fees == pytest.approx(40.0 + second[0].fee)
    assert float(Decimal(order.meta[FEE_ESTIMATED_TOTAL_META])) == pytest.approx(second[0].fee)


# ── 비용이 없어도 나머지 검사는 산다 ─────────────────────────────────────
async def test_a_shrinking_cumulative_amount_is_still_refused_when_costs_are_null():
    broker = _brokerage()
    order = _order(OrderSide.BUY)
    await _poll(broker, order, _detail(order, filled="4", commission=None, tax=None))

    with pytest.raises(BrokerageError, match="줄었습니다"):
        await _poll(broker, order, _detail(order, filled="10", commission=None,
                                           tax=None, amount="200000"))
    assert order.filled_qty == Decimal("4")
    assert not broker.fill_channel_ok


async def test_a_shrinking_cumulative_quantity_is_still_refused_when_costs_are_null():
    broker = _brokerage()
    order = _order(OrderSide.BUY)
    await _poll(broker, order, _detail(order, filled="6", commission=None, tax=None))

    with pytest.raises(BrokerageError, match="줄었습니다"):
        await _poll(broker, order, _detail(order, filled="4", commission=None,
                                           tax=None, status="PARTIAL_FILLED"))


async def test_a_shrinking_explicit_cost_is_still_refused():
    """값이 있을 때 줄어드는 것은 여전히 결함 신호입니다."""
    broker = _brokerage()
    order = _order(OrderSide.BUY)
    await _poll(broker, order, _detail(order, filled="4", commission="40", tax="0"))

    with pytest.raises(BrokerageError, match="수수료/세금"):
        await _poll(broker, order, _detail(order, filled="10", commission="30", tax="0"))


# ── 실계좌 현금 증명 ─────────────────────────────────────────────────────
def _holding(qty: str, avg: str = "70000", market: str | None = None) -> dict:
    return {
        "symbol": KRX.ticker, "name": "삼성전자", "marketCountry": "KR",
        "currency": "KRW", "quantity": qty, "lastPrice": "70000",
        "averagePurchasePrice": avg,
        "marketValue": {"amount": market or str(Decimal(qty) * Decimal(avg))},
        "profitLoss": {"amount": "0", "rate": "0"},
        "dailyProfitLoss": {"amount": "0", "rate": "0"},
        "cost": {"commission": "0", "tax": "0"},
    }


async def _buy_with_estimated_fee() -> tuple[TossBrokerage, Order, float]:
    """실계좌 진실 모드로 매수 하나를 보내고 `null` 비용 체결을 장부화합니다."""
    pf = Portfolio(10_000_000, "KRW")
    pf.mark(KRX, PRICE)
    broker = _brokerage(portfolio=pf)
    venue: FakeToss = broker.client
    assert (await broker.sync())["ok"]

    order = Order(KRX, OrderSide.BUY, QTY, type=OrderType.LIMIT, limit_price=PRICE)
    await broker.submit(order)
    assert order.status is OrderStatus.SUBMITTED, order.reject_reason

    venue.detail = _detail(order, filled="10", commission=None, tax=None)
    fills = await broker.poll_fills()
    assert len(fills) == 1 and fills[0].fee > 0
    pf.apply_fill(fills[0])
    venue.items = [_holding("10")]
    return broker, order, fills[0].fee


async def test_the_cash_proof_forgives_a_one_won_estimate_error():
    """추정이 실제 청구액과 1원 어긋났습니다. 이걸 "장부에 없는 체결" 로 보면
    증명이 재시작 전까지 영영 실패하고, 그동안 모든 새 매수가 막힙니다."""
    broker, _order_, estimate = await _buy_with_estimated_fee()
    venue: FakeToss = broker.client
    actual = estimate + 1.0
    venue.cash = Decimal(10_000_000) - QTY * Decimal(str(PRICE)) - Decimal(str(actual))

    report = await broker.sync()

    assert report["ok"], report
    assert broker.account_ready
    # 증명이 통과하면 장부는 실제 현금으로 돌아갑니다 — 오차는 쌓이지 않습니다.
    assert broker.portfolio.cash == pytest.approx(float(venue.cash))
    assert not broker._capital_order_checkpoints


async def test_the_cash_proof_still_catches_a_whole_unbooked_share():
    """허용폭을 넓힌 것이지 증명을 없앤 것이 아닙니다 — 한 주 값이 통째로
    빠진 현금은 여전히 미정산으로 잡혀야 합니다."""
    broker, order, estimate = await _buy_with_estimated_fee()
    venue: FakeToss = broker.client
    unbooked_share = Decimal(str(PRICE))
    venue.cash = (Decimal(10_000_000) - QTY * Decimal(str(PRICE))
                  - Decimal(str(estimate)) - unbooked_share)

    report = await broker.sync()

    assert not report["ok"]
    assert report["transient"] == "terminal_order_settlement"
    assert "exact-once local fill ledger" in report["unsettled_orders"][order.id]["reason"]
    assert not broker.account_ready
