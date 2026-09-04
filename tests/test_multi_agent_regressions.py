"""적대적 검토가 찾아낸 결함들 — 다시 들어오지 못하게 못을 박는다.

여기 있는 여섯 개는 전부 **실제로 재현했던** 결함입니다. 설계 문서의 걱정이
아니라, 코드를 돌려서 잘못된 숫자를 눈으로 본 것들입니다. 하나하나가 조용히
동작하는 종류라 — 예외도 로그도 없이 그냥 틀린 답을 내놓습니다 — 테스트가
없으면 다음 리팩터링에서 그대로 돌아옵니다.

  · 계좌 하루 손실 한도가 영원히 걸리지 않았다 (실현손익이 언제나 0)
  · 계좌 한도가 봇 수만큼 곱해졌다 (확인과 기록 사이에 네트워크 왕복이 있었다)
  · 공매도를 켜면 클램프가 통째로 꺼졌다
  · 장부에 계좌 합계가 들어오면 클램프가 무력해졌다
  · 시작 시 미귀속 채택이 도난을 지웠다
  · 에이전트 id 가 개행을 통과시켰다
"""
import asyncio
from decimal import Decimal

import pytest

from quant.brokerage.base import BrokerageError
from quant.brokerage.sleeve import SleeveBrokerage
from quant.core.account import Portfolio
from quant.core.events import Event, EventType
from quant.core.types import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    RunMode,
    Symbol,
)
from quant.live.agents import AgentConfigError, AgentGroup, AgentSpec
from quant.live.gateway import AccountGateway, GroupHalted
from quant.live.limits import TradingBudget

SAMSUNG = Symbol("005930", venue="toss", quote_currency="KRW",
                 lot_size=Decimal("1"), tick_size=Decimal("100"))


class SlowVenue:
    """주문 하나에 이벤트 루프를 한 번 양보하는 증권사.

    그 한 번이 전부입니다 — 확인과 기록 사이에 `await` 가 있으면 다른
    에이전트가 그 틈으로 들어옵니다.
    """

    name = "slow"
    portfolio = None
    budget = None

    def __init__(self):
        self.sent: list[Order] = []

    async def submit(self, order):
        self.sent.append(order)
        await asyncio.sleep(0)
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        order.avg_fill_price = 1_000.0
        return order

    async def cancel(self, order):
        return True

    async def open_orders(self):
        return []

    async def positions(self):
        return {}

    async def sync(self):
        return {}

    async def connect(self):
        return None

    async def close(self):
        return None

    def exact_flatten_order_type(self, symbol, cur, target):
        return None


def group(*weights):
    names = ("a1", "a2", "a3", "a4")
    return AgentGroup(agents=tuple(
        AgentSpec(agent_id=names[i], label=f"에이전트 {i}",
                  config_path="c.yaml", capital_weight=w)
        for i, w in enumerate(weights)))


def order(side=OrderSide.BUY, qty=1):
    return Order(symbol=SAMSUNG, side=side, quantity=Decimal(str(qty)),
                 type=OrderType.MARKET)


# ── ① 계좌 손실 한도가 실제로 걸리는가 ───────────────────────────────────
def test_the_account_loss_cap_can_actually_fire():
    """`TradingBudget` 의 손실 게이트는 `ledger.realized_pnl` 만 봅니다.

    그 값을 쓰는 곳은 `record_trade` 하나뿐이고, 그것을 부르는 곳은
    `Engine._book_fills` 하나뿐이며, 거기서 불리는 것은 **에이전트의** 예산입니다.
    계좌 예산까지 잇지 않으면 실현손익이 영원히 0 이라 손실 한도는 하루 종일
    초록색입니다.
    """
    gw = AccountGateway(group(0.5, 0.5), SlowVenue(), base_currency="KRW",
                        master_budget=TradingBudget(max_daily_loss=200_000))
    gw.master_budget.roll(equity=1_000_000)

    gw.record_closed_trade("a1", -150_000)
    gw.record_closed_trade("a2", -150_000)

    allowed, reason = gw._master_check(order())
    assert allowed is False, "계좌가 30만원을 잃었는데 20만원 한도가 통과시켰습니다"
    assert "손실" in reason


