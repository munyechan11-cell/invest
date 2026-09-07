"""그룹이 **보유가 있는 실계좌** 위에서 시작할 수 있는가.

이 파일이 지키는 것은 하나입니다: 계좌에 주식이 들어 있다는 이유로 그룹이
시작을 거부하면 안 된다. 거부하면 그 주식은 손절도 청산도 닿지 않는 채로
남습니다 — 봇이 어제 산 것이든 사용자가 앱에서 산 것이든.

**대역은 반드시 `LiveBrokerage` 를 부모로, `venue_capital_truth=True` 로
쓰세요.** 이 결함이 기존 그룹 테스트를 전부 통과한 이유가 그것입니다. 가짜
증권사는 `positions()` 가 자기 장부를 답하고 `_known_symbols()` 를 거치지
않으므로, 계좌 장부가 비어 있다는 사실 자체가 드러나지 않았습니다.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.brokerage.live_base import LiveBrokerage
from quant.core.account import Portfolio
from quant.core.types import RunMode, Symbol
from quant.live.agents import AgentGroup, AgentSpec
from quant.live.gateway import AccountGateway

from .test_group_trader import config

SAMSUNG = Symbol("005930", venue="toss", quote_currency="KRW",
                 lot_size=Decimal("1"), tick_size=Decimal("100"))
PRICE = 70_000.0


class TruthVenue(LiveBrokerage):
    """토스 실거래의 계약: 계좌 잔고가 자본의 진실이고 보유는 증권사가 답한다."""

    name = "truth-venue"
    venue_capital_truth = True

    def __init__(self, book, *, holdings=None, cash=1_000_000.0):
        super().__init__(book, live=True, max_order_notional=1e9)
        self.holdings = {k: Decimal(str(v)) for k, v in (holdings or {}).items()}
        self.cash = float(cash)

    async def _venue_submit(self, order):
        return f"v-{order.id}"

    async def _venue_cancel(self, order):
        return True

    async def _venue_open_orders(self):
        return []

    async def _venue_positions(self):
        return dict(self.holdings)

    async def _venue_costs(self):
        return dict.fromkeys(self.holdings, PRICE)

    async def _venue_capital(self):
        return {"currency": "KRW", "cash": self.cash,
                "holdings_value": sum(float(q) * PRICE
                                      for q in self.holdings.values())}


class _Ctx:
    """게이트웨이가 종목을 얻어 가는 최소한의 시점."""

    def __init__(self, symbols, portfolio):
        self.universe = list(symbols)
        self.portfolio = portfolio


def group_of(*ids, live=True):
    mode = RunMode.LIVE if live else RunMode.DRY_RUN
    return AgentGroup(agents=tuple(
        AgentSpec(agent_id=a, label=f"에이전트 {a}", config_path="c.yaml",
                  capital_weight=round(1 / len(ids), 4), mode=mode)
        for a in ids))


def gateway_with_agents(venue, *ids):
    """엔진을 붙인 게이트웨이. `GroupTrader._build_trader` 가 하는 일과 같습니다."""
    gw = AccountGateway(group_of(*ids), venue, base_currency="KRW")
    for agent_id in ids:
        book = Portfolio(500_000.0, "KRW")
        gw._agent_books[agent_id] = book
        gw._agent_contexts[agent_id] = _Ctx([SAMSUNG], book)
    return gw


@pytest.mark.asyncio
async def test_a_group_connects_when_the_account_already_holds_shares():
    """보유가 있는 계좌에서 그룹이 켜진다 — 이 파일의 전부입니다.

    되돌리면(계좌 장부에 종목을 등록하지 않으면) `connect()` 가
    "실계좌 자산을 확인하지 못해 실거래를 시작하지 않습니다" 로 죽습니다.
    """
    venue = TruthVenue(Portfolio(1_000.0, "KRW"), holdings={SAMSUNG.key: 10})
    gw = gateway_with_agents(venue, "attack", "defend")

    await gw.connect()

    assert venue.account_ready is True, "계좌 자산 확인이 통과해야 한다"
    # 증권사 보유가 계좌 장부에 실제로 들어왔는가 — 원가까지.
    booked = venue.portfolio.positions[SAMSUNG.key]
    assert booked.quantity == Decimal("10")
    assert booked.avg_price == pytest.approx(PRICE)


@pytest.mark.asyncio
async def test_the_holdings_become_unassigned_and_the_invariant_is_satisfied():
    """시작 뒤 그 보유는 미귀속이고, 합계 불변식이 성립한다.

    연결만 되고 채택이 안 되면 첫 동기화가 같은 수량을 드리프트로 읽어
    그룹을 멈춥니다 — 연결 성공만으로는 부족합니다.
    """
    venue = TruthVenue(Portfolio(1_000.0, "KRW"), holdings={SAMSUNG.key: 10})
    gw = gateway_with_agents(venue, "attack", "defend")
    await gw.connect()

    gw.adopt_unassigned(await gw.read_venue_positions())

    assert gw.unassigned_positions() == {SAMSUNG.key: Decimal("10")}
    assert gw.check_invariant({SAMSUNG.key: Decimal("10")}) == {}
    assert gw.halted is False


@pytest.mark.asyncio
async def test_an_empty_account_still_connects():
    """보유가 없는 계좌(첫날)도 그대로 켜져야 한다 — 회귀 방지."""
    venue = TruthVenue(Portfolio(1_000.0, "KRW"))
    gw = gateway_with_agents(venue, "attack")
    await gw.connect()
    assert venue.account_ready is True


@pytest.mark.asyncio
async def test_a_holding_outside_every_universe_is_still_refused():
    """유니버스 어디에도 없는 종목은 여전히 매핑되지 않는다.

    이 수정이 넓힌 것은 "우리가 아는 종목인데 장부가 몰랐던" 경우뿐입니다.
    모르는 종목까지 조용히 들이면 수량만 있고 사이징·손절이 못 거는
    포지션이 생깁니다 — 어댑터의 기존 정책을 그대로 둡니다.
    """
    from quant.brokerage.base import BrokerageError

    other = Symbol("000660", venue="toss", quote_currency="KRW")
    venue = TruthVenue(Portfolio(1_000.0, "KRW"), holdings={other.key: 3})
    gw = gateway_with_agents(venue, "attack")

    with pytest.raises(BrokerageError, match="확인하지 못해"):
        await gw.connect()


@pytest.mark.asyncio
async def test_registration_adds_no_quantity_and_no_exposure():
    """등록은 자리만 만든다 — 수량·평가액·노출이 생기면 안 된다."""
    venue = TruthVenue(Portfolio(1_000.0, "KRW"))
    gw = gateway_with_agents(venue, "attack")

    added = gw._register_account_symbols()

    assert added == 1
    assert SAMSUNG.key in venue.portfolio.positions
    assert venue.portfolio.open_positions == []
    assert venue.portfolio.holdings_value == 0.0
    assert gw.aggregate_sleeves() == {}


@pytest.mark.asyncio
async def test_config_universe_symbols_reach_the_account_book():
    """설정에 적힌 종목이 실제로 등록되는 경로인가 (`config()` 의 AAA)."""
    from quant.strategy.builder import build_engine

    engine, _provider = build_engine(config("attack"))
    venue = TruthVenue(Portfolio(1_000.0, "KRW"))
    gw = AccountGateway(group_of("attack"), venue, base_currency="KRW")
    gw.attach_engine("attack", engine)

    assert gw._register_account_symbols() >= 1
    assert any(key.endswith("AAA") or "AAA" in key
               for key in venue.portfolio.positions)


# ── 정산이 끝나지 않은 스냅샷으로 불변식을 보면 안 된다 ──────────────────
#
# 토스는 주문 상세가 FILLED 를 먼저 보이고 보유 조회가 늦게 따라오는 구간을
# 어댑터가 명시적으로 전제합니다(`transient: terminal_order_settlement`).
# 그 뒤처진 스냅샷으로 불변식을 보면 방금 적은 체결이 드리프트가 되고, 정지는
# sticky 라 보유가 따라잡아도 안 풀리며, **정지된 그룹은 손절도 못 냅니다.**

class LaggingVenue(TruthVenue):
    """보유 조회가 한 박자 늦는 계좌. 어댑터는 그것을 스스로 알고 알린다."""

    def __init__(self, book, **kw):
        super().__init__(book, **kw)
        self.report_ok = False

    async def sync(self, **kwargs):
        return {
            "ok": self.report_ok,
            "transient": "terminal_order_settlement",
            "error": "체결이 아직 보유에 반영되지 않았습니다",
            # 뒤처진 스냅샷이 그대로 실려 온다 — 이것이 결함의 재료였습니다.
            "venue_positions": {k: float(v) for k, v in self.holdings.items()},
        }


@pytest.mark.asyncio
async def test_an_unsettled_report_does_not_halt_the_group():
    venue = LaggingVenue(Portfolio(1_000.0, "KRW"))
    gw = gateway_with_agents(venue, "attack")
    gw._unassigned_adopted = True
    gw.apply_fill("attack", SAMSUNG, Decimal("5"))   # 원장에는 5주가 적혔고

    report = await gw.sync_for("attack")             # 증권사 보유는 아직 0

    assert report["sleeve_drift"] is None, "확인을 미뤄야 한다"
    assert gw.halted is False, "정산 지연으로 그룹을 멈추면 손절이 막힌다"


@pytest.mark.asyncio
async def test_a_settled_report_still_catches_real_drift():
    """미루기만 하고 영영 안 보면 안전장치가 아닙니다 — 정산되면 잡아야 합니다."""
    venue = LaggingVenue(Portfolio(1_000.0, "KRW"))
    gw = gateway_with_agents(venue, "attack")
    gw._unassigned_adopted = True
    gw.apply_fill("attack", SAMSUNG, Decimal("5"))

    await gw.sync_for("attack")
    assert gw.halted is False

    venue.report_ok = True                            # 정산이 끝났는데
    await gw.sync_for("attack")                       # 증권사에는 여전히 0

    assert gw.halted is True
    assert "005930" in gw.halt_reason


# ── 형제의 미결 매수가 내 손절을 막으면 안 된다 ──────────────────────────
#
# 계좌 진실 어댑터는 같은 종목의 미결 주문이 있으면 추가 주문을 직렬화합니다.
# 막으려는 것은 초과 매도(같은 보유를 두고 두 매도가 각자 사이징하는 것)인데,
# 방향을 가리지 않아 **반대 방향** 미결까지 걸렸습니다.
#
# 그룹에서 그 결과: 형제가 같은 종목에 먼 지정가 매수를 하루 종일 걸어 두면
# 내 손절이 매 유지 주기마다 거절됩니다. 게이트웨이는 남의 주문을 보여 주지도
# 취소하지도 않으므로 빠져나갈 방법이 없습니다.

async def _account_with_holdings(*ids):
    venue = TruthVenue(Portfolio(1_000.0, "KRW"), holdings={SAMSUNG.key: 20})
    gw = gateway_with_agents(venue, *ids)
    await gw.connect()
    gw.adopt_unassigned(await gw.read_venue_positions())
    for agent_id in ids:
        book = gw._agent_books[agent_id]
        book.mark(SAMSUNG, PRICE)
    return venue, gw


def market(side, qty):
    from quant.core.types import Order, OrderType

    return Order(SAMSUNG, side, Decimal(str(qty)), OrderType.MARKET)


def limit(side, qty, price):
    from quant.core.types import Order, OrderType

    return Order(SAMSUNG, side, Decimal(str(qty)), OrderType.LIMIT,
                 limit_price=price)


@pytest.mark.asyncio
async def test_a_siblings_resting_buy_does_not_block_my_stop():
    from quant.core.types import OrderSide, OrderStatus

    venue, gw = await _account_with_holdings("attack", "defend")
    gw.apply_fill("defend", SAMSUNG, Decimal("10"))
    resting = await gw.submit_for("attack", limit(OrderSide.BUY, 1, 60_000.0))
    assert resting.status is OrderStatus.SUBMITTED, resting.reject_reason

    stop = await gw.submit_for("defend", market(OrderSide.SELL, 10))

    assert stop.status is not OrderStatus.REJECTED, stop.reject_reason


@pytest.mark.asyncio
async def test_a_competing_sell_is_still_serialized():
    """반대 방향. 같은 보유를 두 매도가 각자 사이징하면 초과 매도가 됩니다."""
    from quant.core.types import OrderSide, OrderStatus

    venue, gw = await _account_with_holdings("attack", "defend")
    gw.apply_fill("attack", SAMSUNG, Decimal("10"))
    gw.apply_fill("defend", SAMSUNG, Decimal("10"))
    first = await gw.submit_for("attack", limit(OrderSide.SELL, 10, 80_000.0))
    assert first.status is OrderStatus.SUBMITTED, first.reject_reason

    second = await gw.submit_for("defend", market(OrderSide.SELL, 10))

    assert second.status is OrderStatus.REJECTED
    assert "미결 주문" in (second.reject_reason or "")


@pytest.mark.asyncio
async def test_a_new_entry_is_still_serialized_by_any_pending_order():
    """신규 노출은 매수 가능 금액을 소비하므로 예전처럼 전부와 직렬화합니다."""
    from quant.core.types import OrderSide, OrderStatus

    venue, gw = await _account_with_holdings("attack", "defend")
    gw.apply_fill("defend", SAMSUNG, Decimal("10"))
    resting = await gw.submit_for("defend", limit(OrderSide.SELL, 1, 90_000.0))
    assert resting.status is OrderStatus.SUBMITTED, resting.reject_reason

    entry = await gw.submit_for("attack", market(OrderSide.BUY, 1))

    assert entry.status is OrderStatus.REJECTED
