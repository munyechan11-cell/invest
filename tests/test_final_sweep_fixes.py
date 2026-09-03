"""머지 직전 검토가 찾아낸 돈 경로 결함들 — 재현 → 수정 → 못 박기.

이전 두 라운드가 못 본 것들입니다. 공통점은 **가짜 증권사로만 시험했다** 는 것.
슬리브·게이트웨이·그룹 테스트는 전부 `Brokerage` 흉내를 냈지, 실제 어댑터의
부모인 `LiveBrokerage` 를 물린 적이 없었습니다. 그래서 `LiveBrokerage._guard`
가 주문 금액을 매기는 방식과 우리 배선이 만나는 자리가 통째로 비어 있었습니다.

  · 어댑터의 장부는 아무도 마크하지 않는다 → 시장가 주문 전부 거절 (긴급 청산 포함)
  · 자본 분할이 화면에만 있고 엔진에는 안 닿았다
  · 어댑터의 실거래 여부가 요청 순서로 정해졌다
  · 슬리브에 종료 안전 게이트가 없어 실거래 종료가 언제나 "안전" 으로 적혔다
  · 실거래 그룹이 crash quarantine 을 한 번도 걸지 않았다
  · ■ 정지가 계좌를 붙든 채 남아 재시작 전까지 아무것도 못 켰다
  · /api/health 가 2개 이상 그룹을 "안 돈다" 로 보고했다
"""
from __future__ import annotations

import asyncio
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from quant.brokerage.live_base import LiveBrokerage
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
from quant.live.agents import AgentGroup, AgentSpec
from quant.live.gateway import AccountGateway

sys.path.insert(0, str(Path(__file__).parent))

SAMSUNG = Symbol("005930", venue="toss", quote_currency="KRW",
                 lot_size=Decimal("1"), tick_size=Decimal("100"))


class RealAdapter(LiveBrokerage):
    """실제 어댑터의 부모를 그대로 쓴 최소 구현.

    `submit()` 의 dry-run 경로는 네트워크 호출만 빼고 `_guard` 까지 전부
    지나갑니다 — 시장가 주문의 가격을 어댑터 장부에서 읽는 바로 그 코드입니다.
    """

    name = "real-ish"

    async def _venue_submit(self, order):
        return f"v-{order.id}"

    async def _venue_cancel(self, order):
        return True

    async def _venue_open_orders(self):
        return []

    async def _venue_positions(self):
        return {}


def adapter(live=False, cls=None):
    """시험용 어댑터. 주문 금액 상한(기본 1만원)은 여기서 재는 것이 아니므로
    넉넉히 둡니다 — 그 상한에 걸리면 "가격 0 원" 결함과 구별이 안 됩니다."""
    return (cls or RealAdapter)(Portfolio(1_000_000, "KRW"), live=live,
                                max_order_notional=100_000_000.0)


def group(*ids, live=False):
    mode = RunMode.LIVE if live else RunMode.DRY_RUN
    return AgentGroup(agents=tuple(
        AgentSpec(agent_id=a, label=f"에이전트 {a}", config_path="c.yaml",
                  capital_weight=round(1 / len(ids), 4), mode=mode)
        for a in ids))


def market(side=OrderSide.BUY, qty=10):
    return Order(symbol=SAMSUNG, side=side, quantity=Decimal(str(qty)),
                 type=OrderType.MARKET)


# ── ① 시장가 주문이 어댑터 장부의 0 원 가격에 거절되지 않는가 ────────────
@pytest.mark.asyncio
async def test_a_market_order_through_a_real_adapter_is_not_rejected_for_price():
    """어댑터의 장부는 계좌 진실 전용이라 어떤 봉도 마크되지 않습니다.
    `LiveBrokerage._guard` 는 그 장부의 `last_price`(=0) 로 주문 금액을
    매기고, 0 이면 "가격을 알 수 없어" 거절합니다 — 시장가 주문 **전부**,
    전체 청산의 긴급 매도까지."""
    venue = adapter()
    gw = AccountGateway(group("attack", "defend"), venue, base_currency="KRW")

    # 에이전트 장부는 매 봉 마크됩니다 — 게이트웨이가 여기서 시세를 읽어
    # 어댑터 장부에 비춰야 합니다.
    book = Portfolio(500_000, "KRW")
    book.mark(SAMSUNG, 70_000.0)
    gw._agent_books["attack"] = book

    placed = await gw.submit_for("attack", market())

    assert placed.status is not OrderStatus.REJECTED, placed.reject_reason
    assert venue.portfolio.position(SAMSUNG).last_price == 70_000.0


