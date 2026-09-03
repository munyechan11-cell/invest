"""레지스트리가 그룹을 안다 — 그리고 어느 에이전트인지 되묻는다.

`require_trader()` 가 그룹에서 조용히 하나를 고르면, `close_all` 이 그 하나만
정리하고 성공을 돌려줍니다. 사용자는 전부 정리된 줄 알고 화면을 닫고, 나머지
셋은 그대로 시장에 남습니다. 성향 변경도 같은 방식으로 엉뚱한 봇에 적용됩니다.

그래서 여럿일 때는 **묻습니다.** 하나뿐이면 묻지 않습니다 — 답이 하나뿐인
질문은 사용자에게 일만 늘립니다.
"""
import pytest

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
from quant.core.types import OrderStatus, RunMode
from quant.live.agents import AgentGroup, AgentSpec
from quant.live.profile import InvestorProfile
from quant.webapp.accounts import Accounts
from quant.webapp.registry import (
    AgentRequired,
    AlreadyRunning,
    NotRunning,
    UserRegistry,
)

SECRET = "registry-group-test-secret-0123456789ab"


def config(name, cash=50_000):
    return StrategyConfig(
        name=name,
        mode=RunMode.DRY_RUN,
        data=DataConfig(provider="synthetic", params={"seed": 1},
                        timeframe="1d", warmup_bars=30),
        universe=UniverseConfig(symbols=[SymbolSpec(ticker="AAA", venue="SIM")]),
        alpha=[ModelSpec(type="ema_cross")],
        portfolio=PortfolioConfig(starting_cash=cash, cash_reserve_pct=0.0),
        risk=RiskConfig(),
        execution=ExecutionConfig(min_order_notional=1.0),
        broker=BrokerConfig(type="paper"),
    )


class Venue:
    name = "venue"
    portfolio = None
    budget = None
    live = False
    venue_backed = True

    def __init__(self):
        self.connects = 0
        self.closes = 0

    async def submit(self, order):
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        order.avg_fill_price = 100.0
        return order

    async def cancel(self, order):
        return True

    async def open_orders(self):
        return []

    async def positions(self):
        return {}

    async def balances(self):
        return {"KRW": 100_000.0}

    async def sync(self):
        return {}

    async def connect(self):
        self.connects += 1

    async def close(self):
        self.closes += 1

    def exact_flatten_order_type(self, symbol, cur, target):
        return None


def agents(*ids):
    return AgentGroup(agents=tuple(
        AgentSpec(agent_id=a, label=f"에이전트 {a}", config_path=f"{a}.yaml",
                  capital_weight=round(1 / len(ids), 4))
        for a in ids))


@pytest.fixture
def registry(tmp_path):
    accounts = Accounts(tmp_path / "users.db", secret=SECRET)
    reg = UserRegistry(accounts, root=tmp_path / "users")
    user = accounts.register("a@b.com", "pw-12345678")
    yield reg, user.id
    accounts.close()


async def start(reg, uid, *ids):
    return await reg.start_group(
        uid, agents(*ids), {a: config(f"{a}-strat") for a in ids},
        venue=Venue())


# ── 그룹을 띄운다 ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_group_starts_and_reports_every_agent(registry):
    reg, uid = registry
    status = await start(reg, uid, "attack", "defend")
    try:
        assert [a["agent_id"] for a in status["agents"]] == ["attack", "defend"]
        assert reg.agent_ids(uid) == ["attack", "defend"]
        assert reg.running() == [uid]
    finally:
        await reg.shutdown(wait=1.0)


@pytest.mark.asyncio
async def test_capital_is_split_between_the_agents(registry):
    reg, uid = registry
    await start(reg, uid, "attack", "defend")
    try:
        group = reg.group(uid)
        assert group.gateway.sleeve_balances("attack") == {"KRW": 50_000.0}
        assert group.gateway.sleeve_balances("defend") == {"KRW": 50_000.0}
    finally:
        await reg.shutdown(wait=1.0)


@pytest.mark.asyncio
async def test_each_agent_gets_its_own_saved_profile(registry):
    """에이전트마다 손절이 다른 것이 이 기능의 전부입니다."""
    reg, uid = registry
    # `overrides` 는 사용자가 축을 직접 정한 값입니다 — 그것이 있어야
    # `completed` 가 참이 되고 `apply_profile` 이 손절 모델을 붙입니다.
    reg.save_profile(uid, InvestorProfile(
        overrides={"R": 1.0, "H": 0.0, "E": 0.0, "C": 0.0}), "attack")
    reg.save_profile(uid, InvestorProfile(
        overrides={"R": -1.0, "H": 0.0, "E": 0.0, "C": 0.0}), "defend")

    await start(reg, uid, "attack", "defend")
    try:
        group = reg.group(uid)

        def stop_of(agent_id):
            models = group.traders[agent_id].engine.risk.models
            model = next(m for m in models if m.name == "max_dd_per_security")
            return model.atr_multiple

        assert stop_of("attack") != stop_of("defend"), (
            "넷이 같은 성향으로 돌고 있습니다 — 사람의 프로필을 읽었습니다"
        )
    finally:
        await reg.shutdown(wait=1.0)


