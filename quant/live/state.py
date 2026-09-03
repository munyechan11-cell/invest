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
import math
import os
import socket
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
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


class RecoveryArchiveError(RuntimeError):
    """A quarantined run could not be archived without weakening safety."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


RECOVERY_CONFIRMATION_PHRASES = {
    "open_orders": "토스 앱 미체결 없음 확인",
    "today_fills": "토스 앱 당일 체결 대조 완료",
    "holdings": "토스 앱 보유 수량 대조 완료",
    "cash": "토스 앱 현금 대조 완료",
    "daily_loss": "토스 앱 당일 손실 대조 완료",
}
RECOVERY_ACKNOWLEDGEMENT_PHRASE = "기존 실행을 보존하고 새 실행으로 시작합니다"
RECOVERY_INSTRUCTIONS = (
    "이전 실거래 실행이 안전 종료를 완료하지 못했습니다. 토스 앱에서 미체결, "
    "당일 체결, 보유 수량, 현금, 당일 손실을 대조한 뒤 기존 실행을 보존하고 "
    "새 실행으로 시작하세요. 주문 이력은 자동 복원되지 않습니다."
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy TEXT NOT NULL,
  mode TEXT NOT NULL,
  -- 어느 에이전트의 실행인가. 한 계좌를 여럿이 나눠 쓸 때만 채워집니다.
  --
  -- 포지션·잠금·핀·하루 원장이 전부 `run_id` 로 묶여 있으므로, 에이전트마다
  -- runs 행을 하나씩 두면 그 테이블들이 전부 저절로 갈립니다. 새 컬럼은 이
  -- 하나뿐입니다 — 나머지 테이블의 PK 는 건드리지 않습니다(기존 DB 에서
  -- `CREATE TABLE IF NOT EXISTS` 는 PK 변경을 조용히 무시하고, 스키마 버전
  -- 장치가 없어 되돌릴 방법도 없습니다).
  --
  -- 빈 문자열은 "에이전트 개념이 없던 실행" 입니다. 1인 1봇 시절의 기존 행과,
  -- 그룹을 쓰지 않는 지금의 단일 실행이 모두 여기에 들어옵니다.
  agent_id TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  stopped_at TEXT,
  requires_reconciliation INTEGER NOT NULL DEFAULT 0,
  archived_at TEXT,
  archive_reason TEXT,
  archived_by TEXT,
  starting_cash REAL NOT NULL,
  config_json TEXT
);
CREATE TABLE IF NOT EXISTS run_recovery_audit (
  run_id INTEGER PRIMARY KEY,
  archived_at TEXT NOT NULL,
  archived_by TEXT NOT NULL,
  reason TEXT NOT NULL,
  confirmations_json TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES runs(id)
);
CREATE TRIGGER IF NOT EXISTS run_recovery_audit_no_update
BEFORE UPDATE ON run_recovery_audit
BEGIN
  SELECT RAISE(ABORT, 'run recovery audit is immutable');
END;
CREATE TRIGGER IF NOT EXISTS run_recovery_audit_no_delete
BEFORE DELETE ON run_recovery_audit
BEGIN
  SELECT RAISE(ABORT, 'run recovery audit is immutable');
END;
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
  capital_source TEXT NOT NULL DEFAULT 'configured',
  performance_baseline REAL NOT NULL DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS account_budget (
  -- 계좌 단위 하루 한도. `day_budget` 과 **따로** 삽니다.
  --
  -- 같은 테이블에 넣을 자리가 없습니다. `day_budget` 의 PK 는
  -- `(run_id, day)` 인데 계좌 한도에는 run_id 가 없습니다. 아무 에이전트의
  -- run_id 를 빌리면 그 에이전트의 원장과 한 행에서 충돌하고, 가짜 run 행을
  -- 만들면 그것이 `day_budget JOIN runs WHERE mode='live'` 스캔에 걸려
  -- Toss 계좌 게이트가 모든 시작을 영구히 거절합니다.
  --
  -- 상태 DB 는 사용자 하나당 하나이고 그 안의 계좌도 하나이므로, 날짜만으로
  -- 유일합니다.
  -- `mode` 가 PK 에 있는 이유: 모의 그룹의 가상 주문이 실거래 계좌의 하루
  -- 허용치를 먹으면 안 됩니다. 반대 방향은 더 나쁩니다 — 아침에 모의로
  -- 시험하다 20 건을 쓰고 실거래로 바꾸면 계좌가 이미 멈춰 있습니다.
  -- `day_budget` 은 `run_id` 를 통해 `runs.mode` 로 갈리지만 계좌 원장에는
  -- run 이 없으므로 여기서 직접 가릅니다.
  day TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'live',
  notional REAL NOT NULL DEFAULT 0, orders INTEGER NOT NULL DEFAULT 0,
  realized_pnl REAL NOT NULL DEFAULT 0, fees REAL NOT NULL DEFAULT 0,
  starting_equity REAL NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0,
  halt_reason TEXT, tz_offset_hours REAL NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (day, mode)
);
CREATE TABLE IF NOT EXISTS sleeve_position (
  -- "005930 20주 중 누구의 10주인가" 를 재시작 너머로 기억합니다.
  --
  -- 이것이 없으면 재시작이 **모든 보유를 팔 수 없게** 만듭니다. 게이트웨이의
  -- 슬리브 원장이 빈 채로 뜨면 `adopt_unassigned` 가 계좌 전부를 미귀속으로
  -- 받아 적고(미귀속은 아무도 팔 수 없습니다), 에이전트 장부는 positions 에서
  -- 정상 복원되므로 화면에는 포지션이 그대로 보입니다. 그 상태에서 손절이
  -- 나가면 `min(장부, 원장)` 이 0 을 골라 거절합니다 — 그리고 합계 불변식은
  -- Σ슬리브(0) + 미귀속(20) == 증권사(20) 이라 아무 경고도 하지 않습니다.
  --
  -- 에이전트 장부(`positions`)와 별개로 둡니다. 둘은 서로를 검산하고, 어긋나면
  -- `SleeveBrokerage._sleeve_quantity` 가 작은 쪽을 골라 안전한 방향으로
  -- 틀립니다.
  agent_id TEXT NOT NULL, symbol_key TEXT NOT NULL,
  quantity TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (agent_id, symbol_key)
);
CREATE TABLE IF NOT EXISTS order_agent (
  -- 주문 하나가 어느 에이전트의 것인가.
  --
  -- 게이트웨이가 메모리에만 들고 있으면, 미체결 주문을 남긴 채 재시작했을 때
  -- 그 주문의 체결이 **미귀속** 으로 떨어집니다. 미귀속 물량은 어느 에이전트도
  -- 팔 수 없고 합계 불변식은 그것을 정상으로 읽으므로, 판 적도 없는 주식이
  -- 영원히 계좌에 남습니다 — 손절도 청산도 닿지 않는 채로.
  --
  -- **어디까지 실제로 되살리는지는 어댑터가 정합니다.** 체결은 어댑터가
  -- 자기 `_orders` 를 훑어 만들고, 그 표는 재시작하면 비어 있습니다. 즉
  -- 토스 실거래에서 재시작 전 주문의 체결이 이 표를 거쳐 돌아오는 일은
  -- 없습니다 — 그쪽은 더 강하게 막습니다. 소유권을 확인할 수 없는 미체결
  -- 주문이 계좌에 있으면 `TossBrokerage.connect()` 가 아예 연결을 거부하므로,
  -- 애초에 그 상태로 그룹이 시작되지 않습니다.
  --
  -- 그래서 이 표가 실제로 일하는 자리는 좁습니다: 같은 프로세스 안의 재연결,
  -- 모의·페이퍼 경로, 그리고 주문 표를 되살리는 어댑터가 생길 때. 좁다고
  -- 빼지는 않습니다 — 귀속을 잃는 쪽의 대가가 "영원히 팔 수 없는 물량" 이고,
  -- 이 표의 비용은 주문당 한 줄입니다.
  order_id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  symbol_key TEXT,
  created_at TEXT NOT NULL
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
        have_runs = {
            r["name"] for r in self.conn.execute("PRAGMA table_info(runs)")
        }
        if "requires_reconciliation" not in have_runs:
            self.conn.execute(
                "ALTER TABLE runs ADD COLUMN requires_reconciliation INTEGER "
                "NOT NULL DEFAULT 0"
            )
            # Before this explicit marker existed, an absent stopped_at was the
            # only durable evidence that a process had not completed its safety
            # checks. Preserve that uncertainty during migration. A false
            # positive requires manual reconciliation; a false negative can
            # transmit a second real order after an unseen fill.
            self.conn.execute(
                "UPDATE runs SET requires_reconciliation = "
                "CASE WHEN stopped_at IS NULL THEN 1 ELSE 0 END"
            )
        for column in ("archived_at", "archive_reason", "archived_by"):
            if column not in have_runs:
                self.conn.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")
        # `account_budget` 의 PK 가 `(day)` 에서 `(day, mode)` 로 바뀌었습니다.
        # `CREATE TABLE IF NOT EXISTS` 는 기존 테이블의 PK 를 바꾸지 않으므로,
        # 옛 모양이면 새로 만듭니다. 이 표는 배포된 적이 없어 지울 자료도
        # 사실상 없습니다 — 있어도 오늘 하루치이고, 없으면 그날의 계좌 허용치가
        # 한 번 새로 시작할 뿐입니다.
        have_acct = {
            r["name"] for r in self.conn.execute(
                "PRAGMA table_info(account_budget)")
        }
        if have_acct and "mode" not in have_acct:
            self.conn.execute("DROP TABLE account_budget")
            self.conn.executescript(SCHEMA)
        if "agent_id" not in have_runs:
            # 기존 행은 전부 빈 문자열이 됩니다 — 에이전트 개념이 없던 실행이고,
            # 그룹을 쓰지 않는 단일 실행도 같은 값을 씁니다. 그래서 이 마이그레이션
            # 뒤에도 기존 사용자의 재개 경로는 정확히 같은 행을 찾습니다.
            self.conn.execute(
                "ALTER TABLE runs ADD COLUMN agent_id TEXT NOT NULL DEFAULT ''")
        # Older live runs stored only the configured cash and high-water mark.
        # Defaulting them to ``configured`` is deliberate: the first successful
        # account-authoritative sync then establishes a fresh venue baseline,
        # instead of reviving a legacy 800k config as if it were today's balance.
        have_state = {
            r["name"] for r in self.conn.execute("PRAGMA table_info(run_state)")
        }
        if "capital_source" not in have_state:
            self.conn.execute(
                "ALTER TABLE run_state ADD COLUMN capital_source TEXT NOT NULL "
                "DEFAULT 'configured'"
            )
        if "performance_baseline" not in have_state:
            self.conn.execute(
                "ALTER TABLE run_state ADD COLUMN performance_baseline REAL NOT NULL "
                "DEFAULT 0"
            )
        self.conn.commit()
        self.run_id: int | None = None
        #: 지금 도는 **그룹** 이 이 프로세스에서 연 실행들.
        #:
        #: 실거래 실행은 시작하자마자 `mark_reconciliation_required()` 로 crash
        #: quarantine 을 건다. 계좌 게이트는 그 표시가 붙은 미보관 Toss 실행을
        #: "증권사 상태가 불확실하다" 로 읽고 새 실거래를 막는데, 방금 이 프로세스가
        #: 같은 그룹으로 띄운 형제에게는 그 해석이 틀렸다. 그것을 남으로 보면
        #: **실거래 에이전트가 둘 이상인 그룹은 영원히 시작하지 못한다** — 하나가
        #: 뜨는 순간 나머지가 자기 형제에게 막힌다.
        #:
        #: 그룹의 에이전트 시점만 여기 등록한다. 단일 봇은 등록하지 않으므로
        #: 실패한 시작이 남긴 격리가 다음 시도를 막는 기존 동작이 그대로다.
        self._group_run_ids: set[int] = set()
        # Set only by ``restore_positions`` from the durable run_state source.
        # LiveTrader passes this explicit fact to the broker before its first
        # account sync; inferring a restart from a cash number would confuse a
        # genuine first-time account adoption with a stale live ledger.
        self.restored_venue_truth = False
        # Separate from stopped_at because read-only API routes historically use
        # ``resume_run`` to select a run and therefore clear stopped_at. This
        # marker is raised before live connect and only a verified clean shutdown
        # clears it; a crash can never look clean merely because the first account
        # snapshot is still stale.
        self.restored_reconciliation_required = False
        # A successful later SQLite write cannot prove that an earlier budget
        # or execution event made it to disk. Keep that uncertainty sticky for
        # this process so shutdown cannot clear the run's venue quarantine.
        self.accounting_persistence_failed = False
        self._owns = False
        self._lock_fd: int | None = None
        #: `remember_ticker` 의 upsert 를 감싸는 잠금. 이 값이 **없어서**
        #: `remember_ticker` 는 부를 때마다 AttributeError 로 죽었고, 그래서
        #: "한 번 조회한 종목은 다음부터 이름으로도 찾힌다" 가 한 번도 동작한
        #: 적이 없습니다 — 조회 기록 테이블은 늘 비어 있었습니다. 이 경로에만
        #: 잠금이 있는 이유는 이것이 유일한 read-modify-write 이기 때문입니다
        #: (빈 이름으로 알던 이름을 덮지 않으려는 CASE 문).
        self._lock = threading.Lock()
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
                  config_json: str = "", *, now: datetime | None = None,
                  agent_id: str = "") -> int:
        self._claim()
        # Defense in depth for callers outside the web registry. An archived
        # Toss run intentionally starts a new ledger, but never while *any*
        # Toss strategy on this user's account is unresolved or still on the
        # archived KST day. Different templates share the same real account and
        # therefore the same daily-loss allowance.
        target_is_toss_live = bool(
            mode == "live" and self._stored_toss_live_config(config_json, strategy)
        )
        if target_is_toss_live:
            self.assert_toss_account_start_allowed(now=now)
        self.restored_reconciliation_required = False
        self.restored_venue_truth = False
        started_at = self._recovery_now(now).isoformat()
        cur = self.conn.execute(
            "INSERT INTO runs(strategy, mode, agent_id, started_at, "
            "requires_reconciliation, starting_cash, config_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (strategy, mode, str(agent_id or ""), started_at, 0,
             starting_cash, config_json),
        )
        self.conn.commit()
        self.run_id = cur.lastrowid
        return self.run_id

    def prepare_toss_live_run(
            self, strategy: str, starting_cash: float, config_json: str,
            *, resume: bool = True, now: datetime | None = None) -> bool:
        """Claim the account ledger, then choose one exact resume or fresh run.

        The ownership claim deliberately spans warm-up in ``LiveTrader``. A
        second process therefore cannot spend one strategy's allowance after
        this check while the selected strategy continues with an empty budget.
        """
        if self._stored_toss_live_scope(config_json, strategy) is None:
            raise RecoveryArchiveError(
                "daily_budget_target_scope_invalid",
                "시작할 Toss 실거래 전략의 통화와 한도 시간대를 검증할 수 "
                "없어 실행하지 않습니다.",
            )
        self._claim()
        gate = self.assert_toss_account_start_allowed(
            resume_strategy=strategy if resume else None,
            resume_config_json=config_json if resume else None,
            now=now,
            resume_agent_id=self.current_agent_id,
        )
        resumable_run_id = gate["resumable_run_id"] if resume else None
        if resumable_run_id is not None:
            self.resume_run_exact(
                strategy, "live", int(resumable_run_id), config_json,
            )
            return True
        self.start_run(
            strategy, "live", starting_cash, config_json, now=now,
        )
        return False

    def resume_run(self, strategy: str, mode: str,
                   agent_id: str = "") -> int | None:
        """Reopen the most recent run for this strategy, mode and agent.

        Deliberately ignores `stopped_at`. A clean shutdown does not flatten
        the book — the positions are still there when the process comes back —
        so scoping resume to "runs that were never stopped" meant every normal
        restart woke up believing it held nothing and had its full starting
        cash. In live mode that is a second position on top of the first.

        **`agent_id` 가 선택 조건에 들어가는 것이 핵심입니다.** runs 행에 적기만
        하고 여기서 안 보면, 같은 전략 템플릿을 고른 두 에이전트가 `ORDER BY id
        DESC LIMIT 1` 에서 **같은 run_id 로 수렴** 합니다. 그 뒤로 positions 의
        PK 는 `(run_id, symbol_key)` 이므로 나중에 저장한 쪽이 앞사람의 포지션을
        덮어쓰고, day_budget 의 PK 는 `(run_id, day)` 이므로 두 에이전트의 하루
        허용치가 한 행으로 합쳐집니다. 다음 재시작에서는 **둘 다 같은 100주를
        복원** 하고, 그날 오후 두 손절이 함께 나가 100주짜리 보유에 200주 매도가
        떠납니다.
        """
        row = self.conn.execute(
            "SELECT id, requires_reconciliation, archived_at FROM runs "
            "WHERE strategy=? AND mode=? AND agent_id=? ORDER BY id DESC LIMIT 1",
            (strategy, mode, str(agent_id or "")),
        ).fetchone()
        # The latest row is the lifecycle head for this strategy/mode. If it
        # was archived after explicit manual reconciliation, going farther
        # back would resurrect the very stale ledger the operator retired.
        if not row or row["archived_at"] is not None:
            self.run_id = None
            self.restored_reconciliation_required = False
            self.restored_venue_truth = False
            return None
        self.run_id = row["id"]
        self.restored_reconciliation_required = bool(
            row["requires_reconciliation"]
        )
        self.conn.execute("UPDATE runs SET stopped_at=NULL WHERE id=?", (self.run_id,))
        self.conn.commit()
        return self.run_id

    def resume_run_exact(
            self, strategy: str, mode: str, expected_run_id: int,
            expected_config_json: str, agent_id: str = "") -> int:
        """Atomically reopen only the preflighted Toss lifecycle head.

        Generic ``resume_run`` intentionally remains usable by read-only legacy
        callers. Live Toss startup uses this stricter path after taking the DB
        owner claim, so a later KIS/corrupt row or a currency/timezone change
        can never redirect the resume to a different allowance.
        """
        self._claim()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT id, requires_reconciliation, archived_at, config_json "
                "FROM runs WHERE strategy=? AND mode=? AND agent_id=? "
                "ORDER BY id DESC LIMIT 1",
                (strategy, mode, str(agent_id or "")),
            ).fetchone()
            if row is None or int(row["id"]) != int(expected_run_id):
                raise RecoveryArchiveError(
                    "daily_budget_resume_run_changed",
                    "시작 전 확인한 Toss 실행이 최신 실행과 달라져 재개하지 "
                    "않습니다. 상태를 다시 조회하세요.",
                )
            if row["archived_at"] is not None:
                raise RecoveryArchiveError(
                    "daily_budget_resume_run_changed",
                    "시작 전 확인한 Toss 실행이 이미 보관되어 재개하지 "
                    "않습니다. 상태를 다시 조회하세요.",
                )
            if bool(row["requires_reconciliation"]):
                raise RecoveryArchiveError(
                    "reconciliation_required",
                    f"'{strategy}' 실행의 수동 복구가 먼저 필요합니다 "
                    f"(run {expected_run_id}).",
                )
            target_scope = self._stored_toss_live_scope(
                expected_config_json, strategy,
            )
            stored_scope = self._stored_toss_live_scope(
                row["config_json"], strategy,
            )
            if target_scope is None or stored_scope != target_scope:
                raise RecoveryArchiveError(
                    "daily_budget_resume_scope_changed",
                    "시작 전 확인한 Toss 실행의 통화 또는 한도 시간대가 "
                    "달라져 재개하지 않습니다.",
                )
            updated = self.conn.execute(
                "UPDATE runs SET stopped_at=NULL WHERE id=? "
                "AND archived_at IS NULL AND requires_reconciliation=0",
                (expected_run_id,),
            )
            if updated.rowcount != 1:
                raise RecoveryArchiveError(
                    "daily_budget_resume_run_changed",
                    "Toss 실행 상태가 동시에 변경되어 재개하지 않습니다. "
                    "상태를 다시 조회하세요.",
                )
            self.conn.commit()
        except Exception:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise
        self.run_id = int(expected_run_id)
        self.restored_reconciliation_required = False
        self.restored_venue_truth = False
        return self.run_id

    @staticmethod
    def _recovery_now(now: datetime | None = None) -> datetime:
        value = now or datetime.now(UTC)
        if value.tzinfo is None or value.utcoffset() is None:
            raise RecoveryArchiveError(
                "reconciliation_time_invalid",
                "복구 시각에는 UTC 오프셋이 필요합니다.",
            )
        return value.astimezone(UTC)

    @classmethod
    def _next_kst_start(cls, archived_at: str | None) -> datetime | None:
        if archived_at is None:
            return None
        if not isinstance(archived_at, str):
            raise RecoveryArchiveError(
                "reconciliation_archive_time_invalid",
                "보관 시각을 검증할 수 없어 새 실거래를 시작하지 않습니다.",
            )
        try:
            text = archived_at[:-1] + "+00:00" if archived_at.endswith("Z") else archived_at
            archived = datetime.fromisoformat(text)
        except (TypeError, ValueError) as exc:
            raise RecoveryArchiveError(
                "reconciliation_archive_time_invalid",
                "보관 시각을 검증할 수 없어 새 실거래를 시작하지 않습니다.",
            ) from exc
        if archived.tzinfo is None or archived.utcoffset() is None:
            raise RecoveryArchiveError(
                "reconciliation_archive_time_invalid",
                "보관 시각에 UTC 오프셋이 없어 새 실거래를 시작하지 않습니다.",
            )
        archived_kst = archived.astimezone(UTC) + _KST_OFFSET
        next_day = archived_kst.date() + timedelta(days=1)
        return (
            datetime(next_day.year, next_day.month, next_day.day, tzinfo=UTC)
            - _KST_OFFSET
        )

    def recovery_start_gate(self, strategy: str, mode: str,
                            *, now: datetime | None = None) -> dict:
        """Block a fresh ledger until the KST day after an archived head."""
        row = self.conn.execute(
            "SELECT id, archived_at FROM runs WHERE strategy=? AND mode=? "
            "ORDER BY id DESC LIMIT 1", (strategy, mode)
        ).fetchone()
        next_start = self._next_kst_start(
            row["archived_at"] if row is not None else None
        )
        current = self._recovery_now(now)
        return {
            "run_id": int(row["id"]) if row is not None else None,
            "restart_blocked": bool(next_start and current < next_start),
            "next_start_allowed_at": (
                next_start.isoformat() if next_start is not None else None
            ),
        }

    @staticmethod
    def _active_budget_cutoff(
            day_value: object, tz_value: object, updated_value: object,
            current: datetime,
    ) -> datetime | None:
        """Return the UTC reset boundary while one stored daily ledger is live.

        The row's own timezone is authoritative.  That prevents changing the
        next template's timezone from making a still-current Toss allowance
        disappear.  Invalid timing data fails closed instead of granting a new
        live allowance whose boundary we cannot prove.
        """
        try:
            ledger_day = date.fromisoformat(str(day_value))
            offset_hours = float(tz_value)
        except (TypeError, ValueError) as exc:
            raise RecoveryArchiveError(
                "daily_budget_time_invalid",
                "저장된 당일 한도의 거래일을 검증할 수 없어 새 실거래를 "
                "시작하지 않습니다.",
            ) from exc
        if not math.isfinite(offset_hours) or abs(offset_hours) > 24:
            raise RecoveryArchiveError(
                "daily_budget_time_invalid",
                "저장된 당일 한도의 시간대를 검증할 수 없어 새 실거래를 "
                "시작하지 않습니다.",
            )
        if not isinstance(updated_value, str):
            raise RecoveryArchiveError(
                "daily_budget_time_invalid",
                "저장된 당일 한도의 갱신 시각을 검증할 수 없어 새 실거래를 "
                "시작하지 않습니다.",
            )
        try:
            updated_text = (
                updated_value[:-1] + "+00:00"
                if updated_value.endswith("Z") else updated_value
            )
            updated = datetime.fromisoformat(updated_text)
        except ValueError as exc:
            raise RecoveryArchiveError(
                "daily_budget_time_invalid",
                "저장된 당일 한도의 갱신 시각을 검증할 수 없어 새 실거래를 "
                "시작하지 않습니다.",
            ) from exc
        if updated.tzinfo is None or updated.utcoffset() is None:
            raise RecoveryArchiveError(
                "daily_budget_time_invalid",
                "저장된 당일 한도의 갱신 시각에 UTC 오프셋이 없어 새 "
                "실거래를 시작하지 않습니다.",
            )
        updated = updated.astimezone(UTC)
        if updated > current:
            raise RecoveryArchiveError(
                "daily_budget_time_invalid",
                "저장된 당일 한도의 갱신 시각이 현재보다 미래라 새 "
                "실거래를 시작하지 않습니다.",
            )
        offset = timedelta(hours=offset_hours)
        current_source_day = (current + offset).date()
        if ledger_day > current_source_day:
            raise RecoveryArchiveError(
                "daily_budget_time_invalid",
                "저장된 당일 한도의 거래일이 현재 시각보다 미래라 새 "
                "실거래를 시작하지 않습니다.",
            )
        source_day_active = current_source_day == ledger_day
        current_kst_day = (current + _KST_OFFSET).date()
        updated_on_current_kst_day = (
            updated + _KST_OFFSET
        ).date() == current_kst_day
        if not source_day_active and not updated_on_current_kst_day:
            return None
        next_day = ledger_day + timedelta(days=1)
        source_cutoff = (
            datetime(next_day.year, next_day.month, next_day.day, tzinfo=UTC)
            - offset
        )
        next_kst_day = current_kst_day + timedelta(days=1)
        kst_cutoff = (
            datetime(
                next_kst_day.year, next_kst_day.month, next_kst_day.day,
                tzinfo=UTC,
            ) - _KST_OFFSET
        )
        return max(source_cutoff, kst_cutoff)

    @staticmethod
    def _daily_budget_was_used(row: sqlite3.Row) -> bool:
        """Whether opening a fresh ledger would hand back spent allowance."""
        try:
            notional = float(row["notional"])
            orders = float(row["orders"])
            realized_pnl = float(row["realized_pnl"])
            fees = float(row["fees"])
            starting_equity = float(row["starting_equity"])
            blocked = float(row["blocked"])
        except (TypeError, ValueError) as exc:
            raise RecoveryArchiveError(
                "daily_budget_value_invalid",
                "저장된 당일 한도 사용량을 검증할 수 없어 새 실거래를 "
                "시작하지 않습니다.",
            ) from exc
        values = (
            notional, orders, realized_pnl, fees, starting_equity, blocked,
        )
        if (
            not all(math.isfinite(value) for value in values)
            or notional < 0
            or fees < 0
            or starting_equity < 0
            or orders < 0
            or not orders.is_integer()
            or blocked < 0
            or not blocked.is_integer()
            or (row["halt_reason"] is not None
                and not isinstance(row["halt_reason"], str))
        ):
            raise RecoveryArchiveError(
                "daily_budget_value_invalid",
                "저장된 당일 한도 사용량을 검증할 수 없어 새 실거래를 "
                "시작하지 않습니다.",
            )
        return bool(any(value != 0 for value in (
            notional, orders, realized_pnl, fees, blocked,
        ))
                    or str(row["halt_reason"] or "").strip())

    @staticmethod
    def _validated_order_event_time(
            timestamp_value: object, current: datetime,
    ) -> datetime:
        if not isinstance(timestamp_value, str):
            raise RecoveryArchiveError(
                "daily_budget_event_time_invalid",
                "저장된 체결 증거의 시각을 검증할 수 없어 새 실거래를 "
                "시작하지 않습니다.",
            )
        try:
            timestamp_text = (
                timestamp_value[:-1] + "+00:00"
                if timestamp_value.endswith("Z") else timestamp_value
            )
            occurred = datetime.fromisoformat(timestamp_text)
        except ValueError as exc:
            raise RecoveryArchiveError(
                "daily_budget_event_time_invalid",
                "저장된 체결 증거의 시각을 검증할 수 없어 새 실거래를 "
                "시작하지 않습니다.",
            ) from exc
        if occurred.tzinfo is None or occurred.utcoffset() is None:
            raise RecoveryArchiveError(
                "daily_budget_event_time_invalid",
                "저장된 체결 증거의 시각에 UTC 오프셋이 없어 새 실거래를 "
                "시작하지 않습니다.",
            )
        occurred = occurred.astimezone(UTC)
        if occurred > current:
            raise RecoveryArchiveError(
                "daily_budget_event_time_invalid",
                "저장된 체결 증거의 시각이 현재보다 미래라 새 실거래를 "
                "시작하지 않습니다.",
            )
        return occurred

    @classmethod
    def _active_order_event_cutoff(
            cls, timestamp_value: object, offset_hours: float,
            current: datetime,
    ) -> datetime | None:
        """Return the conservative reset boundary for one persisted fill.

        Older releases could persist ``order_filled`` while leaving an empty
        daily ledger after all caps were disabled at runtime. The event proves
        account usage, but its payload cannot reconstruct the accepted order,
        partial-fill notional, or realised loss. It therefore acts only as a
        fail-closed presence signal until both its source day and KST day end.
        """
        occurred = cls._validated_order_event_time(timestamp_value, current)
        offset = timedelta(hours=offset_hours)
        event_source_day = (occurred + offset).date()
        current_source_day = (current + offset).date()
        event_kst_day = (occurred + _KST_OFFSET).date()
        current_kst_day = (current + _KST_OFFSET).date()
        if (event_source_day != current_source_day
                and event_kst_day != current_kst_day):
            return None
        source_next = event_source_day + timedelta(days=1)
        source_cutoff = datetime(
            source_next.year, source_next.month, source_next.day, tzinfo=UTC,
        ) - offset
        kst_next = current_kst_day + timedelta(days=1)
        kst_cutoff = datetime(
            kst_next.year, kst_next.month, kst_next.day, tzinfo=UTC,
        ) - _KST_OFFSET
        return max(source_cutoff, kst_cutoff)

    @classmethod
    def _unknown_order_event_cutoff(
            cls, timestamp_value: object, current: datetime,
    ) -> datetime | None:
        """Bound an unverifiable scope without guessing one timezone.

        From any event instant, every UTC-24..UTC+24 local calendar day ends
        within 24 hours. Until then a corrupt broker/scope cannot prove that a
        Toss allowance expired, so the safe result is a temporary quarantine.
        """
        occurred = cls._validated_order_event_time(timestamp_value, current)
        event_kst_day = (occurred + _KST_OFFSET).date()
        next_kst_day = event_kst_day + timedelta(days=1)
        kst_cutoff = datetime(
            next_kst_day.year, next_kst_day.month, next_kst_day.day,
            tzinfo=UTC,
        ) - _KST_OFFSET
        cutoff = max(occurred + timedelta(days=1), kst_cutoff)
        return cutoff if current < cutoff else None

    def toss_account_start_gate(
            self, *, resume_strategy: str | None = None,
            resume_config_json: str | None = None,
            now: datetime | None = None,
            resume_agent_id: str | None = None,
    ) -> dict:
        """Account-wide gate over each Toss-live strategy's lifecycle head.

        The newest *valid Toss-live* row per strategy/mode is the head. A later
        KIS run reusing the same strategy name must not erase an archived Toss
        run's cooldown, because it is a different brokerage account.

        ``resume_strategy`` permits exactly that strategy's single current-day
        head to reopen.  Every fresh run, a different strategy, or ambiguous
        multiple same-day ledgers stays blocked until all of their own daily
        reset boundaries.  We intentionally do not add KRW and USD counters.
        """
        current = self._recovery_now(now)
        rows = self.conn.execute(
            "SELECT id, strategy, mode, agent_id, requires_reconciliation, "
            "archived_at, config_json FROM runs WHERE mode='live' "
            "ORDER BY id DESC"
        ).fetchall()
        seen: set[tuple[str, str]] = set()
        required: list[sqlite3.Row] = []
        archived_cutoffs: list[datetime] = []
        for row in rows:
            broker_type = self._stored_live_broker_type(
                row["config_json"], row["strategy"],
            )
            if broker_type != "toss":
                if broker_type is None:
                    if (bool(row["requires_reconciliation"])
                            and row["archived_at"] is None):
                        raise RecoveryArchiveError(
                            "reconciliation_stored_config_mismatch",
                            "복구가 필요한 실거래 실행의 증권사를 검증할 수 "
                            "없어 새 Toss 실거래를 시작하지 않습니다.",
                        )
                    cutoff = self._next_kst_start(row["archived_at"])
                    if cutoff is not None:
                        archived_cutoffs.append(cutoff)
                continue
            if (bool(row["requires_reconciliation"])
                    and row["archived_at"] is None
                    and int(row["id"]) not in self._group_run_ids):
                # Every unresolved Toss run quarantines the shared account.
                # ``seen`` is only a lifecycle-head filter for archive
                # cooldowns; applying it here lets a later clean legacy row
                # hide an older unresolved run with real venue uncertainty.
                #
                # `_group_run_ids` 는 방금 이 프로세스가 같은 그룹으로 띄운
                # 형제들이다. 그들의 격리는 시작할 때 누구나 거는 표시이지
                # 증권사 상태가 불확실하다는 증거가 아니다. 남으로 보면 실거래
                # 에이전트가 둘 이상인 그룹은 영원히 시작하지 못한다.
                #
                # 하루 한도 계산(`active_budgets`)에는 이 예외를 적용하지
                # 않는다 — 형제들이 쓴 허용치는 같은 계좌의 것이므로 반드시
                # 합쳐서 보여야 하고, 빼는 순간 방어선이 봇 수만큼 곱해진다.
                required.append(row)
            pair = (row["strategy"], row["mode"])
            if pair in seen:
                continue
            seen.add(pair)
            cutoff = self._next_kst_start(row["archived_at"])
            if cutoff is not None:
                archived_cutoffs.append(cutoff)

        # 재개 대상은 **그 에이전트의** 최신 실행이다. agent_id 를 빼면 같은
        # 전략 템플릿을 쓰는 형제의 실행이 선택되고, 그쪽 허용치를 이어받는다.
        latest_target = next((
            row for row in rows
            if row["strategy"] == resume_strategy and row["mode"] == "live"
            and (resume_agent_id is None
                 or str(row["agent_id"] or "") == str(resume_agent_id))
        ), None)
        target_scope = self._stored_toss_live_scope(
            resume_config_json, resume_strategy,
        )
        stored_scope = (
            self._stored_toss_live_scope(
                latest_target["config_json"], latest_target["strategy"],
            ) if latest_target is not None else None
        )
        resumable_run_id = (
            int(latest_target["id"])
            if latest_target is not None
            and latest_target["archived_at"] is None
            and not bool(latest_target["requires_reconciliation"])
            and target_scope is not None
            and stored_scope == target_scope
            else None
        )
        budget_rows = self.conn.execute(
            "SELECT r.id, r.strategy, r.config_json, d.day, d.notional, "
            "d.orders, d.realized_pnl, d.fees, d.starting_equity, d.blocked, "
            "d.halt_reason, "
            "d.tz_offset_hours, d.updated_at FROM runs r "
            "JOIN day_budget d ON d.run_id=r.id "
            "WHERE r.mode='live' ORDER BY r.id DESC, d.day DESC"
        ).fetchall()
        current_budget_rows: list[tuple[sqlite3.Row, datetime]] = []
        active_budgets: list[tuple[sqlite3.Row, datetime]] = []
        for row in budget_rows:
            broker_type = self._stored_live_broker_type(
                row["config_json"], row["strategy"],
            )
            if broker_type is not None and broker_type != "toss":
                continue
            stored_scope = self._stored_toss_live_scope(
                row["config_json"], row["strategy"],
            )
            if stored_scope is None:
                cutoff = self._active_budget_cutoff(
                    row["day"], row["tz_offset_hours"], row["updated_at"],
                    current,
                )
                if cutoff is not None:
                    raise RecoveryArchiveError(
                        "daily_budget_scope_invalid",
                        "저장된 실거래 당일 한도의 증권사, 통화 또는 시간대를 "
                        "검증할 수 없어 새 Toss 실거래를 시작하지 않습니다.",
                    )
                continue
            trusted_cutoff = self._active_budget_cutoff(
                row["day"], stored_scope[1], row["updated_at"], current,
            )
            try:
                budget_offset = float(row["tz_offset_hours"])
            except (TypeError, ValueError) as exc:
                if trusted_cutoff is not None:
                    raise RecoveryArchiveError(
                        "daily_budget_time_invalid",
                        "저장된 당일 한도의 시간대를 검증할 수 없어 새 "
                        "실거래를 시작하지 않습니다.",
                    ) from exc
                continue
            if not math.isfinite(budget_offset) or abs(budget_offset) > 24:
                if trusted_cutoff is not None:
                    raise RecoveryArchiveError(
                        "daily_budget_time_invalid",
                        "저장된 당일 한도의 시간대를 검증할 수 없어 새 "
                        "실거래를 시작하지 않습니다.",
                    )
                continue
            if budget_offset != stored_scope[1]:
                stored_cutoff = self._active_budget_cutoff(
                    row["day"], budget_offset, row["updated_at"], current,
                )
                if trusted_cutoff is not None or stored_cutoff is not None:
                    raise RecoveryArchiveError(
                        "daily_budget_scope_invalid",
                        "저장된 Toss 실행과 당일 한도의 시간대가 달라 새 "
                        "실거래를 시작하지 않습니다.",
                    )
                continue
            if trusted_cutoff is None:
                continue
            was_used = self._daily_budget_was_used(row)
            stored_config = json.loads(row["config_json"])
            max_loss_pct = abs(float(
                stored_config["limits"]["max_daily_loss_pct"]
            ))
            if (
                was_used
                and max_loss_pct > 0
                and float(row["starting_equity"]) <= 0
            ):
                raise RecoveryArchiveError(
                    "daily_budget_value_invalid",
                    "비율 손실 한도가 있는 당일 원장의 시작 자산을 검증할 "
                    "수 없어 새 실거래를 시작하지 않습니다.",
                )
            current_budget_rows.append((row, trusted_cutoff))
            if was_used:
                active_budgets.append((row, trusted_cutoff))

        if resumable_run_id is not None and target_scope is not None:
            target_config = json.loads(resume_config_json or "")
            target_loss_pct = abs(float(
                target_config["limits"]["max_daily_loss_pct"]
            ))
            if target_loss_pct > 0 and any(
                    int(row["id"]) == resumable_run_id
                    and float(row["starting_equity"]) <= 0
                    for row, _ in active_budgets):
                raise RecoveryArchiveError(
                    "daily_budget_value_invalid",
                    "비율 손실 한도를 적용할 당일 원장의 시작 자산을 검증할 "
                    "수 없어 실거래를 재개하지 않습니다.",
                )

        event_rows = self.conn.execute(
            "SELECT r.id, r.strategy, r.config_json, e.ts, e.type, e.payload "
            "FROM runs r JOIN events e ON e.run_id=r.id "
            "WHERE r.mode='live' AND e.type IN "
            "('order_submitted','order_filled','trade_closed') "
            "ORDER BY e.id DESC"
        ).fetchall()
        active_events: dict[int, tuple[sqlite3.Row, datetime]] = {}
        all_submitted_ids: dict[int, set[str]] = {}
        active_event_days: dict[int, set[str]] = {}
        active_submitted_ids: dict[tuple[int, str], set[str]] = {}
        active_filled_ids: dict[int, set[str]] = {}
        active_trade_runs: set[int] = set()
        active_fill_fees: dict[tuple[int, str], float] = {}
        active_trade_pnl: dict[tuple[int, str], float] = {}
        invalid_event_payload_runs: set[int] = set()
        for row in event_rows:
            broker_type = self._stored_live_broker_type(
                row["config_json"], row["strategy"],
            )
            if broker_type is not None and broker_type != "toss":
                continue
            stored_scope = self._stored_toss_live_scope(
                row["config_json"], row["strategy"],
            )
            event_cutoff = (
                self._active_order_event_cutoff(
                    row["ts"], stored_scope[1], current,
                ) if stored_scope is not None
                else self._unknown_order_event_cutoff(row["ts"], current)
            )
            if stored_scope is None and event_cutoff is not None:
                raise RecoveryArchiveError(
                    "daily_budget_scope_invalid",
                    "저장된 당일 체결 증거의 증권사, 통화 또는 시간대를 "
                    "검증할 수 없어 새 Toss 실거래를 시작하지 않습니다.",
                )
            if stored_scope is None:
                continue
            run_id = int(row["id"])
            occurred = self._validated_order_event_time(row["ts"], current)
            event_day = (
                occurred + timedelta(hours=stored_scope[1])
            ).date().isoformat()
            if event_cutoff is not None:
                previous = active_events.get(run_id)
                if previous is None or event_cutoff > previous[1]:
                    active_events[run_id] = (row, event_cutoff)
                active_event_days.setdefault(run_id, set()).add(event_day)
            try:
                payload = json.loads(row["payload"] or "")
            except (TypeError, json.JSONDecodeError):
                if event_cutoff is not None or row["type"] == "order_submitted":
                    invalid_event_payload_runs.add(run_id)
                continue
            if not isinstance(payload, dict):
                if event_cutoff is not None or row["type"] == "order_submitted":
                    invalid_event_payload_runs.add(run_id)
                continue
            if row["type"] == "order_submitted":
                order_id = payload.get("id")
                if not isinstance(order_id, str) or not order_id.strip():
                    invalid_event_payload_runs.add(run_id)
                    continue
                clean_id = order_id.strip()
                all_submitted_ids.setdefault(run_id, set()).add(clean_id)
                if event_cutoff is not None:
                    active_submitted_ids.setdefault(
                        (run_id, event_day), set(),
                    ).add(clean_id)
            elif event_cutoff is not None and row["type"] == "order_filled":
                order_id = payload.get("order_id")
                if not isinstance(order_id, str) or not order_id.strip():
                    invalid_event_payload_runs.add(run_id)
                    continue
                try:
                    fee = float(payload.get("fee"))
                except (TypeError, ValueError):
                    invalid_event_payload_runs.add(run_id)
                    continue
                if not math.isfinite(fee) or fee < 0:
                    invalid_event_payload_runs.add(run_id)
                    continue
                active_filled_ids.setdefault(run_id, set()).add(order_id.strip())
                key = (run_id, event_day)
                active_fill_fees[key] = active_fill_fees.get(key, 0.0) + fee
            elif event_cutoff is not None and row["type"] == "trade_closed":
                try:
                    pnl = float(payload.get("pnl"))
                except (TypeError, ValueError):
                    invalid_event_payload_runs.add(run_id)
                    continue
                if not math.isfinite(pnl):
                    invalid_event_payload_runs.add(run_id)
                    continue
                active_trade_runs.add(run_id)
                key = (run_id, event_day)
                active_trade_pnl[key] = active_trade_pnl.get(key, 0.0) + pnl

        event_accounted_run_ids: set[int] = set()
        for run_id in active_events:
            linked_ids = all_submitted_ids.get(run_id, set())
            filled_ids = active_filled_ids.get(run_id, set())
            if (
                run_id in invalid_event_payload_runs
                or not filled_ids.issubset(linked_ids)
                or (run_id in active_trade_runs and not filled_ids)
            ):
                continue
            matching_rows = {
                str(row["day"]): row for row, _ in current_budget_rows
                if int(row["id"]) == run_id
            }
            if any(
                    day not in matching_rows
                    for day in active_event_days.get(run_id, set())):
                continue
            submissions_match = all(
                float(matching_rows[day]["orders"]) >= len(order_ids)
                and float(matching_rows[day]["notional"]) > 0
                for (candidate_id, day), order_ids
                in active_submitted_ids.items()
                if candidate_id == run_id
            )
            fees_match = all(
                float(matching_rows[day]["fees"]) + 1e-9 >= fees
                for (candidate_id, day), fees in active_fill_fees.items()
                if candidate_id == run_id
            )
            pnl_is_conservative = all(
                float(matching_rows[day]["realized_pnl"]) <= pnl + 1e-9
                for (candidate_id, day), pnl in active_trade_pnl.items()
                if candidate_id == run_id
            )
            if submissions_match and fees_match and pnl_is_conservative:
                event_accounted_run_ids.add(run_id)
        ambiguous_events = [
            item for run_id, item in active_events.items()
            if (run_id in invalid_event_payload_runs
                or run_id not in event_accounted_run_ids)
        ]

        # ``restore_budget`` consumes exactly the lexically latest day row.
        # An anomalous DB can contain two still-active days for one run, or a
        # newer inactive/corrupt row in front of the active one. In either
        # case excluding the whole run by id would restore only one row and
        # silently hand back the usage in the other, so exact resume is not
        # safe until an operator repairs the ledger.
        if resumable_run_id is not None:
            resumable_active = [
                row for row, _ in active_budgets
                if int(row["id"]) == resumable_run_id
            ]
            latest_budget = next((
                row for row in budget_rows
                if int(row["id"]) == resumable_run_id
            ), None)
            active_matches_restore = bool(
                len(resumable_active) == 1
                and latest_budget is not None
                and resumable_active[0]["day"] == latest_budget["day"]
            )
            if resumable_active and not active_matches_restore:
                resumable_run_id = None
            if any(
                    int(row["id"]) == resumable_run_id
                    for row, _ in ambiguous_events):
                # A fill with a missing or incomplete ledger cannot be
                # reconstructed safely from partial-fill events. Do not resume
                # it with a fresh allowance; wait for the account-day boundary.
                resumable_run_id = None

        conflicting_usage = [
            item for item in active_budgets
            if resumable_run_id is None or int(item[0]["id"]) != resumable_run_id
        ]
        conflicting_usage.extend(
            item for item in ambiguous_events
            if resumable_run_id is None or int(item[0]["id"]) != resumable_run_id
        )

        blocking = required[0] if required else None
        budget_blocking = conflicting_usage[0][0] if conflicting_usage else None
        archive_next = max(archived_cutoffs) if archived_cutoffs else None
        budget_next = (
            max(cutoff for _, cutoff in conflicting_usage)
            if conflicting_usage else None
        )
        all_cutoffs = [value for value in (archive_next, budget_next)
                       if value is not None]
        next_start = max(all_cutoffs) if all_cutoffs else None
        archive_blocked = bool(archive_next and current < archive_next)
        daily_budget_blocked = bool(conflicting_usage)
        return {
            "reconciliation_required": blocking is not None,
            "blocking_run_id": int(blocking["id"]) if blocking is not None else None,
            "blocking_strategy": (
                blocking["strategy"] if blocking is not None else None
            ),
            "archive_cooldown_blocked": archive_blocked,
            "daily_budget_blocked": daily_budget_blocked,
            "budget_blocking_run_id": (
                int(budget_blocking["id"]) if budget_blocking is not None else None
            ),
            "budget_blocking_strategy": (
                budget_blocking["strategy"] if budget_blocking is not None else None
            ),
            "resumable_run_id": resumable_run_id,
            "restart_blocked": bool(archive_blocked or daily_budget_blocked),
            "next_start_allowed_at": (
                next_start.isoformat() if next_start is not None else None
            ),
        }

    def assert_toss_account_start_allowed(
            self, *, resume_strategy: str | None = None,
            resume_config_json: str | None = None,
            now: datetime | None = None,
            resume_agent_id: str | None = None) -> dict:
        """Fail closed before a Toss-live run is resumed or created."""
        gate = self.toss_account_start_gate(
            resume_strategy=resume_strategy,
            resume_config_json=resume_config_json,
            now=now,
            resume_agent_id=resume_agent_id,
        )
        if gate["reconciliation_required"]:
            raise RecoveryArchiveError(
                "reconciliation_required",
                f"'{gate['blocking_strategy']}' 실행의 수동 복구가 먼저 필요합니다 "
                f"(run {gate['blocking_run_id']}).",
            )
        if gate["archive_cooldown_blocked"]:
            raise RecoveryArchiveError(
                "reconciliation_start_blocked_until_next_kst_day",
                "당일 한도가 초기화되지 않도록 보관한 날에는 새 실거래를 "
                f"시작할 수 없습니다. {gate['next_start_allowed_at']} 이후 다시 "
                "시작하세요.",
            )
        if gate["daily_budget_blocked"]:
            raise RecoveryArchiveError(
                "daily_budget_strategy_switch_blocked",
                "같은 Toss 계좌의 당일 한도를 유지하기 위해 "
                f"'{gate['budget_blocking_strategy']}' 실행(run "
                f"{gate['budget_blocking_run_id']})의 거래일이 끝날 때까지 새 "
                "실거래 원장을 열 수 없습니다. "
                f"{gate['next_start_allowed_at']} 이후 다시 시작하세요.",
            )
        return gate

    def reconciliation_run(self, strategy: str, mode: str,
                           *, now: datetime | None = None) -> dict | None:
        """Return the lifecycle head for exactly one strategy/mode pair."""
        row = self.conn.execute(
            "SELECT id, strategy, mode, started_at, stopped_at, "
            "requires_reconciliation, archived_at, archive_reason, archived_by, "
            "config_json "
            "FROM runs WHERE strategy=? AND mode=? ORDER BY id DESC LIMIT 1",
            (strategy, mode),
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        config_json = out.pop("config_json")
        out["_stored_toss_live"] = self._stored_toss_live_config(
            config_json, strategy
        )
        out["requires_reconciliation"] = bool(out["requires_reconciliation"])
        out["required"] = bool(
            out["mode"] == "live"
            and out["requires_reconciliation"]
            and out["archived_at"] is None
        )
        next_start = self._next_kst_start(out["archived_at"])
        out["restart_blocked"] = bool(
            next_start and self._recovery_now(now) < next_start
        )
        out["next_start_allowed_at"] = (
            next_start.isoformat() if next_start is not None else None
        )
        return out

    @staticmethod
    def _validate_recovery_proof(
            reason: str, confirmations: dict[str, str],
            acknowledgement: str) -> tuple[str, str]:
        clean_reason = (reason or "").strip()
        if not 10 <= len(clean_reason) <= 500:
            raise RecoveryArchiveError(
                "reconciliation_reason_invalid",
                "복구 사유는 앞뒤 공백을 제외하고 10자 이상 500자 이하로 적어 주세요.",
            )
        supplied = {str(k): str(v).strip() for k, v in confirmations.items()}
        if supplied != RECOVERY_CONFIRMATION_PHRASES:
            missing = [
                key for key, phrase in RECOVERY_CONFIRMATION_PHRASES.items()
                if supplied.get(key) != phrase
            ]
            raise RecoveryArchiveError(
                "reconciliation_confirmation_required",
                "토스 앱 대조 확인이 정확하지 않습니다: " + ", ".join(missing),
            )
        clean_ack = (acknowledgement or "").strip()
        if clean_ack != RECOVERY_ACKNOWLEDGEMENT_PHRASE:
            raise RecoveryArchiveError(
                "reconciliation_acknowledgement_required",
                "기존 실행을 보존하고 새 실행으로 시작한다는 문구를 정확히 입력하세요.",
            )
        proof_json = json.dumps(
            {"confirmations": supplied, "acknowledgement": clean_ack},
            ensure_ascii=False, sort_keys=True,
        )
        return clean_reason, proof_json

    @staticmethod
    def _stored_live_broker_type(
            raw: str | None, strategy: str,
    ) -> str | None:
        try:
            config = json.loads(raw or "")
        except (TypeError, json.JSONDecodeError):
            return None
        broker = config.get("broker") if isinstance(config, dict) else None
        if not (
            isinstance(broker, dict)
            and config.get("name") == strategy
            and config.get("mode") == "live"
        ):
            return None
        broker_type = broker.get("type")
        if (
            not isinstance(broker_type, str)
            or broker_type not in {"paper", "ccxt", "kis", "alpaca", "toss"}
        ):
            return None
        return broker_type

    @classmethod
    def _stored_toss_live_config(cls, raw: str | None, strategy: str) -> bool:
        return cls._stored_live_broker_type(raw, strategy) == "toss"

    @staticmethod
    def _stored_toss_live_scope(
            raw: str | None, strategy: str | None,
    ) -> tuple[str, float] | None:
        """Return the durable allowance scope for one validated Toss run."""
        if not isinstance(strategy, str) or not strategy:
            return None
        try:
            config = json.loads(raw or "")
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(config, dict):
            return None
        broker = config.get("broker")
        portfolio = config.get("portfolio")
        limits = config.get("limits")
        if not (
            config.get("name") == strategy
            and config.get("mode") == "live"
            and isinstance(broker, dict)
            and broker.get("type") == "toss"
            and isinstance(portfolio, dict)
            and isinstance(limits, dict)
        ):
            return None
        currency = portfolio.get("base_currency")
        offset_value = limits.get("timezone_offset_hours")
        if not isinstance(currency, str) or not currency.strip():
            return None
        if isinstance(offset_value, bool) or not isinstance(
                offset_value, (int, float)):
            return None
        offset_hours = float(offset_value)
        if not math.isfinite(offset_hours) or abs(offset_hours) > 24:
            return None
        cap_values = [
            limits.get("max_daily_notional"),
            limits.get("max_daily_orders"),
            limits.get("max_daily_loss"),
            limits.get("max_daily_loss_pct"),
        ]
        if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in cap_values):
            return None
        if not any(float(value) != 0 for value in cap_values):
            return None
        return currency.strip().upper(), offset_hours

    def archive_reconciliation_run(
            self, *, run_id: int, strategy: str, mode: str, operator: str,
            reason: str, confirmations: dict[str, str],
            acknowledgement: str, now: datetime | None = None) -> dict:
        """Archive one exact quarantined Toss-live lifecycle head atomically.

        No trading object is built here. The evidence is a human comparison in
        Toss, not a reconstructed fill, so the existing ledger remains intact
        and the next start creates a separate run.
        """
        clean_reason, proof_json = self._validate_recovery_proof(
            reason, confirmations, acknowledgement
        )
        clean_operator = (operator or "").strip()
        if not clean_operator:
            raise RecoveryArchiveError(
                "reconciliation_operator_required",
                "복구를 수행한 사용자를 확인할 수 없습니다.",
            )
        if mode != "live":
            raise RecoveryArchiveError(
                "reconciliation_not_required",
                "실거래 격리 실행만 보관할 수 있습니다.",
            )

        self._claim()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT id, strategy, mode, requires_reconciliation, archived_at, "
                "archive_reason, archived_by, config_json FROM runs "
                "WHERE strategy=? AND mode=? ORDER BY id DESC LIMIT 1",
                (strategy, mode),
            ).fetchone()
            if row is None or int(row["id"]) != int(run_id):
                raise RecoveryArchiveError(
                    "reconciliation_run_changed",
                    "복구 대상으로 확인한 실행이 최신 실행과 다릅니다. 상태를 다시 조회하세요.",
                )
            if not self._stored_toss_live_config(row["config_json"], strategy):
                raise RecoveryArchiveError(
                    "reconciliation_stored_config_mismatch",
                    "저장된 실행이 Toss 실거래였음을 검증할 수 없어 보관하지 않았습니다.",
                )

            audit = self.conn.execute(
                "SELECT archived_at, archived_by, reason, confirmations_json "
                "FROM run_recovery_audit WHERE run_id=?", (run_id,)
            ).fetchone()
            if row["archived_at"] is not None:
                if (
                    audit is not None
                    and audit["archived_at"] == row["archived_at"]
                    and audit["archived_by"] == clean_operator
                    and audit["reason"] == clean_reason
                    and audit["confirmations_json"] == proof_json
                ):
                    self.conn.commit()
                    return {
                        "archived": True,
                        "idempotent": True,
                        "run_id": int(row["id"]),
                        "strategy": row["strategy"],
                        "mode": row["mode"],
                        "archived_at": row["archived_at"],
                        "next_start_allowed_at": self._next_kst_start(
                            row["archived_at"]
                        ).isoformat(),
                    }
                raise RecoveryArchiveError(
                    "reconciliation_archive_conflict",
                    "이 실행은 이미 다른 복구 확인으로 보관되었습니다.",
                )
            if not bool(row["requires_reconciliation"]):
                raise RecoveryArchiveError(
                    "reconciliation_not_required",
                    "안전 종료된 실행은 복구 보관 대상이 아닙니다.",
                )

            archived_at = self._recovery_now(now).isoformat()
            next_start_allowed_at = self._next_kst_start(archived_at).isoformat()
            updated = self.conn.execute(
                "UPDATE runs SET archived_at=?, archive_reason=?, archived_by=? "
                "WHERE id=? AND archived_at IS NULL AND requires_reconciliation=1",
                (archived_at, clean_reason, clean_operator, run_id),
            )
            if updated.rowcount != 1:
                raise RecoveryArchiveError(
                    "reconciliation_run_changed",
                    "복구 실행 상태가 동시에 변경되었습니다. 상태를 다시 조회하세요.",
                )
            self.conn.execute(
                "INSERT INTO run_recovery_audit(run_id, archived_at, archived_by, "
                "reason, confirmations_json) VALUES(?,?,?,?,?)",
                (run_id, archived_at, clean_operator, clean_reason, proof_json),
            )
            self.conn.execute(
                "INSERT INTO events(run_id, ts, type, payload) VALUES(?,?,?,?)",
                (run_id, archived_at, "reconciliation_archived", json.dumps({
                    "archived_at": archived_at,
                    "archived_by": clean_operator,
                    "reason": clean_reason,
                    "confirmations": dict(RECOVERY_CONFIRMATION_PHRASES),
                    "acknowledgement": RECOVERY_ACKNOWLEDGEMENT_PHRASE,
                }, ensure_ascii=False, sort_keys=True)),
            )
            self.conn.commit()
            return {
                "archived": True,
                "idempotent": False,
                "run_id": int(row["id"]),
                "strategy": row["strategy"],
                "mode": row["mode"],
                "archived_at": archived_at,
                "next_start_allowed_at": next_start_allowed_at,
            }
        except Exception:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise

    def mark_reconciliation_required(self) -> None:
        """Persist the crash quarantine before live brokerage activity starts."""
        if self.run_id is None:
            raise RuntimeError("실행 기록이 없어 계좌 재조정 플래그를 저장할 수 없습니다")
        self._claim()
        self.conn.execute(
            "UPDATE runs SET requires_reconciliation=1 WHERE id=?",
            (self.run_id,),
        )
        self.conn.commit()

    def stop_run(self) -> None:
        if self.run_id is None:
            return
        self.conn.execute(
            "UPDATE runs SET stopped_at=?, requires_reconciliation=0 WHERE id=?",
            (datetime.now(UTC).isoformat(), self.run_id),
        )
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
            "high_water_mark, total_fees, capital_source, performance_baseline, "
            "updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (self.run_id, portfolio.cash,
             sum(p.realized_pnl for p in portfolio.positions.values()),
             portfolio.high_water_mark, portfolio.total_fees,
             portfolio.capital_source, portfolio.performance_baseline,
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

    # ── 계좌 단위 하루 한도 ──────────────────────────────────────────────
    def save_account_budget(self, budget: TradingBudget,
                            mode: str = "live") -> None:
        """계좌 원장을 적는다. 실행(run)이 아니라 **계좌** 의 것입니다.

        `save_budget` 과 달리 `run_id` 를 보지 않습니다 — 그룹의 마스터 한도는
        어느 에이전트의 실행에도 속하지 않고, 에이전트가 전부 바뀌어도 같은
        계좌의 같은 하루 허용치입니다.
        """
        state = budget.to_state()
        if not state:
            return
        self._claim()
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO account_budget(day, mode, notional, "
                "orders, realized_pnl, fees, starting_equity, blocked, "
                "halt_reason, tz_offset_hours, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (state["day"], str(mode), state["notional"], state["orders"],
                 state["realized_pnl"], state["fees"], state["starting_equity"],
                 state["blocked"], state["halt_reason"],
                 state["tz_offset_hours"], datetime.now(UTC).isoformat()),
            )
            self.conn.commit()
        except Exception:
            # 연결은 공유됩니다. 열린 트랜잭션을 남기면 다음에 쓰는 사람이 —
            # 다른 에이전트가 — 우리의 반쯤 적용된 상태를 커밋합니다.
            with contextlib.suppress(Exception):
                self.conn.rollback()
            raise

    def restore_account_budget(self, budget: TradingBudget,
                               now: datetime | None = None,
                               mode: str = "live") -> bool:
        """계좌 원장을 되살리고, 그 뒤로도 계속 적게 한다.

        이것이 없으면 **재시작이 계좌에 새 허용치를 줍니다.** 하루 손실 한도가
        걸려 "다음 거래일까지 중단" 이 된 계좌를 재배포 한 번이 풀어 주는데,
        그건 한도가 아니라 한도와 초기화 버튼을 함께 둔 것입니다 — 그리고 그
        버튼은 봇이 고장 났을 때 가장 자주 눌립니다.
        """
        self._claim()
        row = self.conn.execute(
            "SELECT * FROM account_budget WHERE mode=? ORDER BY day DESC LIMIT 1",
            (str(mode),),
        ).fetchone()
        if row is not None:
            # **저장된 시간대를 따릅니다.** `load_state` 는 시간대가 다르면
            # 복원을 취소하는데, 계좌 원장에서 그 취소는 곧 "하루 손실 한도로
            # 멈춘 계좌가 다시 열린다" 입니다. 그리고 여기서 시간대는 그룹의
            # 첫 번째 에이전트 설정에서 왔을 뿐이라, 에이전트 순서를 바꾸거나
            # 설정 하나를 손보는 것만으로 달라집니다 — 그 정도의 일이 계좌
            # 방어선을 지워서는 안 됩니다. 계좌의 "오늘" 은 계좌의 성질입니다.
            stored_tz = float(row["tz_offset_hours"] or 0.0)
            if abs(stored_tz - budget.tz_offset.total_seconds() / 3600) > 1e-9:
                log.warning(
                    "계좌 원장의 시간대(UTC%+g)를 따릅니다 — 설정은 UTC%+g "
                    "입니다. 하루 경계를 바꾸려면 원장이 비는 다음 거래일에 "
                    "하세요.", stored_tz,
                    budget.tz_offset.total_seconds() / 3600)
                budget.tz_offset = timedelta(hours=stored_tz)
        restored = budget.load_state(dict(row) if row is not None else {}, now)
        budget.bind_store(_AccountBudgetStore(self, mode))
        self.save_account_budget(budget, mode)
        return restored

    # ── 슬리브 원장 ──────────────────────────────────────────────────────
    def save_sleeves(self, sleeves: dict[str, dict[str, Decimal]]) -> None:
        """누가 무엇을 얼마나 들고 있는지. 계좌 전체를 통째로 다시 씁니다.

        에이전트는 넷까지, 종목은 전략당 몇 개이므로 행은 늘 수십 개 이하입니다.
        부분 갱신보다 통째 교체가 안전합니다 — 0 이 된 항목이 지워지지 않고
        남으면, 다음 재시작이 판 적 없는 물량을 되살립니다.
        """
        self._claim()
        now = datetime.now(UTC).isoformat()
        rows = [(agent_id, key, str(qty), now)
                for agent_id, book in (sleeves or {}).items()
                for key, qty in book.items() if qty != 0]
        try:
            self.conn.execute("DELETE FROM sleeve_position")
            if rows:
                self.conn.executemany(
                    "INSERT INTO sleeve_position(agent_id, symbol_key, quantity, "
                    "updated_at) VALUES(?,?,?,?)", rows)
            self.conn.commit()
        except Exception:
            # 지우고 다시 넣는 사이에 실패하면 원장이 비어 버립니다. 되돌리지
            # 않으면 다음 재시작이 모든 보유를 미귀속으로 읽고, 그러면 아무도
            # 자기 포지션을 팔 수 없습니다.
            with contextlib.suppress(Exception):
                self.conn.rollback()
            raise

    def restore_sleeves(self) -> dict[str, dict[str, Decimal]]:
        """저장된 슬리브 원장. `agent_id → {symbol_key: 수량}`."""
        self._claim()
        out: dict[str, dict[str, Decimal]] = {}
        for row in self.conn.execute(
                "SELECT agent_id, symbol_key, quantity FROM sleeve_position"):
            try:
                qty = Decimal(row["quantity"])
            except (ArithmeticError, TypeError, ValueError):
                log.warning("슬리브 수량을 읽지 못해 건너뜁니다: %s %s",
                            row["agent_id"], row["symbol_key"])
                continue
            if qty != 0:
                out.setdefault(row["agent_id"], {})[row["symbol_key"]] = qty
        return out

    # ── 주문 → 에이전트 귀속 ─────────────────────────────────────────────
    def save_order_agent(self, order_id: str, agent_id: str,
                         symbol_key: str = "") -> None:
        """이 주문이 누구 것인지 적는다. 체결이 돌아올 때 유일한 근거입니다."""
        if not order_id or not agent_id:
            return
        self._claim()
        self.conn.execute(
            "INSERT OR REPLACE INTO order_agent(order_id, agent_id, symbol_key, "
            "created_at) VALUES(?,?,?,?)",
            (str(order_id), str(agent_id), str(symbol_key or ""),
             datetime.now(UTC).isoformat()),
        )
        self.conn.commit()

    def forget_order_agent(self, order_id: str) -> None:
        self._claim()
        self.conn.execute("DELETE FROM order_agent WHERE order_id=?",
                          (str(order_id),))
        self.conn.commit()

    def restore_order_agents(self, prune_older_than_days: int = 7) -> dict[str, str]:
        """재시작 전에 낸 주문들의 귀속. `order.id → agent_id`.

        오래된 행은 지웁니다. 일주일 전에 낸 주문의 체결이 이제 와서 돌아오는
        일은 없고, 지우지 않으면 이 표만 무한히 자랍니다. 지우는 것이 위험한
        방향도 아닙니다 — 귀속을 잃은 체결은 미귀속으로 가고, 미귀속은 아무도
        팔 수 없으므로 남의 물량을 파는 쪽으로는 절대 틀리지 않습니다.
        """
        self._claim()
        cutoff = (datetime.now(UTC) - timedelta(days=prune_older_than_days))
        self.conn.execute("DELETE FROM order_agent WHERE created_at < ?",
                          (cutoff.isoformat(),))
        self.conn.commit()
        return {r["order_id"]: r["agent_id"]
                for r in self.conn.execute(
                    "SELECT order_id, agent_id FROM order_agent")}

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

    def mark_accounting_persistence_failed(self) -> None:
        """Remember one lost accounting write until this store is discarded."""
        self.accounting_persistence_failed = True

    def record_accounting_event(self, event_type: str, payload: dict) -> None:
        """Persist money-moving evidence and retain any write uncertainty."""
        try:
            self.record_event(event_type, payload)
        except Exception:
            self.mark_accounting_persistence_failed()
            raise

    # ── reads ────────────────────────────────────────────────────────────
    def restore_positions(self, portfolio: Portfolio,
                          symbols: dict[str, Symbol]) -> int:
        """Rebuild the position book from disk. Returns how many were restored.

        The venue is still authoritative — `LiveBrokerage.sync()` runs after
        this and corrects any drift. This exists so the bot starts from
        *approximately* right rather than from zero.
        """
        self._claim()
        self.restored_venue_truth = False
        state = self.conn.execute(
            "SELECT * FROM run_state WHERE run_id=?", (self.run_id,)
        ).fetchone()
        if state is not None:
            portfolio.cash = state["cash"]
            portfolio.total_fees = state["total_fees"]
            portfolio.high_water_mark = max(state["high_water_mark"], 0.0) or portfolio.cash
            portfolio.restore_capital_state(
                state["capital_source"], state["performance_baseline"]
            )
            self.restored_venue_truth = state["capital_source"] == "venue"
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

    @property
    def current_agent_id(self) -> str | None:
        """이 저장소가 대표하는 에이전트. 그룹이 아니면 None 입니다.

        `None` 은 "에이전트를 가리지 않는다" 이고 빈 문자열은 "에이전트 개념이
        없던 실행" 입니다. 둘을 구별해야 기존 단일 봇의 게이트 판정이 그대로
        남습니다.
        """
        return None

    def close(self) -> None:
        self._release_ownership()
        self.conn.close()

    # ── 에이전트별 시점 ──────────────────────────────────────────────────
    def agent_view(self, agent_id: str) -> AgentStateView:
        """한 에이전트가 쓰는 시점. 연결과 소유권은 이 저장소의 것을 씁니다.

        에이전트마다 `StateStore` 를 새로 만들 수는 없습니다. `_claim()` 의
        advisory lock 은 **open file description 단위** 라, 같은 프로세스에서
        두 번째로 여는 순간 `LOCK_EX|LOCK_NB` 가 실패하고 `StateInUseError` 로
        끝납니다 — 자기 자신에게 잠기는 셈입니다.

        그렇다고 하나를 그대로 넷이 나눠 쓸 수도 없습니다. `run_id` 가 인스턴스
        속성이라, 마지막으로 `start_run` 을 부른 에이전트의 값이 남고 넷이 전부
        그 run 에 자기 포지션과 하루 원장을 적습니다. 포지션 PK 가
        `(run_id, symbol_key)` 이므로 나중에 적는 쪽이 앞사람을 덮어씁니다.

        그래서 **연결·소유권·파일락은 공유하고 `run_id` 만 가릅니다.**
        `accounting_persistence_failed` 는 공유합니다 — 회계 기록을 잃은 것은
        DB 하나의 사실이고, 한 에이전트의 실패는 그룹 전체의 종료를 안전하지
        않게 만듭니다.
        """
        return AgentStateView(self, agent_id)


class _AccountBudgetStore:
    """`TradingBudget.bind_store` 가 요구하는 두 가지만 갖춘 시점.

    `TradingBudget` 은 값이 바뀔 때마다 `store.save_budget(self)` 를 부릅니다.
    계좌 예산에 `StateStore` 를 그대로 물리면 그 호출이 `day_budget` 으로 가서
    **어느 에이전트의 실행 원장을 계좌 값으로 덮어씁니다.** 여기서 자리를
    바꿔 줍니다.
    """

    __slots__ = ("store", "mode")

    def __init__(self, store: StateStore, mode: str = "live"):
        self.store = store
        self.mode = mode

    def save_budget(self, budget: TradingBudget) -> None:
        self.store.save_account_budget(budget, self.mode)

    def mark_accounting_persistence_failed(self) -> None:
        # 계좌 원장을 잃은 것도 같은 DB 의 사실입니다 — 그룹 전체의 종료를
        # 정상으로 확정할 수 없습니다.
        self.store.mark_accounting_persistence_failed()


class AgentStateView(StateStore):
    """공유 저장소를 한 에이전트의 눈으로 본 것. 연결을 새로 열지 않습니다.

    `StateStore` 를 상속하되 `__init__` 을 부르지 않습니다 — 부르면 같은 파일에
    두 번째 연결이 열리고 파일락이 자기 자신에게 걸립니다. 대신 공유해야 할
    것만 원본에서 가져오고, 갈라야 할 것만 새로 만듭니다.
    """

    def __init__(self, owner: StateStore, agent_id: str):  # noqa: D107
        # super().__init__ 은 의도적으로 부르지 않습니다 (위 docstring 참조).
        self.owner = owner
        self.agent_id = str(agent_id or "")
        # ── 공유: 계좌에 하나뿐인 것들 ──
        self.path = owner.path
        self.conn = owner.conn
        self._lock = owner._lock
        # ── 갈림: 에이전트의 실행 ──
        self.run_id: int | None = None
        self.restored_reconciliation_required = False
        self.restored_venue_truth = False
        self._scored_saved = 0

    # 소유권은 원본의 것입니다. 시점이 따로 주장하면 자기 자신에게 잠깁니다.
    @property
    def _owns(self) -> bool:
        return self.owner._owns

    @_owns.setter
    def _owns(self, value: bool) -> None:
        self.owner._owns = value

    @property
    def _heartbeat_at(self) -> float:
        return self.owner._heartbeat_at

    @_heartbeat_at.setter
    def _heartbeat_at(self, value: float) -> None:
        self.owner._heartbeat_at = value

    @property
    def _lock_fd(self):
        return getattr(self.owner, "_lock_fd", None)

    @_lock_fd.setter
    def _lock_fd(self, value) -> None:
        self.owner._lock_fd = value

    @property
    def accounting_persistence_failed(self) -> bool:
        """회계 기록을 잃은 것은 DB 하나의 사실입니다.

        한 에이전트의 예산 저장이 실패하면 그 DB 로 도는 그룹 전체의 종료를
        정상으로 확정할 수 없습니다.
        """
        return self.owner.accounting_persistence_failed

    @accounting_persistence_failed.setter
    def accounting_persistence_failed(self, value: bool) -> None:
        self.owner.accounting_persistence_failed = bool(value)

    def close(self) -> None:
        """아무것도 닫지 않습니다 — 연결도 소유권도 이 시점의 것이 아닙니다.

        `LiveTrader` 는 두 종료 경로 모두에서 `state.close()` 를 무조건 부릅니다.
        시점이 그것을 그대로 수행하면, 먼저 끝난 에이전트 하나가 아직 매매 중인
        나머지 셋의 DB 연결과 그룹 전체의 소유권 주장을 닫아 버립니다.
        """
        return None

    @property
    def current_agent_id(self) -> str:
        return self.agent_id

    @property
    def _group_run_ids(self) -> set:
        """형제 목록은 그룹의 것입니다 — 시점마다 따로 두면 서로를 못 봅니다."""
        return self.owner._group_run_ids

    # ── 에이전트 실행의 시작/재개 ────────────────────────────────────────
    def _remember_sibling(self, run_id):
        """이 실행을 "지금 이 그룹이 연 것" 으로 등록한다.

        계좌 게이트가 형제의 시작 격리를 남의 미해결 실행으로 읽지 않게 하는
        유일한 근거입니다. 등록은 **에이전트 시점만** 합니다 — 단일 봇이
        등록하면 실패한 시작이 남긴 격리가 다음 시도를 막지 못하게 됩니다.
        """
        if run_id is not None:
            self.owner._group_run_ids.add(int(run_id))
        return run_id

    def start_run(self, strategy, mode, starting_cash, config_json="", *,
                  now=None, agent_id=None):
        return self._remember_sibling(super().start_run(
            strategy, mode, starting_cash, config_json, now=now,
            agent_id=self.agent_id if agent_id is None else agent_id))

    def resume_run(self, strategy, mode, agent_id=None):
        return self._remember_sibling(super().resume_run(
            strategy, mode,
            self.agent_id if agent_id is None else agent_id))

    def resume_run_exact(self, strategy, mode, expected_run_id,
                         expected_config_json, agent_id=None):
        return self._remember_sibling(super().resume_run_exact(
            strategy, mode, expected_run_id, expected_config_json,
            self.agent_id if agent_id is None else agent_id))
