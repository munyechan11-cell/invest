"""같은 전략 템플릿을 쓰는 두 에이전트의 격리 실행 — 둘 다 보관할 수 있는가.

`reconciliation_run` 과 `archive_reconciliation_run` 은 `WHERE strategy=? AND
mode=? ORDER BY id DESC LIMIT 1` 로 **전략의 최신 실행 하나** 만 봤고, 보관은
그 행이 요청한 run_id 와 같을 것을 요구했습니다. 같은 템플릿을 고른 두
에이전트가 함께 죽으면 run 1(a)·run 2(b) 가 모두 격리되는데, 2 를 보관하고
나면 1 은 영원히 "최신 실행과 다릅니다" 로 거절됩니다. 계좌 게이트는 1 을
`blocking_run_id` 로 계속 들고 있어, DB 를 직접 고치기 전에는 이 계좌에서
어떤 실거래도 다시 시작할 수 없습니다.

고친 뒤의 계약:

- `reconciliation_run` 은 그 전략에 **보관되지 않은 격리 실행** 이 있으면 그중
  최신 것을 돌려줍니다(복구 카드가 남은 실행을 차례로 보여 줍니다). 없으면
  전과 같이 lifecycle head 입니다.
- `archive_reconciliation_run` 은 요청한 `run_id` 행을 **id 로** 찾고, 그 행이
  같은 전략·모드의 격리된 미보관 Toss 실거래 실행일 때만 보관합니다. head 여야
  한다는 조건만 사라졌고, 나머지(저장 설정 검증·문구·감사 기록·다음 KST
  시작 시각·삭제 없음)는 그대로입니다.
"""
from datetime import datetime

import pytest

from quant.core.types import UTC
from quant.live.state import (
    RECOVERY_ACKNOWLEDGEMENT_PHRASE,
    RECOVERY_CONFIRMATION_PHRASES,
    RecoveryArchiveError,
    StateStore,
)
from tests.test_reconciliation_archive import (
    api_payload,
    archive,
    recovery_api,  # noqa: F401 — pytest 는 모듈 이름공간의 fixture 를 찾습니다
    register,
    toss_live_config,
)

ARCHIVED_AT = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)
NEXT_KST_DAY = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)


def seed_two_agents_on_one_template(path, config, *, clean=()) -> dict[str, int]:
    """a·b 가 같은 템플릿으로 뜬 뒤 프로세스가 죽은 DB.

    `clean` 에 든 에이전트만 정상 종료를 증명했습니다. 나머지는 격리된 채입니다.
    """
    store = StateStore(path)
    try:
        runs = {}
        for agent_id in ("a", "b"):
            view = store.agent_view(agent_id)
            view.prepare_toss_live_run(
                config.name, 25_000, config.model_dump_json(),
                now=ARCHIVED_AT,
            )
            view.mark_reconciliation_required()
            if agent_id in clean:
                view.stop_run()
            runs[agent_id] = view.run_id
        return runs
    finally:
        store.close()


def quarantined_ids(store) -> list[int]:
    return [int(r["id"]) for r in store.conn.execute(
        "SELECT id FROM runs WHERE requires_reconciliation=1 "
        "AND archived_at IS NULL ORDER BY id")]


# ── 둘 다, 어느 순서로든 ─────────────────────────────────────────────────
@pytest.mark.parametrize("order", [("b", "a"), ("a", "b")])
def test_both_quarantined_runs_can_be_archived_in_either_order(tmp_path, order):
    path = tmp_path / "state.db"
    config = toss_live_config()
    runs = seed_two_agents_on_one_template(path, config)

    store = StateStore(path)
    try:
        for agent_id in order:
            result = archive(store, runs[agent_id], config, now=ARCHIVED_AT)
            assert result["archived"] and not result["idempotent"]
            assert result["run_id"] == runs[agent_id]
            assert result["next_start_allowed_at"] == NEXT_KST_DAY.isoformat()
        assert quarantined_ids(store) == []
        # 보관은 삭제가 아닙니다 — 두 실행과 두 감사 기록이 그대로 남습니다.
        assert store.conn.execute(
            "SELECT COUNT(*) n FROM runs").fetchone()["n"] == 2
        assert store.conn.execute(
            "SELECT COUNT(*) n FROM run_recovery_audit").fetchone()["n"] == 2

        # 계좌 게이트: 격리는 풀렸고, 보관한 날이 끝나면 새 시작이 열립니다.
        blocked = store.toss_account_start_gate(now=ARCHIVED_AT)
        assert not blocked["reconciliation_required"]
        assert blocked["blocking_run_id"] is None
        assert blocked["archive_cooldown_blocked"]
        assert blocked["next_start_allowed_at"] == NEXT_KST_DAY.isoformat()
        cleared = store.toss_account_start_gate(now=NEXT_KST_DAY)
        assert not cleared["restart_blocked"]
        assert store.start_run(
            config.name, "live", 25_000, config.model_dump_json(),
            now=NEXT_KST_DAY, agent_id="a",
        ) > max(runs.values())
    finally:
        store.close()


