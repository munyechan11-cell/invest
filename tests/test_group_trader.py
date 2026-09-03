"""그룹 트레이더 — 계좌 것과 에이전트 것이 제자리에 있는가.

`LiveTrader` 는 "이 프로세스에 봇은 하나" 를 전제로 짜여 있습니다. 넷이 되면
그중 일부만 넷이 되고, **하나여야 할 것이 넷이 되는 순간이 곧 사고** 입니다.

가장 위험한 것은 소유권과 종료입니다. `LiveTrader` 의 두 종료 경로는 모두
`state.close()` 를 무조건 부르므로, 넷이 같은 저장소를 쥐면 먼저 끝난 하나가 —
워밍업에서 죽은 것이라도 — 나머지 셋의 DB 연결과 그룹 전체의 소유권 주장을
닫아 버립니다.
"""
import asyncio
from decimal import Decimal

import pytest

from quant.config.schema import (
    BrokerConfig,
    DataConfig,
    ExecutionConfig,
    ModelSpec,
    PortfolioConfig,
    RiskConfig,
    StrategyConfig,
    SymbolSpec,
    UniverseConfig,
)
from quant.core.types import OrderStatus, RunMode
from quant.live.agents import AgentGroup, AgentSpec
from quant.live.group import GroupTrader
from quant.live.state import AgentStateView


def config(name, cash=50_000, stop=0.10):
    return StrategyConfig(
        name=name,
        mode=RunMode.DRY_RUN,
        data=DataConfig(provider="synthetic", params={"seed": 1},
                        timeframe="1d", warmup_bars=30),
        universe=UniverseConfig(symbols=[SymbolSpec(ticker="AAA", venue="SIM")]),
        alpha=[ModelSpec(type="ema_cross")],
        portfolio=PortfolioConfig(starting_cash=cash, cash_reserve_pct=0.0),
        risk=RiskConfig(models=[ModelSpec(
            type="max_dd_per_security", params={"max_drawdown_pct": stop})]),
        execution=ExecutionConfig(min_order_notional=1.0),
        broker=BrokerConfig(type="paper"),
    )


class Venue:
    """가상 증권사. 계좌 하나의 잔고와 보유만 답합니다."""

    name = "venue"
    portfolio = None
    budget = None
    live = False
    venue_backed = True

    def __init__(self, cash=100_000.0, positions=None):
        self.cash = cash
        self.book = dict(positions or {})
        self.sent = []
        self.connects = 0
        self.closes = 0

    async def submit(self, order):
        self.sent.append(order)
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        order.avg_fill_price = 100.0
        return order

    async def cancel(self, order):
        return True

    async def open_orders(self):
        return []

    async def positions(self):
        return dict(self.book)

    async def balances(self):
        return {"KRW": self.cash}

    async def sync(self):
        return {}

    async def connect(self):
        self.connects += 1

    async def close(self):
        self.closes += 1

    def exact_flatten_order_type(self, symbol, cur, target):
        return None


def two_agents():
    return AgentGroup(agents=(
        AgentSpec(agent_id="attack", label="공격 · 단기",
                  config_path="a.yaml", capital_weight=0.5),
        AgentSpec(agent_id="defend", label="보수 · 장기",
                  config_path="b.yaml", capital_weight=0.5),
    ))


@pytest.fixture
def group(tmp_path):
    venue = Venue()
    gt = GroupTrader(
        two_agents(),
        {"attack": config("attack-strat", stop=0.25),
         "defend": config("defend-strat", stop=0.05)},
        str(tmp_path / "state.db"),
        venue=venue,
    )
    yield gt, venue
    gt.state.close()


# ── 배선 ─────────────────────────────────────────────────────────────────
def test_each_agent_gets_its_own_trader_and_engine(group):
    gt, _ = group
    assert set(gt.traders) == {"attack", "defend"}
    assert gt.traders["attack"].engine is not gt.traders["defend"].engine


