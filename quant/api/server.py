"""REST + WebSocket control plane and dashboard.

이 API 는 매수·매도·전량청산·자격증명 저장을 수행합니다. 1인용일 때 그것을
지키는 것은 공유 토큰 하나(`QUANT_API_TOKEN`)면 됐습니다 — 서버 주인과 계좌
주인이 같은 사람이었으니까요. 여러 사람이 가입하는 순간 그 전제가 깨지고,
질문이 "들어올 수 있는가"에서 **"누구의 것을 만지는가"**로 바뀝니다.

그래서 이 파일의 모든 엔드포인트는 프로세스 전역 상태가 아니라 `Desk` 하나를
받습니다. 데스크는 요청 하나가 만질 수 있는 전부입니다 — 그 사람의 봇, 그
사람의 상태 파일, 그 사람의 성향과 한도와 자격증명. 데스크를 고르는 일은
`_desk` 한 곳에서만 일어나고, 거기서 세션 쿠키가 사람을 정합니다. 엔드포인트가
`state.trader` 를 직접 만지면 그 한 줄이 곧 남의 봇을 조종하는 길이라,
그런 줄은 이 파일에 하나도 없어야 합니다.

인증은 **세션 쿠키 하나**입니다. 가입한 사람이 자기 데스크에 앉는 길이고,
가입자가 있는 배포에서 그것을 대신할 수 있는 것은 없습니다.

`QUANT_API_TOKEN` 은 한때 그 자리를 대신했습니다 — 이 토큰을 든 요청은
관리자 계정으로 동작했습니다. 여러 사람이 쓰는 서비스에서 그것은 공유
마스터 키입니다. 모두가 통과해야 하는 로그인을 혼자 건너뛰고, URL 에 실려
프록시 접근 로그와 브라우저 히스토리와 `Referer` 로 흘러 나가고, 그 한 줄을
주운 사람은 관리자의 봇과 장부와 자격증명 앞에 앉습니다. **계정이 하나라도
있으면 이 토큰은 아무도 인증하지 않습니다.**

토큰이 아직 무언가를 지키는 곳은 계정이 없는 배포 하나뿐입니다
(`OperatorDesk`) — 거기서는 서버 주인과 계좌 주인이 같은 사람이고 로그인이라는
개념 자체가 없습니다. 그 길은 누군가 가입하는 순간 닫힙니다.

자격증명은 `os.environ` 에 올리지 않습니다. 환경변수는 프로세스 전역이라 한
사람의 한투 키를 올리는 순간 같은 프로세스의 다른 사람 봇이 그것으로 주문을
냅니다. 사용자 자격증명은 `Accounts` 안에서 암호화된 채로 살고, 복호화된 값이
가는 곳은 `UserRegistry` 가 어댑터 생성자를 부르는 자리뿐입니다.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

from quant.alpha.llm_client import LLMError
from quant.config.loader import load_config
from quant.config.schema import StrategyConfig
from quant.core.aio import LazyLock, LazySemaphore
from quant.core.context import QUOTE_FUTURE_TOLERANCE
from quant.core.events import Event
from quant.core.types import UTC, RunMode, Symbol
from quant.data.names import NameBook
from quant.live.agents import MAX_AGENTS
from quant.live.credentials import (
    OPERATOR_FIELDS,
    VENUES,
    VENUES_BY_ID,
    WRITABLE_KEYS,
    load_env_file,
    rejection_reason,
    venue_catalog,
)
from quant.live.profile import ProfileStore, questionnaire, score_answers
from quant.live.state import StateStore
from quant.strategy import glossary
from quant.webapp.accounts import AccountError, Accounts, SecretKeyMissing, User
from quant.webapp.auth_api import build_auth, public_user
from quant.webapp.registry import (
    RuntimeProblem,
    UserRegistry,
    required_secrets,
)
from quant.webapp.registry import (
    _targets as credential_targets,
)

log = logging.getLogger("quant.api")
STATIC_DIR = Path(__file__).parent / "static"


class Hub:
    """Fan-out of engine events to connected WebSocket clients.

    한 사람당 하나입니다. 하나를 공유하면 A 의 체결과 보유 평가가 B 의 화면에
    그대로 흐릅니다 — 조작이 아니라 **관람**이지만, 남의 계좌를 들여다보는
    것은 조작만큼이나 이 서비스가 존재하면 안 되는 이유입니다.
    """

    def __init__(self, ring_size: int = 500):
        self.clients: set[WebSocket] = set()
        self.ring: list[dict] = []
        self.ring_size = ring_size

    async def publish(self, event: Event) -> None:
        payload = {
            "type": event.type.value,
            "ts": event.ts.isoformat(),
            "source": event.source,
            "payload": event.payload,
        }
        self.ring.append(payload)
        if len(self.ring) > self.ring_size:
            del self.ring[: len(self.ring) - self.ring_size]
        if not self.clients:
            return
        text = json.dumps(finite(payload), ensure_ascii=False, default=str)
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:
                self.clients.discard(ws)

    def recent(self, limit: int = 100, types: set[str] | None = None) -> list[dict]:
        items = self.ring if types is None else [e for e in self.ring if e["type"] in types]
        return items[-limit:]


class ReadBusy(RuntimeError):
    """The bounded upstream read queue is full; callers should poll later."""


class ReadCoalescer:
    """Short-lived single-flight cache for browser polling.

    It is deliberately process-local and bounded.  The values are still marked
    ``no-store`` on HTTP responses; this tiny server-side window only makes two
    tabs asking the same question share one upstream request.
    """

    def __init__(self, *, ttl_seconds: float, max_concurrent: int,
                 max_inflight: int, max_entries: int = 512):
        self.ttl_seconds = ttl_seconds
        self.max_inflight = max_inflight
        self.max_entries = max_entries
        self._limit = LazySemaphore(max_concurrent)
        self._lock = LazyLock()
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._inflight: dict[tuple, asyncio.Task] = {}

    async def get(self, key: tuple, loader) -> Any:
        now = time.monotonic()
        async with self._lock:
            # TTL is also the retention boundary for account payloads, not
            # merely a rule for whether they may be served. Opportunistically
            # remove every expired key while the cache is already locked.
            for expired_key, (expires_at, _value) in tuple(self._cache.items()):
                if expires_at <= now:
                    self._cache.pop(expired_key, None)
            cached = self._cache.get(key)
            if cached is not None:
                return cached[1]
            task = self._inflight.get(key)
            if task is None:
                if len(self._inflight) >= self.max_inflight:
                    raise ReadBusy("upstream read queue is full")
                task = asyncio.create_task(self._load(key, loader))
                # If every browser waiter disconnects, ``shield`` deliberately
                # leaves the provider cleanup task running. Consume a possible
                # terminal exception so asyncio does not emit a misleading
                # "Task exception was never retrieved" after cleanup finishes.
                task.add_done_callback(self._consume_background_result)
                self._inflight[key] = task
        # A browser can abort its fetch while another tab is waiting for the
        # same upstream read.  Do not let that one waiter cancel the shared
        # loader; the loader owns and closes its provider in its own finally.
        return await asyncio.shield(task)

    @staticmethod
    def _consume_background_result(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            return

    async def _load(self, key: tuple, loader) -> Any:
        current = asyncio.current_task()
        try:
            async with self._limit:
                value = await loader()
            async with self._lock:
                if len(self._cache) >= self.max_entries:
                    oldest = min(self._cache, key=lambda item: self._cache[item][0])
                    self._cache.pop(oldest, None)
                self._cache[key] = (time.monotonic() + self.ttl_seconds, value)
            return value
        finally:
            async with self._lock:
                if self._inflight.get(key) is current:
                    self._inflight.pop(key, None)


class AppState:
    def __init__(self, config: StrategyConfig | None, state_path: str):
        self.config = config
        self.state_path = state_path
        #: 1인용 배포의 이벤트 링. 여러 사람일 때는 `hubs` 만 씁니다.
        self.hub = Hub()
        self.hubs: dict[int, Hub] = {}
        self.trader: Any = None
        self.trader_task: asyncio.Task | None = None
        self.backtests: dict[str, dict] = {}
        #: 지금 백테스트를 돌리고 있는 사람들. 한 사람당 하나까지입니다.
        self.backtests_running: set[str] = set()
        self.started_at = datetime.now(UTC)
        # MARKET_DATA is 15 TPS per Toss client.  Three snapshots at once leave
        # room for the one extra price request used when an unknown ticker name
        # is first resolved, while duplicate tabs still share one loader.
        self.market_reads = ReadCoalescer(
            ttl_seconds=1.0, max_concurrent=3, max_inflight=32,
        )
        # Account reads are costlier and more sensitive; two seconds is still
        # near-real-time for a cash transfer while collapsing tab/reload bursts.
        self.account_reads = ReadCoalescer(
            ttl_seconds=2.0, max_concurrent=2, max_inflight=16,
        )

    def hub_for(self, user_id: int) -> Hub:
        """사용자별 이벤트 링. 봇을 한 번이라도 띄운 사람 수만큼만 생깁니다."""
        hub = self.hubs.get(user_id)
        if hub is None:
            hub = self.hubs[user_id] = Hub()
        return hub


#: 호출자가 요청할 수 있는 최대 백테스트 기간. 템플릿이 스스로 선언한 창은
#: 이 서비스가 고른 것이라 그대로 두고, 요청이 밀어 넣는 창만 묶습니다.
MAX_BACKTEST_DAYS = 3660

#: 동시에 도는 백테스트 — 한 사람은 하나, 프로세스 전체로는 이만큼. 스레드로
#: 내보내도 GIL 은 하나뿐이라, 수를 묶지 않으면 "루프를 막지는 않지만 모두를
#: 느리게 하는" 것으로 바뀔 뿐입니다.
MAX_CONCURRENT_BACKTESTS = 3


class BacktestRequest(BaseModel):
    config_path: str | None = Field(default=None, max_length=200)
    config: dict | None = None
    start: str | None = Field(default=None, max_length=64)
    end: str | None = Field(default=None, max_length=64)
    starting_cash: float | None = Field(default=None, gt=0, le=1e15)


class ManualOrderRequest(BaseModel):
    ticker: str
    quantity: float | None = None
    notional: float | None = None
    limit_price: float | None = None
    #: hand the resulting position back to the strategy instead of pinning it
    manage: bool = False
    note: str = ""


class EvaluateRequest(BaseModel):
    """종목 하나를 지금 심의해 달라는 요청.

    함수 안에 두면 FastAPI 가 본문 모델로 알아보지 못하고 쿼리 파라미터로
    해석합니다 — 모든 호출이 422 로 떨어집니다.
    """

    ticker: str = Field(..., max_length=24)
    strategy: str | None = Field(None, max_length=64)


class ProfileRequest(BaseModel):
    """진단 답안 {question_id: option_id}"""

    answers: dict[str, str] = Field(default_factory=dict)


class ProfileOverrideRequest(BaseModel):
    """마이페이지에서 축을 직접 조정할 때. -1.0 ~ +1.0"""

    overrides: dict[str, float] = Field(default_factory=dict)


#: 저장할 수 있는 값 하나의 최대 길이. 어떤 거래소 키도 이 근처에 가지
#: 않습니다 — 가장 긴 것이 200자 남짓입니다.
MAX_SECRET_LEN = 512
MAX_SECRET_NAME_LEN = 64

#: 요청 본문 상한. 이 API 가 받는 것 중 가장 큰 것이 전략 설정 하나입니다.
MAX_BODY_BYTES = 256 * 1024


class RedeemRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)


class SetupRequest(BaseModel):
    """Values from the setup form. Blank fields leave existing ones alone.

    크기를 여기서 묶습니다. 가입은 공짜고 저장할 수 있는 이름은 스무 개
    남짓이라, 값 하나에 상한이 없으면 계정 하나가 4 MiB짜리 키를 스무 번
    밀어 넣습니다. 그 디스크는 모든 사용자의 포지션과 체결 기록이 사는
    곳이고, 채워지는 순간 봇들은 자기가 무엇을 들고 있는지 적지 못합니다.
    """

    values: dict[str, str] = Field(default_factory=dict)

    @field_validator("values")
    @classmethod
    def _bounded(cls, values: dict[str, str]) -> dict[str, str]:
        # 한 번에 보낼 수 있는 항목 수는 설정 화면이 가진 칸 수까지입니다
        # (계정 화면은 그보다 적게 쓰지만, 1인용 화면은 전부 씁니다).
        if len(values) > len(WRITABLE_KEYS):
            raise ValueError(
                f"한 번에 저장할 수 있는 항목은 {len(WRITABLE_KEYS)}개까지입니다")
        for key, value in values.items():
            if len(key or "") > MAX_SECRET_NAME_LEN:
                raise ValueError("설정 항목 이름이 너무 깁니다")
            if len(value or "") > MAX_SECRET_LEN:
                raise ValueError(
                    f"{(key or '')[:40]} 값이 너무 깁니다 "
                    f"({MAX_SECRET_LEN}자 이하로 입력하세요)")
        return values


class LimitsRequest(BaseModel):
    """Daily caps. Omitted fields are left alone; an explicit 0 removes a cap.

    Partial by design: a client that raises the order count must not silently
    release the loss cap it never mentioned.
    """

    max_daily_notional: float | None = Field(default=None, allow_inf_nan=False)
    max_daily_orders: int | None = None
    max_daily_loss: float | None = Field(default=None, allow_inf_nan=False)
    max_daily_loss_pct: float | None = Field(default=None, allow_inf_nan=False)


#: (요청 필드, TradingBudget 속성, .env 키, 변환)
_LIMIT_FIELDS = (
    ("max_daily_notional", "max_notional", "QUANT_LIMIT_DAILY_NOTIONAL", float),
    ("max_daily_orders", "max_orders", "QUANT_LIMIT_DAILY_ORDERS", int),
    ("max_daily_loss", "max_loss", "QUANT_LIMIT_DAILY_LOSS",
     lambda v: abs(float(v))),
    ("max_daily_loss_pct", "max_loss_pct", "QUANT_LIMIT_DAILY_LOSS_PCT",
     lambda v: abs(float(v))),
)


class StartRequest(BaseModel):
    config_path: str
    mode: str = Field(default="dry_run", pattern="^(dry_run|live)$")
    #: 실거래일 때만 필요합니다. `quant live` 가 콘솔에서 받는 것과 같은 확인 —
    #: 전략 이름을 정확히 적어야 합니다.
    confirm: str = ""


class AgentStartSpec(BaseModel):
    """그룹 안의 에이전트 한 대. 화면이 카드 하나마다 이걸 보냅니다."""

    model_config = {"extra": "forbid"}

    agent_id: str = Field(..., min_length=1, max_length=32)
    label: str = Field(..., min_length=1, max_length=40)
    config_path: str = Field(..., min_length=1, max_length=200)
    capital_weight: float = Field(..., gt=0.0, le=1.0)
    mode: str = Field(default="dry_run", pattern="^(dry_run|live)$")
    #: 실거래일 때만 필요합니다. 전략 이름을 정확히 적어야 합니다 — 에이전트
    #: 마다 따로 받습니다. 하나를 확인했다고 나머지 셋이 열리지 않습니다.
    confirm: str = ""


class GroupStartRequest(BaseModel):
    """에이전트 여럿을 한 계좌에 띄운다."""

    model_config = {"extra": "forbid"}

    agents: list[AgentStartSpec] = Field(..., min_length=1, max_length=4)


class ReconciliationConfirmations(BaseModel):
    """Five independent checks performed in the operator's Toss app."""

    model_config = {"extra": "forbid"}

    open_orders: str = Field(..., min_length=1, max_length=80)
    today_fills: str = Field(..., min_length=1, max_length=80)
    holdings: str = Field(..., min_length=1, max_length=80)
    cash: str = Field(..., min_length=1, max_length=80)
    daily_loss: str = Field(..., min_length=1, max_length=80)


class ReconciliationArchiveRequest(BaseModel):
    """Exact, auditable request to retire one quarantined Toss-live run."""

    model_config = {"extra": "forbid"}

    config_path: str = Field(..., min_length=1, max_length=200)
    run_id: int = Field(..., gt=0)
    reason: str = Field(..., min_length=10, max_length=500)
    confirmations: ReconciliationConfirmations
    acknowledgement: str = Field(..., min_length=1, max_length=100)


#: 이 주소들만 "내 컴퓨터에서만 보인다" 고 말할 수 있습니다.
_LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}


class UnsafeBind(RuntimeError):
    """공개 인터페이스에 토큰 없이 붙이려 할 때."""


def assert_safe_to_bind(host: str) -> None:
    """로그인이 불가능한 상태로 외부에 노출하려 하면 뜨지 않습니다.

    이전에는 경고 한 줄만 찍고 그대로 떴습니다. 그런데 호스팅 플랫폼은
    예외 없이 0.0.0.0 바인딩을 요구하므로, 하필 실제 배포 구성에서만
    경고가 무시되고 매수·매도·전량청산 엔드포인트가 인증 없이 열립니다.
    로그로 남길 성질이 아니라 뜨지 말아야 할 상태입니다.

    공유 토큰은 더 이상 없습니다 — 값 하나가 자리를 열면 그것은 로그인이
    아니라 로그인의 우회이기 때문입니다. 그래서 조건은 "토큰이 있는가" 가
    아니라 "**사람이 가입할 수 있는가**" 로 바뀌었습니다. 암호화 키가 없으면
    계정을 만들 수 없고, 계정이 없으면 이 API 에는 앉을 자리가 없습니다.
    """
    if host.strip().lower() in _LOOPBACK:
        return
    try:
        assert_ready_for_users()
    except SecretKeyMissing as exc:
        raise UnsafeBind(
            f"{host} 로 바인딩할 수 없습니다.\n{exc}") from None


