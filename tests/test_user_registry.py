"""사용자별 실행 단위 — 한 프로세스, 여러 사람의 봇.

여기서 확인하는 것은 "돈다"가 아니라 **섞이지 않는다** 입니다. 남의 증권사
키로 내 봇이 주문을 내거나, 내 재시작이 남의 포지션을 복원하거나, 남의 성향이
내 손절 폭을 정하는 경로가 하나라도 열려 있으면 이 서비스는 존재하면 안 됩니다.
그래서 새어나갈 수 있는 방향마다 테스트가 하나씩 있습니다.

실제 브로커 엔드포인트는 어디서도 호출하지 않습니다. KIS 어댑터는 생성자에서
네트워크를 쓰지 않으므로, 가짜 키로 세워서 "그 키가 어디로 들어갔는지"만 봅니다.
실행 경로는 synthetic 시세 + paper 브로커로만 돌립니다.
"""
import asyncio
import json
import os

import pytest

from quant.config.schema import StrategyConfig
from quant.live.profile import InvestorProfile, ProfileStore
from quant.live.state import StateStore
from quant.webapp.accounts import Accounts
from quant.webapp.registry import (
    AlreadyRunning,
    CredentialsMissing,
    NotRunning,
    UserRegistry,
    required_secrets,
)

SECRET = "registry-test-secret-key-0123456789abcdef"

#: 이 값들이 응답·설정·환경변수 어디에도 나타나면 안 됩니다.
A_KEYS = {"KIS_APP_KEY": "AAAA-app-key-aaaa", "KIS_APP_SECRET": "AAAA-secret-aaaa",
          "KIS_ACCOUNT_NO": "11112222"}
B_KEYS = {"KIS_APP_KEY": "BBBB-app-key-bbbb", "KIS_APP_SECRET": "BBBB-secret-bbbb",
          "KIS_ACCOUNT_NO": "33334444"}

_PROCESS_WIDE = (
    "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO", "KIS_ACCOUNT_PRD_CD",
    "TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_NO",
    "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "QUANT_LIMIT_DAILY_NOTIONAL", "QUANT_LIMIT_DAILY_ORDERS",
    "QUANT_LIMIT_DAILY_LOSS", "QUANT_LIMIT_DAILY_LOSS_PCT",
    "QUANT_PROFILE_FILE",
)


@pytest.fixture(autouse=True)
def a_clean_process(tmp_path, monkeypatch):
    """프로세스에 남아 있는 1인용 시절의 설정이 결과를 만들지 않게 한다.

    작업 디렉터리까지 옮기는 것은 저장소 루트의 `investor_profile.json` 때문입니다.
    그 파일 하나가 모든 사용자의 사이즈와 손절을 다시 정할 수 있는지가
    아래 테스트 중 하나의 주제이기도 합니다.
    """
    for var in _PROCESS_WIDE:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def accounts(tmp_path):
    return Accounts(tmp_path / "users.db", secret=SECRET)


@pytest.fixture
def registry(accounts, tmp_path):
    return UserRegistry(accounts, root=tmp_path / "users")


@pytest.fixture
def build(registry):
    """`build_trader` 를 부르되 열린 상태 DB 를 반드시 닫는다."""
    made = []

    def _build(user_id, config):
        trader = registry.build_trader(user_id, config)
        made.append(trader)
        return trader

    yield _build
    for trader in made:
        trader.state.close()


def user(accounts, email="one@example.com", secrets=None):
    person = accounts.register(email, "korea-invest-1", "테스터")
    for name, value in (secrets or {}).items():
        accounts.put_secret(person.id, name, value)
    return person


def kis_config(**over) -> StrategyConfig:
    """한투 계좌에 붙는 설정. 자격증명은 설정이 아니라 사용자에게서 옵니다."""
    base = {
        "name": "한투전략",
        "mode": "dry_run",
        "data": {"provider": "kis", "timeframe": "1d", "calendar": "always_open",
                 "warmup_bars": 60},
        "universe": {"symbols": [{"ticker": "005930", "venue": "KRX",
                                  "quote_currency": "KRW"}]},
        "alpha": [{"type": "ema_cross"}],
        "broker": {"type": "kis"},
    }
    base.update(over)
    return StrategyConfig.model_validate(base)


