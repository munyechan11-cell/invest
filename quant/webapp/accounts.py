"""사용자 계정과 자격증명 보관 — 여러 사람이 쓰는 서비스의 바닥.

1인용일 때는 자격증명이 `.env` 한 장이면 됐습니다. 그건 서버 주인과 계좌
주인이 같은 사람이라는 전제였고, 그 전제가 깨지는 순간 전부 다시 짜야 합니다.
여기서 지키는 것은 세 가지입니다.

**격리.** 자격증명은 `os.environ` 에 절대 올리지 않습니다. 환경변수는 프로세스
전역이라, 한 사람의 한투 키를 올리는 순간 같은 프로세스에서 도는 다른 사람의
봇도 그것을 읽습니다. 키는 복호화한 뒤 그 사용자의 엔진에만 인자로 건넵니다.

**암호화.** 저장은 Fernet(AES-128-CBC + HMAC)이고 키는 `QUANT_SECRET_KEY` 에서
파생합니다. 그 값이 없으면 서비스가 시작되지 않습니다 — 남의 증권사 키를
평문으로 받아두는 상태로 뜨느니 뜨지 않는 편이 낫습니다.

**되돌려주지 않음.** 저장된 비밀은 어떤 경로로도 다시 조회되지 않습니다.
API 는 "설정됨/안 됨"과 마지막 4자리만 압니다. 화면이 값을 보여줄 수 없으면
화면이 유출될 수도 없습니다.

비밀번호는 PBKDF2-HMAC-SHA256 600,000회로 늘립니다. 세션 토큰은 해시해서
저장하므로 DB 를 읽어도 살아있는 세션을 얻지 못합니다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from quant.core.types import UTC

log = logging.getLogger("quant.accounts")

#: OWASP 2023 권고치. 로그인 한 번의 비용이지 매 요청 비용이 아닙니다.
_PBKDF2_ROUNDS = 600_000
_SESSION_DAYS = 30
#: 사용자가 고른 값이 그대로 암호화 키가 되지 않도록 한 번 늘려서 씁니다.
_KEY_SALT = b"quant.webapp.credential-key.v1"

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: 자격증명 하나의 최대 길이. 실제로 가장 긴 것이 한투 앱 시크릿(180자 남짓)
#: 이라 어떤 거래소 키도 넉넉히 들어갑니다. 화면이 아니라 저장소가 이 선을
#: 들고 있어야 합니다 — 화면은 앞으로도 늘어날 것이고, 그중 하나가 상한을
#: 빠뜨리면 그 화면만 뚫리는 게 아니라 DB 가 뚫립니다.
_MAX_SECRET_CHARS = 1024
#: 이름은 환경변수 이름이라 훨씬 짧습니다. 값과 같은 행을 이루므로 같이 막습니다.
_MAX_SECRET_NAME_CHARS = 128


def make_db_private(path: str | Path) -> None:
    """SQLite DB 와 그 사이드카를 소유자 전용(0600)으로 만듭니다.

    WAL 을 켜면 `-wal` 과 `-shm` 이 본체와 **같은 행을 담은 채** 옆에 생깁니다.
    본체만 0600 이면 옆의 사본이 umask 대로 0644 라, 잠근 적이 없는 것과
    같습니다.

    사이드카는 SQLite 가 필요할 때마다 지우고 다시 만들기 때문에 한 번
    chmod 하는 것으로는 모자랍니다. 대신 SQLite 는 저널과 WAL 을 만들 때
    본체를 stat 해서 그 권한을 그대로 물려줍니다(unix VFS 의
    `findCreateFileMode`). 그래서 **WAL 을 켜기 전에** 본체를 0600 으로
    맞춰두면 이후 몇 번을 다시 만들어도 0600 으로 태어납니다 — 권한이
    재생성을 견디는 이유가 이것입니다. 이미 0644 로 만들어져 있던 사이드카는
    상속으로 고쳐지지 않으므로 여기서 직접 되돌립니다.
    """
    path = Path(path)
    try:
        # 먼저 만들어 두고 권한을 박습니다. 연결이 파일을 만들게 두면 그
        # 순간의 umask 가 권한을 정하고, 사이드카가 그것을 물려받습니다.
        path.touch(mode=0o600, exist_ok=True)
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - 파일시스템이 허용하지 않는 경우
        log.warning("DB 권한을 600 으로 바꾸지 못했습니다: %s", path)
        return
    for suffix in ("-wal", "-shm"):
        side = Path(f"{path}{suffix}")
        try:
            if side.exists():
                os.chmod(side, 0o600)
        except OSError:  # pragma: no cover - 파일시스템이 허용하지 않는 경우
            log.warning("DB 사이드카 권한을 바꾸지 못했습니다: %s", side)


class AccountError(RuntimeError):
    """호출자에게 그대로 보여줄 수 있는 실패."""


class SecretKeyMissing(RuntimeError):
    """서비스를 시작할 수 없는 상태."""


#: 추천 코드 → 요금제. 코드는 서비스 운영자가 발급하는 것이지 사용자가 고르는
#: 것이 아니므로 여기 둡니다. 값을 늘리려면 줄을 더하면 됩니다.
#:
#: 환경변수로도 더할 수 있습니다: QUANT_REFERRAL_CODES="코드:요금제,코드:요금제"
#: — 코드를 바꾸려고 배포하지 않아도 되게.
REFERRAL_CODES: dict[str, str] = {
    "invest916": "unlimited",
}


def referral_plan(code: str) -> str:
    """이 코드가 주는 요금제. 모르는 코드면 빈 문자열."""
    code = (code or "").strip()
    if not code:
        return ""
    extra = {}
    for pair in os.environ.get("QUANT_REFERRAL_CODES", "").split(","):
        name, _, plan = pair.partition(":")
        if name.strip() and plan.strip():
            extra[name.strip()] = plan.strip()
    table = {**REFERRAL_CODES, **extra}
    # 대소문자는 구분하지 않습니다 — 코드를 손으로 옮겨 적는 사람이 대부분입니다.
    lowered = {k.lower(): v for k, v in table.items()}
    return lowered.get(code.lower(), "")



class _Guarded:
    """잠금 안에서 결과까지 다 읽어 오는 sqlite 연결.

    sqlite3 커서는 게으릅니다 — `execute()` 가 돌아온 시점에는 아직 아무 행도
    읽히지 않았고, `fetchone()` 을 부를 때 비로소 엔진이 한 발짝 나갑니다.
    그래서 쓰기만 잠그고 읽기를 열어 두면, 한 스레드가 INSERT 를 커밋하는 동안
    다른 스레드가 **같은 연결 객체 위에서** SELECT 를 밟습니다. 로그인과 가입은
    비밀번호를 늘이느라 느려서 `run_in_threadpool` 로 나가 있으므로, 이건
    이론적인 경합이 아니라 사람 둘이 동시에 가입하면 바로 일어나는 일입니다.
    파이썬 3.9 의 sqlite3 는 `threadsafety == 1` 이라 이 상황에서 프로세스를
    통째로 죽입니다.

    그래서 여기서는 execute 가 돌아올 때 이미 다 읽혀 있습니다. 호출부는
    그대로 `.fetchone()` / `.fetchall()` 을 쓰지만 그때 남은 것은 리스트뿐이라,
    잠금 밖에서 커서를 밟을 방법 자체가 없습니다. 사이트마다 잠금을 기억해서
    거는 대신, 기억하지 않아도 되게 만드는 쪽을 골랐습니다.
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock

    def execute(self, sql: str, args: tuple = ()) -> _Rows:
        with self._lock:
            cur = self._conn.execute(sql, args)
            return _Rows(cur.fetchall(), cur.lastrowid, cur.rowcount)

    def executescript(self, sql: str) -> None:
        with self._lock:
            self._conn.executescript(sql)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class _Rows:
    """이미 다 읽은 결과. 커서인 척하지만 아무것도 미루지 않습니다."""

    def __init__(self, rows: list, lastrowid: int | None, rowcount: int):
        self._rows = rows
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


