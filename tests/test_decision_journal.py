"""판단 일지 — 데스크의 회고와 인사이트 채점이 재시작을 넘기는가.

두 기계 모두 만들어져 있었지만 메모리에만 살았습니다. 데스크의 캘리브레이션
한 줄은 확신 0.7 이상 판단이 4건 채점돼야 나오고, 알파 모델 판정은 20건이
채점돼야 나옵니다 — 일봉에 4종목 후보라면 몇 주치입니다. 배포 한 번이면 그
카운터가 0으로 돌아갔으니, 제품에서는 사실상 한 번도 발화한 적이 없습니다.

여기서 검증하는 것은 세 가지입니다.

* 대기 중인 판단·채점 결과·모델별 점수가 재시작 뒤에도 그대로 있는가.
* 채점이 벤치마크 대비인가 (상승장에서는 모든 매수가 맞아 보입니다).
* 채점하지 못한 판단이 조용히 사라지지 않는가 — 유니버스에서 빠진 종목은
  무작위 절반이 아니라, 대체로 틀린 쪽입니다.
"""
import asyncio
import sqlite3
from datetime import datetime, timedelta

import pytest

from quant.alpha.attribution import InsightLedger
from quant.alpha.desk import DeskDecision, DeskMemory, TradingDesk
from quant.alpha.llm_client import LLMUsage
from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, Bar, Direction, Insight, RunMode, Symbol
from quant.live.state import JOURNAL_VERSION, StateStore

SYM = Symbol("005930", venue="kis", quote_currency="KRW")
OTHER = Symbol("000660", venue="kis", quote_currency="KRW")
BENCH = Symbol("069500", venue="kis", quote_currency="KRW")      # KODEX 200
OTHER_BENCH = Symbol("229200", venue="kis", quote_currency="KRW")  # KODEX 코스닥150
GHOST = Symbol("999999", venue="kis", quote_currency="KRW")      # 시세가 없는 종목
T0 = datetime(2024, 6, 3, tzinfo=UTC)
DAYS = 40


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "state.db")


def make_ctx(prices, *, universe=None, benchmark=None, now=None, days=DAYS):
    """A context whose named symbols ramp linearly from a start to an end price."""
    pf = Portfolio(10_000_000.0, "KRW")
    ctx = Context(SimClock(now or T0 + timedelta(days=days)), pf, EventBus(),
                  timeframe="1d", run_mode=RunMode.DRY_RUN)
    ctx.universe = list(universe if universe is not None else prices)
    ctx.benchmark = benchmark
    for symbol, (start, end) in prices.items():
        for i in range(days):
            p = start + (end - start) * (i / (days - 1))
            ctx.push_bar(Bar(symbol, T0 + timedelta(days=i), p, p, p, p, 1e6, "1d"))
    return ctx


def store_for(db_path, strategy="journal", mode="live"):
    """Open the state DB the way a restart does — resume if a run is there."""
    store = StateStore(db_path)
    if store.resume_run(strategy, mode) is None:
        store.start_run(strategy, mode, 10_000_000.0)
    return store


def call(*, action="buy", at=T0, price=70_000.0, conviction=0.85, horizon=1,
         symbol=SYM, benchmark_key="", benchmark_price=0.0):
    return DeskDecision(
        symbol_key=symbol.key, ticker=symbol.ticker, decided_at=at, action=action,
        conviction=conviction, price_at_decision=price, horizon_bars=horizon,
        benchmark_key=benchmark_key, benchmark_price=benchmark_price,
        rationale="외국인 순매수 연속", invalidation="20일선 종가 이탈",
    )


def count(store, table):
    return store.conn.execute(f"SELECT count(*) AS n FROM {table} WHERE run_id=?",
                              (store.run_id,)).fetchone()["n"]


