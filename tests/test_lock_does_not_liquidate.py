"""잠금은 신규 진입만 막아야 한다 — 보유를 팔면 안 된다.

`Engine.on_bars` 는 리스크가 노출을 줄이면 그 종목의 인사이트를 지웁니다.
그래야 손절이 판 것을 다음 봉이 곧바로 다시 사지 않습니다. 문제는 비교
대상이었습니다: **제안치** 와 비교하면 `TradingLockGate` 의 "add blocked"
(잠금 중 증액 요청을 현재 수량으로 낮춰 돌려주는 것)가 축소로 읽힙니다.

그러면 인사이트가 지워지고 → 다음 봉 목표가 0 이 되고 → lock_gate 는 청산을
허용하므로 → **멀쩡한 보유가 통째로 팔립니다.** 잠금은 `stoploss_guard` 가
거는 것이고 그 기본값이 전 종목 잠금이라, 출하되는 실거래 설정 전부가 이
경로를 지납니다. 재발신하지 않는 알파(`ema_cross`·`xs_momentum`)에서는 그
청산을 되돌릴 신호도 오지 않습니다.

기준은 **지금 들고 있는 수량** 이어야 합니다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

from quant.alpha.base import AlphaModel
from quant.brokerage.paper import PaperBrokerage
from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.engine import Engine
from quant.core.events import EventBus, EventType
from quant.core.types import UTC, Bar, Direction, Insight, OrderSide, Symbol
from quant.execution.costs import NoSlippage, PercentFeeModel
from quant.execution.models import ImmediateExecution
from quant.portfolio.models import EqualWeighting
from quant.risk.models import TradingLockGate

SYM = Symbol("AAA", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))
T0 = datetime(2024, 1, 1, tzinfo=UTC)


class Once(AlphaModel):
    """한 번만 신호를 내는 알파 — 재발신으로 결함이 가려지지 않게."""

    name = "once"

    def __init__(self):
        self.fired = False

    async def update(self, ctx, bars):
        if self.fired:
            return []
        self.fired = True
        return [Insight(SYM, Direction.UP, ctx.bar_delta * 50, ctx.now,
                        confidence=1.0, source=self.name)]


def run_days(*, lock_on_bar: int | None):
    """가격이 100 → 90 으로 내려 목표 수량이 늘어나는 5봉. 그 순간 잠근다."""
    pf = Portfolio(100_000.0)
    ctx = Context(SimClock(T0), pf, EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    broker = PaperBrokerage(pf, fee_model=PercentFeeModel(taker_bps=0),
                            slippage_model=NoSlippage())
    engine = Engine(
        ctx, Once(),
        EqualWeighting(cash_reserve_pct=0.0, max_position_weight=0.5,
                       max_gross_leverage=0.5, min_trade_weight=0.005),
        ImmediateExecution(min_order_notional=1), broker,
        risk_models=[TradingLockGate()],
    )
    actions: list[dict] = []
    ctx.bus.on(EventType.RISK_ACTION, lambda e: actions.append(e.payload))
    asyncio.run(engine.start())

    sells = []
    for i, px in enumerate([100.0, 100.0, 90.0, 90.0, 90.0]):
        if lock_on_bar is not None and i == lock_on_bar:
            ctx.lock(SYM, T0 + timedelta(days=30), "stoploss_guard: 3 stop-outs")
        bar = Bar(SYM, T0 + timedelta(days=i), px, px, px, px, 1e9, "1d")
        before = len(engine.orders)
        asyncio.run(engine.on_bars({SYM.key: bar}))
        sells.extend(o for o in engine.orders[before:]
                     if o.side is OrderSide.SELL)
    return pf, actions, sells, engine


def test_a_lock_that_blocks_adding_does_not_sell_what_we_hold():
    pf, actions, sells, engine = run_days(lock_on_bar=2)

    assert actions and "add blocked" in actions[-1]["reason"], (
        "픽스처가 'add blocked' 경로를 만들지 못했다")
    assert actions[-1]["insights_cancelled"] is False
    assert sells == [], "잠금이 보유를 팔았다"
    assert pf.quantity(SYM) == Decimal("500")


def test_the_insight_survives_so_the_next_bar_holds():
    """지워졌는지를 직접 봅니다 — 매도가 없다는 것만으로는 우연일 수 있습니다."""
    _pf, _actions, _sells, engine = run_days(lock_on_bar=2)
    assert len(engine.insights) > 0


def test_without_a_lock_nothing_changes():
    """대조군. 이 경로에 손대면서 평상시 동작을 바꾸지 않았는가."""
    pf, _actions, sells, _engine = run_days(lock_on_bar=None)
    assert sells == []
    assert pf.quantity(SYM) > 0


def test_a_real_risk_reduction_still_cancels_the_insight():
    """반대 방향도 지켜야 합니다 — 손절이 판 것을 다음 봉이 다시 사면 안 됩니다.

    보유보다 **작은** 목표는 진짜 축소이므로 인사이트를 지워야 합니다.
    """
    from quant.core.types import PortfolioTarget

    pf = Portfolio(100_000.0)
    ctx = Context(SimClock(T0), pf, EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    pf.position(SYM).quantity = Decimal("500")
    pf.position(SYM).avg_price = 100.0

    class Shrink:
        models: list = []

        def manage(self, _ctx, targets):
            return [PortfolioTarget(t.symbol, Decimal("100"), tag="stop",
                                    source="risk") for t in targets]

    class Want500:
        def create_targets(self, _ctx, _insights):
            return [PortfolioTarget(SYM, Decimal("500"))]

    engine = Engine(ctx, Once(), Want500(), ImmediateExecution(min_order_notional=1),
                    PaperBrokerage(pf, fee_model=PercentFeeModel(taker_bps=0),
                                   slippage_model=NoSlippage()))
    engine.risk = Shrink()
    asyncio.run(engine.start())
    engine.insights.add([Insight(SYM, Direction.UP, ctx.bar_delta * 50, ctx.now,
                                 confidence=1.0, source="once")])

    bar = Bar(SYM, T0 + timedelta(days=1), 100.0, 100.0, 100.0, 100.0, 1e9, "1d")
    asyncio.run(engine.on_bars({SYM.key: bar}))

    assert len(engine.insights) == 0, "진짜 축소인데 인사이트가 남았다"


def test_a_lock_still_lets_an_exit_through():
    """잠금이 청산을 막지 않는다는 기존 계약은 그대로여야 합니다."""
    from quant.core.types import PortfolioTarget

    pf = Portfolio(10_000.0)
    clock = SimClock(T0)
    ctx = Context(clock, pf, EventBus(), timeframe="1d")
    pf.position(SYM).quantity = Decimal("10")
    ctx.lock(SYM, clock.now() + timedelta(days=5), "test lock")

    gate = TradingLockGate()
    assert gate.manage(ctx, [PortfolioTarget(SYM, Decimal("0"))])[0].quantity == 0
    assert gate.manage(ctx, [PortfolioTarget(SYM, Decimal("5"))])[0].quantity == 5
