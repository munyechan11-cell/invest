import aiosqlite, json, time, hashlib, secrets, os, logging, jwt, binascii
from datetime import datetime, timedelta
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "toss.db"
_conn: aiosqlite.Connection | None = None

# 보안 설정
SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_urlsafe(32)
    logging.warning("JWT_SECRET_KEY not set — using random per-process key (sessions reset on restart)")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7 

def _hash(pw: str) -> str:
    salt = b"toss_quant_platform_v2_salt" 
    dk = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, 100000)
    return binascii.hexlify(dk).decode()

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        return None


async def get_db() -> aiosqlite.Connection:
    global _conn
    if _conn is None:
        try:
            _conn = await aiosqlite.connect(DB)
            _conn.row_factory = aiosqlite.Row
        except Exception as e:
            import logging
            logging.error(f"Critical DB Error: {e}")
            raise
    return _conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  display_name TEXT NOT NULL DEFAULT '',
  pw_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist (
  symbol TEXT NOT NULL,
  user_id INTEGER NOT NULL DEFAULT 0,
  capital REAL NOT NULL,
  risk_pct REAL NOT NULL DEFAULT 1.0,
  last_position TEXT NOT NULL DEFAULT '관망',
  last_emoji TEXT NOT NULL DEFAULT '⚪',
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
CREATE TABLE IF NOT EXISTS portfolio (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  entry_price REAL NOT NULL,
  shares REAL NOT NULL,
  krw_invested REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
"""


# ── 포트폴리오 (보유 종목) ──────────────────────────────────────
async def add_to_portfolio(user_id: int, symbol: str, entry_price: float, krw_invested: float, shares: float = 0):
    c = await get_db()
    # 수량이 0이면 원화/달러가로 추정 (옵션)
    if shares <= 0 and entry_price > 0:
        shares = krw_invested / (entry_price * 1400) # 대략적 추정 (UI에서 입력받는 것이 정확)
    
    await c.execute(
        "INSERT INTO portfolio(user_id, symbol, entry_price, shares, krw_invested, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (user_id, symbol.upper(), entry_price, shares, krw_invested, time.time())
    )
    await c.commit()

async def list_portfolio(user_id: int) -> list[dict]:
    c = await get_db()
    rows = await (await c.execute(
        "SELECT * FROM portfolio WHERE user_id=? ORDER BY created_at DESC", (user_id,)
    )).fetchall()
    return [dict(r) for r in rows]

async def remove_from_portfolio(pid: int, user_id: int):
    c = await get_db()
    await c.execute("DELETE FROM portfolio WHERE id=? AND user_id=?", (pid, user_id))
    await c.commit()


ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "munyechan11")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")  # .env에 ADMIN_PASSWORD 설정 필요


async def init():
    c = await get_db()
    await c.executescript(SCHEMA)
    # 마이그레이션
    for col, table, default in [
        ("user_id", "watchlist", "INTEGER NOT NULL DEFAULT 0"),
        ("display_name", "users", "TEXT NOT NULL DEFAULT ''"),
        ("is_admin", "users", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        try:
            await c.execute(f"SELECT {col} FROM {table} LIMIT 1")
        except Exception:
            await c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {default}")
    
    # 워치리스트 포지션 컬럼 추가
    for col, default in [("last_position", "TEXT NOT NULL DEFAULT '관망'"), ("last_emoji", "TEXT NOT NULL DEFAULT '⚪'")]:
        try:
            await c.execute(f"SELECT {col} FROM watchlist LIMIT 1")
        except Exception:
            await c.execute(f"ALTER TABLE watchlist ADD COLUMN {col} {default}")
    
    # 포트폴리오 테이블 생성 (스키마에 이미 있으나 혹시 모르니Script 실행)
    await c.executescript(SCHEMA)
    await c.commit()
    
    # 관리자 계정 자동 생성 (ADMIN_PASSWORD가 환경변수에 설정된 경우만)
    if ADMIN_PASSWORD:
        row = await (await c.execute("SELECT id FROM users WHERE username=?", (ADMIN_USERNAME,))).fetchone()
        if not row:
            await c.execute(
                "INSERT INTO users(username, display_name, pw_hash, is_admin, created_at) VALUES(?,?,?,?,?)",
                (ADMIN_USERNAME, "관리자", _hash(ADMIN_PASSWORD), 1, time.time()),
            )
            logging.info(f"Admin user '{ADMIN_USERNAME}' created from ADMIN_PASSWORD env var")
    else:
        logging.warning("ADMIN_PASSWORD not set — admin auto-creation skipped. Set ADMIN_PASSWORD in .env to enable.")
    await c.commit()


# ── 유저 ──────────────────────────────────────────────────
async def check_username(username: str) -> bool:
    """True면 이미 존재하는 아이디."""
    c = await get_db()
    row = await (await c.execute(
        "SELECT id FROM users WHERE username=?", (username,)
    )).fetchone()
    return row is not None


async def register(username: str, password: str, display_name: str = "") -> dict | None:
    c = await get_db()
    existing = await (await c.execute(
        "SELECT id FROM users WHERE username=?", (username,)
    )).fetchone()
    if existing:
        return None
    is_admin = 1 if username == ADMIN_USERNAME else 0
    cursor = await c.execute(
        "INSERT INTO users(username, display_name, pw_hash, is_admin, created_at) VALUES(?,?,?,?,?)",
        (username, display_name, _hash(password), is_admin, time.time()),
    )
    await c.commit()
    user_id = cursor.lastrowid
    token = create_access_token({"sub": str(user_id), "username": username})
    return {"id": user_id, "username": username, "display_name": display_name, "is_admin": is_admin, "access_token": token}


async def login(username: str, password: str) -> dict | None:
    c = await get_db()
    row = await (await c.execute(
        "SELECT * FROM users WHERE username=? AND pw_hash=?",
        (username, _hash(password)),
    )).fetchone()
    if not row:
        return None
    token = create_access_token({"sub": str(row["id"]), "username": row["username"]})
    return {"id": row["id"], "username": row["username"],
            "display_name": row["display_name"], "is_admin": row["is_admin"], "access_token": token}


# ── 관리자 ────────────────────────────────────────────────
async def list_users() -> list[dict]:
    c = await get_db()
    rows = await (await c.execute(
        "SELECT id, username, display_name, is_admin, created_at FROM users ORDER BY id"
    )).fetchall()
    return [dict(r) for r in rows]


async def get_user_by_id(user_id: int) -> dict | None:
    c = await get_db()
    row = await (await c.execute(
        "SELECT * FROM users WHERE id=?", (user_id,)
    )).fetchone()
    return dict(row) if row else None


async def delete_user(user_id: int) -> bool:
    c = await get_db()
    await c.execute("DELETE FROM users WHERE id=? AND is_admin=0", (user_id,))
    await c.execute("DELETE FROM watchlist WHERE user_id=?", (user_id,))
    await c.execute("DELETE FROM trades WHERE user_id=?", (user_id,))
    await c.commit()
    return True


# ── 워치리스트 ────────────────────────────────────────────
async def upsert_watch(symbol: str, capital: float, risk_pct: float, user_id: int = 0):
    c = await get_db()
    await c.execute(
        "INSERT INTO watchlist(symbol,capital,risk_pct,added_at,user_id) VALUES(?,?,?,?,?) "
        "ON CONFLICT(symbol,user_id) DO UPDATE SET capital=excluded.capital, risk_pct=excluded.risk_pct",
        (symbol.upper(), capital, risk_pct, time.time(), user_id),
    )
    await c.commit()


async def remove_watch(symbol: str, user_id: int = 0):
    c = await get_db()
    await c.execute("DELETE FROM watchlist WHERE symbol=? AND user_id=?", (symbol.upper(), user_id))
    await c.commit()


async def update_watch_position(symbol: str, user_id: int, position: str, emoji: str):
    """분석 완료 후 워치리스트 목록의 포지션 정보를 갱신"""
    c = await get_db()
    await c.execute(
        "UPDATE watchlist SET last_position=?, last_emoji=? WHERE symbol=? AND user_id=?",
        (position, emoji, symbol.upper(), user_id)
    )
    await c.commit()


async def list_watch(user_id: int = 0) -> list[dict]:
    c = await get_db()
    rows = await (await c.execute(
        "SELECT * FROM watchlist WHERE user_id=? ORDER BY added_at DESC", (user_id,)
    )).fetchall()
    return [dict(r) for r in rows]


async def list_all_watch() -> list[dict]:
    """모든 유저의 워치리스트 항목을 가져옴 (알림 워커용)"""
    c = await get_db()
    rows = await (await c.execute("SELECT * FROM watchlist")).fetchall()
    return [dict(r) for r in rows]


# ── 분석 계획 및 캐시 ─────────────────────────────────────────────
async def save_plan(symbol: str, payload: dict):
    """분석 결과를 저장하고 캐시 타임스탬프를 갱신"""
    c = await get_db()
    await c.execute(
        "INSERT INTO plans(symbol,payload,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(symbol) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
        (symbol.upper(), json.dumps(payload, ensure_ascii=False), time.time()),
    )
    await c.commit()


async def get_plan(symbol: str, max_age_sec: int = 600) -> dict | None:
    """최근 분석 결과가 있으면 반환 (캐시 기능)"""
    c = await get_db()
    row = await (await c.execute(
        "SELECT payload, updated_at FROM plans WHERE symbol=?", (symbol.upper(),)
    )).fetchone()
    if not row:
        return None
    
    payload, updated_at = row
    # 설정한 시간(기본 10분) 이내의 데이터만 유효한 캐시로 간주
    if time.time() - updated_at > max_age_sec:
        return None
        
    return json.loads(payload)


async def all_plans() -> dict[str, dict]:
    c = await get_db()
    rows = await (await c.execute("SELECT symbol, payload FROM plans")).fetchall()
    return {s: json.loads(p) for s, p in rows}


# ── 알림 ──────────────────────────────────────────────────
async def add_alert(symbol: str, kind: str, message: str, price: float | None):
    c = await get_db()
    await c.execute(
        "INSERT INTO alerts(symbol,kind,message,price,created_at) VALUES(?,?,?,?,?)",
        (symbol.upper(), kind, message, price, time.time()),
    )
    await c.commit()


async def recent_alerts(limit: int = 50) -> list[dict]:
    c = await get_db()
    rows = await (await c.execute(
        "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)
    )).fetchall()
    return [dict(r) for r in rows]


# ── 매매기록 ──────────────────────────────────────────────
async def add_trade(user_id: int, symbol: str, trade_type: str,
                    shares: float, price: float, note: str = "") -> int:
    """매매기록 추가. trade_type: BUY, 익절, 손절, 청산"""
    c = await get_db()
    await c.execute(
        "INSERT INTO trades(user_id,symbol,trade_type,shares,price,note,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (user_id, symbol.upper(), trade_type, shares, price, note, time.time()),
    )
    await c.commit()
    tid = (await (await c.execute("SELECT last_insert_rowid()")).fetchone())[0]
    return tid


async def list_trades(user_id: int) -> list[dict]:
    c = await get_db()
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
