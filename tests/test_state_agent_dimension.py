"""state.db 의 에이전트 차원 — 두 에이전트의 durable 장부가 섞이지 않는가.

포지션·잠금·핀·하루 원장은 전부 `run_id` 로 묶여 있습니다. 그래서 에이전트마다
runs 행을 하나씩 두면 그 테이블들이 전부 저절로 갈립니다 — PK 를 건드릴 필요가
없습니다(기존 DB 에서 `CREATE TABLE IF NOT EXISTS` 는 PK 변경을 조용히 무시하고,
스키마 버전 장치도 없습니다).

**함정은 컬럼이 아니라 조회 키에 있었습니다.** `runs.agent_id` 를 적기만 하고
`resume_run` 의 WHERE 절에 넣지 않으면, 같은 전략 템플릿을 고른 두 에이전트가
`ORDER BY id DESC LIMIT 1` 에서 같은 run_id 로 수렴합니다. 그 뒤로는:

  · positions PK 가 `(run_id, symbol_key)` — 나중에 저장한 쪽이 앞사람을 덮어쓴다
  · day_budget PK 가 `(run_id, day)` — 두 에이전트의 하루 허용치가 한 행이 된다
  · 재시작하면 **둘 다 같은 100주를 복원** 하고, 그날 오후 두 손절이 함께 나가
    100주짜리 보유에 200주 매도가 떠난다

이 파일은 그 수렴이 다시 일어나지 못하게 합니다.
"""
import sqlite3
from datetime import datetime
from decimal import Decimal

import pytest

from quant.core.account import Portfolio
from quant.core.types import UTC, Symbol
from quant.live.state import StateStore

SAMSUNG = Symbol("005930", venue="toss", quote_currency="KRW",
                 lot_size=Decimal("1"), tick_size=Decimal("100"))
HYNIX = Symbol("000660", venue="toss", quote_currency="KRW",
               lot_size=Decimal("1"), tick_size=Decimal("500"))


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "state.db")
    yield s
    s.close()


def book(cash, symbol=SAMSUNG, qty=0, avg=1_000.0):
    pf = Portfolio(starting_cash=cash, base_currency="KRW")
    if qty:
        position = pf.position(symbol)
        position.quantity = Decimal(str(qty))
        position.avg_price = avg
        position.last_price = avg
    return pf


# ── 같은 전략을 두 에이전트가 쓸 때 ──────────────────────────────────────
def test_two_agents_on_the_same_strategy_get_different_runs(store):
    """이 기능을 쓰는 가장 자연스러운 방식입니다 — 같은 전략을 성향만 바꿔
    두 번 돌리는 것."""
    attack = store.start_run("momentum", "live", 50_000, agent_id="attack")
    defend = store.start_run("momentum", "live", 50_000, agent_id="defend")

    assert attack != defend


def test_each_agent_resumes_its_own_run_not_the_other_ones(store):
    """`agent_id` 를 행에만 적고 조회 키에서 빼면 여기서 같은 값이 나옵니다."""
    attack = store.start_run("momentum", "live", 50_000, agent_id="attack")
    defend = store.start_run("momentum", "live", 50_000, agent_id="defend")

    assert store.resume_run("momentum", "live", "attack") == attack
    assert store.resume_run("momentum", "live", "defend") == defend


def test_positions_do_not_overwrite_each_other(store):
    """positions PK 는 `(run_id, symbol_key)` 입니다. run_id 가 같으면 나중에
    저장한 쪽이 앞사람의 포지션을 조용히 덮어씁니다."""
    store.start_run("momentum", "live", 50_000, agent_id="attack")
    store.snapshot_positions(book(30_000, SAMSUNG, qty=10))

    store.start_run("momentum", "live", 50_000, agent_id="defend")
    store.snapshot_positions(book(20_000, SAMSUNG, qty=7))

    store.resume_run("momentum", "live", "attack")
    restored = Portfolio(starting_cash=0, base_currency="KRW")
    store.restore_positions(restored, {SAMSUNG.key: SAMSUNG})
    assert restored.quantity(SAMSUNG) == Decimal("10"), "보수형이 공격형을 덮어썼습니다"

    store.resume_run("momentum", "live", "defend")
    restored2 = Portfolio(starting_cash=0, base_currency="KRW")
    store.restore_positions(restored2, {SAMSUNG.key: SAMSUNG})
    assert restored2.quantity(SAMSUNG) == Decimal("7")


def test_a_restart_does_not_hand_both_agents_the_same_shares(store, tmp_path):
    """비평이 지적한 구체적 사고: 재시작 후 둘 다 같은 100주를 복원하고,
    그날 오후 두 손절이 함께 나가 100주에 200주 매도가 떠난다."""
    store.start_run("momentum", "live", 50_000, agent_id="attack")
    store.snapshot_positions(book(0, SAMSUNG, qty=100))
    store.start_run("momentum", "live", 50_000, agent_id="defend")
    store.snapshot_positions(book(50_000))          # 보수형은 아무것도 없다
    store.close()

    again = StateStore(tmp_path / "state.db")
    try:
        again.resume_run("momentum", "live", "defend")
        defend_book = Portfolio(starting_cash=0, base_currency="KRW")
        again.restore_positions(defend_book, {SAMSUNG.key: SAMSUNG})
        assert defend_book.quantity(SAMSUNG) == 0, (
            "보수형이 공격형의 100주를 자기 것으로 복원했습니다 — "
            "다음 손절이 그것을 팝니다"
        )
    finally:
        again.close()