# ── 데스크 회고: 재시작 ────────────────────────────────────────────────────
def test_the_calibration_line_finally_survives_a_restart(db_path):
    """이 기능이 존재하는 이유 — 4건을 모으기 전에 프로세스가 죽습니다."""
    ctx = make_ctx({SYM: (70_000.0, 63_000.0)})            # -10%: 매수는 모두 오답

    first = store_for(db_path)
    memory = DeskMemory()
    first.restore_desk_memory(memory)                      # 여기서 저장소에 묶입니다
    for i in range(4):
        memory.record(call(at=T0 + timedelta(days=i)))
    assert len(memory.settle(ctx)) == 4
    first.close()

    second = store_for(db_path)
    restored = DeskMemory()
    assert second.restore_desk_memory(restored) == 4
    text = restored.lessons_for(SYM)
    assert "캘리브레이션" in text, "재시작이 캘리브레이션 표본을 0으로 되돌렸습니다"
    assert "낮춰" in text
    assert restored.stats["scored"] == 4
    assert restored.stats["hit_rate"] == 0.0
    second.close()


def test_an_open_call_is_still_open_after_the_restart(db_path):
    """보유기간이 남은 판단까지 잃으면 표본은 채워지지 않습니다."""
    first = store_for(db_path)
    memory = DeskMemory()
    first.restore_desk_memory(memory)
    memory.record(call(horizon=10, price=70_000.0))
    assert count(first, "desk_pending") == 1
    first.close()

    second = store_for(db_path)
    restored = DeskMemory()
    second.restore_desk_memory(restored)
    assert restored.stats["pending"] == 1

    ctx = make_ctx({SYM: (70_000.0, 77_000.0)})            # 보유기간이 지난 뒤
    lessons = restored.settle(ctx)
    assert len(lessons) == 1
    assert lessons[0].entry_price == 70_000.0              # 오늘 값이 아니라 판단 시점 값
    assert lessons[0].realised_pct == pytest.approx(10.0, abs=1e-6)
    assert count(second, "desk_pending") == 0              # 채점됐으니 대기열에서 빠짐
    second.close()


def test_the_same_call_cannot_enter_the_sample_twice(db_path):
    """채점 직후 죽으면 재시작이 같은 판단을 다시 채점합니다 — 표본은 하나여야 합니다."""
    ctx = make_ctx({SYM: (70_000.0, 77_000.0)})

    store = store_for(db_path)
    memory = DeskMemory()
    store.restore_desk_memory(memory)
    memory.record(call(horizon=1))
    memory.settle(ctx)
    assert count(store, "desk_lessons") == 1

    # 커밋 직전에 죽은 프로세스: 대기열이 그대로인 사본이 같은 판단을 다시 채점합니다.
    twin = DeskMemory()
    twin.load_state({"pending": [{
        "symbol_key": SYM.key, "ticker": SYM.ticker, "decided_at": T0.isoformat(),
        "action": "buy", "conviction": 0.85, "price_at_decision": 70_000.0,
        "horizon_bars": 1, "benchmark_key": "", "benchmark_price": 0.0,
        "rationale": "외국인 순매수 연속", "invalidation": "20일선 종가 이탈"}],
        "lessons": [], "unscored": 0})
    twin.bind_store(store)
    twin.settle(ctx)
    assert count(store, "desk_lessons") == 1, "같은 판단이 표본에 두 번 들어갔습니다"
    store.close()


def test_the_journal_is_written_on_every_call_not_on_a_timer(db_path):
    store = store_for(db_path)
    memory = DeskMemory()
    store.restore_desk_memory(memory)
    memory.record(call())
    assert count(store, "desk_pending") == 1, "명시적 저장 없이는 기록되지 않았습니다"
    store.close()


def test_a_broken_store_never_breaks_the_trading_path(db_path):
    """저장 실패는 시끄럽게 알리되, 매매를 멈추지는 않습니다."""

    class Broken:
        def save_desk_memory(self, memory):
            raise sqlite3.OperationalError("database is locked")

    memory = DeskMemory()
    memory.bind_store(Broken())
    memory.record(call())                                  # 예외가 새어나오면 실패
    assert memory.stats["pending"] == 1
    assert memory._store is None, "실패한 저장소를 계속 붙들고 있습니다"