@pytest.mark.asyncio
async def test_each_agent_gets_its_own_saved_limits(registry):
    reg, uid = registry
    reg.save_limits(uid, {"max_daily_orders": 60}, "attack")
    reg.save_limits(uid, {"max_daily_orders": 5}, "defend")

    await start(reg, uid, "attack", "defend")
    try:
        group = reg.group(uid)
        assert group.traders["attack"].engine.budget.max_orders == 60
        assert group.traders["defend"].engine.budget.max_orders == 5
    finally:
        await reg.shutdown(wait=1.0)


# ── 어느 에이전트인지 되묻는다 ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_require_trader_asks_which_agent_when_there_are_several(registry):
    """조용히 하나를 고르면 `close_all` 이 그 하나만 정리하고 성공을
    돌려줍니다."""
    reg, uid = registry
    await start(reg, uid, "attack", "defend")
    try:
        with pytest.raises(AgentRequired) as caught:
            reg.require_trader(uid)
        assert set(caught.value.agents) == {"attack", "defend"}
        assert caught.value.to_dict()["code"] == "agent_required"
    finally:
        await reg.shutdown(wait=1.0)


@pytest.mark.asyncio
async def test_naming_the_agent_resolves_it(registry):
    reg, uid = registry
    await start(reg, uid, "attack", "defend")
    try:
        attack = reg.require_trader(uid, "attack")
        defend = reg.require_trader(uid, "defend")
        assert attack is not defend
        assert attack.config.name == "attack-strat"
    finally:
        await reg.shutdown(wait=1.0)


@pytest.mark.asyncio
async def test_a_single_agent_group_does_not_ask(registry):
    """답이 하나뿐인 질문은 사용자에게 일만 늘립니다."""
    reg, uid = registry
    await start(reg, uid, "solo")
    try:
        assert reg.require_trader(uid).config.name == "solo-strat"
    finally:
        await reg.shutdown(wait=1.0)


@pytest.mark.asyncio
async def test_an_unknown_agent_says_which_one_is_missing(registry):
    reg, uid = registry
    await start(reg, uid, "attack", "defend")
    try:
        with pytest.raises(NotRunning, match="stranger"):
            reg.require_trader(uid, "stranger")
    finally:
        await reg.shutdown(wait=1.0)


def test_no_group_and_no_bot_is_still_the_old_message(registry):
    reg, uid = registry
    with pytest.raises(NotRunning, match="실행 중인 봇이 없습니다"):
        reg.require_trader(uid)


# ── 단일 봇과 그룹은 배타적 ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_second_group_is_refused(registry):
    """계좌가 하나이므로 둘이 동시에 돌면 상태 DB 소유권부터 충돌합니다."""
    reg, uid = registry
    await start(reg, uid, "attack", "defend")
    try:
        with pytest.raises(AlreadyRunning):
            await start(reg, uid, "c3")
    finally:
        await reg.shutdown(wait=1.0)


@pytest.mark.asyncio
async def test_a_group_cannot_start_while_a_single_bot_runs(registry):
    reg, uid = registry
    await reg.start(uid, config("solo"))
    try:
        with pytest.raises(AlreadyRunning):
            await start(reg, uid, "attack")
    finally:
        await reg.shutdown(wait=1.0)


# ── 정지 ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_stop_brings_the_whole_group_down(registry):
    reg, uid = registry
    await start(reg, uid, "attack", "defend")
    try:
        result = await reg.stop(uid, wait=5.0)
        assert result["stopping"] is True
        assert reg.group(uid) is None
    finally:
        await reg.shutdown(wait=1.0)


@pytest.mark.asyncio
async def test_shutdown_releases_the_group(registry):
    reg, uid = registry
    await start(reg, uid, "attack", "defend")
    await reg.shutdown(wait=2.0)

    assert reg.running() == []
    assert reg.group(uid) is None


@pytest.mark.asyncio
async def test_stopping_nothing_is_still_not_running(registry):
    reg, uid = registry
    with pytest.raises(NotRunning):
        await reg.stop_group(uid)


# ── 기존 단일 봇 경로는 그대로 ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_single_bot_still_starts_and_reports_flatly(registry):
    """그룹을 쓰지 않는 사람의 화면이 읽던 키가 그대로여야 합니다."""
    reg, uid = registry
    await reg.start(uid, config("solo"))
    try:
        status = reg.status(uid)
        # 평평한 모양 그대로여야 합니다 — 그룹 응답의 `agents` 배열이 끼면
        # 기존 화면이 읽던 키가 사라집니다.
        assert "agents" not in status
        assert "account" not in status
        assert status["strategy"] == "solo"
    finally:
        await reg.shutdown(wait=1.0)