def assert_ready_for_users(secret: str | None = None) -> None:
    """암호화 키 없이는 서비스를 열지 않습니다 — 공개 바인딩과 같은 이유로.

    `assert_safe_to_bind` 와 짝입니다. 그쪽은 "인증 없이 열지 마라", 이쪽은
    "남의 증권사 키를 평문으로 받아둘 상태로 열지 마라" 입니다. 둘 다 경고로
    두면 하필 실제 배포에서만 무시됩니다 — 로컬에서는 아무도 가입하지 않으니
    문제가 드러나지 않고, 사람이 붙기 시작하는 배포에서 처음으로 대가를
    치릅니다. 그래서 로그가 아니라 기동 거부입니다.
    """
    value = (secret if secret is not None
             else os.environ.get("QUANT_SECRET_KEY", "")).strip()
    how = ("  생성: QUANT_SECRET_KEY="
           "$(python3 -c \"import secrets;print(secrets.token_urlsafe(48))\")\n"
           "  주의: 이 값을 잃어버리면 저장된 자격증명을 되살릴 수 없습니다 — "
           "가입자들이 키를 다시 등록해야 합니다.")
    if not value:
        raise SecretKeyMissing(
            "QUANT_SECRET_KEY 가 없습니다. 이 값으로 가입자들의 증권사 API 키를 "
            f"암호화하므로, 없으면 서비스를 시작하지 않습니다.\n{how}")
    if len(value) < 32:
        # `Accounts` 도 같은 선을 긋습니다. 여기서 먼저 걸러야 "짧다" 는 말이
        # 스택 트레이스가 아니라 기동 거부 메시지로 나옵니다.
        raise SecretKeyMissing(
            f"QUANT_SECRET_KEY 가 너무 짧습니다 ({len(value)}자, 32자 이상 필요). "
            f"이 값 하나가 모든 가입자의 증권사 키를 지킵니다.\n{how}")


def assert_live_start_allowed(config: StrategyConfig, confirm: str) -> None:
    """`quant live` 가 요구하는 것과 정확히 같은 조건을 API 에도 건다.

    대시보드가 실거래로 가는 더 쉬운 길이 되면 안 됩니다. CLI 는 세 가지를
    요구합니다 — 설정 파일 자체가 mode: live 일 것, live_trading_confirmed
    일 것, 사람이 전략 이름을 직접 입력할 것. API 는 두 번째만 손으로 검사했고
    `cfg.mode = ...` 대입은 pydantic 검증을 다시 돌리지 않으므로, '하루 한도
    없는 실거래'가 POST 한 번으로 만들어졌습니다.

    사람마다 다시 겁니다. 여러 사람이 쓴다고 해서 확인 한 번이 모두를 위한
    확인이 되지는 않습니다.
    """
    if config.mode is not RunMode.LIVE:
        raise HTTPException(
            400,
            f"설정 파일 mode 가 {config.mode.value} 입니다. "
            "실거래는 설정 파일이 직접 mode: live 라고 선언한 경우에만 시작합니다 "
            "— API 로 모드를 바꿔 실거래로 넘어갈 수는 없습니다.",
        )
    if not config.broker.live_trading_confirmed:
        raise HTTPException(
            400,
            "refusing to start live trading: set broker.live_trading_confirmed "
            "in the config file, not through the API",
        )
    if confirm.strip() != config.name:
        raise HTTPException(
            400,
            f'실거래 확인이 필요합니다: confirm 에 전략 이름 "{config.name}" 을 '
            "정확히 넣으세요 (CLI 가 콘솔에서 묻는 것과 같은 확인입니다).",
        )


def users_db_path(state_path: str = "quant_state.db") -> str:
    """계정 DB 자리 — `QUANT_USERS_DB`, 없으면 상태 DB 옆."""
    explicit = os.environ.get("QUANT_USERS_DB", "").strip()
    if explicit:
        return explicit
    return str(Path(state_path).expanduser().parent / "quant_users.db")


def registered_accounts(users_db: str) -> int:
    """계정 DB 에 사람이 몇 명 있는지 — 복호화 없이 파일만 셉니다.

    `QUANT_SECRET_KEY` 가 사라지면 `Accounts` 를 열 수 없고, 그러면 이 API 는
    계정이 없는 1인용 배포로 되돌아갑니다. 되돌아간 자리에서 공유 토큰 하나가
    다시 모든 것을 여는데, 정작 가입자들의 데이터는 그대로 디스크에 있습니다.
    "계정이 있는가" 는 그래서 복호화 없이도 답할 수 있어야 합니다.
    """
    path = Path(users_db)
    if not path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            table = conn.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type='table' AND name='users'").fetchone()
            if not table or not table[0]:
                return 0
            return int(conn.execute("SELECT count(*) FROM users").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        # 읽을 수 없는 계정 DB 가 거기 있다는 것 자체가 "1인용이 아니다" 입니다.
        log.warning("계정 DB 를 읽지 못했습니다: %s — 1인용 경로를 닫습니다", users_db)
        return 1


def user_data_root(state_path: str = "quant_state.db") -> str:
    """사용자별 파일이 놓이는 곳 — `QUANT_USER_DATA`, 없으면 상태 DB 옆.

    계정 DB 와 같은 규칙을 쓰는 데는 배포상의 이유가 있습니다. 호스팅에서
    영구 디스크는 보통 하나만 붙이고 상태 DB 를 그 위에 올립니다. 사용자
    디렉터리만 작업 디렉터리 기준으로 남으면, **포지션과 체결 기록이 재배포마다
    사라집니다** — 봇은 다시 뜨지만 자기가 무엇을 들고 있었는지 모르는 채로 뜹니다.
    """
    explicit = os.environ.get("QUANT_USER_DATA", "").strip()
    if explicit:
        return explicit
    return str(Path(state_path).expanduser().parent / "users")


# ── 계정에 저장할 수 있는 설정값 ─────────────────────────────────────────
#: 서비스 전체를 바꾸는 값들. 허용 목록에는 있지만 **한 사람의 계정에는**
#: 들어갈 수 없습니다. 여기가 비어 있으면 가입자 한 명이 `QUANT_API_TOKEN` 을
#: 저장해 전체 배포의 토큰을 정하거나, `QUANT_LIMIT_DAILY_*` 로 모두의 하루
#: 한도를 바꿉니다 — 후자는 실제로 예전 `/api/limits` 가 하던 일입니다.
_SERVICE_SCOPED = frozenset({
    "OPERATOR_NAME", "QUANT_API_TOKEN", "CORS_ORIGINS",
    "QUANT_LIMIT_DAILY_NOTIONAL", "QUANT_LIMIT_DAILY_ORDERS",
    "QUANT_LIMIT_DAILY_LOSS", "QUANT_LIMIT_DAILY_LOSS_PCT",
})

#: 계정에 저장할 수 있는 이름 전부. `WRITABLE_KEYS` 에서 빼는 방식이라,
#: 거래소가 하나 늘면 설정 화면과 이쪽이 자동으로 같이 늡니다 — 목록을 따로
#: 적어두면 새 거래소 필드가 조용히 사라지는 쪽으로 어긋납니다.
ACCOUNT_KEYS: frozenset[str] = frozenset(WRITABLE_KEYS) - _SERVICE_SCOPED

#: 계정 화면이 보여줄 운영자 항목. 이름과 대시보드 토큰은 계정이 대신하므로
#: 뺍니다 — 화면에 남겨두면 로그인한 사람 옆에 중복된 신원이 하나 더 섭니다.
#: 계정 화면에서 자기 것으로 넣을 수 있는 LLM 키. 서비스가 Gemini 로 데스크
#: 비용을 내므로, 자기 키를 넣는 것은 사용량 상한을 벗어나고 싶을 때뿐입니다 —
#: 그래서 서비스가 실제로 쓰는 제공자와 같은 것만 보여줍니다. 쓰지도 않는
#: 제공자의 칸이 서 있으면 사용자는 그것이 필요한 값이라고 읽습니다.
_BYO_LLM_KEY = "GOOGLE_API_KEY"

#: 자기 키 칸에 붙는 설명. 계정 화면에서는 "선택"의 뜻이 달라집니다 —
#: 넣지 않아도 데스크는 돌고, 넣으면 상한이 없어집니다.
_BYO_LLM_LABEL = "Gemini API 키 — 넣으면 AI 데스크 사용 한도가 없어집니다 (선택)"

ACCOUNT_OPERATOR_FIELDS = [
    # BYO 키는 아래에서 계정용 설명을 달아 한 번만 넣습니다. 여기서 통과시키면
    # 같은 칸이 두 번 서고, 화면은 둘 중 어느 쪽이 진짜인지 말해 주지 못합니다.
    (env, label, required)
    for env, label, required in OPERATOR_FIELDS
    if env not in _SERVICE_SCOPED and not env.endswith("_API_KEY")
] + [(_BYO_LLM_KEY, _BYO_LLM_LABEL, False)]

#: 프로세스 환경에 남아 있으면 안 되는 이름들 — 계좌에 닿거나 사람에게 닿는 값.
#:
#: LLM 키(ANTHROPIC/OPENAI/GOOGLE)는 일부러 뺐습니다. 그건 서비스가 자기 비용으로
#: 제공하기로 한 값이고 주문을 낼 수 없습니다 — 여기서 지우면 자기 키가 없는
#: 사용자의 AI 데스크가 통째로 꺼집니다. 증권사 키와 알림 토큰은 반대입니다:
#: 프로세스 전역에 있는 순간 "운영자 계좌로 아무나 주문을 낸다"거나 "남의
#: 텔레그램으로 내 체결이 간다" 가 됩니다.
_NEVER_PROCESS_WIDE: frozenset[str] = frozenset(
    [env for venue in VENUES for env, _, _ in venue.fields]
    + ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
)


def account_rejection_reason(key: str) -> str:
    """이 값을 **계정에** 저장할 수 없는 이유. 저장해도 되면 빈 문자열.

    파일 저장소(`CredentialStore`)와 같은 규칙을 먼저 그대로 겁니다 —
    프로세스를 조종하는 변수 금지, 설정 화면이 아는 이름만 허용. 저장 경로가
    `.env` 에서 계정 DB 로 바뀌었다고 규칙이 헐거워지면, 예전에 막았던 것이
    새 경로로 그대로 들어옵니다.
    """
    reason = rejection_reason(key)
    if reason:
        return reason
    if key.strip() not in ACCOUNT_KEYS:
        return "서비스 전체 설정이라 계정에서는 바꿀 수 없습니다"
    return ""


def _scrub(text: str, secrets_used: dict[str, str]) -> str:
    """검증 실패 메시지에서 값이 보이면 지웁니다.

    어댑터 예외는 대개 이름만 말하지만, 거래소 SDK 는 요청 본문을 그대로 붙여
    던지기도 합니다. 화면으로 나가는 문자열에 키가 섞이는 경로는 여기 하나뿐이라
    여기서 끊습니다.
    """
    for value in secrets_used.values():
        if value and len(value) >= 6 and value in text:
            text = text.replace(value, "***")
    return text


#: 각 자격증명이 어떤 모양이어야 하는가. 값을 검사하는 게 아니라 **모양**만
#: 봅니다 — 유효한지는 불러 봐야 알지만, "브라우저 자동완성이 로그인
#: 비밀번호를 넣어 두었다" 같은 것은 부르기 전에 알 수 있습니다.
#:
#: (접두사, 최소 길이, 사람이 읽을 이름)
_KEY_SHAPE: dict[str, tuple[str, int, str]] = {
    "TOSS_CLIENT_ID": ("tsck_", 20, "토스 클라이언트 ID"),
    "TOSS_CLIENT_SECRET": ("tssk_", 20, "토스 클라이언트 시크릿"),
    "KIS_APP_KEY": ("", 30, "KIS 앱 키"),
    "KIS_APP_SECRET": ("", 100, "KIS 앱 시크릿"),
}


def _shape_problem(env: str, value: str) -> str:
    """이 값이 그 자리에 들어갈 모양인가. 문제 없으면 빈 문자열.

    자동완성이 채운 로그인 비밀번호, 복사하다 딸려온 공백, 잘린 값 —
    전부 부르기 전에 알 수 있는 것들이고, 부른 뒤에는 서버가 그냥
    `access_denied` 라고만 답합니다.
    """
    shape = _KEY_SHAPE.get(env)
    if not shape or not value:
        return ""
    prefix, min_len, label = shape
    if value != value.strip():
        return f"{label}: 앞뒤에 공백이나 줄바꿈이 섞여 있습니다"
    if any(ch.isspace() for ch in value):
        return f"{label}: 값 안에 공백이 있습니다 — 복사할 때 잘린 것 같습니다"
    if prefix and not value.startswith(prefix):
        return (f"{label}: '{prefix}' 로 시작해야 합니다. 브라우저 자동완성이 "
                f"다른 값을 채웠을 수 있습니다 — 칸을 비우고 콘솔에서 복사한 "
                f"값을 다시 붙여 넣어 보세요.")
    if len(value) < min_len:
        return f"{label}: 너무 짧습니다({len(value)}자) — 값이 잘린 것 같습니다"
    return ""


async def _public_ip() -> str:
    """이 서버가 바깥으로 나갈 때 쓰는 공인 IP.

    토스는 허용 IP 목록에 없는 곳에서 부르면 키가 맞아도 403 입니다. 그런데
    "이 서버의 IP" 를 사용자가 알 방법이 없습니다 — 집에서 돌리면 집 IP,
    배포하면 그 플랫폼의 IP 이고, 화면에는 어느 쪽도 적혀 있지 않습니다.
    그래서 등록해야 할 값을 우리가 직접 알아내서 보여줍니다.

    조회 실패는 조용히 넘깁니다. IP 를 못 알아냈다고 설정 화면이 죽으면
    그게 더 나쁩니다.
    """
    import httpx

    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            async with httpx.AsyncClient(timeout=6) as c:
                r = await c.get(url)
            text = (r.text or "").strip()
            # 아주 느슨한 확인 — 응답이 IP 처럼 생겼는지만 봅니다.
            if r.status_code == 200 and 6 <= len(text) <= 45 and " " not in text:
                return text
        except Exception:                   # noqa: BLE001 — 다음 것을 시도
            continue
    return ""


async def _verify_kis(values: dict[str, str]) -> dict:
    """토큰만 보지 않고 **봇이 실제로 밟는 길**을 밟아 봅니다.

    토큰 발급만 확인하면 "검증 성공" 이 뜬 뒤에도 봇이 워밍업에서 죽습니다 —
    시세 조회 권한은 토큰과 별개이고, 그 실패는 시작 버튼을 누른 다음에야
    드러납니다. 여기서 현재가와 일봉까지 받아 보면, 어디서 막히는지 시작하기
    전에 알 수 있습니다.

    실전과 모의를 둘 다 시도합니다. 어느 쪽 키인지는 사용자도 헷갈리는
    부분이고, 우리가 대신 알아봐 주면 되는 일입니다.
    """
    from quant.core.types import Symbol
    from quant.data.providers.kis import KisProvider, kis_token

    key, secret = values["KIS_APP_KEY"], values["KIS_APP_SECRET"]
    steps: list[dict] = []
    env_name = ""
    for paper, label in ((False, "실전"), (True, "모의투자")):
        try:
            await kis_token(key, secret, paper=paper)
            steps.append({"step": f"{label} 토큰 발급", "ok": True})
            env_name = label
            break
        except Exception as exc:
            steps.append({"step": f"{label} 토큰 발급", "ok": False,
                          "detail": _short(exc)})
    if not env_name:
        return {"ok": False, "steps": steps,
                "error": "앱 키·시크릿으로 토큰을 받지 못했습니다. 한국투자증권 "
                         "개발자센터에서 발급한 값이 맞는지, 앞뒤 공백이 섞이지 "
                         "않았는지 확인하세요."}

    paper = env_name == "모의투자"
    provider = KisProvider(app_key=key, app_secret=secret, paper=paper)
    sample = Symbol("005930", venue="kis", quote_currency="KRW")
    try:
        quote = await provider.quote(sample)
        if quote is None:
            steps.append({"step": "현재가 조회 (삼성전자)", "ok": False,
                          "detail": "응답에 가격이 없습니다"})
            return {"ok": False, "steps": steps, "environment": env_name,
                    "error": f"{env_name} 토큰은 받았는데 시세가 오지 않습니다. "
                             f"해당 앱에 국내주식 시세 조회 권한이 있는지 "
                             f"확인하세요 — 봇은 이 단계에서 멈춥니다."}
        steps.append({"step": "현재가 조회 (삼성전자)", "ok": True,
                      "detail": f"{quote.mid:,.0f}원"})

        end = datetime.now(UTC)
        bars = await provider.history(sample, "1d", end - timedelta(days=90), end)
        if len(bars) < 10:
            steps.append({"step": "일봉 조회 (90일)", "ok": False,
                          "detail": f"{len(bars)}개만 왔습니다"})
            return {"ok": False, "steps": steps, "environment": env_name,
                    "error": "현재가는 오는데 과거 일봉이 부족합니다. 봇은 워밍업에 "
                             "최소 10봉이 필요해서 이 상태로는 시작하지 못합니다."}
        steps.append({"step": "일봉 조회 (90일)", "ok": True,
                      "detail": f"{len(bars)}봉, 마지막 {bars[-1].ts.date()}"})
    finally:
        with contextlib.suppress(Exception):
            await provider.close()

    return {"ok": True, "environment": env_name, "steps": steps,
            "detail": f"{env_name} 환경에서 토큰·현재가·일봉까지 확인했습니다."}


async def _verify_toss(values: dict[str, str]) -> dict:
    """토큰뿐 아니라 실제로 주문 전 필요한 계좌 truth까지 읽습니다.

    전에는 현재가만 성공하면 연동 성공이라고 했습니다. 하지만 시세는 계좌
    헤더를 쓰지 않아, 계좌번호를 ``accountSeq`` 자리에 그대로 넣은 설정도
    통과했습니다. 시작 뒤에야 잔고가 400으로 죽고 설정의 80만원이 실제 잔고
    행세를 했습니다. 여기서는 주문 없이 accounts/holdings/buying-power까지
    같은 길을 먼저 밟습니다.
    """
    from quant.brokerage.toss_broker import TossBrokerage, TossProvider, toss_token
    from quant.core.account import Portfolio
    from quant.core.types import Symbol

    cid, secret = values["TOSS_CLIENT_ID"], values["TOSS_CLIENT_SECRET"]
    steps: list[dict] = []
    try:
        await toss_token(cid, secret)
        steps.append({"step": "OAuth 토큰 발급", "ok": True})
    except Exception as exc:
        return {"ok": False, "steps": [{"step": "OAuth 토큰 발급", "ok": False,
                                        "detail": _short(exc)}],
                "error": "클라이언트 ID·시크릿으로 토큰을 받지 못했습니다. "
                         "토스증권 Open API 콘솔에서 발급한 값이 맞는지 확인하세요."}

    account_no = values.get("TOSS_ACCOUNT_NO", "")
    broker = TossBrokerage(
        Portfolio(0.0, "KRW"), client_id=cid, client_secret=secret,
        account_no=account_no, live=False, reconcile_on_start=False,
    )
    try:
        overview = await broker.account_overview()
        buying_power = overview.get("cash_buying_power") or {}
        market_value = overview.get("market_value") or {}
        if not isinstance(buying_power.get("KRW"), (int, float)):
            raise ValueError("KRW 현금 매수 가능 금액이 없습니다")
        if not isinstance(market_value.get("KRW"), (int, float)):
            raise ValueError("KRW 보유 주식 평가금액이 없습니다")
        steps.append({"step": "실계좌 식별·잔고 조회", "ok": True,
                      "detail": "accountSeq·보유주식·현금 매수 가능 금액 확인"})
    except Exception as exc:                       # noqa: BLE001
        steps.append({"step": "실계좌 식별·잔고 조회", "ok": False,
                      "detail": _short(exc)})
        return {
            "ok": False,
            "steps": steps,
            "error": "토큰은 유효하지만 등록한 계좌를 안전하게 식별하거나 "
                     "실제 잔고를 읽지 못했습니다. 계좌번호/accountSeq와 Open API "
                     "계좌 조회 권한을 확인하세요 — 이 상태에서는 실거래를 "
                     "시작하지 않습니다.",
        }
    finally:
        with contextlib.suppress(Exception):
            await broker.close()

    provider = TossProvider(client_id=cid, client_secret=secret,
                            account_no=account_no)
    sample = Symbol("005930", venue="toss", quote_currency="KRW")
    try:
        snapshot = await provider.market_snapshot(sample, depth=1, trade_count=0)
        price = (snapshot.get("quote") or {}).get("price")
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            steps.append({"step": "현재가 조회 (삼성전자)", "ok": False})
            return {"ok": False, "steps": steps,
                    "error": "토큰은 받았는데 시세가 오지 않습니다. 해당 앱에 "
                             "시세 조회 권한이 있는지 확인하세요 — 봇은 이 "
                             "단계에서 멈춥니다."}
        top = bool((snapshot.get("capabilities") or {}).get("top_of_book"))
        detail = f"{price:,.0f}원"
        if not top:
            detail += " · 현재 호가 없음 (신규 진입은 호가가 생길 때까지 보류)"
        steps.append({"step": "현재가 조회 (삼성전자)", "ok": True,
                      "detail": detail})
    finally:
        with contextlib.suppress(Exception):
            await provider.close()
    return {"ok": True, "steps": steps,
            "detail": "토큰·실계좌 잔고·현재가까지 읽기 전용으로 확인했습니다."}


def _short(exc: Exception) -> str:
    """예외를 한 줄로. 값이 아니라 무슨 일이 있었는지만 남깁니다."""
    text = str(exc).replace("\n", " ")
    return text[:160] if text else type(exc).__name__


async def verify_venue(venue_id: str, values: dict[str, str]) -> dict:
    """거래소에 읽기 전용 호출 한 번. 값은 **인자로만** 흐릅니다.

    `CredentialStore.verify()` 와 하는 일은 같지만 `os.environ` 을 읽지
    않습니다. 여러 사람이 쓰는 프로세스에서 검증을 위해 환경변수에 키를 잠깐
    올리는 순간, 그 잠깐 동안 다른 사람의 봇이 그 키를 집어갑니다.
    """
    spec = VENUES_BY_ID.get(venue_id)
    if spec is None:
        return {"ok": False, "error": f"알 수 없는 거래소: {venue_id}"}
    missing = [env for env, _, required in spec.fields
               if required and not values.get(env)]
    if missing:
        return {"ok": False, "error": f"미입력 항목: {', '.join(missing)}"}

    # 부르기 전에 알 수 있는 것부터. 서버는 잘못된 값에 `access_denied` 라고만
    # 답하고, 그 한 마디로는 무엇이 잘못됐는지 알 수 없습니다.
    shape_issues = [msg for env in values
                    if (msg := _shape_problem(env, values.get(env, "")))]
    if shape_issues:
        return {"ok": False,
                "steps": [{"step": "저장된 값 모양 확인", "ok": False,
                           "detail": msg} for msg in shape_issues],
                "error": shape_issues[0]}

    try:
        if venue_id == "kis":
            return await _verify_kis(values)

        if venue_id == "toss":
            return await _verify_toss(values)

        if venue_id == "alpaca":
            import httpx

            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    "https://paper-api.alpaca.markets/v2/account",
                    headers={"APCA-API-KEY-ID": values["ALPACA_API_KEY"],
                             "APCA-API-SECRET-KEY": values["ALPACA_SECRET_KEY"]},
                )
            r.raise_for_status()
            return {"ok": True, "detail": "페이퍼 계좌 조회 성공"}

        import ccxt.async_support as ccxt_async

        key_env, secret_env = [f[0] for f in spec.fields[:2]]
        ex = getattr(ccxt_async, venue_id)({
            "apiKey": values[key_env], "secret": values[secret_env],
            "enableRateLimit": True,
        })
        try:
            balance = await ex.fetch_balance()
            nonzero = sum(1 for v in (balance.get("total") or {}).values() if v)
            return {"ok": True, "detail": f"잔고 조회 성공 (보유 자산 {nonzero}종)"}
        finally:
            await ex.close()
    except ImportError as exc:
        return {"ok": False, "error": f"필요한 패키지가 없습니다: {exc}"}
    except Exception as exc:
        return {"ok": False,
                "error": _scrub(f"{type(exc).__name__}: {str(exc)[:200]}", values)}


