"""계좌 게이트웨이 — 계좌에 하나뿐인 것들이 정말 하나인지.

에이전트는 넷이어도 계좌는 하나입니다. 이 파일이 확인하는 것은 그 "하나" 가
넷으로 늘어나지 않는다는 사실입니다:

  · 하루 손실 한도가 봇 수만큼 곱해지지 않는가
  · "005930 20주 중 누구의 10주인가" 를 원장이 정확히 아는가
  · 원장과 증권사가 갈라졌을 때 **그룹 전체가** 멈추는가

마지막이 제일 중요합니다. 부분 정지는 "누가 틀렸는지 안다" 는 뜻인데, 우리는
모릅니다.
"""
from decimal import Decimal

import pytest

from quant.core.types import (
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    RunMode,
    Symbol,
    utcnow,
)
from quant.live.agents import AgentGroup, AgentSpec
from quant.live.gateway import AccountGateway, GroupHalted
from quant.live.limits import TradingBudget

SAMSUNG = Symbol(ticker="005930", venue="toss", quote_currency="KRW",
                 lot_size=Decimal("1"), tick_size=Decimal("100"))
HYNIX = Symbol(ticker="000660", venue="toss", quote_currency="KRW",
               lot_size=Decimal("1"), tick_size=Decimal("500"))


class FakeVenue:
    """증권사 대역. 무엇이 실제로 나갔는지와 계좌가 무엇을 답하는지만."""

    name = "fake"
    portfolio = None
    budget = None

    def __init__(self, positions=None, reject=False):
        self._positions = positions or {}
        self.reject = reject
        self.submitted: list[Order] = []
        self.canceled: list[Order] = []
        self.connects = 0
        self.closes = 0
        self.syncs = 0

    async def submit(self, order):
        self.submitted.append(order)
        if self.reject:
            order.status = OrderStatus.REJECTED
            order.reject_reason = "증권사 거절"
            return order
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        order.avg_fill_price = 70_000.0
        return order

    async def cancel(self, order):
        self.canceled.append(order)
        return True

    async def open_orders(self):
        return list(self.submitted)

    async def positions(self):
        return dict(self._positions)

    async def sync(self):
        self.syncs += 1
        return {"venue": "synced"}

    async def connect(self):
        self.connects += 1

    async def close(self):
        self.closes += 1

    def exact_flatten_order_type(self, symbol, current_quantity, target_quantity):
        return None


def group(*weights, mode=RunMode.DRY_RUN):
    names = ("attack", "defend", "c3", "c4")
    return AgentGroup(agents=tuple(
        AgentSpec(agent_id=names[i], label=f"에이전트 {i}",
                  config_path="configs/kr_toss_desk.yaml",
                  capital_weight=w, mode=mode)
        for i, w in enumerate(weights)
    ))


def gateway(*weights, venue=None, master=None, **kw):
    venue = venue or FakeVenue()
    gw = AccountGateway(group(*(weights or (0.5, 0.5))), venue,
                        master_budget=master, **kw)
    return gw, venue


def order(side=OrderSide.BUY, qty=10, symbol=SAMSUNG):
    return Order(symbol=symbol, side=side, quantity=Decimal(str(qty)),
                 type=OrderType.MARKET)


def fill(order_id, qty=10, side=OrderSide.BUY, symbol=SAMSUNG, fee=0.0):
    return Fill(order_id=order_id, symbol=symbol, side=side,
                quantity=Decimal(str(qty)), price=70_000.0, fee=fee, ts=utcnow())


# ── 자본 배분 ────────────────────────────────────────────────────────────
def test_ten_man_won_becomes_five_and_five():
    gw, _ = gateway(0.5, 0.5)
    assert gw.allocate_capital(100_000) == {"attack": 50_000.0, "defend": 50_000.0}
    assert gw.sleeve_balances("attack") == {"KRW": 50_000.0}


def test_allocation_never_creates_money_that_is_not_there():
    gw, _ = gateway(0.25, 0.25, 0.25, 0.25)
    allocated = sum(gw.allocate_capital(99_999).values())
    assert allocated <= 99_999


# ── 슬리브 원장 ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_fills_are_attributed_to_the_agent_that_ordered():
    """같은 종목을 둘이 사도 누구의 것인지 갈립니다."""
    gw, _ = gateway(0.5, 0.5)
    await gw.submit_for("attack", order(qty=10))
    await gw.submit_for("defend", order(qty=7))

    assert gw.sleeve_positions("attack") == {"toss:005930": Decimal("10")}
    assert gw.sleeve_positions("defend") == {"toss:005930": Decimal("7")}
    assert gw.aggregate_sleeves() == {"toss:005930": Decimal("17")}