def test_each_engine_receives_a_sleeve_not_the_account_adapter(group):
    """세운 뒤 바꿔치기하면 슬리브의 budget/portfolio 가 None 으로 남습니다."""
    gt, venue = group
    for agent_id, trader in gt.traders.items():
        sleeve = trader.engine.brokerage
        assert sleeve.agent_id == agent_id
        assert sleeve is not venue
        assert sleeve.budget is trader.engine.budget
        assert sleeve.portfolio is trader.engine.ctx.portfolio
        assert trader.engine.ctx.brokerage is sleeve


def test_the_configs_must_match_the_group(tmp_path):
    with pytest.raises(ValueError, match="설정 없음"):
        GroupTrader(two_agents(), {"attack": config("a")},
                    str(tmp_path / "s.db"), venue=Venue())


# ── 상태 저장소는 그룹의 것 ──────────────────────────────────────────────
def test_every_trader_shares_one_connection_and_one_claim(group):
    """에이전트마다 StateStore 를 열면 advisory lock 이 open file description
    단위라 같은 프로세스 안에서 자기 자신에게 잠깁니다."""
    gt, _ = group
    for trader in gt.traders.values():
        assert isinstance(trader.state, AgentStateView)
        assert trader.state.conn is gt.state.conn
        assert trader.state._owns is gt.state._owns


def test_a_trader_does_not_own_the_shared_store(group):
    gt, _ = group
    assert all(t._owns_state is False for t in gt.traders.values())
    assert gt.traders["attack"].state.close() is None
    # 닫히지 않았어야 합니다 — 나머지가 아직 매매 중입니다.
    gt.state.conn.execute("SELECT 1").fetchone()


def test_one_agents_shutdown_does_not_close_the_others_database(group):
    """먼저 끝난 하나가 — 워밍업에서 죽은 것이라도 — 나머지 셋의 연결을
    닫으면 안 됩니다."""
    gt, _ = group
    gt.traders["attack"].state.close()

    gt.traders["defend"].state.start_run("defend-strat", "dry_run", 50_000)
    assert gt.traders["defend"].state.run_id is not None


def test_each_agent_writes_to_its_own_run(group):
    gt, _ = group
    a = gt.traders["attack"].state
    b = gt.traders["defend"].state
    a.start_run("same-strategy", "dry_run", 50_000)
    b.start_run("same-strategy", "dry_run", 50_000)

    assert a.run_id != b.run_id


def test_an_accounting_failure_taints_the_whole_group(group):
    """회계 기록을 잃은 것은 DB 하나의 사실입니다."""
    gt, _ = group
    gt.traders["attack"].state.mark_accounting_persistence_failed()

    assert gt.traders["defend"].state.accounting_persistence_failed is True
    assert gt.state.accounting_persistence_failed is True


# ── 계좌 단위 준비는 한 번 ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_start_connects_the_venue_once_and_splits_the_capital(group):
    gt, venue = group
    await gt.start()
    try:
        assert venue.connects == 1, "슬리브마다 연결하면 토큰이 서로를 무효화합니다"
        assert gt.gateway.sleeve_balances("attack") == {"KRW": 50_000.0}
        assert gt.gateway.sleeve_balances("defend") == {"KRW": 50_000.0}
    finally:
        await gt.shutdown(wait=1.0)


@pytest.mark.asyncio
async def test_pre_existing_holdings_are_adopted_before_any_agent_trades(tmp_path):
    """미귀속 채택 전에 불변식을 보면 사용자가 앱에서 직접 산 주식이
    드리프트로 읽혀 그룹이 즉사합니다."""
    venue = Venue(positions={"SIM:AAA": Decimal("40")})
    gt = GroupTrader(two_agents(),
                     {"attack": config("a"), "defend": config("b")},
                     str(tmp_path / "s.db"), venue=venue)
    try:
        await gt.start()
        assert gt.gateway.unassigned_positions() == {"SIM:AAA": Decimal("40")}
        assert gt.gateway.halted is False
    finally:
        await gt.shutdown(wait=1.0)


