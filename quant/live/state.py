"""Durable state for live sessions.

A live bot restarts — after a deploy, a crash, an OOM kill. Without persistence
it wakes up believing it holds nothing, and the first thing it does is buy a
position it already has. SQLite (stdlib, no server, one file) is the right size
of tool for this.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from quant.core.account import Portfolio
from quant.core.types import UTC, ClosedTrade, Fill, Order, Symbol

log = logging.getLogger("quant.live.state")

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

    # ── runs ─────────────────────────────────────────────────────────────
    def start_run(self, strategy: str, mode: str, starting_cash: float,
                  config_json: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(strategy, mode, started_at, starting_cash, config_json) "
            "VALUES(?,?,?,?,?)",
            (strategy, mode, datetime.now(UTC).isoformat(), starting_cash, config_json),
        )
        self.conn.commit()
        self.run_id = cur.lastrowid
        return self.run_id

    def resume_run(self, strategy: str, mode: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM runs WHERE strategy=? AND mode=? AND stopped_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (strategy, mode)
        ).fetchone()
        if row:
            self.run_id = row["id"]
        return self.run_id

    def stop_run(self) -> None:
        if self.run_id is None:
            return
        self.conn.execute("UPDATE runs SET stopped_at=? WHERE id=?",
                          (datetime.now(UTC).isoformat(), self.run_id))
        self.conn.commit()

    # ── writes ───────────────────────────────────────────────────────────
    def record_fill(self, fill: Fill) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO fills(id, run_id, ts, symbol, venue, side, quantity, "
            "price, fee, order_id, liquidity) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (fill.id, self.run_id, fill.ts.isoformat(), fill.symbol.ticker,
             fill.symbol.venue, fill.side.value, str(fill.quantity), fill.price,
             fill.fee, fill.order_id, fill.liquidity),
        )
        self.conn.commit()

    def record_trade(self, trade: ClosedTrade) -> None:
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
        self.conn.execute(
            "INSERT OR REPLACE INTO equity(run_id, ts, equity, cash, drawdown) "
            "VALUES(?,?,?,?,?)",
            (self.run_id, ts.isoformat(), equity, cash, drawdown),
        )
        self.conn.commit()

    def snapshot_positions(self, portfolio: Portfolio) -> None:
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

    def record_event(self, event_type: str, payload: dict) -> None:
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
        self.conn.close()
