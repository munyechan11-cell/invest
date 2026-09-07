"""3초 유지 주기는 한 단계가 실패해도 살아남아야 한다.

이 주기가 도는 것: 호가 갱신 · 체결 폴링 · **봉 사이 손절** · 수동 주문.
일봉 전략에서 손절을 평가하는 자리는 여기뿐이라, 이 루프가 끝나면 그 봇의
포지션은 다음 재시작까지 아무도 보지 않습니다.

예전에는 수동 주문 단계만 try 로 감싸져 있었습니다. 그래서
 · 토스가 주문 상세를 한 번 못 읽거나(`settle_live_fills` 는 그 실패를
   일부러 다시 던집니다),
 · 계좌 정지 뒤 첫 손절이 `GroupHalted` 를 올리면
그 예외가 `_sleep_serving_manual` 을 지나 `run()` 까지 올라가 `finally` 의
`shutdown()` 을 불렀습니다 — **나가려던 손절은 나가지 않은 채** 봇이 꺼지고,
실거래면 종료가 "안전하지 않음" 으로 기록돼 격리까지 갔습니다.

여기서 검사하는 성질은 둘입니다: 루프가 살아남는가, 그리고 **조용히**
살아남지는 않는가(`status().maintenance_error`).
"""
from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from quant.alpha.base import AlphaModel
from quant.brokerage.live_base import LiveBrokerage
from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.engine import Engine
from quant.core.events import EventBus
from quant.core.types import (
    UTC,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Quote,
    RunMode,
    Symbol,
)
from quant.execution.models import LimitExecution
from quant.live.trader import LiveTrader
from quant.portfolio.models import EqualWeighting
from quant.risk.models import MaximumDrawdownPerSecurity

SYM = Symbol("005930", venue="toss", quote_currency="KRW")
T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


class QuietAlpha(AlphaModel):
    async def update(self, ctx, bars):
        return []


async def _noop(*_a, **_k):
    return None


class FlakyBroker(LiveBrokerage):
    """취소는 되지만 그 직후의 체결 조회가 실패하는 실계좌 대역.

    토스 어댑터가 주문 상세를 못 읽었을 때의 모양 그대로입니다
    (`toss_broker.py` 는 `fill_channel_down` 뒤 `BrokerageError` 를 올립니다).
    """

    def __init__(self, portfolio, *, fail_polls: int = 1):
        super().__init__(portfolio, live=True, max_order_notional=1_000_000_000)
        self.remaining_failures = fail_polls
        self.poll_calls = 0

    async def _venue_submit(self, order):
        return "never"

    async def _venue_cancel(self, order):
        return True

    async def _venue_open_orders(self):
        return []

    async def _venue_positions(self):
        return {}

    async def sync(self, *, expected_positions=None, independent_position_keys=None):
        return {"ok": True}

    async def poll_fills(self):
        self.poll_calls += 1
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise RuntimeError("toss 체결 조회 504")
        return []


def context(cash: float = 1_000.0) -> Context:
    ctx = Context(SimClock(T0), Portfolio(cash, "KRW"), EventBus(),
                  timeframe="1m", run_mode=RunMode.LIVE)
    ctx.universe = [SYM]
    return ctx


def bare_trader(engine, provider) -> LiveTrader:
    """`run()` 이 도는 데 필요한 최소 상태만 세운 트레이더."""
    trader = LiveTrader.__new__(LiveTrader)
    trader.engine = engine
    trader.provider = provider
    trader.calendar = None
    trader.errors = 0
    trader.max_errors = 10
    trader.last_bar_ts = None
    trader.running = True
    trader._stop = None
    trader._last_quote_refresh = time.monotonic()   # 네트워크 호가 조회는 건너뛴다
    trader._quote_failures = {}
    trader._quote_blocked_decision = False
    trader._decision_due_at_next_open = False
    trader._next_fill_poll_at = 0.0
    trader._fill_poll_backoff_s = trader.FILL_POLL_S
    trader._seen = {}
    trader.maintenance_error = ""
    trader._maintenance_failures = 0
    trader.config = SimpleNamespace(data=SimpleNamespace(timeframe="1m"))
    trader.notifier = SimpleNamespace(send=_noop)
    return trader


def losing_position_setup(broker_factory):
    """-20% 인 보유 하나와 그 종목에 걸린 미체결 매수. 손절이 나가야 하는 상태."""
    ctx = context()
    pos = ctx.portfolio.position(SYM)
    pos.quantity = Decimal("10")
    pos.avg_price = 100.0
    pos.mark(80.0)
    ctx.set_quote(Quote(SYM, T0, bid=79.0, ask=81.0))
    broker = broker_factory(ctx.portfolio)
    pending_buy = Order(SYM, OrderSide.BUY, Decimal("1"), OrderType.LIMIT,
                        limit_price=79.0, status=OrderStatus.SUBMITTED)
    broker._orders[pending_buy.id] = pending_buy
    engine = Engine(ctx, QuietAlpha(), EqualWeighting(),
                    LimitExecution(offset_bps=10, urgent_after_bars=2), broker,
                    risk_models=[
                        MaximumDrawdownPerSecurity(max_drawdown_pct=0.10)])
    submitted: list[Order] = []

    async def submit(orders):
        submitted.extend(orders)

    engine._submit = submit
    return ctx, broker, engine, submitted, pending_buy