@pytest.mark.asyncio
async def test_an_emergency_market_sell_also_gets_a_price():
    """나가는 길이 막히는 것이 가장 나쁩니다."""
    venue = adapter()
    gw = AccountGateway(group("attack"), venue, base_currency="KRW")
    book = Portfolio(500_000, "KRW")
    position = book.position(SAMSUNG)
    position.quantity = Decimal("10")
    position.avg_price = 70_000.0
    book.mark(SAMSUNG, 65_000.0)
    gw._agent_books["attack"] = book
    gw.apply_fill("attack", SAMSUNG, Decimal("10"))
    venue.portfolio.position(SAMSUNG).quantity = Decimal("10")

    placed = await gw.submit_for("attack", market(OrderSide.SELL, 10))
    assert placed.status is not OrderStatus.REJECTED, placed.reject_reason


@pytest.mark.asyncio
async def test_without_any_agent_mark_the_guard_still_refuses():
    """비출 시세가 없으면 예전처럼 거절돼야 합니다 — 가격을 모르는 주문을
    지어낸 0 원으로 통과시키는 것이 진짜 사고입니다."""
    venue = adapter()
    gw = AccountGateway(group("attack"), venue, base_currency="KRW")

    placed = await gw.submit_for("attack", market())
    assert placed.status is OrderStatus.REJECTED
    assert "가격을 알 수 없어" in placed.reject_reason


# ── ② 슬리브가 종료·격리 판정에 답하는가 ─────────────────────────────────
def test_sleeve_reports_sends_orders_from_the_account_adapter():
    """`Engine.stop` 은 이 값이 참일 때만 체결 채널 복구를 검사합니다."""
    live_venue = adapter(live=True)
    paper_venue = adapter()
    live = SleeveBrokerage("a1", AccountGateway(group("a1", live=True), live_venue))
    paper = SleeveBrokerage("a1", AccountGateway(group("a1"), paper_venue))
    assert live.sends_orders is True
    assert paper.sends_orders is False


def test_remote_open_order_check_exists_only_when_the_adapter_has_one():
    """없는 어댑터에 0 을 지어내면 확인한 적 없는 것을 확인했다고 적는 셈이고,
    예외를 던지면 페이퍼 그룹의 모든 종료가 "불안전" 이 됩니다."""
    from test_registry_groups import Venue  # LiveBrokerage 가 아닌 가상 증권사

    plain = Venue()
    sleeve = SleeveBrokerage("a1", AccountGateway(group("a1"), plain))
    assert getattr(sleeve, "shutdown_remote_open_order_count", None) is None

    class Counting(RealAdapter):
        async def shutdown_remote_open_order_count(self):
            return 3

    counting = adapter(cls=Counting)
    sleeve2 = SleeveBrokerage("a1", AccountGateway(group("a1"), counting))
    fn = getattr(sleeve2, "shutdown_remote_open_order_count", None)
    assert fn is not None
    assert asyncio.run(fn()) == 3


def test_sleeve_arms_the_crash_quarantine_only_on_a_live_truth_account():
    class Truth(RealAdapter):
        venue_capital_truth = True

    live_truth = adapter(live=True, cls=Truth)
    sleeve = SleeveBrokerage("a1", AccountGateway(group("a1", live=True), live_truth))
    assert sleeve.account_uses_venue_capital is True

    paper = SleeveBrokerage("a1", AccountGateway(
        group("a1"), adapter()))
    assert paper.account_uses_venue_capital is False


def test_live_trader_arms_the_quarantine_for_sleeves_too():
    """`LiveTrader` 는 실거래 진실 계좌에서 시작할 때 격리를 겁니다. 그 조건이
    `isinstance(LiveBrokerage)` 뿐이면 슬리브는 영영 격리 없이 돌고, 죽은
    프로세스가 수동 대조 없이 재시작됩니다."""
    source = Path("quant/live/trader.py").read_text(encoding="utf-8")
    arming = re.search(
        r"if \(\(isinstance\(self\.engine\.brokerage, LiveBrokerage\)(.*?)"
        r"self\.state\.mark_reconciliation_required\(\)",
        source, re.S)
    assert arming, "격리를 거는 분기를 찾지 못했습니다"
    assert "account_uses_venue_capital" in arming.group(1)


# ── ③ 자본 분할·배타성·정지 해제·health ─────────────────────────────────
@pytest.fixture
def registry(tmp_path):
    from quant.webapp.accounts import Accounts
    from quant.webapp.registry import UserRegistry

    accounts = Accounts(tmp_path / "users.db",
                        secret="final-sweep-secret-0123456789abcdef")
    reg = UserRegistry(accounts, root=tmp_path / "users")
    uid = accounts.register("me@x.com", "pw-12345678").id
    yield reg, uid
    accounts.close()