@pytest.mark.asyncio
async def test_sell_reduces_only_the_selling_agents_sleeve():
    gw, _ = gateway(0.5, 0.5)
    await gw.submit_for("attack", order(qty=10))
    await gw.submit_for("defend", order(qty=10))
    await gw.submit_for("attack", order(side=OrderSide.SELL, qty=10))

    assert gw.sleeve_positions("attack") == {}
    assert gw.sleeve_positions("defend") == {"toss:005930": Decimal("10")}


def test_settle_routes_delayed_fills_to_the_right_sleeve():
    gw, _ = gateway(0.5, 0.5)
    gw._order_agent["ord_1"] = "attack"
    gw._order_agent["ord_2"] = "defend"

    by_agent = gw.settle([fill("ord_1", 10), fill("ord_2", 4)])

    assert set(by_agent) == {"attack", "defend"}
    assert gw.sleeve_positions("attack") == {"toss:005930": Decimal("10")}
    assert gw.sleeve_positions("defend") == {"toss:005930": Decimal("4")}


def test_fill_from_an_unknown_order_goes_to_unassigned_not_to_someone():
    """아무 에이전트에게나 주면 그 에이전트가 팔 수 있게 됩니다 — 원장에 없는
    물량을 파는 일입니다."""
    gw, _ = gateway(0.5, 0.5)
    gw.settle([fill("ord_from_the_toss_app", 5)])

    assert gw.sleeve_positions("attack") == {}
    assert gw.sleeve_positions("defend") == {}
    assert gw.unassigned_positions() == {"toss:005930": Decimal("5")}


# ── 미귀속 ───────────────────────────────────────────────────────────────
def test_existing_holdings_become_unassigned_so_the_group_can_start():
    """이 단계가 없으면 기존 보유가 있는 계좌는 시작하자마자 멈춥니다."""
    gw, _ = gateway(0.5, 0.5)
    gw.adopt_unassigned({"toss:005930": Decimal("30")})

    assert gw.unassigned_positions() == {"toss:005930": Decimal("30")}
    assert gw.check_invariant({"toss:005930": Decimal("30")}) == {}
    assert gw.halted is False


def test_unassigned_only_covers_what_no_agent_owns():
    gw, _ = gateway(0.5, 0.5)
    gw.apply_fill("attack", SAMSUNG, Decimal("10"))
    gw.adopt_unassigned({"toss:005930": Decimal("30")})

    assert gw.unassigned_positions() == {"toss:005930": Decimal("20")}


# ── 불변식 ───────────────────────────────────────────────────────────────
def test_matching_ledger_and_account_does_not_halt():
    gw, _ = gateway(0.5, 0.5)
    gw.apply_fill("attack", SAMSUNG, Decimal("10"))
    gw.apply_fill("defend", SAMSUNG, Decimal("10"))

    assert gw.check_invariant({"toss:005930": Decimal("20")}) == {}
    assert gw.halted is False


def test_drift_halts_the_whole_group_not_one_agent():
    """어느 슬리브가 틀렸는지 알 방법이 없으므로 부분 정지는 하지 않습니다."""
    gw, _ = gateway(0.5, 0.5)
    gw.apply_fill("attack", SAMSUNG, Decimal("10"))
    gw.apply_fill("defend", SAMSUNG, Decimal("10"))

    drift = gw.check_invariant({"toss:005930": Decimal("15")})

    assert drift["toss:005930"]["차이"] == "-5"
    assert gw.halted is True
    assert "그룹 전체를 멈췄" in gw.halt_reason


def test_halt_message_names_the_symbol_so_the_user_can_check_the_app():
    gw, _ = gateway(0.5, 0.5)
    gw.apply_fill("attack", SAMSUNG, Decimal("10"))
    gw.check_invariant({"toss:005930": Decimal("3")})

    assert "005930" in gw.halt_reason
    assert "증권사 앱" in gw.halt_reason


def test_a_position_that_vanished_entirely_is_drift():
    gw, _ = gateway(0.5, 0.5)
    gw.apply_fill("attack", SAMSUNG, Decimal("10"))
    assert gw.check_invariant({}) != {}
    assert gw.halted is True


def test_a_position_that_appeared_from_nowhere_is_drift():
    gw, _ = gateway(0.5, 0.5)
    assert gw.check_invariant({"toss:000660": Decimal("4")}) != {}
    assert gw.halted is True


@pytest.mark.asyncio
async def test_halted_group_refuses_every_further_order():
    gw, venue = gateway(0.5, 0.5)
    gw.halt("테스트 정지")

    with pytest.raises(GroupHalted):
        await gw.submit_for("attack", order())
    assert venue.submitted == [], "정지 후에도 주문이 증권사로 갔습니다"


