"""Durable state for live sessions.

A live bot restarts — after a deploy, a crash, an OOM kill. Without persistence
it wakes up believing it holds nothing, and the first thing it does is buy a
position it already has. SQLite (stdlib, no server, one file) is the right size
of tool for this.

The book is only half of it. Everything that *stops* the bot trading has to
come back too — the trading locks, the operator's pins, and the daily budget,
whose whole job is bounding a bot that is already crash-looping. A backstop
that starts over on every restart hands that bot a fresh allowance each time.

And because one file is the account's memory, exactly one process may own it:
two live processes on one `--state` file both resume the same run and both send
orders, so the venue fills twice and the DB records once.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import socket
import sqlite3
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from quant.core.account import Portfolio
from quant.core.context import Context
from quant.core.types import UTC, ClosedTrade, Fill, Order, Symbol
from quant.live.limits import TradingBudget

try:                                    # POSIX only; Windows falls back to the
    import fcntl                        # owner row alone.
except ImportError:                     # pragma: no cover - platform dependent
    fcntl = None                        # type: ignore[assignment]

log = logging.getLogger("quant.live.state")

#: How long an owner's heartbeat stays trustworthy when we cannot check its
#: process directly (another host sharing the file over NFS or a volume mount).
OWNER_STALE_AFTER = timedelta(minutes=5)
#: Heartbeat writes are throttled to this, so a fast timeframe does not turn
#: every tick into an extra write.
OWNER_HEARTBEAT_SECONDS = 20.0


class StateInUseError(RuntimeError):
    """Another live process already owns this state DB."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy TEXT NOT NULL,
  mode TEXT NOT NULL,
  started_at TEXT NOT NULL,
  stopped_at TEXT,
  starting_cash REAL NOT NULL,
  config_json TEXT
);
CREATE TABLE IF NOT EXISTS fills (
  id TEXT PRIMARY KEY,
  run_id INTEGER NOT NULL,
  ts TEXT NOT NULL,
  symbol TEXT NOT NULL, venue TEXT NOT NULL,
  side TEXT NOT NULL, quantity TEXT NOT NULL,
  price REAL NOT NULL, fee REAL NOT NULL,
  order_id TEXT, liquidity TEXT
);
CREATE INDEX IF NOT EXISTS idx_fills_run ON fills(run_id, ts);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  symbol TEXT NOT NULL, side TEXT NOT NULL,
  quantity TEXT NOT NULL, entry_price REAL, exit_price REAL,
  entry_ts TEXT, exit_ts TEXT, pnl REAL, pnl_pct REAL, fees REAL,
  exit_tag TEXT
);
CREATE TABLE IF NOT EXISTS equity (
  run_id INTEGER NOT NULL, ts TEXT NOT NULL,
  equity REAL NOT NULL, cash REAL NOT NULL, drawdown REAL NOT NULL,
  PRIMARY KEY (run_id, ts)
);
CREATE TABLE IF NOT EXISTS positions (
  run_id INTEGER NOT NULL, symbol_key TEXT NOT NULL,
  ticker TEXT, venue TEXT, quantity TEXT NOT NULL,
  avg_price REAL, opened_at TEXT, peak_price REAL,
  PRIMARY KEY (run_id, symbol_key)
);
CREATE TABLE IF NOT EXISTS run_state (
  run_id INTEGER PRIMARY KEY,
  cash REAL NOT NULL,
  realized_pnl REAL NOT NULL DEFAULT 0,
  high_water_mark REAL NOT NULL DEFAULT 0,
  total_fees REAL NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS locks (
  run_id INTEGER NOT NULL, symbol_key TEXT NOT NULL,
  until TEXT NOT NULL, reason TEXT,
  PRIMARY KEY (run_id, symbol_key)
);
CREATE TABLE IF NOT EXISTS pins (
  run_id INTEGER NOT NULL, symbol_key TEXT NOT NULL,
  reason TEXT, pinned_at TEXT NOT NULL,
  PRIMARY KEY (run_id, symbol_key)
);
CREATE TABLE IF NOT EXISTS day_budget (
  run_id INTEGER NOT NULL, day TEXT NOT NULL,
  notional REAL NOT NULL DEFAULT 0, orders INTEGER NOT NULL DEFAULT 0,
  realized_pnl REAL NOT NULL DEFAULT 0, fees REAL NOT NULL DEFAULT 0,
  starting_equity REAL NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0,
  halt_reason TEXT, tz_offset_hours REAL NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, day)
);
CREATE TABLE IF NOT EXISTS db_owner (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  pid INTEGER NOT NULL, host TEXT NOT NULL,
  started_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL, ts TEXT NOT NULL,
  type TEXT NOT NULL, payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id DESC);