@pytest.mark.asyncio
async def test_an_unreadable_balance_allocates_zero_not_a_guess(tmp_path):
    """추정치를 넣으면 그 추정으로 진짜 주문이 나갑니다."""
    class Broken(Venue):
        async def balances(self):
            raise RuntimeError("증권사 응답 없음")

    gt = GroupTrader(two_agents(),
                     {"attack": config("a"), "defend": config("b")},
                     str(tmp_path / "s.db"), venue=Broken())
    try:
        await gt.start()
        assert gt.gateway.sleeve_balances("attack") == {"KRW": 0.0}
    finally:
        await gt.shutdown(wait=1.0)


# ── 청산 손익이 계좌 원장에 닿는가 ───────────────────────────────────────
def test_every_engine_is_attached_to_the_account_ledger(group):
    """이것이 없으면 계좌 하루 손실 한도의 실현손익이 영원히 0 입니다."""
    from quant.core.events import Event, EventType
    from quant.live.limits import TradingBudget

    gt, _ = group
    gt.gateway.master_budget = TradingBudget(max_daily_loss=100_000)
    gt.gateway.master_budget.roll(equity=100_000)

    async def emit():
        for trader in gt.traders.values():
            await trader.engine.ctx.bus.emit(Event(
                type=EventType.TRADE_CLOSED, payload={"pnl": -60_000.0}))
    asyncio.run(emit())

    assert gt.gateway.master_budget.today.realized_pnl == pytest.approx(-120_000.0)


# ── 정지와 종료 ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_stop_asks_everyone_first_then_waits_together(group):
    """하나씩 순서대로 기다리면 마지막 에이전트의 여유가 앞사람들의 대기
    시간만큼 줄어듭니다."""
    gt, _ = group
    await gt.start()
    try:
        result = await gt.stop(wait=5.0)

        # 넷을 함께 기다리므로 하나도 남지 않아야 합니다. 순서대로 기다렸다면
        # 마지막 에이전트가 `pending` 에 남습니다.
        assert result["stopping"] is True
        assert result["stopped"] is True, f"멈추지 못한 에이전트: {result['pending']}"
        assert result["pending"] == []
        assert gt.alive is False
    finally:
        await gt.shutdown(wait=1.0)


@pytest.mark.asyncio
async def test_shutdown_releases_the_account_resources_once(group):
    gt, venue = group
    await gt.start()
    await gt.shutdown(wait=2.0)

    assert venue.closes == 1
    assert gt.alive is False
    await gt.shutdown(wait=1.0)          # 두 번 불러도 안전
    assert venue.closes == 1


# ── 상태 보고 ────────────────────────────────────────────────────────────
def test_status_reports_every_agent_separately(group):
    gt, _ = group
    status = gt.status()

    assert [a["agent_id"] for a in status["agents"]] == ["attack", "defend"]
    assert status["agents"][0]["label"] == "공격 · 단기"
    assert status["agents"][0]["strategy"] == "attack-strat"
    assert status["account"]["halted"] is False


def test_status_carries_the_halt_reason_to_the_screen(group):
    gt, _ = group
    gt.gateway.halt("테스트 정지")
    status = gt.status()

    assert status["account"]["halted"] is True
    assert "테스트 정지" in status["account"]["halt_reason"]


def test_a_dead_agents_reason_is_kept(group):
    """화면에 "실행 중 아님" 만 보이면 사용자는 자기가 멈춘 줄 압니다."""
    gt, _ = group

    class Boom:
        @staticmethod
        def cancelled():
            return False

        @staticmethod
        def exception():
            return RuntimeError("시세를 받지 못해 시작하지 못했습니다")

    gt._finished("attack", Boom)
    assert gt.errors["attack"] == "시세를 받지 못해 시작하지 못했습니다"


def test_a_non_korean_error_keeps_its_type_name(group):
    gt, _ = group

    class Boom:
        @staticmethod
        def cancelled():
            return False

        @staticmethod
        def exception():
            return ValueError("bad config")

    gt._finished("defend", Boom)
    assert gt.errors["defend"] == "ValueError: bad config"