# ── 전략 템플릿 ──────────────────────────────────────────────────────────
def strategy_catalog(root: str | Path | None = None) -> dict[str, Path]:
    """이 서비스가 돌려주는 전략들 — 이름 → 파일.

    가입자가 서버의 **경로**를 지정할 수 있으면 그것은 설정 선택이 아니라
    파일 열람입니다. 이름만 받고, 열리는 파일은 언제나 이 표 안의 값입니다.
    """
    base = Path(root or os.environ.get("QUANT_CONFIG_DIR", "configs"))
    if not base.is_dir():
        return {}
    return {p.stem: p for p in sorted(base.iterdir())
            if p.is_file() and p.suffix.lower() in (".yaml", ".yml", ".json")}


def _config_symbols(config: StrategyConfig) -> list[Symbol]:
    """이 전략이 다루는 종목들. 조회는 여기 있는 것만 허용합니다.

    사용자가 임의의 티커를 물어볼 수 있으면 그것은 시세 조회가 아니라
    이 서비스의 데이터 계약을 남의 종목으로 넓히는 일이 됩니다.
    """
    from quant.strategy.builder import build_symbol

    return [build_symbol(spec) for spec in config.universe.symbols]


def _validated_l1_quote(quote: object, symbol: Symbol,
                        received_at: datetime) -> tuple[dict | None, str]:
    """Validate one provider quote before any API path can display it."""
    if quote is None:
        return None, ""
    try:
        quote_key = quote.symbol.key
        quote_ts = quote.ts
        bid = float(quote.bid)
        ask = float(quote.ask)
        bid_size = float(quote.bid_size)
        ask_size = float(quote.ask_size)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None, "호가 응답 형식이 올바르지 않습니다"
    if quote_key != symbol.key:
        return None, "다른 종목의 호가가 돌아왔습니다"
    if not isinstance(quote_ts, datetime) or quote_ts.tzinfo is None:
        return None, "거래소 시각에 timezone이 없습니다"
    values = (bid, ask, bid_size, ask_size, (bid + ask) / 2.0)
    if not all(math.isfinite(value) for value in values):
        return None, "호가가 유한한 숫자가 아닙니다"
    if bid <= 0 or ask <= 0 or ask < bid:
        return None, "매수·매도 호가 범위가 올바르지 않습니다"
    if bid_size < 0 or ask_size < 0:
        return None, "호가 수량이 0보다 작습니다"
    signed_age = (
        received_at - quote_ts.astimezone(UTC)
    ).total_seconds() * 1000
    if signed_age < -QUOTE_FUTURE_TOLERANCE.total_seconds() * 1000:
        return None, "호가 시각이 서버 시각보다 미래입니다"
    return {
        "quote": quote,
        "ts": quote_ts,
        "bid": bid,
        "ask": ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "price": (bid + ask) / 2.0,
        "age_ms": max(0, round(signed_age)),
    }, ""


def _select_run_readonly(store: StateStore, strategy: str, mode: str,
                         agent_id: str = "") -> int | None:
    """Attach read methods to the latest lifecycle head without reopening it.

    ``StateStore.resume_run`` also clears ``runs.stopped_at``.  That is correct
    when a trader truly resumes, but a chart refresh must not make a stopped or
    quarantined run look live.  Archived heads remain terminal, matching the
    selection semantics of ``resume_run`` without any UPDATE/COMMIT.
    """
    # 그룹에서는 에이전트마다 자기 run 이 있습니다. `agent_id` 없이 고르면
    # 같은 전략을 쓰는 형제 중 **가장 최근 것** 이 걸리고, 화면은 어느
    # 에이전트의 곡선인지 모른 채 그것을 그립니다 — 탭을 눌러도 안 바뀝니다.
    row = store.conn.execute(
        "SELECT id, archived_at FROM runs WHERE strategy=? AND mode=? "
        "AND agent_id=? ORDER BY id DESC LIMIT 1",
        (strategy, mode, str(agent_id or "")),
    ).fetchone()
    store.run_id = (
        int(row["id"])
        if row is not None and row["archived_at"] is None else None
    )
    return store.run_id


def _name_feed(seat: Desk, config: StrategyConfig | None):
    """이름 조회에 쓸 프로바이더. 못 세우면 None.

    이름은 있으면 좋은 것이고, 없다고 화면이 죽으면 안 됩니다. 증권사 키를
    아직 등록하지 않은 사람에게도 목록은 떠야 하고, 그때 정적 표와 조회
    기록이 답을 냅니다.
    """
    if config is None:
        return None
    try:
        return seat.data_provider(config)
    except Exception as exc:                # noqa: BLE001
        log.debug("이름 조회용 프로바이더를 세우지 못했습니다: %s", exc)
        return None


def _named_status(payload: dict, book: NameBook) -> dict:
    """`/api/status` 가 내보내는 티커들에 이름을 답니다.

    `LiveTrader.status()` 자체는 건드리지 않습니다 — 같은 함수를 CLI 도
    쓰는데, 화면 하나 때문에 터미널 출력의 모양을 바꿀 이유가 없습니다.
    """
    out = dict(payload)
    universe = out.get("universe")
    if isinstance(universe, list):
        out["universe"] = book.labels(universe)
    portfolio = out.get("portfolio")
    if isinstance(portfolio, dict) and isinstance(portfolio.get("positions"), list):
        out["portfolio"] = {**portfolio,
                            "positions": book.tag(portfolio["positions"], "symbol")}
    return out


def _template_config(name: str | None):
    """전략 이름으로 설정을 읽습니다. 없거나 잘못된 이름이면 None.

    검색은 봇이 꺼져 있을 때도 되어야 합니다 — 데스크가 어떤 종목을 사라고
    했을 때, 그걸 찾으려고 먼저 봇을 켜야 한다면 순서가 뒤바뀝니다.
    """
    if not name:
        return None
    try:
        return load_config(str(resolve_template(name)))
    except Exception:
        return None


def _selected_read_config(seat: Desk, strategy: str | None,
                          agent_id: str = "") -> StrategyConfig | None:
    """Resolve one read-only screen without hiding an explicit bad selection."""
    if seat.running(agent_id) or (not agent_id and seat.running()):
        picked = seat.run_config(agent_id)
        if picked is not None:
            return picked
    if strategy is not None:
        # `resolve_template` preserves the useful 400 response. Falling through
        # to the process default here can show another strategy/account while
        # the URL still names the missing one.
        path = resolve_template(strategy)
        try:
            return load_config(str(path))
        except Exception as exc:
            log.warning("조회용 전략 템플릿을 읽지 못했습니다 (%s): %s", path.name, exc)
            raise HTTPException(
                400, "선택한 전략 템플릿의 설정이 올바르지 않습니다",
            ) from None
    return seat.run_config()


def _standalone_context(cfg, symbol, bars):
    """심의 한 번을 위한 최소 Context.

    데스크는 과거 봉을 읽어서 브리핑을 만듭니다. 그래서 엔진 없이도 봉을
    담은 Context 가 있으면 심의가 됩니다 — 다만 포트폴리오는 비어 있고
    시계는 마지막 봉에 맞춥니다. 실제 보유 수량을 넣지 않는 것은 의도적
    입니다: 이 호출은 "살까?" 를 묻는 것이고, 주문을 내지 않습니다.
    """
    from quant.core.account import Portfolio
    from quant.core.clock import SimClock
    from quant.core.context import Context
    from quant.core.events import EventBus

    clock = SimClock(bars[-1].end_ts)
    ctx = Context(clock=clock, portfolio=Portfolio(starting_cash=0.0),
                  bus=EventBus(), timeframe=cfg.data.timeframe,
                  run_mode=cfg.mode, history_size=max(len(bars), 200))
    ctx.universe = [symbol]
    for bar in bars:
        ctx.push_bar(bar)
    return ctx


