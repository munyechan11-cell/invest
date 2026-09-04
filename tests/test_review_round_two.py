"""2차 검토가 찾은 것들 — 재현 → 수정 → 못 박기.

  · 실제 어댑터(KIS·토스)에서는 그룹이 **아예 켜지지 않았다** — 잔고를
    `balances()` 로만 읽었는데 그쪽은 기본 구현 `{}` 였다
  · 시장가 주문이 주문 낸 에이전트가 아니라 먼저 붙은 형제의 묵은 시세로
    가격 매겨졌다
  · 에이전트 하나가 멈출 때 계좌 전체의 미결을 세어 형제의 정상 주문이
    "불안전 종료" 가 됐다
  · 그룹 정지가 취소된 에이전트의 정리가 끝나기 전에 계좌 연결과 DB를 닫았다
"""
from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from quant.brokerage.sleeve import SleeveBrokerage
from quant.core.account import Portfolio
from quant.core.types import Order, OrderSide, OrderStatus, OrderType
from quant.live.gateway import AccountGateway
from quant.live.group import GroupTrader

sys.path.insert(0, str(Path(__file__).parent))

from test_final_sweep_fixes import SAMSUNG, RealAdapter, adapter, group, market  # noqa: E402
from test_group_trader import config, two_agents  # noqa: E402
from test_registry_groups import Venue  # noqa: E402

OPEN = next(status for status in OrderStatus if status.is_open)


# ── ① 계좌 현금은 어댑터 계약으로 읽는다 ────────────────────────────────
@pytest.mark.asyncio
async def test_account_cash_comes_from_the_live_adapter_contract():
    """`balances()` 는 페이퍼의 계약입니다. 실제 어댑터의 현금은 `_venue_cash`
    또는 계좌 진실 어댑터의 `_venue_capital()['cash']` 에 있습니다."""
    class Cashy(RealAdapter):
        async def _venue_cash(self):
            return 1_234_000.0

    class Truth(RealAdapter):
        venue_capital_truth = True

        async def _venue_capital(self):
            return {"cash": 500_000.0, "holdings_value": 100_000.0, "currency": "KRW"}

    assert await AccountGateway(group("a"), adapter(cls=Cashy),
                                base_currency="KRW").read_account_cash() == 1_234_000.0
    assert await AccountGateway(group("a"), adapter(cls=Truth),
                                base_currency="KRW").read_account_cash() == 500_000.0
    # 아무 계약도 값을 주지 않으면 0 — 추정치를 지어내지 않습니다.
    assert await AccountGateway(group("a"), adapter(),
                                base_currency="KRW").read_account_cash() == 0.0
    # 페이퍼·가상 증권사는 예전처럼 balances() 로.
    assert await AccountGateway(group("a"), Venue(),
                                base_currency="KRW").read_account_cash() == 100_000.0


@pytest.mark.asyncio
async def test_a_wrong_account_currency_refuses_instead_of_allocating():
    from quant.live.gateway import GroupHalted

    class Usd(RealAdapter):
        venue_capital_truth = True

        async def _venue_capital(self):
            return {"cash": 1000.0, "holdings_value": 0.0, "currency": "USD"}

    with pytest.raises(GroupHalted, match="USD"):
        await AccountGateway(group("a"), adapter(cls=Usd),
                             base_currency="KRW").read_account_cash()


@pytest.mark.asyncio
async def test_a_group_starts_on_a_venue_whose_balances_are_empty(tmp_path):
    """실제 어댑터 모양 — `balances()` 는 `{}`, 현금은 `_venue_cash()` 에."""
    class LiveLike(Venue):
        async def balances(self):
            return {}

        async def _venue_cash(self):
            return 100_000.0

    gt = GroupTrader(two_agents(), {"attack": config("a"), "defend": config("b")},
                     str(tmp_path / "s.db"), venue=LiveLike())
    try:
        status = await gt.start()
        assert status["running"] is True
        assert status["account"]["account_equity"] == 100_000.0
        books = {a: t.engine.ctx.portfolio.cash for a, t in gt.traders.items()}
        assert sum(books.values()) == pytest.approx(100_000.0, abs=1.0)
    finally:
        await gt.shutdown(wait=1.0)


