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
CREATE TABLE IF NOT EXISTS mock_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  entry_price REAL NOT NULL,
  target_price REAL,
  stop_price REAL,
  exit_price REAL,
  exit_reason TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  pnl_pct REAL,
  opened_at REAL NOT NULL,
  closed_at REAL
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


async def list_all_portfolio() -> list[dict]:
    """모든 유저의 포트폴리오 항목 (실시간 폴링 워커용)."""
    c = await get_db()
    rows = await (await c.execute("SELECT * FROM portfolio")).fetchall()
    return [dict(r) for r in rows]


# ── Telegram 알림 ─────────────────────────────────────────
async def set_telegram_chat_id(user_id: int, chat_id: str):
    c = await get_db()
    await c.execute("UPDATE users SET telegram_chat_id=? WHERE id=?",
                    (chat_id.strip(), user_id))
    await c.commit()


async def get_telegram_chat_id(user_id: int) -> str:
    c = await get_db()
    row = await (await c.execute(
        "SELECT telegram_chat_id FROM users WHERE id=?", (user_id,)
    )).fetchone()
    return (dict(row).get("telegram_chat_id") if row else "") or ""


async def all_telegram_subscribers() -> list[tuple[int, str]]:
    """알림 발송 대상 (user_id, chat_id) 목록."""
    c = await get_db()
    rows = await (await c.execute(
        "SELECT id, telegram_chat_id FROM users WHERE telegram_chat_id != ''"
    )).fetchall()
    return [(r["id"], r["telegram_chat_id"]) for r in rows]


# ── 모의 트레이드 (시그널 성과 트래커) ────────────────────
async def open_mock_trade(user_id: int, symbol: str, side: str,
                          entry: float, target: float | None,
                          stop: float | None) -> int:
    c = await get_db()
    cursor = await c.execute(
        "INSERT INTO mock_trades(user_id,symbol,side,entry_price,target_price,stop_price,opened_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (user_id, symbol.upper(), side, entry, target, stop, time.time())
    )
    await c.commit()
    return cursor.lastrowid


async def open_mock_trade_for_all(symbol: str, side: str,
                                  entry: float, target: float | None,
                                  stop: float | None) -> int:
    """모의 트레이드 추적이 켜진 모든 유저에게 자동 기록."""
    c = await get_db()
    rows = await (await c.execute(
        "SELECT id FROM users WHERE mock_trade_enabled=1"
    )).fetchall()
    count = 0
    for r in rows:
        # 같은 종목 같은 방향 미완료 트레이드 중복 방지
        existing = await (await c.execute(
            "SELECT id FROM mock_trades WHERE user_id=? AND symbol=? AND side=? AND status='open'",
            (r["id"], symbol.upper(), side)
        )).fetchone()
        if existing:
            continue
        await c.execute(
            "INSERT INTO mock_trades(user_id,symbol,side,entry_price,target_price,stop_price,opened_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (r["id"], symbol.upper(), side, entry, target, stop, time.time())
        )
        count += 1
    await c.commit()
    return count


async def list_open_mock_trades() -> list[dict]:
    """워커에서 가격 도달 체크용."""
    c = await get_db()
    rows = await (await c.execute(
        "SELECT * FROM mock_trades WHERE status='open'"
    )).fetchall()
    return [dict(r) for r in rows]


async def close_mock_trade(trade_id: int, exit_price: float, exit_reason: str):
    c = await get_db()
    row = await (await c.execute(
        "SELECT entry_price, side FROM mock_trades WHERE id=?", (trade_id,)
    )).fetchone()
    if not row:
        return
    entry = float(row["entry_price"])
    side = row["side"]
    if entry <= 0:
        pnl_pct = 0
    elif side == "buy":
        pnl_pct = (exit_price / entry - 1) * 100
    else:  # sell (숏)
        pnl_pct = (entry / exit_price - 1) * 100 if exit_price > 0 else 0
    await c.execute(
        "UPDATE mock_trades SET exit_price=?, exit_reason=?, status='closed', "
        "pnl_pct=?, closed_at=? WHERE id=?",
        (exit_price, exit_reason, round(pnl_pct, 2), time.time(), trade_id)
    )
    await c.commit()


async def list_user_mock_trades(user_id: int, limit: int = 100) -> list[dict]:
    c = await get_db()
    rows = await (await c.execute(
        "SELECT * FROM mock_trades WHERE user_id=? ORDER BY opened_at DESC LIMIT ?",
        (user_id, limit)
    )).fetchall()
    return [dict(r) for r in rows]


async def mock_trade_stats(user_id: int) -> dict:
    c = await get_db()
    rows = await (await c.execute(
        "SELECT pnl_pct, exit_reason, status FROM mock_trades WHERE user_id=?",
        (user_id,)
    )).fetchall()
    closed = [dict(r) for r in rows if r["status"] == "closed" and r["pnl_pct"] is not None]
    open_count = sum(1 for r in rows if r["status"] == "open")

    if not closed:
        return {
            "total_signals": len(rows), "open": open_count, "closed": 0,
            "wins": 0, "losses": 0, "win_rate": 0,
            "avg_return_pct": 0, "best_pct": 0, "worst_pct": 0,
            "cumulative_pct": 0,
        }

    pnls = [r["pnl_pct"] for r in closed]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    return {
        "total_signals": len(rows),
        "open": open_count, "closed": len(closed),
        "wins": wins, "losses": losses,
        "win_rate": round(wins / len(pnls) * 100, 1) if pnls else 0,
        "avg_return_pct": round(sum(pnls) / len(pnls), 2),
        "best_pct": round(max(pnls), 2),
        "worst_pct": round(min(pnls), 2),
        "cumulative_pct": round(sum(pnls), 2),
    }


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
        ("telegram_chat_id", "users", "TEXT NOT NULL DEFAULT ''"),
        ("mock_trade_enabled", "users", "INTEGER NOT NULL DEFAULT 1"),
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
    
    # 관리자 계정 자동 생성 및 동기화 (보안: 디폴트 비번 절대 금지)
    admin_user = os.environ.get("ADMIN_USERNAME", "").strip()
    admin_pw = os.environ.get("ADMIN_PASSWORD", "").strip()

    if not admin_user or not admin_pw:
        logging.warning(
            "ADMIN_USERNAME 또는 ADMIN_PASSWORD가 비어있음 — 관리자 자동 생성 건너뜀. "
            "관리자가 필요하면 .env에 두 값을 모두 설정하세요."
        )
    elif len(admin_pw) < 8:
        logging.error(
            f"ADMIN_PASSWORD가 너무 짧습니다(8자 미만) — 보안상 관리자 생성 거부. "
            f".env에 강한 비밀번호를 설정하세요."
        )
    else:
        row = await (await c.execute(
            "SELECT id, pw_hash FROM users WHERE username=?", (admin_user,)
        )).fetchone()
        new_hash = _hash(admin_pw)
        if not row:
            await c.execute(
                "INSERT INTO users(username, display_name, pw_hash, is_admin, created_at) VALUES(?,?,?,?,?)",
                (admin_user, "관리자", new_hash, 1, time.time()),
            )
            logging.info(f"Admin user '{admin_user}' created from .env")
        elif dict(row)["pw_hash"] != new_hash:
            await c.execute("UPDATE users SET pw_hash=? WHERE username=?", (new_hash, admin_user))
            logging.info(f"Admin password for '{admin_user}' updated from .env")

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
    # 일반 가입자는 절대 관리자가 될 수 없음 (is_admin = 0 고정)
    is_admin = 0
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
