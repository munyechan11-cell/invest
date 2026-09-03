"""슬리브 — 에이전트가 자기 몫만 보고, 자기 몫만 파는지.

이 파일이 지키는 사고는 하나입니다. 공격형과 보수형이 같은 005930 을 10주씩
들고 있을 때, 증권사에는 20주가 있을 뿐입니다. 공격형의 손절이 20주를 팔면
보수형은 아무것도 하지 않았는데 포지션이 사라지고, 그 손실은 원장 어디에도
이유가 남지 않습니다.

`positions()` 가 계좌 합계를 돌려주는 것과 매도가 클램프되지 않는 것, 두 경로
모두가 그 사고로 이어집니다. 그래서 양쪽을 따로 확인합니다.
"""
from decimal import Decimal

import pytest

from quant.brokerage.base import BrokerageError
from quant.brokerage.sleeve import SleeveBrokerage
from quant.core.account import Portfolio
from quant.core.types import (
    AssetClass,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    RunMode,
    Symbol,
)

SAMSUNG = Symbol(ticker="005930", venue="toss", asset_class=AssetClass.EQUITY,
                 quote_currency="KRW", lot_size=Decimal("1"),
                 tick_size=Decimal("100"))


class FakeGateway:
    """계좌 층의 최소 대역. 무엇이 실제로 내려갔는지만 기록합니다."""

    def __init__(self, sleeves=None, cash=None):
        self.sleeves = sleeves or {}
        self.cash = cash or {}
        self.submitted: list[tuple[str, Order]] = []
        self.canceled: list[tuple[str, Order]] = []
        self.synced: list[str] = []

    async def submit_for(self, agent_id, order):
        self.submitted.append((agent_id, order))
        order.status = OrderStatus.NEW
        return order

    async def cancel_for(self, agent_id, order):
        self.canceled.append((agent_id, order))
        return True

    async def open_orders_for(self, agent_id):
        return [o for a, o in self.submitted if a == agent_id]

    async def sync_for(self, agent_id):
        self.synced.append(agent_id)
        return {"agent_id": agent_id}

    def sleeve_positions(self, agent_id):
        return dict(self.sleeves.get(agent_id, {}))

    def sleeve_balances(self, agent_id):
        return dict(self.cash.get(agent_id, {}))

    def exact_flatten_order_type_for(self, symbol, current_quantity, target_quantity):
        return None


def sleeve(agent_id="attack", gateway=None, held=None, mode=RunMode.DRY_RUN,
           symbol=SAMSUNG):
    """슬리브 하나. `held` 는 **원장과 장부 양쪽** 에 넣습니다.

    한쪽만 채우면 현실에 없는 상태가 됩니다. `_sleeve_quantity` 는 둘 중 작은
    쪽을 쓰므로 원장을 비워 두면 무엇을 넣든 0 이고, 그것은 테스트가 통과하든
    실패하든 아무것도 증명하지 못합니다.
    """
    gateway = gateway or FakeGateway()
    broker = SleeveBrokerage(agent_id, gateway, mode=mode)
    if held is not None:
        gateway.sleeves.setdefault(agent_id, {})[symbol.key] = Decimal(str(held))
        # `Engine.__init__` 이 하는 배선을 여기서 대신 합니다.
        book = Portfolio(starting_cash=1_000_000, base_currency="KRW")
        position = book.position(symbol)
        position.quantity = Decimal(str(held))
        position.avg_price = 70_000.0
        broker.portfolio = book
    return broker, gateway


def sell(qty):
    return Order(symbol=SAMSUNG, side=OrderSide.SELL, quantity=Decimal(str(qty)),
                 type=OrderType.MARKET)


def buy(qty):
    return Order(symbol=SAMSUNG, side=OrderSide.BUY, quantity=Decimal(str(qty)),
                 type=OrderType.MARKET)


# ── 매도 클램프 ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_agent_cannot_sell_another_agents_shares():
    """공격형 10주 + 보수형 10주 = 계좌 20주. 공격형이 20주를 팔려 하면
    자기 10주까지만 나갑니다."""
    broker, gateway = sleeve("attack", held=10)
    await broker.submit(sell(20))

    _, sent = gateway.submitted[0]
    assert sent.quantity == Decimal("10"), "보수형의 10주까지 팔렸습니다"
    assert sent.meta["sleeve_trimmed"] == "10"
    assert sent.meta["sleeve_held"] == "10"


@pytest.mark.asyncio
async def test_selling_exactly_its_own_holding_is_untouched():
    """자기 물량 전량 청산은 줄이지 않습니다 — 손절이 온전히 나가야 합니다."""
    broker, gateway = sleeve("attack", held=10)
    await broker.submit(sell(10))

    _, sent = gateway.submitted[0]
    assert sent.quantity == Decimal("10")
    assert "sleeve_trimmed" not in (sent.meta or {})


@pytest.mark.asyncio
async def test_selling_less_than_held_is_untouched():
    broker, gateway = sleeve("attack", held=10)
    await broker.submit(sell(3))
    assert gateway.submitted[0][1].quantity == Decimal("3")


@pytest.mark.asyncio
async def test_selling_with_nothing_held_is_refused_not_silently_zeroed():
    """0주짜리 주문을 내려보내면 증권사가 거절하고, 그 거절은 이유를 설명하지
    못합니다. 여기서 문장으로 끝냅니다."""
    broker, gateway = sleeve("attack", held=0)
    with pytest.raises(BrokerageError, match="다른 에이전트 물량은 팔 수 없습니다"):
        await broker.submit(sell(10))
    assert gateway.submitted == [], "거절해야 할 주문이 증권사로 갔습니다"


