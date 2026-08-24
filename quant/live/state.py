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

import contextlib
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
from quant.core.types import UTC, ClosedTrade, Direction, Fill, Insight, Symbol
from quant.live.limits import TradingBudget

try:                                    # POSIX only; Windows falls back to the
    import fcntl  # owner row alone.
except ImportError:                     # pragma: no cover - platform dependent
    fcntl = None                        # type: ignore[assignment]

log = logging.getLogger("quant.live.state")

#: How long an owner's heartbeat stays trustworthy when we cannot check its
#: process directly (another host sharing the file over NFS or a volume mount).
OWNER_STALE_AFTER = timedelta(minutes=5)
#: Heartbeat writes are throttled to this, so a fast timeframe does not turn
#: every tick into an extra write.
OWNER_HEARTBEAT_SECONDS = 20.0


#: 하루·주·달의 경계는 한국 시간입니다. UTC 자정으로 자르면 장중 오전 9시에
#: "오늘" 이 바뀌어서, 아침에 팔아 낸 이익이 어제 것으로 기록됩니다.
_KST_OFFSET = timedelta(hours=9)


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
  exit_tag TEXT,
  closes_position INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS known_symbols (
  ticker TEXT NOT NULL, venue TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '', currency TEXT NOT NULL DEFAULT '',
  seen_at TEXT NOT NULL,
  PRIMARY KEY (ticker, venue)
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
CREATE TABLE IF NOT EXISTS journal_meta (
  run_id INTEGER PRIMARY KEY,
  version INTEGER NOT NULL,
  desk_unscored INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS desk_pending (
  run_id INTEGER NOT NULL, symbol_key TEXT NOT NULL, decided_at TEXT NOT NULL,
  ticker TEXT NOT NULL, action TEXT NOT NULL, conviction REAL NOT NULL,
  price_at_decision REAL NOT NULL, horizon_bars INTEGER NOT NULL,
  benchmark_key TEXT, benchmark_price REAL NOT NULL DEFAULT 0,
  rationale TEXT, invalidation TEXT,
  PRIMARY KEY (run_id, symbol_key, decided_at)
);
CREATE TABLE IF NOT EXISTS desk_lessons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL, symbol_key TEXT NOT NULL, ticker TEXT NOT NULL,
  decided_at TEXT NOT NULL, settled_at TEXT NOT NULL,
  action TEXT NOT NULL, conviction REAL NOT NULL, entry_price REAL NOT NULL,
  realised_pct REAL NOT NULL, benchmark_pct REAL NOT NULL DEFAULT 0,
  excess_pct REAL NOT NULL DEFAULT 0, benchmark_key TEXT,
  correct INTEGER NOT NULL, rationale TEXT, invalidation TEXT,
  UNIQUE (run_id, symbol_key, decided_at, action)
);
CREATE TABLE IF NOT EXISTS insight_pending (
  run_id INTEGER NOT NULL, insight_id TEXT NOT NULL, symbol_key TEXT NOT NULL,
  source TEXT NOT NULL, direction INTEGER NOT NULL, confidence REAL NOT NULL,
  magnitude REAL, weight REAL, generated_at TEXT NOT NULL, period_s REAL NOT NULL,
  entry_price REAL NOT NULL, reference_legs TEXT NOT NULL DEFAULT '[]',
  beta REAL NOT NULL DEFAULT 1, reference_label TEXT, benchmark_key TEXT, tag TEXT,
  PRIMARY KEY (run_id, insight_id)
);
CREATE TABLE IF NOT EXISTS insight_scores (
  run_id INTEGER NOT NULL, insight_id TEXT NOT NULL, source TEXT NOT NULL,
  ticker TEXT NOT NULL, direction INTEGER NOT NULL,
  confidence REAL NOT NULL, magnitude REAL,
  generated_at TEXT NOT NULL, settled_at TEXT NOT NULL,
  entry_price REAL NOT NULL, exit_price REAL NOT NULL,
  realised_pct REAL NOT NULL, benchmark_pct REAL NOT NULL DEFAULT 0,
  excess_pct REAL NOT NULL DEFAULT 0, beta REAL NOT NULL DEFAULT 1,
  reference_label TEXT, benchmark_key TEXT,
  correct INTEGER NOT NULL, tag TEXT,
  PRIMARY KEY (run_id, insight_id)
);
CREATE INDEX IF NOT EXISTS idx_insight_scores_run ON insight_scores(run_id, settled_at);
"""

#: Journal rows carry the layout version that wrote them. A build that meets
#: rows from a newer one skips them instead of half-reading them: the desk
#: reads this journal before every decision, so a mis-parsed row is not a
#: cosmetic bug — it is a wrong lesson taught confidently.
JOURNAL_VERSION = 1


class StateStore:
    def __init__(self, path: str | Path = "quant_state.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        # 이 컬럼보다 먼저 만들어진 상태 DB 에도 붙입니다. 없으면 재배포 뒤
        # 첫 체결에서 INSERT 가 죽고, 그 예외가 관측자 안에서 터져 봇이
        # 체결을 못 적는 채로 계속 돕니다.
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(trades)")}
        if "closes_position" not in have:
            self.conn.execute(
                "ALTER TABLE trades ADD COLUMN closes_position INTEGER NOT NULL DEFAULT 1")
        self.conn.commit()
        self.run_id: int | None = None
        self._owns = False
        self._lock_fd: int | None = None
        self._heartbeat_at = 0.0
        #: How many of the ledger's scored insights are already on disk. The
        #: scored list is append-only between saves, so writing only the tail
        #: keeps a per-bar save cheap no matter how long the run has been up.
        self._scored_saved = 0

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
        # 이미 트랜잭션 안이면 그대로 씁니다 — 여는 데 실패한 것이지
        # 쓸 수 없다는 뜻이 아닙니다.
        with contextlib.suppress(sqlite3.OperationalError):
            self.conn.execute("BEGIN IMMEDIATE")
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
        self.record_closed_trade({
            "symbol": trade.symbol.ticker, "side": trade.side.value,
            "quantity": float(trade.quantity), "entry_price": trade.entry_price,
            "exit_price": trade.exit_price, "entry_ts": trade.entry_ts.isoformat(),
            "exit_ts": trade.exit_ts.isoformat(), "pnl": trade.pnl,
            "pnl_pct": trade.pnl_pct * 100, "fees": trade.fees,
            "exit_tag": trade.exit_tag, "closes_position": trade.closes_position,
        })

    def record_closed_trade(self, payload: dict) -> None:
        """확정된 손익 한 건을 남깁니다.

        이벤트 버스가 넘겨주는 dict 를 그대로 받습니다. `ClosedTrade` 를
        되살리려면 Symbol 객체가 필요한데, 관측자가 가진 것은 payload 뿐입니다.

        이 함수가 없어서 `trades` 테이블이 **한 줄도 채워지지 않았습니다** —
        `record_trade` 는 있었지만 아무도 부르지 않았고, 매매 기록과 기간별
        실현손익이 영구히 비어 있었습니다. 화면은 "아직 완료된 매매가
        없습니다" 를 계속 보여줬고, 그게 사실처럼 보여서 아무도 이상하게
        여기지 않았습니다.
        """
        self._claim()
        self.conn.execute(
            "INSERT INTO trades(run_id, symbol, side, quantity, entry_price, "
            "exit_price, entry_ts, exit_ts, pnl, pnl_pct, fees, exit_tag, "
            "closes_position) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, str(payload.get("symbol", "")), str(payload.get("side", "")),
             str(payload.get("quantity", 0)), payload.get("entry_price"),
             payload.get("exit_price"), payload.get("entry_ts"),
             payload.get("exit_ts"), payload.get("pnl"), payload.get("pnl_pct"),
             payload.get("fees"), payload.get("exit_tag", ""),
             1 if payload.get("closes_position", True) else 0),
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

    # ── decision journal ─────────────────────────────────────────────────
    # Two halves of one question — what did this desk decide, and was it right.
    # Both used to live only in memory, which made them dead code in practice:
    # the desk's calibration line needs four settled calls at conviction ≥ 0.7
    # and a source verdict needs twenty scored insights, and on daily bars
    # neither threshold survives to be reached before the next deploy.
    def _journal_readable(self) -> bool:
        """False when this run's journal was written by a newer build."""
        row = self.conn.execute(
            "SELECT version FROM journal_meta WHERE run_id=?", (self.run_id,)
        ).fetchone()
        if row is None or int(row["version"]) <= JOURNAL_VERSION:
            return True
        log.warning("판단 기록이 더 새로운 형식(v%s)으로 저장되어 있습니다 — 잘못 읽는 대신 "
                    "이번 실행은 기록을 사용하지도 덮어쓰지도 않습니다", row["version"])
        return False

    def _stamp_journal(self, desk_unscored: int | None = None) -> None:
        """Mark this run's journal with the layout that wrote it."""
        row = self.conn.execute(
            "SELECT desk_unscored FROM journal_meta WHERE run_id=?", (self.run_id,)
        ).fetchone()
        keep = int(row["desk_unscored"]) if row is not None else 0
        self.conn.execute(
            "INSERT OR REPLACE INTO journal_meta(run_id, version, desk_unscored, "
            "updated_at) VALUES(?,?,?,?)",
            (self.run_id, JOURNAL_VERSION,
             keep if desk_unscored is None else int(desk_unscored),
             datetime.now(UTC).isoformat()),
        )

    def save_desk_memory(self, memory) -> None:
        """Persist the desk's open calls and its settled lessons.

        The whole write is one transaction, so a crash in the middle leaves the
        previous journal standing rather than half of the new one — the SQLite
        equivalent of writing a temp file and renaming it over the old one.
        Lessons go in with INSERT OR IGNORE: a restart between scoring a call
        and committing it must not let the same call into the calibration
        sample twice.
        """
        if self.run_id is None:
            return
        state = memory.to_state()
        self._claim()
        try:
            self.conn.execute("DELETE FROM desk_pending WHERE run_id=?", (self.run_id,))
            self.conn.executemany(
                "INSERT OR REPLACE INTO desk_pending(run_id, symbol_key, decided_at, "
                "ticker, action, conviction, price_at_decision, horizon_bars, "
                "benchmark_key, benchmark_price, rationale, invalidation) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [(self.run_id, r["symbol_key"], r["decided_at"], r["ticker"],
                  r["action"], r["conviction"], r["price_at_decision"],
                  r["horizon_bars"], r["benchmark_key"], r["benchmark_price"],
                  r["rationale"], r["invalidation"]) for r in state["pending"]],
            )
            self.conn.executemany(
                "INSERT OR IGNORE INTO desk_lessons(run_id, symbol_key, ticker, "
                "decided_at, settled_at, action, conviction, entry_price, "
                "realised_pct, benchmark_pct, excess_pct, benchmark_key, correct, "
                "rationale, invalidation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(self.run_id, r["symbol_key"], r["ticker"], r["decided_at"],
                  r["settled_at"], r["action"], r["conviction"], r["entry_price"],
                  r["realised_pct"], r["benchmark_pct"], r["excess_pct"],
                  r["benchmark_key"], r["correct"], r["rationale"],
                  r["invalidation"]) for r in state["lessons"]],
            )
            self._stamp_journal(state.get("unscored", 0))
            self.conn.commit()
        except Exception:
            # Leave nothing half-applied: the connection is shared, so an open
            # transaction would otherwise be committed by whoever writes next —
            # including the DELETE that emptied the open calls.
            self.conn.rollback()
            raise

    def restore_desk_memory(self, memory) -> int:
        """Reload the desk's journal, then keep it durable from here on.

        Returns how many rows came back.
        """
        if self.run_id is None:
            return 0
        self._claim()
        if not self._journal_readable():
            return 0
        pending = [dict(r) for r in self.conn.execute(
            "SELECT * FROM desk_pending WHERE run_id=? ORDER BY decided_at",
            (self.run_id,)).fetchall()]
        # Newest first, then reversed: the table keeps every lesson ever scored
        # (it is the run's record), while the desk only ever reads the last
        # `capacity` of them.
        lessons = [dict(r) for r in reversed(self.conn.execute(
            "SELECT * FROM desk_lessons WHERE run_id=? ORDER BY id DESC LIMIT ?",
            (self.run_id, memory.capacity)).fetchall())]
        meta = self.conn.execute(
            "SELECT desk_unscored FROM journal_meta WHERE run_id=?", (self.run_id,)
        ).fetchone()
        restored = memory.load_state({
            "pending": pending, "lessons": lessons,
            "unscored": meta["desk_unscored"] if meta is not None else 0,
        })
        memory.bind_store(self)
        if restored:
            log.info("복원: 데스크 판단 기록 %d건 (대기 %d, 회고 %d)", restored,
                     memory.stats["pending"], memory.stats["scored"])
        return restored

    def save_insight_ledger(self, ledger) -> None:
        """Persist the attribution ledger: open insights and scored outcomes.

        Open insights are rewritten wholesale — there are few and the set turns
        over every bar — while scored rows are appended, because they are the
        sample every per-source verdict is computed from and rewriting all of
        them once a bar would cost more than the rest of the loop.

        Reaches into `_pending` because the ledger has no accessor for it yet;
        that accessor belongs in attribution.py, not here.
        """
        if self.run_id is None:
            return
        self._claim()
        bench_key = ledger.benchmark.key if ledger.benchmark is not None else ""
        try:
            self.conn.execute("DELETE FROM insight_pending WHERE run_id=?",
                              (self.run_id,))
            self.conn.executemany(
                "INSERT OR REPLACE INTO insight_pending(run_id, insight_id, "
                "symbol_key, source, direction, confidence, magnitude, weight, "
                "generated_at, period_s, entry_price, reference_legs, beta, "
                "reference_label, benchmark_key, tag) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [self._open_insight_row(item, bench_key) for item in ledger._pending],
            )
            scored = ledger.scored
            # A shorter list than we last wrote means the ledger trimmed itself.
            # Re-offer every surviving row (INSERT OR IGNORE makes that a no-op)
            # and mirror the cap on disk, so the table cannot grow for ever
            # either.
            resync = len(scored) < self._scored_saved
            self.conn.executemany(
                "INSERT OR IGNORE INTO insight_scores(run_id, insight_id, source, "
                "ticker, direction, confidence, magnitude, generated_at, settled_at, "
                "entry_price, exit_price, realised_pct, benchmark_pct, excess_pct, "
                "beta, reference_label, benchmark_key, correct, tag) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [self._scored_insight_row(s, bench_key)
                 for s in (scored if resync else scored[self._scored_saved:])],
            )
            if resync:
                self.conn.execute(
                    "DELETE FROM insight_scores WHERE run_id=? AND rowid NOT IN "
                    "(SELECT rowid FROM insight_scores WHERE run_id=? "
                    "ORDER BY settled_at DESC LIMIT ?)",
                    (self.run_id, self.run_id, ledger.max_scored),
                )
            self._stamp_journal()
            self.conn.commit()
            self._scored_saved = len(scored)
        except Exception:
            self.conn.rollback()
            raise

    def _open_insight_row(self, item, bench_key: str) -> tuple:
        ins = item.insight
        # The reference basket is stored by symbol key and entry price, because
        # what a call was measured against is part of the call: re-deriving the
        # basket from today's universe would grade it against a different one.
        legs = json.dumps([[s.key, price] for s, price in item.legs])
        return (self.run_id, ins.id, ins.symbol.key, ins.source, int(ins.direction),
                ins.confidence, ins.magnitude, ins.weight,
                ins.generated_at.isoformat(), ins.period.total_seconds(),
                item.entry, legs, item.beta, item.reference, bench_key, ins.tag)

    def _scored_insight_row(self, s, bench_key: str) -> tuple:
        return (self.run_id, s.insight_id, s.source, s.ticker, s.direction,
                s.confidence, s.magnitude, s.generated_at.isoformat(),
                s.settled_at.isoformat(), s.entry_price, s.exit_price,
                s.realised_pct, s.benchmark_pct, s.excess_pct, s.beta,
                s.reference, bench_key, int(s.correct), s.tag)

    def restore_insight_ledger(self, ledger, symbols: dict[str, Symbol]) -> int:
        """Reload the attribution ledger. Returns how many scored rows came back.

        Per-source scores are rebuilt *from* the scored rows rather than stored
        beside them: two representations of one number drift, and this is the
        number that answers "which alpha model should be removed".
        """
        if self.run_id is None:
            return 0
        self._claim()
        if not self._journal_readable():
            return 0
        # Imported here so the live-state layer does not pull the alpha layer
        # in at module load; this is the only method that needs it.
        from quant.alpha.attribution import ScoredInsight, SourceScore, _Pending

        bench_key = ledger.benchmark.key if ledger.benchmark is not None else ""
        # The benchmark is deliberately kept out of the universe, so it has to
        # be added by hand or every restored call would lose the very leg it
        # was measured against and quietly fall back to its raw return.
        lookup = dict(symbols)
        if ledger.benchmark is not None:
            lookup.setdefault(ledger.benchmark.key, ledger.benchmark)
        known = {item.insight.id for item in ledger._pending}
        missing = unreferenced = swapped = 0
        for r in self.conn.execute("SELECT * FROM insight_pending WHERE run_id=?",
                                   (self.run_id,)).fetchall():
            if r["insight_id"] in known:
                continue
            symbol = lookup.get(r["symbol_key"])
            if symbol is None:
                missing += 1
                continue
            swapped += (r["benchmark_key"] or "") != bench_key
            legs = [(lookup[key], float(price))
                    for key, price in json.loads(r["reference_legs"] or "[]")
                    if key in lookup]
            if not legs and (r["reference_legs"] or "[]") != "[]":
                # Every leg of the basket has left the universe. The ledger
                # already treats an unpriceable reference as no reference —
                # excess becomes the raw return — but say so, because a whole
                # column of the attribution table quietly changes meaning.
                unreferenced += 1
            ledger._pending.append(_Pending(
                insight=Insight(
                    symbol=symbol, direction=Direction(int(r["direction"])),
                    period=timedelta(seconds=float(r["period_s"])),
                    generated_at=datetime.fromisoformat(r["generated_at"]),
                    magnitude=r["magnitude"], confidence=float(r["confidence"] or 0.0),
                    weight=r["weight"], source=r["source"], tag=r["tag"] or "",
                    id=r["insight_id"],
                ),
                entry=float(r["entry_price"] or 0.0), legs=legs,
                beta=float(r["beta"] or 1.0), reference=r["reference_label"] or "",
            ))
        if missing:
            log.warning("보류 중이던 인사이트 %d건이 현재 유니버스에 없는 종목이라 "
                        "채점 대기열로 복원하지 못했습니다", missing)
        if unreferenced:
            log.warning("보류 중이던 인사이트 %d건은 비교 대상 종목이 모두 사라져 "
                        "원수익률로 채점됩니다", unreferenced)
        if swapped:
            # Each call keeps the basket it was stamped with, so the numbers
            # stay honest — but they are no longer all against one reference,
            # and a reader comparing the column across the run must know.
            log.warning("벤치마크가 바뀐 뒤 복원된 인사이트 %d건은 판단 당시의 비교 대상으로 "
                        "채점됩니다 — 이 실행의 초과수익 열은 기준이 섞여 있습니다", swapped)

        seen = {s.insight_id for s in ledger.scored}
        rows = self.conn.execute(
            "SELECT * FROM insight_scores WHERE run_id=? "
            "ORDER BY settled_at DESC, rowid DESC LIMIT ?",
            (self.run_id, ledger.max_scored)).fetchall()
        restored = 0
        for r in reversed(rows):
            if r["insight_id"] in seen:
                continue
            record = ScoredInsight(
                insight_id=r["insight_id"], source=r["source"], ticker=r["ticker"],
                direction=int(r["direction"]), confidence=float(r["confidence"] or 0.0),
                magnitude=r["magnitude"],
                generated_at=datetime.fromisoformat(r["generated_at"]),
                settled_at=datetime.fromisoformat(r["settled_at"]),
                entry_price=r["entry_price"], exit_price=r["exit_price"],
                realised_pct=r["realised_pct"], benchmark_pct=r["benchmark_pct"],
                excess_pct=r["excess_pct"], correct=bool(r["correct"]),
                beta=float(r["beta"] or 1.0), reference=r["reference_label"] or "",
                tag=r["tag"] or "",
            )
            ledger.scored.append(record)
            # Mirrors InsightLedger.settle's own aggregation. It is duplicated
            # rather than shared only because the ledger cannot yet load itself.
            score = ledger.sources.setdefault(record.source, SourceScore(record.source))
            score.scored += 1
            score.correct += int(record.correct)
            score.realised.append(record.realised_pct)
            score.excess.append(record.excess_pct)
            score.confidences.append(record.confidence)
            score.correctness.append(record.correct)
            score.betas.append(record.beta)
            restored += 1
        # Everything in `scored` came off disk only if the ledger started empty.
        # Otherwise make the next save re-offer every row — INSERT OR IGNORE
        # makes that idempotent, and the alternative is losing the rows that
        # were scored before this restore.
        self._scored_saved = 0 if seen else len(ledger.scored)
        if restored:
            log.info("복원: 채점된 인사이트 %d건 · 알파 모델 %d개", restored,
                     len(ledger.sources))
        return restored

    def restore_journal(self, ledger=None, memory=None,
                        symbols: dict[str, Symbol] | None = None) -> dict:
        """Reload both halves of the decision journal, then keep them durable.

        One call because they answer one question. Restoring only half of it
        would report a hit rate over a sample missing its own open calls.
        """
        return {
            "desk": self.restore_desk_memory(memory) if memory is not None else 0,
            "insights": (self.restore_insight_ledger(ledger, symbols or {})
                         if ledger is not None else 0),
        }

    def save_journal(self, ledger=None, memory=None) -> None:
        """Flush both halves.

        The desk half also writes itself through on every change — see
        `DeskMemory.bind_store` — so in practice this is the ledger's route to
        disk, and the desk's belt and braces.
        """
        if memory is not None:
            self.save_desk_memory(memory)
        if ledger is not None:
            self.save_insight_ledger(ledger)

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

    def fills_for(self, symbol_key: str, since: str = "", limit: int = 500) -> list[dict]:
        """이 종목의 체결. 차트에 점을 찍기 위한 것입니다.

        자동매매 중이어도 봇이 무엇을 했는지 눈으로 봐야 합니다 — 왼쪽에서
        심의하고 낸 주문이 오른쪽 봉 위에 나타나야 "지금 뭘 하고 있는지"를
        읽을 수 있습니다.
        """
        ticker, _, venue = symbol_key.partition(":")
        if venue:
            ticker, venue = venue, ticker
        rows = self.conn.execute(
            "SELECT ts, side, quantity, price, fee, liquidity FROM fills "
            "WHERE run_id=? AND symbol=? AND ts>=? ORDER BY ts DESC LIMIT ?",
            (self.run_id, ticker, since or "", limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def position_for(self, symbol_key: str) -> dict | None:
        """장부에 적힌 이 종목의 자리 — 수량과 평균단가."""
        row = self.conn.execute(
            "SELECT quantity, avg_price, opened_at FROM positions "
            "WHERE run_id=? AND symbol_key=?", (self.run_id, symbol_key)).fetchone()
        if row is None or not float(row["quantity"] or 0):
            return None
        return {"quantity": float(row["quantity"]),
                "avg_price": row["avg_price"] or 0.0,
                "opened_at": row["opened_at"]}

    def recent_trades(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE run_id=? ORDER BY id DESC LIMIT ?",
            (self.run_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def pnl_by_period(self, now: datetime | None = None,
                      strategy: str | None = None,
                      mode: str | None = None) -> dict:
        """오늘·이번주·이번달·올해 실현손익.

        **run 을 가로질러 셉니다.** 봇을 멈추고 다시 켜면 새 run 이 열리는데,
        그때마다 "이번 달 수익" 이 0 으로 돌아가면 그건 수익이 아니라 실행
        시간을 재는 숫자입니다. 사람이 알고 싶은 것은 "내 계좌가 이번 달에
        얼마를 벌었나" 이고, 그건 재시작과 무관합니다.

        경계는 한국 시간입니다. UTC 자정으로 자르면 장중 오전 9시에 "오늘"
        이 바뀝니다 — 아침에 팔아서 낸 이익이 어제 것이 됩니다.

        주는 월요일에 시작합니다(ISO). 국내 시장이 월요일에 열리므로, 일요일
        시작 주로 자르면 주말 하나를 사이에 두고 같은 주의 거래가 갈립니다.

        `strategy` 를 주면 그 전략의 run 만 셉니다. 안 주면 이 계정의 전부.

        `mode` 는 모의(dry_run)와 실거래(live)를 가릅니다. **섞으면 안 됩니다** —
        모의로 번 돈은 실제로 번 돈이 아닌데, 한 숫자로 합치면 화면은 그것을
        "실현 수익" 이라고 부릅니다. 모의에서 크게 벌고 실거래에서 잃은 사람이
        자기가 벌고 있다고 믿게 됩니다.
        """
        now = (now or datetime.now(UTC)).astimezone(UTC)
        kst = now + _KST_OFFSET
        day = kst.date()
        bounds = {
            "today": day,
            "week": day - timedelta(days=day.weekday()),
            "month": day.replace(day=1),
            "year": day.replace(month=1, day=1),
        }

        conds, run_args = [], []
        if strategy:
            conds.append("strategy=?")
            run_args.append(strategy)
        if mode:
            conds.append("mode=?")
            run_args.append(mode)
        run_filter = (f" AND t.run_id IN (SELECT id FROM runs WHERE {' AND '.join(conds)})"
                      if conds else "")

        out: dict[str, dict] = {}
        for label, start in bounds.items():
            # 저장된 exit_ts 는 UTC ISO 입니다. KST 경계를 UTC 로 되돌려 자릅니다.
            since = (datetime(start.year, start.month, start.day, tzinfo=UTC)
                     - _KST_OFFSET).isoformat()
            row = self.conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(t.pnl),0) pnl, "
                "COALESCE(SUM(t.fees),0) fees, "
                "COALESCE(SUM(CASE WHEN t.pnl > 0 THEN 1 ELSE 0 END),0) wins "
                f"FROM trades t WHERE t.exit_ts >= ?{run_filter}",
                [since, *run_args]).fetchone()
            n = row["n"]
            out[label] = {
                "trades": n,
                "pnl": round(row["pnl"], 2),
                "fees": round(row["fees"], 2),
                "wins": row["wins"],
                "win_rate": round(row["wins"] / n, 4) if n else None,
                "since": start.isoformat(),
            }
        return out

    def trade_log(self, limit: int = 200, offset: int = 0,
                  strategy: str | None = None, mode: str | None = None) -> dict:
        """매매 기록 — run 을 가로질러, 최근 것부터.

        `recent_trades` 는 지금 돌고 있는 run 만 봅니다. 그건 화면 상단의
        "이번 실행" 요약에는 맞지만, "내가 지금까지 뭘 사고팔았나" 라는
        질문에는 답하지 못합니다. 봇을 어제 껐다 켰으면 어제 거래가 사라집니다.
        """
        conds, args = [], []
        if strategy:
            conds.append("strategy=?")
            args.append(strategy)
        if mode:
            conds.append("mode=?")
            args.append(mode)
        where = (f" WHERE t.run_id IN (SELECT id FROM runs WHERE {' AND '.join(conds)})"
                 if conds else "")
        total = self.conn.execute(
            f"SELECT COUNT(*) n FROM trades t{where}", args).fetchone()["n"]
        rows = self.conn.execute(
            "SELECT t.*, r.strategy, r.mode FROM trades t "
            "JOIN runs r ON r.id = t.run_id" + where
            + " ORDER BY t.exit_ts DESC, t.id DESC LIMIT ? OFFSET ?",
            [*args, limit, offset]).fetchall()
        return {"total": total, "offset": offset, "trades": [dict(r) for r in rows]}

    def remember_ticker(self, info: dict) -> None:
        """조회해 본 종목을 기억합니다 — 다음부터는 이름으로도 찾히게.

        전체 상장 종목 목록을 저장소에 싣는 대신, 이 계정이 실제로 찾아본
        것만 쌓습니다. 종목코드를 지어내는 것보다 정확하고 — 틀린 코드는
        **다른 회사를 사는 것** 입니다 — 쓰다 보면 자기 관심 종목이 목록이
        됩니다.

        `run_id` 와 무관합니다. 봇을 껐다 켜도 찾아본 기록은 남습니다.
        """
        ticker = str(info.get("ticker") or "").strip()
        if not ticker:
            return
        with self._lock:
            self.conn.execute(
                "INSERT INTO known_symbols (ticker, venue, name, currency, seen_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(ticker, venue) DO UPDATE SET "
                # 이름이 빈 채로 덮어쓰면 알던 것을 잃습니다.
                "  name = CASE WHEN excluded.name <> '' THEN excluded.name "
                "              ELSE known_symbols.name END, "
                "  currency = excluded.currency, seen_at = excluded.seen_at",
                (ticker, info.get("venue") or "", info.get("name") or "",
                 info.get("currency") or "", datetime.now(UTC).isoformat()))
            self.conn.commit()

    def known_tickers(self, limit: int = 400) -> list[dict]:
        """전에 조회해 본 종목들 — 최근에 본 것부터."""
        rows = self.conn.execute(
            "SELECT ticker, venue, name, currency FROM known_symbols "
            "ORDER BY seen_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def modes_with_trades(self) -> list[str]:
        """이 계정에 실제로 거래가 남은 모드들 — 모의만 했으면 ["dry_run"].

        화면이 "모의 / 실거래" 를 나눠 보여줄지 정하는 데 씁니다. 실거래를
        한 적도 없는데 빈 실거래 탭을 세우면, 거기 0 이 떠 있는 것이 "실거래
        에서 본전" 처럼 읽힙니다.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT r.mode FROM trades t JOIN runs r ON r.id = t.run_id "
            "ORDER BY r.mode").fetchall()
        return [r["mode"] for r in rows]

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
