"""최종 점검(2026-09-04)이 찾은 그룹 경로의 결함들.

공통점은 전부 **실제 어댑터에서만** 드러난다는 것입니다. 가짜 증권사는
`positions()`·`balances()`·인자 없는 `sync()` 를 갖고 자기 장부를 스스로
적지만, 실제 어댑터(`LiveBrokerage`)는 그 어느 것도 하지 않습니다. 그래서
여기의 대역은 전부 `LiveBrokerage` 를 부모로 둡니다.
"""
from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from quant.brokerage.sleeve import SleeveBrokerage
from quant.core.account import Portfolio
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
from quant.live.gateway import AccountGateway, GroupHalted
from quant.live.group import GroupTrader
from quant.strategy.builder import build_engine
from quant.webapp.accounts import Accounts
from quant.webapp.registry import AlreadyRunning, UserRegistry

sys.path.insert(0, str(Path(__file__).parent))

from test_final_sweep_fixes import SAMSUNG, RealAdapter, adapter, group  # noqa: E402
from test_group_trader import config, two_agents  # noqa: E402
from test_registry_groups import SECRET, Venue, agents  # noqa: E402
from test_registry_groups import config as registry_config  # noqa: E402
from test_sleeve_engine_wiring import Gateway as WiringGateway  # noqa: E402
from test_sleeve_engine_wiring import config as wiring_config  # noqa: E402

HYNIX = Symbol("000660", venue="toss", quote_currency="KRW",
               lot_size=Decimal("1"), tick_size=Decimal("100"))


def fill(order_id, symbol=SAMSUNG, side=OrderSide.BUY, qty=5, price=70_000.0):
    return Fill(order_id=order_id, symbol=symbol, side=side,
                quantity=Decimal(str(qty)), price=price, fee=0.0, ts=utcnow())


# ── ① 슬리브의 거절은 예외가 아니라 REJECTED ──────────────────────────────
@pytest.mark.asyncio
async def test_a_rejected_sleeve_order_does_not_abort_the_rest_of_the_batch():
    """리스크가 [005930 매도, 000660 매도] 를 한 묶음으로 내면 엔진은 순서대로
    보내고 예외를 잡지 않습니다. 첫 주문이 원장에 없는 종목이라 예외로 끝나면
    **둘째 종목의 손절은 나가지 못하고** 포지션이 열린 채 남습니다."""
    aaa = Symbol("AAA", venue="SIM", lot_size=Decimal("1"), tick_size=Decimal("0.01"))
    bbb = Symbol("BBB", venue="SIM", lot_size=Decimal("1"), tick_size=Decimal("0.01"))

    class Ledger(WiringGateway):
        def sleeve_positions(self, agent_id):
            return {bbb.key: Decimal("5")}       # AAA 는 이 에이전트 것이 아니다

    gateway = Ledger()
    book = Portfolio(starting_cash=50_000, base_currency="KRW")
    for sym in (aaa, bbb):
        pos = book.position(sym)
        pos.quantity, pos.avg_price = Decimal("5"), 10.0
        book.mark(sym, 10.0)
    sleeve = SleeveBrokerage("attack", gateway, mode=RunMode.DRY_RUN)
    engine, _ = build_engine(wiring_config(), portfolio=book, brokerage=sleeve)

    batch = [Order(symbol=aaa, side=OrderSide.SELL, quantity=Decimal("5"),
                   type=OrderType.MARKET),
             Order(symbol=bbb, side=OrderSide.SELL, quantity=Decimal("5"),
                   type=OrderType.MARKET)]
    await engine._submit(batch)

    assert batch[0].status is OrderStatus.REJECTED
    assert "다른 에이전트 물량은 팔 수 없습니다" in batch[0].reject_reason
    assert [o.symbol.key for _, o in gateway.submitted] == [bbb.key], (
        "형제 종목의 손절이 첫 주문의 거절에 막혔습니다")


# ── ② 손절 직전 동기화의 인자가 슬리브를 통과한다 ─────────────────────────
@pytest.mark.asyncio
async def test_the_sleeve_forwards_exit_safety_sync_arguments():
    """`LiveTrader._run_exit_safety` 는 `venue_backed` 브로커에
    `expected_positions` 와 `independent_position_keys` 를 넘깁니다. 슬리브가
    받지 못하면 TypeError — 유지보수 루프는 그것을 잡지 않아 에이전트가 죽고
    손절은 나가지 않습니다."""
    class Recorder:
        def __init__(self):
            self.calls = []

        async def sync_for(self, agent_id, **kwargs):
            self.calls.append((agent_id, kwargs))
            return {"ok": True}

    gw = Recorder()
    sleeve = SleeveBrokerage("attack", gw, mode=RunMode.DRY_RUN)
    await sleeve.sync(expected_positions={SAMSUNG.key: Decimal("0")},
                      independent_position_keys={HYNIX.key})
    assert gw.calls == [("attack", {
        "expected_positions": {SAMSUNG.key: Decimal("0")},
        "independent_position_keys": {HYNIX.key},
    })]


