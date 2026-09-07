"""장중 재시작 — 형제가 오늘 쓴 하루 허용치가 나의 재개를 막지 않는가.

`assert_toss_account_start_allowed` 는 사용된 `day_budget` 행 가운데 내가 재개할
run 이 아닌 것을 전부 "다른 전략으로 바꿔 당일 한도를 우회하려는 시도" 로
읽습니다. 단일 봇에서는 옳습니다 — 계좌에 원장이 하나뿐이니 그 밖의 사용은
전부 남의 것입니다.

그룹에서는 틀립니다. 에이전트마다 자기 run 과 자기 원장을 갖고, 각 시점은
자기 `agent_id` 로 `prepare_toss_live_run` 을 부릅니다. 그러면 형제가 오늘 한
번이라도 거래한 순간 나머지 전원의 재개가 `daily_budget_strategy_switch_blocked`
로 거절됩니다 — 장중 배포 한 번이 그룹 전체를 `LiveTrader.start()` 에서 죽이고,
보유는 그날 남은 시간 내내 아무도 관리하지 않습니다.

형제란 **지금 그룹에 속한 다른 에이전트** 입니다(`StateStore.group_agent_ids`,
`GroupTrader` 가 저장소를 연 직후 넣습니다). 형제의 허용치를 내 판정에서 빼도
방어선은 줄지 않습니다 — 그 형제의 `prepare_toss_live_run` 이 자기 실행을
"정확히 재개하거나 거부" 하고, 계좌 한도는 `restore_account_budget` 이 계좌
단위로 따로 잇기 때문입니다(HANDOFF 3: 계좌 한도는 계좌 단위로 이어집니다).

남은 여전히 남입니다: 그룹에서 빠진 에이전트의 행, 에이전트 개념이 없던 단일
봇의 행, 그리고 그룹을 선언하지 않은 저장소에서 본 모든 행. 이 파일은 양쪽을
함께 확인합니다.
"""
import json
from datetime import datetime

import pytest

from quant.core.types import UTC
from quant.live.limits import TradingBudget
from quant.live.state import RecoveryArchiveError, StateStore

#: 2026-08-31 14:00 KST — 장중. 하루 경계(15:00 UTC)는 아직 멀었습니다.
NOW = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)
NEXT_KST_DAY = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)


def toss_config(name: str) -> str:
    return json.dumps({
        "name": name, "mode": "live",
        "broker": {"type": "toss"},
        "portfolio": {"base_currency": "KRW"},
        "limits": {"timezone_offset_hours": 9.0,
                   "max_daily_notional": 1_000_000,
                   "max_daily_orders": 20,
                   "max_daily_loss": 50_000,
                   "max_daily_loss_pct": 0.0},
    })


def launch(store, agent_id: str, strategy: str, *, now: datetime = NOW):
    """에이전트 하나를 실거래로 띄운다 — `LiveTrader.start()` 가 하는 그대로."""
    view = store.agent_view(agent_id)
    view.prepare_toss_live_run(strategy, 25_000, toss_config(strategy), now=now)
    view.mark_reconciliation_required()
    return view


def trade(view, *, now: datetime = NOW, pnl: float = -1_500.0) -> None:
    """이 에이전트가 오늘 실제로 거래했다는 durable 증거를 남긴다.

    `save_budget` 은 벽시계로 `updated_at` 을 적는데, 게이트는 그 시각이 `now`
    보다 미래면 fail closed 합니다. 그래서 다른 게이트 테스트들과 같이 갱신
    시각을 시나리오의 시각으로 맞춥니다.
    """
    budget = TradingBudget(max_daily_loss=50_000, timezone_offset_hours=9)
    view.restore_budget(budget, now=now)
    budget.record_trade(pnl, now=now)
    view.conn.execute(
        "UPDATE day_budget SET updated_at=? WHERE run_id=?",
        (now.isoformat(), view.run_id),
    )
    view.conn.commit()


def seed_group_day(path, *, traded=("a", "b"), crash=()) -> dict[str, int]:
    """a(kr-toss-desk)·b(kr-toss-flow) 가 오늘 돌았던 DB 를 만든다.

    `traded` 에 든 에이전트는 오늘 거래했고, `crash` 에 든 에이전트는 정상
    종료를 증명하지 못한 채(격리 표시가 남은 채) 프로세스가 죽었습니다.
    """
    store = StateStore(path)
    store.group_agent_ids = {"a", "b"}
    try:
        views = {
            "a": launch(store, "a", "kr-toss-desk"),
            "b": launch(store, "b", "kr-toss-flow"),
        }
        for agent_id in traded:
            trade(views[agent_id])
        for agent_id, view in views.items():
            if agent_id not in crash:
                view.stop_run()
        return {agent_id: view.run_id for agent_id, view in views.items()}
    finally:
        store.close()