# ── ② 시장가 주문은 주문 낸 에이전트의 시세로 ────────────────────────────
@pytest.mark.asyncio
async def test_market_orders_are_priced_with_the_submitting_agents_own_mark():
    venue = adapter()
    gw = AccountGateway(group("slow", "fast"), venue, base_currency="KRW")
    slow, fast = Portfolio(500_000, "KRW"), Portfolio(500_000, "KRW")
    slow.mark(SAMSUNG, 50_000.0)     # 일봉 — 어제 종가
    fast.mark(SAMSUNG, 60_000.0)     # 분봉 — 방금
    gw._agent_books["slow"] = slow
    gw._agent_books["fast"] = fast

    placed = await gw.submit_for("fast", market(qty=1))
    assert placed.status is not OrderStatus.REJECTED, placed.reject_reason
    assert venue.portfolio.position(SAMSUNG).last_price == 60_000.0

    # 수량을 바꿉니다 — 어댑터가 10초 안의 동일 주문을 중복으로 막습니다.
    placed = await gw.submit_for("slow", market(qty=2))
    assert placed.status is not OrderStatus.REJECTED, placed.reject_reason
    assert venue.portfolio.position(SAMSUNG).last_price == 50_000.0

    # 자기 장부에 값이 없을 때만 형제의 것으로 물러선다.
    assert gw._last_price(SAMSUNG, "nobody") == 50_000.0


# ── ③ 종료 확인은 내 몫의 미결만 ────────────────────────────────────────
def _resting(broker_id: str) -> Order:
    order = Order(symbol=SAMSUNG, side=OrderSide.BUY, quantity=Decimal("1"),
                  type=OrderType.LIMIT, limit_price=70_000.0)
    order.status = OPEN
    order.broker_id = broker_id
    return order


def test_shutdown_count_ignores_siblings_known_resting_orders():
    class Counting(RealAdapter):
        remote = 2

        async def shutdown_remote_open_order_count(self):
            return self.remote

    venue = adapter(cls=Counting)
    gw = AccountGateway(group("a1", "a2"), venue, base_currency="KRW")
    sibling = _resting("v-1")
    venue._orders[sibling.id] = sibling
    gw._remember_order(sibling.id, "a2", SAMSUNG.key)

    mine = SleeveBrokerage("a1", gw)
    theirs = SleeveBrokerage("a2", gw)
    # 증권사에 2건 — 형제 것 1건을 빼면 내 것(또는 주인 모를 것) 1건.
    assert asyncio.run(mine.shutdown_remote_open_order_count()) == 1
    # 형제 입장에서는 자기 것을 빼지 않는다.
    assert asyncio.run(theirs.shutdown_remote_open_order_count()) == 2
    # 주인을 모르는 로컬 주문은 빼지 않는다 — 확인 못 한 것은 남은 것.
    unknown = _resting("v-2")
    venue._orders[unknown.id] = unknown
    assert asyncio.run(mine.shutdown_remote_open_order_count()) == 1
    # 형제의 주문이 닫히면 계좌 전체를 본다.
    sibling.status = OrderStatus.CANCELED
    assert asyncio.run(mine.shutdown_remote_open_order_count()) == 2
    # 증권사가 0 이면 0.
    venue.remote = 0
    assert asyncio.run(mine.shutdown_remote_open_order_count()) == 0


# ── ④ 취소된 에이전트의 정리가 끝난 뒤에 계좌를 놓는다 ──────────────────
@pytest.mark.asyncio
async def test_group_shutdown_waits_for_cancelled_agents_before_closing(tmp_path, monkeypatch):
    from quant.live import trader as trader_module

    venue = Venue()
    seen: dict[str, int] = {}

    async def slow_flush(self):
        # `Engine.stop` 흉내 — 증권사 취소·체결 수거에 시간이 걸린다.
        await asyncio.sleep(0.6)
        seen[self.strategy_name if hasattr(self, "strategy_name") else id(self)] = venue.closes

    monkeypatch.setattr(trader_module.LiveTrader, "shutdown", slow_flush)
    gt = GroupTrader(two_agents(), {"attack": config("a"), "defend": config("b")},
                     str(tmp_path / "s.db"), venue=venue)
    await gt.start()
    await asyncio.sleep(0.2)
    await gt.shutdown(wait=0.0)      # 기다리지 않고 바로 취소 경로로

    assert len(seen) == 2, "두 에이전트 모두 정리를 끝내야 합니다"
    assert all(closes == 0 for closes in seen.values()), (
        "정리가 끝나기 전에 계좌 연결이 닫혔습니다")
    assert venue.closes == 1
    assert gt.alive is False
