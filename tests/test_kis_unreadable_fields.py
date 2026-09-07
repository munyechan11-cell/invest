"""KIS 체결 row 의 못 읽는 숫자가 0 이 되던 것.

`_number()` 는 비어 있지 않은데 숫자로 안 읽히는 값을 `Decimal("0")` 로
돌려줬습니다. 0 은 "없음" 이지 "모름" 이 아닌데, 두 자리에서 그 차이가 돈이
됐습니다.

* `poll_fills` 에서 `tot_ccld_qty` 가 못 읽히면 `newly <= 0` 이 되어 체결이
  **조용히** 건너뛰어졌습니다. 채널은 살아 있다고 보고했으니(`fill_channel_up`)
  다음 주문은 계속 나갔고, 계좌에는 있는 포지션이 장부에는 없었습니다.
* `_remaining` 에서 `rmn_qty` / `ord_qty` 가 못 읽히면 `_venue_open_orders`
  가 그 row 를 떨어뜨려, 종료 직전 "남은 미결 주문" 수가 실제보다 적었습니다.
  안전 종료 판정이 그 수를 믿습니다.

검사하는 성질: 못 읽는 row 는 사라지지 않고 **왜** 못 읽었는지 이름을 달고
채널을 내리거나(체결) 오류로 올라온다(미결 수). 다른 정상 row 의 체결은
그대로 장부화된다.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.brokerage.base import BrokerageError
from quant.brokerage.kis_broker import KisBrokerage, _number
from quant.core.account import Portfolio
from quant.core.types import Order, OrderSide, OrderStatus, OrderType, Symbol

SYM = Symbol("005930", venue="kis", quote_currency="KRW", tick_size=Decimal("100"))
CREDS = {"app_key": "dummy", "app_secret": "dummy", "account_no": "1234567801"}


def _portfolio() -> Portfolio:
    pf = Portfolio(10_000_000.0, "KRW")
    pf.mark(SYM, 70_000.0)
    return pf


def _order(qty: str = "10") -> Order:
    return Order(symbol=SYM, side=OrderSide.BUY, quantity=Decimal(qty),
                 type=OrderType.LIMIT, limit_price=70_000.0, tag="entry")


def _row(order_id: str, filled: str, price: str = "70000", ordered: str = "10",
         remaining: str | None = None) -> dict:
    row = {
        "odno": order_id, "pdno": SYM.ticker, "ord_qty": ordered,
        "tot_ccld_qty": filled, "avg_prvs": price,
        "ord_dt": "20260823", "ord_tmd": "101530",
    }
    if remaining is not None:
        row["rmn_qty"] = remaining
    return row


class FakeKis(KisBrokerage):
    """네트워크 훅만 대신합니다 — 가드레일과 체결 처리는 진짜 코드입니다."""

    def __init__(self, portfolio, **kwargs):
        self.rows: list[dict] = []
        self.sent: list[Order] = []
        super().__init__(portfolio, **CREDS, paper_trading=True,
                         max_order_notional=100_000_000.0, **kwargs)

    async def _venue_submit(self, order: Order) -> str:
        self.sent.append(order)
        return f"00001234{len(self.sent):02d}"

    async def _venue_executions(self) -> list[dict]:
        return list(self.rows)


# ── _number ──────────────────────────────────────────────────────────────
def test_number_refuses_garbage_and_names_the_field():
    with pytest.raises(BrokerageError, match="filled_qty"):
        _number({"tot_ccld_qty": "N/A"}, "filled_qty")
    with pytest.raises(BrokerageError, match="remaining"):
        _number({"rmn_qty": "NaN"}, "remaining")
    # 비어 있는 값은 필수일 때만 오류 — 미체결 row 의 평균가는 정당하게 비어 있습니다.
    assert _number({}, "avg_price") == Decimal("0")
    with pytest.raises(BrokerageError, match="filled_qty"):
        _number({}, "filled_qty", required=True)
    assert _number({"tot_ccld_qty": "1,234"}, "filled_qty") == Decimal("1234")


# ── poll_fills ───────────────────────────────────────────────────────────
async def test_an_unreadable_filled_quantity_downs_the_channel_instead_of_skipping():
    broker = FakeKis(_portfolio())
    order = _order()
    await broker.submit(order)
    broker.rows = [_row(order.broker_id, "N/A")]

    assert await broker.poll_fills() == []

    assert not broker.fill_channel_ok, "못 읽는 체결이 조용히 건너뛰어졌습니다"
    assert order.broker_id in broker.fill_channel_error
    assert "filled_qty" in broker.fill_channel_error
    assert order.filled_qty == Decimal("0")

    blocked = _order(qty="11")
    await broker.submit(blocked)
    assert blocked.status is OrderStatus.REJECTED
    assert broker.sent == [order]


async def test_a_missing_filled_quantity_is_unreadable_too():
    """체결 수량 칸이 아예 없는 row 는 "0주 체결" 이 아닙니다."""
    broker = FakeKis(_portfolio())
    order = _order()
    await broker.submit(order)
    row = _row(order.broker_id, "10")
    del row["tot_ccld_qty"]
    broker.rows = [row]

    assert await broker.poll_fills() == []
    assert not broker.fill_channel_ok
    assert order.broker_id in broker.fill_channel_error


async def test_a_readable_fill_next_to_an_unreadable_row_is_still_booked():
    """한 row 가 깨졌다고 다른 주문의 체결까지 버리면 그쪽 포지션이 장부에서 빠집니다."""
    broker = FakeKis(_portfolio())
    good, bad = _order(), _order(qty="7")
    await broker.submit(good)
    await broker.submit(bad)
    broker.rows = [_row(good.broker_id, "10"), _row(bad.broker_id, "garbage", ordered="7")]

    fills = await broker.poll_fills()

    assert [f.order_id for f in fills] == [good.id]
    assert good.status is OrderStatus.FILLED
    assert bad.filled_qty == Decimal("0")
    assert not broker.fill_channel_ok
    assert bad.broker_id in broker.fill_channel_error


async def test_the_channel_recovers_once_the_row_is_readable_again():
    broker = FakeKis(_portfolio())
    order = _order()
    await broker.submit(order)
    broker.rows = [_row(order.broker_id, "??")]
    await broker.poll_fills()
    assert not broker.fill_channel_ok

    broker.rows = [_row(order.broker_id, "10")]
    fills = await broker.poll_fills()

    assert broker.fill_channel_ok
    assert len(fills) == 1 and fills[0].quantity == Decimal("10")


# ── _venue_open_orders / 종료 직전 미결 수 ───────────────────────────────
async def test_an_unreadable_remaining_quantity_fails_the_shutdown_count_closed():
    """못 읽는 row 를 빼고 세면 계좌에 걸린 주문이 "없음" 이 됩니다."""
    broker = FakeKis(_portfolio())
    broker.rows = [_row("resting", "4", remaining="?"),
                   _row("done", "10", remaining="0")]

    with pytest.raises(BrokerageError, match="remaining"):
        await broker._venue_open_orders()
    with pytest.raises(BrokerageError):
        await broker.shutdown_remote_open_order_count()


async def test_a_row_without_any_quantity_cannot_be_counted_as_closed():
    """잔량도 주문수량도 없는 row 는 0 - 0 = 0 으로 "다 체결됨" 이 되면 안 됩니다."""
    broker = FakeKis(_portfolio())
    broker.rows = [{"odno": "resting", "pdno": SYM.ticker,
                    "ord_dt": "20260823", "ord_tmd": "101530"}]

    with pytest.raises(BrokerageError, match="order_qty"):
        await broker.shutdown_remote_open_order_count()


async def test_readable_rows_still_count_only_the_resting_ones():
    broker = FakeKis(_portfolio())
    broker.rows = [_row("resting", "4", remaining="6"),
                   _row("done", "10", remaining="0"),
                   _row("no-remaining-field", "3", ordered="10")]

    assert [r["odno"] for r in await broker._venue_open_orders()] == [
        "resting", "no-remaining-field",
    ]
    assert await broker.shutdown_remote_open_order_count() == 2
