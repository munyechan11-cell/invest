"""슬리브가 안전장치를 끄고 있지 않은지.

`LiveTrader` 의 `isinstance(brokerage, LiveBrokerage)` 검사는 두 가지를 한꺼번에
결정하고 있었습니다:

    (가) 계좌 자본을 자기 것으로 채택해도 되는가  — 슬리브는 **아니오**
    (나) 주문이 진짜 증권사로 나가는가            — 슬리브도 **예**

봉 사이 안전 작업은 전부 (나)에 매달려 있습니다. 호가 갱신, 손절·낙폭 재평가,
체결 폴링, 제출 직전 관문. 슬리브가 `LiveBrokerage` 가 아니라는 이유로 이것들이
같이 꺼지면 **일봉 전략의 손절은 하루에 한 번만 평가되고**, 봉 사이 체결은 영영
장부에 오르지 않으며, 주문은 마지막 관문 없이 나갑니다 — 예외도 로그도 없이.

성향이 다른 에이전트를 굴리겠다면서 손절이 하루 한 번만 돈다면 그 기능은 있으나
마나입니다. 그래서 (나)를 `venue_backed` 라는 별도 능력 플래그로 갈랐고, 이
파일이 그 분리가 실제로 유지되는지 확인합니다.
"""
import inspect
from decimal import Decimal

import pytest

from quant.brokerage.base import Brokerage
from quant.brokerage.live_base import LiveBrokerage
from quant.brokerage.paper import PaperBrokerage
from quant.brokerage.sleeve import SleeveBrokerage
from quant.core.account import Portfolio
from quant.core.types import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    RunMode,
    Symbol,
)

SAMSUNG = Symbol("005930", venue="toss", quote_currency="KRW",
                 lot_size=Decimal("1"), tick_size=Decimal("100"))


class Gateway:
    """계좌 층 대역. 등에 업은 증권사가 진짜인지만 답합니다."""

    def __init__(self, venue_backed=True):
        self.venue_backed = venue_backed
        self.fill_channel_ok = True
        self.fill_channel_error = ""
        self.down_reasons = []
        self.ups = 0
        self.drained = []
        self.submitted = []

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
        self.drained.append(agent_id)
        return []

    def fill_channel_down(self, reason):
        self.down_reasons.append(reason)
        self.fill_channel_ok = False

    def fill_channel_up(self):
        self.ups += 1
        self.fill_channel_ok = True

    async def sync_for(self, agent_id):
        return {}

    def sleeve_positions(self, agent_id):
        return {}

    def sleeve_balances(self, agent_id):
        return {}

    def exact_flatten_order_type_for(self, symbol, cur, target):
        return None


def sleeve(venue_backed=True, agent_id="attack"):
    gw = Gateway(venue_backed=venue_backed)
    broker = SleeveBrokerage(agent_id, gw, mode=RunMode.LIVE)
    broker.portfolio = Portfolio(starting_cash=50_000, base_currency="KRW")
    return broker, gw


def order(side=OrderSide.BUY, qty=1):
    return Order(symbol=SAMSUNG, side=side, quantity=Decimal(str(qty)),
                 type=OrderType.MARKET)


# ── 능력 플래그가 두 질문을 실제로 가르는가 ──────────────────────────────
def test_a_simulated_brokerage_is_not_venue_backed():
    """페이퍼는 자기가 받은 봉 안에서 체결을 끝냅니다 — 봉 사이에 할 일이 없습니다."""
    assert Brokerage.venue_backed is False
    paper = PaperBrokerage(Portfolio(10_000))
    assert paper.venue_backed is False


def test_every_venue_adapter_is_venue_backed():
    assert LiveBrokerage.venue_backed is True


def test_a_sleeve_over_a_real_venue_is_venue_backed_but_never_adopts_capital():
    """이 두 줄이 이 파일의 요지입니다 — 두 질문의 답이 서로 다릅니다."""
    broker, _ = sleeve(venue_backed=True)
    assert broker.venue_backed is True, "손절이 하루 한 번만 돌게 됩니다"
    assert broker.uses_venue_capital is False, "같은 현금이 네 번 계산됩니다"
    assert broker.venue_capital_truth is False


def test_a_sleeve_over_a_simulated_venue_is_not_venue_backed():
    broker, _ = sleeve(venue_backed=False)
    assert broker.venue_backed is False