def test_each_agent_staying_under_its_own_cap_does_not_save_the_account():
    """넷이 각자 자기 한도 안에 있어도 계좌는 넘을 수 있습니다 — 그것이
    계좌 한도가 따로 있는 이유 전부입니다."""
    gw = AccountGateway(group(0.25, 0.25, 0.25, 0.25), SlowVenue(),
                        base_currency="KRW",
                        master_budget=TradingBudget(max_daily_loss=200_000))
    gw.master_budget.roll(equity=1_000_000)

    for agent_id in ("a1", "a2", "a3", "a4"):
        gw.record_closed_trade(agent_id, -90_000)   # 각자 10만원 한도 아래

    allowed, _ = gw._master_check(order())
    assert allowed is False


@pytest.mark.asyncio
async def test_a_closed_trade_event_reaches_the_account_ledger():
    """엔진의 버스를 통해 실제로 이어지는지 — 메서드가 있는 것과 불리는 것은
    다른 문제입니다."""
    gw = AccountGateway(group(0.5, 0.5), SlowVenue(), base_currency="KRW",
                        master_budget=TradingBudget(max_daily_loss=100_000))
    gw.master_budget.roll(equity=1_000_000)

    class FakeEngine:
        class ctx:
            from quant.core.events import EventBus
            bus = EventBus()

    gw.attach_engine("a1", FakeEngine)
    await FakeEngine.ctx.bus.emit(Event(type=EventType.TRADE_CLOSED,
                                        payload={"pnl": -120_000.0}))

    allowed, _ = gw._master_check(order())
    assert allowed is False, "청산 이벤트가 계좌 원장에 닿지 않았습니다"


# ── ② 계좌 한도가 봇 수만큼 곱해지지 않는가 ──────────────────────────────
@pytest.mark.asyncio
async def test_the_account_cap_is_not_multiplied_by_concurrent_agents():
    """`check` 는 통과시켜도 아무것도 기록하지 않고, 소모는 `record_order` 가
    합니다. 그 사이에 `await venue.submit(...)` 이 있으면 네트워크 왕복 내내
    창이 열려 있고 네 에이전트가 동시에 들어갑니다."""
    venue = SlowVenue()
    gw = AccountGateway(group(0.25, 0.25, 0.25, 0.25), venue, base_currency="KRW",
                        master_budget=TradingBudget(max_daily_orders=1))

    results = await asyncio.gather(*[
        gw.submit_for(agent_id, order()) for agent_id in ("a1", "a2", "a3", "a4")
    ])

    accepted = [r for r in results if r.status is not OrderStatus.REJECTED]
    assert len(accepted) == 1, f"하루 1건 한도에 {len(accepted)}건이 통과했습니다"
    assert len(venue.sent) == 1, f"증권사에 {len(venue.sent)}건이 도달했습니다"


@pytest.mark.asyncio
async def test_concurrent_notional_is_also_serialised():
    venue = SlowVenue()
    gw = AccountGateway(group(0.25, 0.25, 0.25, 0.25), venue, base_currency="KRW",
                        master_budget=TradingBudget(max_daily_notional=1_500.0))
    gw.master_budget.roll(equity=1_000_000)

    def priced():
        o = order(qty=1)
        o.limit_price = 1_000.0
        o.type = OrderType.LIMIT
        return o

    results = await asyncio.gather(*[
        gw.submit_for(a, priced()) for a in ("a1", "a2", "a3", "a4")
    ])
    accepted = [r for r in results if r.status is not OrderStatus.REJECTED]
    assert len(accepted) <= 2, "거래대금 한도를 동시성이 넘겼습니다"


# ── ③ 공매도가 클램프를 끄지 못하는가 ────────────────────────────────────
def test_short_selling_cannot_be_enabled_on_a_shared_account():
    """예전에는 `allow_short=True` 한 줄로 클램프 전체가 건너뛰어졌습니다 —
    상한이 사라진 매도가 그대로 나갔고, 그것이 남의 물량이었습니다."""
    gw = AccountGateway(group(0.5, 0.5), SlowVenue(), base_currency="KRW")
    with pytest.raises(BrokerageError, match="공매도"):
        SleeveBrokerage("a1", gw, allow_short=True)


