"""재시작을 넘어야 하는 두 가지 — 계좌 한도와 주문의 주인.

둘 다 잃어도 예외가 나지 않습니다. 그래서 위험합니다.

**계좌 하루 한도.** 잃으면 재시작이 계좌에 새 허용치를 줍니다. 하루 손실 한도가
걸려 "다음 거래일까지 중단" 이 된 계좌를 재배포 한 번이 풀어 주는데, 그건 한도가
아니라 한도와 초기화 버튼을 함께 둔 것이고, 그 버튼은 봇이 고장 났을 때 가장
자주 눌립니다.

**주문의 주인.** 잃으면 그 체결이 미귀속으로 떨어집니다. 미귀속 물량은 어느
에이전트도 팔 수 없고 합계 불변식은 그것을 정상으로 읽으므로, 판 적도 없는
주식이 손절도 청산도 닿지 않는 채로 계좌에 남습니다.

한 자리를 더 확인합니다: 계좌 원장이 `day_budget` 이 아니라 **자기 테이블** 에
살아야 합니다. 같은 테이블에 넣으면 `day_budget JOIN runs WHERE mode='live'`
스캔에 걸려 Toss 계좌 게이트가 모든 시작을 영구히 거절합니다.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from quant.core.types import (
    UTC,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    RunMode,
    Symbol,
)
from quant.live.agents import AgentGroup, AgentSpec
from quant.live.gateway import AccountGateway
from quant.live.limits import TradingBudget
from quant.live.state import StateStore

SAMSUNG = Symbol("005930", venue="toss", quote_currency="KRW",
                 lot_size=Decimal("1"), tick_size=Decimal("100"))
NOW = datetime(2026, 3, 3, 4, 0, tzinfo=UTC)


def budget(**caps):
    return TradingBudget(**caps)


def order(qty=1, price=1_000.0):
    return Order(symbol=SAMSUNG, side=OrderSide.BUY, quantity=Decimal(str(qty)),
                 type=OrderType.LIMIT, limit_price=price)


@pytest.fixture
def path(tmp_path):
    return tmp_path / "state.db"


# ── 계좌 한도가 재시작을 넘는가 ──────────────────────────────────────────
def test_a_restart_does_not_hand_the_account_a_fresh_allowance(path):
    """이것이 이 파일의 이유 전부입니다."""
    first = StateStore(path)
    master = budget(max_daily_loss=200_000)
    first.restore_account_budget(master, NOW)
    master.roll(NOW, equity=1_000_000)
    master.record_trade(-250_000, NOW)          # 한도를 넘겼다
    # `halted` 는 다음 `check()` 에서 정해집니다 — 기록만으로는 아직입니다.
    blocked, _ = master.check(order(), 1_000.0, False, NOW, equity=1_000_000)
    assert blocked is False, "재시작 전부터 한도가 안 걸렸습니다"
    first.close()

    again = StateStore(path)
    try:
        fresh = budget(max_daily_loss=200_000)
        assert again.restore_account_budget(fresh, NOW) is True
        assert fresh.today.realized_pnl == pytest.approx(-250_000)
        allowed, why = fresh.check(order(), 1_000.0, False, NOW, equity=1_000_000)
        assert allowed is False, "재배포 한 번이 손실 한도를 풀었습니다"
        assert "손실" in why
    finally:
        again.close()


def test_the_order_and_turnover_counters_survive_too(path):
    first = StateStore(path)
    master = budget(max_daily_orders=3, max_daily_notional=10_000)
    first.restore_account_budget(master, NOW)
    master.roll(NOW, equity=1_000_000)
    for _ in range(2):
        master.record_order(order(), 1_000.0, NOW)
    first.close()

    again = StateStore(path)
    try:
        fresh = budget(max_daily_orders=3, max_daily_notional=10_000)
        again.restore_account_budget(fresh, NOW)
        assert fresh.today.orders == 2
        assert fresh.today.notional == pytest.approx(2_000)
    finally:
        again.close()


def test_a_new_trading_day_starts_clean(path):
    """넘겨야 하는 것은 오늘의 원장이지 어제의 것이 아닙니다."""
    first = StateStore(path)
    master = budget(max_daily_loss=200_000)
    first.restore_account_budget(master, NOW)
    master.roll(NOW, equity=1_000_000)
    master.record_trade(-250_000, NOW)
    first.close()

    tomorrow = NOW + timedelta(days=1)
    again = StateStore(path)
    try:
        fresh = budget(max_daily_loss=200_000)
        again.restore_account_budget(fresh, tomorrow)
        fresh.roll(tomorrow, equity=1_000_000)
        assert fresh.today.realized_pnl == 0
        allowed, _ = fresh.check(order(), 1_000.0, False, tomorrow,
                                 equity=1_000_000)
        assert allowed is True
    finally:
        again.close()


def test_the_bound_ledger_keeps_writing_after_restore(path):
    """스냅샷이 아니라 바인딩입니다 — 주문 하나가 두 스냅샷 사이로 빠져나가면,
    그 하나가 바로 재시작이 두 번 통과시키는 주문입니다."""
    store = StateStore(path)
    try:
        master = budget(max_daily_orders=5)
        store.restore_account_budget(master, NOW)
        master.roll(NOW, equity=1_000_000)
        master.record_order(order(), 1_000.0, NOW)

        row = store.conn.execute(
            "SELECT orders FROM account_budget ORDER BY day DESC LIMIT 1"
        ).fetchone()
        assert row["orders"] == 1, "기록이 저장까지 흐르지 않았습니다"
    finally:
        store.close()


# ── 계좌 원장이 실행 원장과 섞이지 않는가 ────────────────────────────────
def test_the_account_ledger_lives_outside_day_budget(path):
    """`day_budget` 에 넣으면 `JOIN runs WHERE mode='live'` 스캔에 걸려 Toss
    계좌 게이트가 모든 시작을 영구히 거절합니다."""
    store = StateStore(path)
    try:
        store.start_run("s", "live", 100_000, agent_id="attack")
        agent = budget(max_daily_orders=5)
        store.restore_budget(agent, NOW)
        agent.roll(NOW, equity=50_000)
        agent.record_order(order(), 1_000.0, NOW)

        master = budget(max_daily_orders=10)
        store.restore_account_budget(master, NOW)
        master.roll(NOW, equity=100_000)
        for _ in range(4):
            master.record_order(order(), 1_000.0, NOW)

        day_rows = store.conn.execute("SELECT run_id, orders FROM day_budget").fetchall()
        acct_rows = store.conn.execute("SELECT orders FROM account_budget").fetchall()
        assert [(r["run_id"], r["orders"]) for r in day_rows] == [(store.run_id, 1)]
        assert [r["orders"] for r in acct_rows] == [4]
    finally:
        store.close()


def test_the_account_ledger_does_not_need_a_run(path):
    """계좌 한도는 어느 실행에도 속하지 않습니다 — 에이전트가 전부 바뀌어도
    같은 계좌의 같은 하루 허용치입니다."""
    store = StateStore(path)
    try:
        assert store.run_id is None
        master = budget(max_daily_orders=5)
        store.restore_account_budget(master, NOW)
        master.roll(NOW, equity=100_000)
        master.record_order(order(), 1_000.0, NOW)
        assert store.conn.execute(
            "SELECT COUNT(*) c FROM account_budget").fetchone()["c"] == 1
    finally:
        store.close()


# ── 주문의 주인이 재시작을 넘는가 ────────────────────────────────────────
def group(*ids):
    return AgentGroup(agents=tuple(
        AgentSpec(agent_id=a, label=a, config_path="c.yaml",
                  capital_weight=round(1 / len(ids), 4)) for a in ids))


class Venue:
    name = "v"
    portfolio = None
    budget = None

    def __init__(self):
        self.sent = []

    async def submit(self, o):
        self.sent.append(o)
        o.status = OrderStatus.NEW          # 아직 미체결
        return o

    async def cancel(self, o):
        return True

    async def open_orders(self):
        return list(self.sent)

    async def positions(self):
        return {}

    async def sync(self):
        return {}

    async def connect(self):
        return None

    async def close(self):
        return None

    def exact_flatten_order_type(self, s, c, t):
        return None


@pytest.mark.asyncio
async def test_an_in_flight_orders_owner_survives_a_restart(path):
    """미체결 주문을 남긴 채 재시작했을 때, 그 체결이 미귀속으로 떨어지면
    판 적도 없는 주식이 손절도 청산도 닿지 않는 채로 남습니다."""
    from quant.core.types import Fill, utcnow

    first = StateStore(path)
    gw = AccountGateway(group("attack", "defend"), Venue(),
                        base_currency="KRW", store=first)
    placed = await gw.submit_for("attack", order(qty=10))
    first.close()

    again = StateStore(path)
    try:
        gw2 = AccountGateway(group("attack", "defend"), Venue(),
                             base_currency="KRW", store=again)
        assert gw2.adopt_order_agents(again.restore_order_agents()) == 1

        # 재시작 뒤에 그 주문의 체결이 돌아온다
        gw2.settle([Fill(order_id=placed.id, symbol=SAMSUNG, side=OrderSide.BUY,
                         quantity=Decimal("10"), price=1_000.0, fee=0.0,
                         ts=utcnow())])
        assert gw2.sleeve_positions("attack") == {"toss:005930": Decimal("10")}
        assert gw2.unassigned_positions() == {}, "주인을 잃어 미귀속이 됐습니다"
    finally:
        again.close()


@pytest.mark.asyncio
async def test_the_owner_is_written_before_the_order_leaves(path):
    """보낸 뒤에 적으면, 그 사이에 죽은 프로세스가 남긴 주문은 주인을 잃습니다."""
    store = StateStore(path)
    try:
        class Dies(Venue):
            async def submit(self, o):
                raise RuntimeError("프로세스가 여기서 죽었다")

        gw = AccountGateway(group("attack"), Dies(), base_currency="KRW",
                            store=store)
        o = order(qty=5)
        with pytest.raises(RuntimeError):
            await gw.submit_for("attack", o)

        assert store.restore_order_agents() == {o.id: "attack"}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_rejected_order_leaves_no_trace(path):
    store = StateStore(path)
    try:
        class Rejects(Venue):
            async def submit(self, o):
                o.status = OrderStatus.REJECTED
                return o

        gw = AccountGateway(group("attack"), Rejects(), base_currency="KRW",
                            store=store)
        await gw.submit_for("attack", order())
        assert store.restore_order_agents() == {}
    finally:
        store.close()


def test_stale_attributions_are_pruned(path):
    """일주일 전 주문의 체결이 이제 와서 돌아오는 일은 없고, 지우지 않으면 이
    표만 무한히 자랍니다."""
    store = StateStore(path)
    try:
        store.save_order_agent("old", "attack", "toss:005930")
        store.conn.execute(
            "UPDATE order_agent SET created_at=? WHERE order_id='old'",
            ((datetime.now(UTC) - timedelta(days=30)).isoformat(),))
        store.conn.commit()
        store.save_order_agent("new", "defend", "toss:000660")

        assert store.restore_order_agents() == {"new": "defend"}
    finally:
        store.close()


def test_forgetting_an_order_clears_the_durable_row(path):
    store = StateStore(path)
    try:
        gw = AccountGateway(group("attack"), Venue(), base_currency="KRW",
                            store=store)
        gw._remember_order("ord_1", "attack", "toss:005930")
        gw.forget_order("ord_1")
        assert store.restore_order_agents() == {}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_gateway_without_a_store_still_works(path):
    """백테스트와 테스트는 저장소 없이 씁니다 — 메모리에만 남으면 됩니다."""
    gw = AccountGateway(group("attack"), Venue(), base_currency="KRW")
    placed = await gw.submit_for("attack", order())
    assert gw.attribute(placed) == "attack"


def test_memory_wins_over_the_stored_attribution(path):
    """이 프로세스가 방금 적은 것이 더 최신입니다."""
    store = StateStore(path)
    try:
        store.save_order_agent("ord_1", "defend")
        gw = AccountGateway(group("attack", "defend"), Venue(),
                            base_currency="KRW", store=store)
        gw._remember_order("ord_1", "attack")
        gw.adopt_order_agents(store.restore_order_agents())
        assert gw._order_agent["ord_1"] == "attack"
    finally:
        store.close()


# ── 실거래 그룹의 계좌 한도는 지울 수 없다 ───────────────────────────────
#
# 단일 봇에는 "실거래 중 마지막 한도 해제 금지" 가 이미 있었습니다. 그룹에서는
# 계좌 한도가 **에이전트별이 아닌 유일한** 한도이므로, 그것을 지우는 것은 단일
# 봇에서 마지막 한도를 지우는 것보다 위험합니다. 그런데 그 가드는 "이 봇이
# 실거래인가" 로 판정했고, 그룹에서 `trader(user_id, "")` 는 여럿일 때 None 이라
# 그냥 통과했습니다.
class _FakeGroup:
    """`UserRegistry` 가 그룹에서 읽는 것만 갖춘 대역.

    실제 `GroupTrader` 를 세우려면 실거래 설정과 자격증명이 필요한데, 여기서
    확인하려는 것은 한도 가드의 판정 하나입니다.
    """

    def __init__(self, has_live, ids=("a1", "a2")):
        self.has_live = has_live
        self.alive = True
        self.group = SimpleNamespace(ids=tuple(ids))
        self.gateway = SimpleNamespace(master_budget=None)

    def trader(self, agent_id):
        return None


def _registry_with_group(tmp_path, has_live):
    from quant.webapp.accounts import Accounts
    from quant.webapp.registry import UserRegistry

    accounts = Accounts(tmp_path / "users.db",
                        secret="durability-guard-secret-0123456789")
    reg = UserRegistry(accounts, root=tmp_path / "users")
    uid = accounts.register("me@x.com", "pw-12345678").id
    reg._groups[uid] = _FakeGroup(has_live)
    return reg, uid, accounts


def test_a_live_group_cannot_have_its_account_cap_removed(tmp_path):
    from quant.webapp.registry import ConfigRejected

    reg, uid, accounts = _registry_with_group(tmp_path, has_live=True)
    try:
        reg.save_limits(uid, {"max_daily_loss": 200_000})
        with pytest.raises(ConfigRejected, match="실거래 실행 중에는"):
            reg.save_limits(uid, dict.fromkeys(
                ("max_daily_notional", "max_daily_orders",
                 "max_daily_loss", "max_daily_loss_pct"), 0))
        # 파일도 그대로여야 합니다 — 거절했는데 저장됐으면 재시작이 무제한입니다.
        assert reg.limits(uid)["max_daily_loss"] == 200_000
    finally:
        accounts.close()


def test_an_observation_only_group_may_clear_its_account_cap(tmp_path):
    """관찰 전용 그룹에는 진짜 돈이 없습니다 — 막을 이유가 없습니다."""
    reg, uid, accounts = _registry_with_group(tmp_path, has_live=False)
    try:
        reg.save_limits(uid, {"max_daily_loss": 200_000})
        out = reg.save_limits(uid, dict.fromkeys(
            ("max_daily_notional", "max_daily_orders",
             "max_daily_loss", "max_daily_loss_pct"), 0))
        assert "max_daily_loss" in out["removed"]
    finally:
        accounts.close()


def test_a_single_live_bot_keeps_its_old_guard(tmp_path):
    """그룹이 없을 때의 판정은 정확히 그대로여야 합니다."""
    from quant.webapp.accounts import Accounts
    from quant.webapp.registry import UserRegistry

    accounts = Accounts(tmp_path / "users.db",
                        secret="durability-guard-secret-0123456789")
    try:
        reg = UserRegistry(accounts, root=tmp_path / "users")
        uid = accounts.register("me@x.com", "pw-12345678").id
        live = SimpleNamespace(config=SimpleNamespace(mode=RunMode.LIVE))
        assert reg._scope_is_live(uid, "", live) is True
        assert reg._scope_is_live(uid, "", None) is False
    finally:
        accounts.close()


# ── 슬리브 원장이 재시작을 넘는가 (가장 심각했던 결함) ───────────────────
#
# 이것이 없으면 재시작 하나가 **모든 보유를 영구히 팔 수 없게** 만듭니다.
# 게이트웨이의 원장이 빈 채로 뜨면 `adopt_unassigned` 가 계좌 전부를 미귀속으로
# 받아 적고(미귀속은 아무도 팔 수 없습니다), 에이전트 장부는 `positions` 에서
# 정상 복원되므로 화면에는 포지션이 그대로 보입니다. 손절이 나가면
# `min(장부, 원장)` 이 0 을 골라 거절하고, 합계 불변식은
# Σ슬리브(0) + 미귀속(20) == 증권사(20) 이라 아무 경고도 하지 않습니다.
def _gw(store, ids=("attack", "defend"), venue=None):
    return AccountGateway(group(*ids), venue or Venue(),
                          base_currency="KRW", store=store)


def test_the_sleeve_ledger_survives_a_restart(path):
    first = StateStore(path)
    gw = _gw(first)
    gw.apply_fill("attack", SAMSUNG, Decimal("10"))
    gw.apply_fill("defend", SAMSUNG, Decimal("10"))
    first.close()

    again = StateStore(path)
    try:
        gw2 = _gw(again)
        gw2.adopt_sleeves(again.restore_sleeves())
        assert gw2.sleeve_positions("attack") == {"toss:005930": Decimal("10")}
        assert gw2.sleeve_positions("defend") == {"toss:005930": Decimal("10")}
    finally:
        again.close()


def test_a_restart_does_not_make_every_position_unsellable(path):
    """재현했던 그 사고 그대로 — 재시작 후 자기 손절이 거절됐습니다."""
    from quant.brokerage.sleeve import SleeveBrokerage
    from quant.core.account import Portfolio
    from quant.core.types import RunMode

    first = StateStore(path)
    gw = _gw(first)
    gw.apply_fill("attack", SAMSUNG, Decimal("10"))
    gw.apply_fill("defend", SAMSUNG, Decimal("10"))
    first.close()

    again = StateStore(path)
    try:
        gw2 = _gw(again)
        gw2.adopt_sleeves(again.restore_sleeves())
        gw2.adopt_unassigned({"toss:005930": Decimal("20")})
        assert gw2.unassigned_positions() == {}, "계좌 전부가 미귀속이 됐습니다"

        sleeve = SleeveBrokerage("attack", gw2, mode=RunMode.LIVE)
        book = Portfolio(50_000, "KRW")
        position = book.position(SAMSUNG)
        position.quantity = Decimal("10")
        position.avg_price = 70_000.0
        sleeve.portfolio = book

        clamped, _ = sleeve.clamp_to_sleeve(Order(
            symbol=SAMSUNG, side=OrderSide.SELL, quantity=Decimal("10"),
            type=OrderType.MARKET))
        assert clamped.quantity == Decimal("10"), "자기 포지션의 손절이 막혔습니다"
    finally:
        again.close()


def test_the_unassigned_bucket_survives_too(path):
    """사용자가 앱에서 직접 산 주식은 재시작 뒤에도 미귀속이어야 합니다 —
    잊으면 다음 채택이 그것을 어느 에이전트의 것으로 오해합니다."""
    first = StateStore(path)
    gw = _gw(first)
    gw.apply_fill("attack", SAMSUNG, Decimal("10"))
    gw.adopt_unassigned({"toss:005930": Decimal("50")})
    assert gw.unassigned_positions() == {"toss:005930": Decimal("40")}
    first.close()

    again = StateStore(path)
    try:
        gw2 = _gw(again)
        gw2.adopt_sleeves(again.restore_sleeves())
        assert gw2.unassigned_positions() == {"toss:005930": Decimal("40")}
        assert gw2.sleeve_positions("attack") == {"toss:005930": Decimal("10")}
        gw2.adopt_unassigned({"toss:005930": Decimal("50")})
        assert gw2.unassigned_positions() == {"toss:005930": Decimal("40")}, \
            "두 번 세었습니다"
    finally:
        again.close()


def test_a_zeroed_sleeve_is_not_resurrected(path):
    """0 이 된 항목이 지워지지 않고 남으면, 다음 재시작이 판 적 없는 물량을
    되살립니다."""
    first = StateStore(path)
    gw = _gw(first)
    gw.apply_fill("attack", SAMSUNG, Decimal("10"))
    gw.apply_fill("attack", SAMSUNG, Decimal("-10"))     # 전량 청산
    first.close()

    again = StateStore(path)
    try:
        assert again.restore_sleeves().get("attack", {}) == {}
    finally:
        again.close()


# ── 유령 에이전트 ────────────────────────────────────────────────────────
def test_a_fill_for_an_agent_outside_the_group_goes_to_unassigned(path):
    """예전에는 `setdefault` 가 없는 에이전트의 장부를 만들어 냈고, 그 유령의
    수량이 합계에 더해져 불변식을 정상으로 통과시켰습니다."""
    store = StateStore(path)
    try:
        gw = _gw(store)
        gw.apply_fill("ghost", SAMSUNG, Decimal("10"))
        assert gw.sleeve_positions("ghost") == {}
        assert gw.unassigned_positions() == {"toss:005930": Decimal("10")}
        assert "ghost" not in gw._sleeves
    finally:
        store.close()


def test_a_stored_attribution_for_a_departed_agent_is_dropped(path):
    """상태 DB 는 사용자당 하나라 예전 그룹의 이름이 남아 있고, 이름은 사용자가
    정하므로 실제로 바뀝니다."""
    store = StateStore(path)
    try:
        store.save_order_agent("ord_old", "someone_else")
        gw = _gw(store)
        assert gw.adopt_order_agents(store.restore_order_agents()) == 0
        assert gw._order_agent == {}
    finally:
        store.close()


def test_a_departed_agents_holdings_become_unassigned(path):
    """지금 그룹에 없는 에이전트의 보유는 계좌에 분명히 있습니다 — 아무도 팔 수
    없지만 불변식은 그것을 알아야 합니다."""
    first = StateStore(path)
    gw = _gw(first, ids=("attack", "gone"))
    gw.apply_fill("gone", SAMSUNG, Decimal("7"))
    first.close()

    again = StateStore(path)
    try:
        gw2 = _gw(again, ids=("attack", "defend"))
        gw2.adopt_sleeves(again.restore_sleeves())
        assert gw2.unassigned_positions() == {"toss:005930": Decimal("7")}
        assert gw2.check_invariant({"toss:005930": Decimal("7")}) == {}
    finally:
        again.close()


# ── 미귀속이 음수로 숨지 않는가 ──────────────────────────────────────────
def test_a_negative_unassigned_halts_the_group(path):
    """매도 체결의 주인을 잃으면 미귀속이 음수가 되고, 그 음수가 파는 쪽
    슬리브의 남은 수량과 상쇄되어 합계가 맞아떨어집니다 — 원장은 있다고 하는데
    계좌에는 없는 주식이 그렇게 숨습니다."""
    from quant.core.types import Fill, utcnow

    store = StateStore(path)
    try:
        gw = _gw(store)
        gw.apply_fill("attack", SAMSUNG, Decimal("10"))
        # 주인을 모르는 매도 체결이 돌아온다
        gw.settle([Fill(order_id="unknown", symbol=SAMSUNG, side=OrderSide.SELL,
                        quantity=Decimal("10"), price=70_000.0, fee=0.0,
                        ts=utcnow())])
        assert gw.unassigned_positions() == {"toss:005930": Decimal("-10")}

        # 합계로는 0 == 0 이라 예전에는 통과했습니다.
        drift = gw.check_invariant({})
        assert drift != {}, "음수 미귀속이 합계 뒤에 숨었습니다"
        assert gw.halted is True
        assert "음수" in gw.halt_reason
    finally:
        store.close()


# ── 애매한 거절에서 귀속을 지우지 않는가 ─────────────────────────────────
@pytest.mark.asyncio
async def test_an_ambiguous_rejection_keeps_the_attribution(path):
    """`LiveBrokerage.submit` 은 어댑터의 모든 예외를 REJECTED 로 접습니다.
    그중에는 "증권사가 이미 받았을 수도 있다" 가 섞여 있고, 그때 귀속을 지우면
    이 기록이 존재하는 이유인 바로 그 창에서 기록이 사라집니다."""
    store = StateStore(path)
    try:
        class Ambiguous(Venue):
            fill_channel_ok = False        # 어댑터가 채널을 내렸다

            async def submit(self, o):
                o.status = OrderStatus.REJECTED
                return o

        gw = _gw(store, venue=Ambiguous())
        placed = await gw.submit_for("attack", order())
        assert placed.status is OrderStatus.REJECTED
        assert store.restore_order_agents() == {placed.id: "attack"}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_certain_rejection_still_clears_the_attribution(path):
    store = StateStore(path)
    try:
        class Certain(Venue):
            fill_channel_ok = True

            async def submit(self, o):
                o.status = OrderStatus.REJECTED
                return o

        gw = _gw(store, venue=Certain())
        await gw.submit_for("attack", order())
        assert store.restore_order_agents() == {}
    finally:
        store.close()


# ── 적대적 검토 2차가 찾아낸 것들 ────────────────────────────────────────
def test_a_market_order_is_not_priced_at_zero_by_the_account_cap(path):
    """증권사 어댑터의 장부는 계좌 진실 전용이라 어떤 봉도 마크하지 않습니다.
    거기서 가격을 읽으면 시장가 주문의 거래대금이 전부 0 이 되고, 계좌 거래대금
    한도는 하루 종일 초록색인 채로 아무것도 막지 않습니다."""
    from quant.core.account import Portfolio

    store = StateStore(path)
    try:
        gw = _gw(store)
        gw.master_budget = TradingBudget(max_daily_notional=1_000_000)
        gw.master_budget.roll(equity=1_000_000)

        book = Portfolio(1_000_000, "KRW")
        book.mark(SAMSUNG, 70_000.0)
        gw._agent_books["attack"] = book

        market = Order(symbol=SAMSUNG, side=OrderSide.BUY,
                       quantity=Decimal("10"), type=OrderType.MARKET)
        assert gw._last_price(SAMSUNG) == 70_000.0
        gw._master_record(market)
        assert gw.master_budget.today.notional == pytest.approx(700_000)
    finally:
        store.close()


def test_a_live_group_needs_an_account_cap(tmp_path):
    """에이전트별 한도만으로는 계좌를 지키지 못합니다 — 넷이 각자 자기 한도를
    지켜도 계좌는 그 네 배를 잃습니다. 이 기능이 막으려는 사고 그 자체입니다."""
    import asyncio

    from quant.config.schema import (
        BrokerConfig,
        DataConfig,
        ExecutionConfig,
        ModelSpec,
        PortfolioConfig,
        RiskConfig,
        StrategyConfig,
        SymbolSpec,
        UniverseConfig,
    )
    from quant.webapp.accounts import Accounts
    from quant.webapp.registry import ConfigRejected, UserRegistry

    cfg = StrategyConfig(
        name="x", mode=RunMode.DRY_RUN,
        data=DataConfig(provider="synthetic", params={"seed": 1},
                        timeframe="1d", warmup_bars=30),
        universe=UniverseConfig(symbols=[SymbolSpec(ticker="AAA", venue="SIM")]),
        alpha=[ModelSpec(type="ema_cross")],
        portfolio=PortfolioConfig(starting_cash=50_000, base_currency="KRW"),
        risk=RiskConfig(), execution=ExecutionConfig(),
        broker=BrokerConfig(type="paper"))

    accounts = Accounts(tmp_path / "u.db",
                        secret="durability-cap-secret-0123456789ab")
    try:
        reg = UserRegistry(accounts, root=tmp_path / "users")
        uid = accounts.register("me@x.com", "pw-12345678").id
        assert reg.account_budget(uid, cfg).configured is False

        live = AgentGroup(agents=(
            AgentSpec(agent_id="a1", label="a", config_path="x",
                      capital_weight=0.5, mode=RunMode.LIVE),
            AgentSpec(agent_id="a2", label="b", config_path="y",
                      capital_weight=0.5, mode=RunMode.LIVE)))
        with pytest.raises(ConfigRejected, match="계좌 전체 하루 한도"):
            asyncio.run(reg.start_group(uid, live, {"a1": cfg, "a2": cfg},
                                        venue=object()))
    finally:
        accounts.close()


def test_the_account_halt_can_be_released(tmp_path):
    """걸린 것이 계좌 한도일 때 그것을 푸는 길이 화면 어디에도 없었습니다."""
    from quant.webapp.accounts import Accounts
    from quant.webapp.registry import UserRegistry

    accounts = Accounts(tmp_path / "u.db",
                        secret="durability-halt-secret-0123456789a")
    try:
        reg = UserRegistry(accounts, root=tmp_path / "users")
        uid = accounts.register("me@x.com", "pw-12345678").id
        master = TradingBudget(max_daily_orders=1)
        reg._groups[uid] = _FakeGroup(True)
        reg._groups[uid].gateway = SimpleNamespace(master_budget=master)

        assert reg._live_budget(uid, "") is master
    finally:
        accounts.close()


def test_a_dry_run_group_does_not_spend_the_live_accounts_allowance(path):
    """아침에 모의로 시험하다 20 건을 쓰고 실거래로 바꾸면 계좌가 이미 멈춰
    있습니다. 반대 방향도 마찬가지입니다."""
    store = StateStore(path)
    try:
        paper = TradingBudget(max_daily_orders=20)
        store.restore_account_budget(paper, NOW, mode="dry_run")
        paper.roll(NOW, equity=1_000_000)
        for _ in range(18):
            paper.record_order(order(), 1_000.0, NOW)

        real = TradingBudget(max_daily_orders=20)
        assert store.restore_account_budget(real, NOW, mode="live") is False
        assert real.roll(NOW, equity=1_000_000).orders == 0, \
            "모의 주문이 실거래 허용치를 먹었습니다"
    finally:
        store.close()


def test_a_timezone_edit_does_not_erase_the_account_halt(path):
    """시간대는 그룹의 첫 에이전트 설정에서 왔을 뿐이라, 에이전트 순서를
    바꾸거나 설정 하나를 손보는 것만으로 달라집니다 — 그 정도의 일이 계좌
    방어선을 지워서는 안 됩니다."""
    first = StateStore(path)
    kst = TradingBudget(max_daily_loss=200_000, timezone_offset_hours=9.0)
    first.restore_account_budget(kst, NOW)
    kst.roll(NOW, equity=1_000_000)
    kst.record_trade(-250_000, NOW)
    first.close()

    again = StateStore(path)
    try:
        utc = TradingBudget(max_daily_loss=200_000, timezone_offset_hours=0.0)
        assert again.restore_account_budget(utc, NOW) is True
        assert utc.today.realized_pnl == pytest.approx(-250_000)
        allowed, _ = utc.check(order(), 1_000.0, False, NOW, equity=1_000_000)
        assert allowed is False, "시간대 한 줄이 계좌 손실 한도를 풀었습니다"

        row = again.conn.execute(
            "SELECT tz_offset_hours FROM account_budget").fetchone()
        assert row["tz_offset_hours"] == 9.0, "저장된 하루 경계가 덮어써졌습니다"
    finally:
        again.close()


@pytest.mark.asyncio
async def test_a_failed_group_start_does_not_brick_the_next_one(tmp_path):
    """시작이 실패하면 그 그룹은 상태 DB 소유권만 붙들고 앉아 있습니다.
    그대로 두면 자격증명을 고치고 다시 눌러도 "이미 트레이더를 돌리고 있습니다"
    로 막히고, 그 문장은 지금 상황을 설명하지 못합니다."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_registry_groups import Venue, agents, config

    from quant.webapp.accounts import Accounts
    from quant.webapp.registry import UserRegistry

    class Flaky(Venue):
        fail = True

        async def connect(self):
            if Flaky.fail:
                Flaky.fail = False
                raise RuntimeError("토큰 만료")
            await super().connect()

    accounts = Accounts(tmp_path / "u.db",
                        secret="registry-group-test-secret-0123456789ab")
    try:
        reg = UserRegistry(accounts, root=tmp_path / "users")
        uid = accounts.register("a@b.com", "pw-12345678").id
        ids = ("attack", "defend")
        cfgs = {a: config(f"{a}-strat") for a in ids}

        with pytest.raises(RuntimeError, match="토큰 만료"):
            await reg.start_group(uid, agents(*ids), cfgs, venue=Flaky())
        assert reg.group(uid) is None

        # 자격증명을 고치고 다시 누른다
        await reg.start_group(uid, agents(*ids), cfgs, venue=Flaky())
        assert reg.group(uid) is not None
        await reg.shutdown(wait=1.0)
    finally:
        accounts.close()