# ── 벤치마크 대비 채점 ─────────────────────────────────────────────────────
def test_a_call_that_lagged_a_rising_index_is_not_scored_as_a_hit(db_path):
    """상승장에서는 모든 매수가 맞아 보입니다 — 그 적중률은 지수의 것입니다."""
    ctx = make_ctx({SYM: (70_000.0, 73_500.0),             # +5%
                    BENCH: (30_000.0, 33_000.0)},          # +10%
                   universe=[SYM], benchmark=BENCH)
    memory = DeskMemory()
    memory.record(call(benchmark_key=BENCH.key, benchmark_price=30_000.0))
    lesson = memory.settle(ctx)[0]

    assert lesson.realised_pct == pytest.approx(5.0, abs=1e-6)
    assert lesson.benchmark_pct == pytest.approx(10.0, abs=1e-6)
    assert lesson.excess_pct == pytest.approx(-5.0, abs=1e-6)
    assert lesson.correct is False
    assert lesson.benchmark_key == BENCH.key               # 어떤 지수였는지 남습니다
    assert "벤치" in memory.lessons_for(SYM)


def test_a_call_that_beat_a_falling_index_is_a_hit(db_path):
    ctx = make_ctx({SYM: (70_000.0, 70_700.0),             # +1%
                    BENCH: (30_000.0, 29_400.0)},          # -2%
                   universe=[SYM], benchmark=BENCH)
    memory = DeskMemory()
    memory.record(call(benchmark_key=BENCH.key, benchmark_price=30_000.0))
    lesson = memory.settle(ctx)[0]
    assert lesson.excess_pct == pytest.approx(3.0, abs=1e-6)
    assert lesson.correct is True


def test_a_swapped_benchmark_scores_raw_rather_than_wrong(db_path):
    """운영 중에 벤치마크를 바꾸면 옛 진입가는 다른 지수의 것입니다."""
    ctx = make_ctx({SYM: (70_000.0, 73_500.0),
                    OTHER_BENCH: (12_000.0, 18_000.0)},
                   universe=[SYM], benchmark=OTHER_BENCH)
    memory = DeskMemory()
    memory.record(call(benchmark_key=BENCH.key, benchmark_price=30_000.0))
    lesson = memory.settle(ctx)[0]

    assert lesson.benchmark_pct == 0.0
    assert lesson.excess_pct == pytest.approx(5.0, abs=1e-6)
    assert lesson.benchmark_key == "", "다른 지수 대비 수치가 벤치마크로 기록됐습니다"
    assert lesson.correct is True


def test_an_unpriced_benchmark_is_not_stamped_on_the_call():
    """설정만 되고 시세가 없는 벤치마크는 평평한 선입니다 — 지수인 척하면 안 됩니다."""
    ctx = make_ctx({SYM: (70_000.0, 73_500.0)}, universe=[SYM], benchmark=BENCH)
    assert ctx.price(BENCH) == 0.0
    memory = DeskMemory()
    memory.record(call(benchmark_key=BENCH.key, benchmark_price=0.0))
    lesson = memory.settle(ctx)[0]
    assert lesson.benchmark_key == ""
    assert lesson.benchmark_pct == 0.0


# ── 채점하지 못한 판단 ─────────────────────────────────────────────────────
def test_a_call_on_a_name_that_left_the_universe_is_still_scored():
    """유니버스에서 빠졌다고 조용히 버리면 표본이 생존 편향에 걸립니다."""
    ctx = make_ctx({SYM: (70_000.0, 73_500.0), OTHER: (100_000.0, 60_000.0)},
                   universe=[SYM])                        # OTHER 는 이미 빠졌습니다
    memory = DeskMemory()
    memory.record(call(symbol=OTHER, price=100_000.0))
    lessons = memory.settle(ctx)

    assert len(lessons) == 1, "유니버스에서 빠진 종목의 판단이 사라졌습니다"
    assert lessons[0].realised_pct == pytest.approx(-40.0, abs=1e-6)
    assert lessons[0].correct is False
    assert memory.stats["unscored"] == 0


