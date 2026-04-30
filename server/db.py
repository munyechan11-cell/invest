"""SQLite — 유저 + 워치리스트 + 매매기록 + 분석 계획."""
from __future__ import annotations
import aiosqlite, json, time, hashlib, secrets
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "toss.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  pw_hash TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist (
  symbol TEXT NOT NULL,
  user_id INTEGER NOT NULL DEFAULT 0,
  capital REAL NOT NULL,
  risk_pct REAL NOT NULL DEFAULT 1.0,
  added_at REAL NOT NULL,
  PRIMARY KEY (symbol, user_id)
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
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  trade_type TEXT NOT NULL,
  shares REAL NOT NULL,
  price REAL NOT NULL,
  note TEXT DEFAULT '',
  created_at REAL NOT NULL
);
"""


def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


async def init():
    async with aiosqlite.connect(DB) as c:
        await c.executescript(SCHEMA)
        # 기존 watchlist에 user_id 컬럼이 없을 수 있으므로 안전하게 마이그레이션
        try:
            await c.execute("SELECT user_id FROM watchlist LIMIT 1")
        except Exception:
            await c.execute("ALTER TABLE watchlist ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")
        await c.commit()


# ── 유저 ──────────────────────────────────────────────────
async def register(username: str, password: str) -> dict | None:
    async with aiosqlite.connect(DB) as c:
        try:
            await c.execute(
                "INSERT INTO users(username, pw_hash, created_at) VALUES(?,?,?)",
                (username, _hash(password), time.time()),
            )
            await c.commit()
            uid = (await (await c.execute("SELECT last_insert_rowid()")).fetchone())[0]
            return {"id": uid, "username": username}
        except Exception:
            return None


async def login(username: str, password: str) -> dict | None:
    async with aiosqlite.connect(DB) as c:
        c.row_factory = aiosqlite.Row
        row = await (await c.execute(
            "SELECT * FROM users WHERE username=? AND pw_hash=?",
            (username, _hash(password)),
        )).fetchone()
        return {"id": row["id"], "username": row["username"]} if row else None


# ── 워치리스트 ────────────────────────────────────────────
async def upsert_watch(symbol: str, capital: float, risk_pct: float, user_id: int = 0):
    async with aiosqlite.connect(DB) as c:
        await c.execute(
            "INSERT INTO watchlist(symbol,capital,risk_pct,added_at,user_id) VALUES(?,?,?,?,?) "
            "ON CONFLICT(symbol,user_id) DO UPDATE SET capital=excluded.capital, risk_pct=excluded.risk_pct",
            (symbol.upper(), capital, risk_pct, time.time(), user_id),
        )
        await c.commit()


async def remove_watch(symbol: str, user_id: int = 0):
    async with aiosqlite.connect(DB) as c:
        await c.execute("DELETE FROM watchlist WHERE symbol=? AND user_id=?", (symbol.upper(), user_id))
        await c.commit()


async def list_watch(user_id: int = 0) -> list[dict]:
    async with aiosqlite.connect(DB) as c:
        c.row_factory = aiosqlite.Row
        rows = await (await c.execute(
            "SELECT * FROM watchlist WHERE user_id=? ORDER BY added_at DESC", (user_id,)
        )).fetchall()
        return [dict(r) for r in rows]


# ── 분석 계획 ─────────────────────────────────────────────
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


# ── 알림 ──────────────────────────────────────────────────
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


# ── 매매기록 ──────────────────────────────────────────────
async def add_trade(user_id: int, symbol: str, trade_type: str,
                    shares: float, price: float, note: str = "") -> int:
    """매매기록 추가. trade_type: BUY, 익절, 손절, 청산"""
    async with aiosqlite.connect(DB) as c:
        await c.execute(
            "INSERT INTO trades(user_id,symbol,trade_type,shares,price,note,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (user_id, symbol.upper(), trade_type, shares, price, note, time.time()),
        )
        await c.commit()
        tid = (await (await c.execute("SELECT last_insert_rowid()")).fetchone())[0]
        return tid


async def list_trades(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB) as c:
        c.row_factory = aiosqlite.Row
        rows = await (await c.execute(
            "SELECT * FROM trades WHERE user_id=? ORDER BY created_at DESC", (user_id,)
        )).fetchall()
        return [dict(r) for r in rows]


async def portfolio_summary(user_id: int) -> dict:
    """총 수익/손실 요약 계산."""
    trades = await list_trades(user_id)
    if not trades:
        return {"total_invested": 0, "total_returned": 0, "total_pnl": 0, "total_pnl_pct": 0, "trades_count": 0}

    total_invested = 0.0
    total_returned = 0.0
    for t in trades:
        amount = t["shares"] * t["price"]
        if t["trade_type"] == "BUY":
            total_invested += amount
        else:  # 익절, 손절, 청산
            total_returned += amount

    pnl = total_returned - total_invested
    pnl_pct = (pnl / total_invested * 100) if total_invested > 0 else 0

    return {
        "total_invested": round(total_invested, 2),
        "total_returned": round(total_returned, 2),
        "total_pnl": round(pnl, 2),
        "total_pnl_pct": round(pnl_pct, 2),
        "trades_count": len(trades),
    }