# ── 형제의 허용치는 나를 막지 않는다 ─────────────────────────────────────
def test_same_process_restart_resumes_both_agents_after_they_traded(tmp_path):
    """같은 프로세스 안에서 멈췄다 다시 띄우는 경우(운영자의 정지→시작)."""
    store = StateStore(tmp_path / "state.db")
    store.group_agent_ids = {"a", "b"}
    try:
        a = launch(store, "a", "kr-toss-desk")
        b = launch(store, "b", "kr-toss-flow")
        trade(a)
        trade(b)
        a_run, b_run = a.run_id, b.run_id
        a.stop_run()
        b.stop_run()

        again_a = launch(store, "a", "kr-toss-desk")
        again_b = launch(store, "b", "kr-toss-flow")
        assert (again_a.run_id, again_b.run_id) == (a_run, b_run)
    finally:
        store.close()


@pytest.mark.parametrize("traded", [("a", "b"), ("a",), ("b",)])
def test_new_process_restart_resumes_each_agents_exact_run(tmp_path, traded):
    """배포·재시작 — 새 프로세스가 같은 파일을 연다. 이것이 실제 사고 경로.

    거래한 쪽이 누구든 둘 다 **자기** run 을 정확히 이어받아야 합니다. 어느
    한쪽이라도 거절되면 그 에이전트의 보유는 그날 남은 시간 동안 아무도 관리하지
    않습니다.
    """
    path = tmp_path / "state.db"
    runs = seed_group_day(path, traded=traded)

    store = StateStore(path)
    store.group_agent_ids = {"a", "b"}
    try:
        a = launch(store, "a", "kr-toss-desk")
        b = launch(store, "b", "kr-toss-flow")
        assert a.run_id == runs["a"], "a 가 자기 실행을 이어받지 못했습니다"
        assert b.run_id == runs["b"], "b 가 자기 실행을 이어받지 못했습니다"
        assert store.conn.execute(
            "SELECT COUNT(*) n FROM runs").fetchone()["n"] == 2, (
            "재개 대신 새 원장을 열었습니다 — 당일 한도가 초기화됩니다"
        )
        # 이어받은 원장은 오늘 쓴 만큼을 그대로 기억합니다. 거래하지 않은
        # 쪽은 오늘 행이 없으므로 새 원장으로 시작합니다.
        for agent_id, view in (("a", a), ("b", b)):
            restored = TradingBudget(max_daily_loss=50_000,
                                     timezone_offset_hours=9)
            found = view.restore_budget(restored, now=NOW)
            assert found is (agent_id in traded)
            if found:
                assert restored.today.realized_pnl == pytest.approx(-1_500.0)
    finally:
        store.close()


def test_a_siblings_unprovable_fill_evidence_blocks_only_that_sibling(
        tmp_path):
    """형제의 원장이 자기 체결 증거와 안 맞아도 그것은 **그 형제의** 문제입니다.

    형제는 자기 `prepare_toss_live_run` 에서 스스로 거절됩니다. 그 불확실성이
    나까지 막으면 한 에이전트의 legacy 행 하나가 그룹 전체를 세웁니다.
    """
    path = tmp_path / "state.db"
    store = StateStore(path)
    store.group_agent_ids = {"a", "b"}
    a = launch(store, "a", "kr-toss-desk")
    b = launch(store, "b", "kr-toss-flow")
    trade(a)
    # b: 원장 없이 체결 이벤트만 남은 옛 형태 — 허용치를 재구성할 수 없다.
    store.conn.execute(
        "INSERT INTO events(run_id, ts, type, payload) VALUES(?,?,?,?)",
        (b.run_id, NOW.isoformat(), "order_filled",
         json.dumps({"order_id": "legacy-1", "fee": 0})),
    )
    store.conn.commit()
    a_run, b_run = a.run_id, b.run_id
    a.stop_run()
    b.stop_run()
    store.close()

    again = StateStore(path)
    again.group_agent_ids = {"a", "b"}
    try:
        assert launch(again, "a", "kr-toss-desk").run_id == a_run
        with pytest.raises(RecoveryArchiveError) as refused:
            launch(again, "b", "kr-toss-flow")
        assert refused.value.code == "daily_budget_strategy_switch_blocked"
        assert again.toss_account_start_gate(
            resume_strategy="kr-toss-flow",
            resume_config_json=toss_config("kr-toss-flow"),
            now=NOW, resume_agent_id="b",
        )["budget_blocking_run_id"] == b_run
    finally:
        again.close()


# ── 남은 여전히 남이다 ───────────────────────────────────────────────────
def test_an_agent_removed_from_the_group_still_blocks_the_rest(tmp_path):
    """b 를 그룹에서 빼면 b 가 오늘 쓴 허용치는 남의 것이 됩니다.

    그렇지 않으면 그룹 구성을 바꾸는 것만으로 당일 한도를 우회합니다.
    """
    path = tmp_path / "state.db"
    runs = seed_group_day(path)

    store = StateStore(path)
    store.group_agent_ids = {"a", "c"}
    try:
        with pytest.raises(RecoveryArchiveError) as refused:
            launch(store, "a", "kr-toss-desk")
        assert refused.value.code == "daily_budget_strategy_switch_blocked"
        gate = store.toss_account_start_gate(
            resume_strategy="kr-toss-desk",
            resume_config_json=toss_config("kr-toss-desk"),
            now=NOW, resume_agent_id="a",
        )
        assert gate["budget_blocking_run_id"] == runs["b"]
        assert gate["next_start_allowed_at"] == NEXT_KST_DAY.isoformat()
    finally:
        store.close()


