"""같은 계좌, 같은 종목, 다른 성향 — 진짜 엔진 둘로 확인한다.

앞의 세 파일은 각 층을 따로 봤습니다. 여기서는 실제 `Engine` 두 개를 세워
사용자가 요구한 그대로의 상황을 만듭니다:

    계좌에 10만원. 공격형과 보수형이 5만원씩. 둘 다 005930 을 든다.
    가격이 떨어진다. **보수형만 손절되고 공격형은 버틴다.**

이것이 되면 나머지는 배선입니다. 되지 않으면 이 기능은 존재할 수 없습니다 —
손절 폭이 다르다는 것은 같은 가격에서 한쪽만 팔린다는 뜻이고, 그 매도가 상대의
물량을 건드리는 순간 두 성향은 하나로 뭉개집니다.

엔진은 자기가 계좌를 통째로 쓴다고 믿은 채로 돕니다. 그 믿음이 유지되는지를
`ctx.portfolio` 로 확인하고, 계좌의 진실이 어긋나지 않는지를 게이트웨이의
합계 불변식으로 확인합니다.
"""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.alpha.base import AlphaModel
from quant.brokerage.sleeve import SleeveBrokerage
from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.engine import Engine
from quant.core.events import EventBus
from quant.core.types import (
    UTC,
    Bar,
    Direction,
    Insight,
    OrderStatus,
    Quote,
    RunMode,
    Symbol,
)
from quant.execution.models import ImmediateExecution
from quant.live.agents import AgentGroup, AgentSpec
from quant.live.gateway import AccountGateway
from quant.live.limits import TradingBudget
from quant.portfolio.models import EqualWeighting
from quant.risk.models import MaximumDrawdownPerSecurity

SAMSUNG = Symbol("005930", venue="toss", quote_currency="KRW",
                 lot_size=Decimal("1"), tick_size=Decimal("100"))
START = datetime(2026, 3, 2, tzinfo=UTC)


class BuyAndHold(AlphaModel):
    """첫 바에서만 매수 신호. 이후로는 조용합니다 — 손절만 보기 위해서."""

    def __init__(self):
        self.fired = False

    async def update(self, ctx, symbols):
        if self.fired or not symbols:
            return []
        self.fired = True
        return [Insight(symbol=SAMSUNG, direction=Direction.UP,
                        period=timedelta(days=100), generated_at=ctx.now,
                        confidence=1.0, source="test")]


class InstantVenue:
    """증권사 대역 — 보낸 대로 즉시 체결. 계좌 합계만 기억합니다."""

    name = "instant"
    portfolio = None
    budget = None

    def __init__(self):
        self.book: dict[str, Decimal] = {}
        self.submitted = []

    async def submit(self, order):
        self.submitted.append(order)
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        order.avg_fill_price = order.limit_price or self.mark
        self.book[order.symbol.key] = (
            self.book.get(order.symbol.key, Decimal("0")) + order.signed_filled
        )
        return order

    async def cancel(self, order):
        return True

    async def open_orders(self):
        return []

    async def positions(self):
        return {k: v for k, v in self.book.items() if v != 0}

    async def sync(self):
        return {}

    async def connect(self):
        return None

    async def close(self):
        return None

    def exact_flatten_order_type(self, symbol, current_quantity, target_quantity):
        return None

    mark = 1_000.0


def bar(ts, close, high=None, low=None):
    return Bar(SAMSUNG, ts, close, high or close * 1.005, low or close * 0.995,
               close, 1e6, "1d")


class Desk:
    """에이전트 한 대의 배선 — 장부·컨텍스트·엔진·슬리브."""

    def __init__(self, agent_id, gateway, cash, stop_pct, clock):
        self.agent_id = agent_id
        self.portfolio = Portfolio(starting_cash=cash, base_currency="KRW")
        self.ctx = Context(clock, self.portfolio, EventBus(), timeframe="1d",
                           run_mode=RunMode.DRY_RUN)
        self.ctx.universe = [SAMSUNG]
        self.sleeve = SleeveBrokerage(agent_id, gateway, mode=RunMode.DRY_RUN)
        self.engine = Engine(
            self.ctx,
            BuyAndHold(),
            EqualWeighting(),
            ImmediateExecution(),
            self.sleeve,
            risk_models=[MaximumDrawdownPerSecurity(max_drawdown_pct=stop_pct,
                                                    lock_bars=0)],
            budget=TradingBudget(),
        )

    @property
    def held(self) -> Decimal:
        return self.portfolio.quantity(SAMSUNG)