def test_halt_does_not_clear_itself_when_the_numbers_happen_to_match_later():
    """다음 동기화에서 숫자가 맞아떨어져도 그 사이에 무슨 일이 있었는지는
    여전히 모릅니다."""
    gw, _ = gateway(0.5, 0.5)
    gw.apply_fill("attack", SAMSUNG, Decimal("10"))
    gw.check_invariant({"toss:005930": Decimal("15")})
    assert gw.halted is True

    gw.apply_fill("attack", SAMSUNG, Decimal("5"))       # 이제 15 로 맞는다
    gw.check_invariant({"toss:005930": Decimal("15")})
    assert gw.halted is True, "정지가 스스로 풀렸습니다"


# ── 계좌 단위 한도 ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_account_cap_binds_before_the_agents_have_each_used_theirs():
    """에이전트 한도를 봇 수만큼 곱한 것이 계좌 한도가 되면 안 됩니다.

    주문 3건짜리 계좌 한도라면, 에이전트 넷이 각자 3건씩 낼 수 있는 것이
    아니라 넷이 합쳐 3건입니다.
    """
    master = TradingBudget(max_daily_orders=3)
    gw, venue = gateway(0.25, 0.25, 0.25, 0.25, master=master)

    accepted = 0
    for agent_id in ("attack", "defend", "c3", "c4"):
        result = await gw.submit_for(agent_id, order(qty=1))
        if result.status is not OrderStatus.REJECTED:
            accepted += 1

    assert accepted == 3
    assert len(venue.submitted) == 3


@pytest.mark.asyncio
async def test_account_cap_rejection_says_which_cap_it_was():
    """에이전트 한도와 계좌 한도는 사용자가 고치는 자리가 다릅니다."""
    master = TradingBudget(max_daily_orders=1)
    gw, _ = gateway(0.5, 0.5, master=master)

    await gw.submit_for("attack", order(qty=1))
    blocked = await gw.submit_for("defend", order(qty=1))

    assert blocked.status is OrderStatus.REJECTED
    assert blocked.reject_reason.startswith("계좌 한도:")


@pytest.mark.asyncio
async def test_account_cap_never_blocks_an_exit():
    """나가는 길을 막는 한도는 안전장치가 아닙니다."""
    master = TradingBudget(max_daily_orders=1)
    gw, venue = gateway(0.5, 0.5, master=master)
    gw.apply_fill("attack", SAMSUNG, Decimal("10"))

    await gw.submit_for("defend", order(qty=1, symbol=HYNIX))   # 한도 소진
    exit_order = await gw.submit_for(
        "attack", order(side=OrderSide.SELL, qty=10))

    assert exit_order.status is not OrderStatus.REJECTED


# ── 주문 소유권 ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_an_agent_cannot_cancel_another_agents_order():
    """취소는 그 에이전트의 의도를 되돌리는 일입니다."""
    gw, venue = gateway(0.5, 0.5)
    theirs = await gw.submit_for("attack", order())

    assert await gw.cancel_for("defend", theirs) is False
    assert venue.canceled == []
    assert await gw.cancel_for("attack", theirs) is True


@pytest.mark.asyncio
async def test_open_orders_are_scoped_by_agent():
    gw, _ = gateway(0.5, 0.5)
    await gw.submit_for("attack", order())
    await gw.submit_for("defend", order())

    assert len(await gw.open_orders_for("attack")) == 1
    assert len(await gw.open_orders_for("defend")) == 1


@pytest.mark.asyncio
async def test_rejected_order_does_not_keep_an_attribution():
    gw, _ = gateway(0.5, 0.5, venue=FakeVenue(reject=True))
    rejected = await gw.submit_for("attack", order())

    assert rejected.status is OrderStatus.REJECTED
    assert gw.attribute(rejected) is None
    assert gw.sleeve_positions("attack") == {}


# ── 연결은 하나 ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_venue_is_connected_once_for_the_whole_group():
    """슬리브마다 연결하면 토큰 재발급이 서로를 무효화합니다."""
    gw, venue = gateway(0.25, 0.25, 0.25, 0.25)
    await gw.connect()
    await gw.connect()
    assert venue.connects == 1

    await gw.close()
    await gw.close()
    assert venue.closes == 1


@pytest.mark.asyncio
async def test_sync_checks_the_invariant_and_reports_the_caller():
    gw, venue = gateway(0.5, 0.5, venue=FakeVenue(
        positions={"toss:005930": Decimal("10")}))
    gw.apply_fill("attack", SAMSUNG, Decimal("10"))

    report = await gw.sync_for("attack")

    assert report["requested_by"] == "attack"
    assert report["sleeve_drift"] == {}
    assert report["halted"] is False
    assert venue.syncs == 1


