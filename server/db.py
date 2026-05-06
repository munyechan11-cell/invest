import aiosqlite, json, time, hashlib, secrets, os, logging, jwt, binascii
from datetime import datetime, timedelta
from pathlib import Path

# DB 경로 — DB_PATH 환경변수로 오버라이드 가능 (Render Persistent Disk 등)
# 기본값은 레포 root의 sift.db. Render는 컨테이너 휘발성이므로 운영 환경에선
# 반드시 DB_PATH=/var/data/sift.db 같이 영구 디스크 마운트 경로를 지정할 것.
_db_env = os.environ.get("DB_PATH", "").strip()
DB = Path(_db_env) if _db_env else (Path(__file__).resolve().parent.parent / "sift.db")
DB.parent.mkdir(parents=True, exist_ok=True)
_conn: aiosqlite.Connection | None = None

# 보안 설정
SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_urlsafe(32)
    logging.warning("JWT_SECRET_KEY not set — using random per-process key (sessions reset on restart)")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7 

def _hash(pw: str, salt_str: str = "") -> str:
    """비밀번호 해시 — per-user salt 권장.

    - 신규 가입: salt 새로 생성 후 함께 저장 권장
    - 기존(salt 없음): 레거시 고정 salt로 호환 유지 (마이그레이션 스킵)
    """
    if salt_str:
        salt = salt_str.encode()
    else:
        # 레거시 호환 (기존 비번 — 절대 변경 금지! 바꾸면 모든 비번 무효화됨)
        salt = b"toss_quant_platform_v2_salt"
    dk = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, 100000)
    return binascii.hexlify(dk).decode()


def _new_salt() -> str:
    return secrets.token_urlsafe(16)

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


_conn_lock = None  # asyncio.Lock — lazy init