@pytest.mark.asyncio
async def test_expected_positions_are_translated_to_account_totals():
    """증권사는 종목별 합계 하나만 압니다. 공격형이 "005930 은 이제 0" 이라
    해도 보수형이 10주를 들고 있으면 계좌에는 10 이 남는 것이 정상입니다.
    옮기지 않으면 어댑터가 그 10 을 '아직 반영 안 된 체결' 로 읽습니다."""
    class Recording(RealAdapter):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.seen = None

        async def sync(self, **kwargs):
            self.seen = kwargs
            return {"ok": True, "venue_positions": {SAMSUNG.key: 10.0}}

    venue = adapter(cls=Recording)
    gw = AccountGateway(group("attack", "defend"), venue, base_currency="KRW")
    gw.apply_fill("attack", SAMSUNG, Decimal("3"))
    gw.apply_fill("defend", SAMSUNG, Decimal("10"))
    gw._unassigned_adopted = True

    await gw.sync_for("attack", expected_positions={SAMSUNG.key: Decimal("0")},
                      independent_position_keys={HYNIX.key})
    assert venue.seen["expected_positions"] == {SAMSUNG.key: Decimal("10")}
    assert venue.seen["independent_position_keys"] == {HYNIX.key}


# ── ③ 실제 어댑터의 계좌 장부에도 체결이 적힌다 ───────────────────────────
@pytest.mark.asyncio
async def test_fills_are_booked_into_the_account_book_of_a_real_adapter():
    """그룹에서는 엔진들이 각자 자기 장부에만 적어 계좌 장부는 비어 있었습니다.
    그러면 토스의 매도 보유 검사가 0 을 보고 모든 매도를 공매도로 거절하고,
    계좌 자본 확인은 '체결이 장부에 없다' 로 영원히 실패합니다."""
    venue = adapter()
    gw = AccountGateway(group("attack", "defend"), venue, base_currency="KRW")
    cash_before = venue.portfolio.cash
    gw._remember_order("o-1", "attack", SAMSUNG.key)

    gw.settle([fill("o-1", qty=5, price=70_000.0)])
    assert venue.portfolio.quantity(SAMSUNG) == Decimal("5")
    assert venue.portfolio.cash == pytest.approx(cash_before - 5 * 70_000.0)
    assert gw.sleeve_positions("attack") == {SAMSUNG.key: Decimal("5")}

    # 주인을 모르는 체결도 계좌에는 분명히 있었던 일입니다.
    gw.settle([fill("stranger", symbol=HYNIX, qty=2, price=100_000.0)])
    assert venue.portfolio.quantity(HYNIX) == Decimal("2")
    assert gw.unassigned_positions() == {HYNIX.key: Decimal("2")}


@pytest.mark.asyncio
async def test_a_paper_venue_is_not_double_booked():
    """페이퍼 증권사는 자기 장부를 스스로 적습니다. 여기서 또 적으면 두 번입니다."""
    paper = Venue()
    paper.portfolio = Portfolio(100_000, "KRW")
    gw = AccountGateway(group("attack"), paper, base_currency="KRW")
    gw._remember_order("o-1", "attack", SAMSUNG.key)
    gw.settle([fill("o-1", qty=5)])
    assert paper.portfolio.quantity(SAMSUNG) == 0


# ── ④ 실패한 동기화의 낡은 장부로 불변식을 보지 않는다 ────────────────────
@pytest.mark.asyncio
async def test_a_failed_sync_does_not_halt_the_group_on_a_stale_snapshot():
    class Flaky(RealAdapter):
        report = {"ok": False, "error": "일시 장애"}

        async def sync(self, **kwargs):
            return dict(self.report)

    venue = adapter(cls=Flaky)
    gw = AccountGateway(group("attack"), venue, base_currency="KRW")
    gw.apply_fill("attack", SAMSUNG, Decimal("3"))
    gw._unassigned_adopted = True

    out = await gw.sync_for("attack")
    assert out["sleeve_drift"] is None
    assert gw.halted is False

    # 스냅샷이 있으면 본다 — 맞으면 통과, 틀리면 정지.
    venue.report = {"ok": True, "venue_positions": {SAMSUNG.key: 3.0}}
    assert (await gw.sync_for("attack"))["sleeve_drift"] == {}
    assert gw.halted is False
    venue.report = {"ok": True, "venue_positions": {SAMSUNG.key: 1.0}}
    assert (await gw.sync_for("attack"))["sleeve_drift"]
    assert gw.halted is True


