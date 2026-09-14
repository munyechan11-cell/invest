"""대기 중인 수동 주문은 **브로커가 받게 될 숫자** 로 보고합니다.

`_build_one` 은 보내기 직전에 수량을 lot 격자로, 지정가를 호가 격자로
스냅합니다. 접수한 원값을 그대로 돌려주면, 이 줄이 존재하는 이유 — "이걸
정말 낼 것인가" — 에 **실행되지 않을 숫자로** 답하게 됩니다.

실측(수정 전): 보유 1000주 000660 을 1000.7주·지정가 71,234 로 매도 접수하면
보고되는 줄은 `1000.7주 · 지정가 71234`, 실제 주문은 `1000주 @ 71,300`.
운영자는 71,234 에 걸린 줄 알고 기다리는데 호가가 71,300 을 찍고 돌아서면
본인은 청산됐다고 믿고, 주문은 그대로 남아 있습니다.

여기서 고정하는 것은 **특정 숫자가 아니라 등식** 입니다: 보고된 값 ==
`build_orders` 가 실제로 만든 주문의 값. 숫자를 박아 두면 반올림 규칙을 고칠
때 그 테스트가 괴리를 **지키는** 쪽으로 돌아섭니다.
"""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, Bar, Symbol
from quant.live.manual import ManualControl

T0 = datetime(2024, 6, 3, 4, 0, tzinfo=UTC)

#: 호가 사다리를 쓰는 국내 종목. 20만원을 넘으므로 실제 틱은 500 입니다.
HYNIX = Symbol("000660", venue="toss", quote_currency="KRW",
               tick_size=Decimal("100"), lot_size=Decimal("1"), tick_ladder="krx")
#: 고정 틱. 사다리를 켜지 않은 설정도 같은 계약을 지켜야 합니다.
FIXED = Symbol("AAA", venue="SIM", tick_size=Decimal("0.05"),
               lot_size=Decimal("0.1"))


def ctx_with(symbol: Symbol, held: float, price: float) -> Context:
    pf = Portfolio(100_000_000.0)
    ctx = Context(SimClock(T0), pf, EventBus(), timeframe="1d")
    ctx.universe = [symbol]
    for i in range(10):
        ctx.push_bar(Bar(symbol, T0 - timedelta(days=10 - i), price, price,
                         price, price, 1e6, "1d"))
    if held:
        pos = pf.position(symbol)
        pos.quantity = Decimal(str(held))
        pos.avg_price = price
        pos.mark(price)
    return ctx


def only_order(manual: ManualControl, ctx: Context):
    orders = manual.build_orders(ctx)
    assert len(orders) == 1, f"주문이 {len(orders)}건입니다"
    return orders[0]


# ── 등식: 보고된 줄 == 실제로 나가는 주문 ────────────────────────────────
@pytest.mark.parametrize("symbol,held,price,qty,limit", [
    (HYNIX, 1000, 213_400, "1000.7", 71_234),      # 사다리 + lot 둘 다 어긋남
    (HYNIX, 1000, 213_400, "500", 213_333),        # 20만원대 → 틱 500
    (HYNIX, 1000, 213_400, "500", 213_500),        # 이미 격자 위 — 안 움직임
    (FIXED, 100, 10.0, "3.37", 10.03),             # 고정 틱 + 소수 lot
])
def test_the_reported_row_is_the_order_that_will_be_sent(symbol, held, price,
                                                         qty, limit):
    ctx = ctx_with(symbol, held, price)
    manual = ManualControl()
    row = manual.sell(symbol, quantity=Decimal(qty), limit_price=limit).to_dict()
    sent = only_order(manual, ctx)

    assert row["quantity"] == pytest.approx(float(sent.quantity)), (
        f"줄은 {row['quantity']}주 라는데 실제 주문은 {sent.quantity}주 입니다")
    assert row["limit_price"] == pytest.approx(float(sent.limit_price)), (
        f"줄은 {row['limit_price']} 라는데 실제 주문은 {sent.limit_price} 입니다")


def test_the_same_holds_for_a_buy_which_rounds_the_other_way():
    """매수는 내림, 매도는 올림 — 방향을 잘못 잡으면 한쪽만 맞습니다."""
    ctx = ctx_with(HYNIX, 0, 213_400)
    manual = ManualControl()
    row = manual.buy(HYNIX, quantity=Decimal("10"), limit_price=213_400).to_dict()
    sent = only_order(manual, ctx)
    assert row["limit_price"] == pytest.approx(float(sent.limit_price)) == 213_000
    assert row["limit_price"] < 213_400, "매수는 내림이어야 합니다"


# ── 왜 달라졌는지 말합니다 ───────────────────────────────────────────────
def test_an_adjusted_row_says_where_the_number_moved():
    manual = ManualControl()
    row = manual.sell(HYNIX, quantity=Decimal("1000.7"), limit_price=71_234).to_dict()
    assert row["requested_quantity"] == 1000.7
    assert row["requested_limit_price"] == 71_234
    assert "수량" in row["adjusted"] and "호가단위" in row["adjusted"]


def test_a_row_that_did_not_move_says_nothing():
    """안 움직인 줄까지 설명을 달면, 진짜 움직인 줄이 묻힙니다."""
    manual = ManualControl()
    row = manual.sell(HYNIX, quantity=Decimal("10"), limit_price=213_500).to_dict()
    assert row["adjusted"] == ""
    assert row["quantity"] == 10 and row["limit_price"] == 213_500


# ── 모르는 것은 지어내지 않습니다 ────────────────────────────────────────
def test_a_notional_order_reports_no_quantity():
    """수량은 발주 시점 시세가 정합니다. 여기서 지어내면 그게 원래 문제입니다."""
    manual = ManualControl()
    row = manual.buy(HYNIX, notional=1_000_000).to_dict()
    assert row["quantity"] is None and row["notional"] == 1_000_000


def test_a_market_order_reports_no_limit_price():
    manual = ManualControl()
    row = manual.buy(HYNIX, quantity=Decimal("3")).to_dict()
    assert row["limit_price"] is None and row["adjusted"] == ""


def test_close_all_has_no_symbol_and_survives_serialisation():
    manual = ManualControl()
    row = manual.close_all(note="전량").to_dict()
    assert row["symbol"] is None and row["quantity"] is None
    assert row["limit_price"] is None and row["adjusted"] == ""


def test_a_symbol_without_a_grid_passes_values_through():
    synthetic = Symbol("SYN", venue="SIM", tick_size=Decimal("0"),
                       lot_size=Decimal("0"))
    manual = ManualControl()
    row = manual.buy(synthetic, quantity=Decimal("1.2345"),
                     limit_price=9.87654).to_dict()
    assert row["quantity"] == pytest.approx(1.2345)
    assert row["limit_price"] == pytest.approx(9.87654)
    assert row["adjusted"] == ""


# ── 여전히 알 수 없는 것은 detail 로 남습니다 ────────────────────────────
def test_selling_more_than_held_is_still_reported_by_detail_not_by_the_row():
    """보유까지만 줄이는 것은 발주 시점 장부가 정합니다 — 접수 시점에는
    모르고, 모르는 자리에 숫자를 지어내지 않는 것이 이 수정의 요점입니다."""
    ctx = ctx_with(HYNIX, held=10, price=213_400)
    manual = ManualControl()
    request = manual.sell(HYNIX, quantity=Decimal("40"), limit_price=213_500)
    assert request.to_dict()["quantity"] == 40
    sent = only_order(manual, ctx)
    assert sent.quantity == Decimal("10")
    assert "보유 수량까지만" in request.detail
