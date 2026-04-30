"""SQLite — 워치리스트 + 마지막 분석 계획."""
from __future__ import annotations
import aiosqlite, json, time
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "toss.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
  symbol TEXT PRIMARY KEY,
  capital REAL NOT NULL,
  risk_pct REAL NOT NULL DEFAULT 1.0,
  added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
  symbol TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  kind TEXT NOT NULL,
  message TEXT NOT NULL,
  price REAL,
  created_at REAL NOT NULL
);
"""


async def init():
    async with aiosqlite.connect(DB) as c:
        await c.executescript(SCHEMA)
        await c.commit()


async def upsert_watch(symbol: str, capital: float, risk_pct: float):
    async with aiosqlite.connect(DB) as c:
        await c.execute(
            "INSERT INTO watchlist(symbol,capital,risk_pct,added_at) VALUES(?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET capital=excluded.capital, risk_pct=excluded.risk_pct",
            (symbol.upper(), capital, risk_pct, time.time()),
        )
        await c.commit()


async def remove_watch(symbol: str):
    async with aiosqlite.connect(DB) as c:
        await c.execute("DELETE FROM watchlist WHERE symbol=?", (symbol.upper(),))
        await c.execute("DELETE FROM plans WHERE symbol=?", (symbol.upper(),))
        await c.commit()


async def list_watch() -> list[dict]:
    async with aiosqlite.connect(DB) as c:
        c.row_factory = aiosqlite.Row
        rows = await (await c.execute("SELECT * FROM watchlist ORDER BY added_at DESC")).fetchall()
        return [dict(r) for r in rows]


async def save_plan(symbol: str, payload: dict):
    async with aiosqlite.connect(DB) as c:
        await c.execute(
            "INSERT INTO plans(symbol,payload,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (symbol.upper(), json.dumps(payload, ensure_ascii=False), time.time()),
        )
        await c.commit()


async def get_plan(symbol: str) -> dict | None:
    async with aiosqlite.connect(DB) as c:
        row = await (await c.execute(
            "SELECT payload FROM plans WHERE symbol=?", (symbol.upper(),)
        )).fetchone()
        return json.loads(row[0]) if row else None


async def all_plans() -> dict[str, dict]:
    async with aiosqlite.connect(DB) as c:
        rows = await (await c.execute("SELECT symbol, payload FROM plans")).fetchall()
        return {s: json.loads(p) for s, p in rows}


async def add_alert(symbol: str, kind: str, message: str, price: float | None):
    async with aiosqlite.connect(DB) as c:
        await c.execute(
            "INSERT INTO alerts(symbol,kind,message,price,created_at) VALUES(?,?,?,?,?)",
            (symbol.upper(), kind, message, price, time.time()),
        )
        await c.commit()


async def recent_alerts(limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(DB) as c:
        c.row_factory = aiosqlite.Row
        rows = await (await c.execute(
            "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)
        )).fetchall()
        return [dict(r) for r in rows]