def finite(value):
    """NaN·무한대를 None 으로 바꾼 사본. 중첩 dict/list 를 따라 들어갑니다.

    JSON 에는 NaN 이 없습니다. FastAPI 의 인코더는 그것을 만나면 응답 전체를
    500 으로 바꾸고 — 실제로 그랬습니다 — 화면에는 "서버 오류" 만 뜹니다.
    직렬화를 느슨하게 풀면 이번엔 브라우저의 `JSON.parse` 가 죽습니다.

    NaN 이 나오는 자리는 대개 나눗셈의 분모가 0 인 곳입니다: 거래가 없는데
    승률, 변동성이 0 인데 샤프. 그 자리의 정직한 값은 0 이 아니라 "없음"
    이고, 화면은 None 을 "—" 로 그립니다. 0 으로 바꾸면 "계산해 봤더니
    0" 처럼 보여서 더 나쁩니다.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite(v) for v in value]
    return value


class SafeJSONResponse(JSONResponse):
    """NaN 을 지나보내지 않는 응답.

    한 자리에서 막습니다. 엔드포인트마다 기억해서 거르게 하면 반드시 한 곳을
    빠뜨리고, 빠뜨린 그곳은 평소에 잘 돌다가 거래가 0건인 날에만 터집니다.
    """

    def render(self, content) -> bytes:
        return super().render(finite(content))


def resolve_template(name: str) -> Path:
    catalog = strategy_catalog()
    # 경로처럼 생긴 것이 와도 이름만 뽑아 씁니다. `../../etc/passwd` 는
    # `passwd` 가 되고, 표에 없으니 거기서 끝납니다.
    wanted = Path((name or "").strip()).stem
    path = catalog.get(wanted)
    if path is None:
        raise HTTPException(
            400,
            f"전략 템플릿 이름을 지정하세요 (서버 파일 경로는 쓸 수 없습니다). "
            f"사용 가능: {', '.join(sorted(catalog)) or '없음'}",
        )
    return path


def _with_mode(config: StrategyConfig, mode: RunMode) -> StrategyConfig:
    """모드를 바꿨으면 스키마 검증을 다시 돌린다.

    대입만으로는 돌지 않아서 '하루 한도 없는 실거래'와 'paper 브로커 실거래'가
    그대로 통과했습니다.
    """
    try:
        return StrategyConfig.model_validate({**config.model_dump(), "mode": mode})
    except ValidationError as exc:
        raise HTTPException(
            400, f"이 설정으로는 시작할 수 없습니다: {exc.errors()[0]['msg']}"
        ) from exc


def _parse_ts(value: str, field: str) -> datetime:
    """ISO 8601 하나. 파싱 실패는 500 이 아니라 400 입니다."""
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            400, f"{field} 형식이 올바르지 않습니다 (예: 2024-01-01T00:00:00Z)"
        ) from exc


def _assert_window_bounded(config: StrategyConfig) -> None:
    """요청이 정한 백테스트 창이 상식적인 크기인지.

    시작이 끝보다 뒤면 러너가 `ValueError` 로 끝내는데, 그건 화면에 500 으로
    나갑니다. 길이 쪽은 비용의 문제입니다 — 창이 길수록 CPU 를 오래 씁니다.
    """
    end = config.backtest.end or datetime.now(UTC)
    start = config.backtest.start or (end - timedelta(days=730))
    start = start if start.tzinfo else start.replace(tzinfo=UTC)
    end = end if end.tzinfo else end.replace(tzinfo=UTC)
    if start >= end:
        raise HTTPException(400, "백테스트 시작이 종료보다 앞서야 합니다")
    if (end - start).days > MAX_BACKTEST_DAYS:
        raise HTTPException(
            400, f"백테스트 기간이 너무 깁니다 — 최대 {MAX_BACKTEST_DAYS}일입니다")


def _apply_profile_live(trader, profile) -> dict | None:
    """실행 중인 봇에 즉시 반영할 수 있는 것만 반영한다 (1인용 경로).

    사이즈·손절·한도는 바로 바뀌지만 봉 주기나 알파 구성은 바뀌지 않습니다 —
    그건 엔진을 다시 세워야 하는 일이라, 반쯤 바뀐 상태로 돌리는 것보다
    재시작이 필요하다고 말하는 편이 정직합니다.
    """
    if trader is None:
        return None
    settings = profile.settings()
    engine = trader.engine
    pm = engine.portfolio_model
    pm.max_position_weight = settings["max_position_weight"]
    pm.max_gross_leverage = settings["max_gross_leverage"]
    pm.cash_reserve_pct = settings["cash_reserve_pct"]
    if hasattr(pm, "target_vol"):
        pm.target_vol = settings["target_annual_vol"]
    budget = engine.budget
    budget.max_loss_pct = settings["max_daily_loss_pct"]
    budget.max_orders = settings["max_daily_orders"]
    for model in engine.risk.models:
        if model.name == "max_dd_per_security":
            model.atr_multiple = settings["stop_atr_multiple"]
            model.limit = settings["stop_ceiling_pct"]
        elif model.name == "trailing_stop":
            model.atr_multiple = settings["trailing_atr_multiple"]
        elif model.name == "max_positions":
            model.max_positions = settings["max_positions"]
    return {
        "sizing_and_risk": "즉시 적용됨",
        "needs_restart": ["봉 주기", "알파 모델 구성", "AI 데스크 사용 여부"],
    }


# ── 요청 하나가 만질 수 있는 전부 ────────────────────────────────────────
class Desk:
    """봇 하나, 상태 파일 하나, 성향 하나, 한도 하나, 자격증명 한 벌.

    엔드포인트는 이것만 받습니다. `state.trader` 를 직접 만지는 줄이 하나라도
    남으면 그 줄이 남의 봇을 조종하는 경로가 되므로, 고르는 일은 `_desk`
    한 곳에만 있고 나머지는 자기 데스크만 압니다.

    에이전트가 여럿이면 `agent_id` 가 그중 하나를 가리킵니다. 비워 두면
    레지스트리가 **되묻습니다**(`AgentRequired`, 400) — 조용히 하나를 고르면
    `close_all` 이 그 하나만 정리하고 성공을 돌려주고, 사용자는 전부 정리된
    줄 알고 화면을 닫습니다.
    """

    user: User | None = None

    # ── 봇 ───────────────────────────────────────────────────────────────
    def trader(self, agent_id: str = "") -> Any:
        raise NotImplementedError

    def require_trader(self, agent_id: str = "") -> Any:
        raise NotImplementedError

    def agents(self) -> list[str]:
        """지금 도는 에이전트 목록. 그룹이 아니면 빈 목록입니다."""
        return []

    def running(self, agent_id: str = "") -> bool:
        trader = self.trader(agent_id)
        if trader is not None and trader.running:
            return True
        # 에이전트가 둘 이상이면 `trader()` 는 None 입니다(어느 것인지 되묻는
        # 규칙). 그 None 을 "안 돈다" 로 읽으면 /api/health 가 실거래 그룹을
        # 통째로 멈춘 것으로 보고합니다.
        if agent_id:
            return False
        registry = getattr(self, "registry", None)
        user = getattr(self, "user", None)
        return bool(registry is not None and user is not None
                    and registry.group(user.id) is not None)

    def desk_model(self, agent_id: str = ""):
        trader = self.trader(agent_id)
        return trader.desk() if trader is not None else None

    def release_halt(self, agent_id: str = "") -> dict:
        """오늘 하루만 한도를 면제한다.

        `agent_id` 가 비었는데 그룹이 돌면 **계좌 전체** 한도입니다. 예전에는
        여기서 `require_trader()` 만 불러, 그룹에서는 "어느 에이전트?" 로 되묻고
        에이전트를 지정하면 그 에이전트의 한도만 풀렸습니다 — 걸린 것이 계좌
        한도일 때 그것을 푸는 길이 화면 어디에도 없었습니다.
        """
        budget = self.registry._live_budget(self.user.id, agent_id)
        if budget is None:
            # 이 경로가 곧 "어느 에이전트?" 를 묻습니다.
            budget = self.require_trader(agent_id).engine.budget
        budget.release()
        return budget.status()

    async def sync(self, agent_id: str = "") -> dict:
        engine = self.require_trader(agent_id).engine
        # A cumulative live fill can appear in both the order detail and the
        # holdings snapshot.  The normal trader tick books order fills before
        # adopting venue truth; the operator-triggered sync must preserve that
        # exact ordering or this button can double cash/quantity accounting.
        await engine.settle_live_fills()
        return await engine.brokerage.sync()

    # ── 파일 ─────────────────────────────────────────────────────────────
    @property
    def hub(self) -> Hub:
        raise NotImplementedError

    @property
    def state_path(self) -> str:
        raise NotImplementedError

    def data_provider(self, config: StrategyConfig):
        """이 사용자의 키로 세운 시세 프로바이더."""
        return self.registry.data_provider(self.user.id, config)

    def run_config(self, agent_id: str = "") -> StrategyConfig | None:
        raise NotImplementedError

    def status(self) -> dict:
        raise NotImplementedError

    # ── 투자 성향 ────────────────────────────────────────────────────────
    def profile_store(self, agent_id: str = "") -> ProfileStore:
        raise NotImplementedError

    def save_profile(self, profile, agent_id: str = "") -> dict | None:
        raise NotImplementedError

    def clear_override(self, axis: str, agent_id: str = "") -> dict:
        store = self.profile_store(agent_id)
        profile = store.load()
        profile.overrides.pop(axis.upper(), None)
        store.save(profile)
        self.record("profile_override_cleared", axis.upper())
        return profile.to_dict()

    # ── 하루 한도 ────────────────────────────────────────────────────────
    def limits(self, agent_id: str = "") -> dict:
        raise NotImplementedError

    def save_limits(self, req: LimitsRequest, agent_id: str = "") -> dict:
        raise NotImplementedError

    # ── 자격증명 ─────────────────────────────────────────────────────────
    def setup(self) -> dict:
        raise NotImplementedError

    def save_setup(self, values: dict[str, str]) -> dict:
        raise NotImplementedError

    async def verify(self, venue_id: str) -> dict:
        raise NotImplementedError

    def disconnect(self, venue_id: str) -> dict:
        raise NotImplementedError

    # ── 실행 ─────────────────────────────────────────────────────────────
    def load_strategy(self, config_path: str) -> StrategyConfig:
        raise NotImplementedError

    async def start(self, req: StartRequest) -> dict:
        raise NotImplementedError

    async def start_group(self, req: GroupStartRequest) -> dict:
        raise NotImplementedError

    async def stop(self) -> dict:
        raise NotImplementedError

    def reconciliation_status(self, config_path: str) -> dict:
        raise NotImplementedError

    def archive_reconciliation(self,
                               req: ReconciliationArchiveRequest) -> dict:
        raise NotImplementedError

    def record(self, action: str, detail: str = "") -> None:
        """감사 기록. 값은 절대 남기지 않습니다 — 이름과 종목까지만."""


class UserDesk(Desk):
    """가입자 한 명의 데스크. 여기 있는 모든 것이 `user.id` 로 좁혀집니다.

    이 클래스의 어떤 메서드도 사용자 id 를 인자로 받지 않는다는 점이 중요합니다.
    id 는 세션에서 한 번 정해져 생성자로만 들어오므로, 요청 본문이나 경로에
    남의 id 를 적어 넣을 자리가 애초에 없습니다.
    """

    def __init__(self, user: User, state: AppState, accounts: Accounts,
                 registry: UserRegistry):
        self.user = user
        self.state = state
        self.accounts = accounts
        self.registry = registry

    # ── 봇 ───────────────────────────────────────────────────────────────
    def trader(self, agent_id: str = "") -> Any:
        return self.registry.trader(self.user.id, agent_id)

    def require_trader(self, agent_id: str = "") -> Any:
        return self.registry.require_trader(self.user.id, agent_id)

    def agents(self) -> list[str]:
        return self.registry.agent_ids(self.user.id)

    @property
    def hub(self) -> Hub:
        return self.state.hub_for(self.user.id)

    @property
    def state_path(self) -> str:
        return self.registry.state_path(self.user.id)

    def run_config(self, agent_id: str = "") -> StrategyConfig | None:
        trader = self.trader(agent_id)
        # 돌고 있으면 그 봇의 설정이 이 사람의 상태 DB 에 적힌 run 과 같은
        # 이름을 가집니다. 아니면 프로세스 기본 템플릿으로 물러섭니다.
        return trader.config if trader is not None else self.state.config

    def status(self) -> dict:
        return self.registry.status(self.user.id)

    # ── 투자 성향 ────────────────────────────────────────────────────────
    def profile_store(self, agent_id: str = "") -> ProfileStore:
        return self.registry.profile_store(self.user.id, agent_id)

    def save_profile(self, profile, agent_id: str = "") -> dict | None:
        return self.registry.save_profile(self.user.id, profile, agent_id)

    # ── 하루 한도 ────────────────────────────────────────────────────────
    def limits(self, agent_id: str = "") -> dict:
        # 그룹이 돌 때 `agent_id` 가 비면 **계좌 전체** 한도입니다. 그 자리에서
        # "실행 중 아님" 을 돌려주면, 지금 걸려 있는 계좌 한도를 화면이 보여줄
        # 방법이 없어집니다 — 그리고 그 한도야말로 실거래에서 마지막 방어선입니다.
        budget = self.registry._live_budget(self.user.id, agent_id)
        if budget is not None:
            return {**budget.status(),
                    "scope": "agent" if agent_id else (
                        "account" if self.registry.group(self.user.id) else "bot")}
        return {"running": False,
                "configured": self.registry.limits(self.user.id, agent_id)}

    def save_limits(self, req: LimitsRequest, agent_id: str = "") -> dict:
        out = self.registry.save_limits(
            self.user.id, req.model_dump(), agent_id)
        note = ("실행 중인 봇에는 즉시 적용됩니다" if out["applied_now"]
                else "다음 실행부터 적용됩니다")
        if out["removed"]:
            note += (f" — 주의: {', '.join(out['removed'])} 한도가 "
                     "해제되었습니다 (무제한)")
        return {**out, "note": note}

    # ── 자격증명 ─────────────────────────────────────────────────────────
    def setup(self) -> dict:
        configured = self.accounts.configured(self.user.id)
        linked = [v.id for v in VENUES
                  if all(env in configured
                         for env, _, needed in v.fields if needed)]
        return {
            "state": {
                "configured": bool(linked),
                "operator": self.user.display_name or self.user.email,
                "venues": linked,
                "has_llm": any(name in configured for name in
                               ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY")),
                "has_notifier": ("TELEGRAM_BOT_TOKEN" in configured
                                 and "TELEGRAM_CHAT_ID" in configured),
                "updated_at": "",
            },
            "venues": venue_catalog(),
            "operator_fields": [{"env": e, "label": label, "required": required}
                                for e, label, required in ACCOUNT_OPERATOR_FIELDS],
            # 이름과 마지막 4자리뿐입니다. 값은 어떤 경로로도 돌아가지 않습니다.
            "configured": configured,
        }

    def save_setup(self, values: dict[str, str]) -> dict:
        written: list[str] = []
        rejected: dict[str, str] = {}
        for raw_key, raw_value in values.items():
            key = (raw_key or "").strip()
            reason = account_rejection_reason(key)
            if reason:
                rejected[key or "(빈 키)"] = reason
                log.warning("설정 저장 거부: user=%s key=%r — %s",
                            self.user.id, key, reason)
                continue
            value = (raw_value or "").strip()
            if not value:
                # 빈 칸은 "이미 저장된 것을 그대로 두라"는 뜻입니다. 설정 화면이
                # 비밀 칸을 비운 채 폼을 제출할 수 있어야 하니까요.
                continue
            if "\n" in value or "\r" in value:
                rejected[key] = "값에 줄바꿈이 있어 저장할 수 없습니다"
                log.warning("설정 저장 거부: user=%s key=%r — 줄바꿈", self.user.id, key)
                continue
            self.accounts.put_secret(self.user.id, key, value)
            written.append(key)
        return {"written": written, "rejected": rejected, **self.setup()}

    async def verify(self, venue_id: str) -> dict:
        spec = VENUES_BY_ID[venue_id]
        wanted = {env for env, _, _ in spec.fields}
        # 복호화된 값이 사는 유일한 구간입니다. 이 함수 밖으로 나가지 않습니다.
        mine = {k: v for k, v in self.accounts.secrets_for(self.user.id).items()
                if k in wanted}
        result = await verify_venue(venue_id, mine)
        if not result.get("ok"):
            # 실패했을 때만 알아봅니다. 잘 되는 사람에게 굳이 외부 조회를
            # 붙일 이유가 없습니다.
            ip = await _public_ip()
            if ip:
                result["server_ip"] = ip
        self.record("credentials_verified" if result.get("ok")
                    else "credentials_verify_failed", venue_id)
        return result

    def disconnect(self, venue_id: str) -> dict:
        spec = VENUES_BY_ID[venue_id]
        configured = self.accounts.configured(self.user.id)
        removed = [env for env, _, _ in spec.fields if env in configured]
        for name in removed:
            self.accounts.drop_secret(self.user.id, name)
        return {"disconnected": venue_id, "removed": removed}

    # ── 실행 ─────────────────────────────────────────────────────────────
    def load_strategy(self, config_path: str) -> StrategyConfig:
        path = resolve_template(config_path)
        try:
            return load_config(str(path))
        except Exception as exc:
            raise HTTPException(400, f"설정을 불러오지 못했습니다: {exc}") from exc

    async def start(self, req: StartRequest) -> dict:
        cfg = self.load_strategy(req.config_path)
        mode = RunMode(req.mode)
        if mode is RunMode.LIVE:
            # 사람마다 다시 겁니다 — 남이 확인했다고 내 실거래가 열리지 않습니다.
            assert_live_start_allowed(cfg, req.confirm)
        cfg = _with_mode(cfg, mode)
        try:
            return await self.registry.start(self.user.id, cfg,
                                             on_event=self.hub.publish)
        except LLMError as exc:
            # 봇을 세우다 LLM 클라이언트에서 죽었습니다. 그대로 500 을 내면
            # 화면에는 "서버 오류 — 로그를 확인하세요" 만 뜨고, 사용자는 무엇을
            # 해야 하는지 알 방법이 없습니다. 실제로 이것 때문에 "왜 안 되는지
            # 모르겠다" 가 반복됐습니다.
            log.warning("봇 시작 실패(LLM): %s", exc)
            has_desk = any(m.type in ("desk", "council") for m in cfg.alpha)
            raise HTTPException(
                503,
                f"'{cfg.name}' 은 AI 데스크를 쓰는 전략인데 쓸 수 있는 LLM 키가 "
                f"없습니다. 마이페이지에서 본인 Gemini 키를 넣거나, 데스크가 없는 "
                f"전략을 고르세요."
                if has_desk else f"모델을 준비하지 못했습니다: {exc}") from None

    async def start_group(self, req: GroupStartRequest) -> dict:
        """에이전트 여럿을 한 계좌에 띄운다.

        실거래 확인은 **에이전트마다** 받습니다. 하나를 확인했다고 나머지가
        열리면, 사용자는 관찰용으로 넣은 에이전트가 진짜 주문을 내고 있다는
        사실을 모른 채로 하루를 보냅니다.
        """
        from quant.live.agents import AgentConfigError, AgentGroup

        configs: dict = {}
        rows: list[dict] = []
        for spec in req.agents:
            cfg = self.load_strategy(spec.config_path)
            mode = RunMode(spec.mode)
            if mode is RunMode.LIVE:
                assert_live_start_allowed(cfg, spec.confirm)
            configs[spec.agent_id] = _with_mode(cfg, mode)
            rows.append(spec.model_dump())

        try:
            group = AgentGroup.from_dicts(rows)
        except AgentConfigError as exc:
            raise HTTPException(400, str(exc)) from None

        try:
            return await self.registry.start_group(
                self.user.id, group, configs, on_event=self.hub.publish)
        except LLMError as exc:
            log.warning("그룹 시작 실패(LLM): %s", exc)
            raise HTTPException(
                503,
                "AI 데스크를 쓰는 전략인데 쓸 수 있는 LLM 키가 없습니다. "
                "마이페이지에서 본인 Gemini 키를 넣거나, 데스크가 없는 전략을 "
                f"고르세요. ({exc})") from None

    async def stop(self) -> dict:
        return await self.registry.stop(self.user.id)

    def reconciliation_status(self, config_path: str) -> dict:
        cfg = self.load_strategy(config_path)
        return self.registry.reconciliation_status(self.user.id, cfg)

    def archive_reconciliation(self,
                               req: ReconciliationArchiveRequest) -> dict:
        cfg = self.load_strategy(req.config_path)
        return self.registry.archive_reconciliation(
            self.user.id,
            cfg,
            run_id=req.run_id,
            reason=req.reason,
            confirmations=req.confirmations.model_dump(),
            acknowledgement=req.acknowledgement,
        )

    def record(self, action: str, detail: str = "") -> None:
        self.accounts.record(self.user.id, action, detail)


def _resolve(trader, ticker: str):
    ctx = trader.engine.ctx
    wanted = ticker.strip().upper()
    for sym in ctx.universe:
        if sym.ticker.upper() == wanted:
            return sym
    for pos in ctx.portfolio.open_positions:
        if pos.symbol.ticker.upper() == wanted:
            return pos.symbol
    raise HTTPException(404, f"유니버스에도 보유에도 없는 종목: {ticker}")


def create_app(config: StrategyConfig | None = None,
               state_path: str = "quant_state.db") -> FastAPI:
    # The setup screen writes credentials to .env; load them before anything
    # reads os.environ, or a fully configured operator still sees "key required".
    load_env_file(os.environ.get("QUANT_ENV_FILE", ".env"))

    state = AppState(config, state_path)
    secret = os.environ.get("QUANT_SECRET_KEY", "").strip()

    accounts: Accounts | None = None
    registry: UserRegistry | None = None
    if secret:
        accounts = Accounts(users_db_path(state_path), secret)
        registry = UserRegistry(accounts, root=user_data_root(state_path))
        # 1인용 시절의 `.env` 나 배포 환경변수에 증권사 키가 남아 있을 수
        # 있습니다. 프로세스 환경은 전역이라, 여러 사람이 도는 프로세스에서
        # 그 한 줄은 "운영자의 계좌로 아무나 주문을 낼 수 있다" 와 같은
        # 말입니다. 사용자 봇은 자격증명을 언제나 인자로 받으므로 이 값들은
        # 이제 아무도 필요로 하지 않습니다 — 내려놓고 이름만 남깁니다.
        stale = sorted(name for name in _NEVER_PROCESS_WIDE if os.environ.get(name))
        for name in stale:
            os.environ.pop(name, None)
        if stale:
            log.warning(
                "프로세스 환경에 있던 계정용 자격증명 %d건을 내렸습니다 (%s) — "
                "여러 사람이 쓰는 배포에서는 각자 설정 화면에서 등록합니다",
                len(stale), ", ".join(stale))

    #: 계정 DB 에 사람이 있는데 암호화 키가 없는 상태. 1인용으로 되돌아가면
    #: 공유 토큰 하나가 다시 전부를 여는 자리라, 그 길을 아예 닫습니다.
    orphaned_accounts = (registry is None
                         and registered_accounts(users_db_path(state_path)) > 0)

    def _desk(scope) -> Desk:
        """이 요청이 앉을 데스크. 인증이 일어나는 유일한 자리입니다.

        가입자가 있는 배포에서 사람을 정하는 것은 세션 쿠키뿐입니다.
        공유 토큰은 존재하지 않습니다. 값 하나가 관리자 자리를 열면 그것은
        로그인이 아니라 로그인의 우회이고, 그 값은 주소창과 프록시 접근 로그를
        타고 서비스 바깥으로 흐릅니다. 브라우저 WebSocket 도 쿠키로 인증하므로
        `?token=` 이 필요한 자리는 남아 있지 않습니다.
        """
        if registry is None or accounts is None:
            # 여기 오면 앱이 잘못 조립된 것입니다. `create_app` 이 이미
            # QUANT_SECRET_KEY 를 요구하므로 정상 경로에서는 도달하지 않습니다.
            raise HTTPException(
                503, "서비스가 준비되지 않았습니다 — QUANT_SECRET_KEY 를 설정하고 "
                     "다시 시작하세요.")
        # 쿠키 이름은 https 여부에 따라 달라집니다 (`__Host-` 는 Secure 없이는
        # 브라우저가 저장하지 않습니다). 인증하는 쪽이 그 규칙을 알고 있어야
        # 로컬 http 개발에서 로그인이 조용히 실패하지 않습니다.
        user = accounts.user_for_session(auth.session_token(scope))
        if user is None:
            raise HTTPException(
                401, "로그인이 필요합니다 — 계정이 없으면 먼저 가입하세요.")
        return UserDesk(user, state, accounts, registry)

    async def desk(request: Request) -> Desk:
        return _desk(request)

    async def maybe_desk(request: Request) -> Desk | None:
        """인증되지 않았으면 None. `/api/health` 처럼 막으면 안 되는 곳에서만."""
        try:
            return _desk(request)
        except HTTPException:
            return None

    async def require_admin(request: Request) -> Desk:
        seat = _desk(request)
        if seat.user is not None and not seat.user.is_admin:
            raise HTTPException(403, "관리자만 접근할 수 있습니다")
        return seat

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if orphaned_accounts:
            log.error(
                "계정 DB 에 가입자가 있는데 QUANT_SECRET_KEY 가 없습니다 — "
                "이 프로세스는 아무 요청도 받지 않습니다. 키를 설정하고 다시 "
                "시작하세요 (키를 잃어버렸다면 저장된 자격증명은 되살릴 수 "
                "없고, 가입자들이 키를 다시 등록해야 합니다)."
            )
        elif registry is None:
            # 계정 없이 조립된 앱은 어떤 요청도 받지 못합니다 (`_desk` 가 503).
            # `quant serve` 는 이 상태로 뜨지 않으니, 여기 오는 것은 임베딩한
            # 코드가 QUANT_SECRET_KEY 없이 create_app 을 부른 경우뿐입니다.
            log.error(
                "QUANT_SECRET_KEY is not set — no accounts can exist, so this "
                "app will refuse every request. Set it and restart."
            )
        else:
            accounts.purge_expired()
        yield
        if registry is not None:
            # 프로세스가 죽기 전에 모든 봇이 마지막 상태를 적게 합니다.
            await registry.shutdown()
        if state.trader is not None:
            state.trader.running = False
        if state.trader_task is not None:
            state.trader_task.cancel()
        if accounts is not None:
            accounts.close()

    app = FastAPI(title="Quant Engine", version="1.0.0", lifespan=lifespan,
                  default_response_class=SafeJSONResponse)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        # No cross-origin caller by default, token or not. The dashboard is
        # served by this same app, so it never needs a CORS header; the only
        # thing "*" bought was letting any page the operator happens to visit
        # POST /api/manual/close_all or /api/setup at their own localhost.
        # Anyone genuinely serving the UI from elsewhere names it explicitly.
        # 세션 쿠키가 생긴 뒤로는 더 강한 이유가 하나 더 있습니다: SameSite=Lax
        # 가 CSRF 방어인데, 출처를 되비추는 관대한 CORS 정책은 그 방어가 지키던
        # 것을 그대로 돌려줍니다.
        allow_origins=[o for o in os.environ.get("CORS_ORIGINS", "").split(",") if o],
        allow_methods=["*"], allow_headers=["*"],
    )
    @app.middleware("http")
    async def bound_request_body(request: Request, call_next):
        """본문 크기 상한. 이 API 가 받는 가장 큰 것도 설정 하나(수십 KB)입니다.

        값 하나의 상한은 `SetupRequest` 가 이미 걸지만, 그건 본문을 다 읽고
        JSON 으로 세운 뒤의 이야기입니다. 4 MiB 를 스무 번 보내는 쪽에서 보면
        거절당하는 것과 거절당하기까지 서버가 그것을 전부 들고 있는 것은 다른
        일입니다.
        """
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": f"요청 본문이 너무 큽니다 "
                                   f"({MAX_BODY_BYTES // 1024} KiB 이하로 보내세요)"})
        response = await call_next(request)
        # Every API response can contain identity-, strategy-, or account-bound
        # state. Never let a browser, CDN, or shared proxy replay it after logout
        # or account switching. The bounded process-local coalescers are the only
        # caches and are partitioned by user.
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "private, no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
            vary = {part.strip() for part in response.headers.get("Vary", "").split(",")
                    if part.strip()}
            vary.add("Cookie")
            response.headers["Vary"] = ", ".join(sorted(vary))
        return response

    app.state.quant = state
    app.state.accounts = accounts
    app.state.registry = registry

    auth = build_auth(accounts) if accounts is not None else None
    if auth is not None:
        app.include_router(auth.router)

    # ── read-only ────────────────────────────────────────────────────────
    @app.get("/api/health")
    async def health(seat: Desk | None = Depends(maybe_desk)):
        running = bool(seat is not None and seat.running())
        out = {
            "ok": True,
            "version": "1.0.0",
            # 코드를 고쳐도 파이썬은 재시작 전까지 옛 모듈을 씁니다. 화면에
            # 이 시각이 없으면 "고쳤는데 왜 그대로냐" 를 알아낼 방법이
            # 없습니다 — 실제로 그것 때문에 몇 시간을 잃었습니다.
            "started_at": state.started_at.isoformat(),
            "uptime_s": round((datetime.now(UTC) - state.started_at).total_seconds(), 1),
            # 자기 봇만 봅니다. 로그인하지 않았으면 볼 봇이 없습니다.
            "trader_running": running,
            "authenticated": registry is not None,
            "multiuser": registry is not None,
        }
        # 봇이 **죽었으면** 왜 죽었는지 함께 보냅니다. 이게 없으면 화면은
        # "시작됨" 을 띄운 뒤 조용히 정지 상태로 돌아가고, 사용자는 봇이 돌고
        # 있다고 믿은 채로 기다립니다. 실제로 그랬습니다 — 워밍업에서 시세를
        # 못 받아 죽은 봇이 화면에서는 그냥 "오프라인" 이었습니다.
        if seat is not None and not running:
            try:
                st = seat.status()
            except Exception:                    # noqa: BLE001 — 상태 조회 실패로
                st = {}                          # health 가 죽으면 안 됩니다
            if st.get("error") or st.get("stopped_at"):
                out["last_error"] = st.get("error")
                out["last_strategy"] = st.get("strategy")
                out["stopped_at"] = st.get("stopped_at")
        return out

    @app.get("/api/config")
    async def get_config(seat: Desk = Depends(desk)):
        """이 프로세스가 들고 있는 전략 설정. 자격증명은 값도 신원도 나가지 않습니다."""
        cfg = state.config
        if cfg is None:
            raise HTTPException(404, "no config loaded")
        fields = _credential_fields(cfg)
        if registry is not None and seat.user is not None:
            # 이 사람의 성향과 한도가 반영된 사본입니다 — `prepare()` 는 키를
            # 넣지 않는 쪽입니다.
            body = json.loads(registry.prepare(seat.user.id, cfg).model_dump_json())
            # 남는 것은 프로세스 템플릿에 적힌 **운영자의** 배선값입니다. 가입한
            # 사람에게는 쓸모가 없고(자기 봇은 자기 키로 섭니다), 거기에는
            # client_id 와 account_no 처럼 이름에 secret 이 들어가지 않는
            # 신원값이 그대로 있습니다. 가리는 것이 아니라 비웁니다.
            return _redact(_without_wiring_params(body), fields)
        return _redact(json.loads(cfg.model_dump_json()), fields)

    @app.get("/api/status")
    async def status(seat: Desk = Depends(desk)):
        return _named_status(seat.status(), NameBook(seat.state_path))

    # 조회 한도는 핸들러가 아니라 여기서 막습니다. `limit=99999999999999999999`
    # 하나면 sqlite 안에서 터져 500 이 되고, 그건 로그인한 아무나 누를 수 있는
    # 크래시 경로였습니다. FastAPI 가 요청을 받기 전에 422 로 끝냅니다.
    @app.get("/api/equity")
    async def equity(limit: int = Query(2000, ge=1, le=10_000),
                     strategy: str | None = Query(None, max_length=64),
                     mode: str | None = Query(
                         None, pattern="^(backtest|dry_run|live)$",
                     ),
                     agent_id: str = "",
                     seat: Desk = Depends(desk)):
        store = StateStore(seat.state_path)
        try:
            # A stopped desk falls back to the process template from
            # ``run_config()``.  The chart must instead follow the strategy the
            # person selected, or a live Toss screen can quietly draw the demo
            # backtest curve underneath it.
            cfg = _selected_read_config(seat, strategy, agent_id)
            selected_mode = mode or (cfg.mode.value if cfg else None)
            if cfg and selected_mode:
                _select_run_readonly(store, cfg.name, selected_mode, agent_id)
            return {
                "points": store.equity_curve(limit),
                "agent_id": agent_id or None,
                "strategy": cfg.name if cfg else None,
                "mode": selected_mode,
            }
        finally:
            store.close()

    @app.post("/api/account/redeem")
    async def redeem(req: RedeemRequest, seat: Desk = Depends(desk)):
        """추천 코드로 요금제를 올립니다."""
        user = getattr(seat, "user", None)
        if user is None:
            raise HTTPException(400, "계정이 필요합니다")
        try:
            plan = accounts.redeem(user.id, req.code)
        except AccountError as exc:
            raise HTTPException(400, str(exc)) from None
        from quant.webapp.usage import plan_for

        p = plan_for(plan)
        return {"plan": p.id, "label": p.label_ko,
                "daily_deliberations": p.daily_deliberations or None,
                "monthly_cost_usd": p.monthly_cost_usd or None,
                "note": f"{p.label_ko} 요금제로 변경되었습니다"}

    @app.get("/api/account/plan")
    async def account_plan(seat: Desk = Depends(desk)):
        user = getattr(seat, "user", None)
        if user is None:
            raise HTTPException(400, "계정이 필요합니다")
        from quant.webapp.usage import plan_for

        p = plan_for(user.plan)
        return {"plan": p.id, "label": p.label_ko,
                "daily_deliberations": p.daily_deliberations or None,
                "monthly_cost_usd": p.monthly_cost_usd or None}

    @app.get("/api/candles")
    async def candles(ticker: str = Query(..., min_length=1, max_length=32),
                      timeframe: str = Query("1d", max_length=8),
                      count: int = Query(120, ge=20, le=500),
                      strategy: str | None = Query(None, max_length=64),
                      seat: Desk = Depends(desk)):
        """봉 + 현재가 + 내 자리 + 미체결 + 체결 — 한 번에.

        화면 오른쪽이 답해야 하는 질문은 셋입니다: 지금 얼마인가, 나는 얼마에
        들어갔는가, 봇이 방금 무엇을 했는가. 세 번 나눠 부르면 서로 다른 순간의
        답이 한 화면에 섞이므로 한 응답으로 묶습니다.
        """
        # 돌고 있는 봇의 설정을 먼저 쓰고, 없으면 화면이 고른 전략을 씁니다.
        # 봇이 꺼져 있을 때 시세를 보는 것이 이 화면의 절반이라, 봇을 켜야만
        # 호가를 볼 수 있으면 순서가 거꾸로입니다.
        #
        # `run_config()` 는 봇이 없으면 프로세스 기본 템플릿으로 물러섭니다.
        # 그래서 `if cfg is None:` 안에 둔 strategy 처리는 **한 번도 실행되지
        # 않았고**, 차트는 언제나 그 템플릿의 종목만 알았습니다 — 다른 전략을
        # 고르면 그 전략의 종목이 "이 전략의 종목이 아닙니다" 로 404 였습니다.
        cfg = _selected_read_config(seat, strategy)
        if cfg is None:
            raise HTTPException(
                400, "전략을 고르세요 — 어느 거래소에서 조회할지 알 수 없습니다.")

        symbol = next((s for s in _config_symbols(cfg)
                       if s.ticker.upper() == ticker.strip().upper()), None)
        if symbol is None:
            raise HTTPException(
                404, f"{ticker} 는 이 전략의 종목이 아닙니다.")

        bars, quote, stale, provider = [], None, True, None
        try:
            provider = seat.data_provider(cfg)
            bars = await provider.latest_bars(symbol, timeframe, count)
            quote = await provider.quote(symbol)
            validated_quote, quote_problem = _validated_l1_quote(
                quote, symbol, datetime.now(UTC),
            )
            if validated_quote is None:
                if quote_problem:
                    log.warning("시세 호가 검증 실패 %s: %s", symbol.key, quote_problem)
                quote = None
            stale = False
        except Exception as exc:  # 시세가 없다고 화면 전체가 죽으면 안 됩니다.
            log.warning("시세 조회 실패 %s: %s", symbol.key, exc)

        store = None
        try:
            store = StateStore(seat.state_path)
            _select_run_readonly(store, cfg.name, cfg.mode.value)
            since = bars[0].end_ts.isoformat() if bars else ""
            fills = store.fills_for(symbol.key, since=since)
            position = store.position_for(symbol.key)
            # 지금 보고 있는 종목 하나입니다. 조회 기록에도 정적 표에도 없으면
            # 증권사에 물어보고, 찾으면 적어 둡니다 — 다음부터는 조회 없이.
            book = NameBook(seat.state_path, store=store)
            ticker_name = await book.resolve(symbol.ticker, provider)
        finally:
            if store is not None:
                store.close()
            if provider is not None:
                with contextlib.suppress(Exception):
                    await provider.close()

        last = quote.mid if quote and quote.mid else (bars[-1].close if bars else 0.0)
        if position and position["avg_price"] > 0 and last > 0:
            direction = 1.0 if position["quantity"] > 0 else -1.0
            position["unrealized_pct"] = direction * (
                last / position["avg_price"] - 1.0)

        trader = seat.trader()
        orders = []
        if trader is not None:
            for o in getattr(trader.engine, "orders", []) or []:
                if o.symbol.key == symbol.key and o.status.is_open:
                    orders.append({"side": o.side.value, "price": o.limit_price or 0.0,
                                   "quantity": float(o.quantity), "status": o.status.value})

        return {
            "ticker": symbol.ticker,
            # 코드만 띄우면 지금 무엇을 보고 있는지 코드를 외운 사람만 압니다.
            "ticker_name": ticker_name,
            "timeframe": timeframe,
            "currency": symbol.quote_currency,
            "tick_size": float(symbol.tick_size or 0),
            "bars": [{"t": b.end_ts.isoformat(), "o": b.open, "h": b.high,
                      "l": b.low, "c": b.close, "v": b.volume} for b in bars],
            "quote": ({"price": last, "ts": quote.ts.isoformat(),
                       "price_kind": "midpoint"} if quote
                      else ({"price": last, "ts": bars[-1].end_ts.isoformat(),
                             "price_kind": "bar_close"}
                            if bars else None)),
            "position": position,
            "orders": orders,
            "fills": fills,
            "stale": stale,
        }

    @app.get("/api/market/snapshot")
    async def market_snapshot(
        ticker: str = Query(..., min_length=1, max_length=32),
        strategy: str | None = Query(None, max_length=64),
        depth: int = Query(10, ge=1, le=20),
        trade_count: int = Query(20, ge=0, le=50),
        seat: Desk = Depends(desk),
    ):
        """Near-real-time read-only quote, depth, and recent venue trades.

        This endpoint never constructs a brokerage and cannot place, modify, or
        cancel an order.  Unsupported fields remain null with capability flags;
        a previous-close change or market session is never inferred from price.
        """
        cfg = _selected_read_config(seat, strategy)
        if cfg is None:
            raise HTTPException(
                400, "전략을 고르세요 — 어느 거래소에서 조회할지 알 수 없습니다.")
        symbol = next((s for s in _config_symbols(cfg)
                       if s.ticker.upper() == ticker.strip().upper()), None)
        if symbol is None:
            raise HTTPException(404, f"{ticker} 는 이 전략의 종목이 아닙니다.")

        user_scope = seat.user.id if seat.user is not None else 0
        cache_key = (
            "market", user_scope, cfg.name, symbol.key, depth, trade_count,
        )

        async def load_market() -> dict:
            provider = None
            try:
                provider = seat.data_provider(cfg)
                reader = provider
                # CachingProvider intentionally exposes only the generic data
                # contract.  Its inner Toss provider owns the richer, still
                # read-only snapshot contract.
                seen: set[int] = set()
                while getattr(reader, "inner", None) is not None:
                    if id(reader) in seen:
                        break
                    seen.add(id(reader))
                    reader = reader.inner
                richer = getattr(reader, "market_snapshot", None)
                if richer is not None:
                    body = await richer(
                        symbol, depth=depth, trade_count=trade_count,
                    )
                else:
                    quote = await provider.quote(symbol)
                    received_at = datetime.now(UTC)
                    validated_quote, invalid_reason = _validated_l1_quote(
                        quote, symbol, received_at,
                    )
                    age_ms = None
                    price = bid = ask = bid_size = ask_size = None
                    quote_ts = None
                    if validated_quote is not None:
                        price = validated_quote["price"]
                        bid = validated_quote["bid"]
                        ask = validated_quote["ask"]
                        bid_size = validated_quote["bid_size"]
                        ask_size = validated_quote["ask_size"]
                        quote_ts = validated_quote["ts"]
                        age_ms = validated_quote["age_ms"]
                    if quote is None:
                        freshness_status = "unavailable"
                        freshness_message = "현재 시세를 불러오지 못했습니다"
                        poll_after_ms = 10_000
                    elif invalid_reason:
                        freshness_status = "unknown"
                        freshness_message = invalid_reason
                        poll_after_ms = 5_000
                    elif age_ms is not None and age_ms <= 5_000:
                        freshness_status = "fresh"
                        freshness_message = "현재 호가를 표시합니다"
                        poll_after_ms = 2_500
                    elif age_ms is not None and age_ms <= 60_000:
                        freshness_status = "delayed"
                        freshness_message = "호가가 잠시 늦게 도착하고 있습니다"
                        poll_after_ms = 5_000
                    else:
                        freshness_status = "stale"
                        freshness_message = "마지막 호가가 오래되었습니다"
                        poll_after_ms = 10_000
                    body = {
                        "ticker": symbol.ticker,
                        "currency": symbol.quote_currency,
                        "quote": {
                            "price": price,
                            "price_kind": "midpoint" if price is not None else None,
                            "bid": bid,
                            "ask": ask,
                            "bid_quantity": bid_size,
                            "ask_quantity": ask_size,
                            "change": None,
                            "change_pct": None,
                            "ts": quote_ts.isoformat() if quote_ts is not None else None,
                            "source": (getattr(provider, "name", None)
                                       if price is not None else None),
                        },
                        "market": {
                            "state": None,
                            "session_label": "시장 상태 미조회",
                        },
                        "freshness": {
                            "status": freshness_status,
                            "age_ms": age_ms,
                            "poll_after_ms": poll_after_ms,
                            "message": freshness_message,
                            "components": {
                                "quote": {
                                    "status": freshness_status,
                                    "age_ms": age_ms,
                                    "ts": (quote_ts.isoformat()
                                           if quote_ts is not None else None),
                                    "affects_overall": True,
                                },
                                "depth": {"status": "unavailable",
                                          "age_ms": None, "ts": None,
                                          "affects_overall": False},
                                "trades": {"status": "unavailable",
                                           "age_ms": None, "ts": None,
                                           "affects_overall": False},
                            },
                        },
                        "capabilities": {
                            "rest_polling": True,
                            "top_of_book": bid is not None and ask is not None,
                            "depth": False,
                            "depth_available": False,
                            "recent_trades": False,
                            "recent_trades_available": False,
                            "websocket_available": False,
                            "websocket_active": False,
                            "market_session": False,
                        },
                        "depth": None,
                        "recent_trades": [],
                    }
                body = dict(body)
                body["ticker_name"] = await NameBook(seat.state_path).resolve(
                    symbol.ticker, provider,
                )
                return body
            except Exception as exc:  # noqa: BLE001 — keep the account panel up
                log.warning("시장 스냅샷 조회 실패 %s: %s", symbol.key, exc)
                return {
                    "ticker": symbol.ticker,
                    "ticker_name": NameBook(seat.state_path).name(symbol.ticker),
                    "currency": symbol.quote_currency,
                    "quote": {
                        "price": None, "bid": None, "ask": None,
                        "bid_quantity": None, "ask_quantity": None,
                        "change": None, "change_pct": None,
                        "ts": None, "source": None, "price_kind": None,
                    },
                    "market": {
                        "state": None,
                        "session_label": "시장 상태 미조회",
                    },
                    "freshness": {
                        "status": "unavailable", "age_ms": None,
                        "poll_after_ms": 10_000,
                        "message": "현재 시세를 불러오지 못했습니다",
                        "components": {
                            "quote": {"status": "unavailable",
                                      "age_ms": None, "ts": None,
                                      "affects_overall": False},
                            "depth": {"status": "unavailable",
                                      "age_ms": None, "ts": None,
                                      "affects_overall": False},
                            "trades": {"status": "unavailable",
                                       "age_ms": None, "ts": None,
                                       "affects_overall": False},
                        },
                    },
                    "capabilities": {
                        "rest_polling": True, "top_of_book": False,
                        "depth": False, "depth_available": False,
                        "recent_trades": False,
                        "recent_trades_available": False,
                        "websocket_available": False,
                        "websocket_active": False,
                        "market_session": False,
                    },
                    "depth": None,
                    "recent_trades": [],
                }
            finally:
                if provider is not None:
                    with contextlib.suppress(Exception):
                        await provider.close()

        try:
            market = await state.market_reads.get(cache_key, load_market)
        except ReadBusy:
            raise HTTPException(
                503, "시세 조회가 잠시 밀려 있습니다 — 곧 다시 시도하세요",
                headers={"Retry-After": "1"},
            ) from None

        store = StateStore(seat.state_path)
        try:
            _select_run_readonly(store, cfg.name, cfg.mode.value)
            position = store.position_for(symbol.key)
        finally:
            store.close()
        quote_price = (market.get("quote") or {}).get("price")
        if (position and position["avg_price"] > 0
                and isinstance(quote_price, (int, float)) and quote_price > 0):
            direction = 1.0 if position["quantity"] > 0 else -1.0
            position["unrealized_pct"] = direction * (
                quote_price / position["avg_price"] - 1.0)

        trader = seat.trader()
        orders = []
        if trader is not None:
            for order in getattr(trader.engine, "orders", []) or []:
                if order.symbol.key == symbol.key and order.status.is_open:
                    orders.append({
                        "side": order.side.value,
                        "price": order.limit_price or 0.0,
                        "quantity": float(order.quantity),
                        "status": order.status.value,
                    })
        capabilities = dict(market.get("capabilities") or {})
        capabilities.update({
            # These two blocks describe this bot's local ledger, not every
            # order/holding visible in the Toss app. Keep the boundary machine-
            # readable so the UI cannot accidentally relabel it as account truth.
            "orders_source": "running_engine" if trader is not None else "unavailable",
            "orders_complete": False,
            "position_source": "latest_bot_ledger",
            "position_authoritative": False,
        })
        return {
            **market,
            "capabilities": capabilities,
            "tick_size": float(symbol.tick_size or 0),
            "orders": orders,
            "position": position,
        }

    @app.get("/api/trades")
    async def trades(limit: int = Query(100, ge=1, le=2_000),
                     strategy: str | None = Query(None, max_length=64),
                     mode: str | None = Query(
                         None, pattern="^(backtest|dry_run|live)$",
                     ),
                     agent_id: str = "",
                     seat: Desk = Depends(desk)):
        store = StateStore(seat.state_path)
        try:
            cfg = _selected_read_config(seat, strategy, agent_id)
            selected_mode = mode or (cfg.mode.value if cfg else None)
            if cfg and selected_mode:
                _select_run_readonly(store, cfg.name, selected_mode, agent_id)
            book = NameBook(seat.state_path, store=store)
            return {
                "trades": book.tag(store.recent_trades(limit), "symbol"),
                "strategy": cfg.name if cfg else None,
                "mode": selected_mode,
            }
        finally:
            store.close()

    @app.get("/api/pnl")
    async def pnl(strategy: str | None = Query(None, max_length=80),
                  mode: str | None = Query(None, max_length=16),
                  agent_id: str = "",
                  seat: Desk = Depends(desk)):
        """오늘·이번주·이번달·올해 실현손익.

        run 을 가로질러 셉니다. 봇을 껐다 켤 때마다 "이번 달 수익" 이 0 으로
        돌아가면 그건 수익이 아니라 실행 시간을 재는 숫자입니다.
        """
        store = StateStore(seat.state_path)
        try:
            return {"periods": store.pnl_by_period(strategy=strategy, mode=mode,
                                                   agent_id=agent_id or None),
                    # 모의로 번 돈은 실제로 번 돈이 아닙니다. 화면이 그 둘을
                    # 나눠 보여줄 수 있게 어느 모드에 거래가 있는지 알려줍니다.
                    "modes": store.modes_with_trades()}
        finally:
            store.close()

    @app.get("/api/tradelog")
    async def tradelog(limit: int = Query(100, ge=1, le=500),
                       offset: int = Query(0, ge=0),
                       strategy: str | None = Query(None, max_length=80),
                       mode: str | None = Query(None, max_length=16),
                       agent_id: str = "",
                       seat: Desk = Depends(desk)):
        """매매 기록 — 지금 돌고 있는 run 만이 아니라 전부.

        `/api/trades` 는 현재 run 만 봅니다. 어제 껐다 켰으면 어제 거래가
        사라지는데, "내가 뭘 사고팔았나" 는 재시작과 무관한 질문입니다.
        """
        store = StateStore(seat.state_path)
        try:
            payload = store.trade_log(limit=limit, offset=offset,
                                      strategy=strategy, mode=mode,
                                      agent_id=agent_id or None)
            book = NameBook(seat.state_path, store=store)
            # 기록은 나중에 다시 읽는 것입니다. 반년 전 줄에 코드만 남아 있으면
            # 그때 내가 무엇을 사고팔았는지 알아볼 수 없습니다.
            payload["trades"] = book.tag(payload.get("trades"), "symbol")
            return payload
        finally:
            store.close()

    @app.get("/api/lookup")
    async def lookup(q: str = Query("", max_length=40),
                     strategy: str | None = Query(None, max_length=64),
                     seat: Desk = Depends(desk)):
        """종목 검색 — 코드로 조회하고, 한 번 찾은 것은 이름으로도 찾힙니다.

        코드를 넣으면 연동된 증권사에 물어봅니다. 이름을 넣으면 **이미 아는
        종목** 안에서 찾습니다: 전략에 들어 있는 종목과, 이 계정이 전에 조회해
        본 종목입니다.

        전체 상장 종목을 이름으로 검색하려면 종목 마스터가 필요하고, 그건
        증권사마다 다른 별도 파일입니다. 없는 목록을 지어내는 것보다 — 잘못된
        종목코드는 **다른 회사를 사는 것** 입니다 — 아는 것만 정확히 찾아주고
        모르는 것은 코드로 물어보게 하는 편이 낫습니다.
        """
        text = (q or "").strip()
        if not text:
            return {"results": [], "message": ""}

        cfg = _selected_read_config(seat, strategy)
        book = NameBook(seat.state_path)
        known: dict[str, dict] = {}
        if cfg is not None:
            for sym in _config_symbols(cfg):
                # 전략에 박혀 있는 종목은 조회해 본 적이 없어도 이름이 떠야
                # 합니다 — 화면이 처음 열렸을 때 보이는 것이 이 목록입니다.
                known[sym.ticker] = {"ticker": sym.ticker, "venue": sym.venue,
                                     "currency": sym.quote_currency,
                                     "name": book.name(sym.ticker),
                                     "source": "strategy"}
        store = StateStore(seat.state_path)
        try:
            for row in store.known_tickers():
                known.setdefault(row["ticker"], {})
                known[row["ticker"]].update(
                    {"ticker": row["ticker"], "venue": row["venue"],
                     "currency": row.get("currency") or "",
                     "name": row.get("name") or book.name(row["ticker"]),
                     "source": "seen"})
        finally:
            store.close()

        digits = "".join(ch for ch in text if ch.isdigit())
        # 6자리 숫자는 국내 종목코드입니다. 증권사에 직접 물어봅니다.
        if len(digits) == 6 and cfg is not None:
            provider = None
            try:
                provider = seat.data_provider(cfg)
                found = await provider.describe(digits)
            except Exception as exc:
                log.warning("종목 조회 실패 %s: %s", digits, exc)
                found = None
            finally:
                if provider is not None:
                    with contextlib.suppress(Exception):
                        await provider.close()
            if found:
                # 다음부터는 이름으로도 찾히게 기억합니다.
                store = StateStore(seat.state_path)
                try:
                    store.remember_ticker(found)
                finally:
                    store.close()
                # 증권사가 이름을 안 줬으면 아는 것으로 채웁니다. 코드만 뜬
                # 검색 결과는 고르는 사람에게 아무 정보도 주지 않습니다.
                named = {**found, "name": found.get("name") or book.name(digits)}
                return {"results": [{**named, "source": "venue"}], "message": ""}
            return {"results": [], "message":
                    f"{digits} 를 찾지 못했습니다. 증권사 연동을 확인하거나, "
                    f"장 시간이 아니면 시세가 오지 않을 수 있습니다."}

        lowered = text.lower()
        hits = [v for v in known.values()
                if lowered in v["ticker"].lower() or lowered in (v["name"] or "").lower()]
        if hits:
            return {"results": hits[:20], "message": ""}
        return {"results": [], "message":
                "이름으로는 전에 조회한 종목과 전략에 들어 있는 종목만 찾습니다. "
                "처음 찾는 종목은 6자리 종목코드를 넣어 주세요."}

    @app.post("/api/evaluate")
    async def evaluate(req: EvaluateRequest, seat: Desk = Depends(desk)):
        """종목 하나를 지금 심의합니다 — 봇을 켜지 않고.

        데스크가 어떤 종목을 사라고 했을 때, 그걸 확인하려고 먼저 봇을 켜야
        한다면 순서가 뒤바뀝니다. 검색해서 고른 종목을 그 자리에서 16명에게
        물어볼 수 있어야 합니다.

        **이 호출은 돈이 듭니다.** 심의 한 번이 약 $0.06 이고 그 비용은
        서비스가 냅니다. 그래서 요금제 한도를 먼저 확인하고, 끝난 뒤에는
        실제 토큰 수로 계량합니다 — 계량하지 않으면 한 사람의 반복 클릭이
        운영자 카드로 청구되고, 나중에 소급해서 만들 수도 없습니다.
        """
        ticker = (req.ticker or "").strip().upper()
        if not ticker:
            raise HTTPException(400, "종목을 지정하세요")

        # 돌고 있는 봇이 있으면 그 설정으로 심의합니다(데스크의 기억과 이력이
        # 이어집니다). 꺼져 있으면 사용자가 고른 전략으로 — `run_config()` 는
        # 봇이 없을 때 프로세스 기본 템플릿으로 물러서므로, 그 값을 그대로
        # 쓰면 사용자가 무엇을 골랐든 데모 전략으로 심의하게 됩니다.
        cfg = (seat.run_config() if seat.running()
               else (_template_config(req.strategy) or seat.run_config()))
        if cfg is None:
            raise HTTPException(
                400, "전략을 먼저 고르세요 — 심의는 그 전략의 설정(봉 간격, "
                     "리스크 한도, 데스크 모델)을 그대로 씁니다.")

        if not any(m.type in ("desk", "council") for m in cfg.alpha):
            raise HTTPException(
                400, f"'{cfg.name}' 전략에는 AI 데스크가 없습니다. "
                     f"데스크가 있는 전략을 고르세요.")

        # `own_key` 는 요금제 상한을 면제할지 정하는 값입니다. "GOOGLE_API_KEY
        # 라는 이름이 등록돼 있는가" 로 판정하면, 아무 문자열이나 저장해 둔
        # 사람이 상한을 없애면서 정작 심의는 운영자 키로 나갑니다 — 상한도
        # 없고 운영자 집계에도 안 잡히는 조합입니다. 데스크가 **실제로 들고
        # 있는 키**가 그 사람 것인지로 봅니다.
        model = seat.desk_model()
        if model is not None:
            # 돌고 있는 봇의 데스크를 그대로 씁니다 — 기억과 이력이 이어집니다.
            # 자기 키로 도는지는 따로 물어야 합니다: 안 넣은 사람의 봇도
            # 운영자 키로 잘 돕니다.
            own_key = await run_in_threadpool(
                seat.registry.desk_owns_key, seat.user.id, cfg)
        else:
            try:
                model, own_key = await run_in_threadpool(
                    seat.registry.desk_for, seat.user.id, cfg)
            except Exception as exc:
                log.warning("데스크 생성 실패: %s", exc)
                # 사용자가 읽어야 하는 문장입니다. 원문이 영어면 무엇을 해야
                # 하는지 알 수 없으므로, 가장 흔한 원인은 한국어로 바꿔 줍니다.
                text = str(exc)
                if "no API key" in text or "api key" in text.lower():
                    # 이미 자기 키를 넣은 사람에게 "키를 넣으세요" 라고 말하면,
                    # 맞는 말도 아니고 고칠 방법도 알려주지 못합니다.
                    mine = await run_in_threadpool(
                        seat.registry.desk_owns_key, seat.user.id, cfg)
                    raise HTTPException(
                        503,
                        "넣어 두신 Gemini 키를 쓸 수 없습니다 — 값이 맞는지, "
                        "해당 키에 Gemini API 사용 권한이 있는지 확인하세요."
                        if mine else
                        "AI 데스크를 쓸 수 없습니다 — 서비스의 Gemini 키가 "
                        "설정되지 않았거나 한도에 걸렸습니다. 마이페이지에서 "
                        "본인 Gemini 키를 넣으면 바로 쓸 수 있습니다.") from None
                raise HTTPException(
                    503, f"데스크를 준비할 수 없습니다: {text}") from None

        plan = getattr(seat.user, "plan", "free")
        usage = seat.registry.usage
        allowed, why = await run_in_threadpool(
            usage.allow, seat.user.id, plan, own_key)
        if not allowed:
            raise HTTPException(429, why)

        symbol = next((s for s in _config_symbols(cfg)
                       if s.ticker.upper() == ticker), None)
        if symbol is None:
            try:
                provider = seat.data_provider(cfg)
                symbol = await provider.resolve(ticker)
            except Exception as exc:
                log.warning("종목 해석 실패 %s: %s", ticker, exc)
                symbol = None
        if symbol is None:
            raise HTTPException(
                404, f"{ticker} 를 찾을 수 없습니다. 6자리 종목코드인지, "
                     f"증권사가 연동되어 있는지 확인하세요.")

        # 심의는 과거 봉 위에서 이뤄집니다. 봉이 없으면 16명이 아무것도 볼 수
        # 없고, 그 상태에서 나온 결론은 심의가 아니라 추측입니다.
        try:
            provider = seat.data_provider(cfg)
            bars = await provider.latest_bars(symbol, cfg.data.timeframe,
                                              cfg.data.warmup_bars)
        except Exception as exc:
            log.warning("시세 조회 실패 %s: %s", symbol.key, exc)
            raise HTTPException(
                503, f"{ticker} 의 시세를 받지 못했습니다: {exc}") from None
        if len(bars) < 60:
            raise HTTPException(
                422, f"{ticker} 의 과거 봉이 {len(bars)}개뿐입니다 — 심의하기에 "
                     f"부족합니다(최소 60개). 상장 직후이거나 거래가 드문 종목일 수 있습니다.")

        ctx = _standalone_context(cfg, symbol, bars)
        before_calls, before_cost = model.status()["llm_calls"], model.estimated_cost_usd
        try:
            decision = await model.deliberate(ctx, symbol)
        except Exception as exc:
            log.warning("심의 실패 %s: %s", ticker, exc)
            raise HTTPException(502, f"심의 중 오류: {exc}") from None
        finally:
            # 실패했어도 부른 만큼은 청구됩니다. 성공만 계량하면 실패한
            # 호출의 비용이 아무 계정에도 잡히지 않습니다.
            after = model.status()
            spent = max(0.0, model.estimated_cost_usd - before_cost)
            calls = max(0, after["llm_calls"] - before_calls)
            if calls:
                await run_in_threadpool(
                    usage.record_spend, seat.user.id, calls, spent, own_key)

        if decision is None:
            raise HTTPException(
                422, "심의가 결론에 이르지 못했습니다 — 마감 시간을 넘겼거나 "
                     "데스크 한도에 걸렸습니다. 잠시 후 다시 시도하세요.")
        out = decision.to_dict()
        out["metered"] = {"llm_calls": calls, "cost_usd": round(spent, 4),
                          "billed_to": "own_key" if own_key else "service"}
        return out

    @app.get("/api/events")
    async def events(limit: int = Query(100, ge=1, le=500),
                     type: str | None = Query(None, max_length=500),
                     seat: Desk = Depends(desk)):
        types = {t.strip() for t in type.split(",")} if type else None
        return {"events": seat.hub.recent(limit, types)}

    @app.get("/api/desk")
    async def desk_status(limit: int = Query(20, ge=1, le=200),
                          seat: Desk = Depends(desk)):
        """Desk status plus recent deliberations, newest first.

        This is what the trading-floor view renders: one entry per full
        ten-seat deliberation, including every seat's own output so the debate
        can be replayed rather than just summarised.
        """
        model = seat.desk_model()
        if model is None:
            return {"enabled": False,
                    "message": "no desk configured — add an alpha of type 'desk'",
                    "deliberations": []}
        book = NameBook(seat.state_path)
        payload = model.status()
        payload["deliberations"] = [
            {**d.to_dict(), "ticker_name": book.name(d.ticker)}
            for d in reversed(model.history[-limit:])]
        payload.pop("latest", None)
        return payload

    @app.get("/api/desk/{ticker}")
    async def desk_symbol(ticker: str, seat: Desk = Depends(desk)):
        model = seat.desk_model()
        if model is None:
            raise HTTPException(404, "no desk configured")
        for decision in reversed(model.history):
            if decision.ticker.upper() == ticker.upper():
                book = NameBook(seat.state_path)
                return {**decision.to_dict(),
                        "ticker_name": book.name(decision.ticker)}
        raise HTTPException(404, f"no deliberation recorded for {ticker}")

    @app.get("/api/flow")
    async def flow(symbol: str | None = Query(None, max_length=64),
                   window: int = Query(20, ge=1, le=500),
                   sessions: int = Query(30, ge=1, le=500),
                   seat: Desk = Depends(desk)):
        """투자자별 수급 — foreign / institution / retail net buying."""
        # 여기 `message` 는 로그가 아니라 화면 빈 칸에 그대로 찍힙니다. 영어로
        # 적으면 읽는 사람이 못 읽고, `flow.provider` 같은 설정 키를 가리키면
        # 화면에서 손댈 수 없는 곳을 가리키게 됩니다.
        trader = seat.trader()
        if trader is None:
            return {"available": False,
                    "message": "자동매매가 돌고 있지 않습니다 — 수급은 봇이 도는 "
                               "동안 모입니다.",
                    "symbols": {}}
        feed = getattr(trader.engine, "flow_feed", None)
        if feed is None or not feed.has_data:
            return {"available": False,
                    "message": "수급 자료가 아직 없습니다 — 이 전략이 수급을 받도록 "
                               "설정돼 있지 않거나, 첫 자료가 아직 오지 "
                               "않았습니다. '외국인·기관 수급' 신호가 들어 있는 "
                               "전략을 고르면 채워집니다.",
                    "failures": getattr(feed, "failures", {}), "symbols": {}}
        ctx = trader.engine.ctx
        wanted = [s for s in ctx.universe
                  if symbol is None or s.ticker.upper() == symbol.upper()]
        book = NameBook(seat.state_path)
        out = {}
        for sym in wanted:
            summary = feed.summary(sym, ctx.now, window)
            recent = feed.get(sym, ctx.now)[-sessions:]
            out[sym.ticker] = {
                "name": book.name(sym.ticker),
                "summary": summary.to_dict() if summary else None,
                "sessions": [f.to_dict() for f in recent],
            }
        return {"available": True, "window": window, "symbols": out}

    @app.get("/api/universe")
    async def universe(strategy: str | None = Query(None, max_length=64),
                       seat: Desk = Depends(desk)):
        """Symbols with their last mark — drives the ticker tape and the chart.

        봇이 꺼져 있으면 전략 이름으로 그 템플릿의 종목을 돌려줍니다. 시세를
        보려면 먼저 봇을 켜야 한다면, 데스크가 수동 매수를 추천했을 때 정작
        그 종목을 고를 수가 없습니다.
        """
        book = NameBook(seat.state_path)
        trader = seat.trader()
        if trader is None:
            if not strategy:
                return {"symbols": []}
            cfg = load_config(str(resolve_template(strategy)))
            symbols = _config_symbols(cfg)
            # 이 목록의 이름은 **한 번에** 물어봅니다. 종목마다 한 번씩 부르면
            # 유니버스가 넓을수록 느려지고, 레이트 리밋에 걸리면 이름이 하나도
            # 안 뜹니다.
            provider = _name_feed(seat, cfg)
            try:
                names = await book.resolve_many(
                    [s.ticker for s in symbols], provider,
                )
            finally:
                if provider is not None:
                    with contextlib.suppress(Exception):
                        await provider.close()
            return {"symbols": [
                {"ticker": sym.ticker, "name": names.get(sym.ticker, sym.ticker),
                 "venue": sym.venue,
                 "currency": sym.quote_currency, "price": None,
                 "change_pct": None, "invested": False}
                for sym in symbols]}
        ctx = trader.engine.ctx
        names = await book.resolve_many([s.ticker for s in ctx.universe],
                                        getattr(trader, "provider", None))
        out = []
        for sym in ctx.universe:
            bars = ctx.history(sym, 2)
            last = bars[-1] if bars else None
            prev = bars[0] if len(bars) > 1 else None
            change = ((last.close / prev.close - 1) * 100
                      if last and prev and prev.close > 0 else None)
            out.append({
                "ticker": sym.ticker, "name": names.get(sym.ticker, sym.ticker),
                "venue": sym.venue,
                "currency": sym.quote_currency,
                "price": round(last.close, 4) if last else None,
                "change_pct": round(change, 2) if change is not None else None,
                "invested": ctx.is_invested(sym),
            })
        return {"symbols": out}

    # ── 수동 개입 ────────────────────────────────────────────────────────
    @app.get("/api/manual")
    async def manual_status(agent_id: str = "", seat: Desk = Depends(desk)):
        trader = seat.trader(agent_id)
        if trader is None:
            return {"running": False, "paused": False, "pending": [], "recent": []}
        engine = trader.engine
        book = NameBook(seat.state_path)
        manual = engine.manual.status()
        # 대기 중인 주문은 "이걸 정말 낼 것인가" 를 묻는 줄입니다. 코드만
        # 떠 있으면 확인이 되지 않습니다.
        for field in ("pending", "recent"):
            manual[field] = book.tag(manual.get(field), "symbol")
        return {
            "running": True,
            **manual,
            "pinned": engine.ctx.pinned,
            "budget": engine.budget.status() if engine.budget.configured else None,
        }

    @app.post("/api/manual/buy")
    async def manual_buy(req: ManualOrderRequest, agent_id: str = "",
                         seat: Desk = Depends(desk)):
        trader = seat.require_trader(agent_id)
        symbol = _resolve(trader, req.ticker)
        if req.quantity is None and req.notional is None:
            raise HTTPException(400, "수량 또는 금액 중 하나는 지정해야 합니다")
        from decimal import Decimal

        request = trader.engine.manual.buy(
            symbol,
            quantity=Decimal(str(req.quantity)) if req.quantity is not None else None,
            notional=req.notional, limit_price=req.limit_price,
            manage=req.manage, note=req.note or "대시보드 수동 매수",
        )
        seat.record("manual_buy", symbol.ticker)
        return {"queued": request.to_dict(),
                "note": "다음 봉 처리 시 브로커 안전장치(주문 한도·일일 한도)를 거쳐 발주됩니다"}

    @app.post("/api/manual/sell")
    async def manual_sell(req: ManualOrderRequest, agent_id: str = "",
                          seat: Desk = Depends(desk)):
        trader = seat.require_trader(agent_id)
        symbol = _resolve(trader, req.ticker)
        from decimal import Decimal

        request = trader.engine.manual.sell(
            symbol,
            quantity=Decimal(str(req.quantity)) if req.quantity is not None else None,
            notional=req.notional, limit_price=req.limit_price,
            note=req.note or "대시보드 수동 매도",
        )
        seat.record("manual_sell", symbol.ticker)
        return {"queued": request.to_dict()}

    @app.post("/api/manual/close")
    async def manual_close(req: ManualOrderRequest, agent_id: str = "",
                           seat: Desk = Depends(desk)):
        trader = seat.require_trader(agent_id)
        symbol = _resolve(trader, req.ticker)
        seat.record("manual_close", symbol.ticker)
        return {"queued": trader.engine.manual.close(
            symbol, note=req.note or "대시보드 수동 청산").to_dict()}

    @app.post("/api/manual/close_all")
    async def manual_close_all(agent_id: str = "", seat: Desk = Depends(desk)):
        # 에이전트를 지정하지 않으면 `require_trader` 가 400 으로 되묻습니다.
        # 조용히 하나만 정리하고 성공을 돌려주면, 사용자는 전부 정리된 줄 알고
        # 화면을 닫고 나머지 셋은 그대로 시장에 남습니다.
        trader = seat.require_trader(agent_id)
        seat.record("manual_close_all")
        return {"queued": trader.engine.manual.close_all(
            note="대시보드 전체 청산").to_dict()}

    @app.post("/api/manual/pause")
    async def manual_pause(agent_id: str = "", seat: Desk = Depends(desk)):
        trader = seat.require_trader(agent_id)
        trader.engine.manual.pause("대시보드에서 일시정지")
        seat.record("manual_pause")
        return {"paused": True,
                "note": "신규 진입만 중단됩니다. 손절·청산·수동주문은 계속 동작합니다."}

    @app.post("/api/manual/resume")
    async def manual_resume(agent_id: str = "", seat: Desk = Depends(desk)):
        trader = seat.require_trader(agent_id)
        trader.engine.manual.resume()
        seat.record("manual_resume")
        return {"paused": False}

    @app.post("/api/manual/unpin/{ticker}")
    async def manual_unpin(ticker: str, agent_id: str = "",
                           seat: Desk = Depends(desk)):
        trader = seat.require_trader(agent_id)
        trader.engine.ctx.unpin(_resolve(trader, ticker))
        return {"unpinned": ticker, "note": "이 종목을 다시 전략이 관리합니다"}

    @app.post("/api/limits")
    async def limits_save(req: LimitsRequest, agent_id: str = "",
                          seat: Desk = Depends(desk)):
        """Apply daily caps now, and persist them for the next restart.

        **Partial.** Only the caps named in the request body change; a field
        that is absent (or null) is left exactly as it was, in the running bot
        and in storage alike. Sending an explicit `0` removes that cap — which
        means unlimited — and the response names every cap it removed.
        """
        return seat.save_limits(req, agent_id)

    @app.get("/api/limits")
    async def limits_get(agent_id: str = "", seat: Desk = Depends(desk)):
        return seat.limits(agent_id)

    @app.post("/api/limits/release")
    async def limits_release(agent_id: str = "", seat: Desk = Depends(desk)):
        """Clear a daily-cap halt for the rest of today. Deliberately explicit."""
        out = seat.release_halt(agent_id)
        seat.record("limits_released")
        return out

    # ── 투자 성향 ────────────────────────────────────────────────────────
    @app.get("/api/profile/questions")
    async def profile_questions(_: Desk = Depends(desk)):
        return {
            "questions": questionnaire(),
            "note": "여덟 문항으로 위험 감내도를 정확히 잴 수는 없습니다. "
                    "이건 진단이 아니라 출발점이고, 언제든 마이페이지에서 바꿀 수 "
                    "있습니다. 마지막 문항만은 성향이 아니라 사실을 묻는 것입니다.",
        }

    @app.get("/api/profile")
    async def profile_get(agent_id: str = "", seat: Desk = Depends(desk)):
        return seat.profile_store(agent_id).load().to_dict()

    @app.post("/api/profile")
    async def profile_save(req: ProfileRequest, agent_id: str = "",
                           seat: Desk = Depends(desk)):
        profile = score_answers(req.answers)
        if not profile.answers:
            raise HTTPException(400, "인식된 답안이 없습니다")
        applied = seat.save_profile(profile, agent_id)
        return {**profile.to_dict(), "applied_now": applied}

    @app.patch("/api/profile")
    async def profile_override(req: ProfileOverrideRequest, agent_id: str = "",
                               seat: Desk = Depends(desk)):
        """마이페이지에서 축을 직접 밀어 올리거나 내린다."""
        profile = seat.profile_store(agent_id).load()
        for axis, value in req.overrides.items():
            axis = axis.upper()
            if axis not in ("R", "H", "E", "C"):
                raise HTTPException(400, f"알 수 없는 축: {axis}")
            profile.overrides[axis] = max(-1.0, min(1.0, float(value)))
        applied = seat.save_profile(profile, agent_id)
        return {**profile.to_dict(), "applied_now": applied}

    @app.delete("/api/profile/override/{axis}")
    async def profile_clear_override(axis: str, agent_id: str = "",
                                     seat: Desk = Depends(desk)):
        return seat.clear_override(axis, agent_id)

    # ── 초기 설정 ────────────────────────────────────────────────────────
    @app.get("/api/setup")
    async def setup_status(seat: Desk = Depends(desk)):
        return seat.setup()

    @app.post("/api/setup")
    async def setup_save(req: SetupRequest, seat: Desk = Depends(desk)):
        """Store setup values. Only the keys this screen owns are accepted.

        Anything else — a typo, or a `HTTPS_PROXY` that would reroute the next
        broker call — is refused and named in `rejected`.
        """
        out = seat.save_setup(req.values)
        note = "저장된 값은 다시 조회할 수 없습니다 (재발급만 가능)"
        if out["rejected"]:
            note += (" — 거부된 항목: "
                     + ", ".join(f"{k}({v})" for k, v in out["rejected"].items()))
        return {**out, "note": note}

    @app.get("/api/setup/inspect")
    async def setup_inspect(seat: Desk = Depends(desk)):
        """저장된 자격증명의 **모양**만 봅니다. 값은 어떤 경로로도 나가지 않습니다.

        "키를 넣었는데 403" 일 때 가장 먼저 확인해야 하는 것은 "내가 넣은 값이
        실제로 저장돼 있는가" 입니다. 브라우저 자동완성이 로그인 비밀번호를
        채우고 그것이 덮어써지는 일이 실제로 일어나고, 그때 서버는 그냥
        `access_denied` 라고만 답합니다.

        길이·접두사·공백 여부만 돌려줍니다. 그것만으로 잘못 저장된 값은
        대부분 드러나고, 값 자체는 여전히 다시 조회할 수 없습니다.
        """
        secrets = await run_in_threadpool(
            seat.registry.accounts.secrets_for, seat.user.id)
        out = []
        for env in sorted(secrets):
            value = secrets[env] or ""
            problem = _shape_problem(env, value)
            out.append({
                "name": env,
                "length": len(value),
                # 앞 5자면 접두사를 확인하기에 충분하고, 그것만으로는 키를
                # 되살릴 수 없습니다.
                "starts_with": value[:5],
                "ends_with": value[-4:] if len(value) > 12 else "",
                "has_whitespace": any(ch.isspace() for ch in value),
                "trimmed": value != value.strip(),
                "problem": problem,
            })
        return {"secrets": out}

    @app.post("/api/setup/verify/{venue_id}")
    async def setup_verify(venue_id: str, seat: Desk = Depends(desk)):
        if venue_id not in VENUES_BY_ID:
            raise HTTPException(404, f"알 수 없는 거래소: {venue_id}")
        return await seat.verify(venue_id)

    @app.post("/api/setup/disconnect/{venue_id}")
    async def setup_disconnect(venue_id: str, seat: Desk = Depends(desk)):
        """이 거래소에 저장된 값을 전부 지운다. 되돌릴 수 없습니다."""
        if venue_id not in VENUES_BY_ID:
            raise HTTPException(404, f"알 수 없는 거래소: {venue_id}")
        out = seat.disconnect(venue_id)
        return {**out, "note": "다시 쓰려면 키를 새로 등록해야 합니다"}

    @app.get("/api/strategies")
    async def strategies(seat: Desk = Depends(desk)):
        """이 서비스가 돌려주는 전략 목록 — `/api/trader/start` 가 받는 이름들.

        각 전략이 **무엇을 하는지 한국어로** 함께 내보냅니다. 화면에
        `kr-toss-flow` 만 뜨면 그게 뭔지 이미 아는 사람만 쓸 수 있고, 이건
        자기 돈을 넣는 사람이 읽어야 하는 목록입니다.
        """
        book = NameBook(seat.state_path)
        out = []
        for name, path in strategy_catalog().items():
            try:
                cfg = load_config(str(path))
            except Exception:
                continue          # 전략이 아닌 YAML(파라미터 공간 등)
            signals = [glossary.describe(glossary.ALPHA, m.type) for m in cfg.alpha]
            out.append({
                "id": name, "name": cfg.name, "mode": cfg.mode.value,
                "broker": cfg.broker.type,
                "symbols": len(cfg.universe.symbols),
                "requires": required_secrets(cfg),
                # ── 한국어 ──────────────────────────────────────────────
                # `name` 은 로그와 설정이 쓰는 주소라 영어로 두고, 사람이 고르는
                # 이름은 여기로 내보냅니다. 설정에 없으면 빈 문자열이 나가고,
                # 화면은 그때 `name` 으로 떨어집니다 — 이름이 없다고 목록에서
                # 그 전략이 사라지면 그게 훨씬 나쁩니다.
                "label_ko": cfg.label_ko,
                "mode_ko": glossary.MODE.get(cfg.mode.value, cfg.mode.value),
                "broker_ko": glossary.BROKER.get(cfg.broker.type, cfg.broker.type),
                "timeframe": cfg.data.timeframe,
                # 시세가 실시간인지 지연인지는 전략을 고르기 전에 알아야 합니다.
                # 지연 시세로 실시간 매매를 하면 화면에서 본 가격과 주문이 닿는
                # 가격이 달라지고, 그 차이는 손실로만 나타납니다.
                "feed": cfg.data.provider,
                "feed_ko": glossary.PROVIDER.get(cfg.data.provider, cfg.data.provider),
                "realtime": cfg.data.provider not in glossary.DELAYED_PROVIDERS,
                # 장세 필터는 사는 쪽이 아니라 막는 쪽입니다. 섞어서 보여주면
                # "이것만 켜면 알아서 산다" 로 읽힙니다.
                "signals": [g for g in signals if g["kind"] == "signal"],
                "guards": [g for g in signals if g["kind"] == "guard"],
                "execution": glossary.describe(glossary.EXECUTION,
                                               cfg.execution.model.type),
                "protections": [glossary.describe(glossary.RISK, r.type)
                                for r in cfg.risk.models],
                # 전략을 고르는 화면입니다. "005930, 000660" 만 늘어놓으면
                # 무엇에 돈을 넣는 전략인지 코드를 외운 사람만 읽습니다.
                # 여기서는 증권사에 묻지 않습니다 — 전략 목록 한 번에 설정
                # 파일 전부를 도는 자리라, 조회를 섞으면 목록이 느려지고
                # 키를 등록하지 않은 사람에게는 아예 안 뜹니다.
                "tickers": book.labels(
                    [sy.ticker for sy in cfg.universe.symbols][:12]),
                # 실거래를 켜기 전에 사람이 읽어야 하는 숫자입니다. 화면이
                # 확인 창에서 이 값을 그대로 보여줍니다 — "얼마까지 잃어도
                # 되는가" 를 모르는 채로 켜는 일이 없어야 합니다.
                "limits": {
                    "daily_notional": cfg.limits.max_daily_notional or None,
                    "daily_orders": cfg.limits.max_daily_orders or None,
                    "daily_loss": cfg.limits.max_daily_loss or None,
                    "daily_loss_pct": cfg.limits.max_daily_loss_pct or None,
                    "per_order": cfg.broker.max_order_notional or None,
                },
            })
        return {"strategies": out}

    @app.get("/api/account/broker")
    async def broker_account(strategy: str | None = Query(None, max_length=120),
                             seat: Desk = Depends(desk)):
        """증권사가 말하는 계좌 — 봇이 꺼져 있어도.

        "내 계좌" 탭은 돌고 있는 봇의 장부만 그렸습니다. 그래서 연동을 마친
        사람이 봇을 켜기 전에 이 탭을 열면 통째로 비어 있었고, 그건 "연동이
        안 됐다" 로 읽힙니다.

        조회 전용입니다 — 이 경로로는 주문이 나가지 않습니다.
        """
        cfg = _selected_read_config(seat, strategy)
        if cfg is None:
            return {"supported": False, "message": "전략을 먼저 고르세요"}
        user_scope = seat.user.id if seat.user is not None else 0
        # Two configs can route the same adapter type to different accounts or
        # exchanges. A two-second duplicate read is cheaper than showing account
        # A's cash under strategy B, so strategy identity stays in the partition.
        cache_key = ("broker-account", user_scope, cfg.name, cfg.broker.type)
        try:
            return await state.account_reads.get(
                cache_key,
                lambda: seat.registry.broker_account(seat.user.id, cfg),
            )
        except ReadBusy:
            raise HTTPException(
                503, "계좌 조회가 잠시 밀려 있습니다 — 곧 다시 시도하세요",
                headers={"Retry-After": "1"},
            ) from None
        except RuntimeProblem as exc:
            # 자격증명이 없거나 설정이 거부된 경우 — 사용자가 무엇을 해야
            # 하는지 그 문장이 이미 말해 줍니다.
            return {"supported": False, "message": str(exc)}
        except Exception as exc:                      # noqa: BLE001
            log.warning("계좌 조회 실패: %s", exc)
            # 증권사가 답을 안 준 것과 연동이 안 된 것은 다릅니다. 사용자가
            # 무엇을 해야 하는지 갈리므로 문장을 그대로 넘깁니다.
            out: dict[str, Any] = {
                "supported": True,
                "error": f"계좌를 불러오지 못했습니다: {exc}",
            }
            retry_after = getattr(exc, "retry_after", None)
            try:
                retry_seconds = float(retry_after)
            except (TypeError, ValueError, OverflowError):
                retry_seconds = 0.0
            status_code = getattr(exc, "status_code", None)
            if ((status_code == 429 or status_code in range(500, 505))
                    and math.isfinite(retry_seconds) and retry_seconds > 0):
                out["retry_after_ms"] = min(
                    15 * 60 * 1000,
                    max(1000, int(retry_seconds * 1000)),
                )
            return out

    @app.get("/api/glossary")
    async def glossary_all(_: Desk = Depends(desk)):
        """부품 이름 사전 전체 — 화면이 어디서든 한국어로 쓸 수 있게."""
        return {
            "alpha": {k: glossary.describe(glossary.ALPHA, k) for k in glossary.ALPHA},
            "execution": {k: glossary.describe(glossary.EXECUTION, k)
                          for k in glossary.EXECUTION},
            "risk": {k: glossary.describe(glossary.RISK, k) for k in glossary.RISK},
            "mode": glossary.MODE, "broker": glossary.BROKER,
        }

    @app.get("/api/models")
    async def models(_: Desk = Depends(desk)):
        from quant.execution.models import BUILTIN_EXECUTION_MODELS
        from quant.portfolio.models import BUILTIN_PORTFOLIO_MODELS
        from quant.risk.models import BUILTIN_RISK_MODELS
        from quant.risk.protections import BUILTIN_PROTECTIONS
        from quant.strategy.builder import BUILTIN_ALPHA_MODELS

        return {
            "alpha": sorted(BUILTIN_ALPHA_MODELS) + ["council"],
            "portfolio": sorted(BUILTIN_PORTFOLIO_MODELS),
            "risk": sorted(BUILTIN_RISK_MODELS),
            "protections": sorted(BUILTIN_PROTECTIONS),
            "execution": sorted(BUILTIN_EXECUTION_MODELS),
        }

    # ── 관리자 ───────────────────────────────────────────────────────────
    @app.get("/api/admin/users")
    async def admin_users(seat: Desk = Depends(require_admin)):
        """가입자 목록. 이름과 봇 상태까지이고, 자격증명은 개수도 값도 아닙니다."""
        if registry is None or accounts is None:
            raise HTTPException(404, "계정이 없는 배포입니다")
        running = set(registry.running())
        rows = accounts.conn.execute(
            "SELECT id FROM users ORDER BY id").fetchall()
        out = []
        for row in rows:
            person = accounts.user(row["id"])
            if person is None:      # pragma: no cover - 조회 사이에 지워진 경우
                continue
            out.append({**public_user(person), "bot_running": person.id in running})
        return {"users": out}

    # ── actions ──────────────────────────────────────────────────────────
    @app.post("/api/backtest")
    async def backtest(req: BacktestRequest, seat: Desk = Depends(desk)):
        """Run a backtest off the event loop, one at a time per caller.

        백테스트는 CPU 를 끝까지 씁니다. `run_backtest` 안에는 진짜 대기 지점이
        없어서, 이벤트 루프 위에서 그대로 돌리면 끝날 때까지 루프를 붙잡습니다 —
        그동안 **모든 사용자의 봇이** 한 틱도 돌지 못하고, 패닉 버튼도 응답하지
        않습니다. 서비스가 스스로 제공하는 템플릿 하나로 20초였습니다.

        그래서 새 루프를 든 스레드로 내보내고, 한 사람이 한 번에 하나만, 프로세스
        전체로도 몇 개만 돌게 묶습니다. 스레드도 GIL 을 나눠 쓰므로 수를 묶지
        않으면 "막지는 않지만 모두를 느리게 하는" 것으로 바뀔 뿐입니다.
        """
        from quant.backtest.runner import run_backtest

        if registry is not None and req.config is not None:
            # 임의의 설정을 그대로 돌리면 데이터 제공자 하나로 서버 파일을 읽는
            # 길이 열립니다(csv 제공자 + 경로). 여러 사람이 쓰는 배포에서는
            # 템플릿 이름만 받습니다.
            raise HTTPException(
                400, "설정을 통째로 보내는 백테스트는 지원하지 않습니다 — "
                     "전략 템플릿 이름을 config_path 로 지정하세요.")
        if req.config_path:
            cfg = seat.load_strategy(req.config_path)
        elif req.config:
            cfg = StrategyConfig.model_validate(req.config)
        elif state.config:
            cfg = state.config.model_copy(deep=True)
        else:
            raise HTTPException(400, "supply config_path or config")
        cfg.mode = RunMode.BACKTEST
        if req.start:
            cfg.backtest.start = _parse_ts(req.start, "start")
        if req.end:
            cfg.backtest.end = _parse_ts(req.end, "end")
        if req.start or req.end:
            _assert_window_bounded(cfg)
        if req.starting_cash:
            cfg.portfolio.starting_cash = req.starting_cash

        owner = f"u{seat.user.id}" if seat.user is not None else "operator"
        inflight = state.backtests_running
        if owner in inflight:
            raise HTTPException(
                429, "백테스트가 이미 하나 돌고 있습니다 — 끝난 뒤에 다시 시도하세요")
        if len(inflight) >= MAX_CONCURRENT_BACKTESTS:
            raise HTTPException(
                429, "지금 백테스트가 밀려 있습니다 — 잠시 후 다시 시도하세요")
        inflight.add(owner)
        try:
            # 코루틴이지만 이 루프에서 기다릴 것이 없습니다. 스레드에서 자기
            # 루프로 돌리면 다른 사람의 봇은 계속 틱을 받습니다.
            result = await asyncio.to_thread(lambda: asyncio.run(run_backtest(cfg)))
        except Exception as exc:
            raise HTTPException(400, f"backtest failed: {exc}") from exc
        finally:
            inflight.discard(owner)
        return result.to_dict()

    @app.post("/api/trader/start")
    async def start_trader(req: StartRequest, seat: Desk = Depends(desk)):
        """Start a dry-run or live trader.

        `mode: "live"` carries exactly the preconditions `quant live` does: the
        config must itself declare `mode: live`, `broker.live_trading_confirmed`
        must be true, at least one daily cap must be set under `limits:`, and
        `confirm` must repeat the strategy name — the API's stand-in for the
        name the CLI makes a human type. 가입자마다 다시 요구합니다.
        """
        return await seat.start(req)

    @app.post("/api/trader/group/start")
    async def start_group(req: GroupStartRequest, seat: Desk = Depends(desk)):
        """한 계좌에 성향이 다른 에이전트를 최대 넷까지 띄운다.

        자본은 `capital_weight` 대로 나뉘고 합이 100% 를 넘을 수 없습니다.
        성향·손절·하루 한도는 에이전트마다 자기 파일에서 옵니다.

        실거래 확인(`confirm`)은 에이전트마다 따로 받습니다.
        """
        return await seat.start_group(req)

    @app.get("/api/agents")
    async def list_agents(seat: Desk = Depends(desk)):
        """지금 도는 에이전트와, 각자의 저장된 성향·한도.

        봇이 돌지 않아도 저장된 설정은 보여야 합니다 — 화면에서 성향을 먼저
        정하고 나중에 켜는 것이 정상 순서입니다.
        """
        running = seat.agents()
        return {
            "running": running,
            "max_agents": MAX_AGENTS,
            "agents": [
                {
                    "agent_id": agent_id,
                    "running": True,
                    "profile": seat.profile_store(agent_id).load().to_dict(),
                    "limits": seat.limits(agent_id),
                }
                for agent_id in running
            ],
        }

    @app.post("/api/trader/stop")
    async def stop_trader(seat: Desk = Depends(desk)):
        """단일 봇이든 그룹이든 멈춘다. 보유 포지션은 그대로 둡니다."""
        return await seat.stop()

    @app.get("/api/trader/reconciliation")
    async def reconciliation_status(
            config_path: str = Query(..., min_length=1, max_length=200),
            seat: Desk = Depends(desk)):
        """Show the exact quarantined Toss-live run for this signed-in owner."""
        return seat.reconciliation_status(config_path)

    @app.post("/api/trader/reconciliation/archive")
    async def archive_reconciliation(
            req: ReconciliationArchiveRequest, seat: Desk = Depends(desk)):
        """Preserve one manually reconciled run and make the next start fresh."""
        return seat.archive_reconciliation(req)

    @app.post("/api/trader/sync")
    async def sync_positions(seat: Desk = Depends(desk)):
        return await seat.sync()

    # ── stream ───────────────────────────────────────────────────────────
    @app.websocket("/ws")
    async def stream(ws: WebSocket):
        try:
            # 브라우저는 소켓 핸드셰이크에 헤더를 붙일 수 없습니다. 계정이 있는
            # 배포에서는 쿠키가 따라오므로 여기서도 `?token=` 은 아무것도
            # 열지 않습니다 — 계정이 없는 1인용 배포에서만 쓰입니다.
            seat = _desk(ws)
        except HTTPException:
            # 소켓에는 상태 코드로 말할 자리가 없습니다. 4401 은 대시보드가
            # "로그인이 끊겼다" 로 읽는 약속된 코드입니다.
            await ws.close(code=4401)
            return
        hub = seat.hub
        await ws.accept()
        hub.clients.add(ws)
        try:
            for item in hub.recent(50):
                await ws.send_text(json.dumps(finite(item), default=str))
            while True:
                await ws.receive_text()          # keep-alive / client pings
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            hub.clients.discard(ws)

    # ── dashboard ────────────────────────────────────────────────────────
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        # 서비스 워커는 자기가 놓인 경로만 통제합니다. /static/sw.js 로 두면
        # 스코프가 /static/ 이라 앱 전체를 감싸지 못하므로 루트에서 냅니다.
        @app.get("/sw.js", include_in_schema=False)
        async def service_worker():
            return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript")

        @app.get("/manifest.webmanifest", include_in_schema=False)
        async def manifest():
            return FileResponse(STATIC_DIR / "manifest.webmanifest",
                                media_type="application/manifest+json")

        @app.get("/")
        async def index():
            return FileResponse(STATIC_DIR / "index.html")

    @app.exception_handler(RuntimeProblem)
    async def runtime_problem(_request: Request, exc: RuntimeProblem):
        """레지스트리가 문장으로 끝낸 실패 — 상태 코드까지 스스로 알고 있습니다."""
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def request_validation_problem(
            _request: Request, exc: RequestValidationError):
        """Return serializable validation details without echoing secrets."""
        def safe_detail(value):
            if value is None or isinstance(value, (str, int, bool)):
                return value
            if isinstance(value, float):
                return value if math.isfinite(value) else None
            if isinstance(value, dict):
                return {str(key): safe_detail(item)
                        for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [safe_detail(item) for item in value]
            return str(value)[:500]

        details = [
            {key: safe_detail(value) for key, value in error.items()
             if key != "input"}
            for error in exc.errors()
        ]
        return SafeJSONResponse(status_code=422, content={"detail": details})

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception):
        log.exception("unhandled API error")
        if registry is not None:
            # 예외 문자열에는 어댑터가 붙인 것이 무엇이든 들어갈 수 있습니다.
            # 남의 서비스 위에서 도는 사람에게 그것을 그대로 보여주지 않습니다.
            return JSONResponse(status_code=500,
                                content={"error": "서버 오류 — 로그를 확인하세요"})
        return JSONResponse(status_code=500, content={"error": str(exc)})

    return app


_SECRET_HINTS = ("key", "secret", "token", "password", "passphrase")

#: 어떤 거래소 배선표에도 없지만 사람을 가리키는 필드들. 알림은 사람의
#: 텔레그램이고, `api_key` 는 AI 데스크 슬롯에 들어가는 LLM 키입니다.
_ALWAYS_SECRET = frozenset({"telegram_bot_token", "telegram_chat_id", "api_key"})

#: 설정 트리에서 배선 파라미터가 사는 자리.
_WIRING_SECTIONS = ("broker", "data", "flow")


def _credential_fields(config: StrategyConfig) -> frozenset[str]:
    """이 설정에서 자격증명이 들어가는 필드 이름 — 배선표가 선언한 그대로.

    이름을 여기 손으로 적어두면 거래소가 하나 늘 때마다 조용히 어긋납니다.
    실제로 `client_id` 와 `account_no` 가 그렇게 빠져나갔습니다: 이름에
    key/secret/token 이 없다는 이유만으로 통과했고, 그건 그 값이 비밀이 아니라는
    뜻이 아니라 부분 문자열로 비밀을 알아맞히려 했다는 뜻이었습니다. OAuth
    client id 와 계좌번호는 열쇠가 아니라 **신원**이고, 남에게 줄 이유는 열쇠와
    똑같이 없습니다.

    배선표(`registry._targets`)는 어느 자격증명이 어느 생성자 인자로 들어가는지
    이미 알고 있습니다. 가려야 할 이름은 정확히 그 인자들이라, 거래소가 늘면
    가리는 목록도 같이 늡니다.
    """
    names = set(_ALWAYS_SECRET)
    for _params, wiring in credential_targets(config):
        names.update(wiring.args)
    return frozenset(names)


def _without_wiring_params(body: dict) -> dict:
    """브로커·시세·수급 파라미터를 통째로 비운 사본.

    가입자에게 보여주는 설정에서만 씁니다. 거기 남아 있는 것은 프로세스
    템플릿에 적힌 운영자의 배선값이고, 그 사람의 봇은 그것으로 서지 않습니다.
    """
    out = dict(body)
    for section in _WIRING_SECTIONS:
        block = out.get(section)
        if isinstance(block, dict) and "params" in block:
            out[section] = {**block, "params": {}}
    return out


def _redact(node: Any, fields: frozenset[str] = frozenset()) -> Any:
    """Never hand a credential back through the API, even to an authed caller.

    `fields` 가 규칙입니다 — 배선표가 자격증명이라고 선언한 이름들. 부분 문자열
    힌트는 그 위에 남겨둔 그물일 뿐이라, 표에 없는 이름(설정 파일이 직접 적은
    `password` 같은 것)만 그쪽에 걸립니다.
    """
    if isinstance(node, dict):
        return {
            k: ("***" if v and (k.lower() in fields
                                or any(h in k.lower() for h in _SECRET_HINTS))
                else _redact(v, fields))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_redact(v, fields) for v in node]
    return node