@pytest.mark.asyncio
async def test_clamp_is_a_trim_not_a_rejection():
    """줄여서 내보내지 않고 거절하면 손절이 막혀 손실 포지션에 갇힙니다.

    `quant.live.limits` — "빠져나오지 못하게 하는 한도는 안전장치가 아니다".
    """
    broker, gateway = sleeve("attack", held=7)
    order = await broker.submit(sell(100))
    assert order.status is not OrderStatus.REJECTED
    assert gateway.submitted[0][1].quantity == Decimal("7")


def test_a_short_enabled_sleeve_is_refused_outright():
    """공매도는 한 계좌를 나눠 쓰는 동안 표현이 불가능합니다.

    a1 이 10주를 공매도하고 a2 가 10주를 들고 있으면 증권사 순수량은 0 이고,
    합계 불변식은 성립하지만 **a2 의 10주는 계좌에 없습니다.** 조용히 허용한
    결과는 "공매도가 된다" 가 아니라 "한 에이전트가 다른 에이전트의 주식을
    소비한다" 입니다. 게다가 예전에는 `allow_short` 하나로 클램프 자체가
    통째로 건너뛰어져, 상한이 사라진 매도가 그대로 나갔습니다.
    """
    with pytest.raises(BrokerageError, match="공매도를 켤 수 없습니다"):
        SleeveBrokerage("attack", FakeGateway(), allow_short=True)


def test_a_sleeve_always_reports_short_as_disabled():
    broker, _ = sleeve("attack", held=10)
    assert broker.allow_short is False


@pytest.mark.asyncio
async def test_buy_orders_are_never_clamped():
    """매수는 남의 물량을 건드릴 수 없습니다. 현금은 슬리브 장부가 이미
    제한하고, 계좌 한도는 게이트웨이가 봅니다."""
    broker, gateway = sleeve("attack", held=0)
    await broker.submit(buy(50))
    assert gateway.submitted[0][1].quantity == Decimal("50")


@pytest.mark.asyncio
async def test_clamp_respects_the_lot_grid():
    """줄인 수량도 격자 위에 있어야 합니다 — 아니면 증권사가 거절합니다."""
    odd = Symbol(ticker="XYZ", venue="toss", quote_currency="KRW",
                 lot_size=Decimal("10"), tick_size=Decimal("1"))
    broker, gateway = sleeve("attack", held=25, symbol=odd)

    order = Order(symbol=odd, side=OrderSide.SELL, quantity=Decimal("100"),
                  type=OrderType.MARKET)
    await broker.submit(order)
    assert gateway.submitted[0][1].quantity == Decimal("20")  # 25 → 격자 아래로


# ── 자기 몫만 본다 ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_positions_returns_the_sleeve_never_the_account_total():
    """여기서 합계를 돌려주면 엔진의 재조정이 남의 물량을 자기 것으로
    채택하고, 다음 손절이 그것까지 팝니다."""
    gateway = FakeGateway(sleeves={
        "attack": {"toss:005930": Decimal("10")},
        "defend": {"toss:005930": Decimal("10")},
    })
    broker, _ = sleeve("attack", gateway=gateway)
    assert await broker.positions() == {"toss:005930": Decimal("10")}


@pytest.mark.asyncio
async def test_balances_returns_only_the_allocated_slice():
    gateway = FakeGateway(cash={"attack": {"KRW": 50_000.0},
                                "defend": {"KRW": 50_000.0}})
    broker, _ = sleeve("attack", gateway=gateway)
    assert await broker.balances() == {"KRW": 50_000.0}


@pytest.mark.asyncio
async def test_open_orders_are_scoped_to_this_agent():
    gateway = FakeGateway()
    attack, _ = sleeve("attack", gateway=gateway, held=10)
    defend, _ = sleeve("defend", gateway=gateway, held=10)
    await attack.submit(sell(5))
    await defend.submit(sell(5))

    assert len(await attack.open_orders()) == 1
    assert len(await defend.open_orders()) == 1


# ── 계좌에 하나뿐인 것들 ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_connect_and_close_are_no_ops_on_the_sleeve():
    """슬리브마다 연결하면 같은 증권사에 네 개의 세션이 열리고, 토큰
    재발급이 서로를 무효화합니다."""
    broker, gateway = sleeve("attack")
    assert await broker.connect() is None
    assert await broker.close() is None


def test_sleeve_does_not_adopt_account_capital():
    """계좌 진실은 게이트웨이만 압니다. 슬리브 넷이 각자 채택하면 같은
    현금이 네 번 계산됩니다."""
    broker, _ = sleeve("attack")
    assert broker.venue_capital_truth is False
    assert broker.uses_venue_capital is False


def test_sleeve_is_not_a_live_brokerage_subclass():
    """`LiveTrader` 의 `isinstance(brokerage, LiveBrokerage)` 분기가 슬리브에서
    꺼지는 것이 의도된 동작입니다 — 계좌 단위 복구 격리는 계좌에 하나여야
    합니다."""
    from quant.brokerage.live_base import LiveBrokerage
    broker, _ = sleeve("attack")
    assert not isinstance(broker, LiveBrokerage)


def test_live_mode_is_carried_onto_the_sleeve():
    broker, _ = sleeve("attack", mode=RunMode.LIVE)
    assert broker.live is True
    assert broker.run_mode is RunMode.LIVE


@pytest.mark.asyncio
async def test_sync_is_delegated_with_the_agent_identity():
    broker, gateway = sleeve("attack")
    await broker.sync()
    assert gateway.synced == ["attack"]