@pytest.fixture
def desks():
    """공격형(손절 -25%)과 보수형(손절 -5%)이 계좌 10만원을 반씩 나눠 갖는다."""
    clock = SimClock(START)
    venue = InstantVenue()
    group = AgentGroup(agents=(
        AgentSpec(agent_id="attack", label="공격 · 단기",
                  config_path="configs/kr_toss_desk.yaml", capital_weight=0.5),
        AgentSpec(agent_id="defend", label="보수 · 장기",
                  config_path="configs/kr_toss.yaml", capital_weight=0.5),
    ))
    gateway = AccountGateway(group, venue, base_currency="KRW")
    allocations = gateway.allocate_capital(100_000)

    attack = Desk("attack", gateway, allocations["attack"], 0.25, clock)
    defend = Desk("defend", gateway, allocations["defend"], 0.05, clock)
    return gateway, venue, attack, defend, clock


async def feed(desks_, closes):
    """두 엔진에 같은 시세를 흘린다. 반응은 각자의 성향이 정합니다."""
    _, _, attack, defend, clock = desks_
    # 시계가 기준입니다 — `feed` 를 여러 번 부르면 이어서 흐릅니다. START 로
    # 되감으면 SimClock 이 "시간이 거꾸로 갔다" 며 거부합니다.
    ts = clock.now()
    for close in closes:
        ts += timedelta(days=1)
        clock.set(ts)
        for desk in (attack, defend):
            # dry_run 의 마크는 봉이 아니라 호가에서 옵니다. `Context` 는 지금
            # 시각으로 끝나는 봉을 무재조회 규칙에 따라 아직 확정된 과거로
            # 보지 않으므로, 호가를 주지 않으면 손절이 한 봉 늦게 반응합니다.
            desk.ctx.set_quote(Quote(SAMSUNG, ts, bid=close, ask=close))
            desk.portfolio.mark(SAMSUNG, close)
        for desk in (attack, defend):
            # `LiveTrader` 의 순서 그대로입니다: 체결을 먼저 장부에 적고, 그
            # 다음에 전략이 재조정된 계좌를 봅니다. 순서를 바꾸면 같은 체결이
            # 보유 스냅샷과 체결 통지로 두 번 반영됩니다.
            await desk.engine.settle_live_fills()
            await desk.engine.on_bars({SAMSUNG.key: bar(ts, close)}, ts=ts,
                                      settle=False)


#: 진입이 두 장부에 모두 잡힐 때까지. 주문이 나간 다음 봉에 체결이 돌아오므로
#: 한 봉으로는 부족합니다.
ENTRY = [1_000, 1_000, 1_000]


# ── 자본 분할 ────────────────────────────────────────────────────────────
def test_ten_man_won_is_split_in_two(desks):
    gateway, _, attack, defend, _ = desks
    assert attack.portfolio.starting_cash == 50_000
    assert defend.portfolio.starting_cash == 50_000
    assert sum(gateway.allocate_capital(100_000).values()) == 100_000


# ── 같은 종목을 둘이 든다 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_both_agents_can_hold_the_same_symbol(desks):
    """증권사에는 합계 하나뿐이지만 원장에는 둘로 나뉘어 있습니다."""
    gateway, venue, attack, defend, _ = desks
    await feed(desks, ENTRY)

    assert attack.held > 0, "공격형이 진입하지 못했습니다"
    assert defend.held > 0, "보수형이 진입하지 못했습니다"

    sleeves = gateway.aggregate_sleeves()
    assert sleeves["toss:005930"] == attack.held + defend.held
    assert (await venue.positions())["toss:005930"] == sleeves["toss:005930"]


@pytest.mark.asyncio
async def test_each_engine_sees_only_its_own_position(desks):
    """엔진은 자기가 계좌를 통째로 쓴다고 믿은 채로 돕니다.

    상대의 물량이 자기 장부에 보이면 그것까지 포함해 사이징하고, 다음
    손절이 그것까지 팝니다.
    """
    gateway, _, attack, defend, _ = desks
    await feed(desks, ENTRY)

    assert await attack.sleeve.positions() == {"toss:005930": attack.held}
    assert await defend.sleeve.positions() == {"toss:005930": defend.held}
    assert attack.held != gateway.aggregate_sleeves()["toss:005930"]