def flaky(fail_polls: int):
    return lambda pf: FlakyBroker(pf, fail_polls=fail_polls)


@pytest.mark.asyncio
async def test_a_failing_fill_read_in_the_stop_path_does_not_end_the_loop():
    """손절 경로가 취소 뒤 부르는 `settle_live_fills` 는 실패를 다시 던집니다.

    그 예외가 `_sleep_serving_manual` → `run()` 까지 올라가면 봇이 꺼졌습니다.
    """
    ctx, broker, engine, submitted, _buy = losing_position_setup(flaky(99))
    trader = bare_trader(engine, SimpleNamespace())

    # run() 이 봉 사이에 도는 바로 그 호출. 예외가 밖으로 나오면 봇이 꺼진다.
    await trader._sleep_serving_manual(0.01)

    assert trader.running is True
    assert ctx.portfolio.quantity(SYM) == Decimal("10")


@pytest.mark.asyncio
async def test_the_failure_is_reported_rather_than_swallowed():
    """조용히 살아남는 것은 고친 것이 아닙니다 — 이유가 화면까지 가야 합니다."""
    ctx, broker, engine, _submitted, _buy = losing_position_setup(flaky(99))
    errors: list[dict] = []
    ctx.bus.on(None, lambda e: errors.append(e.payload)
               if e.type.value == "error" else None)
    trader = bare_trader(engine, SimpleNamespace())

    await trader._sleep_serving_manual(0.01)

    assert "504" in trader.maintenance_error
    assert "손절" in trader.maintenance_error, "어느 단계가 죽었는지 말해야 한다"
    assert trader._maintenance_failures >= 1
    assert any("504" in str(payload.get("error", "")) for payload in errors)


@pytest.mark.asyncio
async def test_the_next_cycle_recovers_and_the_stop_goes_out():
    """일시적 실패가 지나면 손절이 실제로 나가야 합니다.

    "루프가 안 죽는다" 만으로는 부족합니다 — 다음 주기가 제 일을 해야
    포지션이 관리됩니다.
    """
    ctx, broker, engine, submitted, _buy = losing_position_setup(flaky(99))
    trader = bare_trader(engine, SimpleNamespace())

    await trader._sleep_serving_manual(0.01)      # 1) 조회가 죽은 주기
    assert trader.maintenance_error
    assert submitted == []

    broker.remaining_failures = 0                 # 2) 증권사가 돌아왔다
    await trader._maintenance_cycle()

    assert trader.maintenance_error == "", "회복하면 사유가 지워져야 한다"
    assert trader._maintenance_failures == 0
    assert submitted, "손절이 나가지 않았다"
    assert all(o.side is OrderSide.SELL for o in submitted)


@pytest.mark.asyncio
async def test_a_halted_group_rejects_the_exit_instead_of_killing_the_agent():
    """정지된 계좌의 거절은 **거절** 이어야지 에이전트를 끝내면 안 됩니다."""
    from quant.brokerage.sleeve import SleeveBrokerage
    from quant.live.agents import AgentGroup, AgentSpec
    from quant.live.gateway import AccountGateway

    class Venue:
        name, live, venue_backed = "v", False, True
        portfolio = budget = None

        async def submit(self, order):
            return order

        async def cancel(self, order):
            return True

        async def open_orders(self):
            return []

        async def positions(self):
            return {}

        async def balances(self):
            return {"KRW": 100_000.0}

    group = AgentGroup(agents=(
        AgentSpec(agent_id="a1", label="하나", config_path="c.yaml",
                  capital_weight=1.0, mode=RunMode.DRY_RUN),))
    gw = AccountGateway(group, Venue(), base_currency="KRW")
    gw.apply_fill("a1", SYM, Decimal("10"))
    gw.halt("슬리브 원장과 증권사 보유가 다릅니다")

    sleeve = SleeveBrokerage("a1", gw, mode=RunMode.DRY_RUN)
    sleeve.portfolio = Portfolio(1_000.0, "KRW")
    held = sleeve.portfolio.position(SYM)
    held.quantity, held.avg_price = Decimal("10"), 100.0
    exit_order = Order(SYM, OrderSide.SELL, Decimal("10"), OrderType.MARKET)

    out = await sleeve.submit(exit_order)

    assert out.status is OrderStatus.REJECTED
    assert "슬리브 원장" in (out.reject_reason or "")


@pytest.mark.asyncio
async def test_a_quote_outage_still_lets_the_stop_run():
    """호가 갱신이 실패해도 뒤 단계(손절)는 돌아야 합니다 — 순서가 이유입니다."""
    ctx, broker, engine, submitted, _buy = losing_position_setup(
        lambda pf: FlakyBroker(pf, fail_polls=0))
    trader = bare_trader(engine, SimpleNamespace())
    trader._last_quote_refresh = float("-inf")    # 호가 갱신 단계로 들어가게

    async def broken_quotes(*_a, **_k):
        raise RuntimeError("호가 API 500")

    trader._refresh_quotes = broken_quotes

    await trader._maintenance_cycle()

    assert "호가" in trader.maintenance_error
    assert submitted, "호가 실패가 손절까지 막았다"
