"""사용자별 실행 단위 — 한 프로세스 안에서 여러 사람의 봇을 따로 굴린다.

1인용일 때 `AppState` 는 하나였습니다. 트레이더 하나, 상태 DB 하나, 설정 하나.
서버 주인과 계좌 주인이 같은 사람이었으니 그걸로 충분했습니다. 그 전제가
깨지면 하나였던 것이 전부 사람 수만큼 늘어나야 하고, **늘어나지 않은 것이 곧
남의 계좌로 새는 구멍**입니다. 이 모듈이 그 구멍을 하나씩 막습니다.

**자격증명은 인자로만 흐릅니다.** `os.environ` 에는 절대 올리지 않습니다.
환경변수는 프로세스 전역이라, 한 사람의 한투 키를 올리는 순간 같은 프로세스의
다른 사람 봇이 그것으로 주문을 냅니다. 브로커·시세·수급 어댑터는 모두 명시적
인자를 받으므로(`KisBrokerage(app_key=...)`), 여기서 그 인자를 채워 넣고
환경변수 폴백은 절대 타지 않습니다. `secrets_for()` 의 반환값이 가는 곳은
어댑터 생성자뿐입니다 — 응답에도, 로그에도, 예외 메시지에도 넣지 않습니다.

**설정 사본도 키를 오래 들고 있지 않습니다.** 키를 담은 설정은 엔진을 세우는
동안만 존재하고, 트레이더가 계속 들고 다니는 `trader.config` 는 키가 없는
쪽으로 되돌립니다. 이유는 구체적입니다 — `LiveTrader.start()` 는
`config.model_dump_json()` 을 상태 DB 에 그대로 적습니다. 되돌리지 않으면
남의 앱 시크릿이 평문으로 디스크에 남습니다.

**상태 DB 는 사람마다 하나입니다.** 포지션·현금·잠금·핀·일일 원장이 한 파일에
섞이면 A 의 재시작이 B 의 보유를 복원합니다. `data/users/u{id}/state.db` 로
갈라 두고, 그 디렉터리는 0700 입니다.

**성향과 하루 한도도 사람 것입니다.** 둘 다 1인용 시절에는 프로세스에 하나씩
있었습니다(`investor_profile.json`, `QUANT_LIMIT_*` 환경변수). 사이즈와 손절과
하루 손실 한도를 정하는 값이라, 남의 것이 내 봇에 적용되는 것은 설정이 섞이는
정도의 일이 아닙니다.

**한 사람당 봇 하나.** 같은 사람이 두 번 시작하면 같은 계좌에 두 개의 봇이
붙고, 같은 상태 파일을 두 프로세스가 잡는 것과 같은 사고가 한 프로세스
안에서 일어납니다. 두 번째 요청은 거절합니다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from quant.config.schema import StrategyConfig
from quant.core.types import UTC
from quant.live.credentials import VENUES_BY_ID
from quant.live.profile import InvestorProfile, ProfileStore, apply_profile
from quant.webapp.accounts import Accounts
from quant.webapp.usage import UsageStore

log = logging.getLogger("quant.webapp.registry")

#: 봇을 멈추라고 말한 뒤 스스로 정리하기를 기다리는 시간. 마지막 사이클과
#: 상태 플러시가 끝나야 포지션 기록이 맞습니다.
STOP_GRACE_SECONDS = 20.0

#: 사용자별로 저장하는 하루 한도. 이름은 `LimitsConfig` 와 같습니다.
LIMIT_KEYS = ("max_daily_notional", "max_daily_orders",
              "max_daily_loss", "max_daily_loss_pct")

#: 하루 한도 이름 → `TradingBudget` 속성. 실행 중인 봇에 즉시 반영할 때 씁니다.
_BUDGET_ATTR = {
    "max_daily_notional": "max_notional",
    "max_daily_orders": "max_orders",
    "max_daily_loss": "max_loss",
    "max_daily_loss_pct": "max_loss_pct",
}

#: LLM 제공자별 자격증명 이름. 데스크 알파가 있을 때만 봅니다.
_LLM_SECRETS = {"anthropic": "ANTHROPIC_API_KEY",
                "openai": "OPENAI_API_KEY",
                "google": "GOOGLE_API_KEY"}


# ── 화면이 설명할 수 있는 실패 ───────────────────────────────────────────
class RuntimeProblem(RuntimeError):
    """스택 트레이스가 아니라 문장으로 끝나야 하는 실패.

    호출자(API 계층)가 그대로 응답으로 옮길 수 있도록 상태 코드와 기계가 읽는
    `code` 를 함께 답니다. 사람이 읽는 문장은 언제나 한국어입니다.
    """

    status = 400
    code = "runtime_problem"

    def to_dict(self) -> dict:
        return {"error": str(self), "code": self.code}


class CredentialsMissing(RuntimeProblem):
    """이 설정을 돌리려면 있어야 하는 자격증명이 아직 없다."""

    status = 400
    code = "credentials_missing"

    def __init__(self, missing: list[dict]):
        #: [{"name": "KIS_APP_KEY", "label": "앱 키", "venue": "kis",
        #:   "venue_label": "한국투자증권 (KIS)"}, ...]
        self.missing = missing
        venues: dict[str, list[str]] = {}
        for item in missing:
            venues.setdefault(item["venue_label"], []).append(item["label"])
        detail = " / ".join(f"{venue}: {', '.join(fields)}"
                            for venue, fields in venues.items())
        super().__init__(
            f"봇을 시작할 자격증명이 없습니다 — {detail}. "
            "설정 화면에서 먼저 등록해 주세요."
        )

    def to_dict(self) -> dict:
        return {**super().to_dict(), "missing": self.missing}


class ConfigRejected(RuntimeProblem):
    """성향·한도를 반영한 결과가 스키마를 통과하지 못했다."""

    status = 400
    code = "config_rejected"


class AlreadyRunning(RuntimeProblem):
    """이미 이 사용자의 봇이 돌고 있다."""

    status = 409
    code = "already_running"

    def __init__(self, strategy: str = ""):
        super().__init__(
            f"이미 봇이 실행 중입니다{f' ({strategy})' if strategy else ''} — "
            "새로 시작하려면 먼저 정지하세요."
        )


class NotRunning(RuntimeProblem):
    """멈추거나 조작할 봇이 없다."""

    status = 404
    code = "not_running"

    def __init__(self) -> None:
        super().__init__("실행 중인 봇이 없습니다")


# ── 어느 자격증명이 어느 인자로 들어가는가 ───────────────────────────────
@dataclass(frozen=True)
class _Wiring:
    """자격증명 이름 → 어댑터 생성자 인자.

    이 표가 이 모듈의 핵심입니다. 어댑터들은 모두 명시적 인자를 받고 없을 때만
    환경변수로 물러서는데, 그 폴백이 바로 여러 사람이 쓰는 순간 남의 키를 집는
    경로입니다. 여기서 인자를 채우면 폴백은 영원히 실행되지 않습니다.
    """

    venue: str                       # credentials.VENUES 의 id — 화면 라벨용
    args: dict[str, str]             # 생성자 인자 ← 자격증명 이름
    required: tuple[str, ...] = ()   # 없으면 시작 자체가 불가능한 것


def _ccxt_wiring(exchange: str, required: bool) -> _Wiring:
    """거래소 이름에서 자격증명 이름을 만든다 (binance → BINANCE_KEY/SECRET).

    설정 화면이 아는 세 거래소(binance/upbit/bybit)와 같은 규칙이라, 목록에
    없는 거래소를 쓰는 사용자도 같은 방식으로 키를 등록하면 그대로 붙습니다.
    """
    prefix = (exchange or "binance").upper()
    key, secret = f"{prefix}_KEY", f"{prefix}_SECRET"
    return _Wiring(
        venue=(exchange or "binance").lower(),
        args={"api_key": key, "secret": secret},
        required=(key, secret) if required else (),
    )


def _broker_wiring(config: StrategyConfig) -> _Wiring | None:
    kind = config.broker.type
    if kind == "kis":
        return _Wiring(
            "kis",
            {"app_key": "KIS_APP_KEY", "app_secret": "KIS_APP_SECRET",
             "account_no": "KIS_ACCOUNT_NO", "product_code": "KIS_ACCOUNT_PRD_CD"},
            ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO"),
        )
    if kind == "toss":
        return _Wiring(
            "toss",
            {"client_id": "TOSS_CLIENT_ID", "client_secret": "TOSS_CLIENT_SECRET",
             "account_no": "TOSS_ACCOUNT_NO"},
            ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_NO"),
        )
    if kind == "alpaca":
        return _Wiring(
            "alpaca",
            {"api_key": "ALPACA_API_KEY", "secret_key": "ALPACA_SECRET_KEY"},
            ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"),
        )
    if kind == "ccxt":
        return _ccxt_wiring(str(config.broker.params.get("exchange", "binance")),
                            required=True)
    return None                      # paper — 아무 계좌에도 닿지 않습니다


def _data_wiring(config: StrategyConfig) -> _Wiring | None:
    provider = config.data.provider
    if provider == "kis":
        return _Wiring("kis",
                       {"app_key": "KIS_APP_KEY", "app_secret": "KIS_APP_SECRET"},
                       ("KIS_APP_KEY", "KIS_APP_SECRET"))
    if provider == "toss":
        return _Wiring("toss",
                       {"client_id": "TOSS_CLIENT_ID",
                        "client_secret": "TOSS_CLIENT_SECRET"},
                       ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET"))
    if provider == "ccxt":
        # 시세는 공개 엔드포인트로도 받습니다. 키가 있으면 쓰되, 없다고 시작을
        # 막지는 않습니다 — 막으면 백테스트도 못 돌립니다.
        return _ccxt_wiring(str(config.data.params.get("exchange", "binance")),
                            required=False)
    return None                      # synthetic · csv · yahoo — 키가 없습니다


def _flow_wiring(config: StrategyConfig) -> _Wiring | None:
    if config.flow.provider == "kis":
        return _Wiring("kis",
                       {"app_key": "KIS_APP_KEY", "app_secret": "KIS_APP_SECRET"},
                       ("KIS_APP_KEY", "KIS_APP_SECRET"))
    return None


def _targets(config: StrategyConfig) -> list[tuple[dict, _Wiring]]:
    """(파라미터 딕셔너리, 배선) 쌍 — 키가 들어갈 자리 전부."""
    pairs = ((config.broker.params, _broker_wiring(config)),
             (config.data.params, _data_wiring(config)),
             (config.flow.params, _flow_wiring(config)))
    return [(params, wiring) for params, wiring in pairs if wiring is not None]


def _field_label(name: str) -> tuple[str, str]:
    """자격증명 이름 → (거래소 id, 한국어 라벨). 모르는 이름은 이름 그대로."""
    for venue in VENUES_BY_ID.values():
        for env, label, _required in venue.fields:
            if env == name:
                return venue.id, label
    return "", name


def _venue_label(venue_id: str) -> str:
    spec = VENUES_BY_ID.get(venue_id)
    return spec.label_ko if spec else (venue_id or "알 수 없는 거래소")


def required_secrets(config: StrategyConfig) -> list[str]:
    """이 설정으로 봇을 돌리려면 반드시 있어야 하는 자격증명 이름들.

    설정 화면이 "이 전략을 쓰려면 무엇을 등록해야 하는지" 를 미리 보여줄 수
    있도록 공개합니다. 값이 아니라 이름만 다룹니다.
    """
    names: list[str] = []
    for _params, wiring in _targets(config):
        for name in wiring.required:
            if name not in names:
                names.append(name)
    return names


def _with_credentials(config: StrategyConfig, secrets: dict[str, str]) -> StrategyConfig:
    """이 사용자의 키를 담은 **일회용 사본**.

    설정 파일에 이미 키가 적혀 있어도 사용자의 것이 이깁니다. 여러 사람이 쓰는
    서비스에서 템플릿 YAML 에 남아 있는 키는 운영자의 것이고, 그것으로 남의
    봇이 주문을 내면 그 주문은 운영자 계좌로 갑니다.

    반환값은 엔진을 세우는 동안만 살아 있어야 합니다. 응답으로 내보내거나
    저장하지 마세요 — 그러라고 `UserRegistry.prepare()` 가 따로 있습니다.
    """
    wired = config.model_copy(deep=True)
    for params, wiring in _targets(wired):
        for arg, name in wiring.args.items():
            value = secrets.get(name, "")
            if value:
                params[arg] = value

    # 알림은 사람을 가리킵니다. 자기 토큰이 있으면 그것을 쓰고, 없으면 템플릿에
    # 남아 있는 토큰을 **지웁니다** — 없는 편이 남의 텔레그램으로 내 체결 내역이
    # 가는 것보다 낫습니다.
    token = secrets.get("TELEGRAM_BOT_TOKEN", "")
    chat = secrets.get("TELEGRAM_CHAT_ID", "")
    if token and chat:
        wired.notify.telegram_bot_token, wired.notify.telegram_chat_id = token, chat
    else:
        wired.notify.telegram_bot_token = wired.notify.telegram_chat_id = ""

    # AI 데스크. 자기 키가 있으면 자기 비용으로 돌고, 없으면 설정에 있는 대로
    # 둡니다 — 서비스가 데스크를 제공하기로 한 경우가 그쪽입니다.
    for spec in wired.alpha:
        if spec.type not in ("desk", "council"):
            continue
        for slot in ("llm", "decision_llm"):
            llm = spec.params.get(slot)
            if not isinstance(llm, dict):
                continue
            name = _LLM_SECRETS.get(str(llm.get("provider", "anthropic")), "")
            if name and secrets.get(name):
                llm["api_key"] = secrets[name]
    return wired


def _tighter(configured: float, saved: float) -> float:
    """둘 중 더 낮은 한도. 0 은 "한도 없음" 이라 경쟁하지 않습니다.

    `builder._cap` 이 설정 파일과 설정 화면 사이에서 하는 것과 같은 규칙입니다.
    양쪽 다 누군가 최대치로 적은 숫자이므로, 한쪽이 다른 쪽의 천장을 올리는
    일은 없어야 합니다.
    """
    candidates = [v for v in (configured, saved) if v]
    return min(candidates) if candidates else 0.0


# ── 실행 중인 봇 한 대 ───────────────────────────────────────────────────
@dataclass
class _Bot:
    trader: Any
    task: asyncio.Task
    strategy: str
    started_at: datetime
    error: str = ""
    stopped_at: datetime | None = None

    @property
    def alive(self) -> bool:
        return not self.task.done()


@dataclass(frozen=True)
class UserPaths:
    """한 사용자의 파일들. 전부 그 사람 디렉터리 안에 있습니다."""

    home: Path
    state_db: Path
    profile: Path
    limits: Path


class UserRegistry:
    """사용자 한 명 = 상태 DB 하나, 성향 하나, 한도 하나, 봇 하나.

    API 계층은 세션에서 알아낸 `user_id` 만 가지고 이 객체를 부릅니다. 어떤
    메서드도 다른 사용자의 것을 돌려주지 않고, 어떤 반환값에도 자격증명이
    들어 있지 않습니다.
    """

    def __init__(self, accounts: Accounts, root: str | Path | None = None):
        self.accounts = accounts
        self.root = Path(root or os.environ.get("QUANT_USER_DATA", "data/users"))
        self.root.mkdir(parents=True, exist_ok=True)
        self._bots: dict[int, _Bot] = {}
        # AI 데스크 비용은 서비스가 냅니다. 계량을 켜 두지 않으면 한 사람의
        # 설정 실수가 운영자 카드로 청구되고, 나중에 소급해서 만들 수도
        # 없습니다 — "누가 얼마나 썼나" 는 기록이 없으면 답이 없습니다.
        self.usage = UsageStore(self.root / "desk_usage.db")

    # ── 파일 자리 ────────────────────────────────────────────────────────
    @staticmethod
    def _uid(user_id: int) -> int:
        """사용자 id 는 디렉터리 이름이 됩니다.

        요청에서 흘러온 문자열이 그대로 경로가 되면 `../` 하나로 남의 상태
        파일에 닿습니다. 정수가 아닌 것은 여기서 끝냅니다.
        """
        uid = int(user_id)
        if uid <= 0:
            raise ValueError(f"user id 는 양의 정수여야 합니다: {user_id!r}")
        return uid

    def paths(self, user_id: int) -> UserPaths:
        uid = self._uid(user_id)
        home = self.root / f"u{uid}"
        home.mkdir(parents=True, exist_ok=True)
        try:
            # 포지션과 체결 기록이 들어 있는 디렉터리입니다. 같은 머신의 다른
            # 계정이 읽을 이유가 없습니다.
            os.chmod(home, 0o700)
        except OSError:  # pragma: no cover - 파일시스템이 허용하지 않는 경우
            log.warning("사용자 디렉터리 권한을 700 으로 바꾸지 못했습니다: %s", home)
        return UserPaths(home=home, state_db=home / "state.db",
                         profile=home / "profile.json", limits=home / "limits.json")

    def state_path(self, user_id: int) -> str:
        """이 사용자의 상태 DB 경로. 포지션·현금·잠금·핀·일일 원장이 여기 있습니다."""
        return str(self.paths(user_id).state_db)

    # ── 투자 성향 ────────────────────────────────────────────────────────
    def profile_store(self, user_id: int) -> ProfileStore:
        return ProfileStore(self.paths(user_id).profile)

    def profile(self, user_id: int) -> InvestorProfile:
        return self.profile_store(user_id).load()

    def save_profile(self, user_id: int, profile: InvestorProfile) -> dict | None:
        """성향을 저장하고, 돌고 있는 봇에는 즉시 반영할 수 있는 것만 반영한다."""
        self.profile_store(user_id).save(profile)
        self.accounts.record(user_id, "profile_saved", profile.code)
        return self.apply_profile_live(user_id, profile)

    def apply_profile_live(self, user_id: int, profile: InvestorProfile) -> dict | None:
        """사이즈·손절·한도는 바로 바뀝니다. 봉 주기와 알파 구성은 안 바뀝니다.

        후자는 엔진을 다시 세워야 하는 일이라, 반쯤 바뀐 채로 도는 것보다
        재시작이 필요하다고 말하는 편이 정직합니다.
        """
        trader = self.trader(user_id)
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
        engine.budget.max_loss_pct = settings["max_daily_loss_pct"]
        engine.budget.max_orders = settings["max_daily_orders"]
        for model in engine.risk.models:
            if model.name == "max_dd_per_security":
                model.atr_multiple = settings["stop_atr_multiple"]
                model.limit = settings["stop_ceiling_pct"]
            elif model.name == "trailing_stop":
                model.atr_multiple = settings["trailing_atr_multiple"]
            elif model.name == "max_positions":
                model.max_positions = settings["max_positions"]
        return {"sizing_and_risk": "즉시 적용됨",
                "needs_restart": ["봉 주기", "알파 모델 구성", "AI 데스크 사용 여부"]}

    # ── 하루 한도 ────────────────────────────────────────────────────────
    def limits(self, user_id: int) -> dict[str, float]:
        """이 사용자가 저장해 둔 하루 한도. 0 은 "한도 없음" 입니다."""
        path = self.paths(user_id).limits
        if not path.exists():
            return dict.fromkeys(LIMIT_KEYS, 0.0)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # 읽지 못했다고 무한대로 돌릴 수는 없습니다. 0(한도 없음)으로
            # 물러서되 시끄럽게 남깁니다.
            log.warning("사용자 %s 의 하루 한도를 읽지 못했습니다 (%s)", user_id, exc)
            return dict.fromkeys(LIMIT_KEYS, 0.0)
        return {key: float(raw.get(key) or 0.0) for key in LIMIT_KEYS}

    def save_limits(self, user_id: int, caps: dict[str, float | None]) -> dict:
        """부분 갱신. 안 보낸 항목은 그대로 두고, 명시적 0 은 한도를 해제합니다.

        실행 중인 봇의 원장에도 그 자리에서 반영합니다 — 다음 재시작까지
        기다려야 하는 한도는 지금 필요해서 누른 사람에게 아무 소용이 없습니다.
        """
        stored = self.limits(user_id)
        budget = getattr(getattr(self.trader(user_id), "engine", None), "budget", None)
        updated: list[str] = []
        removed: list[str] = []
        ignored = [key for key in caps if key not in LIMIT_KEYS]
        for key in LIMIT_KEYS:
            sent = caps.get(key)
            if sent is None:
                continue
            value = abs(float(sent))
            if key == "max_daily_orders":
                value = float(int(value))
            if not value and stored[key]:
                removed.append(key)
            stored[key] = value
            updated.append(key)
            if budget is not None:
                setattr(budget, _BUDGET_ATTR[key],
                        int(value) if key == "max_daily_orders" else value)
        path = self.paths(user_id).limits
        path.write_text(json.dumps(stored, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        if updated:
            self.accounts.record(user_id, "limits_saved", ", ".join(updated))
        if removed:
            log.warning("사용자 %s 의 하루 한도 해제: %s — 다시 설정하기 전까지 무제한입니다",
                        user_id, ", ".join(removed))
        return {"saved": stored, "updated": updated, "removed": removed,
                "ignored": ignored,
                "applied_now": budget.status() if budget is not None else None}

    # ── 시작 전 점검 ─────────────────────────────────────────────────────
    def readiness(self, user_id: int, config: StrategyConfig) -> dict:
        """이 설정으로 시작할 수 있는지, 없다면 무엇이 없는지.

        값은 어디서도 읽지 않습니다 — 등록된 자격증명의 **이름**만으로 판단합니다.
        화면이 그대로 렌더링할 수 있는 모양으로 돌려줍니다.
        """
        # 이름만 봅니다. `configured()` 의 값(마지막 4자리)은 짧은 비밀에서
        # 빈 문자열이라, 값의 참·거짓으로 판단하면 멀쩡히 등록된 키가
        # "없음" 으로 읽힙니다.
        have = set(self.accounts.configured(user_id))
        missing: list[dict] = []
        seen: set[str] = set()
        for _params, wiring in _targets(config):
            for name in wiring.required:
                if name in have or name in seen:
                    continue
                seen.add(name)
                # 목록에 없는 거래소(예: ccxt 의 다른 이름)면 배선이 아는 이름을
                # 그대로 씁니다 — "알 수 없음" 보다 낫습니다.
                venue_id, label = _field_label(name)
                venue_id = venue_id or wiring.venue
                missing.append({
                    "name": name, "label": label,
                    "venue": venue_id, "venue_label": _venue_label(venue_id),
                })
        return {
            "ready": not missing,
            "missing": missing,
            "required": required_secrets(config),
            "configured": sorted(have),
        }

    def assert_ready(self, user_id: int, config: StrategyConfig) -> None:
        report = self.readiness(user_id, config)
        if not report["ready"]:
            raise CredentialsMissing(report["missing"])

    # ── 설정 준비 ────────────────────────────────────────────────────────
    def prepare(self, user_id: int, config: StrategyConfig) -> StrategyConfig:
        """이 사용자의 성향과 하루 한도를 반영한 설정. **자격증명은 없습니다.**

        이 반환값은 화면에 보여줘도 되고 저장해도 됩니다. 키가 들어간 사본은
        엔진을 세울 때만 따로 만들어집니다.
        """
        cfg = config.model_copy(deep=True)

        profile = self.profile(user_id)
        if profile.completed:
            cfg, touched = apply_profile(cfg, profile)
            if touched:
                log.info("사용자 %s 투자 성향 '%s' 적용: %s",
                         user_id, profile.name, ", ".join(touched))

        saved = self.limits(user_id)
        for key in LIMIT_KEYS:
            value = _tighter(float(getattr(cfg.limits, key)), saved[key])
            setattr(cfg.limits, key, int(value) if key == "max_daily_orders" else value)

        # 대입만으로는 pydantic 검증이 다시 돌지 않습니다. 성향이 채운 값이나
        # 사용자가 해제한 한도 때문에 "하루 한도 없는 실거래" 같은 조합이
        # 만들어질 수 있으므로, 시작 전에 한 번 더 통과시킵니다.
        try:
            return StrategyConfig.model_validate(cfg.model_dump())
        except ValidationError as exc:
            raise ConfigRejected(
                f"이 설정으로는 시작할 수 없습니다: {exc.errors()[0]['msg']}"
            ) from exc

    # ── 봇 ───────────────────────────────────────────────────────────────
    def data_provider(self, user_id: int, config: StrategyConfig):
        """이 사용자의 키로 세운 시세 프로바이더 — 봇과 무관하게.

        봇이 돌지 않아도 시세는 봐야 합니다. 화면 오른쪽에서 호가를 보고
        그 자리에서 수동으로 사는 것이 이 서비스의 절반이고, 그때 봇은 대개
        꺼져 있습니다.

        브로커는 세우지 않습니다. 조회만 하는 경로가 주문을 낼 수 있는 객체를
        들고 다닐 이유가 없습니다.
        """
        from quant.strategy.builder import build_data_provider

        cfg = self.prepare(user_id, config)
        wired = _with_credentials(cfg, self.accounts.secrets_for(user_id))
        return build_data_provider(wired)

    def desk_owns_key(self, user_id: int, config: StrategyConfig) -> bool:
        """이 사용자의 데스크가 **자기 키**로 도는가 — 데스크를 세우지 않고.

        요금제 상한을 면제할지 정하는 값입니다. "GOOGLE_API_KEY 라는 이름이
        등록돼 있는가" 로 판정하면 아무 문자열이나 저장해 둔 사람이 상한을
        없애면서 정작 심의는 운영자 키로 나갑니다 — 상한도 없고 운영자
        집계에도 안 잡히는 조합입니다.

        한 자리라도 운영자 키로 돌면 False 입니다. 분석석은 자기 키, 결정석은
        운영자 키로 도는 설정에서 "자기 키니까 무제한" 은 성립하지 않습니다.
        """
        spec = next((m for m in config.alpha if m.type in ("desk", "council")), None)
        if spec is None:
            return False
        secrets = self.accounts.secrets_for(user_id)
        wired = _with_credentials(self.prepare(user_id, config), secrets)
        wired_spec = next(m for m in wired.alpha if m.type in ("desk", "council"))
        slots = [wired_spec.params.get(s) for s in ("llm", "decision_llm")]
        slots = [s for s in slots if isinstance(s, dict)]
        if not slots:
            return False
        for llm in slots:
            name = _LLM_SECRETS.get(str(llm.get("provider", "anthropic")), "")
            mine = secrets.get(name, "") if name else ""
            if not mine or llm.get("api_key") != mine:
                return False
        return True

    def desk_for(self, user_id: int, config: StrategyConfig):
        """이 사용자의 키로 세운 AI 데스크 — 봇과 무관하게.

        `/api/evaluate` 가 부릅니다. 봇이 꺼져 있어도 종목 하나를 물어볼 수
        있어야 하는데, 그때 원본 템플릿으로 데스크를 세우면 설정 파일에 키가
        없으므로 `LLMConfig.resolved_key()` 가 **프로세스 환경변수** 로
        폴백합니다 — 즉 사용자가 자기 Gemini 키를 넣어 두었어도 심의는
        운영자 키로 나갑니다. 시세 프로바이더(`data_provider`)가 이미
        `_with_credentials` 를 타는 것과 같은 이유로 여기도 태웁니다.

        돌려주는 두 번째 값은 "이 데스크가 사용자 자기 키로 도는가" 입니다.
        요금제 상한을 면제할지 정하는 값이라, **이름이 등록됐는지**가 아니라
        **그 값이 실제로 데스크에 들어갔는지**로 판정해야 합니다. 아무 문자열이나
        저장해 두면 상한이 사라지는데 정작 심의는 운영자 키로 나가는 조합이
        가능해집니다.
        """
        spec = next((m for m in config.alpha if m.type in ("desk", "council")), None)
        if spec is None:
            return None, False

        wired = _with_credentials(self.prepare(user_id, config),
                                  self.accounts.secrets_for(user_id))
        wired_spec = next(m for m in wired.alpha if m.type in ("desk", "council"))
        own = self.desk_owns_key(user_id, config)

        from quant.strategy.builder import _build_council, _build_desk
        desk = (_build_desk(wired_spec, None) if wired_spec.type == "desk"
                else _build_council(wired_spec))
        return desk, own

    def build_trader(self, user_id: int, config: StrategyConfig):
        """이 사용자의 키로 세운 `LiveTrader`. 아직 돌지는 않습니다.

        보통은 `start()` 가 부릅니다. 직접 부른 경우 정리도 직접 해야 합니다
        (`trader.state.close()`) — 상태 DB 연결이 이미 열려 있습니다.
        """
        from quant.live.trader import LiveTrader

        cfg = self.prepare(user_id, config)
        self.assert_ready(user_id, cfg)
        paths = self.paths(user_id)

        # 복호화된 값이 사는 유일한 구간입니다. `secrets_for()` 의 결과는 이
        # 표현식 밖으로 나가지 않고, 사본은 아래 생성자에서 어댑터의 인자가 된
        # 뒤 이 함수와 함께 사라집니다.
        wired = _with_credentials(cfg, self.accounts.secrets_for(user_id))
        trader = LiveTrader(wired, str(paths.state_db),
                            profile_path=str(paths.profile))
        # 엔진은 키를 받았고, 트레이더가 들고 다니는 설정은 받지 않습니다.
        # `LiveTrader.start()` 가 `config.model_dump_json()` 을 상태 DB 에
        # 그대로 적기 때문에, 되돌리지 않으면 앱 시크릿이 평문으로 남습니다.
        trader.config = cfg
        return trader

    async def start(self, user_id: int, config: StrategyConfig,
                    on_event: Callable | None = None) -> dict:
        """이 사용자의 봇을 띄운다. 한 사람당 하나까지.

        `on_event` 는 이 사용자의 엔진 이벤트만 받습니다 — API 계층이 사용자별
        WebSocket 팬아웃을 붙이는 자리입니다.
        """
        uid = self._uid(user_id)
        existing = self._bots.get(uid)
        if existing is not None and existing.alive:
            raise AlreadyRunning(existing.strategy)

        # 여기부터 슬롯을 채울 때까지 `await` 가 없습니다. 이벤트 루프가 중간에
        # 다른 요청으로 넘어가지 못하므로, 같은 사용자의 동시 요청 두 개가 둘 다
        # "비어 있다" 를 보고 봇을 두 대 띄우는 일이 생기지 않습니다.
        trader = self.build_trader(uid, config)
        if on_event is not None:
            trader.engine.ctx.bus.on(None, on_event)
        task = asyncio.create_task(trader.run(), name=f"quant-bot-u{uid}")
        bot = _Bot(trader=trader, task=task, strategy=trader.config.name,
                   started_at=datetime.now(UTC))
        task.add_done_callback(lambda t, b=bot: self._finished(uid, b, t))
        self._bots[uid] = bot
        self.accounts.record(uid, "bot_started",
                             f"{trader.config.name} ({trader.config.mode.value})")
        log.info("사용자 %s 봇 시작: %s (%s)", uid, trader.config.name,
                 trader.config.mode.value)
        # 서버 파일 경로는 돌려주지 않습니다. 사용자가 알아야 할 것은 자기 봇이
        # 떴다는 사실이지 그것이 서버 어느 디렉터리에 사는지가 아니고, 경로에는
        # 사용자 번호와 디렉터리 구조가 함께 실려 나갑니다.
        return {"started": True, "strategy": trader.config.name,
                "mode": trader.config.mode.value}

    def _finished(self, uid: int, bot: _Bot, task: asyncio.Task) -> None:
        """봇이 끝났다 — 스스로 멈췄든, 죽었든.

        죽은 이유를 남겨 두는 것이 중요합니다. 화면에서 "실행 중 아님" 만
        보이면 사용자는 자기가 멈춘 줄 알고, 워밍업 실패나 인증 거절은 아무도
        모르는 채로 지나갑니다.
        """
        bot.stopped_at = datetime.now(UTC)
        if task.cancelled():
            bot.error = "취소됨"
            return
        exc = task.exception()
        if exc is None:
            self._note(uid, "bot_stopped", bot.strategy)
            return
        # 어댑터 예외 메시지에는 값이 아니라 이름만 들어갑니다(예: "KIS brokerage
        # needs KIS_APP_KEY"). 그래도 길이는 잘라서 남깁니다.
        bot.error = f"{type(exc).__name__}: {str(exc)[:300]}"
        log.error("사용자 %s 봇 중단: %s", uid, bot.error)
        self._note(uid, "bot_failed", type(exc).__name__)

    def _note(self, uid: int, action: str, detail: str = "") -> None:
        """감사 기록. 여기서 나는 예외는 삼킵니다.

        이 함수는 완료 콜백에서도 불립니다 — 그 자리에서 던져 봐야 받을 사람이
        없고, 종료 처리 도중이면 계정 DB 가 이미 닫혀 있을 수 있습니다.
        기록 실패가 봇 종료 처리를 망치게 두지 않습니다.
        """
        try:
            self.accounts.record(uid, action, detail)
        except Exception:       # pragma: no cover - 계정 DB 가 닫힌 경우 등
            log.warning("감사 기록 실패: user=%s action=%s", uid, action)

    def trader(self, user_id: int):
        """돌고 있는 트레이더, 없으면 None. 수동 매매·조회 엔드포인트가 씁니다."""
        bot = self._bots.get(self._uid(user_id))
        return bot.trader if bot is not None and bot.alive else None

    def require_trader(self, user_id: int):
        trader = self.trader(user_id)
        if trader is None:
            raise NotRunning()
        return trader

    def status(self, user_id: int) -> dict:
        bot = self._bots.get(self._uid(user_id))
        if bot is None:
            return {"running": False, "message": "봇이 실행 중이 아닙니다"}
        if not bot.alive:
            return {
                "running": False,
                "strategy": bot.strategy,
                "started_at": bot.started_at.isoformat(),
                "stopped_at": bot.stopped_at.isoformat() if bot.stopped_at else None,
                "error": bot.error,
                "message": bot.error or "봇이 정상적으로 종료되었습니다",
            }
        # `trader.status()["started_at"]` 은 워밍업이 끝나 루프가 도는 시각이라
        # 시작 직후에는 아직 없습니다. 요청을 받은 시각은 그와 별개로 답니다.
        return {**bot.trader.status(), "requested_at": bot.started_at.isoformat()}

    def running(self) -> list[int]:
        return sorted(uid for uid, bot in self._bots.items() if bot.alive)

    async def stop(self, user_id: int, wait: float = STOP_GRACE_SECONDS) -> dict:
        """이번 사이클을 끝내고 멈춘다. 보유 포지션은 그대로 둡니다."""
        uid = self._uid(user_id)
        bot = self._bots.get(uid)
        if bot is None or not bot.alive:
            raise NotRunning()
        bot.trader.request_stop()
        if wait > 0:
            await asyncio.wait({bot.task}, timeout=wait)
        stopped = bot.task.done()
        if not stopped:
            log.warning("사용자 %s 봇이 %.0f초 안에 멈추지 않았습니다 — "
                        "현재 사이클이 끝나면 종료됩니다", uid, wait)
        return {"stopping": True, "stopped": stopped,
                "note": "현재 사이클을 마치고 멈춥니다. 보유 포지션은 그대로 둡니다."}

    async def shutdown(self, wait: float = STOP_GRACE_SECONDS) -> None:
        """전부 내린다. 프로세스가 죽기 전에 마지막 상태를 반드시 적게 합니다.

        먼저 모두에게 멈추라고 말한 뒤 함께 기다립니다 — 한 명씩 순서대로
        기다리면 마지막 사람의 여유 시간이 앞사람들의 대기 시간만큼 줄어듭니다.
        """
        alive = [bot for bot in self._bots.values() if bot.alive]
        for bot in alive:
            bot.trader.request_stop()
        if alive:
            await asyncio.wait({bot.task for bot in alive}, timeout=wait)
        for bot in alive:
            if not bot.task.done():
                # 마지막 수단. `LiveTrader.run()` 은 취소를 삼키고 상태를
                # 플러시한 뒤 끝나므로, 포지션 기록은 여기서도 남습니다.
                log.warning("봇 %s 가 제때 멈추지 않아 현재 사이클을 취소합니다",
                            bot.strategy)
                bot.task.cancel()
        if alive:
            await asyncio.wait({bot.task for bot in alive}, timeout=5.0)
        self._bots.clear()