def paper_config(name="모의전략", symbols=("SIM1",), **over) -> StrategyConfig:
    """실제로 돌릴 수 있는 설정 — 시세는 합성, 체결은 시뮬레이션."""
    base = {
        "name": name,
        "mode": "dry_run",
        "data": {"provider": "synthetic", "timeframe": "1d",
                 "calendar": "always_open", "warmup_bars": 60},
        "universe": {"symbols": [{"ticker": t} for t in symbols]},
        "alpha": [{"type": "ema_cross"}],
        "broker": {"type": "paper"},
    }
    base.update(over)
    return StrategyConfig.model_validate(base)


async def until_running(registry, user_id, timeout=15.0):
    async def wait():
        while True:
            trader = registry.trader(user_id)
            if trader is None:                      # 죽었으면 이유를 그대로 보여준다
                raise AssertionError(registry.status(user_id))
            if trader.running:
                return trader
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(wait(), timeout)


# ── 파일이 섞이지 않는다 ────────────────────────────────────────────────
def test_each_user_gets_their_own_state_file(registry, accounts):
    a = user(accounts, "a@example.com")
    b = user(accounts, "b@example.com")
    assert registry.state_path(a.id) != registry.state_path(b.id)

    store_a = StateStore(registry.state_path(a.id))
    store_b = StateStore(registry.state_path(b.id))
    try:
        store_a.start_run("A 전략", "dry_run", 1_000_000, "{}")
        rows = store_b.conn.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"]
        assert rows == 0, "한 사람의 실행 기록이 다른 사람의 상태 DB 에 보입니다"
    finally:
        store_a.close()
        store_b.close()


def test_every_user_file_lives_under_that_users_directory(registry, accounts):
    person = user(accounts)
    paths = registry.paths(person.id)
    for path in (paths.state_db, paths.profile, paths.limits):
        assert path.parent == paths.home


@pytest.mark.skipif(os.name == "nt", reason="POSIX 권한 비트")
def test_a_users_directory_is_not_readable_by_anyone_else(registry, accounts):
    person = user(accounts)
    mode = registry.paths(person.id).home.stat().st_mode & 0o777
    assert mode == 0o700, "포지션과 체결 기록이 들어 있는 디렉터리입니다"


@pytest.mark.parametrize("bad", ["../../etc", "0", "-1", "abc"])
def test_a_user_id_that_is_not_a_positive_number_never_becomes_a_path(registry, bad):
    """id 가 경로가 되므로, 요청에서 흘러온 문자열을 그대로 붙이면 안 됩니다."""
    with pytest.raises(ValueError):
        registry.paths(bad)


# ── 자격증명이 없을 때 ──────────────────────────────────────────────────
def test_starting_without_credentials_says_what_to_register(registry, accounts):
    person = user(accounts)                       # 키를 하나도 등록하지 않았다
    with pytest.raises(CredentialsMissing) as exc:
        registry.build_trader(person.id, kis_config())

    problem = exc.value
    assert problem.status == 400 and problem.code == "credentials_missing"
    assert "한국투자증권" in str(problem)
    assert "설정 화면" in str(problem), "무엇을 해야 하는지가 문장에 있어야 합니다"
    names = {item["name"] for item in problem.to_dict()["missing"]}
    assert {"KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO"} <= names
    labels = {item["label"] for item in problem.missing}
    assert "앱 키" in labels, "화면이 그대로 보여줄 한국어 라벨이어야 합니다"


def test_a_partly_configured_user_is_told_only_what_is_left(registry, accounts):
    person = user(accounts, secrets={"KIS_APP_KEY": "k", "KIS_APP_SECRET": "s"})
    report = registry.readiness(person.id, kis_config())
    assert report["ready"] is False
    assert [item["name"] for item in report["missing"]] == ["KIS_ACCOUNT_NO"]


def test_a_configured_user_is_ready(registry, accounts):
    person = user(accounts, secrets=A_KEYS)
    assert registry.readiness(person.id, kis_config())["ready"] is True


def test_required_secrets_tells_the_setup_screen_what_a_strategy_needs():
    """화면이 "이 전략을 쓰려면 무엇이 필요한지" 를 미리 물어볼 수 있어야 합니다."""
    needed = required_secrets(kis_config(flow={"provider": "kis"}))
    assert set(needed) == {"KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO"}
    assert required_secrets(paper_config()) == []


def test_a_paper_strategy_needs_nothing_and_starts(registry, accounts, build):
    person = user(accounts)                       # 키가 하나도 없어도
    trader = build(person.id, paper_config())     # 모의 전략은 세워집니다
    assert trader.engine.brokerage.name == "paper"