def test_the_recovery_card_cycles_through_the_remaining_quarantined_runs(
        tmp_path):
    """`reconciliation_run` 은 복구 카드가 보여 줄 실행입니다.

    head(run 2) 를 보관한 뒤에도 run 1 이 격리돼 있으면 카드는 run 1 을 보여야
    합니다 — 그렇지 않으면 카드는 "복구할 것 없음" 인데 시작은 run 1 때문에
    거절되는, 빠져나갈 수 없는 화면이 됩니다.
    """
    path = tmp_path / "state.db"
    config = toss_live_config()
    runs = seed_two_agents_on_one_template(path, config)

    store = StateStore(path)
    try:
        shown = store.reconciliation_run(config.name, "live")
        assert (shown["id"], shown["required"]) == (runs["b"], True)

        archive(store, runs["b"], config, now=ARCHIVED_AT)
        shown = store.reconciliation_run(config.name, "live")
        assert (shown["id"], shown["required"]) == (runs["a"], True)
        # 카드가 보여 주는 실행과 게이트가 막는 실행이 같은 것이어야 합니다.
        assert store.toss_account_start_gate(
            now=ARCHIVED_AT)["blocking_run_id"] == runs["a"]

        archive(store, runs["a"], config, now=ARCHIVED_AT)
        shown = store.reconciliation_run(config.name, "live")
        assert shown["id"] == runs["b"], "격리가 없으면 전처럼 head 입니다"
        assert not shown["required"] and shown["archived_at"]
    finally:
        store.close()


def test_a_quarantined_run_behind_a_clean_sibling_head_is_still_recoverable(
        tmp_path):
    """a 는 죽고 b 는 정상 종료한 경우 — head 는 깨끗한데 계좌는 격리 상태.

    이전에는 카드가 head(b) 를 보여 "복구 필요 없음" 이라 하면서 게이트는
    a 때문에 시작을 거절했습니다.
    """
    path = tmp_path / "state.db"
    config = toss_live_config()
    runs = seed_two_agents_on_one_template(path, config, clean=("b",))

    store = StateStore(path)
    try:
        gate = store.toss_account_start_gate(now=ARCHIVED_AT)
        assert gate["reconciliation_required"]
        assert gate["blocking_run_id"] == runs["a"]

        shown = store.reconciliation_run(config.name, "live")
        assert (shown["id"], shown["required"]) == (runs["a"], True)

        result = archive(store, runs["a"], config, now=ARCHIVED_AT)
        assert result["run_id"] == runs["a"]
        assert not store.toss_account_start_gate(
            now=ARCHIVED_AT)["reconciliation_required"]
        # 깨끗한 형제의 실행은 손대지 않았습니다.
        row = store.conn.execute(
            "SELECT archived_at, requires_reconciliation FROM runs WHERE id=?",
            (runs["b"],)).fetchone()
        assert row["archived_at"] is None and not row["requires_reconciliation"]
    finally:
        store.close()


# ── head 조건만 사라졌고, 나머지 검사는 그대로 ────────────────────────────
def test_archiving_a_non_head_run_is_still_idempotent_and_conflict_checked(
        tmp_path):
    path = tmp_path / "state.db"
    config = toss_live_config()
    runs = seed_two_agents_on_one_template(path, config)

    store = StateStore(path)
    try:
        first = archive(store, runs["a"], config, now=ARCHIVED_AT)
        again = archive(store, runs["a"], config, now=ARCHIVED_AT)
        assert not first["idempotent"] and again["idempotent"]
        assert again["archived_at"] == first["archived_at"]
        assert store.conn.execute(
            "SELECT COUNT(*) n FROM run_recovery_audit").fetchone()["n"] == 1
        with pytest.raises(RecoveryArchiveError) as conflict:
            archive(store, runs["a"], config,
                    reason="다른 사유로 덮어쓰려고 했습니다", now=ARCHIVED_AT)
        assert conflict.value.code == "reconciliation_archive_conflict"
        assert quarantined_ids(store) == [runs["b"]]
    finally:
        store.close()