def test_an_unpriceable_call_is_held_then_dropped_out_loud(db_path):
    ctx = make_ctx({SYM: (70_000.0, 73_500.0)}, universe=[SYM],
                   now=T0 + timedelta(days=2))
    store = store_for(db_path)
    memory = DeskMemory(settle_grace_bars=5)
    store.restore_desk_memory(memory)
    memory.record(call(symbol=GHOST, price=1_000.0, horizon=1))

    assert memory.settle(ctx) == []
    assert memory.stats["pending"] == 1, "시세가 늦을 뿐인 판단을 곧바로 버렸습니다"

    ctx.clock.set(T0 + timedelta(days=DAYS))
    assert memory.settle(ctx) == []
    assert memory.stats["pending"] == 0
    assert memory.stats["unscored"] == 1
    store.close()

    second = store_for(db_path)
    restored = DeskMemory()
    second.restore_desk_memory(restored)
    assert restored.stats["unscored"] == 1, "표본이 조건부라는 사실이 재시작에 지워졌습니다"
    second.close()


# ── 스키마 이행 ────────────────────────────────────────────────────────────
def test_a_database_written_before_this_change_still_opens(db_path):
    """추가만 하는 이행 — 기존 사용자의 파일은 그대로 열려야 합니다."""
    old = sqlite3.connect(db_path)
    old.executescript(
        "CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT "
        "NOT NULL, mode TEXT NOT NULL, started_at TEXT NOT NULL, stopped_at TEXT, "
        "starting_cash REAL NOT NULL, config_json TEXT);"
    )
    old.execute("INSERT INTO runs(strategy, mode, started_at, starting_cash) "
                "VALUES('journal','live',?,?)", (T0.isoformat(), 10_000_000.0))
    old.commit()
    old.close()

    store = StateStore(db_path)
    assert store.resume_run("journal", "live") == 1, "이전 실행 기록을 잃었습니다"
    memory, ledger = DeskMemory(), InsightLedger()
    assert store.restore_journal(ledger=ledger, memory=memory,
                                 symbols={SYM.key: SYM}) == {"desk": 0, "insights": 0}
    memory.record(call())
    assert count(store, "desk_pending") == 1
    store.close()


def test_a_journal_from_a_newer_build_is_neither_read_nor_overwritten(db_path):
    """잘못 읽은 회고는 틀린 교훈을 확신을 담아 가르칩니다."""
    first = store_for(db_path)
    memory = DeskMemory()
    first.restore_desk_memory(memory)
    memory.record(call(horizon=10))
    first.conn.execute("UPDATE journal_meta SET version=? WHERE run_id=?",
                       (JOURNAL_VERSION + 1, first.run_id))
    first.conn.commit()
    first.close()

    second = store_for(db_path)
    restored = DeskMemory()
    assert second.restore_desk_memory(restored) == 0
    assert restored.stats["pending"] == 0
    restored.record(call(at=T0 + timedelta(days=1)))
    assert count(second, "desk_pending") == 1, "더 새로운 형식의 기록을 덮어썼습니다"
    second.close()


def test_a_failed_write_leaves_the_previous_journal_standing(db_path):
    """반쯤 쓰인 일지가 캘리브레이션을 오염시키면 안 됩니다."""

    class FlakyConn:
        """디스크가 중간에 차는 상황 — n번째 executemany 에서 실패합니다."""

        def __init__(self, conn, fail_on):
            self._conn, self._fail_on, self._n = conn, fail_on, 0

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def executemany(self, sql, rows):
            self._n += 1
            if self._n == self._fail_on:
                raise sqlite3.OperationalError("database or disk is full")
            return self._conn.executemany(sql, rows)

    store = store_for(db_path)
    memory = DeskMemory()
    store.restore_desk_memory(memory)
    memory.record(call(horizon=10))
    memory.record(call(at=T0 + timedelta(days=1), horizon=10))
    assert count(store, "desk_pending") == 2

    healthy = store.conn
    store.conn = FlakyConn(healthy, fail_on=2)             # 회고 INSERT 에서 실패
    with pytest.raises(sqlite3.OperationalError):
        store.save_desk_memory(memory)
    store.conn = healthy
    assert count(store, "desk_pending") == 2, "삭제만 남고 재삽입이 사라졌습니다"
    store.close()


# ── 인사이트 채점 원장 ─────────────────────────────────────────────────────
def insights(source, direction, n, ctx, period_days=3):
    return [Insight(symbol=SYM, direction=direction,
                    period=timedelta(days=period_days), generated_at=ctx.now,
                    confidence=0.6, source=source, tag=f"{source} {i}")
            for i in range(n)]