# ── 자격증명이 흐르는 길 ────────────────────────────────────────────────
def test_the_keys_reach_the_adapters_as_constructor_arguments(registry, accounts, build):
    person = user(accounts, secrets=A_KEYS)
    trader = build(person.id, kis_config(flow={"provider": "kis"}))

    broker = trader.engine.brokerage
    assert broker.app_key == A_KEYS["KIS_APP_KEY"]
    assert broker.app_secret == A_KEYS["KIS_APP_SECRET"]
    assert broker.account_no == A_KEYS["KIS_ACCOUNT_NO"]
    assert trader.provider.inner.app_key == A_KEYS["KIS_APP_KEY"]
    assert trader.engine.flow_feed.provider.app_key == A_KEYS["KIS_APP_KEY"]


def test_no_credential_is_ever_written_to_the_environment(registry, accounts, build):
    """환경변수는 프로세스 전역이라, 거기 올린 키는 남의 봇도 읽습니다."""
    person = user(accounts, secrets=A_KEYS)
    build(person.id, kis_config(flow={"provider": "kis"}))
    for name, value in A_KEYS.items():
        assert name not in os.environ
        assert value not in os.environ.values()


def test_two_users_bots_never_hold_each_others_keys(registry, accounts, build):
    a = user(accounts, "a@example.com", A_KEYS)
    b = user(accounts, "b@example.com", B_KEYS)
    broker_a = build(a.id, kis_config()).engine.brokerage
    broker_b = build(b.id, kis_config()).engine.brokerage

    assert broker_a.app_key == A_KEYS["KIS_APP_KEY"]
    assert broker_b.app_key == B_KEYS["KIS_APP_KEY"]
    assert broker_a.account_no != broker_b.account_no


def test_the_config_the_trader_carries_has_no_keys_in_it(registry, accounts, build):
    """`LiveTrader.start()` 는 이 설정을 그대로 상태 DB 에 적습니다."""
    person = user(accounts, secrets=A_KEYS)
    trader = build(person.id, kis_config(flow={"provider": "kis"}))
    dumped = trader.config.model_dump_json()
    for value in A_KEYS.values():
        assert value not in dumped


def test_a_key_left_in_the_template_loses_to_the_users_own(registry, accounts, build):
    """템플릿에 남은 키는 운영자의 것입니다. 그것으로 남의 주문이 나가면 안 됩니다."""
    person = user(accounts, secrets=A_KEYS)
    config = kis_config(broker={"type": "kis",
                                "params": {"app_key": "운영자키",
                                           "app_secret": "운영자시크릿",
                                           "account_no": "99999999"}})
    broker = build(person.id, config).engine.brokerage
    assert broker.app_key == A_KEYS["KIS_APP_KEY"]
    assert broker.account_no == A_KEYS["KIS_ACCOUNT_NO"]


def test_the_shared_template_config_is_never_mutated(registry, accounts, build):
    """API 계층은 설정 객체 하나를 모든 사용자에게 돌려씁니다.

    그 객체에 한 사람의 키나 한도가 남으면, 다음 사람은 남의 설정으로 시작합니다.
    """
    person = user(accounts, secrets=A_KEYS)
    registry.save_limits(person.id, {"max_daily_orders": 5})
    shared = kis_config()
    build(person.id, shared)

    assert shared.broker.params == {} and shared.data.params == {}
    assert shared.limits.max_daily_orders == 0


def test_a_user_without_a_telegram_token_does_not_borrow_the_templates(
        registry, accounts, build):
    """남의 텔레그램으로 내 체결 내역이 가는 것보다 알림이 없는 편이 낫습니다."""
    person = user(accounts, secrets=A_KEYS)
    config = kis_config(notify={"telegram_bot_token": "운영자봇토큰",
                                "telegram_chat_id": "12345"})
    trader = build(person.id, config)
    assert trader.notifier.enabled is False


def test_a_user_with_their_own_telegram_token_gets_their_own_alerts(
        registry, accounts, build):
    person = user(accounts, secrets={**A_KEYS,
                                     "TELEGRAM_BOT_TOKEN": "내봇토큰-1234",
                                     "TELEGRAM_CHAT_ID": "77777"})
    trader = build(person.id, kis_config())
    assert trader.notifier.enabled is True
    assert "내봇토큰-1234" not in trader.config.model_dump_json()