@pytest.mark.asyncio
async def test_sync_surfaces_drift_and_halts():
    gw, _ = gateway(0.5, 0.5, venue=FakeVenue(
        positions={"toss:005930": Decimal("2")}))
    gw.apply_fill("attack", SAMSUNG, Decimal("10"))

    report = await gw.sync_for("defend")

    assert report["sleeve_drift"] != {}
    assert report["halted"] is True


# ── 상태 ─────────────────────────────────────────────────────────────────
def test_status_shows_who_holds_what():
    gw, _ = gateway(0.5, 0.5)
    gw.allocate_capital(100_000)
    gw.apply_fill("attack", SAMSUNG, Decimal("10"))

    status = gw.status()
    assert status["allocations"] == {"attack": 50_000.0, "defend": 50_000.0}
    assert status["sleeves"]["attack"] == {"toss:005930": "10"}
    assert status["halted"] is False


# ── 체결이 두 번 반영되지 않는다 ─────────────────────────────────────────
#
# 슬리브 원장과 에이전트 장부는 같은 체결로만 움직여야 합니다. 한쪽은 주문
# 응답으로, 다른 쪽은 체결 폴링으로 갱신하면 같은 체결이 두 번 반영되고
# (`Engine.settle_live_fills` 가 경고하는 그 사고), 합계 불변식이 이유 없이
# 깨집니다.
class PollingVenue(FakeVenue):
    """체결을 주문 응답과 별도로 알려 주는 증권사 — 진짜 증권사 쪽 모양."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.pending: list[Fill] = []
        self.polls = 0

    async def submit(self, order):
        submitted = await super().submit(order)
        if submitted.status is not OrderStatus.REJECTED:
            self.pending.append(fill(order.id, order.quantity,
                                     side=order.side, symbol=order.symbol))
        return submitted

    async def poll_fills(self):
        self.polls += 1
        drained, self.pending = self.pending, []
        return drained


@pytest.mark.asyncio
async def test_a_polling_venue_does_not_book_the_same_fill_twice():
    """체결을 따로 알려 주는 증권사에서는 주문 응답의 체결 수량을 무시합니다."""
    gw, _ = gateway(0.5, 0.5, venue=PollingVenue())
    await gw.submit_for("attack", order(qty=10))

    # 아직 폴링 전 — 슬리브는 비어 있어야 합니다.
    assert gw.sleeve_positions("attack") == {}

    await gw.poll_fills_for("attack")
    assert gw.sleeve_positions("attack") == {"toss:005930": Decimal("10")}


@pytest.mark.asyncio
async def test_the_venue_is_polled_once_and_split_between_agents():
    """슬리브마다 훑으면 먼저 부른 하나가 나머지 셋의 체결까지 비워 갑니다."""
    venue = PollingVenue()
    gw, _ = gateway(0.5, 0.5, venue=venue)
    await gw.submit_for("attack", order(qty=10))
    await gw.submit_for("defend", order(qty=4))

    attack_fills = await gw.poll_fills_for("attack")
    defend_fills = await gw.poll_fills_for("defend")

    assert [f.quantity for f in attack_fills] == [Decimal("10")]
    assert [f.quantity for f in defend_fills] == [Decimal("4")]
    assert gw.sleeve_positions("attack") == {"toss:005930": Decimal("10")}
    assert gw.sleeve_positions("defend") == {"toss:005930": Decimal("4")}


@pytest.mark.asyncio
async def test_a_fill_is_handed_to_its_agent_exactly_once():
    """두 번 가져가면 엔진이 같은 체결을 장부에 두 번 적습니다."""
    gw, _ = gateway(0.5, 0.5, venue=PollingVenue())
    await gw.submit_for("attack", order(qty=10))

    assert len(await gw.poll_fills_for("attack")) == 1
    assert await gw.poll_fills_for("attack") == []
    assert gw.sleeve_positions("attack") == {"toss:005930": Decimal("10")}


@pytest.mark.asyncio
async def test_a_synchronous_venue_records_the_fill_from_the_order_response():
    """체결을 따로 알려 주지 않는 증권사(페이퍼)에서는 응답이 체결 통지입니다.

    여기서 옮겨 적지 않으면 슬리브는 영원히 비어 있고, 다음 불변식 검사가
    계좌와의 차이를 드리프트로 읽어 그룹을 멈춥니다.
    """
    gw, _ = gateway(0.5, 0.5)                     # FakeVenue 는 poll_fills 가 없다
    await gw.submit_for("attack", order(qty=10))

    assert gw.sleeve_positions("attack") == {"toss:005930": Decimal("10")}
    assert len(await gw.poll_fills_for("attack")) == 1
