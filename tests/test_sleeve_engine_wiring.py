"""엔진에 슬리브를 끼우는 이음매.

`build_engine` 에 브로커 인자가 없던 동안에는 슬리브를 쓰는 방법이 하나뿐이었습니다
— 엔진을 세운 뒤 `engine.brokerage = sleeve` 로 바꿔치기하는 것. 그 지름길은
조용한 격리 구멍입니다:

  · `Engine.__init__` 이 `brokerage.budget` 과 `brokerage.portfolio` 를 꽂아
    주는데, 나중에 바꾼 슬리브는 둘 다 `None` 인 채로 남습니다. 슬리브의
    `_budget_check` 는 그러면 이 에이전트의 하루 한도를 보지 못하고,
    `clamp_to_sleeve` 는 자기 보유량을 장부에서 읽지 못합니다.
  · `ctx.brokerage` 는 여전히 계좌 어댑터를 가리킵니다. 실행 계층이 거기로
    손을 뻗으면 슬리브의 모든 약속을 우회해 계좌 전체에 직접 주문할 수 있습니다.

그래서 브로커는 **생성자 인자** 여야 합니다. 이 파일이 그 네 갈래 배선이 실제로
이어지는지 확인합니다.
"""
from datetime import datetime
from decimal import Decimal

import pytest

from quant.brokerage.sleeve import SleeveBrokerage
from quant.config.schema import (
    BacktestConfig,
    BrokerConfig,
    CostConfig,
    DataConfig,
    ExecutionConfig,
    ModelSpec,
    PortfolioConfig,
    RiskConfig,
    StrategyConfig,
    SymbolSpec,
    UniverseConfig,
)
from quant.core.account import Portfolio
from quant.core.types import UTC, Order, OrderStatus, RunMode
from quant.strategy.builder import build_engine


class Gateway:
    """계좌 층 대역 — 무엇이 실제로 내려갔는지만."""

    venue_backed = True
    fill_channel_ok = True
    fill_channel_error = ""

    def __init__(self):
        self.submitted: list[tuple[str, Order]] = []

    async def submit_for(self, agent_id, order):
        self.submitted.append((agent_id, order))
        order.status = OrderStatus.NEW
        return order

    async def cancel_for(self, agent_id, order):
        return True

    async def open_orders_for(self, agent_id):
        return []

    async def poll_fills_for(self, agent_id):
        return []

    def drain_pending_fills_for(self, agent_id):
        return []

    def fill_channel_down(self, reason):
        pass

    def fill_channel_up(self):
        pass

    async def sync_for(self, agent_id):
        return {}

    def sleeve_positions(self, agent_id):
        return {}

    def sleeve_balances(self, agent_id):
        return {"KRW": 50_000.0}

    def exact_flatten_order_type_for(self, symbol, cur, target):
        return None


def config(**overrides) -> StrategyConfig:
    cfg = StrategyConfig(
        name="sleeve-wiring",
        mode=RunMode.BACKTEST,
        data=DataConfig(provider="synthetic", params={"seed": 1}, timeframe="1d",
                        warmup_bars=30),
        universe=UniverseConfig(symbols=[SymbolSpec(ticker="AAA", venue="SIM")]),
        alpha=[ModelSpec(type="ema_cross")],
        portfolio=PortfolioConfig(starting_cash=50_000, max_gross_leverage=1.0,
                                  cash_reserve_pct=0.0, min_trade_weight=0.0),
        risk=RiskConfig(),
        execution=ExecutionConfig(min_order_notional=1.0),
        costs=CostConfig(preset="zero_cost"),
        broker=BrokerConfig(type="paper"),
        backtest=BacktestConfig(start=datetime(2024, 1, 1, tzinfo=UTC),
                                end=datetime(2024, 6, 1, tzinfo=UTC)),
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


@pytest.fixture
def wired():
    gateway = Gateway()
    book = Portfolio(starting_cash=50_000, base_currency="KRW")
    sleeve = SleeveBrokerage("attack", gateway, mode=RunMode.DRY_RUN)
    engine, _ = build_engine(config(), portfolio=book, brokerage=sleeve)
    return engine, sleeve, book, gateway


# ── 네 갈래 배선 ─────────────────────────────────────────────────────────
def test_the_engine_uses_the_injected_sleeve(wired):
    engine, sleeve, _, _ = wired
    assert engine.brokerage is sleeve


def test_the_sleeve_receives_this_agents_budget(wired):
    """받지 못하면 `_budget_check` 가 이 에이전트의 하루 한도를 보지 못합니다."""
    engine, sleeve, _, _ = wired
    assert sleeve.budget is engine.budget
    assert sleeve.budget is not None


def test_the_sleeve_receives_this_agents_book(wired):
    """받지 못하면 `clamp_to_sleeve` 가 자기 보유량을 읽지 못하고, 남의 물량까지
    파는 매도를 막을 근거를 잃습니다."""
    _, sleeve, book, _ = wired
    assert sleeve.portfolio is book


def test_the_context_points_at_the_sleeve_not_the_account(wired):
    """`ctx.brokerage` 가 계좌 어댑터를 가리키면 실행 계층이 슬리브를 우회해
    계좌 전체에 직접 주문할 수 있습니다."""
    engine, sleeve, _, _ = wired
    assert engine.ctx.brokerage is sleeve


# ── 주입하지 않으면 이전 그대로 ──────────────────────────────────────────
def test_omitting_the_brokerage_builds_the_configured_one_as_before(wired):
    """단일 봇 경로는 한 글자도 달라지면 안 됩니다."""
    from quant.brokerage.paper import PaperBrokerage

    engine, _ = build_engine(config())
    assert isinstance(engine.brokerage, PaperBrokerage)
    assert engine.brokerage.portfolio is engine.ctx.portfolio


# ── 배선이 실제로 통하는가 ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_an_order_from_the_engine_reaches_the_gateway_with_the_agent_id(wired):
    engine, _, _, gateway = wired
    from quant.core.types import OrderSide, OrderType, Symbol

    sym = Symbol("AAA", venue="SIM", lot_size=Decimal("1"), tick_size=Decimal("0.01"))
    await engine.brokerage.submit(Order(symbol=sym, side=OrderSide.BUY,
                                        quantity=Decimal("5"), type=OrderType.MARKET))

    assert [a for a, _ in gateway.submitted] == ["attack"]


def test_the_sleeve_reports_only_its_allocated_cash(wired):
    _, sleeve, _, _ = wired
    assert sleeve.gateway.sleeve_balances("attack") == {"KRW": 50_000.0}