def scored_ledger(ctx, benchmark=BENCH):
    """A ledger with twenty settled calls from each of two alpha models."""
    ledger = InsightLedger(benchmark=benchmark)
    ledger.record(ctx, insights("alpha_up", Direction.UP, 20, ctx))
    ledger.record(ctx, insights("alpha_down", Direction.DOWN, 20, ctx))
    ctx.clock.set(T0 + timedelta(days=DAYS))
    ledger.settle(ctx)
    return ledger


def test_source_scores_survive_a_restart(db_path):
    """모델 판정에는 20건이 필요합니다 — 재시작마다 0이면 영영 나오지 않습니다."""
    ctx = make_ctx({SYM: (70_000.0, 77_000.0), BENCH: (30_000.0, 30_600.0)},
                   universe=[SYM], benchmark=BENCH, now=T0 + timedelta(days=10))
    first = store_for(db_path)
    ledger = scored_ledger(ctx)
    assert ledger.worst_source == "alpha_down"
    first.save_insight_ledger(ledger)
    assert count(first, "insight_scores") == 40
    first.close()

    second = store_for(db_path)
    restored = InsightLedger(benchmark=BENCH)
    assert second.restore_insight_ledger(restored, {SYM.key: SYM}) == 40
    assert restored.worst_source == "alpha_down"
    before, after = ledger.report()["by_source"], restored.report()["by_source"]
    assert after["alpha_down"] == before["alpha_down"]
    assert after["alpha_up"] == before["alpha_up"]
    second.close()


def test_an_open_insight_settles_at_its_original_entry_price(db_path):
    ctx = make_ctx({SYM: (70_000.0, 77_000.0), BENCH: (30_000.0, 33_000.0)},
                   universe=[SYM], benchmark=BENCH, now=T0 + timedelta(days=10))
    entry, bench_entry = ctx.price(SYM), ctx.price(BENCH)

    first = store_for(db_path)
    ledger = InsightLedger(benchmark=BENCH)
    ledger.record(ctx, insights("alpha_up", Direction.UP, 1, ctx, period_days=20))
    first.save_insight_ledger(ledger)
    assert count(first, "insight_pending") == 1
    first.close()

    second = store_for(db_path)
    restored = InsightLedger(benchmark=BENCH)
    second.restore_insight_ledger(restored, {SYM.key: SYM})
    ctx.clock.set(T0 + timedelta(days=DAYS))
    settled = restored.settle(ctx)

    assert len(settled) == 1
    assert settled[0].entry_price == pytest.approx(entry)
    assert settled[0].realised_pct == pytest.approx(ctx.price(SYM) / entry - 1)
    assert settled[0].benchmark_pct == pytest.approx(ctx.price(BENCH) / bench_entry - 1)
    second.close()


def test_a_swapped_benchmark_keeps_grading_the_call_on_its_own_reference(db_path):
    """운영 중에 벤치마크를 바꿔도, 이미 낸 판단은 그때의 비교 대상으로 채점됩니다."""
    ctx = make_ctx({SYM: (70_000.0, 77_000.0), BENCH: (30_000.0, 33_000.0),
                    OTHER_BENCH: (12_000.0, 18_000.0)},
                   universe=[SYM], benchmark=BENCH, now=T0 + timedelta(days=10))
    first = store_for(db_path)
    ledger = InsightLedger(benchmark=BENCH)
    ledger.record(ctx, insights("alpha_up", Direction.UP, 1, ctx, period_days=20))
    bench_entry = ctx.price(BENCH)
    first.save_insight_ledger(ledger)
    first.close()

    second = store_for(db_path)
    restored = InsightLedger(benchmark=OTHER_BENCH)
    second.restore_insight_ledger(restored, {SYM.key: SYM, BENCH.key: BENCH})
    ctx.clock.set(T0 + timedelta(days=DAYS))
    settled = restored.settle(ctx)

    assert settled[0].benchmark_pct == pytest.approx(ctx.price(BENCH) / bench_entry - 1), (
        "판단 당시 기준이 아니라 새 벤치마크로 채점했습니다")
    second.close()


