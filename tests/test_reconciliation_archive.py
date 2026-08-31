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