# ── 투자 성향은 사람 것 ─────────────────────────────────────────────────
def defensive() -> InvestorProfile:
    """가장 방어적인 답안 — 모든 축을 낮게."""
    return InvestorProfile(overrides={"R": -1.0, "H": -1.0, "E": -1.0, "C": -1.0})


def aggressive() -> InvestorProfile:
    return InvestorProfile(overrides={"R": 1.0, "H": 1.0, "E": 1.0, "C": 1.0})


def test_each_user_carries_their_own_profile(registry, accounts, build):
    a = user(accounts, "a@example.com", A_KEYS)
    b = user(accounts, "b@example.com", B_KEYS)
    registry.save_profile(a.id, defensive())
    registry.save_profile(b.id, aggressive())

    model_a = build(a.id, kis_config()).engine.portfolio_model
    model_b = build(b.id, kis_config()).engine.portfolio_model
    assert model_a.max_position_weight < model_b.max_position_weight, (
        "방어형과 공격형이 같은 사이즈로 도는 것은 성향이 섞였다는 뜻입니다")


def test_a_leftover_process_profile_does_not_shape_a_users_bot(
        registry, accounts, build, tmp_path):
    """1인용 시절의 `investor_profile.json` 이 모두의 손절 폭을 정하면 안 됩니다."""
    ProfileStore(tmp_path / "investor_profile.json").save(aggressive())
    person = user(accounts, secrets=A_KEYS)       # 이 사람은 진단하지 않았다

    trader = build(person.id, kis_config())
    default_weight = StrategyConfig().portfolio.max_position_weight
    assert trader.engine.portfolio_model.max_position_weight == default_weight
    assert trader.config.limits.max_daily_loss_pct == 0.0, (
        "남의 성향이 이 사람의 하루 손실 한도를 정했습니다")


async def test_saving_a_profile_reaches_a_running_bot(registry, accounts):
    person = user(accounts)
    await registry.start(person.id, paper_config())
    trader = await until_running(registry, person.id)
    try:
        applied = registry.save_profile(person.id, defensive())
        assert trader.engine.portfolio_model.max_position_weight == pytest.approx(0.12)
        assert "봉 주기" in applied["needs_restart"], (
            "재시작이 필요한 것은 그렇다고 말해야 합니다")
    finally:
        await registry.stop(person.id)


# ── 하루 한도도 사람 것 ─────────────────────────────────────────────────
def test_daily_limits_belong_to_the_user(registry, accounts, build):
    a = user(accounts, "a@example.com", A_KEYS)
    b = user(accounts, "b@example.com", B_KEYS)
    registry.save_limits(a.id, {"max_daily_orders": 5})

    assert build(a.id, kis_config()).engine.budget.max_orders == 5
    assert build(b.id, kis_config()).engine.budget.max_orders == 0


def test_a_saved_limit_can_only_tighten_the_configured_one(registry, accounts, build):
    person = user(accounts, secrets=A_KEYS)
    registry.save_limits(person.id, {"max_daily_orders": 5})

    loose = kis_config(limits={"max_daily_orders": 100})
    tight = kis_config(limits={"max_daily_orders": 3})
    assert build(person.id, loose).engine.budget.max_orders == 5
    assert build(person.id, tight).engine.budget.max_orders == 3


def test_partial_limit_updates_leave_the_others_alone(registry, accounts):
    person = user(accounts)
    registry.save_limits(person.id, {"max_daily_orders": 5, "max_daily_loss": 100_000})
    result = registry.save_limits(person.id, {"max_daily_orders": 9})

    assert result["saved"]["max_daily_orders"] == 9
    assert result["saved"]["max_daily_loss"] == 100_000
    assert result["updated"] == ["max_daily_orders"]


def test_removing_a_limit_is_named_in_the_answer(registry, accounts):
    person = user(accounts)
    registry.save_limits(person.id, {"max_daily_orders": 5})
    result = registry.save_limits(person.id, {"max_daily_orders": 0})
    assert result["removed"] == ["max_daily_orders"], "무제한이 된 것은 말해줘야 합니다"


def test_limits_survive_a_restart_of_the_process(registry, accounts, tmp_path):
    person = user(accounts)
    registry.save_limits(person.id, {"max_daily_loss_pct": 0.02})
    reborn = UserRegistry(registry.accounts, root=tmp_path / "users")
    assert reborn.limits(person.id)["max_daily_loss_pct"] == pytest.approx(0.02)