async def get_db() -> aiosqlite.Connection:
    """글로벌 SQLite connection.

    동시성: WAL 모드 + busy_timeout으로 다중 await 안전.
    SQLite는 단일 connection 내 다중 cursor 시리얼화로 안전 (aiosqlite).
    """
    global _conn, _conn_lock
    if _conn_lock is None:
        import asyncio as _aio
        _conn_lock = _aio.Lock()
    if _conn is None:
        async with _conn_lock:
            if _conn is None:  # double-check
                try:
                    _conn = await aiosqlite.connect(DB, timeout=30)
                    _conn.row_factory = aiosqlite.Row
                    # WAL 모드: reader/writer 동시 가능, 동시성 향상
                    await _conn.execute("PRAGMA journal_mode=WAL")
                    await _conn.execute("PRAGMA busy_timeout=5000")
                    await _conn.execute("PRAGMA synchronous=NORMAL")
                    await _conn.execute("PRAGMA foreign_keys=ON")
                    await _conn.commit()
                except Exception as e:
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
CREATE TABLE IF NOT EXISTS user_settings (
  user_id INTEGER PRIMARY KEY,
  alert_cooldown_sec INTEGER NOT NULL DEFAULT 300,
  telegram_min_score REAL NOT NULL DEFAULT 0,
  daily_max_order_krw REAL NOT NULL DEFAULT 300000,
  daily_max_order_usd REAL NOT NULL DEFAULT 200,
  fee_rate_kr REAL NOT NULL DEFAULT 0.00015,
  fee_rate_us REAL NOT NULL DEFAULT 0.0007,
  enable_inline_actions INTEGER NOT NULL DEFAULT 1,
  auto_stoploss INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS daily_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  trade_date TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  qty REAL NOT NULL,
  price REAL NOT NULL,
  amount REAL NOT NULL,
  fee REAL NOT NULL DEFAULT 0,
  market TEXT NOT NULL DEFAULT 'KR',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS realized_pnl (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  qty REAL NOT NULL,
  entry_price REAL NOT NULL,
  exit_price REAL NOT NULL,
  fee REAL NOT NULL DEFAULT 0,
  pnl REAL NOT NULL,
  pnl_pct REAL NOT NULL,
  reason TEXT,
  closed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snoozes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  until_ts REAL NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS price_alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  target_price REAL NOT NULL,
  condition TEXT NOT NULL DEFAULT '>=',
  note TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  triggered_at REAL,
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
  confluence_score INTEGER,
  opened_at REAL NOT NULL,
  closed_at REAL
);
CREATE TABLE IF NOT EXISTS subscriptions (
  user_id INTEGER PRIMARY KEY,
  plan TEXT NOT NULL DEFAULT 'free',
  status TEXT NOT NULL DEFAULT 'active',
  trial_ends_at REAL,
  expires_at REAL,
  discount_percent INTEGER NOT NULL DEFAULT 0,
  discount_until REAL,
  billing_key TEXT,
  applied_coupon_code TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS coupons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  discount_percent INTEGER NOT NULL DEFAULT 0,
  trial_days INTEGER NOT NULL DEFAULT 0,
  duration_months INTEGER,
  max_uses INTEGER,
  used_count INTEGER NOT NULL DEFAULT 0,
  expires_at REAL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS coupon_redemptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  coupon_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  redeemed_at REAL NOT NULL,
  UNIQUE(coupon_id, user_id)
);
CREATE TABLE IF NOT EXISTS payment_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  amount INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'KRW',
  method TEXT,
  status TEXT NOT NULL,
  toss_payment_key TEXT,
  toss_order_id TEXT,
  description TEXT,
  paid_at REAL NOT NULL
);
"""


# ── 포트폴리오 (보유 종목) ──────────────────────────────────────
async def add_to_portfolio(user_id: int, symbol: str, entry_price: float,
                           krw_invested: float, shares: float = 0):
    """포트폴리오 항목 추가.

    수량이 0이면 시장 자동 판별로 정확한 추정:
    - KR (6자리): krw_invested / entry_price (그대로)
    - US: krw_invested / entry_price (이미 USD 단위 가정)
    UI에서 수량 직접 입력 권장.
    """
    sym = symbol.upper()
    c = await get_db()
    if shares <= 0 and entry_price > 0 and krw_invested > 0:
        # 시장과 무관하게 entry_price 단위로 나눔 (사용자가 같은 단위로 입력했다는 가정)
        # 환율 추정 제거 — 부정확한 1400 하드코딩 버그 픽스
        shares = krw_invested / entry_price
    await c.execute(
        "INSERT INTO portfolio(user_id, symbol, entry_price, shares, krw_invested, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (user_id, sym, entry_price, shares, krw_invested, time.time())
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


async def get_portfolio_item(pid: int, user_id: int) -> dict | None:
    c = await get_db()
    row = await (await c.execute(
        "SELECT * FROM portfolio WHERE id=? AND user_id=?", (pid, user_id)
    )).fetchone()
    return dict(row) if row else None


async def average_down(pid: int, user_id: int,
                       additional_shares: float, additional_price: float) -> dict | None:
    """추매 — 새 평단가 자동 계산 (가중 평균).

    new_avg = (old_shares * old_avg + new_shares * new_price) / total_shares
    """
    c = await get_db()
    row = await (await c.execute(
        "SELECT shares, entry_price, krw_invested FROM portfolio WHERE id=? AND user_id=?",
        (pid, user_id)
    )).fetchone()
    if not row:
        return None
    old = dict(row)
    old_shares = float(old["shares"] or 0)
    old_avg = float(old["entry_price"] or 0)
    old_invested = float(old["krw_invested"] or (old_shares * old_avg))

    if additional_shares <= 0 or additional_price <= 0:
        return {"error": "추가 수량과 가격은 0보다 커야 합니다"}

    new_invested = old_invested + (additional_shares * additional_price)
    new_shares = old_shares + additional_shares
    new_avg = new_invested / new_shares if new_shares > 0 else additional_price

    await c.execute(
        "UPDATE portfolio SET shares=?, entry_price=?, krw_invested=? WHERE id=?",
        (new_shares, new_avg, new_invested, pid)
    )
    await c.commit()
    return {
        "ok": True,
        "old_shares": old_shares,
        "old_avg": round(old_avg, 2),
        "added_shares": additional_shares,
        "added_price": additional_price,
        "new_shares": new_shares,
        "new_avg": round(new_avg, 2),
        "avg_change_pct": round((new_avg / old_avg - 1) * 100, 2) if old_avg > 0 else 0,
        "total_invested": round(new_invested, 2),
    }


async def list_all_portfolio() -> list[dict]:
    """모든 유저의 포트폴리오 항목 (실시간 폴링 워커용)."""
    c = await get_db()
    rows = await (await c.execute("SELECT * FROM portfolio")).fetchall()
    return [dict(r) for r in rows]


# ── 사용자 설정 ────────────────────────────────────────────
DEFAULT_SETTINGS = {
    "alert_cooldown_sec": 300,
    "telegram_min_score": 0,
    "daily_max_order_krw": 300000,
    "daily_max_order_usd": 200,
    "fee_rate_kr": 0.00015,
    "fee_rate_us": 0.0007,
    "enable_inline_actions": 1,
    "auto_stoploss": 0,    # 1=손절가 도달 시 자동 매도
}


async def get_user_settings(user_id: int) -> dict:
    c = await get_db()
    row = await (await c.execute(
        "SELECT * FROM user_settings WHERE user_id=?", (user_id,)
    )).fetchone()
    if not row:
        return dict(DEFAULT_SETTINGS)
    d = dict(row)
    d.pop("user_id", None)
    d.pop("updated_at", None)
    return d


async def update_user_settings(user_id: int, settings: dict):
    allowed = set(DEFAULT_SETTINGS.keys())
    cur = await get_user_settings(user_id)
    cur.update({k: v for k, v in settings.items() if k in allowed})
    c = await get_db()
    cols = list(DEFAULT_SETTINGS.keys())
    placeholders = ",".join(["?"] * (len(cols) + 2))  # +user_id +updated_at
    fields = "user_id," + ",".join(cols) + ",updated_at"
    values = [user_id] + [cur[k] for k in cols] + [time.time()]
    await c.execute(
        f"INSERT OR REPLACE INTO user_settings({fields}) VALUES({placeholders})",
        values
    )
    await c.commit()
    return cur


# ── 일일 주문 한도 추적 ────────────────────────────────────
async def record_order(user_id: int, symbol: str, side: str,
                       qty: float, price: float, market: str = "KR",
                       fee: float = 0) -> int:
    from datetime import datetime
    c = await get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    amount = qty * price
    cur = await c.execute(
        "INSERT INTO daily_orders(user_id,trade_date,symbol,side,qty,price,amount,fee,market,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (user_id, today, symbol, side, qty, price, amount, fee, market, time.time())
    )
    await c.commit()
    return cur.lastrowid


async def daily_used(user_id: int, market: str = "KR") -> float:
    """오늘 사용된 주문 누적 금액."""
    from datetime import datetime
    c = await get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    row = await (await c.execute(
        "SELECT COALESCE(SUM(amount),0) AS s FROM daily_orders "
        "WHERE user_id=? AND trade_date=? AND market=?",
        (user_id, today, market)
    )).fetchone()
    return float(row[0]) if row else 0.0


# ── 실현 손익 ─────────────────────────────────────────────
async def record_realized_pnl(user_id: int, symbol: str, qty: float,
                              entry_price: float, exit_price: float,
                              fee: float = 0, reason: str = "") -> dict:
    pnl_gross = (exit_price - entry_price) * qty
    pnl = pnl_gross - fee
    pnl_pct = ((exit_price / entry_price - 1) * 100) if entry_price > 0 else 0
    c = await get_db()
    await c.execute(
        "INSERT INTO realized_pnl(user_id,symbol,qty,entry_price,exit_price,fee,pnl,pnl_pct,reason,closed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (user_id, symbol.upper(), qty, entry_price, exit_price, fee,
         pnl, pnl_pct, reason, time.time())
    )
    await c.commit()
    return {"pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2), "fee": round(fee, 2)}


async def list_realized_pnl(user_id: int, days: int = 30) -> list[dict]:
    c = await get_db()
    cutoff = time.time() - days * 86400
    rows = await (await c.execute(
        "SELECT * FROM realized_pnl WHERE user_id=? AND closed_at>=? ORDER BY closed_at DESC",
        (user_id, cutoff)
    )).fetchall()
    return [dict(r) for r in rows]


async def realized_pnl_summary(user_id: int) -> dict:
    rows = await list_realized_pnl(user_id, days=365)
    if not rows:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "total_pnl": 0, "avg_pnl_pct": 0, "best_pct": 0, "worst_pct": 0}
    pnls = [r["pnl_pct"] for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    return {
        "trades": len(rows), "wins": wins, "losses": len(rows) - wins,
        "win_rate": round(wins / len(rows) * 100, 1),
        "total_pnl": round(sum(r["pnl"] for r in rows), 2),
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 2),
        "best_pct": round(max(pnls), 2),
        "worst_pct": round(min(pnls), 2),
    }


# ── 스누즈 (인라인 버튼) ──────────────────────────────────
async def add_snooze(user_id: int, symbol: str, minutes: int):
    c = await get_db()
    until = time.time() + minutes * 60
    await c.execute(
        "INSERT INTO snoozes(user_id,symbol,until_ts,created_at) VALUES(?,?,?,?)",
        (user_id, symbol.upper(), until, time.time())
    )
    await c.commit()


async def is_snoozed(user_id: int, symbol: str) -> bool:
    c = await get_db()
    row = await (await c.execute(
        "SELECT 1 FROM snoozes WHERE user_id=? AND symbol=? AND until_ts>?",
        (user_id, symbol.upper(), time.time())
    )).fetchone()
    return row is not None


# ── 사용자 지정 가격 알림 ──────────────────────────────────
async def add_price_alert(user_id: int, symbol: str, target_price: float,
                          condition: str = ">=", note: str = "") -> int:
    if condition not in (">=", "<=", "=="):
        condition = ">="
    c = await get_db()
    cur = await c.execute(
        "INSERT INTO price_alerts(user_id,symbol,target_price,condition,note,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (user_id, symbol.upper(), target_price, condition, note, time.time())
    )
    await c.commit()
    return cur.lastrowid


async def list_user_price_alerts(user_id: int, only_active: bool = True) -> list[dict]:
    c = await get_db()
    q = "SELECT * FROM price_alerts WHERE user_id=?"
    if only_active:
        q += " AND active=1"
    q += " ORDER BY created_at DESC"
    rows = await (await c.execute(q, (user_id,))).fetchall()
    return [dict(r) for r in rows]


async def list_active_price_alerts() -> list[dict]:
    """워커가 가격 도달 체크용 — 모든 유저의 활성 알림."""
    c = await get_db()
    rows = await (await c.execute(
        "SELECT * FROM price_alerts WHERE active=1"
    )).fetchall()
    return [dict(r) for r in rows]


async def trigger_price_alert(alert_id: int):
    c = await get_db()
    await c.execute(
        "UPDATE price_alerts SET active=0, triggered_at=? WHERE id=?",
        (time.time(), alert_id)
    )
    await c.commit()


async def delete_price_alert(alert_id: int, user_id: int):
    c = await get_db()
    await c.execute(
        "DELETE FROM price_alerts WHERE id=? AND user_id=?",
        (alert_id, user_id)
    )
    await c.commit()


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
                                  stop: float | None,
                                  confluence_score: int | None = None) -> int:
    """모의 트레이드 추적이 켜진 모든 유저에게 자동 기록.

    confluence_score: 알림 발송 시점의 confluence 점수 (0-5). 백테스트 승률
    분석용 — 점수별 win rate 비교 가능.
    """
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
            "INSERT INTO mock_trades(user_id,symbol,side,entry_price,target_price,stop_price,"
            "confluence_score,opened_at) VALUES(?,?,?,?,?,?,?,?)",
            (r["id"], symbol.upper(), side, entry, target, stop,
             confluence_score, time.time())
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


async def close_mock_trade(trade_id: int, exit_price: float, exit_reason: str) -> dict | None:
    """모의 트레이드 청산. 청산된 trade row(symbol, user_id, pnl_pct 등)을
    반환 — 호출자가 텔레그램 결과 알림 발송 가능."""
    c = await get_db()
    row = await (await c.execute(
        "SELECT id, user_id, symbol, entry_price, side, confluence_score "
        "FROM mock_trades WHERE id=?", (trade_id,)
    )).fetchone()
    if not row:
        return None
    entry = float(row["entry_price"])
    side = row["side"]
    if entry <= 0:
        pnl_pct = 0
    elif side == "buy":
        pnl_pct = (exit_price / entry - 1) * 100
    else:  # sell (숏)
        pnl_pct = (entry / exit_price - 1) * 100 if exit_price > 0 else 0
    pnl_pct_r = round(pnl_pct, 2)
    await c.execute(
        "UPDATE mock_trades SET exit_price=?, exit_reason=?, status='closed', "
        "pnl_pct=?, closed_at=? WHERE id=?",
        (exit_price, exit_reason, pnl_pct_r, time.time(), trade_id)
    )
    await c.commit()
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "symbol": row["symbol"],
        "side": side,
        "entry_price": entry,
        "exit_price": exit_price,
        "pnl_pct": pnl_pct_r,
        "exit_reason": exit_reason,
        "confluence_score": row["confluence_score"],
    }


async def list_user_mock_trades(user_id: int, limit: int = 100) -> list[dict]:
    c = await get_db()
    rows = await (await c.execute(
        "SELECT * FROM mock_trades WHERE user_id=? ORDER BY opened_at DESC LIMIT ?",
        (user_id, limit)
    )).fetchall()
    return [dict(r) for r in rows]


async def mock_trade_stats(user_id: int) -> dict:
    """전체 통계 + confluence 점수별 breakdown (백테스트 — 어느 점수대가
    실제로 승률 높은지 사용자가 직접 확인 가능)."""
    c = await get_db()
    rows = await (await c.execute(
        "SELECT pnl_pct, exit_reason, status, confluence_score "
        "FROM mock_trades WHERE user_id=?",
        (user_id,)
    )).fetchall()
    closed = [dict(r) for r in rows if r["status"] == "closed" and r["pnl_pct"] is not None]
    open_count = sum(1 for r in rows if r["status"] == "open")

    base = {
        "total_signals": len(rows),
        "open": open_count,
        "closed": len(closed),
        "wins": 0, "losses": 0, "win_rate": 0,
        "avg_return_pct": 0, "best_pct": 0, "worst_pct": 0,
        "cumulative_pct": 0,
        "by_confluence": [],
    }

    if not closed:
        return base

    pnls = [r["pnl_pct"] for r in closed]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)

    # Confluence 점수별 breakdown (5/5, 4/5, 3/5, ...) — 추적 가능한 closed 만
    by_conf: dict[int, list[float]] = {}
    for r in closed:
        cs = r.get("confluence_score")
        if cs is None:
            continue
        by_conf.setdefault(int(cs), []).append(float(r["pnl_pct"]))
    confluence_breakdown = []
    for score in sorted(by_conf.keys(), reverse=True):
        ps = by_conf[score]
        w = sum(1 for p in ps if p > 0)
        confluence_breakdown.append({
            "score": score,
            "trades": len(ps),
            "wins": w,
            "losses": len(ps) - w,
            "win_rate": round(w / len(ps) * 100, 1),
            "avg_return_pct": round(sum(ps) / len(ps), 2),
        })

    return {
        "total_signals": len(rows),
        "open": open_count,
        "closed": len(closed),
        "wins": wins, "losses": losses,
        "win_rate": round(wins / len(pnls) * 100, 1) if pnls else 0,
        "avg_return_pct": round(sum(pnls) / len(pnls), 2),
        "best_pct": round(max(pnls), 2),
        "worst_pct": round(min(pnls), 2),
        "cumulative_pct": round(sum(pnls), 2),
        "by_confluence": confluence_breakdown,
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

    # user_settings 신규 컬럼 마이그레이션
    try:
        await c.execute("SELECT auto_stoploss FROM user_settings LIMIT 1")
    except Exception:
        try:
            await c.execute("ALTER TABLE user_settings ADD COLUMN auto_stoploss INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass

    # mock_trades.confluence_score 마이그레이션 (백테스트 승률 분석용)
    try:
        await c.execute("SELECT confluence_score FROM mock_trades LIMIT 1")
    except Exception:
        try:
            await c.execute("ALTER TABLE mock_trades ADD COLUMN confluence_score INTEGER")
        except Exception:
            pass
    
    # 인덱스 추가 — 폴링 워커의 풀스캔 방지
    await c.executescript("""
        CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id);
        CREATE INDEX IF NOT EXISTS idx_portfolio_user ON portfolio(user_id);
        CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_mock_open ON mock_trades(status, symbol);
        CREATE INDEX IF NOT EXISTS idx_pricealerts_active ON price_alerts(active, symbol);
        CREATE INDEX IF NOT EXISTS idx_trades_user ON trades(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_payment_user ON payment_history(user_id, paid_at DESC);
    """)
    await c.commit()

    # 시드 쿠폰 — 최초 1회만 생성, 이미 있으면 스킵 (운영 중 코드 수정해도 덮어쓰지 않음)
    seed_coupons = [
        # (code, description, discount_percent, trial_days, duration_months, max_uses)
        ("FREE100",   "1개월 100% 무료 (지인·교수님)",     100,  0,  1,    None),
        ("FIRST30",   "첫 달 30% 할인 (가입 환영)",         30,  0,  1,    None),
        ("SPECIAL20", "7일 무료 + 12개월 20% 할인 (특가)",  20,  7,  12,   None),
    ]
    for code, desc, pct, trial, months, maxu in seed_coupons:
        existing = await (await c.execute(
            "SELECT id FROM coupons WHERE code=?", (code,)
        )).fetchone()
        if not existing:
            await c.execute(
                "INSERT INTO coupons(code, description, discount_percent, trial_days, "
                "duration_months, max_uses, created_at) VALUES(?,?,?,?,?,?,?)",
                (code, desc, pct, trial, months, maxu, time.time())
            )
            logging.info(f"Seed coupon created: {code}")
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


async def search_alerts(symbol: str = "", kind: str = "",
                        days: int = 7, q: str = "",
                        limit: int = 100) -> list[dict]:
    """알림 audit log 검색.

    symbol: 종목코드 부분 일치 (대소문자 무관)
    kind: BUY/SELL/TP/SL/CUSTOM/DART/SCREENER 정확 일치
    days: 최근 N일 (기본 7일)
    q: 메시지 본문 부분 일치 (LIKE %q%)
    """
    cutoff = time.time() - days * 86400
    where = ["created_at >= ?"]
    args: list = [cutoff]

    if symbol:
        where.append("UPPER(symbol) LIKE ?")
        args.append(f"%{symbol.upper()}%")
    if kind:
        where.append("kind = ?")
        args.append(kind.upper())
    if q:
        where.append("message LIKE ?")
        args.append(f"%{q}%")

    sql = (f"SELECT * FROM alerts WHERE {' AND '.join(where)} "
           f"ORDER BY id DESC LIMIT ?")
    args.append(min(limit, 500))

    c = await get_db()
    rows = await (await c.execute(sql, args)).fetchall()
    return [dict(r) for r in rows]


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


# ── 구독 (subscriptions) ────────────────────────────────────
async def get_subscription(user_id: int) -> dict:
    """사용자 구독 row 반환. 없으면 free 기본값 dict."""
    c = await get_db()
    row = await (await c.execute(
        "SELECT * FROM subscriptions WHERE user_id=?", (user_id,)
    )).fetchone()
    if row:
        return dict(row)
    return {
        "user_id": user_id, "plan": "free", "status": "active",
        "trial_ends_at": None, "expires_at": None,
        "discount_percent": 0, "discount_until": None,
        "billing_key": None, "applied_coupon_code": None,
        "created_at": 0, "updated_at": 0,
    }


async def upsert_subscription(user_id: int, **fields):
    """구독 row 생성/갱신 (지정한 필드만 업데이트)."""
    cur = await get_subscription(user_id)
    cur.update(fields)
    cur["updated_at"] = time.time()
    if not cur.get("created_at"):
        cur["created_at"] = time.time()
    c = await get_db()
    await c.execute(
        "INSERT OR REPLACE INTO subscriptions("
        "user_id, plan, status, trial_ends_at, expires_at, discount_percent, "
        "discount_until, billing_key, applied_coupon_code, created_at, updated_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, cur["plan"], cur["status"], cur["trial_ends_at"], cur["expires_at"],
         cur["discount_percent"], cur["discount_until"], cur["billing_key"],
         cur["applied_coupon_code"], cur["created_at"], cur["updated_at"])
    )
    await c.commit()
    return cur


# ── 쿠폰 ────────────────────────────────────────────────────
async def get_coupon_by_code(code: str) -> dict | None:
    c = await get_db()
    row = await (await c.execute(
        "SELECT * FROM coupons WHERE code=?", (code.strip().upper(),)
    )).fetchone()
    return dict(row) if row else None


async def has_redeemed(coupon_id: int, user_id: int) -> bool:
    c = await get_db()
    row = await (await c.execute(
        "SELECT 1 FROM coupon_redemptions WHERE coupon_id=? AND user_id=?",
        (coupon_id, user_id)
    )).fetchone()
    return row is not None


async def redeem_coupon(coupon_id: int, user_id: int):
    """쿠폰 사용 기록 + used_count 증가 (트랜잭션)."""
    c = await get_db()
    await c.execute(
        "INSERT INTO coupon_redemptions(coupon_id, user_id, redeemed_at) VALUES(?,?,?)",
        (coupon_id, user_id, time.time())
    )
    await c.execute(
        "UPDATE coupons SET used_count = used_count + 1 WHERE id=?",
        (coupon_id,)
    )
    await c.commit()


async def list_coupons(active_only: bool = True) -> list[dict]:
    c = await get_db()
    q = "SELECT * FROM coupons"
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY created_at DESC"
    rows = await (await c.execute(q)).fetchall()
    return [dict(r) for r in rows]


# ── 결제 내역 (Phase 2 본격 사용) ───────────────────────────
async def add_payment(user_id: int, amount: int, status: str,
                      method: str | None = None, currency: str = "KRW",
                      toss_payment_key: str | None = None,
                      toss_order_id: str | None = None,
                      description: str | None = None) -> int:
    c = await get_db()
    cur = await c.execute(
        "INSERT INTO payment_history(user_id,amount,currency,method,status,"
        "toss_payment_key,toss_order_id,description,paid_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (user_id, amount, currency, method, status,
         toss_payment_key, toss_order_id, description, time.time())
    )
    await c.commit()
    return cur.lastrowid


async def list_user_payments(user_id: int, limit: int = 50) -> list[dict]:
    c = await get_db()
    rows = await (await c.execute(
        "SELECT * FROM payment_history WHERE user_id=? ORDER BY paid_at DESC LIMIT ?",
        (user_id, limit)
    )).fetchall()
    return [dict(r) for r in rows]