def test_the_exact_run_id_must_belong_to_this_strategy_and_be_quarantined(
        tmp_path):
    """id 로 찾는다고 아무 id 나 받지 않습니다.

    다른 템플릿의 실행, 없는 실행은 "확인한 실행과 다릅니다" 이고, 안전 종료한
    실행은 전과 같이 "보관 대상이 아닙니다" 입니다.
    """
    path = tmp_path / "state.db"
    config = toss_live_config()
    other = toss_live_config("recover-toss-b")
    runs = seed_two_agents_on_one_template(path, config, clean=("a",))

    store = StateStore(path)
    try:
        with pytest.raises(RecoveryArchiveError) as wrong_strategy:
            archive(store, runs["b"], other, now=ARCHIVED_AT)
        assert wrong_strategy.value.code == "reconciliation_run_changed"

        with pytest.raises(RecoveryArchiveError) as unknown:
            archive(store, max(runs.values()) + 100, config, now=ARCHIVED_AT)
        assert unknown.value.code == "reconciliation_run_changed"

        with pytest.raises(RecoveryArchiveError) as clean:
            archive(store, runs["a"], config, now=ARCHIVED_AT)
        assert clean.value.code == "reconciliation_not_required"

        assert quarantined_ids(store) == [runs["b"]]
        assert store.conn.execute(
            "SELECT COUNT(*) n FROM run_recovery_audit").fetchone()["n"] == 0
    finally:
        store.close()


# ── 웹 registry 를 거쳐도 같은 순환 ──────────────────────────────────────
def test_registry_recovery_card_archives_both_runs_of_one_template(
        recovery_api):  # noqa: F811 — 가져온 fixture 를 인자로 받는다
    """`Registry.reconciliation_status` → 카드 → `archive_reconciliation` 순환.

    registry 는 카드에 보인 `run["id"]` 를 그대로 보관 요청에 싣습니다. 그 두
    호출이 같은 실행을 가리키는 한 registry 는 바뀔 것이 없습니다.
    """
    client, app, config = recovery_api
    owner_id, _cookie = register(client, "owner@example.com")
    runs = seed_two_agents_on_one_template(
        app.state.registry.state_path(owner_id), config)

    seen: list[int] = []
    for _ in ("first", "second"):
        status = client.get(
            "/api/trader/reconciliation", params={"config_path": "recover_toss"},
        )
        assert status.status_code == 200, status.text
        body = status.json()
        assert body["required"] and body["account_reconciliation_required"]
        shown = int(body["run"]["id"])
        assert shown == body["blocking_run_id"], (
            "카드가 보여 주는 실행과 게이트가 막는 실행이 다릅니다"
        )
        seen.append(shown)
        archived = client.post(
            "/api/trader/reconciliation/archive", json=api_payload(shown),
        )
        assert archived.status_code == 200, archived.text
        assert archived.json()["run_id"] == shown

    assert sorted(seen) == sorted(runs.values())
    after = client.get(
        "/api/trader/reconciliation", params={"config_path": "recover_toss"},
    ).json()
    assert not after["required"]
    assert not after["account_reconciliation_required"]
    assert after["blocking_run_id"] is None
    assert after["restart_blocked"]        # 보관한 날은 새 시작을 막는다


def test_archive_result_is_unchanged_in_shape(tmp_path):
    """복구 화면이 읽는 필드는 그대로입니다."""
    path = tmp_path / "state.db"
    config = toss_live_config()
    runs = seed_two_agents_on_one_template(path, config)
    store = StateStore(path)
    try:
        result = store.archive_reconciliation_run(
            run_id=runs["a"], strategy=config.name, mode="live",
            operator="user:7", reason="토스 앱과 다섯 항목을 직접 대조했습니다",
            confirmations=dict(RECOVERY_CONFIRMATION_PHRASES),
            acknowledgement=RECOVERY_ACKNOWLEDGEMENT_PHRASE, now=ARCHIVED_AT,
        )
        assert set(result) == {
            "archived", "idempotent", "run_id", "strategy", "mode",
            "archived_at", "next_start_allowed_at",
        }
    finally:
        store.close()