def test_an_open_insight_whose_reference_is_gone_falls_back_to_the_raw_return(db_path):
    ctx = make_ctx({SYM: (70_000.0, 77_000.0), BENCH: (30_000.0, 33_000.0)},
                   universe=[SYM], benchmark=BENCH, now=T0 + timedelta(days=10))
    first = store_for(db_path)
    ledger = InsightLedger(benchmark=BENCH)
    ledger.record(ctx, insights("alpha_up", Direction.UP, 1, ctx, period_days=20))
    first.save_insight_ledger(ledger)
    first.close()

    second = store_for(db_path)
    restored = InsightLedger(benchmark=None)               # 비교 대상이 사라진 실행
    second.restore_insight_ledger(restored, {SYM.key: SYM})
    ctx.clock.set(T0 + timedelta(days=DAYS))
    settled = restored.settle(ctx)

    assert settled[0].benchmark_pct == 0.0
    assert settled[0].excess_pct == pytest.approx(settled[0].realised_pct)
    second.close()


def test_a_restart_does_not_change_what_a_korean_call_scores(db_path):
    """KRX 기준은 지수가 아니라 동일가중 동종 바스켓입니다 — 바스켓째 살아남아야 합니다."""
    peers = [Symbol(f"00000{i}", venue="kis", quote_currency="KRW") for i in range(1, 6)]
    prices = {SYM: (70_000.0, 77_000.0), BENCH: (30_000.0, 33_000.0)}
    prices.update(dict.fromkeys(peers, (10_000.0, 10_200.0)))
    ctx = make_ctx(prices, universe=[SYM, *peers], benchmark=BENCH,
                   now=T0 + timedelta(days=10))
    batch = insights("alpha_up", Direction.UP, 1, ctx, period_days=20)

    control = InsightLedger(benchmark=BENCH)               # 재시작하지 않은 대조군
    control.record(ctx, batch)
    live = InsightLedger(benchmark=BENCH)
    live.record(ctx, batch)
    assert live._pending[0].reference.startswith("동일가중"), "동종 바스켓이 아닙니다"

    first = store_for(db_path)
    first.save_insight_ledger(live)
    first.close()

    second = store_for(db_path)
    restored = InsightLedger(benchmark=BENCH)
    second.restore_insight_ledger(restored, {s.key: s for s in ctx.universe})
    ctx.clock.set(T0 + timedelta(days=DAYS))

    assert restored.settle(ctx)[0].to_dict() == control.settle(ctx)[0].to_dict()
    second.close()


def test_saving_twice_neither_duplicates_nor_loses_a_scored_row(db_path):
    ctx = make_ctx({SYM: (70_000.0, 77_000.0), BENCH: (30_000.0, 30_600.0)},
                   universe=[SYM], benchmark=BENCH, now=T0 + timedelta(days=10))
    store = store_for(db_path)
    ledger = scored_ledger(ctx)
    store.save_insight_ledger(ledger)
    store.save_insight_ledger(ledger)
    assert count(store, "insight_scores") == 40

    ledger.record(ctx, insights("alpha_up", Direction.UP, 1, ctx, period_days=1))
    ctx.clock.set(T0 + timedelta(days=DAYS + 2))
    ledger.settle(ctx)
    store.save_insight_ledger(ledger)
    assert count(store, "insight_scores") == 41
    store.close()


def test_restoring_twice_does_not_double_the_sample(db_path):
    ctx = make_ctx({SYM: (70_000.0, 77_000.0), BENCH: (30_000.0, 30_600.0)},
                   universe=[SYM], benchmark=BENCH, now=T0 + timedelta(days=10))
    store = store_for(db_path)
    store.save_insight_ledger(scored_ledger(ctx))

    restored = InsightLedger(benchmark=BENCH)
    assert store.restore_insight_ledger(restored, {SYM.key: SYM}) == 40
    assert store.restore_insight_ledger(restored, {SYM.key: SYM}) == 0
    assert restored.report()["scored"] == 40

    store.save_insight_ledger(restored)                    # 다시 저장해도 마찬가지
    assert count(store, "insight_scores") == 40
    store.close()