async def test_a_users_limits_reach_their_running_bot_now(registry, accounts):
    person = user(accounts)
    await registry.start(person.id, paper_config())
    trader = await until_running(registry, person.id)
    try:
        result = registry.save_limits(person.id, {"max_daily_orders": 7})
        assert trader.engine.budget.max_orders == 7
        assert result["applied_now"] is not None
    finally:
        await registry.stop(person.id)


# ── 한 사람당 봇 하나 ───────────────────────────────────────────────────
def test_status_before_anything_is_started(registry, accounts):
    person = user(accounts)
    status = registry.status(person.id)
    assert status["running"] is False and "실행" in status["message"]


async def test_only_one_bot_runs_per_user(registry, accounts):
    person = user(accounts)
    await registry.start(person.id, paper_config())
    await until_running(registry, person.id)
    try:
        with pytest.raises(AlreadyRunning) as exc:
            await registry.start(person.id, paper_config(name="두번째"))
        assert exc.value.status == 409
        assert registry.running() == [person.id]
    finally:
        await registry.stop(person.id)


async def test_two_users_run_side_by_side_on_their_own_files(registry, accounts):
    a = user(accounts, "a@example.com")
    b = user(accounts, "b@example.com")
    await registry.start(a.id, paper_config(name="A전략"))
    await registry.start(b.id, paper_config(name="B전략"))
    try:
        trader_a = await until_running(registry, a.id)
        trader_b = await until_running(registry, b.id)
        assert trader_a.state.path != trader_b.state.path
        assert registry.running() == sorted([a.id, b.id])
    finally:
        await registry.shutdown()


async def test_stopping_frees_the_slot_for_the_next_start(registry, accounts):
    person = user(accounts)
    await registry.start(person.id, paper_config())
    await until_running(registry, person.id)
    result = await registry.stop(person.id)
    assert result["stopping"] is True and result["stopped"] is True
    assert registry.running() == []

    await registry.start(person.id, paper_config(name="다시"))
    try:
        assert (await until_running(registry, person.id)).config.name == "다시"
    finally:
        await registry.stop(person.id)


async def test_stopping_nothing_is_a_sentence_not_a_traceback(registry, accounts):
    person = user(accounts)
    with pytest.raises(NotRunning) as exc:
        await registry.stop(person.id)
    assert exc.value.status == 404
    assert exc.value.to_dict()["error"]


async def test_a_bot_that_dies_says_why_and_frees_the_slot(registry, accounts):
    """유니버스가 비어 워밍업이 실패합니다 — 화면은 이유를 알아야 합니다."""
    person = user(accounts)
    await registry.start(person.id, paper_config(name="빈유니버스", symbols=()))

    async def wait_dead():
        while registry.trader(person.id) is not None:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(wait_dead(), 15)
    status = registry.status(person.id)
    assert status["running"] is False
    assert "universe" in status["error"], status
    assert registry.running() == []
    await registry.start(person.id, paper_config())      # 슬롯은 비어 있다
    await registry.stop(person.id)


async def test_shutdown_stops_everyone(registry, accounts):
    a = user(accounts, "a@example.com")
    b = user(accounts, "b@example.com")
    await registry.start(a.id, paper_config(name="A전략"))
    await registry.start(b.id, paper_config(name="B전략"))
    await until_running(registry, a.id)
    await until_running(registry, b.id)

    await registry.shutdown()
    assert registry.running() == []


async def test_a_stopped_bot_left_its_state_on_disk(registry, accounts):
    """멈춘 뒤에도 이 사람의 기록은 이 사람 파일에 남아 있어야 합니다."""
    person = user(accounts)
    await registry.start(person.id, paper_config())
    await until_running(registry, person.id)
    await registry.stop(person.id)

    store = StateStore(registry.state_path(person.id))
    try:
        runs = store.conn.execute("SELECT strategy, config_json FROM runs").fetchall()
        assert [r["strategy"] for r in runs] == ["모의전략"]
        assert json.loads(runs[0]["config_json"])["name"] == "모의전략"
    finally:
        store.close()


# ── 감사 기록 ───────────────────────────────────────────────────────────
async def test_starting_and_stopping_are_recorded_without_any_values(
        registry, accounts):
    person = user(accounts, secrets=A_KEYS)
    await registry.start(person.id, paper_config())
    await until_running(registry, person.id)
    await registry.stop(person.id)

    history = json.dumps(accounts.history(person.id), ensure_ascii=False)
    assert "bot_started" in history and "bot_stopped" in history
    for value in A_KEYS.values():
        assert value not in history