# ── 핵심: 성향이 다르면 한쪽만 팔린다 ────────────────────────────────────
@pytest.mark.asyncio
async def test_the_tight_stop_exits_while_the_wide_stop_holds(desks):
    """사용자가 요구한 바로 그 동작.

    같은 종목, 같은 시세, 다른 손절. -10% 에서 보수형(-5%)은 나가고
    공격형(-25%)은 버팁니다.
    """
    gateway, _, attack, defend, _ = desks
    await feed(desks, ENTRY)
    entered_attack, entered_defend = attack.held, defend.held
    assert entered_attack > 0 and entered_defend > 0

    await feed(desks, [900, 900])     # -10%, 손절 주문과 그 체결까지

    assert defend.held == 0, "보수형의 -5% 손절이 걸리지 않았습니다"
    assert attack.held > 0, (
        "공격형이 자기 손절(-25%)에 닿지 않았는데 청산됐습니다 — "
        "보수형의 매도가 공격형 물량까지 팔았습니다"
    )
    # 수량이 진입 시점과 정확히 같을 필요는 없습니다. 가격이 떨어지면
    # 비중 유지를 위해 재조정이 일어나고, 그것은 이 성향의 정상 동작입니다.
    # 확인해야 할 것은 "청산되지 않았다" 이지 "한 주도 안 움직였다" 가 아닙니다.


@pytest.mark.asyncio
async def test_the_exit_leaves_the_other_agents_shares_at_the_venue(desks):
    """계좌에서도 공격형의 주식은 그대로 남아 있어야 합니다."""
    gateway, venue, attack, defend, _ = desks
    await feed(desks, ENTRY)
    await feed(desks, [900, 900])

    # 보수형이 완전히 빠져나간 뒤, 계좌에 남은 주식은 전부 공격형의 것입니다.
    # 이 등식이 깨지면 보수형의 매도가 공격형 물량을 건드렸다는 뜻입니다.
    #
    # 슬리브와 엔진 장부를 직접 비교하지는 않습니다. 슬리브는 주문이 나갈 때,
    # 장부는 그 체결이 돌아올 때 움직이므로 한 봉만큼 슬리브가 앞섭니다 —
    # 증권사도 같은 시점에 앞서므로 불변식은 그대로 성립합니다.
    assert gateway.sleeve_positions("defend") == {}
    assert attack.held > 0
    assert (await venue.positions()) == gateway.sleeve_positions("attack")


@pytest.mark.asyncio
async def test_the_invariant_holds_through_the_whole_run(desks):
    """매 단계에서 `Σ 슬리브 == 증권사 합계` 가 성립해야 합니다.

    한 번이라도 깨지면 그 시점 이후의 모든 손절은 남의 물량을 팔 수 있습니다.
    """
    gateway, venue, _, _, _ = desks
    for closes in (ENTRY, [900, 900], [800], [700, 700]):
        await feed(desks, closes)
        drift = gateway.check_invariant(await venue.positions())
        assert drift == {}, f"원장이 계좌와 갈라졌습니다: {drift}"
    assert gateway.halted is False


@pytest.mark.asyncio
async def test_the_wide_stop_eventually_exits_too(desks):
    """공격형도 자기 손절에는 걸립니다 — 버티는 것이지 면제가 아닙니다."""
    _, _, attack, _, _ = desks
    await feed(desks, ENTRY)
    await feed(desks, [900, 800, 720, 700, 700])          # -28%

    assert attack.held == 0


# ── 한도는 각자 것 ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_each_agent_carries_its_own_daily_ledger(desks):
    """에이전트의 하루 원장은 자기 것입니다. 계좌 한도는 그 위에 따로 있습니다."""
    _, _, attack, defend, _ = desks
    await feed(desks, ENTRY)

    assert attack.engine.budget is not defend.engine.budget

    def orders_seen(budget):
        # 원장은 하루마다 갈리므로 봉이 여러 날에 걸치면 `today` 는 비어 있고
        # 실제 주문은 `history` 에 있습니다.
        return budget.today.orders + sum(day.orders for day in budget.history)

    assert orders_seen(attack.engine.budget) >= 1
    assert orders_seen(defend.engine.budget) >= 1


@pytest.mark.asyncio
async def test_a_halted_group_stops_both_engines(desks):
    """정지는 그룹 단위입니다 — 어느 슬리브가 틀렸는지 모르기 때문입니다."""
    gateway, venue, attack, defend, _ = desks
    await feed(desks, ENTRY)

    # 사용자가 증권사 앱에서 직접 팔았다고 하자.
    venue.book["toss:005930"] -= Decimal("1")
    gateway.check_invariant(await venue.positions())
    assert gateway.halted is True

    from quant.live.gateway import GroupHalted
    for desk in (attack, defend):
        with pytest.raises(GroupHalted):
            await gateway.submit_for(desk.agent_id, _any_order())


def _any_order():
    from quant.core.types import Order, OrderSide, OrderType
    return Order(symbol=SAMSUNG, side=OrderSide.BUY, quantity=Decimal("1"),
                 type=OrderType.MARKET)