@pytest.mark.asyncio
async def test_the_capital_split_reaches_each_engines_book(registry):
    """예전에는 게이트웨이가 몫을 계산만 하고 아무 장부에도 쓰지 않아, 넷이
    각자 템플릿의 starting_cash 로 사이징했습니다 — 80만원 템플릿 넷이면
    100만원 계좌에 320만원어치 매수 의도가 실립니다."""
    from test_registry_groups import Venue, agents, config

    reg, uid = registry
    ids = ("attack", "defend")
    await reg.start_group(uid, agents(*ids),
                          {a: config(f"{a}-strat", cash=800_000) for a in ids},
                          venue=Venue())            # 계좌 잔고 100,000
    try:
        g = reg.group(uid)
        for a in ids:
            book = g.traders[a].engine.ctx.portfolio
            assert book.cash == pytest.approx(50_000), (
                f"{a} 는 템플릿 800,000 으로 사이징합니다"
            )
            assert book.starting_cash == pytest.approx(50_000)
    finally:
        await reg.shutdown(wait=1.0)


@pytest.mark.asyncio
async def test_a_single_bot_cannot_start_over_a_running_group(registry):
    """배타성은 양방향이어야 합니다."""
    from test_registry_groups import Venue, agents, config

    from quant.webapp.registry import AlreadyRunning

    reg, uid = registry
    ids = ("attack", "defend")
    await reg.start_group(uid, agents(*ids),
                          {a: config(f"{a}-strat") for a in ids}, venue=Venue())
    try:
        with pytest.raises(AlreadyRunning):
            await reg.start(uid, config("solo"))
    finally:
        await reg.shutdown(wait=1.0)


@pytest.mark.asyncio
async def test_stopping_a_group_releases_the_account_for_the_next_start(registry):
    """멈추는 것과 놓는 것은 다릅니다 — 예전에는 ■ 정지 뒤 프로세스를
    재시작하기 전까지 이 계좌로 아무것도 켤 수 없었습니다."""
    from test_registry_groups import Venue, agents, config

    reg, uid = registry
    ids = ("attack", "defend")
    cfgs = {a: config(f"{a}-strat") for a in ids}
    await reg.start_group(uid, agents(*ids), cfgs, venue=Venue())
    await reg.stop(uid, wait=5.0)

    assert reg.group(uid) is None
    await reg.start_group(uid, agents(*ids), cfgs, venue=Venue())
    try:
        assert reg.group(uid) is not None
    finally:
        await reg.shutdown(wait=1.0)


@pytest.mark.asyncio
async def test_a_mixed_group_gets_a_live_adapter_regardless_of_order(registry):
    """어댑터의 실거래 여부는 가장 위험한 에이전트가 정합니다. 예전에는 첫
    번째 설정으로 세워서 [관찰, 실거래] 순서면 실거래를 확인한 에이전트가
    조용히 가상 체결을 받았습니다."""
    from test_registry_groups import Venue, config

    from quant.webapp.registry import ConfigRejected

    reg, uid = registry
    reg.save_limits(uid, {"max_daily_loss": 200_000})   # 실거래 그룹의 계좌 한도
    mixed = AgentGroup(agents=(
        AgentSpec(agent_id="watch", label="관찰", config_path="a", capital_weight=0.5,
                  mode=RunMode.DRY_RUN),
        AgentSpec(agent_id="real", label="실거래", config_path="b", capital_weight=0.5,
                  mode=RunMode.LIVE),
    ))
    paper = Venue()                                   # live 속성 없음 → 모의
    with pytest.raises(ConfigRejected, match="실거래 여부가 에이전트 구성과"):
        await reg.start_group(uid, mixed,
                              {"watch": config("a"), "real": config("b")},
                              venue=paper)


def test_health_sees_a_running_group(tmp_path):
    """`trader()` 는 여럿일 때 None 입니다(되묻는 규칙). 그 None 을 "안 돈다"
    로 읽으면 /api/health 가 실거래 그룹을 통째로 멈춘 것으로 보고합니다."""
    from types import SimpleNamespace

    from quant.api.server import UserDesk

    class FakeRegistry:
        def trader(self, uid, agent_id=""):
            return None                               # 에이전트 둘 → 되묻기

        def group(self, uid):
            return SimpleNamespace(alive=True)

    desk = UserDesk.__new__(UserDesk)
    desk.registry = FakeRegistry()
    desk.user = SimpleNamespace(id=1)
    assert desk.running() is True
    assert desk.running("attack") is False
