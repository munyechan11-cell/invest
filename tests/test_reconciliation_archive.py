"""Explicit, owner-scoped retirement of an unresolved Toss-live run.

The recovery path never reconstructs an order.  A human compares the five
account facts in Toss, the old ledger stays intact, and the next start gets a
new run rather than silently reviving an older one.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from quant.api.server import create_app
from quant.config.schema import StrategyConfig
from quant.core.account import Portfolio
from quant.core.types import UTC
from quant.live.limits import TradingBudget
from quant.live.state import (
    RECOVERY_ACKNOWLEDGEMENT_PHRASE,
    RECOVERY_CONFIRMATION_PHRASES,
    RecoveryArchiveError,
    StateInUseError,
    StateStore,
)
from quant.live.trader import LiveTrader
from quant.webapp import accounts as accounts_module
from quant.webapp.auth_api import SESSION_COOKIE
from quant.webapp.registry import ReconciliationProblem


def toss_live_config(name: str = "recover-toss") -> StrategyConfig:
    return StrategyConfig.model_validate({
        "name": name,
        "mode": "live",
        "data": {
            "provider": "synthetic", "timeframe": "1d",
            "calendar": "always_open", "warmup_bars": 60,
        },
        "universe": {"symbols": [{
            "ticker": "005930", "venue": "toss", "quote_currency": "KRW",
        }]},
        "alpha": [{"type": "ema_cross"}],
        "broker": {"type": "toss", "live_trading_confirmed": True},
        "limits": {"max_daily_orders": 5},
    })


def toss_live_scope_config(
        name: str, *, currency: str = "KRW", timezone: float = 9,
) -> StrategyConfig:
    data = toss_live_config(name).model_dump()
    data["portfolio"]["base_currency"] = currency
    data["limits"]["timezone_offset_hours"] = timezone
    return StrategyConfig.model_validate(data)


def other_live_config(name: str, broker: str) -> StrategyConfig:
    data = toss_live_config(name).model_dump()
    data["broker"] = {
        "type": broker,
        "live_trading_confirmed": True,
    }
    return StrategyConfig.model_validate(data)


def seed_run(path, config: StrategyConfig, *, quarantined: bool = True,
             config_json: str | None = None) -> int:
    store = StateStore(path)
    try:
        run_id = store.start_run(
            config.name, config.mode.value, config.portfolio.starting_cash,
            config.model_dump_json() if config_json is None else config_json,
        )
        if quarantined:
            store.mark_reconciliation_required()
        else:
            store.stop_run()
        return run_id
    finally:
        store.close()


def archive(store: StateStore, run_id: int, config: StrategyConfig,
            *, reason: str = "토스 앱과 다섯 항목을 직접 대조했습니다",
            confirmations: dict[str, str] | None = None,
            acknowledgement: str = RECOVERY_ACKNOWLEDGEMENT_PHRASE,
            now: datetime | None = None) -> dict:
    return store.archive_reconciliation_run(
        run_id=run_id,
        strategy=config.name,
        mode=config.mode.value,
        operator="user:7",
        reason=reason,
        confirmations=(dict(RECOVERY_CONFIRMATION_PHRASES)
                       if confirmations is None else confirmations),
        acknowledgement=acknowledgement,
        now=now,
    )


def test_archive_preserves_the_old_run_and_creates_an_immutable_audit(tmp_path):
    path = tmp_path / "state.db"
    config = toss_live_config()
    run_id = seed_run(path, config)
    store = StateStore(path)
    try:
        before = dict(store.conn.execute(
            "SELECT * FROM runs WHERE id=?", (run_id,)
        ).fetchone())

        result = archive(store, run_id, config)

        assert result["archived"] and not result["idempotent"]
        after = dict(store.conn.execute(
            "SELECT * FROM runs WHERE id=?", (run_id,)
        ).fetchone())
        for key in (
            "id", "strategy", "mode", "started_at", "stopped_at",
            "requires_reconciliation", "starting_cash", "config_json",
        ):
            assert after[key] == before[key], f"archive overwrote {key}"
        assert after["archived_at"]
        assert after["archive_reason"] == "토스 앱과 다섯 항목을 직접 대조했습니다"
        assert after["archived_by"] == "user:7"

        audit = store.conn.execute(
            "SELECT * FROM run_recovery_audit WHERE run_id=?", (run_id,)
        ).fetchone()
        assert audit["archived_at"] == after["archived_at"]
        proof = json.loads(audit["confirmations_json"])
        assert proof["confirmations"] == RECOVERY_CONFIRMATION_PHRASES
        assert proof["acknowledgement"] == RECOVERY_ACKNOWLEDGEMENT_PHRASE
        events = store.conn.execute(
            "SELECT type, payload FROM events WHERE run_id=?", (run_id,)
        ).fetchall()
        assert [row["type"] for row in events] == ["reconciliation_archived"]

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.conn.execute(
                "UPDATE run_recovery_audit SET reason='changed' WHERE run_id=?",
                (run_id,),
            )
        store.conn.rollback()
    finally:
        store.close()


def test_archived_head_makes_resume_create_fresh_not_revive_an_older_run(tmp_path):
    path = tmp_path / "state.db"
    config = toss_live_config()
    store = StateStore(path)
    try:
        old_id = store.start_run(
            config.name, "live", 100.0, config.model_dump_json()
        )
        store.stop_run()
        archived_id = store.start_run(
            config.name, "live", 200.0, config.model_dump_json()
        )
        store.mark_reconciliation_required()
        archived_at = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)
        archive(store, archived_id, config, now=archived_at)

        assert store.resume_run(config.name, "live") is None
        with pytest.raises(RecoveryArchiveError) as blocked:
            store.start_run(
                config.name, "live", 300.0, config.model_dump_json(),
                now=datetime(2026, 8, 31, 14, 59, 59, tzinfo=UTC),
            )
        assert blocked.value.code == \
            "reconciliation_start_blocked_until_next_kst_day"
        fresh_id = store.start_run(
            config.name, "live", 300.0, config.model_dump_json(),
            now=datetime(2026, 8, 31, 15, 0, tzinfo=UTC),
        )
        assert fresh_id > archived_id > old_id
        assert store.conn.execute(
            "SELECT COUNT(*) n FROM runs"
        ).fetchone()["n"] == 3
        assert store.conn.execute(
            "SELECT archived_at FROM runs WHERE id=?", (old_id,)
        ).fetchone()["archived_at"] is None
    finally:
        store.close()


def test_live_trader_start_creates_a_new_run_after_archive(tmp_path):
    path = tmp_path / "state.db"
    config = toss_live_config()
    archived_id = seed_run(path, config)
    retiring = StateStore(path)
    try:
        archive(
            retiring, archived_id, config,
            now=datetime.now(UTC) - timedelta(days=2),
        )
    finally:
        retiring.close()

    trader = LiveTrader.__new__(LiveTrader)
    trader.config = config
    trader.resume = True
    trader.state = StateStore(path)
    trader.engine = SimpleNamespace(
        ctx=SimpleNamespace(
            universe=[],
            portfolio=Portfolio(config.portfolio.starting_cash),
        ),
        brokerage=object(),
        budget=TradingBudget(max_daily_orders=5),
    )
    trader._attach_observers = lambda: None

    async def warmup():
        return None

    async def engine_start():
        return None

    async def notify(_message):
        return None

    async def opening_deliberation():
        return None

    trader.warmup = warmup
    trader.engine.start = engine_start
    trader.notifier = SimpleNamespace(send=notify)
    trader._opening_deliberation = opening_deliberation
    trader.calendar = None

    try:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(trader.start())
        finally:
            loop.close()
        assert trader.state.run_id > archived_id
        assert trader.state.conn.execute(
            "SELECT archived_at FROM runs WHERE id=?", (archived_id,)
        ).fetchone()["archived_at"]
        assert trader.state.conn.execute(
            "SELECT archived_at FROM runs WHERE id=?", (trader.state.run_id,)
        ).fetchone()["archived_at"] is None
    finally:
        trader.state.close()


def test_only_the_latest_exact_run_can_be_archived(tmp_path):
    path = tmp_path / "state.db"
    config = toss_live_config()
    first = seed_run(path, config, quarantined=False)
    store = StateStore(path)
    try:
        second = store.start_run(
            config.name, "live", 200.0, config.model_dump_json()
        )
        store.mark_reconciliation_required()
        with pytest.raises(RecoveryArchiveError) as exc:
            archive(store, first, config)
        assert exc.value.code == "reconciliation_run_changed"
        assert store.conn.execute(
            "SELECT archived_at FROM runs WHERE id=?", (second,)
        ).fetchone()["archived_at"] is None
    finally:
        store.close()


def test_same_archive_is_idempotent_but_different_reason_cannot_overwrite(tmp_path):
    path = tmp_path / "state.db"
    config = toss_live_config()
    run_id = seed_run(path, config)
    store = StateStore(path)
    try:
        first = archive(store, run_id, config)
        second = archive(store, run_id, config)
        assert not first["idempotent"] and second["idempotent"]
        assert store.conn.execute(
            "SELECT COUNT(*) n FROM run_recovery_audit"
        ).fetchone()["n"] == 1
        assert store.conn.execute(
            "SELECT COUNT(*) n FROM events WHERE type='reconciliation_archived'"
        ).fetchone()["n"] == 1

        with pytest.raises(RecoveryArchiveError) as exc:
            archive(store, run_id, config, reason="다른 사유로 덮어쓰려고 했습니다")
        assert exc.value.code == "reconciliation_archive_conflict"
    finally:
        store.close()


def test_kst_midnight_gate_is_blocked_before_and_open_at_the_boundary(tmp_path):
    path = tmp_path / "state.db"
    config = toss_live_config()
    run_id = seed_run(path, config)
    store = StateStore(path)
    try:
        # 14:59:59 UTC is 23:59:59 KST. The next KST date starts one
        # second later at exactly 15:00:00 UTC.
        result = archive(
            store, run_id, config,
            now=datetime(2026, 8, 31, 14, 59, 59, tzinfo=UTC),
        )
        assert result["next_start_allowed_at"] == \
            "2026-08-31T15:00:00+00:00"

        before = store.recovery_start_gate(
            config.name, "live",
            now=datetime(2026, 8, 31, 14, 59, 59, 999999, tzinfo=UTC),
        )
        assert before["restart_blocked"]
        assert before["next_start_allowed_at"] == \
            "2026-08-31T15:00:00+00:00"

        boundary = store.recovery_start_gate(
            config.name, "live",
            now=datetime(2026, 8, 31, 15, 0, tzinfo=UTC),
        )
        assert not boundary["restart_blocked"]

        with pytest.raises(RecoveryArchiveError) as invalid:
            store.recovery_start_gate(
                config.name, "live", now=datetime(2026, 8, 31, 15, 0)
            )
        assert invalid.value.code == "reconciliation_time_invalid"
    finally:
        store.close()


def test_toss_account_gate_cannot_be_bypassed_with_another_strategy(tmp_path):
    path = tmp_path / "state.db"
    strategy_a = toss_live_config("toss-a")
    strategy_b = toss_live_config("toss-b")
    dry_b = StrategyConfig.model_validate({
        **strategy_b.model_dump(), "mode": "dry_run",
    })
    kis_live = other_live_config("kis-live", "kis")
    archived_at = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)

    run_a = seed_run(path, strategy_a)
    store = StateStore(path)
    try:
        gate = store.toss_account_start_gate(now=archived_at)
        assert gate["reconciliation_required"]
        assert gate["blocking_run_id"] == run_a
        assert gate["blocking_strategy"] == strategy_a.name

        with pytest.raises(RecoveryArchiveError) as unresolved:
            store.start_run(
                strategy_b.name, "live", 100.0,
                strategy_b.model_dump_json(), now=archived_at,
            )
        assert unresolved.value.code == "reconciliation_required"

        # The quarantine belongs to the Toss real account. A simulation and a
        # different brokerage account do not spend that account's daily budget.
        dry_id = store.start_run(
            dry_b.name, "dry_run", 100.0, dry_b.model_dump_json(),
            now=archived_at,
        )
        kis_id = store.start_run(
            kis_live.name, "live", 100.0, kis_live.model_dump_json(),
            now=archived_at,
        )
        assert kis_id > dry_id > run_a

        result = archive(store, run_a, strategy_a, now=archived_at)
        assert result["next_start_allowed_at"] == \
            "2026-08-31T15:00:00+00:00"

        with pytest.raises(RecoveryArchiveError) as same_day:
            store.start_run(
                strategy_b.name, "live", 100.0,
                strategy_b.model_dump_json(),
                now=datetime(2026, 8, 31, 14, 59, 59, tzinfo=UTC),
            )
        assert same_day.value.code == \
            "reconciliation_start_blocked_until_next_kst_day"

        next_day_id = store.start_run(
            strategy_b.name, "live", 100.0,
            strategy_b.model_dump_json(),
            now=datetime(2026, 8, 31, 15, 0, tzinfo=UTC),
        )
        assert next_day_id > kis_id
    finally:
        store.close()


def test_clean_stop_cannot_reset_the_toss_daily_loss_with_another_strategy(
        tmp_path):
    """A clean stop must not turn a strategy switch into a fresh allowance."""
    path = tmp_path / "state.db"
    strategy_a = toss_live_config("toss-a")
    strategy_b = toss_live_config("toss-b")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)

    store = StateStore(path)
    try:
        store.start_run(
            strategy_a.name, "live", 100_000.0,
            strategy_a.model_dump_json(), now=now,
        )
        budget = TradingBudget(
            max_daily_loss=1_000.0, timezone_offset_hours=9,
        )
        assert not store.restore_budget(budget, now=now)
        budget.record_trade(-1_500.0, now=now)
        store.conn.execute(
            "UPDATE day_budget SET updated_at=? WHERE run_id=?",
            (now.isoformat(), store.run_id),
        )
        store.conn.commit()
        store.stop_run()

        with pytest.raises(RecoveryArchiveError) as blocked:
            store.start_run(
                strategy_b.name, "live", 100_000.0,
                strategy_b.model_dump_json(), now=now,
            )
        assert blocked.value.code == \
            "daily_budget_strategy_switch_blocked"
    finally:
        store.close()


def test_same_toss_strategy_resumes_its_exact_daily_ledger(tmp_path):
    path = tmp_path / "state.db"
    config = toss_live_config("toss-a")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)

    first = StateStore(path)
    run_id = first.start_run(
        config.name, "live", 100_000.0, config.model_dump_json(), now=now,
    )
    budget = TradingBudget(max_daily_loss=1_000.0, timezone_offset_hours=9)
    first.restore_budget(budget, now=now)
    budget.record_trade(-1_500.0, now=now)
    first.conn.execute(
        "INSERT INTO events(run_id, ts, type, payload) VALUES(?,?,?,?)",
        (run_id, now.isoformat(), "order_submitted",
         json.dumps({"id": "fill-1"})),
    )
    first.conn.execute(
        "INSERT INTO events(run_id, ts, type, payload) VALUES(?,?,?,?)",
        (run_id, now.isoformat(), "order_filled",
         json.dumps({"order_id": "fill-1", "fee": 0})),
    )
    first.conn.execute(
        "UPDATE day_budget SET orders=1, notional=100, updated_at=? "
        "WHERE run_id=?",
        (now.isoformat(), run_id),
    )
    first.conn.commit()
    first.stop_run()
    first.close()

    resumed = StateStore(path)
    try:
        gate = resumed.assert_toss_account_start_allowed(
            resume_strategy=config.name,
            resume_config_json=config.model_dump_json(),
            now=now,
        )
        assert not gate["daily_budget_blocked"]
        assert gate["resumable_run_id"] == run_id
        assert resumed.prepare_toss_live_run(
            config.name, 100_000.0, config.model_dump_json(), now=now,
        )
        assert resumed.run_id == run_id
        restored = TradingBudget(
            max_daily_loss=1_000.0, timezone_offset_hours=9,
        )
        assert resumed.restore_budget(restored, now=now)
        assert restored.today.realized_pnl == pytest.approx(-1_500.0)
        resumed.conn.execute(
            "UPDATE day_budget SET updated_at=? WHERE run_id=?",
            (now.isoformat(), run_id),
        )
        resumed.conn.commit()

        # Only the exact resume is safe. A direct fresh run with the same name
        # would reset the ledger just as surely as changing the name.
        with pytest.raises(RecoveryArchiveError) as fresh:
            resumed.start_run(
                config.name, "live", 100_000.0,
                config.model_dump_json(), now=now,
            )
        assert fresh.value.code == "daily_budget_strategy_switch_blocked"
    finally:
        resumed.close()


def test_unused_toss_ledger_can_switch_and_used_ledger_expires_at_its_boundary(
        tmp_path):
    config_a = toss_live_scope_config("toss-a", timezone=0)
    config_b = toss_live_config("toss-b")
    now = datetime(2026, 8, 31, 20, 0, tzinfo=UTC)

    unused = StateStore(tmp_path / "unused.db")
    try:
        unused.start_run(
            config_a.name, "live", 100_000.0,
            config_a.model_dump_json(), now=now,
        )
        empty = TradingBudget(max_daily_orders=5, timezone_offset_hours=9)
        unused.restore_budget(empty, now=now)
        unused.stop_run()
        assert unused.start_run(
            config_b.name, "live", 100_000.0,
            config_b.model_dump_json(), now=now,
        )
    finally:
        unused.close()

    used = StateStore(tmp_path / "used.db")
    try:
        used.start_run(
            config_a.name, "live", 100_000.0,
            config_a.model_dump_json(), now=now,
        )
        utc_budget = TradingBudget(
            max_daily_loss=1_000.0, timezone_offset_hours=0,
        )
        used.restore_budget(utc_budget, now=now)
        utc_budget.record_trade(-1_500.0, now=now)
        used.conn.execute(
            "UPDATE day_budget SET updated_at=? WHERE run_id=?",
            (now.isoformat(), used.run_id),
        )
        used.conn.commit()
        used.stop_run()

        blocked = used.toss_account_start_gate(
            resume_strategy=config_b.name,
            resume_config_json=config_b.model_dump_json(),
            now=now,
        )
        assert blocked["daily_budget_blocked"]
        assert blocked["next_start_allowed_at"] == \
            "2026-09-01T15:00:00+00:00"

        allowed_id = used.start_run(
            config_b.name, "live", 100_000.0,
            config_b.model_dump_json(),
            now=datetime(2026, 9, 1, 15, 0, tzinfo=UTC),
        )
        assert allowed_id > 0
    finally:
        used.close()


def test_toss_daily_budget_gate_does_not_capture_dry_run_or_kis(tmp_path):
    path = tmp_path / "state.db"
    toss = toss_live_config("toss-a")
    dry = StrategyConfig.model_validate({
        **toss_live_config("dry-b").model_dump(), "mode": "dry_run",
    })
    kis = other_live_config("kis-b", "kis")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)

    store = StateStore(path)
    try:
        toss_id = store.start_run(
            toss.name, "live", 100_000.0, toss.model_dump_json(), now=now,
        )
        budget = TradingBudget(max_daily_orders=5, timezone_offset_hours=9)
        store.restore_budget(budget, now=now)
        budget.record_trade(-1.0, now=now)
        store.stop_run()

        dry_id = store.start_run(
            dry.name, "dry_run", 100_000.0, dry.model_dump_json(), now=now,
        )
        kis_id = store.start_run(
            kis.name, "live", 100_000.0, kis.model_dump_json(), now=now,
        )
        assert kis_id > dry_id > toss_id
    finally:
        store.close()


@pytest.mark.parametrize(
    ("column", "value", "code"),
    [
        ("day", "not-a-day", "daily_budget_time_invalid"),
        ("tz_offset_hours", 99.0, "daily_budget_time_invalid"),
        ("updated_at", "not-a-time", "daily_budget_time_invalid"),
        ("realized_pnl", "not-a-number", "daily_budget_value_invalid"),
        ("starting_equity", float("inf"), "daily_budget_value_invalid"),
        ("starting_equity", -1.0, "daily_budget_value_invalid"),
        ("orders", -1, "daily_budget_value_invalid"),
        ("orders", 1.5, "daily_budget_value_invalid"),
        ("notional", -100.0, "daily_budget_value_invalid"),
        ("fees", -1.0, "daily_budget_value_invalid"),
        ("blocked", -1, "daily_budget_value_invalid"),
    ],
)
def test_invalid_toss_daily_budget_fails_closed(
        tmp_path, column, value, code):
    path = tmp_path / "state.db"
    config = toss_live_config("toss-a")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)
    store = StateStore(path)
    try:
        run_id = store.start_run(
            config.name, "live", 100_000.0,
            config.model_dump_json(), now=now,
        )
        budget = TradingBudget(max_daily_loss=1_000.0, timezone_offset_hours=9)
        store.restore_budget(budget, now=now)
        budget.record_trade(-1.0, now=now)
        store.conn.execute(
            "UPDATE day_budget SET updated_at=? WHERE run_id=?",
            (now.isoformat(), run_id),
        )
        store.conn.execute(
            f"UPDATE day_budget SET {column}=? WHERE run_id=?",
            (value, run_id),
        )
        store.conn.commit()

        with pytest.raises(RecoveryArchiveError) as blocked:
            store.toss_account_start_gate(
                resume_strategy="toss-b",
                resume_config_json=toss_live_config(
                    "toss-b",
                ).model_dump_json(),
                now=now,
            )
        assert blocked.value.code == code
    finally:
        store.close()


@pytest.mark.parametrize("archive_first", [False, True])
def test_direct_live_trader_checks_the_toss_account_before_resuming(
        tmp_path, archive_first):
    path = tmp_path / "state.db"
    strategy_a = toss_live_config("toss-a")
    strategy_b = toss_live_config("toss-b")

    # B has a clean resumable head. A later crash must still stop B before its
    # own clean resume path can reach warm-up or the broker.
    seed_run(path, strategy_b, quarantined=False)
    run_a = seed_run(path, strategy_a)
    if archive_first:
        retiring = StateStore(path)
        try:
            archive(retiring, run_a, strategy_a)
        finally:
            retiring.close()

    trader = LiveTrader.__new__(LiveTrader)
    trader.config = strategy_b
    trader.resume = True
    trader.state = StateStore(path)
    calls = {"warmup": 0}

    async def warmup():
        calls["warmup"] += 1
        raise AssertionError("Toss account gate ran after warm-up")

    trader.warmup = warmup
    try:
        with pytest.raises(RecoveryArchiveError) as blocked:
            asyncio.run(trader.start())
        assert blocked.value.code == (
            "reconciliation_start_blocked_until_next_kst_day"
            if archive_first else "reconciliation_required"
        )
        assert calls["warmup"] == 0
        assert trader.state.run_id is None
    finally:
        trader.state.close()


def test_direct_live_trader_can_only_resume_the_used_strategy(
        tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    config = toss_live_config("toss-a")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)
    original_recovery_now = StateStore._recovery_now
    monkeypatch.setattr(
        StateStore, "_recovery_now",
        staticmethod(lambda value=None: (
            now if value is None else original_recovery_now(value)
        )),
    )

    seeded = StateStore(path)
    run_id = seeded.start_run(
        config.name, "live", 100_000.0, config.model_dump_json(), now=now,
    )
    budget = TradingBudget(max_daily_loss=1_000.0, timezone_offset_hours=9)
    seeded.restore_budget(budget, now=now)
    budget.record_trade(-1_500.0, now=now)
    seeded.conn.execute(
        "UPDATE day_budget SET updated_at=? WHERE run_id=?",
        (now.isoformat(), run_id),
    )
    seeded.conn.commit()
    seeded.stop_run()
    seeded.close()

    trader = LiveTrader.__new__(LiveTrader)
    trader.config = config
    trader.resume = True
    trader.state = StateStore(path)
    trader._attach_observers = lambda: None

    class ReachedWarmup(RuntimeError):
        pass

    async def warmup():
        raise ReachedWarmup

    trader.warmup = warmup
    try:
        with pytest.raises(ReachedWarmup):
            asyncio.run(trader.start())
        assert trader.state.run_id == run_id
    finally:
        trader.state.close()


@pytest.mark.parametrize(
    ("old_currency", "old_timezone", "new_currency", "new_timezone"),
    [
        ("USD", 9, "KRW", 9),
        ("KRW", 9, "KRW", 0),
    ],
)
def test_same_name_toss_scope_change_cannot_relabel_a_used_ledger(
        tmp_path, old_currency, old_timezone, new_currency, new_timezone):
    path = tmp_path / "state.db"
    name = "same-name"
    old = toss_live_scope_config(
        name, currency=old_currency, timezone=old_timezone,
    )
    target = toss_live_scope_config(
        name, currency=new_currency, timezone=new_timezone,
    )
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)

    seeded = StateStore(path)
    run_id = seeded.start_run(
        name, "live", 100_000.0, old.model_dump_json(), now=now,
    )
    budget = TradingBudget(
        max_daily_loss=1_000.0,
        timezone_offset_hours=old_timezone,
    )
    seeded.restore_budget(budget, now=now)
    budget.record_trade(-1_500.0, now=now)
    seeded.conn.execute(
        "UPDATE day_budget SET updated_at=? WHERE run_id=?",
        (now.isoformat(), run_id),
    )
    seeded.conn.commit()
    seeded.stop_run()
    seeded.close()

    contender = StateStore(path)
    try:
        gate = contender.toss_account_start_gate(
            resume_strategy=name,
            resume_config_json=target.model_dump_json(),
            now=now,
        )
        assert gate["resumable_run_id"] is None
        assert gate["daily_budget_blocked"]
        with pytest.raises(RecoveryArchiveError) as blocked:
            contender.prepare_toss_live_run(
                name, 100_000.0, target.model_dump_json(), now=now,
            )
        assert blocked.value.code == "daily_budget_strategy_switch_blocked"
        assert contender.conn.execute(
            "SELECT COUNT(*) n FROM runs",
        ).fetchone()["n"] == 1
    finally:
        contender.close()


@pytest.mark.parametrize("later_kind", ["kis", "corrupt"])
def test_later_same_name_non_toss_run_cannot_redirect_exact_resume(
        tmp_path, later_kind):
    path = tmp_path / "state.db"
    config = toss_live_scope_config("same-name")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)

    seeded = StateStore(path)
    toss_run_id = seeded.start_run(
        config.name, "live", 100_000.0, config.model_dump_json(), now=now,
    )
    budget = TradingBudget(max_daily_loss=1_000.0, timezone_offset_hours=9)
    seeded.restore_budget(budget, now=now)
    budget.record_trade(-1_500.0, now=now)
    seeded.conn.execute(
        "UPDATE day_budget SET updated_at=? WHERE run_id=?",
        (now.isoformat(), toss_run_id),
    )
    seeded.conn.commit()
    seeded.stop_run()
    later_json = (
        other_live_config(config.name, "kis").model_dump_json()
        if later_kind == "kis" else "{corrupt"
    )
    later_id = seeded.start_run(
        config.name, "live", 100_000.0, later_json, now=now,
    )
    seeded.stop_run()
    seeded.close()

    contender = StateStore(path)
    try:
        gate = contender.toss_account_start_gate(
            resume_strategy=config.name,
            resume_config_json=config.model_dump_json(),
            now=now,
        )
        assert gate["resumable_run_id"] is None
        assert gate["budget_blocking_run_id"] == toss_run_id
        with pytest.raises(RecoveryArchiveError) as blocked:
            contender.prepare_toss_live_run(
                config.name, 100_000.0, config.model_dump_json(), now=now,
            )
        assert blocked.value.code == "daily_budget_strategy_switch_blocked"
        assert contender.conn.execute(
            "SELECT id FROM runs ORDER BY id DESC LIMIT 1",
        ).fetchone()["id"] == later_id
    finally:
        contender.close()


def test_positive_fourteen_budget_stays_blocked_until_next_kst_day(
        tmp_path):
    path = tmp_path / "state.db"
    source = toss_live_scope_config("source", timezone=14)
    target = toss_live_scope_config("target", timezone=9)
    used_at = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)

    store = StateStore(path)
    source_id = store.start_run(
        source.name, "live", 100_000.0,
        source.model_dump_json(), now=used_at,
    )
    budget = TradingBudget(max_daily_orders=5, timezone_offset_hours=14)
    store.restore_budget(budget, now=used_at)
    budget.record_trade(-1.0, now=used_at)
    store.conn.execute(
        "UPDATE day_budget SET updated_at=? WHERE run_id=?",
        (used_at.isoformat(), source_id),
    )
    store.conn.commit()
    store.stop_run()

    after_source_midnight = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
    gate = store.toss_account_start_gate(
        resume_strategy=target.name,
        resume_config_json=target.model_dump_json(),
        now=after_source_midnight,
    )
    assert gate["daily_budget_blocked"]
    assert gate["next_start_allowed_at"] == "2026-08-31T15:00:00+00:00"

    new_id = store.start_run(
        target.name, "live", 100_000.0, target.model_dump_json(),
        now=datetime(2026, 8, 31, 15, 0, tzinfo=UTC),
    )
    assert new_id > source_id
    store.close()


def test_toss_prepare_claims_state_before_warmup_window(tmp_path):
    path = tmp_path / "state.db"
    first_config = toss_live_scope_config("first")
    second_config = toss_live_scope_config("second")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)

    first = StateStore(path)
    second = StateStore(path)
    try:
        assert not first.prepare_toss_live_run(
            first_config.name, 100_000.0,
            first_config.model_dump_json(), now=now,
        )
        with pytest.raises(StateInUseError):
            second.prepare_toss_live_run(
                second_config.name, 100_000.0,
                second_config.model_dump_json(), now=now,
            )
    finally:
        first.close()
        second.close()


def test_exact_resume_rejects_multiple_active_ledgers_for_one_run(tmp_path):
    path = tmp_path / "state.db"
    config = toss_live_scope_config("same-run")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)

    seeded = StateStore(path)
    run_id = seeded.start_run(
        config.name, "live", 100_000.0, config.model_dump_json(), now=now,
    )
    budget = TradingBudget(max_daily_loss=1_000.0, timezone_offset_hours=9)
    seeded.restore_budget(budget, now=now)
    budget.record_trade(-500.0, now=now)
    seeded.conn.execute(
        "UPDATE day_budget SET updated_at=? WHERE run_id=?",
        (now.isoformat(), run_id),
    )
    seeded.conn.execute(
        "INSERT INTO day_budget(run_id, day, notional, orders, realized_pnl, "
        "fees, starting_equity, blocked, halt_reason, tz_offset_hours, "
        "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, "2026-08-30", 0.0, 0, -900.0, 0.0, 0.0, 0, "", 9.0,
         now.isoformat()),
    )
    seeded.conn.commit()
    seeded.stop_run()
    seeded.close()

    contender = StateStore(path)
    try:
        gate = contender.toss_account_start_gate(
            resume_strategy=config.name,
            resume_config_json=config.model_dump_json(),
            now=now,
        )
        assert gate["resumable_run_id"] is None
        assert gate["daily_budget_blocked"]
        with pytest.raises(RecoveryArchiveError) as blocked:
            contender.prepare_toss_live_run(
                config.name, 100_000.0, config.model_dump_json(), now=now,
            )
        assert blocked.value.code == "daily_budget_strategy_switch_blocked"
    finally:
        contender.close()


@pytest.mark.parametrize(
    "corrupt_config",
    ["{broken", "name-mismatch"],
)
def test_unknown_used_live_ledger_fails_closed_for_toss_target(
        tmp_path, corrupt_config):
    path = tmp_path / "state.db"
    source = toss_live_scope_config("source")
    target = toss_live_scope_config("target")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)

    store = StateStore(path)
    run_id = store.start_run(
        source.name, "live", 100_000.0, source.model_dump_json(), now=now,
    )
    budget = TradingBudget(max_daily_loss=1_000.0, timezone_offset_hours=9)
    store.restore_budget(budget, now=now)
    budget.record_trade(-1_500.0, now=now)
    replacement = (
        "{broken" if corrupt_config == "{broken"
        else toss_live_scope_config("somebody-else").model_dump_json()
    )
    store.conn.execute(
        "UPDATE runs SET config_json=? WHERE id=?", (replacement, run_id),
    )
    store.conn.execute(
        "UPDATE day_budget SET updated_at=? WHERE run_id=?",
        (now.isoformat(), run_id),
    )
    store.conn.commit()
    store.stop_run()

    with pytest.raises(RecoveryArchiveError) as blocked:
        store.prepare_toss_live_run(
            target.name, 100_000.0, target.model_dump_json(), now=now,
        )
    assert blocked.value.code == "daily_budget_scope_invalid"
    assert store.conn.execute(
        "SELECT COUNT(*) n FROM runs",
    ).fetchone()["n"] == 1
    store.close()


def test_unknown_unresolved_live_run_fails_closed_for_toss_target(tmp_path):
    path = tmp_path / "state.db"
    source = toss_live_scope_config("source")
    target = toss_live_scope_config("target")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)

    store = StateStore(path)
    run_id = store.start_run(
        source.name, "live", 100_000.0, source.model_dump_json(), now=now,
    )
    store.mark_reconciliation_required()
    store.conn.execute(
        "UPDATE runs SET config_json='{broken' WHERE id=?", (run_id,),
    )
    store.conn.commit()

    with pytest.raises(RecoveryArchiveError) as blocked:
        store.prepare_toss_live_run(
            target.name, 100_000.0, target.model_dump_json(), now=now,
        )
    assert blocked.value.code == "reconciliation_stored_config_mismatch"
    store.close()


def test_nonscalar_broker_type_with_current_fill_fails_closed(tmp_path):
    path = tmp_path / "state.db"
    source = toss_live_scope_config("source")
    target = toss_live_scope_config("target")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)
    raw = source.model_dump()
    raw["broker"]["type"] = []

    store = StateStore(path)
    run_id = store.start_run(
        source.name, "live", 100_000.0, json.dumps(raw), now=now,
    )
    store.conn.execute(
        "INSERT INTO events(run_id, ts, type, payload) VALUES(?,?,?,?)",
        (run_id, now.isoformat(), "order_filled",
         json.dumps({"order_id": "legacy"})),
    )
    store.conn.commit()
    store.stop_run()

    with pytest.raises(RecoveryArchiveError) as blocked:
        store.prepare_toss_live_run(
            target.name, 100_000.0, target.model_dump_json(), now=now,
        )
    assert blocked.value.code == "daily_budget_scope_invalid"
    store.close()


def test_unknown_event_scope_uses_worst_case_day_boundary(tmp_path):
    path = tmp_path / "state.db"
    source = toss_live_scope_config("source", timezone=-5)
    target = toss_live_scope_config("target")
    event_at = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)
    before_every_possible_reset = datetime(2026, 9, 1, 4, 0, tzinfo=UTC)
    raw = source.model_dump()
    raw["broker"]["type"] = []

    store = StateStore(path)
    run_id = store.start_run(
        source.name, "live", 100_000.0, json.dumps(raw), now=event_at,
    )
    store.conn.execute(
        "INSERT INTO events(run_id, ts, type, payload) VALUES(?,?,?,?)",
        (run_id, event_at.isoformat(), "order_filled",
         json.dumps({"order_id": "legacy", "fee": 0})),
    )
    store.conn.commit()
    store.stop_run()

    with pytest.raises(RecoveryArchiveError) as blocked:
        store.prepare_toss_live_run(
            target.name, 100_000.0, target.model_dump_json(),
            now=before_every_possible_reset,
        )
    assert blocked.value.code == "daily_budget_scope_invalid"

    target_id = store.start_run(
        target.name, "live", 100_000.0, target.model_dump_json(),
        now=event_at + timedelta(days=1),
    )
    assert target_id > run_id
    store.close()


def test_older_unresolved_toss_run_cannot_hide_behind_newer_clean_head(
        tmp_path):
    """Legacy duplicate heads cannot erase venue uncertainty for the account."""
    path = tmp_path / "state.db"
    same = toss_live_scope_config("same")
    other = toss_live_scope_config("other")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)

    seeded = StateStore(path)
    older_id = seeded.start_run(
        same.name, "live", 100_000.0, same.model_dump_json(), now=now,
    )
    seeded.stop_run()
    newer_id = seeded.start_run(
        same.name, "live", 100_000.0, same.model_dump_json(), now=now,
    )
    seeded.stop_run()
    seeded.conn.execute(
        "UPDATE runs SET requires_reconciliation=1, archived_at=NULL "
        "WHERE id=?",
        (older_id,),
    )
    seeded.conn.commit()
    seeded.close()

    contender = StateStore(path)
    try:
        for target in (other, same):
            with pytest.raises(RecoveryArchiveError) as blocked:
                contender.prepare_toss_live_run(
                    target.name, 100_000.0,
                    target.model_dump_json(), now=now,
                )
            assert blocked.value.code == "reconciliation_required"
            gate = contender.toss_account_start_gate(
                resume_strategy=target.name,
                resume_config_json=target.model_dump_json(),
                now=now,
            )
            assert gate["blocking_run_id"] == older_id
        assert contender.run_id is None
        totals = contender.conn.execute(
            "SELECT MAX(id) latest, COUNT(*) count FROM runs",
        ).fetchone()
        assert (totals["latest"], totals["count"]) == (newer_id, 2)
    finally:
        contender.close()


def test_future_toss_budget_day_fails_closed_after_clock_rollback(tmp_path):
    path = tmp_path / "state.db"
    source = toss_live_scope_config("source")
    other = toss_live_scope_config("other")
    used_at = datetime(2026, 9, 1, 5, 0, tzinfo=UTC)
    corrected_now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)

    seeded = StateStore(path)
    run_id = seeded.start_run(
        source.name, "live", 100_000.0,
        source.model_dump_json(), now=used_at,
    )
    budget = TradingBudget(max_daily_orders=5, timezone_offset_hours=9)
    seeded.restore_budget(budget, now=used_at)
    budget.record_trade(-1.0, now=used_at)
    seeded.conn.execute(
        "UPDATE day_budget SET updated_at=? WHERE run_id=?",
        (used_at.isoformat(), run_id),
    )
    seeded.conn.commit()
    seeded.stop_run()
    seeded.close()

    contender = StateStore(path)
    try:
        for target in (other, source):
            with pytest.raises(RecoveryArchiveError) as blocked:
                contender.prepare_toss_live_run(
                    target.name, 100_000.0, target.model_dump_json(),
                    now=corrected_now,
                )
            assert blocked.value.code == "daily_budget_time_invalid"
        assert contender.run_id is None
        assert contender.conn.execute(
            "SELECT COUNT(*) n FROM runs",
        ).fetchone()["n"] == 1
    finally:
        contender.close()


@pytest.mark.parametrize(
    ("ledger_kind", "event_type", "event_count"),
    [
        ("missing", "order_filled", 1),
        ("zero", "order_filled", 2),
        ("zero", "trade_closed", 1),
        ("fee_only", "order_filled", 1),
        ("fee_and_pnl", "trade_closed", 1),
    ],
)
def test_legacy_fill_without_used_budget_blocks_resume_and_fresh_run(
        tmp_path, ledger_kind, event_type, event_count):
    """A legacy fill proves usage but cannot safely reconstruct its allowance."""
    path = tmp_path / "state.db"
    source = toss_live_scope_config("source")
    other = toss_live_scope_config("other")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)

    seeded = StateStore(path)
    run_id = seeded.start_run(
        source.name, "live", 100_000.0,
        source.model_dump_json(), now=now,
    )
    if ledger_kind == "zero":
        budget = TradingBudget(max_daily_orders=5, timezone_offset_hours=9)
        seeded.restore_budget(budget, now=now)
    elif ledger_kind in {"fee_only", "fee_and_pnl"}:
        budget = TradingBudget(max_daily_orders=5, timezone_offset_hours=9)
        seeded.restore_budget(budget, now=now)
        seeded.conn.execute(
            "UPDATE day_budget SET fees=1, realized_pnl=?, updated_at=? "
            "WHERE run_id=?",
            (-5 if ledger_kind == "fee_and_pnl" else 0,
             now.isoformat(), run_id),
        )
    for index in range(event_count):
        payload = (
            json.dumps({"order_id": f"legacy-{index}"})
            if event_type == "order_filled" else "{}"
        )
        seeded.conn.execute(
            "INSERT INTO events(run_id, ts, type, payload) VALUES(?,?,?,?)",
            (run_id, now.isoformat(), event_type, payload),
        )
    seeded.conn.commit()
    seeded.stop_run()
    seeded.close()

    contender = StateStore(path)
    try:
        for target in (source, other):
            gate = contender.toss_account_start_gate(
                resume_strategy=target.name,
                resume_config_json=target.model_dump_json(), now=now,
            )
            assert gate["resumable_run_id"] is None
            assert gate["daily_budget_blocked"]
            assert gate["budget_blocking_run_id"] == run_id
            with pytest.raises(RecoveryArchiveError) as blocked:
                contender.prepare_toss_live_run(
                    target.name, 100_000.0,
                    target.model_dump_json(), now=now,
                )
            assert blocked.value.code == "daily_budget_strategy_switch_blocked"
        assert contender.conn.execute(
            "SELECT COUNT(*) n FROM runs",
        ).fetchone()["n"] == 1

        next_day_id = contender.start_run(
            other.name, "live", 100_000.0, other.model_dump_json(),
            now=datetime(2026, 8, 31, 15, 0, tzinfo=UTC),
        )
        assert next_day_id > run_id
    finally:
        contender.close()


@pytest.mark.parametrize(
    ("submitted_ids", "fill_payloads", "resume_allowed"),
    [
        (["partial-1"],
         [{"order_id": "partial-1", "fee": 0},
          {"order_id": "partial-1", "fee": 0}], True),
        (["first"],
         [{"order_id": "first", "fee": 0},
          {"order_id": "unrecorded-second", "fee": 0}], False),
        (["first"], ["{broken"], False),
    ],
)
def test_fill_order_ids_must_fit_the_durable_order_count(
        tmp_path, submitted_ids, fill_payloads, resume_allowed):
    path = tmp_path / "state.db"
    config = toss_live_scope_config("source")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)

    seeded = StateStore(path)
    run_id = seeded.start_run(
        config.name, "live", 100_000.0,
        config.model_dump_json(), now=now,
    )
    budget = TradingBudget(max_daily_orders=5, timezone_offset_hours=9)
    budget.roll(now)
    seeded.save_budget(budget)
    seeded.conn.execute(
        "UPDATE day_budget SET orders=1, notional=100, updated_at=? "
        "WHERE run_id=?",
        (now.isoformat(), run_id),
    )
    for order_id in submitted_ids:
        seeded.conn.execute(
            "INSERT INTO events(run_id, ts, type, payload) VALUES(?,?,?,?)",
            (run_id, now.isoformat(), "order_submitted",
             json.dumps({"id": order_id})),
        )
    for payload in fill_payloads:
        serialized = payload if isinstance(payload, str) else json.dumps(payload)
        seeded.conn.execute(
            "INSERT INTO events(run_id, ts, type, payload) VALUES(?,?,?,?)",
            (run_id, now.isoformat(), "order_filled", serialized),
        )
    seeded.conn.commit()
    seeded.stop_run()
    seeded.close()

    contender = StateStore(path)
    try:
        gate = contender.toss_account_start_gate(
            resume_strategy=config.name,
            resume_config_json=config.model_dump_json(), now=now,
        )
        assert (gate["resumable_run_id"] == run_id) is resume_allowed
        assert gate["daily_budget_blocked"] is (not resume_allowed)
        if resume_allowed:
            assert contender.prepare_toss_live_run(
                config.name, 100_000.0,
                config.model_dump_json(), now=now,
            )
        else:
            with pytest.raises(RecoveryArchiveError) as blocked:
                contender.prepare_toss_live_run(
                    config.name, 100_000.0,
                    config.model_dump_json(), now=now,
                )
            assert blocked.value.code == "daily_budget_strategy_switch_blocked"
    finally:
        contender.close()


def test_fill_and_trade_events_cannot_claim_more_than_the_durable_ledger(
        tmp_path):
    path = tmp_path / "state.db"
    config = toss_live_scope_config("source")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)

    store = StateStore(path)
    run_id = store.start_run(
        config.name, "live", 100_000.0,
        config.model_dump_json(), now=now,
    )
    budget = TradingBudget(max_daily_orders=5, timezone_offset_hours=9)
    budget.roll(now, equity=100_000.0)
    store.save_budget(budget)
    store.conn.execute(
        "UPDATE day_budget SET orders=1, notional=100, fees=0, "
        "realized_pnl=0, updated_at=? WHERE run_id=?",
        (now.isoformat(), run_id),
    )
    events = [
        ("order_submitted", {"id": "order-1"}),
        ("order_filled", {"order_id": "order-1", "fee": 5}),
        ("trade_closed", {"pnl": -1_500}),
    ]
    for event_type, payload in events:
        store.conn.execute(
            "INSERT INTO events(run_id, ts, type, payload) VALUES(?,?,?,?)",
            (run_id, now.isoformat(), event_type, json.dumps(payload)),
        )
    store.conn.commit()
    store.stop_run()

    gate = store.toss_account_start_gate(
        resume_strategy=config.name,
        resume_config_json=config.model_dump_json(), now=now,
    )
    assert gate["resumable_run_id"] is None
    assert gate["daily_budget_blocked"]
    with pytest.raises(RecoveryArchiveError) as blocked:
        store.prepare_toss_live_run(
            config.name, 100_000.0, config.model_dump_json(), now=now,
        )
    assert blocked.value.code == "daily_budget_strategy_switch_blocked"
    store.close()


def test_cross_midnight_fill_links_to_its_prior_day_submission(tmp_path):
    path = tmp_path / "state.db"
    config = toss_live_scope_config("source")
    submitted_at = datetime(2026, 8, 31, 14, 59, tzinfo=UTC)
    filled_at = datetime(2026, 8, 31, 15, 1, tzinfo=UTC)
    current = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)

    store = StateStore(path)
    run_id = store.start_run(
        config.name, "live", 100_000.0,
        config.model_dump_json(), now=submitted_at,
    )
    rows = [
        (run_id, "2026-08-31", 200.0, 1, 0.0, 0.0, 100_000.0, 0,
         "", 9.0, submitted_at.isoformat()),
        (run_id, "2026-09-01", 0.0, 0, 0.0, 1.0, 100_000.0, 0,
         "", 9.0, filled_at.isoformat()),
    ]
    store.conn.executemany(
        "INSERT INTO day_budget(run_id, day, notional, orders, realized_pnl, "
        "fees, starting_equity, blocked, halt_reason, tz_offset_hours, "
        "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    store.conn.execute(
        "INSERT INTO events(run_id, ts, type, payload) VALUES(?,?,?,?)",
        (run_id, submitted_at.isoformat(), "order_submitted",
         json.dumps({"id": "overnight-1"})),
    )
    store.conn.execute(
        "INSERT INTO events(run_id, ts, type, payload) VALUES(?,?,?,?)",
        (run_id, filled_at.isoformat(), "order_filled",
         json.dumps({"order_id": "overnight-1", "fee": 1})),
    )
    store.conn.commit()
    store.stop_run()

    gate = store.toss_account_start_gate(
        resume_strategy=config.name,
        resume_config_json=config.model_dump_json(), now=current,
    )
    assert not gate["daily_budget_blocked"]
    assert gate["resumable_run_id"] == run_id
    assert store.prepare_toss_live_run(
        config.name, 100_000.0, config.model_dump_json(), now=current,
    )
    store.close()


@pytest.mark.parametrize(
    ("stored_pct", "target_pct"), [(0.05, 0.05), (0.0, 0.05)],
)
def test_used_percentage_loss_ledger_requires_positive_starting_equity(
        tmp_path, stored_pct, target_pct):
    path = tmp_path / "state.db"
    stored_data = toss_live_scope_config("source").model_dump()
    stored_data["limits"]["max_daily_loss_pct"] = stored_pct
    stored = StrategyConfig.model_validate(stored_data)
    target_data = stored.model_dump()
    target_data["limits"]["max_daily_loss_pct"] = target_pct
    target = StrategyConfig.model_validate(target_data)
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)

    store = StateStore(path)
    run_id = store.start_run(
        stored.name, "live", 100_000.0,
        stored.model_dump_json(), now=now,
    )
    store.conn.execute(
        "INSERT INTO day_budget(run_id, day, notional, orders, realized_pnl, "
        "fees, starting_equity, blocked, halt_reason, tz_offset_hours, "
        "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, "2026-08-31", 100.0, 1, -10_000.0, 0.0, 0.0, 0,
         "", 9.0, now.isoformat()),
    )
    store.conn.commit()
    store.stop_run()

    with pytest.raises(RecoveryArchiveError) as blocked:
        store.prepare_toss_live_run(
            target.name, 100_000.0, target.model_dump_json(), now=now,
        )
    assert blocked.value.code == "daily_budget_value_invalid"
    store.close()


def test_future_budget_update_time_fails_closed_even_when_day_is_old(tmp_path):
    path = tmp_path / "state.db"
    source = toss_live_scope_config("source")
    target = toss_live_scope_config("target")
    used_at = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)
    current = datetime(2026, 9, 1, 5, 0, tzinfo=UTC)

    store = StateStore(path)
    run_id = store.start_run(
        source.name, "live", 100_000.0,
        source.model_dump_json(), now=used_at,
    )
    budget = TradingBudget(max_daily_orders=5, timezone_offset_hours=9)
    store.restore_budget(budget, now=used_at)
    budget.record_trade(-1.0, now=used_at)
    store.conn.execute(
        "UPDATE day_budget SET updated_at=? WHERE run_id=?",
        (datetime(2026, 9, 2, 5, 0, tzinfo=UTC).isoformat(), run_id),
    )
    store.conn.commit()
    store.stop_run()

    with pytest.raises(RecoveryArchiveError) as blocked:
        store.prepare_toss_live_run(
            target.name, 100_000.0, target.model_dump_json(), now=current,
        )
    assert blocked.value.code == "daily_budget_time_invalid"
    assert store.conn.execute(
        "SELECT COUNT(*) n FROM runs",
    ).fetchone()["n"] == 1
    store.close()


def test_expired_budget_ignores_corrupt_numeric_usage(tmp_path):
    path = tmp_path / "state.db"
    source = toss_live_scope_config("source")
    target = toss_live_scope_config("target")
    written_at = datetime(2026, 8, 30, 5, 0, tzinfo=UTC)
    current = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)

    store = StateStore(path)
    run_id = store.start_run(
        source.name, "live", 100_000.0,
        source.model_dump_json(), now=written_at,
    )
    store.conn.execute(
        "INSERT INTO day_budget(run_id, day, notional, orders, realized_pnl, "
        "fees, starting_equity, blocked, halt_reason, tz_offset_hours, "
        "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, "2026-08-30", 100.0, 1, "bad", 0.0, 100_000.0, 0,
         "", 9.0, written_at.isoformat()),
    )
    store.conn.commit()
    store.stop_run()

    target_id = store.start_run(
        target.name, "live", 100_000.0,
        target.model_dump_json(), now=current,
    )
    assert target_id > run_id
    store.close()


@pytest.mark.parametrize("event_ts", ["not-a-time", "2026-08-31T05:00:01+00:00"])
def test_invalid_or_future_toss_fill_time_fails_closed(tmp_path, event_ts):
    path = tmp_path / "state.db"
    source = toss_live_scope_config("source")
    target = toss_live_scope_config("target")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)

    store = StateStore(path)
    run_id = store.start_run(
        source.name, "live", 100_000.0,
        source.model_dump_json(), now=now,
    )
    store.conn.execute(
        "INSERT INTO events(run_id, ts, type, payload) VALUES(?,?,?,?)",
        (run_id, event_ts, "order_filled", "{}"),
    )
    store.conn.commit()
    store.stop_run()

    with pytest.raises(RecoveryArchiveError) as blocked:
        store.prepare_toss_live_run(
            target.name, 100_000.0, target.model_dump_json(), now=now,
        )
    assert blocked.value.code == "daily_budget_event_time_invalid"
    assert store.conn.execute(
        "SELECT COUNT(*) n FROM runs",
    ).fetchone()["n"] == 1
    store.close()


def test_kis_fill_event_does_not_capture_the_toss_account_gate(tmp_path):
    path = tmp_path / "state.db"
    kis = other_live_config("kis", "kis")
    toss = toss_live_scope_config("toss")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)

    store = StateStore(path)
    kis_id = store.start_run(
        kis.name, "live", 100_000.0, kis.model_dump_json(), now=now,
    )
    store.conn.execute(
        "INSERT INTO events(run_id, ts, type, payload) VALUES(?,?,?,?)",
        (kis_id, now.isoformat(), "order_filled", "{broken"),
    )
    store.conn.commit()
    store.stop_run()

    toss_id = store.start_run(
        toss.name, "live", 100_000.0, toss.model_dump_json(), now=now,
    )
    assert toss_id > kis_id
    store.close()


def test_malformed_kis_budget_does_not_capture_toss_account_gate(tmp_path):
    path = tmp_path / "state.db"
    kis = other_live_config("kis", "kis")
    toss = toss_live_scope_config("toss")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)

    store = StateStore(path)
    kis_id = store.start_run(
        kis.name, "live", 100_000.0, kis.model_dump_json(), now=now,
    )
    budget = TradingBudget(max_daily_loss=1_000.0, timezone_offset_hours=9)
    store.restore_budget(budget, now=now)
    budget.record_trade(-1.0, now=now)
    store.conn.execute(
        "UPDATE day_budget SET realized_pnl='not-a-number' WHERE run_id=?",
        (kis_id,),
    )
    store.conn.commit()
    store.stop_run()

    toss_id = store.start_run(
        toss.name, "live", 100_000.0, toss.model_dump_json(), now=now,
    )
    assert toss_id > kis_id
    store.close()


def test_mutated_zero_cap_toss_config_is_rejected_at_start_boundary(tmp_path):
    config = toss_live_scope_config("zero-cap")
    config.limits.max_daily_notional = 0
    config.limits.max_daily_orders = 0
    config.limits.max_daily_loss = 0
    config.limits.max_daily_loss_pct = 0

    store = StateStore(tmp_path / "state.db")
    try:
        with pytest.raises(RecoveryArchiveError) as blocked:
            store.prepare_toss_live_run(
                config.name, 100_000.0, config.model_dump_json(),
            )
        assert blocked.value.code == "daily_budget_target_scope_invalid"
    finally:
        store.close()

    trader = LiveTrader.__new__(LiveTrader)
    trader.config = config
    with pytest.raises(ValueError, match="at least one daily cap"):
        asyncio.run(trader.start())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_daily_notional", float("nan")),
        ("max_daily_loss", float("inf")),
        ("max_daily_loss_pct", float("-inf")),
        ("timezone_offset_hours", float("nan")),
        ("timezone_offset_hours", 25.0),
    ],
)
def test_nonfinite_or_out_of_range_live_limits_are_rejected(field, value):
    data = toss_live_scope_config("invalid-limit").model_dump()
    data["limits"][field] = value
    with pytest.raises(ValueError):
        StrategyConfig.model_validate(data)


@pytest.mark.parametrize("value", ["NaN", "Infinity"])
def test_limits_api_rejects_nonfinite_values(recovery_api, value):
    client, _app, _config = recovery_api
    register(client, f"finite-{value.lower()}@example.com")
    response = client.post(
        "/api/limits",
        content=f'{{"max_daily_loss": {value}}}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422


def test_running_live_bot_cannot_remove_its_last_daily_cap(
        recovery_api, monkeypatch):
    client, app, config = recovery_api
    owner_id, _cookie = register(client, "last-cap@example.com")
    app.state.registry.save_limits(owner_id, {"max_daily_orders": 5})
    budget = TradingBudget(max_daily_orders=5)
    trader = SimpleNamespace(
        config=config,
        engine=SimpleNamespace(budget=budget),
    )
    monkeypatch.setattr(
        app.state.registry, "trader",
        lambda user_id: trader if user_id == owner_id else None,
    )

    response = client.post("/api/limits", json={
        "max_daily_notional": 0,
        "max_daily_orders": 0,
        "max_daily_loss": 0,
        "max_daily_loss_pct": 0,
    })
    assert response.status_code == 400
    assert response.json()["code"] == "config_rejected"
    assert budget.max_orders == 5
    assert app.state.registry.limits(owner_id)["max_daily_orders"] == 5


@pytest.mark.parametrize("reason", ["1234567890", "가" * 500])
def test_reason_boundaries_are_accepted(tmp_path, reason):
    path = tmp_path / "state.db"
    config = toss_live_config()
    run_id = seed_run(path, config)
    store = StateStore(path)
    try:
        assert archive(store, run_id, config, reason=reason)["archived"]
    finally:
        store.close()


@pytest.mark.parametrize(
    ("reason", "confirmations", "acknowledgement", "code"),
    [
        ("          ", dict(RECOVERY_CONFIRMATION_PHRASES),
         RECOVERY_ACKNOWLEDGEMENT_PHRASE, "reconciliation_reason_invalid"),
        ("가" * 501, dict(RECOVERY_CONFIRMATION_PHRASES),
         RECOVERY_ACKNOWLEDGEMENT_PHRASE, "reconciliation_reason_invalid"),
        ("충분히 구체적인 운영 복구 사유입니다", {},
         RECOVERY_ACKNOWLEDGEMENT_PHRASE,
         "reconciliation_confirmation_required"),
        ("충분히 구체적인 운영 복구 사유입니다",
         {**RECOVERY_CONFIRMATION_PHRASES, "cash": "확인"},
         RECOVERY_ACKNOWLEDGEMENT_PHRASE,
         "reconciliation_confirmation_required"),
        ("충분히 구체적인 운영 복구 사유입니다",
         dict(RECOVERY_CONFIRMATION_PHRASES), "확인",
         "reconciliation_acknowledgement_required"),
    ],
)
def test_incomplete_or_inexact_proof_is_rejected(
        tmp_path, reason, confirmations, acknowledgement, code):
    path = tmp_path / "state.db"
    config = toss_live_config()
    run_id = seed_run(path, config)
    store = StateStore(path)
    try:
        with pytest.raises(RecoveryArchiveError) as exc:
            archive(store, run_id, config, reason=reason,
                    confirmations=confirmations,
                    acknowledgement=acknowledgement)
        assert exc.value.code == code
        assert store.reconciliation_run(config.name, "live")["required"]
    finally:
        store.close()


def test_clean_dry_and_unverifiable_non_toss_runs_are_rejected(tmp_path):
    config = toss_live_config()

    clean_path = tmp_path / "clean.db"
    clean_id = seed_run(clean_path, config, quarantined=False)
    clean = StateStore(clean_path)
    try:
        with pytest.raises(RecoveryArchiveError) as exc:
            archive(clean, clean_id, config)
        assert exc.value.code == "reconciliation_not_required"
    finally:
        clean.close()

    dry_path = tmp_path / "dry.db"
    dry = StateStore(dry_path)
    try:
        dry_id = dry.start_run(config.name, "dry_run", 100.0,
                               config.model_dump_json())
        dry.mark_reconciliation_required()
        with pytest.raises(RecoveryArchiveError) as exc:
            dry.archive_reconciliation_run(
                run_id=dry_id, strategy=config.name, mode="dry_run",
                operator="user:7", reason="충분히 구체적인 운영 복구 사유입니다",
                confirmations=dict(RECOVERY_CONFIRMATION_PHRASES),
                acknowledgement=RECOVERY_ACKNOWLEDGEMENT_PHRASE,
            )
        assert exc.value.code == "reconciliation_not_required"
    finally:
        dry.close()

    mismatch_path = tmp_path / "mismatch.db"
    mismatch_id = seed_run(mismatch_path, config, config_json="{}")
    mismatch = StateStore(mismatch_path)
    try:
        with pytest.raises(RecoveryArchiveError) as exc:
            archive(mismatch, mismatch_id, config)
        assert exc.value.code == "reconciliation_stored_config_mismatch"
    finally:
        mismatch.close()


def test_legacy_database_migrates_without_losing_the_quarantined_run(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT NOT "
        "NULL, mode TEXT NOT NULL, started_at TEXT NOT NULL, stopped_at TEXT, "
        "starting_cash REAL NOT NULL, config_json TEXT)"
    )
    conn.execute(
        "INSERT INTO runs(strategy, mode, started_at, stopped_at, starting_cash, "
        "config_json) VALUES('legacy','live','2026-01-01T00:00:00+00:00',NULL,100,'{}')"
    )
    conn.commit()
    conn.close()

    migrated = StateStore(path)
    try:
        columns = {
            row["name"] for row in migrated.conn.execute("PRAGMA table_info(runs)")
        }
        assert {"requires_reconciliation", "archived_at", "archive_reason",
                "archived_by"} <= columns
        run = migrated.reconciliation_run("legacy", "live")
        assert run and run["id"] == 1 and run["required"]
        assert migrated.conn.execute(
            "SELECT COUNT(*) n FROM runs"
        ).fetchone()["n"] == 1
        assert migrated.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='run_recovery_audit'"
        ).fetchone()
    finally:
        migrated.close()


def test_concurrent_archive_creates_one_audit_and_never_double_mutates(tmp_path):
    path = tmp_path / "state.db"
    config = toss_live_config()
    run_id = seed_run(path, config)
    gate = Barrier(2)

    def attempt():
        store = StateStore(path)
        try:
            gate.wait(timeout=5)
            try:
                result = archive(store, run_id, config)
                return "idempotent" if result["idempotent"] else "archived"
            except StateInUseError:
                return "busy"
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _n: attempt(), range(2)))
    assert results.count("archived") == 1
    assert set(results) <= {"archived", "idempotent", "busy"}

    reader = StateStore(path)
    try:
        assert reader.conn.execute(
            "SELECT COUNT(*) n FROM run_recovery_audit"
        ).fetchone()["n"] == 1
        assert reader.conn.execute(
            "SELECT COUNT(*) n FROM events WHERE type='reconciliation_archived'"
        ).fetchone()["n"] == 1
    finally:
        reader.close()


@pytest.fixture
def recovery_api(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts_module, "_PBKDF2_ROUNDS", 1_000)
    templates = tmp_path / "templates"
    templates.mkdir()
    live = toss_live_config()
    live_b = toss_live_config("recover-toss-b")
    dry = StrategyConfig.model_validate({**live.model_dump(), "mode": "dry_run"})
    (templates / "recover_toss.json").write_text(
        live.model_dump_json(), encoding="utf-8"
    )
    (templates / "dry_toss.json").write_text(
        dry.model_dump_json(), encoding="utf-8"
    )
    (templates / "recover_toss_b.json").write_text(
        live_b.model_dump_json(), encoding="utf-8"
    )
    monkeypatch.setenv("QUANT_SECRET_KEY", "r" * 48)
    monkeypatch.setenv("QUANT_USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setenv("QUANT_USER_DATA", str(tmp_path / "users"))
    monkeypatch.setenv("QUANT_CONFIG_DIR", str(templates))
    monkeypatch.setenv("QUANT_ENV_FILE", str(tmp_path / "empty.env"))
    app = create_app(None, state_path=str(tmp_path / "app.db"))
    with TestClient(app, base_url="https://desk.example") as client:
        yield client, app, live


def register(client: TestClient, email: str) -> tuple[int, str]:
    client.cookies.clear()
    response = client.post("/api/auth/register", json={
        "email": email,
        "password": "correct-horse-9",
        "display_name": email.split("@", 1)[0],
    })
    assert response.status_code == 201, response.text
    return int(response.json()["id"]), client.cookies.get(SESSION_COOKIE)


def act_as(client: TestClient, cookie: str) -> None:
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, cookie)


def api_payload(run_id: int, *, reason: str = "토스 앱과 다섯 항목을 직접 대조했습니다"):
    return {
        "config_path": "recover_toss",
        "run_id": run_id,
        "reason": reason,
        "confirmations": dict(RECOVERY_CONFIRMATION_PHRASES),
        "acknowledgement": RECOVERY_ACKNOWLEDGEMENT_PHRASE,
    }


def test_registry_reports_clean_same_day_toss_strategy_switch(
        recovery_api):
    client, app, config = recovery_api
    owner_id, _cookie = register(client, "owner@example.com")
    other = toss_live_config("recover-toss-b")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=UTC)
    store = StateStore(app.state.registry.state_path(owner_id))
    try:
        run_id = store.start_run(
            config.name, "live", 100_000.0,
            config.model_dump_json(), now=now,
        )
        budget = TradingBudget(
            max_daily_loss=1_000.0, timezone_offset_hours=9,
        )
        store.restore_budget(budget, now=now)
        budget.record_trade(-1_500.0, now=now)
        store.conn.execute(
            "UPDATE day_budget SET updated_at=? WHERE run_id=?",
            (now.isoformat(), run_id),
        )
        store.conn.commit()
        store.stop_run()
    finally:
        store.close()

    # Exact resume remains available; the other template cannot open a new
    # allowance, and the operator receives a specific non-numeric explanation.
    app.state.registry._assert_recovery_start_allowed(
        owner_id, config, now=now,
    )
    with pytest.raises(ReconciliationProblem) as blocked:
        app.state.registry._assert_recovery_start_allowed(
            owner_id, other, now=now,
        )
    assert blocked.value.code == "daily_budget_strategy_switch_blocked"
    assert blocked.value.details == {
        "budget_blocking_run_id": run_id,
        "budget_blocking_strategy": config.name,
        "next_start_allowed_at": "2026-08-31T15:00:00+00:00",
    }

    status = app.state.registry.reconciliation_status(
        owner_id, other, now=now,
    )
    assert not status["required"]
    assert status["daily_budget_blocked"] and status["restart_blocked"]
    assert status["budget_blocking_run_id"] == run_id
    assert "다른 실거래 전략을 시작할 수 없습니다" in status["message"]
    assert "1,500" not in status["message"]


def test_owner_scoped_api_archives_without_constructing_a_broker(
        recovery_api, monkeypatch):
    client, app, config = recovery_api
    owner_id, owner_cookie = register(client, "owner@example.com")
    stranger_id, stranger_cookie = register(client, "stranger@example.com")
    run_id = seed_run(app.state.registry.state_path(owner_id), config)

    def broker_must_not_exist(*_args, **_kwargs):
        raise AssertionError("reconciliation route constructed a broker")

    monkeypatch.setattr(
        "quant.strategy.builder.build_brokerage", broker_must_not_exist
    )

    built = {"count": 0}

    def trader_must_not_exist(*_args, **_kwargs):
        built["count"] += 1
        raise AssertionError("reconciliation gate constructed a trader")

    monkeypatch.setattr(app.state.registry, "build_trader", trader_must_not_exist)

    act_as(client, stranger_cookie)
    stranger_get = client.get(
        "/api/trader/reconciliation", params={"config_path": "recover_toss"}
    )
    assert stranger_get.status_code == 200
    assert stranger_get.json()["run"] is None
    stranger_post = client.post(
        "/api/trader/reconciliation/archive", json=api_payload(run_id)
    )
    assert stranger_post.status_code == 409
    assert stranger_post.json()["code"] == "reconciliation_run_changed"

    act_as(client, owner_cookie)
    status = client.get(
        "/api/trader/reconciliation", params={"config_path": "recover_toss"}
    )
    assert status.status_code == 200
    body = status.json()
    assert body["required"] and not body["bot_running"]
    assert body["run"] == {
        **body["run"],
        "id": run_id,
        "strategy": config.name,
        "mode": "live",
        "requires_reconciliation": True,
    }
    assert body["confirmation_phrases"] == RECOVERY_CONFIRMATION_PHRASES
    assert body["acknowledgement_phrase"] == RECOVERY_ACKNOWLEDGEMENT_PHRASE

    dry_same_template = StrategyConfig.model_validate({
        **config.model_dump(), "mode": "dry_run",
    })
    app.state.registry._assert_recovery_start_allowed(
        owner_id, dry_same_template
    )
    app.state.registry._assert_recovery_start_allowed(
        owner_id, other_live_config("kis-live", "kis")
    )

    other_start = client.post("/api/trader/start", json={
        "config_path": "recover_toss_b",
        "mode": "live",
        "confirm": "recover-toss-b",
    })
    assert other_start.status_code == 409, other_start.text
    assert other_start.json()["code"] == "reconciliation_required"
    assert other_start.json()["blocking_run_id"] == run_id
    assert other_start.json()["blocking_strategy"] == config.name
    assert built["count"] == 0

    archived = client.post(
        "/api/trader/reconciliation/archive", json=api_payload(run_id)
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["fresh_run_on_next_start"]
    assert archived.json()["run_id"] == run_id
    next_start = archived.json()["next_start_allowed_at"]
    assert datetime.fromisoformat(next_start).utcoffset() == timedelta(0)
    assert "오늘은 새 실거래를 시작할 수 없습니다" in archived.json()["message"]

    start = client.post("/api/trader/start", json={
        "config_path": "recover_toss",
        "mode": "live",
        "confirm": config.name,
    })
    assert start.status_code == 409, start.text
    assert start.json()["code"] == \
        "reconciliation_start_blocked_until_next_kst_day"
    assert start.json()["next_start_allowed_at"] == next_start
    assert app.state.registry.trader(owner_id) is None
    assert built["count"] == 0

    again = client.post(
        "/api/trader/reconciliation/archive", json=api_payload(run_id)
    )
    assert again.status_code == 200 and again.json()["idempotent"]
    after = client.get(
        "/api/trader/reconciliation", params={"config_path": "recover_toss"}
    ).json()
    assert not after["required"] and after["run"]["archived_at"]
    assert after["restart_blocked"]
    assert after["next_start_allowed_at"] == next_start
    assert app.state.registry.state_path(stranger_id) != \
        app.state.registry.state_path(owner_id)


def test_api_rejects_bot_running_wrong_template_and_incomplete_body(
        recovery_api, monkeypatch):
    client, app, config = recovery_api
    owner_id, cookie = register(client, "owner@example.com")
    run_id = seed_run(app.state.registry.state_path(owner_id), config)
    act_as(client, cookie)

    wrong = client.get(
        "/api/trader/reconciliation", params={"config_path": "dry_toss"}
    )
    assert wrong.status_code == 400
    assert wrong.json()["code"] == "reconciliation_not_toss_live"

    missing = api_payload(run_id)
    del missing["acknowledgement"]
    assert client.post(
        "/api/trader/reconciliation/archive", json=missing
    ).status_code == 422

    monkeypatch.setattr(app.state.registry, "trader", lambda _uid: object())
    status = client.get(
        "/api/trader/reconciliation", params={"config_path": "recover_toss"}
    )
    assert status.json()["bot_running"]
    running = client.post(
        "/api/trader/reconciliation/archive", json=api_payload(run_id)
    )
    assert running.status_code == 409
    assert running.json()["code"] == "reconciliation_bot_running"


def test_registry_rejects_a_database_owned_by_another_worker(recovery_api):
    client, app, config = recovery_api
    owner_id, cookie = register(client, "owner@example.com")
    path = app.state.registry.state_path(owner_id)
    run_id = seed_run(path, config)
    holder = StateStore(path)
    try:
        assert holder.resume_run(config.name, "live") == run_id
        holder.mark_reconciliation_required()
        act_as(client, cookie)
        response = client.post(
            "/api/trader/reconciliation/archive", json=api_payload(run_id)
        )
        assert response.status_code == 409
        assert response.json()["code"] == "reconciliation_state_in_use"
    finally:
        holder.close()
