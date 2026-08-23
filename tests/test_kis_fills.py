"""KIS가 체결을 보고하는가, 그리고 모의투자로 주문이 나가는가.

세 가지가 겹쳐서 KIS는 실계좌에 눈을 감고 들어가는 어댑터였습니다.

1. 체결 채널이 없었습니다. 주문은 나가고 주식은 실제로 사지는데 엔진 장부는
   0주여서, 손절도 사이징도 존재를 모르는 포지션에는 걸리지 않았습니다.
2. `live` 하나가 '어느 서버'와 '주문을 내도 되는가'를 동시에 뜻해서, 실제로
   주문이 나가는 유일한 모드가 실계좌였습니다. 모의투자 TR id는 죽은 코드였습니다.
3. 체결을 못 보는 상태여도 어댑터는 계속 주문했습니다.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.brokerage import kis_broker
from quant.brokerage.base import BrokerageError
from quant.brokerage.kis_broker import KisBrokerage
from quant.brokerage.live_base import LiveBrokerage
from quant.core.account import Portfolio
from quant.core.types import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    RunMode,
    Symbol,
)
from quant.data.providers.kis import MOCK_HOST
from quant.live.limits import TradingBudget

SYM = Symbol("005930", venue="kis", quote_currency="KRW", tick_size=Decimal("100"))
CREDS = {"app_key": "dummy", "app_secret": "dummy", "account_no": "1234567801"}


def _portfolio(cash: float = 10_000_000.0) -> Portfolio:
    pf = Portfolio(cash, "KRW")
    pf.mark(SYM, 70_000.0)
    return pf


def _order(side: OrderSide = OrderSide.BUY, qty: str = "10",
           price: float | None = 70_000.0) -> Order:
    return Order(symbol=SYM, side=side, quantity=Decimal(qty),
                 type=OrderType.LIMIT if price else OrderType.MARKET,
                 limit_price=price, tag="entry")


def _row(order_id: str, filled: float, price: float, ordered: float | None = None,
         day: str = "20260823", clock: str = "101530") -> dict:
    """One 주식일별주문체결조회 output1 row."""
    ordered = filled if ordered is None else ordered
    return {
        "odno": order_id, "pdno": SYM.ticker,
        "ord_qty": str(int(ordered)), "tot_ccld_qty": str(int(filled)),
        "avg_prvs": str(price), "tot_ccld_amt": str(int(filled * price)),
        "rmn_qty": str(int(ordered - filled)),
        "ord_dt": day, "ord_tmd": clock,
    }


class FakeKis(KisBrokerage):
    """네트워크 훅만 대신합니다 — 가드레일과 체결 처리는 진짜 코드입니다."""

    def __init__(self, portfolio, **kwargs):
        self.rows: list[dict] = []
        self.sent: list[Order] = []
        self.executions_fail = ""
        super().__init__(portfolio, **CREDS, **kwargs)

    async def _venue_submit(self, order: Order) -> str:
        self.sent.append(order)
        return f"00001234{len(self.sent):02d}"

    async def _venue_executions(self) -> list[dict]:
        if self.executions_fail:
            raise RuntimeError(self.executions_fail)
        return list(self.rows)


# ── 체결 채널 ────────────────────────────────────────────────────────────
async def test_a_kis_fill_reaches_the_engine_with_price_quantity_fee_and_time():
    """예전에는 poll_fills() 가 영원히 빈 리스트였습니다."""
    pf = _portfolio()
    broker = FakeKis(pf, paper_trading=True, max_order_notional=100_000_000.0)
    order = _order()
    await broker.submit(order)
    assert broker.sent == [order]

    broker.rows = [_row(order.broker_id, 10, 70_000.0)]
    fills = await broker.poll_fills()

    assert len(fills) == 1
    fill = fills[0]
    assert fill.quantity == Decimal("10")
    assert fill.price == 70_000.0
    assert fill.fee > 0                     # 수수료 0원은 회계층이 그대로 믿습니다
    assert fill.ts.tzinfo is not None
    assert fill.tag == "entry"
    assert order.status is OrderStatus.FILLED

    pf.apply_fill(fill)
    assert pf.quantity(SYM) == Decimal("10")
    assert pf.position(SYM).avg_price == 70_000.0
    assert pf.cash == pytest.approx(10_000_000.0 - 700_000.0 - fill.fee)


async def test_the_same_fill_is_not_reported_twice():
    """일별 체결 조회는 누적 수량을 줍니다 — 델타만 보고해야 합니다."""
    broker = FakeKis(_portfolio(), paper_trading=True,
                     max_order_notional=100_000_000.0)
    order = _order()
    await broker.submit(order)
    broker.rows = [_row(order.broker_id, 10, 70_000.0)]

    assert len(await broker.poll_fills()) == 1
    assert await broker.poll_fills() == []


async def test_a_partial_fill_books_each_slice_at_its_own_price():
    """평균가를 그대로 쓰면 나중 체결가가 앞 체결로 번집니다."""
    broker = FakeKis(_portfolio(), paper_trading=True,
                     max_order_notional=100_000_000.0)
    order = _order(qty="10")
    await broker.submit(order)

    broker.rows = [_row(order.broker_id, 4, 70_000.0, ordered=10)]
    first = await broker.poll_fills()
    assert first[0].quantity == Decimal("4")
    assert order.status is OrderStatus.PARTIAL

    broker.rows = [_row(order.broker_id, 10, 70_500.0, ordered=10)]
    second = await broker.poll_fills()
    assert second[0].quantity == Decimal("6")
    assert second[0].price == pytest.approx((70_500.0 * 10 - 70_000.0 * 4) / 6)
    assert order.status is OrderStatus.FILLED
    assert order.avg_fill_price == pytest.approx(70_500.0)


async def test_a_sell_fill_carries_the_transaction_tax_as_well_as_commission():
    """국내 매도는 증권거래세가 붙습니다 — 매수보다 확실히 비쌉니다."""
    broker = FakeKis(_portfolio(), paper_trading=True,
                     max_order_notional=100_000_000.0)
    buy, sell = _order(), _order(side=OrderSide.SELL)
    await broker.submit(buy)
    await broker.submit(sell)

    broker.rows = [_row(buy.broker_id, 10, 70_000.0),
                   _row(sell.broker_id, 10, 70_000.0)]
    fees = {f.side: f.fee for f in await broker.poll_fills()}

    notional = 700_000.0
    assert fees[OrderSide.BUY] == pytest.approx(notional * 1.5 / 10_000)
    assert fees[OrderSide.SELL] == pytest.approx(notional * 19.5 / 10_000)


async def test_open_orders_come_from_the_execution_rows():
    """_venue_open_orders() 는 빈 리스트를 돌려주는 자리가 아닙니다."""
    broker = FakeKis(_portfolio(), paper_trading=True)
    broker.rows = [_row("resting", 4, 70_000.0, ordered=10),
                   _row("done", 10, 70_000.0, ordered=10)]
    assert [r["odno"] for r in await broker._venue_open_orders()] == ["resting"]


# ── 체결을 못 보면 주문하지 않는다 ───────────────────────────────────────
async def test_connect_refuses_a_session_that_cannot_read_its_own_fills():
    broker = FakeKis(_portfolio(), paper_trading=True)
    broker.executions_fail = "500 Internal Server Error"
    with pytest.raises(BrokerageError) as exc:
        await broker.connect()
    assert "체결" in str(exc.value)


async def test_a_broken_fill_channel_blocks_the_next_order_and_then_recovers():
    broker = FakeKis(_portfolio(), paper_trading=True,
                     max_order_notional=100_000_000.0)
    first = _order()
    await broker.submit(first)

    broker.executions_fail = "read timeout"
    await broker.poll_fills()
    assert not broker.fill_channel_ok

    blocked = _order()
    await broker.submit(blocked)
    assert blocked.status is OrderStatus.REJECTED
    assert "체결" in blocked.reject_reason
    assert broker.sent == [first]           # 아무것도 새로 나가지 않았습니다

    broker.executions_fail = ""
    await broker.poll_fills()
    assert broker.fill_channel_ok
    again = _order(qty="11")
    await broker.submit(again)
    assert again.status is OrderStatus.SUBMITTED


async def test_a_fill_with_no_readable_price_is_not_booked_at_zero():
    """가격 0원으로 장부에 넣느니 채널을 내리는 편이 낫습니다."""
    broker = FakeKis(_portfolio(), paper_trading=True,
                     max_order_notional=100_000_000.0)
    order = _order()
    await broker.submit(order)
    broker.rows = [{"odno": order.broker_id, "pdno": SYM.ticker,
                    "ord_qty": "10", "tot_ccld_qty": "10"}]

    assert await broker.poll_fills() == []
    assert not broker.fill_channel_ok
    assert order.filled_qty == Decimal("0")


# ── 모의투자와 실계좌의 분리 ─────────────────────────────────────────────
async def test_the_paper_venue_really_submits_and_uses_the_mock_transaction_ids(
    monkeypatch,
):
    """VTTC0802U 는 죽은 코드였습니다 — 모의투자로는 주문이 나가지 않았습니다."""
    posted: dict = {}

    async def fake_token(app_key, app_secret, paper):
        assert paper is True
        return "token"

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"rt_cd": "0", "output": {"ODNO": "0000123456"}}

    class _Client:
        async def post(self, url, headers=None, json=None):
            posted.update(url=url, headers=headers, body=json)
            return _Response()

    monkeypatch.setattr(kis_broker, "kis_token", fake_token)
    broker = KisBrokerage(_portfolio(), **CREDS, paper_trading=True,
                          max_order_notional=100_000_000.0)
    broker._client = _Client()

    order = _order()
    await broker.submit(order)

    assert order.status is OrderStatus.SUBMITTED
    assert order.broker_id == "0000123456"
    assert order.meta.get("dry_run") is None
    assert posted["url"].startswith(MOCK_HOST)
    assert posted["headers"]["tr_id"] == "VTTC0802U"


async def test_a_dry_run_without_the_paper_flag_still_sends_nothing():
    broker = FakeKis(_portfolio(), max_order_notional=100_000_000.0)
    order = _order()
    await broker.submit(order)
    assert broker.sent == []
    assert order.meta["dry_run"] is True


def test_run_mode_reports_live_only_for_real_money():
    assert KisBrokerage(_portfolio(), **CREDS).run_mode is RunMode.DRY_RUN
    paper = KisBrokerage(_portfolio(), **CREDS, paper_trading=True)
    assert paper.run_mode is RunMode.DRY_RUN
    assert paper.sends_orders is True
    real = KisBrokerage(_portfolio(), **CREDS, live=True)
    assert real.run_mode is RunMode.LIVE
    assert real.paper_venue is False


def test_asking_for_the_paper_venue_and_real_money_at_once_is_refused():
    with pytest.raises(BrokerageError) as exc:
        KisBrokerage(_portfolio(), **CREDS, live=True, paper_trading=True)
    assert "모의투자" in str(exc.value)


# ── LiveBrokerage 공통 가드레일 ──────────────────────────────────────────
class FakeVenue(LiveBrokerage):
    name = "fake"

    def __init__(self, portfolio, **kwargs):
        super().__init__(portfolio, **kwargs)
        self.sent: list[Order] = []
        self.holdings: dict[str, Decimal] = {}
        self.costs: dict[str, float] = {}
        self.cash: float | None = None

    async def _venue_submit(self, order: Order) -> str:
        self.sent.append(order)
        return "venue-id"

    async def _venue_positions(self) -> dict[str, Decimal]:
        return dict(self.holdings)

    async def _venue_costs(self) -> dict[str, float]:
        return dict(self.costs)

    async def _venue_cash(self) -> float | None:
        return self.cash


async def test_a_market_order_opening_a_new_position_is_bounded_by_the_ceiling():
    """미보유 종목의 시장가 주문이 한도를 그냥 통과했습니다."""
    pf = _portfolio()
    broker = FakeVenue(pf, paper_venue=True, max_order_notional=3_000_000.0)
    order = _order(qty="1000", price=None)
    await broker.submit(order)
    assert order.status is OrderStatus.REJECTED
    assert "per-order limit" in order.reject_reason
    assert broker.sent == []


async def test_an_order_nobody_can_price_is_refused_rather_than_waved_through():
    pf = Portfolio(10_000_000.0, "KRW")          # 시세를 한 번도 못 받은 상태
    broker = FakeVenue(pf, paper_venue=True, max_order_notional=3_000_000.0)
    order = _order(qty="1", price=None)
    await broker.submit(order)
    assert order.status is OrderStatus.REJECTED
    assert "가격" in order.reject_reason
    assert broker.sent == []


async def test_a_dry_run_accumulates_the_daily_budget_it_is_supposed_to_rehearse():
    """dry_run 이 한도를 누적하지 않으면, 한도는 실거래에서 처음 발동합니다."""
    pf = _portfolio()
    broker = FakeVenue(pf, max_order_notional=100_000_000.0)
    broker.budget = TradingBudget(max_daily_notional=500_000.0)

    first, second = _order(qty="5"), _order(qty="5")
    await broker.submit(first)
    await broker.submit(second)

    assert first.status is OrderStatus.SUBMITTED
    assert second.status is OrderStatus.REJECTED
    assert "한도" in second.reject_reason
    assert broker.budget.roll().notional == pytest.approx(350_000.0)


# ── sync(): 거래소가 이긴다 ──────────────────────────────────────────────
async def test_sync_adopts_a_position_the_local_book_has_never_seen():
    """드리프트를 로그만 남기고 포지션을 안 만들면 아무것도 보정되지 않습니다."""
    pf = _portfolio()
    broker = FakeVenue(pf, paper_venue=True)
    broker.holdings = {SYM.key: Decimal("10")}
    broker.costs = {SYM.key: 70_000.0}

    report = await broker.sync()

    assert pf.quantity(SYM) == Decimal("10")
    assert pf.position(SYM).avg_price == 70_000.0
    assert pf.cash == pytest.approx(10_000_000.0 - 700_000.0)
    assert report["corrected"][SYM.key] == {"local": 0.0, "venue": 10.0}
    assert report["uncorrected"] == {}


async def test_sync_says_so_when_it_cannot_map_a_venue_holding():
    """이름조차 모르는 보유분은 '보정됨'이 아니라 '눈 감고 거래 중'입니다."""
    pf = _portfolio()
    broker = FakeVenue(pf, paper_venue=True)
    broker.holdings = {"kis:000660": Decimal("5")}

    report = await broker.sync()

    assert report["corrected"] == {}
    assert report["uncorrected"] == {"kis:000660": {"local": 0.0, "venue": 5.0}}
    assert "kis:000660" not in pf.positions


async def test_sync_returns_the_capital_of_a_position_closed_outside_the_engine():
    """수량만 지우면 그 포지션만큼의 자본이 장부에서 증발합니다."""
    pf = _portfolio()
    pf.position(SYM).quantity = Decimal("10")
    pf.position(SYM).avg_price = 70_000.0
    pf.cash = 9_300_000.0
    broker = FakeVenue(pf, paper_venue=True)

    await broker.sync()

    assert pf.quantity(SYM) == Decimal("0")
    assert pf.cash == pytest.approx(10_000_000.0)


async def test_sync_surfaces_the_account_cash_without_resizing_the_strategy():
    """계좌 현금을 그대로 가져오면, 계좌의 일부만 굴리려던 예산이 통째로 커집니다."""
    pf = _portfolio()
    broker = FakeVenue(pf, paper_venue=True)
    broker.cash = 50_000_000.0

    report = await broker.sync()

    assert report["cash"] == {"local": 10_000_000.0, "venue": 50_000_000.0}
    assert pf.cash == 10_000_000.0


async def test_a_dry_run_is_not_flattened_by_a_venue_it_never_traded_on():
    """모의 포지션은 거래소가 알 리 없습니다 — '거래소가 이긴다'가 여기선 틀립니다."""
    pf = _portfolio()
    pf.position(SYM).quantity = Decimal("10")
    pf.position(SYM).avg_price = 70_000.0
    broker = FakeVenue(pf)                        # live=False, paper_venue=False

    report = await broker.sync()

    assert pf.quantity(SYM) == Decimal("10")
    assert report["drift"] == {}
    assert "observed_only" in report
