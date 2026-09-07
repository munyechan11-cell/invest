"""사람이 계좌에 넣거나 뺀 돈은 전략의 성과가 아니다.

실계좌 자본을 진실로 쓰는 모드(토스 실거래)에서는 `Portfolio.cash` 가 매
동기화마다 증권사 값으로 덮입니다. 그런데 자산이 줄면 `drawdown` 이 커지고,
`max_dd_portfolio`(국내 설정 0.18)가 그것을 전략의 손실로 읽습니다. 그
킬스위치는 3초 유지 주기에서 **보유 전체를 시장가로 청산** 하고 열흘을
쉽니다.

즉 사용자가 자기 돈 20% 를 옮기는 순간 봇이 계좌를 비웠습니다. 반대로 입금은
최고점만 끌어올려 이후의 정상적인 등락을 낙폭으로 만들었습니다.

거래로 설명되지 않는 현금 변화는 기준선과 최고점을 **함께** 옮깁니다. 단,
그렇게 읽어도 되는 조건일 때만 — 못 본 체결을 입출금으로 읽으면 킬스위치가
눈을 감기 때문입니다. 그 반대 방향도 여기서 검사합니다.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.brokerage.live_base import LiveBrokerage
from quant.core.account import Portfolio
from quant.core.types import Symbol

SYM = Symbol("005930", venue="toss", quote_currency="KRW")


class Account(LiveBrokerage):
    """계좌가 답하는 현금·보유를 시험이 직접 정하는 실거래 대역."""

    name = "account"
    venue_capital_truth = True

    def __init__(self, book, cash=1_000_000.0, holdings_value=0.0):
        super().__init__(book, live=True, max_order_notional=1e12)
        self.cash = float(cash)
        self.holdings_value = float(holdings_value)
        self.remote = {}

    async def _venue_submit(self, order):
        return "x"

    async def _venue_cancel(self, order):
        return True

    async def _venue_open_orders(self):
        return []

    async def _venue_positions(self):
        return dict(self.remote)

    async def _venue_costs(self):
        return {}

    async def _venue_capital(self):
        return {"currency": "KRW", "cash": self.cash,
                "holdings_value": self.holdings_value}


async def settled_account(**kw) -> Account:
    """첫 채택까지 끝난 계좌. 이후의 변화만 보기 위해서입니다."""
    broker = Account(Portfolio(1_000_000.0, "KRW"), **kw)
    await broker.sync()
    return broker


@pytest.mark.asyncio
async def test_a_withdrawal_does_not_look_like_a_drawdown():
    broker = await settled_account(cash=1_000_000.0)
    pf = broker.portfolio
    assert pf.drawdown == pytest.approx(0.0)

    broker.cash = 800_000.0          # 사용자가 20만원을 뺐다
    await broker.sync()

    assert pf.equity == pytest.approx(800_000.0)
    assert pf.drawdown == pytest.approx(0.0), "출금이 낙폭으로 읽혔다"
    assert pf.total_return == pytest.approx(0.0)
    assert pf.external_flow_total == pytest.approx(-200_000.0)


@pytest.mark.asyncio
async def test_a_withdrawal_does_not_trip_the_portfolio_kill_switch():
    """실제로 청산이 나가는지까지 봅니다 — 지표만 고치면 반쪽입니다."""
    from datetime import datetime

    from quant.core.clock import SimClock
    from quant.core.context import Context
    from quant.core.events import EventBus
    from quant.core.types import UTC, PortfolioTarget
    from quant.risk.models import MaximumDrawdownPortfolio

    broker = await settled_account(cash=1_000_000.0)
    pf = broker.portfolio
    held = pf.position(SYM)
    held.quantity, held.avg_price = Decimal("10"), 70_000.0
    held.mark(70_000.0)

    broker.cash = 800_000.0
    await broker.sync()

    ctx = Context(SimClock(datetime(2026, 9, 1, tzinfo=UTC)), pf, EventBus(),
                  timeframe="1d")
    kill = MaximumDrawdownPortfolio(max_drawdown_pct=0.18)
    out = kill.manage(ctx, [PortfolioTarget(SYM, Decimal("10"))])

    assert out[0].quantity == Decimal("10"), "출금 때문에 전량 청산이 나갔다"


@pytest.mark.asyncio
async def test_a_deposit_does_not_inflate_the_high_water_mark():
    broker = await settled_account(cash=1_000_000.0)
    pf = broker.portfolio

    broker.cash = 1_500_000.0        # 사용자가 50만원을 넣었다
    await broker.sync()

    assert pf.high_water_mark == pytest.approx(1_500_000.0)
    assert pf.drawdown == pytest.approx(0.0)
    assert pf.total_return == pytest.approx(0.0), "넣은 돈이 수익이 되었다"


@pytest.mark.asyncio
async def test_a_real_market_loss_is_still_a_drawdown():
    """반대 방향. 이 수정이 킬스위치를 무디게 만들면 안 됩니다."""
    broker = await settled_account(cash=200_000.0, holdings_value=800_000.0)
    pf = broker.portfolio

    broker.holdings_value = 500_000.0    # 주가가 빠졌다 (현금은 그대로)
    await broker.sync()

    assert pf.equity == pytest.approx(700_000.0)
    assert pf.drawdown == pytest.approx(0.30)
    assert pf.external_flow_total == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_a_cash_change_is_not_called_a_transfer_while_fills_are_unseen():
    """체결 채널이 끊겨 있으면 못 본 체결일 수 있습니다.

    그 손실을 입출금으로 읽으면 낙폭 킬스위치가 눈을 감습니다 — 이 수정이
    만들 수 있는 가장 나쁜 오류라 명시적으로 막습니다.
    """
    broker = await settled_account(cash=1_000_000.0)
    pf = broker.portfolio
    broker.fill_channel_down("체결 조회 실패")

    broker.cash = 800_000.0
    await broker.sync()

    assert pf.external_flow_total == pytest.approx(0.0)
    assert pf.drawdown > 0.15, "설명되지 않는 손실이 낙폭에서 빠졌다"


@pytest.mark.asyncio
async def test_the_first_snapshot_still_sets_the_baseline():
    """첫 채택은 기준선을 세우는 자리입니다 — 입출금으로 세면 안 됩니다."""
    broker = Account(Portfolio(800_000.0, "KRW"), cash=420_000.0)

    await broker.sync()

    assert broker.portfolio.performance_baseline == pytest.approx(420_000.0)
    assert broker.portfolio.external_flow_total == pytest.approx(0.0)
    assert broker.portfolio.total_return == pytest.approx(0.0)