# ── ④ 장부에 계좌 합계가 들어와도 클램프가 버티는가 ──────────────────────
def test_the_clamp_never_reads_a_number_larger_than_the_ledger():
    """증권사 어댑터의 `_sync_once` 는 **계좌 전체** 수량을 자기 portfolio 에
    적습니다. 그 portfolio 가 어느 에이전트의 장부이면 520주가 그 장부에
    들어앉고, 클램프는 통과이고, 손절 하나가 계좌를 비웁니다.

    합계 불변식도 이것을 잡지 못합니다 — 나간 520주가 발주 에이전트에게
    귀속되므로 Σ 는 여전히 맞습니다. 그래서 여기서 막아야 합니다.
    """
    gw = AccountGateway(group(0.5, 0.5), SlowVenue(), base_currency="KRW")
    gw.apply_fill("a1", SAMSUNG, Decimal("10"))
    gw.apply_fill("a2", SAMSUNG, Decimal("10"))

    sleeve = SleeveBrokerage("a1", gw, mode=RunMode.LIVE)
    poisoned = Portfolio(starting_cash=50_000, base_currency="KRW")
    position = poisoned.position(SAMSUNG)
    position.quantity = Decimal("520")          # 계좌 합계가 들어왔다
    position.avg_price = 1_000.0
    sleeve.portfolio = poisoned

    clamped, trimmed = sleeve.clamp_to_sleeve(
        order(side=OrderSide.SELL, qty=520))

    assert clamped.quantity == Decimal("10"), "손절 하나가 계좌를 비웠습니다"
    assert trimmed == Decimal("510")


def test_a_book_smaller_than_the_ledger_is_still_believed():
    """반대 방향은 장부를 씁니다 — 방금 체결이 원장에 아직 안 온 경우라
    작은 쪽이 맞습니다."""
    gw = AccountGateway(group(0.5, 0.5), SlowVenue(), base_currency="KRW")
    gw.apply_fill("a1", SAMSUNG, Decimal("10"))

    sleeve = SleeveBrokerage("a1", gw, mode=RunMode.LIVE)
    book = Portfolio(starting_cash=50_000, base_currency="KRW")
    book.position(SAMSUNG).quantity = Decimal("4")
    sleeve.portfolio = book

    clamped, _ = sleeve.clamp_to_sleeve(order(side=OrderSide.SELL, qty=10))
    assert clamped.quantity == Decimal("4")


# ── ⑤ 미귀속 채택이 도난을 지우지 않는가 ─────────────────────────────────
def test_adoption_refuses_to_absorb_a_shortfall():
    """`unassigned := 증권사 − Σ슬리브` 를 그대로 받아 적으면 음수도 들어옵니다.

    그러면 합계는 맞아떨어지고, 불변식이 잡으라고 있는 단 하나의 사건 —
    누가 우리 물량을 옮겼다 — 이 채택 단계에서 소멸합니다.
    """
    gw = AccountGateway(group(0.5, 0.5), SlowVenue(), base_currency="KRW")
    gw.apply_fill("a1", SAMSUNG, Decimal("10"))
    gw.apply_fill("a2", SAMSUNG, Decimal("10"))

    with pytest.raises(GroupHalted):
        gw.adopt_unassigned({"toss:005930": Decimal("5")})

    assert gw.halted is True
    assert "005930" in gw.halt_reason


def test_adoption_happens_only_once_per_group():
    """매 시작마다 채택하면 불변식이 언제나 참이 됩니다."""
    gw = AccountGateway(group(0.5, 0.5), SlowVenue(), base_currency="KRW")
    gw.adopt_unassigned({"toss:005930": Decimal("30")})

    with pytest.raises(GroupHalted, match="한 번만"):
        gw.adopt_unassigned({"toss:005930": Decimal("30")})


def test_adoption_still_accepts_genuine_pre_existing_holdings():
    """정상 경로는 그대로여야 합니다 — 사용자가 앱에서 직접 산 주식."""
    gw = AccountGateway(group(0.5, 0.5), SlowVenue(), base_currency="KRW")
    gw.apply_fill("a1", SAMSUNG, Decimal("10"))

    adopted = gw.adopt_unassigned({"toss:005930": Decimal("30")})

    assert adopted == {"toss:005930": Decimal("20")}
    assert gw.halted is False
    assert gw.check_invariant({"toss:005930": Decimal("30")}) == {}


# ── ⑥ 에이전트 id 가 디렉터리 이름으로 안전한가 ──────────────────────────
@pytest.mark.parametrize("sneaky", ["ok\n", "ok\r", "ok\n\n"])
def test_agent_id_rejects_a_trailing_newline(sneaky):
    """파이썬의 `$` 는 문자열 끝의 개행 **앞** 에서도 일치합니다.

    경로 탈출은 아니지만 `"attack"` 과 `"attack\\n"` 이 서로 다른 디렉터리가
    되어, 화면에는 같은 이름의 에이전트가 둘 보이고 각자 다른 성향 파일을
    읽습니다.
    """
    with pytest.raises(AgentConfigError):
        AgentSpec(agent_id=sneaky, label="x", config_path="c.yaml",
                  capital_weight=0.5)