"""


class StateStore:
    def __init__(self, path: str | Path = "quant_state.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.run_id: int | None = None
        self._owns = False
        self._lock_fd: int | None = None
        self._heartbeat_at = 0.0

    # ── exclusive ownership ──────────────────────────────────────────────
    def _claim(self) -> None:
        """Take this state DB for this process, or refuse to run.

        Two live processes pointed at one `--state` file both resume the same
        run and both send orders. The venue fills twice, the DB records the
        last writer only, and each process gets its own copy of the daily
        budget — so the caps are doubled at exactly the moment the book is.
        Nothing downstream can see the other process, so it has to be caught
        here, at the one file they share.

        Claiming happens on the first *live* touch rather than in `__init__`,
        because the dashboard opens the same file read-only while a bot is
        running and must not be locked out of it.
        """
        if self._owns:
            self._heartbeat()
            return

        holder = self._owner_row()
        if not self._take_file_lock():
            raise StateInUseError(self._busy_message(holder))
        # The lock alone does not settle it: a process from an older build, or
        # one on another host sharing the file, holds no lock at all.
        if holder is not None and self._holder_is_running(holder):
            self._release_file_lock()
            raise StateInUseError(self._busy_message(holder))

        now = datetime.now(UTC).isoformat()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            pass
        self.conn.execute(
            "INSERT OR REPLACE INTO db_owner(id, pid, host, started_at, heartbeat_at) "
            "VALUES(1,?,?,?,?)",
            (os.getpid(), socket.gethostname(), now, now),
        )
        self.conn.commit()
        self._owns = True
        self._heartbeat_at = time.monotonic()
        if holder is not None:
            log.warning("%s 를 사용하던 프로세스(PID %s)의 흔적이 남아 있었습니다 — "
                        "종료된 것으로 보고 이어받습니다", self.path, holder["pid"])

    def _owner_row(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM db_owner WHERE id=1").fetchone()

    def _holder_is_running(self, holder: sqlite3.Row) -> bool:
        if holder["pid"] == os.getpid() and holder["host"] == socket.gethostname():
            return False                       # our own leftover row
        if holder["host"] != socket.gethostname():
            # Can't signal a process on another machine; trust the heartbeat.
            try:
                beat = datetime.fromisoformat(holder["heartbeat_at"])
            except (TypeError, ValueError):
                return False
            return datetime.now(UTC) - beat < OWNER_STALE_AFTER
        try:
            os.kill(holder["pid"], 0)
        except (OSError, TypeError):
            return False
        return True

    def _busy_message(self, holder: sqlite3.Row | None) -> str:
        if holder is not None and holder["pid"] == os.getpid():
            return (f"이 프로세스가 이미 {self.path} 로 트레이더를 돌리고 있습니다 — "
                    f"기존 트레이더가 완전히 종료된 뒤에 다시 시작하세요.")
        who = (f"{holder['host']} PID {holder['pid']} (마지막 신호 {holder['heartbeat_at']})"
               if holder is not None else "다른 프로세스")
        return (f"이 상태 파일은 이미 사용 중입니다: {self.path} — {who}. "
                f"두 프로세스가 같은 계좌에 각자 주문하면 실제 체결만 두 배가 되고 "
                f"기록은 한 번만 남습니다. 기존 프로세스를 먼저 종료하거나 "
                f"--state 로 다른 파일을 지정하세요.")

    def _take_file_lock(self) -> bool:
        """Advisory lock on a sidecar file. The OS drops it when we die.

        A sidecar rather than the DB file itself: SQLite takes its own POSIX
        record locks on that file, and there is no reason to run two locking
        schemes over one inode.

        A filesystem that cannot lock (some network mounts) must not stop a
        legitimate single process from starting — the owner row still covers
        that case, less precisely.
        """
        if fcntl is None:                      # pragma: no cover - platform dependent
            return True
        lock_path = self.path.with_name(self.path.name + ".lock")
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as exc:                 # pragma: no cover - permissions
            log.warning("잠금 파일 %s 을(를) 만들 수 없습니다 (%s) — "
                        "중복 실행 감지가 약해집니다", lock_path, exc)
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return False                   # somebody else holds it
            log.warning("%s 잠금을 지원하지 않는 저장소입니다 (%s) — "
                        "중복 실행 감지가 약해집니다", lock_path, exc)
            return True
        self._lock_fd = fd
        return True

    def _release_file_lock(self) -> None:
        if self._lock_fd is None:
            return
        try:
            if fcntl is not None:              # pragma: no branch
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(self._lock_fd)
            self._lock_fd = None

    def _heartbeat(self) -> None:
        """Keep the owner row fresh so a killed process looks killed."""
        if not self._owns:
            return
        now = time.monotonic()
        if now - self._heartbeat_at < OWNER_HEARTBEAT_SECONDS:
            return
        self._heartbeat_at = now
        self.conn.execute("UPDATE db_owner SET heartbeat_at=? WHERE id=1 AND pid=?",
                          (datetime.now(UTC).isoformat(), os.getpid()))
        self.conn.commit()

    def _release_ownership(self) -> None:
        if not self._owns:
            return
        try:
            self.conn.execute("DELETE FROM db_owner WHERE id=1 AND pid=?",
                              (os.getpid(),))
            self.conn.commit()
        except sqlite3.Error:                  # closing a broken DB is not fatal
            log.debug("could not clear the owner row", exc_info=True)
        self._owns = False
        self._release_file_lock()

    # ── runs ─────────────────────────────────────────────────────────────
    def start_run(self, strategy: str, mode: str, starting_cash: float,
                  config_json: str = "") -> int:
        self._claim()
        cur = self.conn.execute(
            "INSERT INTO runs(strategy, mode, started_at, starting_cash, config_json) "
            "VALUES(?,?,?,?,?)",
            (strategy, mode, datetime.now(UTC).isoformat(), starting_cash, config_json),
        )
        self.conn.commit()
        self.run_id = cur.lastrowid
        return self.run_id

    def resume_run(self, strategy: str, mode: str) -> int | None:
        """Reopen the most recent run for this strategy and mode.

        Deliberately ignores `stopped_at`. A clean shutdown does not flatten
        the book — the positions are still there when the process comes back —
        so scoping resume to "runs that were never stopped" meant every normal
        restart woke up believing it held nothing and had its full starting
        cash. In live mode that is a second position on top of the first.
        """
        row = self.conn.execute(
            "SELECT id FROM runs WHERE strategy=? AND mode=? ORDER BY id DESC LIMIT 1",
            (strategy, mode),
        ).fetchone()
        if not row:
            return None
        self.run_id = row["id"]
        self.conn.execute("UPDATE runs SET stopped_at=NULL WHERE id=?", (self.run_id,))
        self.conn.commit()
        return self.run_id

    def stop_run(self) -> None:
        if self.run_id is None:
            return
        self.conn.execute("UPDATE runs SET stopped_at=? WHERE id=?",
                          (datetime.now(UTC).isoformat(), self.run_id))
        self.conn.commit()

    # ── writes ───────────────────────────────────────────────────────────
    def record_fill(self, fill: Fill) -> None:
        self._claim()
        self.conn.execute(
            "INSERT OR IGNORE INTO fills(id, run_id, ts, symbol, venue, side, quantity, "
            "price, fee, order_id, liquidity) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (fill.id, self.run_id, fill.ts.isoformat(), fill.symbol.ticker,
             fill.symbol.venue, fill.side.value, str(fill.quantity), fill.price,
             fill.fee, fill.order_id, fill.liquidity),
        )
        self.conn.commit()

    def record_trade(self, trade: ClosedTrade) -> None:
        self._claim()
        self.conn.execute(
            "INSERT INTO trades(run_id, symbol, side, quantity, entry_price, exit_price, "
            "entry_ts, exit_ts, pnl, pnl_pct, fees, exit_tag) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, trade.symbol.ticker, trade.side.value, str(trade.quantity),
             trade.entry_price, trade.exit_price, trade.entry_ts.isoformat(),
             trade.exit_ts.isoformat(), trade.pnl, trade.pnl_pct, trade.fees,
             trade.exit_tag),
        )
        self.conn.commit()

    def record_equity(self, ts: datetime, equity: float, cash: float,
                      drawdown: float) -> None:
        self._claim()
        self.conn.execute(
            "INSERT OR REPLACE INTO equity(run_id, ts, equity, cash, drawdown) "
            "VALUES(?,?,?,?,?)",
            (self.run_id, ts.isoformat(), equity, cash, drawdown),
        )
        self.conn.commit()

    def snapshot_positions(self, portfolio: Portfolio) -> None:
        """Persist the whole book: cash first, then positions.

        Cash matters as much as the positions do. Restoring 100 shares while
        resetting cash to the configured starting balance produces an equity
        figure that is wrong by the cost of the position, and every subsequent
        size is computed from that wrong number.
        """
        self._claim()
        self.conn.execute(
            "INSERT OR REPLACE INTO run_state(run_id, cash, realized_pnl, "
            "high_water_mark, total_fees, updated_at) VALUES(?,?,?,?,?,?)",
            (self.run_id, portfolio.cash,
             sum(p.realized_pnl for p in portfolio.positions.values()),
             portfolio.high_water_mark, portfolio.total_fees,
             datetime.now(UTC).isoformat()),
        )
        self.conn.execute("DELETE FROM positions WHERE run_id=?", (self.run_id,))
        self.conn.executemany(
            "INSERT INTO positions(run_id, symbol_key, ticker, venue, quantity, "
            "avg_price, opened_at, peak_price) VALUES(?,?,?,?,?,?,?,?)",
            [(self.run_id, p.symbol.key, p.symbol.ticker, p.symbol.venue,
              str(p.quantity), p.avg_price,
              p.opened_at.isoformat() if p.opened_at else None, p.peak_price)
             for p in portfolio.open_positions],
        )
        self.conn.commit()

    def save_locks(self, locks: dict[str, tuple[datetime, str]]) -> None:
        """Persist trading locks.

        Without this a crash is a free pass: every protection the engine has —
        stoploss guard, cooldown, low-profit, drawdown halt — lives in memory,
        so a restart re-enters exactly the names that were just locked out,
        at exactly the worst moment.
        """
        self._claim()
        self.conn.execute("DELETE FROM locks WHERE run_id=?", (self.run_id,))
        self.conn.executemany(
            "INSERT INTO locks(run_id, symbol_key, until, reason) VALUES(?,?,?,?)",
            [(self.run_id, key, until.isoformat(), reason)
             for key, (until, reason) in locks.items()],
        )
        self.conn.commit()

    def restore_locks(self, now: datetime) -> dict[str, tuple[datetime, str]]:
        self._claim()
        rows = self.conn.execute(
            "SELECT symbol_key, until, reason FROM locks WHERE run_id=?", (self.run_id,)
        ).fetchall()
        out: dict[str, tuple[datetime, str]] = {}
        for r in rows:
            until = datetime.fromisoformat(r["until"])
            if until > now:
                out[r["symbol_key"]] = (until, r["reason"] or "")
        if out:
            log.info("복원: 거래 잠금 %d건", len(out))
        return out

    def save_pins(self, pins: dict[str, str]) -> None:
        """Persist operator pins.

        A pin is the operator saying "this one is mine". Nothing in the
        strategy knows why a manually bought position exists, so the portfolio
        model computes a target of zero for it and sells it straight back on
        the next bar — which is precisely what a restart used to cause, minutes
        after the operator's own trade.
        """
        self._claim()
        self.conn.execute("DELETE FROM pins WHERE run_id=?", (self.run_id,))
        now = datetime.now(UTC).isoformat()
        self.conn.executemany(
            "INSERT INTO pins(run_id, symbol_key, reason, pinned_at) VALUES(?,?,?,?)",
            [(self.run_id, key, reason, now) for key, reason in pins.items()],
        )
        self.conn.commit()

    def restore_pins(self, ctx: Context, symbols: dict[str, Symbol]) -> int:
        """Re-pin everything the operator had pinned. Returns how many.

        Takes the context rather than returning a map because a pin only means
        anything applied — and because the reason string has to survive with
        it, so the dashboard still shows why the name is out of the strategy's
        hands.
        """
        self._claim()
        rows = self.conn.execute(
            "SELECT symbol_key, reason FROM pins WHERE run_id=?", (self.run_id,)
        ).fetchall()
        restored = 0
        for row in rows:
            symbol = symbols.get(row["symbol_key"])
            if symbol is None:
                log.warning("고정된 종목 %s 이(가) 현재 유니버스에 없어 복원하지 못했습니다 — "
                            "전략이 이 종목을 정리할 수 있습니다", row["symbol_key"])
                continue
            ctx.pin(symbol, row["reason"] or "")
            restored += 1
        if restored:
            log.info("복원: 운영자 고정 %d건", restored)
        return restored

    # ── daily budget ─────────────────────────────────────────────────────
    def save_budget(self, budget: TradingBudget) -> None:
        """Store today's daily ledger, keyed by its local trading day."""
        state = budget.to_state()
        if not state or self.run_id is None:
            return
        self._claim()
        self.conn.execute(
            "INSERT OR REPLACE INTO day_budget(run_id, day, notional, orders, "
            "realized_pnl, fees, starting_equity, blocked, halt_reason, "
            "tz_offset_hours, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, state["day"], state["notional"], state["orders"],
             state["realized_pnl"], state["fees"], state["starting_equity"],
             state["blocked"], state["halt_reason"], state["tz_offset_hours"],
             datetime.now(UTC).isoformat()),
        )
        self.conn.commit()

    def restore_budget(self, budget: TradingBudget,
                       now: datetime | None = None) -> bool:
        """Reload today's daily ledger, then keep it durable from here on.

        Returns whether a ledger for the current trading day was found.

        The daily caps are the backstop for a bot that is already misbehaving,
        and crash-looping is the headline case — so rebuilding them empty on
        every start was backwards: each restart granted a fresh allowance, and
        "다음 거래일까지 중단" lasted until the next deploy instead. A stored
        day that is no longer today is left where it is; `load_state` decides,
        so a genuinely new trading day still starts clean.
        """
        self._claim()
        row = self.conn.execute(
            "SELECT * FROM day_budget WHERE run_id=? ORDER BY day DESC LIMIT 1",
            (self.run_id,)
        ).fetchone()
        restored = budget.load_state(dict(row) if row is not None else {}, now)
        budget.bind_store(self)
        self.save_budget(budget)          # the stored row now matches the ledger
        return restored

    def record_event(self, event_type: str, payload: dict) -> None:
        self._claim()
        self.conn.execute(
            "INSERT INTO events(run_id, ts, type, payload) VALUES(?,?,?,?)",
            (self.run_id, datetime.now(UTC).isoformat(), event_type,
             json.dumps(payload, ensure_ascii=False, default=str)),
        )
        self.conn.commit()

    # ── reads ────────────────────────────────────────────────────────────
    def restore_positions(self, portfolio: Portfolio,
                          symbols: dict[str, Symbol]) -> int:
        """Rebuild the position book from disk. Returns how many were restored.

        The venue is still authoritative — `LiveBrokerage.sync()` runs after
        this and corrects any drift. This exists so the bot starts from
        *approximately* right rather than from zero.
        """
        self._claim()
        state = self.conn.execute(
            "SELECT * FROM run_state WHERE run_id=?", (self.run_id,)
        ).fetchone()
        if state is not None:
            portfolio.cash = state["cash"]
            portfolio.total_fees = state["total_fees"]
            portfolio.high_water_mark = max(state["high_water_mark"], 0.0) or portfolio.cash
            log.info("복원: 현금 %.2f, 최고자산 %.2f", portfolio.cash,
                     portfolio.high_water_mark)
        else:
            log.warning("이전 현금 잔고 기록 없음 — 설정값 %.2f 로 시작합니다",
                        portfolio.cash)

        rows = self.conn.execute(
            "SELECT * FROM positions WHERE run_id=?", (self.run_id,)
        ).fetchall()
        restored = 0
        for row in rows:
            symbol = symbols.get(row["symbol_key"])
            if symbol is None:
                log.warning("stored position %s is not in the current universe — skipped",
                            row["symbol_key"])
                continue
            pos = portfolio.position(symbol)
            pos.quantity = Decimal(row["quantity"])
            pos.avg_price = row["avg_price"] or 0.0
            pos.peak_price = row["peak_price"] or 0.0
            if row["opened_at"]:
                pos.opened_at = datetime.fromisoformat(row["opened_at"])
            restored += 1
        return restored

    def equity_curve(self, limit: int = 5000) -> list[dict]:
        rows = self.conn.execute(
            "SELECT ts, equity, cash, drawdown FROM equity WHERE run_id=? "
            "ORDER BY ts DESC LIMIT ?", (self.run_id, limit)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def recent_trades(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE run_id=? ORDER BY id DESC LIMIT ?",
            (self.run_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_events(self, limit: int = 200, event_type: str | None = None) -> list[dict]:
        sql = "SELECT ts, type, payload FROM events WHERE run_id=?"
        args: list = [self.run_id]
        if event_type:
            sql += " AND type=?"
            args.append(event_type)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = self.conn.execute(sql, args).fetchall()
        return [{"ts": r["ts"], "type": r["type"],
                 "payload": json.loads(r["payload"] or "{}")} for r in rows]

    def close(self) -> None:
        self._release_ownership()
        self.conn.close()
