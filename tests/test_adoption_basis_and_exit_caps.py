"""장부에 들어오면 안 되는 수량과, 장부에서 나가야 하는데 막히던 주문.

두 결함 모두 `LiveBrokerage` 의 공통 층에 있어, 토스든 KIS 든 같이 걸립니다.

**원가 없는 수량 채택.** 실계좌 진실 분기는 평균 매수가를 모르는 보유분을
"venue average cost is unavailable" 로 거부했는데, 그렇지 않은 분기(KIS 모의·
실계좌 등 `venue_capital_truth = False`)는 같은 보유분을 avg_price 0 으로
들였습니다. KIS 는 `pchs_avg_pric` 이 비면 0 을 보냅니다. 원가 0 인 포지션은
평가액이 통째로 이익으로 잡혀 자본이 부풀고, 퍼센트 손절은 기준가 0 으로
계산돼 영영 안 걸립니다. 게다가 `report["uncorrected"]` 를 읽는 곳이 없어,
"눈 감고 거래 중" 인 종목에도 신규 진입이 그대로 나갔습니다.

**주문 상한이 청산을 가둠.** `max_order_notional` 이 줄이는 주문에도 걸려,
상한보다 커진 포지션(가격이 오른 보유분, 외부 입고분)을 손절할 길이 없었습니다.
이 저장소의 규칙은 "한도에 닿으면 신규 진입만 멈추고 청산은 계속됩니다" 입니다.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.brokerage.live_base import LiveBrokerage
from quant.core.account import Portfolio
from quant.core.types import Order, OrderSide, OrderStatus, OrderType, Symbol

SYM = Symbol("005930", venue="kis", quote_currency="KRW", tick_size=Decimal("100"))
OTHER = Symbol("000660", venue="kis", quote_currency="KRW", tick_size=Decimal("100"))
PRICE = 70_000.0


class FakeVenue(LiveBrokerage):
    """실제 어댑터의 부모를 그대로 쓴 최소 구현 — 가드와 동기화는 진짜 코드입니다."""

    name = "fake"

    def __init__(self, portfolio, **kwargs):
        super().__init__(portfolio, paper_venue=True, **kwargs)
        self.sent: list[Order] = []
        self.holdings: dict[str, Decimal] = {}
        self.costs: dict[str, float] = {}

    async def _venue_submit(self, order: Order) -> str:
        self.sent.append(order)
        return f"venue-{len(self.sent)}"

    async def _venue_positions(self) -> dict[str, Decimal]:
        return dict(self.holdings)

    async def _venue_costs(self) -> dict[str, float]:
        return dict(self.costs)


def _portfolio() -> Portfolio:
    pf = Portfolio(10_000_000.0, "KRW")
    pf.mark(SYM, PRICE)
    pf.mark(OTHER, PRICE)
    return pf


def _order(side: OrderSide, qty: str, symbol: Symbol = SYM) -> Order:
    return Order(symbol=symbol, side=side, quantity=Decimal(qty),
                 type=OrderType.LIMIT, limit_price=PRICE)


# ── 원가를 모르는 수량은 들이지 않는다 ───────────────────────────────────
async def test_sync_refuses_to_adopt_a_quantity_whose_cost_basis_is_unknown():
    pf = _portfolio()
    broker = FakeVenue(pf)
    broker.holdings = {SYM.key: Decimal("10")}
    broker.costs = {}                       # KIS: pchs_avg_pric 이 비어 0

    report = await broker.sync()

    assert pf.quantity(SYM) == Decimal("0"), "원가 0 으로 수량이 들어왔습니다"
    assert pf.cash == pytest.approx(10_000_000.0)
    assert report["corrected"] == {}
    assert report["uncorrected"][SYM.key]["reason"] == "venue average cost is unavailable"
    assert report["uncorrected"][SYM.key]["venue"] == 10.0


async def test_a_venue_basis_is_still_adopted_normally():
    """거부는 원가를 **모를 때** 만입니다 — 알면 예전처럼 수량과 현금을 함께 들입니다."""
    pf = _portfolio()
    broker = FakeVenue(pf)
    broker.holdings = {SYM.key: Decimal("10")}
    broker.costs = {SYM.key: PRICE}

    report = await broker.sync()

    assert pf.quantity(SYM) == Decimal("10")
    assert pf.position(SYM).avg_price == PRICE
    assert pf.cash == pytest.approx(10_000_000.0 - 10 * PRICE)
    assert report["uncorrected"] == {}


async def test_a_new_entry_on_an_unresolved_symbol_is_refused_until_a_basis_arrives():
    pf = _portfolio()
    broker = FakeVenue(pf, max_order_notional=100_000_000.0)
    broker.holdings = {SYM.key: Decimal("10")}
    broker.costs = {}
    await broker.sync()

    entry = _order(OrderSide.BUY, "5")
    await broker.submit(entry)
    assert entry.status is OrderStatus.REJECTED
    assert SYM.ticker in entry.reject_reason
    assert "신규 진입" in entry.reject_reason
    assert broker.sent == []

    # 다른 종목은 무관합니다 — 종목 단위의 격리여야 합니다.
    unrelated = _order(OrderSide.BUY, "5", symbol=OTHER)
    await broker.submit(unrelated)
    assert unrelated.status is OrderStatus.SUBMITTED

    # 증권사가 원가를 주면 다음 동기화가 자연히 풉니다.
    broker.costs = {SYM.key: PRICE}
    await broker.sync()
    assert pf.quantity(SYM) == Decimal("10")
    released = _order(OrderSide.BUY, "5")
    await broker.submit(released)
    assert released.status is OrderStatus.SUBMITTED, released.reject_reason


async def test_a_new_entry_on_an_unmapped_holding_is_refused_too():
    """이름조차 모르는 보유분 위에 또 사면 노출은 커지는데 아무 규칙도 못 봅니다."""
    pf = Portfolio(10_000_000.0, "KRW")
    pf.mark(SYM, PRICE)
    broker = FakeVenue(pf, max_order_notional=100_000_000.0)
    broker.holdings = {OTHER.key: Decimal("5")}      # OTHER 는 이 실행이 모르는 종목
    await broker.sync()

    pf.mark(OTHER, PRICE)                             # 이제야 시세를 받아 주문을 냅니다
    entry = _order(OrderSide.BUY, "5", symbol=OTHER)
    await broker.submit(entry)

    assert entry.status is OrderStatus.REJECTED
    assert OTHER.ticker in entry.reject_reason
    assert broker.sent == []


async def test_a_reduction_on_an_unresolved_symbol_is_never_blocked():
    """장부에 이미 있던 수량은 줄일 수 있어야 합니다 — 격리가 청산을 가두면 안 됩니다."""
    pf = _portfolio()
    held = pf.position(SYM)
    held.quantity = Decimal("10")
    held.avg_price = 0.0                    # 원가 없는 legacy 행
    broker = FakeVenue(pf, max_order_notional=100_000_000.0)
    broker.holdings = {SYM.key: Decimal("15")}
    broker.costs = {}
    report = await broker.sync()
    assert SYM.key in report["uncorrected"]
    assert pf.quantity(SYM) == Decimal("10")

    exit_order = _order(OrderSide.SELL, "10")
    await broker.submit(exit_order)
    assert exit_order.status is OrderStatus.SUBMITTED, exit_order.reject_reason

    more = _order(OrderSide.BUY, "1")
    await broker.submit(more)
    assert more.status is OrderStatus.REJECTED


# ── 주문 상한은 신규 노출에만 ────────────────────────────────────────────
async def test_a_reduction_larger_than_the_per_order_cap_is_still_sent():
    pf = _portfolio()
    held = pf.position(SYM)
    held.quantity = Decimal("100")
    held.avg_price = PRICE                  # 7,000,000 원 — 상한의 두 배가 넘습니다
    broker = FakeVenue(pf, max_order_notional=3_000_000.0)

    exit_order = _order(OrderSide.SELL, "100")
    await broker.submit(exit_order)
    assert exit_order.status is OrderStatus.SUBMITTED, exit_order.reject_reason
    assert broker.sent == [exit_order]

    entry = _order(OrderSide.BUY, "100")
    await broker.submit(entry)
    assert entry.status is OrderStatus.REJECTED
    assert "per-order limit" in entry.reject_reason
    assert broker.sent == [exit_order]


async def test_an_oversell_is_not_a_reduction_and_still_hits_the_cap():
    """보유보다 큰 매도는 남는 만큼 새 공매도입니다 — 상한이 그대로 걸려야 합니다."""
    pf = _portfolio()
    held = pf.position(SYM)
    held.quantity = Decimal("10")
    held.avg_price = PRICE
    broker = FakeVenue(pf, max_order_notional=3_000_000.0)

    oversell = _order(OrderSide.SELL, "100")
    await broker.submit(oversell)

    assert oversell.status is OrderStatus.REJECTED
    assert "per-order limit" in oversell.reject_reason
    assert broker.sent == []
