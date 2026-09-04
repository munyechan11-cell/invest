"""Toss 계좌 게이트가 형제와 남을 구분하는가.

실거래 실행은 시작하자마자 `mark_reconciliation_required()` 로 crash quarantine
을 겁니다. 계좌 게이트는 그 표시가 붙은 미보관 Toss 실행을 "증권사 상태가
불확실하다" 로 읽고 새 실거래를 막습니다 — 옳은 판정입니다, 그 실행이 **남의
것일 때는.**

방금 이 프로세스가 같은 그룹으로 띄운 형제에게는 틀린 해석입니다. 그것을 남으로
보면 **실거래 에이전트가 둘 이상인 그룹은 영원히 시작하지 못합니다** — 하나가
뜨는 순간 나머지가 자기 형제에게 막힙니다.

그렇다고 격리를 무르면 안 됩니다. 이 파일은 양쪽을 함께 확인합니다.
"""
import json

import pytest

from quant.live.state import RecoveryArchiveError, StateStore


def toss_config(name="kr-toss-desk"):
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


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "state.db")
    yield s
    s.close()


def launch(store, agent_id, strategy="kr-toss-desk", quarantine=True):
    """에이전트 하나를 실거래로 띄운다 — `LiveTrader.start()` 가 하는 그대로."""
    view = store.agent_view(agent_id)
    view.prepare_toss_live_run(strategy, 25_000, toss_config(strategy))
    if quarantine:
        view.mark_reconciliation_required()
    return view


# ── 형제는 서로를 막지 않는다 ────────────────────────────────────────────
def test_a_second_live_toss_agent_can_start(store):
    """이것이 막히면 실거래 그룹은 존재할 수 없습니다."""
    launch(store, "attack")
    second = launch(store, "defend")
    assert second.run_id is not None


def test_four_live_toss_agents_can_all_start(store):
    views = [launch(store, a) for a in ("attack", "defend", "c3", "c4")]
    run_ids = [v.run_id for v in views]
    assert len(set(run_ids)) == 4, f"실행이 겹쳤습니다: {run_ids}"


def test_siblings_on_different_strategies_also_pass(store):
    launch(store, "attack", "kr-toss-desk")
    other = launch(store, "defend", "kr-toss-flow")
    assert other.run_id is not None


# ── 남의 미해결 실행은 여전히 막는다 ─────────────────────────────────────
def test_an_unresolved_run_from_a_previous_process_still_blocks(tmp_path):
    """프로세스가 죽으면서 남긴 격리는 진짜 불확실성입니다."""
    path = tmp_path / "state.db"
    first = StateStore(path)
    launch(first, "attack")
    first.close()                       # 프로세스가 죽었다

    second = StateStore(path)
    try:
        with pytest.raises(RecoveryArchiveError) as caught:
            launch(second, "defend")
        assert caught.value.code == "reconciliation_required"
    finally:
        second.close()


def test_the_same_agent_restarting_after_a_crash_is_also_blocked(tmp_path):
    """자기 자신의 미해결 실행도 남입니다 — 죽은 것은 이 프로세스가 아닙니다."""
    path = tmp_path / "state.db"
    first = StateStore(path)
    launch(first, "attack")
    first.close()

    second = StateStore(path)
    try:
        with pytest.raises(RecoveryArchiveError):
            launch(second, "attack")
    finally:
        second.close()


def test_a_single_bot_keeps_its_old_gate_behaviour(tmp_path):
    """그룹 시점만 형제로 등록합니다. 단일 봇이 등록하면 실패한 시작이 남긴
    격리가 다음 시도를 막지 못하게 됩니다."""
    path = tmp_path / "state.db"
    store = StateStore(path)
    try:
        store.prepare_toss_live_run(
            "kr-toss-desk", 800_000, toss_config())
        store.mark_reconciliation_required()

        with pytest.raises(RecoveryArchiveError) as caught:
            store.prepare_toss_live_run(
                "kr-toss-desk", 800_000, toss_config())
        assert caught.value.code == "reconciliation_required"
    finally:
        store.close()


# ── 재개 대상은 그 에이전트의 것 ─────────────────────────────────────────
def test_each_agent_resumes_its_own_toss_run(tmp_path):
    """agent_id 를 빼면 같은 전략 템플릿을 쓰는 형제의 실행이 선택되고,
    그쪽 하루 허용치를 이어받습니다."""
    path = tmp_path / "state.db"
    first = StateStore(path)
    attack = launch(first, "attack", quarantine=False)
    defend = launch(first, "defend", quarantine=False)
    attack_run, defend_run = attack.run_id, defend.run_id
    first.stop_run()                    # defend 의 실행을 정상 종료로
    first.close()

    second = StateStore(path)
    try:
        again = second.agent_view("attack")
        again.prepare_toss_live_run(
            "kr-toss-desk", 25_000, toss_config())
        assert again.run_id == attack_run, (
            f"attack 이 defend 의 실행({defend_run})을 이어받았습니다"
        )
    finally:
        second.close()


# ── 하루 한도는 여전히 계좌 전체의 것 ────────────────────────────────────
def test_the_sibling_exemption_does_not_touch_the_daily_allowance(store):
    """형제를 격리 판정에서 빼는 것과 하루 허용치에서 빼는 것은 다릅니다.

    후자를 빼면 방어선이 봇 수만큼 곱해집니다 — 이 기능이 막으려는 사고 그 자체.
    """
    from quant.live.limits import TradingBudget

    attack = launch(store, "attack")
    budget = TradingBudget(max_daily_orders=10, max_daily_loss=50_000)
    ledger = budget.roll(equity=25_000)
    ledger.orders = 4
    ledger.notional = 900_000
    attack.save_budget(budget)

    rows = store.conn.execute(
        "SELECT run_id, orders FROM day_budget").fetchall()
    assert [(r["run_id"], r["orders"]) for r in rows] == [(attack.run_id, 4)], (
        "형제의 사용량이 원장에서 사라졌습니다"
    )