# ── LiveTrader 가 안전 분기에서 무엇을 보는가 ────────────────────────────
def _source(name):
    from quant.live import trader
    return inspect.getsource(getattr(trader.LiveTrader, name))


@pytest.mark.parametrize("method", [
    "_bind_submission_guard",     # 제출 직전 관문
    "_poll_live_fills",           # 봉 사이 체결
    "_maintenance_cycle",         # 호가 갱신 + 손절 재평가
])
def test_between_bar_safety_keys_off_the_capability_not_the_class(method):
    """이 검사가 `isinstance(LiveBrokerage)` 로 되돌아가면 슬리브에서 조용히 꺼집니다."""
    src = _source(method)
    assert "venue_backed" in src, f"{method} 가 능력 플래그를 보지 않습니다"
    assert "LiveBrokerage" not in src, (
        f"{method} 가 다시 클래스를 검사합니다 — 슬리브에서 이 안전장치가 꺼집니다"
    )


def test_account_capital_adoption_still_keys_off_the_class():
    """반대 방향도 지켜야 합니다. 슬리브가 계좌 자본을 채택하면 안 됩니다."""
    src = _source("start")
    assert "isinstance(self.engine.brokerage, LiveBrokerage)" in src
    assert "expect_restored_venue_truth" in src


# ── 제출 직전 관문 ───────────────────────────────────────────────────────
def test_the_trader_can_install_its_guard_on_a_sleeve():
    """슬리브에 `set_submission_guard` 가 없으면 `_bind_submission_guard` 는
    아무것도 하지 않고 지나가고, 그 사실은 어디에도 남지 않습니다."""
    broker, _ = sleeve()
    assert hasattr(broker, "set_submission_guard")
    broker.set_submission_guard(lambda o: "")
    assert broker._submission_guard_error(order()) == ""


@pytest.mark.asyncio
async def test_a_guarded_sleeve_refuses_to_send_when_the_guard_objects():
    broker, gw = sleeve()
    broker.set_submission_guard(lambda o: "장이 닫혀 있습니다")

    result = await broker.submit(order())

    assert result.status is OrderStatus.REJECTED
    assert result.reject_reason == "장이 닫혀 있습니다"
    assert gw.submitted == [], "관문이 막은 주문이 증권사로 갔습니다"


@pytest.mark.asyncio
async def test_a_guard_that_raises_fails_closed():
    """불확실하면 막는 쪽으로 닫습니다 — 관문이 고장 났다고 주문을 통과시키면
    그 관문은 있으나 마나입니다."""
    def broken(o):
        raise RuntimeError("호가를 읽지 못했습니다")

    broker, gw = sleeve()
    broker.set_submission_guard(broken)

    result = await broker.submit(order())

    assert result.status is OrderStatus.REJECTED
    assert "확인하지 못했습니다" in result.reject_reason
    assert gw.submitted == []


@pytest.mark.asyncio
async def test_an_unguarded_sleeve_still_sends():
    """관문을 걸지 않은 dry_run 경로가 막히면 안 됩니다."""
    broker, gw = sleeve()
    result = await broker.submit(order())
    assert result.status is not OrderStatus.REJECTED
    assert len(gw.submitted) == 1


# ── 체결 채널 ────────────────────────────────────────────────────────────
def test_the_fill_channel_is_one_account_wide_fact():
    """채널이 죽으면 네 에이전트가 다 눈을 감은 것입니다."""
    gw = Gateway()
    a = SleeveBrokerage("attack", gw, mode=RunMode.LIVE)
    b = SleeveBrokerage("defend", gw, mode=RunMode.LIVE)

    a.fill_channel_down("체결 조회 실패")

    assert a.fill_channel_ok is False
    assert b.fill_channel_ok is False, "한 에이전트만 눈을 감았습니다"
    assert "attack" in gw.down_reasons[0], "어느 에이전트가 신고했는지 남지 않습니다"

    b.fill_channel_up()
    assert a.fill_channel_ok is True


def test_pending_fills_are_drained_per_agent():
    """슬리브마다 증권사를 비우면 먼저 부른 하나가 나머지 몫까지 가져갑니다."""
    broker, gw = sleeve()
    assert broker.drain_pending_fills() == []
    assert gw.drained == ["attack"]