def test_a_store_that_declares_no_group_treats_agent_rows_as_foreign(
        tmp_path):
    """면제의 근거는 행의 `agent_id` 가 아니라 **그룹의 선언** 입니다.

    `group_agent_ids` 를 넣지 않은 저장소(단일 봇, 웹 registry 의 읽기 전용
    조회)에서는 어제의 판정이 한 글자도 바뀌지 않아야 합니다.
    """
    path = tmp_path / "state.db"
    seed_group_day(path)

    store = StateStore(path)
    try:
        assert store.group_agent_ids == set()
        with pytest.raises(RecoveryArchiveError) as refused:
            launch(store, "a", "kr-toss-desk")
        assert refused.value.code == "daily_budget_strategy_switch_blocked"
    finally:
        store.close()


def test_a_legacy_single_bot_ledger_still_blocks_a_group_agent(tmp_path):
    """에이전트 개념이 없던 실행(agent_id '')이 오늘 거래했다면 그룹은 못 뜹니다.

    빈 agent_id 는 형제가 아닙니다 — 그 원장은 계좌 전체의 것이고, 그것을
    이어받을 에이전트도 없습니다.
    """
    path = tmp_path / "state.db"
    single = StateStore(path)
    single.prepare_toss_live_run(
        "kr-toss-desk", 800_000, toss_config("kr-toss-desk"), now=NOW)
    trade(single)
    single.stop_run()
    single.close()

    store = StateStore(path)
    store.group_agent_ids = {"a", "b"}
    try:
        for strategy in ("kr-toss-flow", "kr-toss-desk"):
            with pytest.raises(RecoveryArchiveError) as refused:
                launch(store, "a", strategy)
            assert refused.value.code == "daily_budget_strategy_switch_blocked"
        assert store.conn.execute(
            "SELECT COUNT(*) n FROM runs").fetchone()["n"] == 1
    finally:
        store.close()


def test_the_single_bot_strategy_switch_rule_is_untouched(tmp_path):
    """단일 봇 경로 — `tests/test_reconciliation_archive.py::
    test_clean_stop_cannot_reset_the_toss_daily_loss_with_another_strategy`
    가 지키는 규칙을 여기서도 한 번 더 확인합니다."""
    store = StateStore(tmp_path / "state.db")
    try:
        store.prepare_toss_live_run(
            "kr-toss-desk", 800_000, toss_config("kr-toss-desk"), now=NOW)
        trade(store)
        store.stop_run()
        with pytest.raises(RecoveryArchiveError) as refused:
            store.prepare_toss_live_run(
                "kr-toss-flow", 800_000, toss_config("kr-toss-flow"), now=NOW)
        assert refused.value.code == "daily_budget_strategy_switch_blocked"
    finally:
        store.close()


def test_a_crashed_sibling_from_a_previous_process_still_quarantines(
        tmp_path):
    """허용치 면제가 격리 판정으로 새면 안 됩니다.

    지난 프로세스에서 죽은 형제의 격리는 진짜 불확실성입니다 — 증권사에 무엇이
    남았는지 아무도 모릅니다. 그룹을 선언했더라도 새 프로세스는 막힙니다.
    """
    path = tmp_path / "state.db"
    seed_group_day(path, crash=("a",))

    store = StateStore(path)
    store.group_agent_ids = {"a", "b"}
    try:
        for agent_id, strategy in (("b", "kr-toss-flow"), ("a", "kr-toss-desk")):
            with pytest.raises(RecoveryArchiveError) as refused:
                launch(store, agent_id, strategy)
            assert refused.value.code == "reconciliation_required"
    finally:
        store.close()


def test_next_kst_day_lifts_the_block_for_a_removed_agent(tmp_path):
    """남의 허용치는 그 거래일이 끝나면 더는 막지 않습니다 — 기존 규칙 그대로."""
    path = tmp_path / "state.db"
    seed_group_day(path)

    store = StateStore(path)
    store.group_agent_ids = {"a", "c"}
    try:
        assert launch(store, "a", "kr-toss-desk", now=NEXT_KST_DAY).run_id
    finally:
        store.close()


# ── 시점은 그룹의 선언을 그대로 본다 ─────────────────────────────────────
def test_agent_views_share_the_groups_agent_ids(tmp_path):
    """`agent_view()` 는 `__init__` 을 부르지 않으므로, 프록시가 없으면 시점에는
    이 속성이 아예 없습니다(AttributeError) — 그러면 게이트가 시점에서 죽습니다."""
    store = StateStore(tmp_path / "state.db")
    try:
        assert store.group_agent_ids == set()
        store.group_agent_ids = {"a", "b"}
        view = store.agent_view("a")
        assert view.group_agent_ids == {"a", "b"}
        assert view.group_agent_ids is store.group_agent_ids
        view.group_agent_ids = {"a", "b", "c"}
        assert store.group_agent_ids == {"a", "b", "c"}
    finally:
        store.close()