# ── 부수: 동기화가 체결을 먼저 비우는가 ──────────────────────────────────
@pytest.mark.asyncio
async def test_sync_drains_fills_before_judging_drift():
    """마지막 폴링 이후 들어온 체결은 증권사 잔고에는 있고 원장에는 없습니다.

    그 상태로 불변식을 보면 정상 체결이 드리프트로 읽히고, 그 오판이 그룹을
    멈춥니다 — 그리고 멈춘 그룹은 **나가는 주문도 못 냅니다.**
    """
    from quant.core.types import Fill, utcnow

    class VenueWithLateFill(SlowVenue):
        def __init__(self):
            super().__init__()
            self.pending = [Fill(order_id="ord_x", symbol=SAMSUNG,
                                 side=OrderSide.BUY, quantity=Decimal("10"),
                                 price=1_000.0, fee=0.0, ts=utcnow())]

        async def poll_fills(self):
            out, self.pending = self.pending, []
            return out

        async def positions(self):
            return {"toss:005930": Decimal("10")}

    gw = AccountGateway(group(0.5, 0.5), VenueWithLateFill(), base_currency="KRW")
    gw._order_agent["ord_x"] = "a1"

    report = await gw.sync_for("a1")

    assert report["sleeve_drift"] == {}, "정상 체결이 드리프트로 읽혔습니다"
    assert gw.halted is False, "체결 하나가 그룹을 멈추고 탈출을 막았습니다"
    assert gw.sleeve_positions("a1") == {"toss:005930": Decimal("10")}


# ── ⑦ 관찰용 에이전트가 진짜 돈을 쓰지 못하는가 ──────────────────────────
class RealMoneyVenue(SlowVenue):
    """진짜 돈이 나가는 어댑터."""

    live = True
    venue_backed = True


def live_and_watching():
    return AgentGroup(agents=(
        AgentSpec(agent_id="watch", label="관찰만", config_path="c.yaml",
                  capital_weight=0.5, mode=RunMode.DRY_RUN),
        AgentSpec(agent_id="real", label="실거래", config_path="c.yaml",
                  capital_weight=0.5, mode=RunMode.LIVE),
    ))


@pytest.mark.asyncio
async def test_a_dry_run_agent_cannot_send_orders_to_a_real_account():
    """`AgentSpec.mode` 는 화면 라벨이 아니라 약속입니다.

    슬리브의 주문은 그룹이 물고 있는 하나뿐인 어댑터로 갑니다 — 그 어댑터가
    실거래면 관찰용 에이전트의 주문도 진짜 돈으로 체결됩니다. "하나는 실거래,
    하나는 관찰만" 은 이 기능을 쓰는 가장 흔한 방식입니다.
    """
    venue = RealMoneyVenue()
    gw = AccountGateway(live_and_watching(), venue, base_currency="KRW")

    with pytest.raises(GroupHalted, match="관찰"):
        await gw.submit_for("watch", order(qty=10))

    assert venue.sent == [], "관찰용 에이전트의 주문이 실계좌로 나갔습니다"


@pytest.mark.asyncio
async def test_the_live_agent_in_the_same_group_still_trades():
    venue = RealMoneyVenue()
    gw = AccountGateway(live_and_watching(), venue, base_currency="KRW")

    result = await gw.submit_for("real", order(qty=10))

    assert result.status is not OrderStatus.REJECTED
    assert len(venue.sent) == 1


@pytest.mark.asyncio
async def test_a_dry_run_group_on_a_simulated_venue_is_unaffected():
    """관찰 전용 그룹은 그대로 돌아야 합니다 — 막을 이유가 없습니다."""
    venue = SlowVenue()                      # live 아님
    gw = AccountGateway(group(0.5, 0.5), venue, base_currency="KRW")

    result = await gw.submit_for("a1", order(qty=10))

    assert result.status is not OrderStatus.REJECTED
    assert len(venue.sent) == 1


@pytest.mark.asyncio
async def test_an_order_from_an_agent_outside_the_group_is_refused():
    venue = RealMoneyVenue()
    gw = AccountGateway(live_and_watching(), venue, base_currency="KRW")

    with pytest.raises(GroupHalted, match="그룹에 없는"):
        await gw.submit_for("stranger", order(qty=1))
    assert venue.sent == []
