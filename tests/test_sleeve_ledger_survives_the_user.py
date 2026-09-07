"""원장이 사람의 정상적인 행동을 사고로 읽으면 안 된다.

둘 다 문서가 안내하는 사용 경로에서 나옵니다.

  · **사용자가 자기 주식을 판다.** 미귀속은 정의상 사용자가 자기 뜻대로
    사고파는 물량인데, 그것이 줄어든 것을 "원장이 주장하는 보유가 계좌에
    없다" 로 읽어 그룹을 멈췄습니다. 저장된 미귀속은 그대로 남으므로 다시
    시도해도 같은 결과 — **이후 모든 시작이 영구히 거부** 됐고, 원장을 되돌릴
    화면도 API 도 없었습니다.
  · **에이전트를 잠시 뺀다.** `quant/live/agents.py` 가 직접 안내하는
    방법입니다("쓰지 않을 에이전트는 목록에서 빼세요"). 그런데 그 에이전트의
    보유가 이름을 잃고 미귀속으로 접혔고, 저장에서 그 행이 지워져 **다시 넣어도
    돌아오지 않았습니다.** 장부는 `positions` 에서 복원되므로 화면에는 포지션이
    보이는데 손절은 `min(장부, 원장)` 이 0 을 골라 거절하고, 합계 불변식은 그
    상태를 정상으로 읽습니다.

반대 방향 — **봇이 산 물량** 이 사라지는 것 — 은 여전히 정지 사유입니다.
그것이 불변식이 존재하는 단 하나의 이유입니다.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.core.types import RunMode, Symbol
from quant.live.gateway import AccountGateway, GroupHalted
from quant.live.state import StateStore

from .test_group_starts_with_holdings import group_of

SAMSUNG = Symbol("005930", venue="toss", quote_currency="KRW")
HYNIX = Symbol("000660", venue="toss", quote_currency="KRW")


class Venue:
    """원장만 보는 시험이라 증권사는 받아 주기만 합니다."""

    name, live, venue_backed = "venue", False, True
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


def gateway(*ids, store=None):
    return AccountGateway(group_of(*ids, live=False), Venue(),
                          base_currency="KRW", store=store)


# ── 사용자가 자기 주식을 팔았다 ──────────────────────────────────────────
def test_the_user_selling_their_own_shares_does_not_halt_the_start():
    gw = gateway("a1")
    gw.adopt_unassigned({SAMSUNG.key: Decimal("10")})     # 사용자 물량 10주

    again = gateway("a1")
    again.adopt_sleeves({"__UNASSIGNED__": {SAMSUNG.key: Decimal("10")}})
    again.adopt_unassigned({SAMSUNG.key: Decimal("4")})   # 6주를 팔았다

    assert again.halted is False
    assert again.unassigned_positions() == {SAMSUNG.key: Decimal("4")}


def test_selling_all_of_them_is_also_fine():
    gw = gateway("a1")
    gw.adopt_sleeves({"__UNASSIGNED__": {SAMSUNG.key: Decimal("10")}})

    gw.adopt_unassigned({})

    assert gw.halted is False
    assert gw.unassigned_positions() == {}


def test_the_ledger_no_longer_refuses_every_later_start(tmp_path):
    """저장까지 따라가 봅니다 — 원장이 그대로면 다음 시작도 같은 이유로 막힙니다."""
    store = StateStore(str(tmp_path / "s.db"))
    try:
        first = gateway("a1", store=store)
        first.adopt_unassigned({SAMSUNG.key: Decimal("10")})

        for observed in (Decimal("4"), Decimal("4"), Decimal("4")):
            gw = gateway("a1", store=store)
            gw.adopt_sleeves(store.restore_sleeves())
            gw.adopt_unassigned({SAMSUNG.key: observed})
            assert gw.halted is False

        assert store.restore_sleeves()["__UNASSIGNED__"] == {
            SAMSUNG.key: Decimal("4")}
    finally:
        store.close()


def test_a_missing_bot_holding_still_halts():
    """반대 방향. 봇이 산 물량이 사라지는 것은 여전히 정지 사유입니다."""
    gw = gateway("a1")
    gw.apply_fill("a1", SAMSUNG, Decimal("10"))

    with pytest.raises(GroupHalted, match="계좌에 없습니다"):
        gw.adopt_unassigned({SAMSUNG.key: Decimal("3")})

    assert gw.halted is True


def test_the_user_portion_absorbs_only_what_is_theirs():
    """사용자 몫을 다 깎고도 모자라면 그때부터는 봇 물량입니다."""
    gw = gateway("a1")
    gw.apply_fill("a1", SAMSUNG, Decimal("10"))
    gw.adopt_sleeves({"a1": {SAMSUNG.key: Decimal("10")},
                      "__UNASSIGNED__": {SAMSUNG.key: Decimal("5")}})

    with pytest.raises(GroupHalted):
        gw.adopt_unassigned({SAMSUNG.key: Decimal("8")})   # 15 → 8, 7 부족


# ── 에이전트를 뺐다가 다시 넣는다 ───────────────────────────────────────
def test_a_removed_agents_holding_keeps_its_owner(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    try:
        both = gateway("a1", "a2", store=store)
        both.apply_fill("a2", SAMSUNG, Decimal("5"))

        alone = gateway("a1", store=store)          # a2 를 잠시 뺐다
        alone.adopt_sleeves(store.restore_sleeves())

        assert alone.retired_positions() == {SAMSUNG.key: Decimal("5")}
        assert alone.unassigned_positions() == {}, "이름을 잃고 미귀속이 되었다"
        # 계좌에는 분명히 있으므로 불변식은 그 수량을 알아야 합니다.
        assert alone.expected_venue_positions() == {SAMSUNG.key: Decimal("5")}
        assert alone.check_invariant({SAMSUNG.key: Decimal("5")}) == {}
    finally:
        store.close()


def test_putting_the_agent_back_returns_its_holding(tmp_path):
    store = StateStore(str(tmp_path / "s.db"))
    try:
        both = gateway("a1", "a2", store=store)
        both.apply_fill("a2", SAMSUNG, Decimal("5"))

        alone = gateway("a1", store=store)
        alone.adopt_sleeves(store.restore_sleeves())
        alone.adopt_unassigned({SAMSUNG.key: Decimal("5")})

        back = gateway("a1", "a2", store=store)
        back.adopt_sleeves(store.restore_sleeves())

        assert back.sleeve_positions("a2") == {SAMSUNG.key: Decimal("5")}, (
            "다시 넣었는데 보유가 돌아오지 않았다 — 손절이 '보유는 0' 으로 거절된다")
        assert back.retired_positions() == {}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_returned_holding_can_actually_be_sold(tmp_path):
    """원장에 있는 것만으로는 부족합니다 — 슬리브가 매도를 허용해야 합니다."""
    from quant.brokerage.sleeve import SleeveBrokerage
    from quant.core.account import Portfolio
    from quant.core.types import Order, OrderSide, OrderStatus, OrderType

    store = StateStore(str(tmp_path / "s.db"))
    try:
        both = gateway("a1", "a2", store=store)
        both.apply_fill("a2", SAMSUNG, Decimal("5"))
        alone = gateway("a1", store=store)
        alone.adopt_sleeves(store.restore_sleeves())
        alone.adopt_unassigned({SAMSUNG.key: Decimal("5")})

        back = gateway("a1", "a2", store=store)
        back.adopt_sleeves(store.restore_sleeves())
        sleeve = SleeveBrokerage("a2", back, mode=RunMode.DRY_RUN)
        sleeve.portfolio = Portfolio(10_000.0, "KRW")
        held = sleeve.portfolio.position(SAMSUNG)
        held.quantity, held.avg_price = Decimal("5"), 70_000.0
        held.mark(70_000.0)

        out = await sleeve.submit(
            Order(SAMSUNG, OrderSide.SELL, Decimal("5"), OrderType.MARKET))

        assert out.status is not OrderStatus.REJECTED, out.reject_reason
    finally:
        store.close()


def test_a_retired_agents_holding_is_visible_on_screen():
    """왜 못 파는지 화면이 말할 수 있어야 합니다."""
    gw = gateway("a1")
    gw.adopt_sleeves({"gone": {HYNIX.key: Decimal("3")}})

    assert gw.status()["retired"] == {"gone": {HYNIX.key: "3"}}