def test_an_insight_whose_symbol_left_the_universe_is_reported_not_hidden(db_path):
    ctx = make_ctx({SYM: (70_000.0, 77_000.0), BENCH: (30_000.0, 30_600.0)},
                   universe=[SYM], benchmark=BENCH, now=T0 + timedelta(days=10))
    store = store_for(db_path)
    ledger = InsightLedger(benchmark=BENCH)
    ledger.record(ctx, insights("alpha_up", Direction.UP, 1, ctx, period_days=20))
    store.save_insight_ledger(ledger)

    restored = InsightLedger(benchmark=BENCH)
    assert store.restore_insight_ledger(restored, {}) == 0
    assert restored.report()["pending"] == 0
    store.close()


class ScriptedLLM:
    """좌석 응답을 스키마 모양으로 흉내 냅니다 — 모델 호출도 비용도 없습니다."""

    def __init__(self):
        self.usage = LLMUsage()

    async def complete(self, system, user, schema=None):
        self.usage.add(400, 200)
        if schema is None:
            return "OK"
        props = set(schema.get("properties", {}))
        if "data_sufficient" in props:
            return {"stance": "bullish", "conviction": 0.9, "data_sufficient": True,
                    "key_points": ["scripted"]}
        if "proposed_scale" in props:
            return {"argument": "scripted", "proposed_scale": 0.6}
        if "position_scale" in props:
            return {"position_scale": 0.9, "veto": False, "reasoning": "scripted"}
        if "strategic_actions" in props:
            return {"rating": "buy", "rationale": "scripted",
                    "strategic_actions": "scale in", "conviction": 0.7}
        if "entry_style" in props:
            return {"action": "buy", "entry_style": "market_now",
                    "execution_note": "scripted", "conviction": 0.7}
        if "invalidation" in props:
            return {"action": "buy", "conviction": 0.8, "target_weight_pct": 20,
                    "expected_move_pct": 4.0, "horizon_bars": 10,
                    "rationale": "scripted", "invalidation": "20일선 종가 이탈",
                    "dissent": ""}
        return {"argument": "scripted", "conviction": 0.6}


def test_a_real_deliberation_stamps_its_benchmark_and_lands_on_disk(db_path):
    """실제 심의 경로에서도 벤치마크가 찍히고, 저장까지 이어져야 합니다."""
    ctx = make_ctx({SYM: (70_000.0, 77_000.0), BENCH: (30_000.0, 33_000.0)},
                   universe=[SYM], benchmark=BENCH)
    store = store_for(db_path)
    desk = TradingDesk(ScriptedLLM(), min_conviction=0.1)
    store.restore_desk_memory(desk.memory)

    asyncio.run(desk.on_start(ctx))
    asyncio.run(desk.update(ctx, {SYM.key: ctx.history(SYM, 1)[0]}))

    row = store.conn.execute("SELECT * FROM desk_pending WHERE run_id=?",
                             (store.run_id,)).fetchone()
    assert row is not None, "심의 결과가 저장되지 않았습니다"
    assert row["action"] == "buy"
    assert row["benchmark_key"] == BENCH.key
    assert row["benchmark_price"] == pytest.approx(ctx.price(BENCH))
    assert row["price_at_decision"] == pytest.approx(ctx.price(SYM))
    store.close()


def test_the_whole_journal_comes_back_in_one_call(db_path):
    """두 반쪽은 한 질문의 양면입니다 — 한쪽만 복원하면 표본이 어긋납니다."""
    ctx = make_ctx({SYM: (70_000.0, 77_000.0), BENCH: (30_000.0, 30_600.0)},
                   universe=[SYM], benchmark=BENCH, now=T0 + timedelta(days=10))
    first = store_for(db_path)
    memory = DeskMemory()
    first.restore_desk_memory(memory)
    memory.record(call(horizon=60))
    ledger = scored_ledger(ctx)
    first.save_journal(ledger=ledger, memory=memory)
    first.close()

    second = store_for(db_path)
    memory2, ledger2 = DeskMemory(), InsightLedger(benchmark=BENCH)
    counts = second.restore_journal(ledger=ledger2, memory=memory2,
                                    symbols={SYM.key: SYM})
    assert counts == {"desk": 1, "insights": 40}
    assert memory2.stats["pending"] == 1
    assert ledger2.worst_source == "alpha_down"
    second.close()