@dataclass(frozen=True)
class User:
    id: int
    email: str
    display_name: str
    created_at: datetime
    is_admin: bool = False
    plan: str = "free"
    #: 첫 화면 안내를 본 적이 있는가. 브라우저가 아니라 계정에 답니다 —
    #: 폰에서 보고 노트북에서 또 보면 안내가 아니라 방해입니다.
    tour_seen: bool = False


def _now() -> datetime:
    return datetime.now(UTC)


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, want = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(rounds))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), want)


def _fernet(secret: str) -> Fernet:
    key = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), _KEY_SALT, 200_000)
    return Fernet(base64.urlsafe_b64encode(key))


def password_problem(password: str) -> str:
    """왜 못 쓰는지, 사람이 읽을 수 있게. 통과하면 빈 문자열."""
    if len(password) < 10:
        return "비밀번호는 10자 이상이어야 합니다"
    if password.isdigit() or password.isalpha():
        return "숫자와 문자를 함께 쓰세요"
    return ""


class Accounts:
    """사용자, 세션, 그리고 사용자별 암호화 자격증명."""

    def __init__(self, path: str | Path, secret: str | None = None):
        secret = (secret if secret is not None
                  else os.environ.get("QUANT_SECRET_KEY", "")).strip()
        if not secret:
            raise SecretKeyMissing(
                "QUANT_SECRET_KEY 가 없습니다. 이 값으로 사용자들의 증권사 키를 "
                "암호화하므로, 없으면 서비스를 시작하지 않습니다.\n"
                "  생성: python3 -c \"import secrets;print(secrets.token_urlsafe(48))\"\n"
                "  주의: 이 값을 잃어버리면 저장된 자격증명을 되살릴 수 없습니다."
            )
        if len(secret) < 32:
            raise SecretKeyMissing(
                "QUANT_SECRET_KEY 가 너무 짧습니다 (32자 이상 권장). "
                "이 값 하나가 모든 사용자의 증권사 키를 지킵니다.")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fernet = _fernet(secret)
        self._lock = threading.RLock()
        # 계정 DB 는 비밀번호 해시와 암호문을 담습니다. 같은 머신의 다른
        # 사용자가 읽을 이유가 없습니다. WAL 을 켜기 전에 걸어야 사이드카가
        # 같은 권한으로 태어납니다.
        make_db_private(self.path)
        raw = sqlite3.connect(str(self.path), check_same_thread=False)
        raw.row_factory = sqlite3.Row
        self.conn = _Guarded(raw, self._lock)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        # 권한 상속은 unix VFS 의 동작입니다. 그렇지 않은 파일시스템에서도
        # 방금 생긴 사이드카가 열린 채로 남지 않도록 한 번 더 확인합니다.
        make_db_private(self.path)

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                display_name  TEXT NOT NULL DEFAULT '',
                is_admin      INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL,
                last_login_at TEXT,
                plan          TEXT NOT NULL DEFAULT 'free',
                referral      TEXT NOT NULL DEFAULT '',
                tour_seen     INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
            CREATE TABLE IF NOT EXISTS user_secrets (
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name       TEXT NOT NULL,
                ciphertext BLOB NOT NULL,
                hint       TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, name)
            );
            CREATE TABLE IF NOT EXISTS audit (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                ts      TEXT NOT NULL,
                action  TEXT NOT NULL,
                detail  TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS audit_user ON audit(user_id, ts);
            """
        )
        # 이 서비스보다 먼저 만들어진 DB 에 컬럼을 붙입니다.
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(users)")}
        for column, ddl in (("plan", "TEXT NOT NULL DEFAULT 'free'"),
                            ("referral", "TEXT NOT NULL DEFAULT ''"),
                            ("tour_seen", "INTEGER NOT NULL DEFAULT 0")):
            if column not in have:
                self.conn.execute(f"ALTER TABLE users ADD COLUMN {column} {ddl}")
        self.conn.commit()

    # ── 감사 ─────────────────────────────────────────────────────────────
    def record(self, user_id: int | None, action: str, detail: str = "") -> None:
        """무슨 일이 있었는지만 남깁니다. 값은 절대 남기지 않습니다."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO audit (user_id, ts, action, detail) VALUES (?,?,?,?)",
                (user_id, _now().isoformat(), action, detail))
            self.conn.commit()

    def history(self, user_id: int, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT ts, action, detail FROM audit WHERE user_id=? "
            "ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
        return [dict(r) for r in rows]

    # ── 가입 · 로그인 ────────────────────────────────────────────────────
    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]

    def register(self, email: str, password: str, display_name: str = "") -> User:
        email = (email or "").strip()
        if not _EMAIL.match(email):
            raise AccountError("이메일 형식이 올바르지 않습니다")
        problem = password_problem(password or "")
        if problem:
            raise AccountError(problem)
        # 첫 사용자가 관리자입니다. 아무도 없는 서비스에 관리자를 만들 다른
        # 경로가 없고, 두 번째부터는 자동으로 일반 사용자가 됩니다.
        with self._lock:
            # 세는 것과 넣는 것 사이가 벌어지면, 동시에 가입한 두 사람이 둘 다
            # `count() == 0` 을 보고 둘 다 관리자가 됩니다. 한 잠금 안에 둡니다.
            first = self.count() == 0
            try:
                cur = self.conn.execute(
                    "INSERT INTO users (email, password_hash, display_name, "
                    "is_admin, created_at) VALUES (?,?,?,?,?)",
                    (email, hash_password(password), (display_name or "").strip(),
                     1 if first else 0, _now().isoformat()))
                self.conn.commit()
            except sqlite3.IntegrityError:
                raise AccountError("이미 가입된 이메일입니다") from None
        self.record(cur.lastrowid, "register", email)
        log.info("새 사용자 %s (관리자=%s)", email, first)
        return self.user(cur.lastrowid)

    def user(self, user_id: int) -> User | None:
        row = self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return self._as_user(row)

    def by_email(self, email: str) -> User | None:
        row = self.conn.execute("SELECT * FROM users WHERE email=?",
                                ((email or "").strip(),)).fetchone()
        return self._as_user(row)

    @staticmethod
    def _as_user(row) -> User | None:
        if row is None:
            return None
        keys = row.keys()
        return User(id=row["id"], email=row["email"],
                    display_name=row["display_name"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    is_admin=bool(row["is_admin"]),
                    # 이 컬럼 없이 만들어진 DB 도 열립니다.
                    plan=(row["plan"] if "plan" in keys else None) or "free",
                    tour_seen=bool(row["tour_seen"] if "tour_seen" in keys else 0))

    def authenticate(self, email: str, password: str) -> User | None:
        row = self.conn.execute("SELECT * FROM users WHERE email=?",
                                ((email or "").strip(),)).fetchone()
        if row is None:
            # 없는 계정도 있는 계정과 같은 시간을 쓰게 합니다. 응답 시간만으로
            # 어떤 이메일이 가입돼 있는지 알아낼 수 있으면 안 됩니다.
            hash_password(password or "")
            return None
        if not verify_password(password or "", row["password_hash"]):
            self.record(row["id"], "login_failed")
            return None
        with self._lock:
            self.conn.execute("UPDATE users SET last_login_at=? WHERE id=?",
                              (_now().isoformat(), row["id"]))
            self.conn.commit()
        return self._as_user(row)

    def redeem(self, user_id: int, code: str) -> str:
        """추천 코드를 요금제로 바꿉니다. 성공하면 새 요금제 이름.

        틀린 코드는 조용히 무시하지 않고 그렇게 말합니다 — 코드를 넣었는데
        아무 일도 안 일어나면, 사용자는 넣은 것이 먹혔는지 알 수 없습니다.
        """
        plan = referral_plan(code)
        if not plan:
            raise AccountError("사용할 수 없는 코드입니다")
        with self._lock:
            self.conn.execute("UPDATE users SET plan=?, referral=? WHERE id=?",
                              (plan, (code or "").strip(), user_id))
            self.conn.commit()
        self.record(user_id, "plan_upgraded", f"{code.strip()} → {plan}")
        log.info("사용자 %s 요금제 %s (코드 %s)", user_id, plan, code.strip())
        return plan

    def mark_tour_seen(self, user_id: int) -> None:
        """안내를 끝까지 봤거나 건너뛰었다고 적습니다.

        브라우저 저장소에 두지 않는 이유는 두 가지입니다. 기기를 바꿔도 다시
        뜨면 안 되고, 이 화면의 자바스크립트는 세션을 손에 쥐지 않는다는 규칙을
        지켜야 합니다 — 저장소를 하나 열면 그 규칙을 검사할 방법이 없어집니다.
        """
        with self._lock:
            self.conn.execute("UPDATE users SET tour_seen=1 WHERE id=?", (user_id,))
            self.conn.commit()

    def change_password(self, user_id: int, current: str, new: str) -> None:
        row = self.conn.execute("SELECT password_hash FROM users WHERE id=?",
                                (user_id,)).fetchone()
        if row is None or not verify_password(current or "", row["password_hash"]):
            raise AccountError("현재 비밀번호가 맞지 않습니다")
        problem = password_problem(new or "")
        if problem:
            raise AccountError(problem)
        with self._lock:
            self.conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                              (hash_password(new), user_id))
            self.conn.commit()
        # 비밀번호를 바꾸는 이유의 절반은 누가 봤을지 모른다는 것입니다.
        # 다른 기기에 남아 있던 세션을 살려두면 바꾼 의미가 없습니다.
        self.revoke_all(user_id)
        self.record(user_id, "password_changed")

    # ── 세션 ─────────────────────────────────────────────────────────────
    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = _now()
        with self._lock:
            self.conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
                "VALUES (?,?,?,?)",
                (self._hash_token(token), user_id, now.isoformat(),
                 (now + timedelta(days=_SESSION_DAYS)).isoformat()))
            self.conn.commit()
        self.record(user_id, "login")
        return token

    @staticmethod
    def _hash_token(token: str) -> str:
        # 세션을 평문으로 두면 DB 사본 하나가 곧 모든 사람의 로그인 상태입니다.
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def user_for_session(self, token: str) -> User | None:
        if not token:
            return None
        row = self.conn.execute(
            "SELECT user_id, expires_at FROM sessions WHERE token_hash=?",
            (self._hash_token(token),)).fetchone()
        if row is None:
            return None
        if datetime.fromisoformat(row["expires_at"]) <= _now():
            self.revoke(token)
            return None
        return self.user(row["user_id"])

    def revoke(self, token: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM sessions WHERE token_hash=?",
                              (self._hash_token(token),))
            self.conn.commit()

    def revoke_all(self, user_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self.conn.commit()

    def purge_expired(self) -> int:
        with self._lock:
            cur = self.conn.execute("DELETE FROM sessions WHERE expires_at<=?",
                                    (_now().isoformat(),))
            self.conn.commit()
        return cur.rowcount

    # ── 자격증명 ─────────────────────────────────────────────────────────
    def put_secret(self, user_id: int, name: str, value: str) -> None:
        """저장하고, 다시는 돌려주지 않습니다."""
        name = (name or "").strip()
        value = (value or "").strip()
        if not value:
            return
        if not name or len(name) > _MAX_SECRET_NAME_CHARS:
            raise AccountError("자격증명 이름이 비었거나 너무 깁니다")
        if len(value) > _MAX_SECRET_CHARS:
            # 길이를 먼저 봅니다. 뒤따르는 검사와 암호화·저장이 모두 값 길이에
            # 비례하니, 상한을 넘긴 값은 아무 일도 시키기 전에 돌려보냅니다.
            raise AccountError(
                f"값이 너무 깁니다 (최대 {_MAX_SECRET_CHARS}자). 증권사·거래소 키는 "
                f"이보다 훨씬 짧습니다 — 잘못 붙여넣은 것이 아닌지 확인해 주세요")
        if "\n" in value or "\r" in value:
            raise AccountError("값에 줄바꿈이 있어 저장할 수 없습니다")
        hint = value[-4:] if len(value) > 8 else ""
        with self._lock:
            self.conn.execute(
                "INSERT INTO user_secrets (user_id, name, ciphertext, hint, updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(user_id, name) DO UPDATE SET "
                "ciphertext=excluded.ciphertext, hint=excluded.hint, "
                "updated_at=excluded.updated_at",
                (user_id, name, self._fernet.encrypt(value.encode("utf-8")),
                 hint, _now().isoformat()))
            self.conn.commit()
        self.record(user_id, "secret_saved", name)

    def drop_secret(self, user_id: int, name: str) -> None:
        # `put_secret` 이 이름을 다듬어 넣으므로 지울 때도 같게 다듬습니다.
        # 한쪽만 다듬으면 "지웠는데 남아 있는" 자격증명이 생깁니다.
        name = (name or "").strip()
        with self._lock:
            self.conn.execute("DELETE FROM user_secrets WHERE user_id=? AND name=?",
                              (user_id, name))
            self.conn.commit()
        self.record(user_id, "secret_removed", name)

    def secrets_for(self, user_id: int) -> dict[str, str]:
        """복호화된 자격증명 — 이 사용자의 엔진을 만들 때만 부릅니다.

        API 응답에 넣지 마세요. 이 함수의 반환값이 프로세스 밖으로 나가는 유일한
        경로는 브로커 어댑터에 인자로 들어가는 것뿐이어야 합니다.
        """
        out: dict[str, str] = {}
        for row in self.conn.execute(
                "SELECT name, ciphertext FROM user_secrets WHERE user_id=?",
                (user_id,)).fetchall():
            try:
                out[row["name"]] = self._fernet.decrypt(row["ciphertext"]).decode("utf-8")
            except InvalidToken:
                # QUANT_SECRET_KEY 가 바뀌면 여기로 옵니다. 조용히 빈 값으로
                # 넘어가면 봇이 인증 실패를 알 수 없는 이유로 겪게 됩니다.
                log.error("사용자 %s 의 %s 을(를) 복호화할 수 없습니다 — "
                          "QUANT_SECRET_KEY 가 저장 당시와 다릅니다", user_id, row["name"])
        return out

    def configured(self, user_id: int) -> dict[str, str]:
        """이름과 마지막 4자리만. 화면이 쓸 수 있는 전부입니다."""
        return {r["name"]: r["hint"] for r in self.conn.execute(
            "SELECT name, hint FROM user_secrets WHERE user_id=?",
            (user_id,)).fetchall()}

    def has(self, user_id: int, *names: str) -> bool:
        if not names:
            return False
        marks = ",".join("?" * len(names))
        row = self.conn.execute(
            f"SELECT COUNT(*) c FROM user_secrets WHERE user_id=? AND name IN ({marks})",
            (user_id, *names)).fetchone()
        return row["c"] == len(names)

    def close(self) -> None:
        self.conn.close()
