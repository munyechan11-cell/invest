"""자격증명 보관 — the operator's own keys, on the operator's own machine.

There are no accounts here. One person runs this bot, on their machine, with
their own brokerage keys; there is nothing to log into and nothing stored
anywhere else. The setup flow exists because typing six API keys into a YAML
file by hand is how a character gets transposed and a live order goes to the
wrong account.

Rules this module enforces rather than documents:

* **Secrets are never read back out.** Every API response redacts them. If you
  lose a key you reissue it at the venue; you do not recover it from here.
* **The file is 0600 and gitignored.** Written atomically, so a crash mid-write
  cannot leave a half-file that silently drops a credential.
* **Nothing is trusted until it has been used once.** `verify()` makes a real
  read-only call against each venue, because a key that authenticates is worth
  more than a key that merely looks well-formed.
"""
from __future__ import annotations

import logging
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from quant.core.types import UTC

log = logging.getLogger("quant.credentials")

SECRET_HINT = re.compile(r"(key|secret|token|password|passphrase|appkey)", re.I)


def _parse_value(raw: str) -> str:
    """Strip quotes and trailing comments from a dotenv value.

    `KEY=          # 8-digit account number` must read as empty, not as the
    comment. Getting this wrong is quiet and expensive: the engine boots
    "configured", the venue rejects every request, and the error looks like a
    network fault rather than a config one.

    A `#` inside a quoted value is legitimate and preserved.
    """
    raw = raw.strip()
    if raw.startswith("#"):
        return ""          # the whole "value" was a trailing comment
    if raw[:1] in ("'", '"'):
        quote = raw[0]
        end = raw.find(quote, 1)
        return raw[1:end] if end > 0 else raw[1:]
    # unquoted: a comment must be preceded by whitespace, so URLs with a
    # fragment survive
    cut = re.search(r"\s#", raw)
    if cut:
        raw = raw[: cut.start()]
    return raw.strip()


@dataclass
class VenueSpec:
    """One venue the setup flow knows how to configure."""

    id: str
    label_ko: str
    kind: str                       # equity_kr | equity_us | crypto
    fields: list[tuple[str, str, bool]]      # (env var, 한국어 라벨, required)
    note_ko: str = ""
    paper_supported: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id, "label": self.label_ko, "kind": self.kind,
            "paper_supported": self.paper_supported, "note": self.note_ko,
            "fields": [{"env": e, "label": l, "required": r} for e, l, r in self.fields],
        }


VENUES: list[VenueSpec] = [
    VenueSpec(
        id="kis", label_ko="한국투자증권 (KIS)", kind="equity_kr",
        fields=[
            ("KIS_APP_KEY", "앱 키", True),
            ("KIS_APP_SECRET", "앱 시크릿", True),
            ("KIS_ACCOUNT_NO", "계좌번호 (앞 8자리)", True),
            ("KIS_ACCOUNT_PRD_CD", "상품코드 (보통 01)", False),
        ],
        note_ko="모의투자 환경이 별도로 있어 실계좌와 완전히 분리해 시험할 수 있습니다. "
                "https://apiportal.koreainvestment.com 에서 발급.",
        paper_supported=True,
    ),
    VenueSpec(
        id="toss", label_ko="토스증권", kind="equity_kr",
        fields=[
            ("TOSS_CLIENT_ID", "클라이언트 ID", True),
            ("TOSS_CLIENT_SECRET", "클라이언트 시크릿", True),
            ("TOSS_ACCOUNT_NO", "계좌번호", True),
        ],
        note_ko="⚠️ 토스 Open API에는 모의투자 환경이 없습니다. 따라서 이 엔진의 "
                "dry_run 모드(실시간 시세 + 가상 체결)로 충분히 검증한 뒤에만 "
                "실거래로 넘어가세요. https://corp.tossinvest.com/ko/open-api",
        paper_supported=False,
    ),
    VenueSpec(
        id="alpaca", label_ko="Alpaca (미국주식)", kind="equity_us",
        fields=[
            ("ALPACA_API_KEY", "API 키", True),
            ("ALPACA_SECRET_KEY", "시크릿 키", True),
        ],
        note_ko="페이퍼 계좌가 기본입니다. https://alpaca.markets",
        paper_supported=True,
    ),
    VenueSpec(
        id="binance", label_ko="Binance (코인)", kind="crypto",
        fields=[
            ("BINANCE_KEY", "API 키", True),
            ("BINANCE_SECRET", "시크릿", True),
        ],
        note_ko="키 발급 시 **출금 권한은 반드시 끄세요.** 거래 권한만 있으면 됩니다. "
                "테스트넷을 지원합니다.",
        paper_supported=True,
    ),
    VenueSpec(
        id="upbit", label_ko="업비트 (코인)", kind="crypto",
        fields=[
            ("UPBIT_KEY", "Access 키", True),
            ("UPBIT_SECRET", "Secret 키", True),
        ],
        note_ko="출금 권한 없이 발급하세요. 등록한 IP에서만 동작합니다.",
        paper_supported=False,
    ),
    VenueSpec(
        id="bybit", label_ko="Bybit (코인)", kind="crypto",
        fields=[
            ("BYBIT_KEY", "API 키", True),
            ("BYBIT_SECRET", "시크릿", True),
        ],
        note_ko="출금 권한 없이 발급하세요. 테스트넷을 지원합니다.",
        paper_supported=True,
    ),
]