def test_daily_budgets_are_separate_rows(store):
    """day_budget PK 는 `(run_id, day)` 입니다. run_id 가 같으면 두 에이전트의
    하루 허용치가 한 행으로 합쳐지고, 살아남은 행을 둘 다 복원합니다."""
    from quant.live.limits import TradingBudget

    now = datetime(2026, 3, 3, 4, 0, tzinfo=UTC)

    store.start_run("momentum", "live", 50_000, agent_id="attack")
    attack_budget = TradingBudget(max_daily_orders=10)
    ledger = attack_budget.roll(now, equity=50_000)
    ledger.orders = 7
    store.save_budget(attack_budget)

    store.start_run("momentum", "live", 50_000, agent_id="defend")
    defend_budget = TradingBudget(max_daily_orders=10)
    ledger2 = defend_budget.roll(now, equity=50_000)
    ledger2.orders = 1
    store.save_budget(defend_budget)

    store.resume_run("momentum", "live", "attack")
    restored = TradingBudget(max_daily_orders=10)
    store.restore_budget(restored, now)
    assert restored.today.orders == 7, "보수형의 원장이 공격형에 복원됐습니다"


def test_locks_are_scoped_to_the_agent(store):
    """공격형이 손절 후 걸어 둔 재진입 금지가 보수형까지 묶으면 안 됩니다."""
    store.start_run("momentum", "live", 50_000, agent_id="attack")
    until = datetime(2026, 3, 10, tzinfo=UTC)
    store.save_locks({SAMSUNG.key: (until, "stop_loss")})

    store.start_run("momentum", "live", 50_000, agent_id="defend")
    store.resume_run("momentum", "live", "defend")
    assert store.restore_locks(datetime(2026, 3, 4, tzinfo=UTC)) == {}

    store.resume_run("momentum", "live", "attack")
    assert SAMSUNG.key in store.restore_locks(datetime(2026, 3, 4, tzinfo=UTC))


# ── 서로 다른 전략을 쓸 때도 갈린다 ──────────────────────────────────────
def test_different_strategies_still_separate(store):
    attack = store.start_run("momentum", "live", 50_000, agent_id="attack")
    defend = store.start_run("meanrev", "live", 50_000, agent_id="defend")

    assert store.resume_run("momentum", "live", "attack") == attack
    assert store.resume_run("meanrev", "live", "defend") == defend
    assert store.resume_run("momentum", "live", "defend") is None


def test_the_same_agent_id_on_two_strategies_does_not_cross(store):
    """에이전트 id 는 전략 안에서가 아니라 전략과 함께 키를 이룹니다."""
    store.start_run("momentum", "live", 50_000, agent_id="a1")
    momentum = store.run_id
    store.start_run("meanrev", "live", 50_000, agent_id="a1")

    assert store.resume_run("momentum", "live", "a1") == momentum


# ── 기존 사용자를 깨뜨리지 않는다 ────────────────────────────────────────
def test_a_run_without_an_agent_resumes_exactly_as_before(store):
    """그룹을 쓰지 않는 사람의 경로는 정확히 그대로여야 합니다."""
    run = store.start_run("kr-toss-desk", "live", 800_000)
    assert store.resume_run("kr-toss-desk", "live") == run


def test_a_legacy_database_gains_the_column_and_still_resumes(tmp_path):
    """`agent_id` 이전에 만들어진 상태 DB 를 열었을 때.

    마이그레이션이 기존 행을 빈 문자열로 채우므로, 그룹을 쓰지 않는 재개
    경로는 마이그레이션 전과 정확히 같은 행을 찾습니다.
    """
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript("""
        CREATE TABLE runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          strategy TEXT NOT NULL, mode TEXT NOT NULL,
          started_at TEXT NOT NULL, stopped_at TEXT,
          starting_cash REAL NOT NULL, config_json TEXT
        );
        INSERT INTO runs(strategy, mode, started_at, starting_cash, config_json)
        VALUES('kr-toss-desk', 'live', '2026-03-01T00:00:00+00:00', 800000, '{}');
    """)
    legacy.commit()
    legacy.close()

    store = StateStore(path)
    try:
        columns = {r[1] for r in store.conn.execute("PRAGMA table_info(runs)")}
        assert "agent_id" in columns
        assert store.resume_run("kr-toss-desk", "live") == 1
    finally:
        store.close()


def test_an_agent_cannot_resume_a_legacy_single_bot_run(store):
    """빈 문자열은 "에이전트 개념이 없던 실행" 입니다.

    그것을 어느 에이전트가 이어받으면, 그 에이전트 하나가 예전 단일 봇의
    포지션 전부를 자기 슬리브로 주장하게 됩니다.
    """
    store.start_run("kr-toss-desk", "live", 800_000)
    assert store.resume_run("kr-toss-desk", "live", "attack") is None