@pytest.mark.asyncio
async def test_an_open_bot_order_defers_the_invariant():
    """체결 하나가 '체결 비우기' 와 '보유 조회' 사이에 떨어지면 그 한 건이
    드리프트로 읽힙니다. 주문이 다 끝난 다음 동기화에서 봅니다."""
    class Snap(RealAdapter):
        async def sync(self, **kwargs):
            return {"ok": True, "venue_positions": {SAMSUNG.key: 4.0}}

    venue = adapter(cls=Snap)
    gw = AccountGateway(group("attack"), venue, base_currency="KRW")
    gw.apply_fill("attack", SAMSUNG, Decimal("3"))
    gw._unassigned_adopted = True
    resting = Order(symbol=SAMSUNG, side=OrderSide.BUY, quantity=Decimal("1"),
                    type=OrderType.MARKET)
    resting.status = OrderStatus.SUBMITTED
    venue._orders[resting.id] = resting

    out = await gw.sync_for("attack")
    assert out["sleeve_drift"] is None
    assert gw.halted is False


@pytest.mark.asyncio
async def test_unreadable_venue_holdings_refuse_to_start():
    class Broken(RealAdapter):
        async def _venue_positions(self):
            raise RuntimeError("holdings 503")

    gw = AccountGateway(group("attack"), adapter(cls=Broken), base_currency="KRW")
    with pytest.raises(GroupHalted, match="증권사 보유를 읽지 못해"):
        await gw.read_venue_positions()


# ── ⑤ 시작 중인 그룹은 살아 있다 ──────────────────────────────────────────
class SlowVenue(Venue):
    """`connect()` 가 신호를 받을 때까지 멈춰 있는 증권사 — 시작의 창을 벌립니다."""

    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()
        self.entered = asyncio.Event()

    async def connect(self):
        self.entered.set()
        await self.release.wait()
        await super().connect()


@pytest.mark.asyncio
async def test_a_group_is_alive_while_it_is_still_connecting(tmp_path):
    venue = SlowVenue()
    gt = GroupTrader(two_agents(), {"attack": config("a"), "defend": config("b")},
                     str(tmp_path / "s.db"), venue=venue)
    task = asyncio.create_task(gt.start())
    try:
        await asyncio.wait_for(venue.entered.wait(), 2)
        assert gt.alive is True, "연결 중인 그룹을 죽은 것으로 봅니다"
        assert gt.status()["running"] is True
        venue.release.set()
        await asyncio.wait_for(task, 5)
        assert gt.alive is True
    finally:
        venue.release.set()
        await gt.shutdown(wait=1.0)


@pytest.mark.asyncio
async def test_a_second_start_during_startup_is_refused_and_the_first_keeps_its_seat(
        tmp_path):
    """둘째 요청이 첫 그룹의 자리를 덮어쓰고 실패하면, 첫 그룹은 실거래를
    계속하는데 API 에서는 사라져 멈출 손잡이가 없습니다."""
    accounts = Accounts(tmp_path / "users.db", secret=SECRET)
    reg = UserRegistry(accounts, root=tmp_path / "users")
    uid = accounts.register("a@b.com", "pw-12345678").id
    venue = SlowVenue()
    first = asyncio.create_task(reg.start_group(
        uid, agents("attack", "defend"),
        {a: registry_config(f"{a}-strat") for a in ("attack", "defend")},
        venue=venue))
    try:
        await asyncio.wait_for(venue.entered.wait(), 2)
        with pytest.raises(AlreadyRunning):
            await reg.start_group(
                uid, agents("solo"), {"solo": registry_config("solo-strat")},
                venue=Venue())
        venue.release.set()
        await asyncio.wait_for(first, 5)
        assert reg.group(uid) is not None
        assert reg.agent_ids(uid) == ["attack", "defend"]
    finally:
        venue.release.set()
        await reg.shutdown(wait=1.0)
        accounts.close()


# ── ⑥ 죽은 그룹은 이유와 시각을 말한다 ────────────────────────────────────
@pytest.mark.asyncio
async def test_a_dead_group_reports_its_error_and_stopped_at(tmp_path):
    """`/api/health` 는 `error` 나 `stopped_at` 이 있어야 '봇이 멈췄습니다' 를
    띄웁니다. 없으면 워밍업에서 죽은 그룹이 화면에서는 그냥 조용합니다."""
    gt = GroupTrader(two_agents(), {"attack": config("a"), "defend": config("b")},
                     str(tmp_path / "s.db"), venue=Venue())

    async def boom(label):
        raise RuntimeError(f"{label} 인증 거절")

    for agent_id, trader in gt.traders.items():
        trader.run = lambda a=agent_id: boom(a)
    try:
        await gt.start()
        for _ in range(10):
            await asyncio.sleep(0)
        status = gt.status()
        assert status["running"] is False
        assert status["stopped_at"], "죽은 시각이 없습니다"
        assert "attack 인증 거절" in status["error"]
        assert "defend 인증 거절" in status["error"]
    finally:
        await gt.shutdown(wait=0.5)