VENUES_BY_ID = {v.id: v for v in VENUES}

#: non-venue settings the setup flow also writes
OPERATOR_FIELDS = [
    ("OPERATOR_NAME", "이름 (기록용)", False),
    ("ANTHROPIC_API_KEY", "Anthropic API 키 (AI 데스크용, 선택)", False),
    ("TELEGRAM_BOT_TOKEN", "텔레그램 봇 토큰 (알림, 선택)", False),
    ("TELEGRAM_CHAT_ID", "텔레그램 챗 ID (알림, 선택)", False),
    ("QUANT_API_TOKEN", "대시보드 접근 토큰", False),
]


@dataclass
class SetupState:
    configured: bool = False
    operator: str = ""
    venues: list[str] = field(default_factory=list)
    has_llm: bool = False
    has_notifier: bool = False
    has_api_token: bool = False
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "configured": self.configured, "operator": self.operator,
            "venues": self.venues, "has_llm": self.has_llm,
            "has_notifier": self.has_notifier, "has_api_token": self.has_api_token,
            "updated_at": self.updated_at,
        }


class CredentialStore:
    """Reads and writes a dotenv-style file, and never hands secrets back."""

    def __init__(self, path: str | Path = ".env"):
        self.path = Path(path)

    # ── read ─────────────────────────────────────────────────────────────
    def _raw(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        out: dict[str, str] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = _parse_value(value)
        return out

    def present(self, env_var: str) -> bool:
        """Is this credential set — here or already in the environment."""
        return bool(self._raw().get(env_var) or os.environ.get(env_var, "").strip())

    def state(self) -> SetupState:
        raw = self._raw()

        def has(var: str) -> bool:
            return bool(raw.get(var) or os.environ.get(var, "").strip())

        venues = [
            v.id for v in VENUES
            if all(has(env) for env, _, required in v.fields if required)
        ]
        stamp = ""
        if self.path.exists():
            stamp = datetime.fromtimestamp(
                self.path.stat().st_mtime, tz=UTC).isoformat()
        return SetupState(
            configured=bool(venues),
            operator=raw.get("OPERATOR_NAME", "") or os.environ.get("OPERATOR_NAME", ""),
            venues=venues,
            has_llm=has("ANTHROPIC_API_KEY") or has("OPENAI_API_KEY")
            or has("GOOGLE_API_KEY"),
            has_notifier=has("TELEGRAM_BOT_TOKEN") and has("TELEGRAM_CHAT_ID"),
            has_api_token=has("QUANT_API_TOKEN"),
            updated_at=stamp,
        )

    def redacted(self) -> dict[str, str]:
        """Everything the UI is allowed to see: set-or-not, never the value."""
        raw = self._raw()
        out: dict[str, str] = {}
        for key, value in raw.items():
            if SECRET_HINT.search(key) and value:
                out[key] = "설정됨 ••••" + value[-4:] if len(value) > 8 else "설정됨"
            else:
                out[key] = value
        return out

    # ── write ────────────────────────────────────────────────────────────
    def update(self, values: dict[str, str]) -> list[str]:
        """Merge values in, preserving anything already there. Returns keys written.

        An empty string means "leave whatever is already set alone", so the UI
        can submit a form with blank secret fields without wiping them — the
        single most annoying way a settings page loses a credential.
        """
        raw = self._raw()
        written: list[str] = []
        for key, value in values.items():
            key = key.strip()
            if not key or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                log.warning("자격증명 키 형식이 올바르지 않아 무시: %r", key)
                continue
            value = (value or "").strip()
            if not value:
                continue
            raw[key] = value
            written.append(key)
        if written:
            self._write(raw)
            # Make them live in this process too, so a restart is not required.
            for key in written:
                os.environ[key] = raw[key]
        return written

    def remove(self, keys: list[str]) -> list[str]:
        raw = self._raw()
        removed = [k for k in keys if raw.pop(k, None) is not None]
        if removed:
            self._write(raw)
            for k in removed:
                os.environ.pop(k, None)
        return removed

    def _write(self, values: dict[str, str]) -> None:
        """Atomic, 0600. A half-written credential file is worse than none."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = [
            "# quant engine credentials — 이 파일은 절대 커밋하지 마세요.",
            "# 대시보드 설정 화면에서 생성/갱신됩니다.",
            "",
        ]
        for key in sorted(values):
            body.append(f"{key}={values[key]}")
        text = "\n".join(body) + "\n"

        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent or "."), prefix=".env.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)      # 0600
            os.replace(tmp, self.path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
        log.info("자격증명 %d건 저장 (%s, 권한 0600)", len(values), self.path)

    # ── verification ─────────────────────────────────────────────────────
    async def verify(self, venue_id: str) -> dict:
        """Make one real read-only call. A key that authenticates beats a key
        that merely looks well-formed."""
        spec = VENUES_BY_ID.get(venue_id)
        if spec is None:
            return {"ok": False, "error": f"알 수 없는 거래소: {venue_id}"}
        missing = [env for env, _, required in spec.fields
                   if required and not self.present(env)]
        if missing:
            return {"ok": False, "error": f"미입력 항목: {', '.join(missing)}"}

        try:
            if venue_id == "kis":
                from quant.data.providers.kis import kis_token

                await kis_token(os.environ["KIS_APP_KEY"],
                                os.environ["KIS_APP_SECRET"], paper=True)
                return {"ok": True, "detail": "모의투자 토큰 발급 성공"}

            if venue_id == "toss":
                from quant.brokerage.toss_broker import toss_token

                await toss_token(os.environ["TOSS_CLIENT_ID"],
                                 os.environ["TOSS_CLIENT_SECRET"])
                return {"ok": True, "detail": "OAuth 토큰 발급 성공 (모의투자 환경 없음)"}

            if venue_id == "alpaca":
                import httpx

                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(
                        "https://paper-api.alpaca.markets/v2/account",
                        headers={"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
                                 "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]},
                    )
                r.raise_for_status()
                return {"ok": True, "detail": "페이퍼 계좌 조회 성공"}

            # crypto venues all go through ccxt
            import ccxt.async_support as ccxt_async

            key_env, secret_env = [f[0] for f in spec.fields[:2]]
            ex = getattr(ccxt_async, venue_id)({
                "apiKey": os.environ[key_env], "secret": os.environ[secret_env],
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
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def load_env_file(path: str | Path = ".env", override: bool = False) -> list[str]:
    """`.env` 를 프로세스 환경으로 올린다. 적용된 키 목록을 돌려준다.

    설정 화면이 `.env` 에 쓰는데 시작할 때 아무도 읽지 않으면, 사용자는 키를 다
    입력하고도 "환경변수가 필요합니다" 를 보게 됩니다. 조용하고 비싼 실패라
    엔트리포인트마다 이 함수를 호출합니다.

    같은 파서(`_parse_value`)를 쓰는 것이 중요합니다. 쓰는 쪽과 읽는 쪽이 서로
    다른 규칙을 가지면 인라인 주석 하나로 계좌번호가 주석 문자열이 됩니다.

    기본적으로 **이미 설정된 환경변수가 우선**입니다. 배포 환경에서 진짜 환경변수로
    주입한 값을 저장소에 남아 있던 오래된 파일이 덮어쓰면 안 되기 때문입니다.
    """
    store = CredentialStore(path)
    if not store.path.exists():
        return []
    applied: list[str] = []
    for key, value in store._raw().items():
        if not value:
            continue
        if not override and os.environ.get(key):
            continue
        os.environ[key] = value
        applied.append(key)
    if applied:
        log.info("%s 에서 설정 %d건 로드", store.path, len(applied))
    return applied


def venue_catalog() -> list[dict]:
    return [v.to_dict() for v in VENUES]
