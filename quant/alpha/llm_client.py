"""Minimal multi-provider LLM client for the research council.

Deliberately dependency-light: one `httpx` call per provider rather than three
vendor SDKs, because the council only ever needs "send messages, get structured
JSON back". Structured output uses each provider's native mechanism (Anthropic
tool-use, OpenAI json_schema, Gemini response_schema) so the council never has
to regex a JSON blob out of prose.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import itertools
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from quant.core.aio import LazyLock
from quant.core.types import one_line_error

log = logging.getLogger("quant.alpha.llm")

# Newest Claude generation. Override per-config if you need a cheaper tier for
# the high-volume analyst roles.
# Aliases where the provider offers one. A pinned name that 404s is a worse
# default than an alias that quietly tracks the current release: the pinned one
# fails for every user the moment the provider retires it, and the failure
# reads as a config error rather than a stale default.
DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "openai": "gpt-5.5",
    "google": "gemini-pro-latest",
    # Jev 가 응답에 스스로 밝히는 모델 이름. 고를 수 있는 다른 모델은 없습니다.
    "jev": "typesafe-ai/jev",
}


#: USD per 1M tokens (input, output), matched by name prefix. 2026-08.
#: Longest prefix wins, so a family entry can carry a whole generation.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus": (5.00, 25.00),
    "claude-sonnet": (3.00, 15.00),
    "claude-haiku": (1.00, 5.00),
    "gemini-3.1-pro": (2.00, 12.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.7-flash": (0.75, 3.75),
    "gpt-5": (1.25, 10.00),
    # Jev: 입력 1M 토큰당 $0.042, 출력은 무료 — 운영자가 알려 준 단가(2026-09-24).
    # 출력이 공짜인 이유는 Jev 가 글이 아니라 확률만 돌려주기 때문입니다. 단가가
    # 바뀌면 여기를 고치세요. 이 표에 없으면 `_FALLBACK_PRICE`(가장 비싼 요율)로
    # 매겨져 `cost_limit_usd` 가 수백 배 일찍 걸립니다.
    "typesafe-ai/jev": (0.042, 0.0),
}

#: An unknown model is priced at the most expensive thing we know about. The
#: estimate drives `cost_limit_usd`, and a limit that under-counts spend is
#: worse than one that stops you early.
_FALLBACK_PRICE = (5.00, 25.00)


def price_for(model: str) -> tuple[float, float]:
    """(input, output) USD per 1M tokens."""
    name = (model or "").lower()
    best = ""
    for prefix in MODEL_PRICES:
        if name.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return MODEL_PRICES[best] if best else _FALLBACK_PRICE


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    #: which model these tokens were spent on — set by the client that owns
    #: this counter. Cost lives here rather than in the desk because this is
    #: the only object that knows both halves of the multiplication.
    model: str = ""
    #: 실패 뒤 다시 보낸 횟수(`complete()` 의 재시도). 재시도가 결국 성공하면
    #: 호출 수에도 좌석 실패에도 남지 않아서, 흔들리는 제공자가 보이지 않았습니다.
    retries: int = 0

    def add(self, i: int, o: int, latency_ms: float = 0.0) -> None:
        self.input_tokens += i
        self.output_tokens += o
        self.calls += 1
        tally = _TALLY.get()
        if tally is not None:
            pin, pout = price_for(self.model)
            tally.record(i, o, i / 1e6 * pin + o / 1e6 * pout, latency_ms)

    def retry(self) -> None:
        self.retries += 1
        tally = _TALLY.get()
        if tally is not None:
            tally.record_retry()

    @property
    def cost_usd(self) -> float:
        pin, pout = price_for(self.model)
        return self.input_tokens / 1e6 * pin + self.output_tokens / 1e6 * pout


class UsageTally:
    """**이 작업이** 쓴 LLM 사용량 — 심의 한 번, 봉 한 번, 요청 한 번.

    `LLMUsage` 는 클라이언트 하나의 누적이라, 앞뒤 값을 빼서 "이번에 쓴 것"
    을 구하면 **동시에 도는 다른 작업의 호출까지** 섞입니다. 데스크는 한
    클라이언트로 종목 4개를 동시에 심의하므로, 종목마다 적힌 호출 수가 16이
    아니라 64였습니다. 그래서 작업마다 따로 셉니다: `usage_tally()` 로 연
    작업과 그 작업이 만든 태스크(`asyncio.gather`·`wait_for` 는 컨텍스트를
    물려받습니다)의 호출만 여기에 적힙니다. 안쪽에서 다시 열면 바깥에도
    같이 적힙니다 — 봉 전체의 합과 종목 하나의 몫을 함께 셀 수 있게.
    """

    __slots__ = ("calls", "input_tokens", "output_tokens", "cost_usd", "retries",
                 "latency_ms", "latency_max_ms", "_parent")

    def __init__(self, parent: UsageTally | None = None):
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0
        #: 재시도 횟수 — 이 작업의 소요 시간 가운데 백오프 대기가 있었는가.
        self.retries = 0
        #: 제공자가 스스로 밝힌 처리 시간(Jev 의 `latency_ms`)의 합과 최댓값.
        #: 벽시계 시간에서 이것을 빼면 전송·대기 몫이 보입니다.
        self.latency_ms = 0.0
        self.latency_max_ms = 0.0
        self._parent = parent

    def record(self, i: int, o: int, cost: float, latency_ms: float = 0.0) -> None:
        tally: UsageTally | None = self
        while tally is not None:
            tally.calls += 1
            tally.input_tokens += i
            tally.output_tokens += o
            tally.cost_usd += cost
            tally.latency_ms += latency_ms
            tally.latency_max_ms = max(tally.latency_max_ms, latency_ms)
            tally = tally._parent

    def record_retry(self) -> None:
        tally: UsageTally | None = self
        while tally is not None:
            tally.retries += 1
            tally = tally._parent


_TALLY: contextvars.ContextVar = contextvars.ContextVar("llm_usage_tally", default=None)


@contextlib.contextmanager
def usage_tally():
    """이 블록(과 여기서 만든 태스크)이 쓴 LLM 사용량을 센다."""
    tally = UsageTally(_TALLY.get())
    token = _TALLY.set(tally)
    try:
        yield tally
    finally:
        _TALLY.reset(token)


@dataclass
class LLMConfig:
    provider: str = "anthropic"
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.2
    max_tokens: int = 2048
    timeout: float = 120.0
    max_retries: int = 3
    #: 분당 요청 상한. 0 이면 제한 없음. 무료 티어는 대개 5~15 RPM 이라,
    #: 16석 데스크가 한 번에 19번 호출하면 절반이 429 로 떨어진다.
    requests_per_minute: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def resolved_model(self) -> str:
        return self.model or DEFAULT_MODELS.get(self.provider, "")

    def resolved_key(self) -> str:
        if self.api_key:
            return self.api_key
        return os.environ.get(KEY_ENV.get(self.provider, ""), "")


#: 제공자 → 키를 읽는 환경 변수(설정 화면의 칸 이름과 같습니다).
KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "jev": "JEV_API_KEY",
}


#: 제공자별 "여기서 충전/발급하세요". 이름과 주소가 한곳에 있어야 합니다 —
#: 데스크는 이 중 아무거나로 돌 수 있는데, 안내문이 한 곳을 박아 두고 있으면
#: 제미나이로 돌리는 사람이 없는 Anthropic 계정을 충전하러 갑니다.
BILLING: dict[str, tuple[str, str]] = {
    "anthropic": ("Anthropic", "console.anthropic.com 의 Plans & Billing"),
    "openai": ("OpenAI", "platform.openai.com 의 Billing"),
    "google": ("Google AI Studio", "aistudio.google.com/app/apikey "
                                   "(무료 티어는 하루 할당량이 있습니다)"),
    # 결제 화면 주소를 모릅니다. 모르는 주소를 지어내느니 이름만 둡니다.
    "jev": ("Jev", ""),
}


def billing_hint(provider: str) -> tuple[str, str]:
    """`(제공자 이름, 어디서 해결하는가)`. 모르는 제공자는 이름만 돌려줍니다."""
    return BILLING.get(provider, (provider or "LLM 제공자", ""))


class LLMError(RuntimeError):
    pass


class QuotaExhausted(LLMError):
    """A 429 that will not clear in a useful timeframe.

    Distinct from ordinary throttling because the response is different: a
    per-second burst limit wants a short sleep, an exhausted daily allowance
    wants the caller to stop entirely. Retrying the latter burns the deadline
    and then fails anyway — which is exactly what a sixteen-seat desk did for
    ten minutes before this existed.
    """


class MissingKey(LLMError):
    """이 제공자의 키가 아예 없다(설정에도 환경 변수에도).

    클라이언트를 만들 때 나는 `LLMError` 는 이것만이 아닙니다 — 잘못 적은
    `llm.extra.undecided_below` 도 시작할 때 거절됩니다. 봇 시작 화면은 모든
    `LLMError` 를 "쓸 수 있는 키가 없습니다" 로 적어서, 값을 잘못 적은 사람이
    멀쩡한 키를 확인하러 갔습니다. 이 형으로 둘을 가릅니다.
    """


class UnsendableKey(LLMError):
    """키에 HTTP 헤더로 보낼 수 없는 글자가 있다. 요청은 나가지 않았습니다.

    "키가 거부되었다" 와 다릅니다 — 서버는 이 키를 본 적이 없습니다. 사전
    점검은 이 구분으로 "다시 붙여 넣으세요" 라고 말합니다.
    """


def _header_key(config: LLMConfig, tag: str) -> str:
    """헤더에 실을 키. ASCII 가 아닌 글자가 있으면 **보내기 전에** 꼬리표 달린 401.

    httpx 는 헤더를 ASCII 로 인코딩합니다. 채팅 앱·문서에서 붙여 넣은 키에
    섞인 보이지 않는 공백(U+200B)이나 둥근 따옴표는 `strip()` 으로 지워지지
    않고, 요청이 나가기도 전에 `UnicodeEncodeError` 가 났습니다. 그 예외는
    `LLMError` 가 아니라서 `complete()` 의 분류를 건너뛰었고, 사전 점검은
    "'ascii' codec can't encode … position 20"(앞의 'Bearer ' 까지 센 위치)만
    말했으며, `/api/evaluate` 는 키를 말하지 않는 502 였습니다.

    401 은 재시도 대상이 아니고(`_NO_RETRY_STATUS`), 요청을 보내지 않았으니
    청구도 없습니다. 글자 **위치와 코드포인트만** 적습니다 — 키의 다른 글자는
    적지 않습니다.
    """
    key = config.resolved_key()
    for i, ch in enumerate(key):
        if not ch.isascii():
            name = KEY_ENV.get(config.provider) or "API 키"
            raise UnsendableKey(
                f"{tag} 401: {name} 의 {i + 1}번째 글자가 ASCII 가 아닙니다"
                f"(U+{ord(ch):04X} — 보이지 않는 공백·둥근 따옴표 등). 요청은 "
                f"보내지 않았습니다 — 키를 다시 붙여 넣으세요")
    return key


def _raise_for_status(response: httpx.Response, provider: str) -> None:
    """Surface the provider's own explanation.

    `raise_for_status()` alone gives "400 Bad Request" and throws away the body,
    which is the only part that says *what* was wrong. Debugging a desk where
    all sixteen seats fail identically is impossible without it.
    """
    if response.status_code < 400:
        return
    detail = ""
    try:
        payload = response.json()
        err = payload.get("error") or payload
        detail = err.get("message") or json.dumps(err, ensure_ascii=False)[:400]
    except Exception:
        detail = response.text[:400]
    raise LLMError(f"{provider} {response.status_code}: {detail}")


_RETRY_HINT = re.compile(r"retry in ([\d.]+)\s*(ms|s)\b|retryDelay[\"':\s]+([\d.]+)s",
                         re.I)


def _retry_after(message: str) -> float | None:
    """Pull the provider's suggested wait out of a 429 message."""
    m = _RETRY_HINT.search(message)
    if not m:
        return None
    if m.group(3):
        return min(float(m.group(3)), 60.0)
    value = float(m.group(1))
    return min(value / 1000.0 if m.group(2).lower() == "ms" else value, 60.0)


#: a suggested wait beyond this is not throttling, it is an exhausted allowance
_LONG_WAIT_S = 25.0

#: `LLMError` 의 상태 꼬리표 — "google 429: …", "jev 503: …". 글의 **맨 앞** 만.
_STATUS_TAG = re.compile(r"^[A-Za-z_][\w.-]* (\d{3}):")

#: 다시 보내도 같은 답이 오는 상태. 재시도하지 않습니다.
_NO_RETRY_STATUS = frozenset({400, 401, 403, 404, 422})


def _status_of(message: str) -> int | None:
    m = _STATUS_TAG.match(message)
    return int(m.group(1)) if m else None


def failure_status(exc: BaseException) -> int | None:
    """`complete()` 가 올린 실패의 상태 꼬리표. 없으면 None.

    재시도 끝의 실패는 "LLM call failed after N attempts: …" 로 한 번 싸여
    올라옵니다(원인은 `__cause__`). 그 글 **안** 에서 " 401:" 을 찾으면
    "jev 503: Upstream provider returned 401: …" 같은 일시 장애가 키 거절로
    읽힙니다. 그래서 싼 글이면 원인의 **맨 앞** 꼬리표만 읽습니다 —
    `complete()` 가 재시도 여부를 정할 때와 같은 규칙입니다.
    """
    status = _status_of(str(exc))
    cause = exc.__cause__
    if status is None and isinstance(cause, LLMError):
        status = _status_of(str(cause))
    return status


def _describe(exc: BaseException | None) -> str:
    """마지막 실패를 한 줄로. `str()` 이 빈 예외(httpx 시간 초과)는 이름이라도.

    예전에는 `str(last)` 만 붙여, 느린 Jev 가 "LLM call failed after 3 attempts: "
    로 끝나는 빈 문장을 로그와 좌석 `error` 칸에 남겼습니다 — 시간 초과인지
    다른 실패인지 알 수 없었습니다. 우리 오류(`LLMError`)는 꼬리표가 이미 원인을
    말하므로 그대로 둡니다.
    """
    if exc is None:
        return "알 수 없는 오류"
    if isinstance(exc, LLMError) and str(exc).strip():
        return str(exc)
    text = " ".join(str(exc).split())
    name = type(exc).__name__
    return f"{name}: {text}" if text else name


#: 다시 시도해도 **오늘 안에는 풀리지 않는** 429 들.
#:
#: 앞의 넷은 하루 할당량, 뒤의 셋은 **돈** 입니다. 돈 쪽이 빠져 있어서 실제로
#: 이런 일이 있었습니다 — 구글이 "monthly spending cap" 을 돌려주는데 그것이
#: 평범한 혼잡으로 분류되어, 데스크가 **사흘 내내** 사이클마다 열아홉 번씩
#: 실패하고 그때마다 "분석가 합의 대체" 로 물러섰습니다. 화면에는 판정이
#: 정상처럼 떴고, 그게 축약본이라는 사실은 작은 표 하나뿐이었습니다.
#:
#: 대기 시간으로도 못 걸렀습니다. 한도 초과 응답에는 `retryDelay` 가 아예
#: 없어서 "얼마나 기다리면 되나" 로는 판정할 수 없습니다.
_TERMINAL_429 = (
    "per day", "perday", "daily", "quota_exceeded",
    "spending cap",              # 프로젝트 월 지출 한도
    "credits are depleted",      # 선불 잔액 소진
    "billing account",           # 결제 계정 자체의 문제
)


def _is_long_exhaustion(message: str) -> bool:
    if any(w in message.lower() for w in _TERMINAL_429):
        return True
    wait = _retry_after(message)
    return wait is not None and wait >= _LONG_WAIT_S


class Truncated(LLMError):
    """The model hit the output cap mid-answer.

    Worth its own type because the response is *correct so far* — retrying the
    identical request reproduces the identical truncation, so the generic retry
    path burns the call three times and fails anyway. The fix is a bigger
    budget, which only the caller can grant.
    """


#: what each provider calls "I ran out of room".
_TRUNCATION_MARKERS = {"max_tokens", "length", "MAX_TOKENS"}


def _check_truncated(reason: str | None, text: str) -> None:
    if reason and reason in _TRUNCATION_MARKERS:
        raise Truncated(f"출력 토큰 한도에서 잘렸습니다 ({reason}): …{text[-80:]!r}")


def _extract_json(text: str) -> dict:
    """Last-resort parser for providers/models that ignore the schema."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"model did not return JSON: {text[:400]}") from exc


# ── Jev 전송 (MCP streamable HTTP, SDK 없이 httpx 만) ────────────────────────
#: `LLMConfig.base_url` 이 비어 있을 때의 주소.
JEV_DEFAULT_URL = "https://jev-mcp-rose.vercel.app/api/mcp"
#: 우리가 제안하는 MCP 버전. 서버가 initialize 에서 다른 값을 고르면 그 값을 씁니다.
MCP_PROTOCOL_VERSION = "2025-06-18"


def _sse_messages(text: str):
    """`text/event-stream` 본문 → 이벤트별 `data:` 를 이은 문자열들."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for block in text.split("\n\n"):
        lines = [line[5:] for line in block.split("\n") if line.startswith("data:")]
        # SSE 규칙: `data:` 뒤의 공백 한 칸은 구분자이지 값이 아닙니다.
        lines = [line[1:] if line.startswith(" ") else line for line in lines]
        if lines:
            yield "\n".join(lines)


def _jev_message(response: httpx.Response, request_id: int) -> dict:
    """응답에서 **이 요청의** JSON-RPC 메시지를 꺼낸다.

    SSE 로 오면 진행 알림 같은 다른 메시지가 섞일 수 있어 id 로 고릅니다.
    """
    ctype = response.headers.get("content-type", "").lower()
    if ctype.startswith("text/event-stream"):
        for data in _sse_messages(response.text):
            try:
                message = json.loads(data)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                return message
        raise LLMError(f"jev: SSE 응답에 요청 {request_id} 의 답이 없습니다: "
                       f"{response.text[:200]}")
    try:
        message = response.json()
    except ValueError as exc:
        raise LLMError(f"jev: JSON 이 아닌 응답 ({response.status_code}): "
                       f"{response.text[:200]}") from exc
    if isinstance(message, list):                      # JSON-RPC 배치
        message = next((m for m in message
                        if isinstance(m, dict) and m.get("id") == request_id), None)
    if not isinstance(message, dict):
        raise LLMError(f"jev: JSON-RPC 메시지가 아닙니다: {response.text[:200]}")
    return message


def _rpc_error_text(error: Any) -> str:
    if isinstance(error, dict):
        return str(error.get("message") or json.dumps(error, ensure_ascii=False))
    return str(error)


def _jev_error_text(response: httpx.Response) -> str:
    """HTTP 오류 본문의 설명. JSON-RPC 오류든 `{"error": "missing_token"}` 이든."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:400]
    if isinstance(payload, dict) and "error" in payload:
        return _rpc_error_text(payload["error"])
    return response.text[:400]


#: 이 말이 들어간 거절은 요청이 틀린 게 아니라 **세션이 사라진** 것입니다.
_SESSION_WORDS = ("session", "not initialized", "not initialised")


def _session_lost(text: str) -> bool:
    lowered = text.lower()
    return any(w in lowered for w in _SESSION_WORDS)


# ── Jev 오류 → 상태 꼬리표 ───────────────────────────────────────────────────
# 꼬리표가 `complete()` 의 행동을 정합니다: 422 는 바로 실패, 429 는 한도 경로
# (기다리거나 `QuotaExhausted` 로 데스크 전체를 멈춤), 503 은 잠깐의 장애로 보고
# 다시 시도. 전에는 Jev 가 돌려준 오류를 **전부** 422 로 적었습니다. 그러면
# 게이트웨이의 일시 장애 한 번이 헤드 좌석을 재시도 없이 떨어뜨리고, 데스크는
# 분석가 합의로 물러서 보유를 팔았습니다 — 같은 장애가 HTTP 503 으로 왔으면 한 번
# 더 묻고 관망했을 자리입니다. 하루 한도 소진도 평범한 오류로 읽혀, 데스크가
# 멈추지 않고 봉마다 16번씩 실패했습니다.

#: 요청 자체가 틀렸다는 JSON-RPC 오류 코드 — 같은 요청을 다시 보내도 같습니다.
#: 파싱 불가(-32700), 잘못된 요청(-32600), 없는 메서드(-32601), 잘못된 인자(-32602).
#: 나머지 — 내부 오류(-32603), 서버 오류(-32000~-32099), 모르는 코드 — 는 서버
#: 사정일 수 있어 다시 물을 가치가 있습니다.
_JEV_BAD_REQUEST_CODES = frozenset({-32700, -32600, -32601, -32602})

#: **돈** 이 떨어졌다는 말 — 잔액·결제·지출 한도. 기다려도 풀리지 않으므로
#: 곧바로 `QuotaExhausted`("jev 402") 로 데스크를 세웁니다. 전에는 이 말들이
#: 평범한 429 로 적혀 세 번씩 재시도된 뒤 좌석 실패로 끝났고, 데스크는 켜진
#: 채로 봉마다 16석 × 3번을 다시 실패했습니다. 분석가가 답한 **뒤에** 잔액이
#: 떨어지면 헤드만 실패해 분석가 합의로 물러서고, 그 합의가 매도였습니다.
_JEV_BILLING_WORDS = ("credit", "billing", "payment", "insufficient funds",
                      "spending cap")

#: 할당량이 찼다는 말. Jev 에는 알려진 분 단위 할당량이 없어, 짧은 창을 말하지
#: 않는 한 "오늘 안에는 안 풀리는" 쪽으로 읽습니다("Quota exceeded for this
#: token"). 제미나이가 쓰는 공용 `_TERMINAL_429` 에는 넣지 않습니다 — 거기서는
#: "Quota exceeded … per minute" 가 흔한 **일시** 429 입니다.
_JEV_QUOTA_WORDS = ("quota", "per day", "daily")

#: 잠깐의 속도 제한. 무엇이 긴 대기인지는 `_is_long_exhaustion` 이 정합니다.
_JEV_THROTTLE_WORDS = ("rate limit", "rate-limit", "rate_limit", "ratelimit",
                       "too many requests")

#: 한도 문구가 **짧은 창** 을 말하면 기다리면 풀립니다 — 데스크를 세울 일이 아닙니다.
#:
#: 밑줄·하이픈을 공백으로 바꾼 글에서 찾습니다. 예전 목록("per minute",
#: "/minute" …)은 Vertex 의 `generate_content_requests_per_minute_per_project`
#: 나 "tokens per min (TPM)" 을 못 알아봐서, 분 단위 한도가 하루 한도처럼
#: `QuotaExhausted` 가 되어 데스크가 재시작할 때까지 꺼졌습니다. 낱말 경계를
#: 봅니다 — "/min" 이 주소 속 "/minimum" 에, "/sec" 이 "/secure" 에 걸리면 진짜
#: 잔액 소진이 일시 제한으로 읽힙니다(헤드에서 그것은 합의 대체 → 매도입니다).
_SHORT_WINDOW = re.compile(r"(?:\bper |/)(?:min(?:ute)?|sec(?:ond)?)s?\b"
                           r"|\b(?:rpm|tpm)\b")

#: 도구 오류(`isError`) 가운데 **입력이 틀렸다** 는 말. 도구 오류에는 코드가
#: 없어서 글로 봅니다. SDK 는 입력 검증 실패를 "MCP error -32602: …" 로 적습니다.
#:
#: **명시적인 표지만** 둡니다. 예전에는 "too long", "exceeds", "validation",
#: "must be" 같은 넓은 말이 있어서 "The upstream model took too long to respond,
#: please try again" 이나 "output validation failed, retry" 같은 **일시 장애** 가
#: 422(재시도 없음)로 읽혔습니다. 헤드에서 그 한 번이 분석가 합의 대체 → 매도
#: 였습니다. 틀리는 방향의 값이 다릅니다 — 입력 오류를 503 으로 읽으면 재시도
#: 두 번이 더 들 뿐이고, 일시 장애를 422 로 읽으면 보유가 팔립니다.
_JEV_BAD_INPUT_WORDS = ("mcp error -32700", "mcp error -32600", "mcp error -32601",
                        "mcp error -32602", "invalid argument", "unknown tool")


def _short_window(text: str) -> bool:
    normalised = text.lower().replace("_", " ").replace("-", " ")
    if _SHORT_WINDOW.search(normalised):
        return True
    wait = _retry_after(text)
    return wait is not None and wait < _LONG_WAIT_S


def _jev_failure(text: str, *, bad_request: bool, status: int | None = None) -> LLMError:
    """Jev 가 돌려준 오류에 `complete()` 가 읽는 상태 꼬리표를 붙인다.

    돈·할당량 소진은 여기서 곧바로 `QuotaExhausted` 입니다(`complete()` 가
    재시도하지 않고 그대로 올립니다). `status` 는 HTTP 로 온 402·429 일 때.
    """
    lowered = text.lower()
    body = text[:400]
    short = _short_window(text)
    if status == 402 or (not short and any(w in lowered for w in _JEV_BILLING_WORDS)):
        return QuotaExhausted(f"jev 402: {body}")
    if not short and any(w in lowered for w in _JEV_QUOTA_WORDS):
        return QuotaExhausted(f"jev 429: {body}")
    if status == 429 or any(w in lowered for w in (*_JEV_THROTTLE_WORDS, *_JEV_QUOTA_WORDS,
                                                   *_JEV_BILLING_WORDS)):
        return LLMError(f"jev 429: {body}")
    return LLMError(f"jev {422 if bad_request else 503}: {body}")


def _redirect_target(location: str) -> str:
    """옮겨 간 곳의 **호스트까지만**. 경로와 쿼리는 옮기지 않습니다.

    `Location` 에 무엇이 실려 올지 모릅니다(토큰이 든 쿼리 등). 운영자에게
    필요한 것은 "https 로 옮겨 갔다", "다른 도메인이다" 정도라 거기서 끊습니다.
    """
    parts = urlsplit(location or "")
    if not parts.netloc:
        return "같은 호스트의 다른 경로" if location else "?"
    host = parts.hostname or "?"
    if parts.port:
        host = f"{host}:{parts.port}"
    return f"{parts.scheme}://{host}" if parts.scheme else host


def _jev_raise_for_status(response: httpx.Response) -> None:
    """HTTP 오류 → 꼬리표. 402·429 는 본문을 보고 한도 경로로 보냅니다.

    **3xx 도 오류입니다.** httpx 는 리디렉션을 따라가지 않고(따라가도 다른
    호스트로 가면 Authorization 을 떼어 냅니다), `_raise_for_status` 는 400
    아래를 통과시켰습니다. 그러면 308 의 빈 본문을 JSON-RPC 로 읽다가 꼬리표
    없는 오류가 나서 일시 장애처럼 세 번 재시도했고, 사전 점검은 "JSON 이 아닌
    응답 (308): " 로 끝났습니다 — 어디로 옮겨 갔는지는 말하지 않고. 같은
    주소로 다시 보내면 같은 3xx 가 오므로 "jev 404"(재시도 없음)로 적어,
    사전 점검이 llm.base_url 을 가리키게 합니다.
    """
    status = response.status_code
    if 300 <= status < 400:
        where = _redirect_target(response.headers.get("location", ""))
        raise LLMError(f"jev 404: 엔드포인트가 옮겨졌습니다 ({status} → {where}) — "
                       "llm.base_url 을 확인하세요")
    if status in (402, 429):
        raise _jev_failure(_jev_error_text(response), bad_request=False,
                           status=response.status_code)
    _raise_for_status(response, "jev")


def _session_lost_failure(response: httpx.Response, where: str) -> LLMError:
    """세션 유실 → "jev 503". 요청이 틀린 게 아니라 서버가 세션을 잊은 것입니다.

    `_raise_for_status` 로 보내면 "jev 404"·"jev 400" 이 되어 `complete()` 가
    재시도하지 않습니다(`_NO_RETRY_STATUS`).
    """
    return LLMError(f"jev 503: 세션을 잃었습니다 ({where}, HTTP {response.status_code}): "
                    f"{_jev_error_text(response)[:300]}")


def _shared_failure(exc: Exception) -> LLMError:
    """잠금을 기다리던 좌석에게 앞 좌석의 세션 열기 실패를 **새 예외로** 건넨다.

    같은 예외 객체를 여러 태스크에서 다시 올리면 traceback 이 서로 이어 붙습니다.
    꼬리표는 그대로 둡니다 — 키 거절(401)이면 기다린 좌석도 재시도하지 않고,
    시간 초과·일시 장애면 기다린 좌석도 백오프 뒤 다시 묻습니다.
    """
    if isinstance(exc, LLMError):
        try:
            return type(exc)(str(exc))
        except Exception:  # noqa: BLE001 — 생성자가 다른 하위 형
            return LLMError(str(exc))
    return LLMError(f"jev 503: 세션을 열지 못했습니다 — {_describe(exc)}")


def _rpc_failure(error: Any, prefix: str = "") -> LLMError:
    """JSON-RPC `error` → 꼬리표. 요청이 틀렸는지는 코드로만 봅니다."""
    code = error.get("code") if isinstance(error, dict) else None
    return _jev_failure(prefix + _rpc_error_text(error),
                        bad_request=isinstance(code, int)
                        and code in _JEV_BAD_REQUEST_CODES)


def _tool_failure(text: str) -> LLMError:
    """도구의 `isError` 결과 → 꼬리표. 코드가 없어 글로 봅니다."""
    lowered = text.lower()
    return _jev_failure(text, bad_request=any(w in lowered for w in _JEV_BAD_INPUT_WORDS))


def _as_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


class _RateLimiter:
    """Simple pacer. Spreads requests evenly rather than firing a burst.

    A burst is what actually trips a free-tier quota: the limit is per minute,
    but sixteen concurrent calls arrive in the same second and most of them are
    rejected. Pacing them costs the same wall-clock minute and loses nothing.
    """

    def __init__(self, per_minute: float):
        self._interval = 60.0 / per_minute if per_minute > 0 else 0.0
        self._next = 0.0
        self._lock = LazyLock()

    async def wait(self) -> None:
        if self._interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = max(now, self._next) + self._interval


class LLMClient:
    """One client, four wire protocols.

    The fourth, Jev, is not a text model: it answers typed questions with
    probabilities, so `_jev` asks it narrow questions and `quant.alpha.jev`
    turns the answers into the caller's schema. The contract the desk sees —
    `complete()` and `usage` — is the same for all four.
    """

    def __init__(self, config: LLMConfig):
        self.config = config
        self.usage = LLMUsage()
        self._limiter = _RateLimiter(config.requests_per_minute)
        self.usage.model = config.resolved_model()
        self._client = httpx.AsyncClient(timeout=config.timeout)
        if not config.resolved_key():
            raise MissingKey(
                f"no API key for provider {config.provider!r} — set the matching env var"
            )
        # ── Jev(MCP) 세션 ──
        # 데스크는 이 클라이언트 하나로 좌석 여럿을 **동시에** 부릅니다. 세션을
        # 좌석마다 열면 16석 × 종목 수만큼 initialize 가 나가고, 서버가 어느
        # 세션을 기억할지도 알 수 없습니다. 그래서 처음 한 번만(single-flight)
        # 열고 모두가 같이 씁니다. 세대 번호는 "내가 쓰던 세션이 죽었다" 는
        # 신고가 여럿 겹쳐도 다시 여는 것은 한 번이게 하려고 둡니다.
        self._jev_lock = LazyLock()
        self._jev_ready = False
        self._jev_session = ""
        self._jev_protocol = ""
        self._jev_generation = 0
        # 끝난 세션 열기 시도의 수와 마지막 실패. 잠금을 **기다리는 동안** 앞
        # 시도가 실패했으면 그 실패를 같이 받습니다(`_jev_connect`).
        self._jev_connects_done = 0
        self._jev_connect_error: Exception | None = None
        self._jev_ids = itertools.count(1)
        self._undecided_below = 0.65
        if config.provider == "jev":
            from quant.alpha.jev import DEFAULT_UNDECIDED_BELOW, undecided_threshold
            # 잘못 적은 설정은 첫 심의가 아니라 시작할 때 드러나야 합니다.
            self._undecided_below = undecided_threshold(
                (config.extra or {}).get("undecided_below", DEFAULT_UNDECIDED_BELOW))

    async def complete(self, system: str, user: str, schema: dict | None = None) -> Any:
        """Return parsed JSON when `schema` is given, else raw text."""
        last: Exception | None = None
        budget = self.config.max_tokens
        for attempt in range(self.config.max_retries):
            await self._limiter.wait()
            try:
                if self.config.provider == "anthropic":
                    return await self._anthropic(system, user, schema, budget)
                if self.config.provider in ("openai", "openai_compatible"):
                    return await self._openai(system, user, schema, budget)
                if self.config.provider == "google":
                    return await self._google(system, user, schema, budget)
                if self.config.provider == "jev":
                    return await self._jev(system, user, schema)
                raise LLMError(f"unsupported provider {self.config.provider!r}")
            except Truncated as exc:
                last = exc
                # Retrying the same budget reproduces the same cut. Give it
                # room instead — but cap the growth, because a model that
                # rambles past 4x the budget is not going to stop at 8x.
                if budget >= self.config.max_tokens * 4:
                    raise
                budget *= 2
                log.info("%s 응답이 잘려 출력 한도를 %d 토큰으로 올려 재시도합니다",
                         self.config.resolved_model(), budget)
                continue
            except QuotaExhausted:
                # 이미 "오늘 안에는 안 풀린다" 로 판정된 실패(Jev 의 잔액·할당량).
                raise
            except (httpx.HTTPError, LLMError) as exc:
                last = exc
                # A 4xx is a bad request, not a blip. Retrying it three times
                # just triples the latency before the same failure.
                #
                # 꼬리표는 **맨 앞** 에서만 읽습니다. 예전에는 글 전체에서
                # " 400:" 을 찾아서, "jev 503: Upstream provider returned 400:
                # overloaded" 같은 일시 장애가 서버 문장 속 숫자 때문에 한 번에
                # 실패했습니다.
                text = str(exc)
                status = _status_of(text)
                if status in _NO_RETRY_STATUS:
                    raise
                # 한도 소진 판정은 **마지막 시도 전에** 합니다. 예전에는 마지막
                # 시도에서 먼저 빠져나가, 일시 장애 두 번 뒤에 온 하루 한도가
                # 평범한 LLMError 가 되었습니다(max_retries=1 이면 늘 그랬습니다).
                # 헤드에서 그것은 데스크 정지가 아니라 분석가 합의 대체였습니다.
                if status == 429 and _is_long_exhaustion(text):
                    raise QuotaExhausted(text) from exc
                if attempt == self.config.max_retries - 1:
                    break
                # A 429 usually carries the provider's own suggested delay.
                # Guessing a shorter one just burns another rejected request.
                wait = _retry_after(text) if status == 429 else None
                delay = wait if wait is not None else 1.5 * (2 ** attempt)
                # 재시도가 결국 성공하면 좌석 실패로도 호출 수로도 남지 않습니다.
                # 그래서 여기서 적고 셉니다 — 그 대기는 심의의 소요 시간에 그대로
                # 들어가, 세지 않으면 "제공자가 느리다" 로 읽힙니다.
                log.info("%s 호출 재시도 %d/%d — %.1f초 뒤 (%s)",
                         self.config.provider, attempt + 2, self.config.max_retries,
                         delay, one_line_error(_describe(exc), 160))
                self.usage.retry()
                await asyncio.sleep(delay)
        raise LLMError(f"LLM call failed after {self.config.max_retries} attempts: "
                       f"{_describe(last)}") from last

    # ── providers ────────────────────────────────────────────────────────
    async def _anthropic(self, system: str, user: str, schema: dict | None,
                         budget: int = 0):
        body: dict[str, Any] = {
            "model": self.config.resolved_model(),
            "max_tokens": budget or self.config.max_tokens,
            "temperature": self.config.temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if schema:
            # Anthropic's structured output is a forced tool call.
            body["tools"] = [{"name": "emit", "description": "Return the analysis.",
                              "input_schema": schema}]
            body["tool_choice"] = {"type": "tool", "name": "emit"}
        r = await self._client.post(
            f"{self.config.base_url or 'https://api.anthropic.com'}/v1/messages",
            headers={"x-api-key": _header_key(self.config, "anthropic"),
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=body,
        )
        _raise_for_status(r, "anthropic")
        data = r.json()
        u = data.get("usage") or {}
        self.usage.add(u.get("input_tokens", 0), u.get("output_tokens", 0))
        blocks = data.get("content") or []
        _check_truncated(data.get("stop_reason"),
                         "".join(b.get("text", "") for b in blocks))
        if schema:
            for b in blocks:
                if b.get("type") == "tool_use":
                    return b.get("input") or {}
            return _extract_json("".join(b.get("text", "") for b in blocks))
        return "".join(b.get("text", "") for b in blocks)

    async def _openai(self, system: str, user: str, schema: dict | None,
                      budget: int = 0):
        body: dict[str, Any] = {
            "model": self.config.resolved_model(),
            "temperature": self.config.temperature,
            "max_completion_tokens": budget or self.config.max_tokens,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        if schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "analysis", "strict": False, "schema": schema},
            }
        r = await self._client.post(
            f"{self.config.base_url or 'https://api.openai.com/v1'}/chat/completions",
            headers={"Authorization": f"Bearer {_header_key(self.config, 'openai')}"},
            json=body,
        )
        _raise_for_status(r, "openai")
        data = r.json()
        u = data.get("usage") or {}
        self.usage.add(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
        choice = data["choices"][0]
        text = (choice["message"].get("content") or "").strip()
        _check_truncated(choice.get("finish_reason"), text)
        return _extract_json(text) if schema else text

    async def _google(self, system: str, user: str, schema: dict | None,
                      budget: int = 0):
        base = self.config.base_url or "https://generativelanguage.googleapis.com/v1beta"
        gen: dict[str, Any] = {
            "temperature": self.config.temperature,
            "maxOutputTokens": budget or self.config.max_tokens,
        }
        if schema:
            gen["responseMimeType"] = "application/json"
            gen["responseSchema"] = _to_google_schema(schema)
        # Auth goes in a header, never the query string: URLs end up in access
        # logs, proxy caches and error reports, and a key that leaks that way
        # leaks quietly. Google accepts both forms.
        #
        # Two credential shapes exist, and the default matters: AI Studio has
        # issued both "AIza..." and "AQ...." API keys over time, so keying off
        # a specific prefix goes stale. Only OAuth access tokens ("ya29....")
        # are bearer tokens; everything else is an API key. Guessing wrong
        # produces a 401 that reads like a bad key rather than a wrong scheme.
        key = _header_key(self.config, "google")
        auth_header = ({"Authorization": f"Bearer {key}"} if key.startswith("ya29.")
                       else {"x-goog-api-key": key})
        r = await self._client.post(
            f"{base}/models/{self.config.resolved_model()}:generateContent",
            headers=auth_header,
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": gen,
            },
        )
        _raise_for_status(r, "google")
        data = r.json()
        u = data.get("usageMetadata") or {}
        self.usage.add(u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0))
        candidate = (data.get("candidates") or [{}])[0]
        parts = candidate.get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        _check_truncated(candidate.get("finishReason"), text)
        return _extract_json(text) if schema else text

    # ── Jev: 판단 모델, MCP streamable HTTP ──────────────────────────────
    async def _jev(self, system: str, user: str, schema: dict | None):
        """좌석 하나 = `jev_evaluate` 한 번. 숫자와 문장은 `quant.alpha.jev` 가 만든다.

        Jev 는 글을 쓰지 않으므로 다른 제공자처럼 스키마를 넘기고 JSON 을 받는
        방식이 안 됩니다. 좁은 질문을 확률로 받아 코드가 스키마를 채웁니다.
        """
        # 여기서 가져오는 이유: jev 가 이 모듈의 LLMError 를 씁니다(순환 import).
        from quant.alpha import jev

        if schema is None:
            # 데스크의 사전 점검("Reply with the single word OK."). Jev 는 그 말을
            # 할 수 없으니, 좌석이 쓰는 도구와 질문 모양 그대로 작은 호출 하나를
            # 보내 키·연결·**질문 형식**·답의 모양을 함께 확인합니다. 예전에는
            # `jev_check` 로 키와 연결만 봐서, 서버가 좌석의 질문 형식을 거절해도
            # 점검은 통과하고 데스크는 켜진 채 봉마다 16석이 실패했습니다.
            payload = await self._jev_tool("jev_evaluate", jev.preflight_arguments())
            jev.read_preflight(payload)
            return "OK"
        request = jev.build_request(system, user, schema)
        payload = await self._jev_tool("jev_evaluate", request.arguments)
        return jev.map_answers(request, payload, undecided_below=self._undecided_below)

    def _jev_url(self) -> str:
        return self.config.base_url or JEV_DEFAULT_URL

    async def _jev_post(self, body: dict, session: str = "",
                        protocol: str = "") -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {_header_key(self.config, 'jev')}",
            "Content-Type": "application/json",
            # 스펙이 둘 다 받겠다고 말하라고 요구합니다. 서버는 둘 중 하나로 답합니다.
            "Accept": "application/json, text/event-stream",
        }
        if session:
            headers["Mcp-Session-Id"] = session
        if protocol:
            headers["MCP-Protocol-Version"] = protocol
        # `timeout` 은 httpx 에서 **단계마다**(연결, 읽기 한 번 …) 따로 걸립니다.
        # 읽기 시간 초과는 바이트가 올 때마다 다시 세서, `: keepalive` 를 조금씩
        # 흘리는 SSE 응답이나 느리게 새어 나오는 본문은 설정한 시간을 넘겨도
        # 끝나지 않았습니다(시간 초과 1초에 8초짜리 호출이 성공). 호출 하나의
        # **전체** 시간을 여기서 묶고, 넘기면 일시 장애(503)로 올려 `complete()`
        # 가 다시 묻게 합니다.
        limit = self.config.timeout if self.config.timeout and self.config.timeout > 0 else None
        try:
            return await asyncio.wait_for(
                self._client.post(self._jev_url(), json=body, headers=headers),
                timeout=limit)
        except asyncio.TimeoutError:
            raise LLMError(f"jev 503: 응답이 {limit:g}초 안에 끝나지 않았습니다 "
                           "(호출 하나의 전체 시간 상한 — llm.timeout)") from None

    async def _jev_connect(self) -> int:
        """세션이 없으면 한 번만 연다. 지금 세션의 세대 번호를 돌려준다.

        **실패도 한 번입니다.** 잠금을 기다리던 좌석들은, 기다리는 사이 앞
        시도가 실패했으면 각자 initialize 를 다시 보내지 않고 그 실패를 같이
        받습니다. 예전에는 성공만 나눠 가져서, 멈춘 서버 앞에 좌석 8개 ×
        재시도 3번 = initialize 24번이 **하나씩 차례로** 줄을 섰습니다(마지막
        좌석은 시간 초과의 24배 뒤에 실패). 기다리기 시작한 **뒤에** 끝난 시도만
        나눕니다 — 백오프를 마치고 새로 부르는 쪽은 새로 엽니다.
        """
        if self._jev_ready:
            return self._jev_generation
        seen = self._jev_connects_done
        async with self._jev_lock:
            if self._jev_ready:                 # 기다리는 사이 누가 열었다
                return self._jev_generation
            failed = self._jev_connect_error
            if self._jev_connects_done != seen and failed is not None:
                # 기다리는 사이 누가 열다 실패했다 — 같은 서버에 또 줄 서지 않는다.
                raise _shared_failure(failed) from failed
            try:
                generation = await self._jev_initialize()
            except Exception as exc:
                self._jev_connect_error = exc
                self._jev_connects_done += 1
                raise
            except BaseException:
                # 취소는 이 좌석의 사정입니다 — 기다리던 좌석은 스스로 엽니다.
                self._jev_connect_error = None
                raise
            self._jev_connect_error = None
            self._jev_connects_done += 1
            return generation

    async def _jev_initialize(self) -> int:
        """initialize → notifications/initialized. 잠금 안에서만 부릅니다."""
        rid = next(self._jev_ids)
        r = await self._jev_post({
            "jsonrpc": "2.0", "id": rid, "method": "initialize",
            "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "quant-desk", "version": "1"}},
        })
        _jev_raise_for_status(r)
        message = _jev_message(r, rid)
        if message.get("error") is not None:
            raise _rpc_failure(message["error"], "initialize 거부: ")
        result = message.get("result") if isinstance(message.get("result"), dict) else {}
        # 상태 없는(stateless) 서버는 세션 id 를 주지 않습니다. 그때는 안 보냅니다.
        session = r.headers.get("mcp-session-id", "")
        protocol = str(result.get("protocolVersion") or MCP_PROTOCOL_VERSION)
        ack = await self._jev_post({"jsonrpc": "2.0",
                                    "method": "notifications/initialized"},
                                   session, protocol)
        # 서버리스 배포는 initialize 와 이 알림을 **다른 인스턴스** 로 보낼 수
        # 있습니다. 그때의 "Session not found" 는 도구 호출에서와 같은 세션
        # 유실입니다 — 예전에는 여기서만 "jev 404"(재시도 없음)로 올라가, 헤드
        # 좌석이면 분석가 합의로, 시작 점검이면 "llm.base_url 을 확인하세요" 로
        # 끝났습니다. 503 으로 올려 `complete()` 가 새로 열어 다시 묻게 합니다.
        if ack.status_code >= 400 and ((ack.status_code == 404 and session)
                                       or _session_lost(_jev_error_text(ack))):
            raise _session_lost_failure(ack, "notifications/initialized")
        _jev_raise_for_status(ack)   # 200·202·204 무엇이든, 본문은 없다
        self._jev_session, self._jev_protocol = session, protocol
        self._jev_generation += 1
        self._jev_ready = True
        return self._jev_generation

    def _jev_drop(self, generation: int) -> None:
        """그 세션이 아직 지금 세션이면 버린다. 이미 누가 다시 열었으면 그대로."""
        if self._jev_generation == generation:
            self._jev_ready = False
            self._jev_session = ""

    async def _jev_tool(self, name: str, arguments: dict) -> dict:
        """`tools/call` 한 번. 세션이 죽었으면 한 번만 다시 열고 한 번만 다시 묻는다.

        서버리스 배포(Vercel)는 인스턴스가 바뀌면 세션을 잊습니다. 그건 우리
        요청이 틀린 게 아니므로 좌석 실패로 넘기지 않고 조용히 다시 엽니다.

        두 번째에도 세션을 잃으면 여기서는 그만 묻되, **일시 장애(503)** 로
        올립니다. 예전에는 HTTP 404 로 온 두 번째 유실이 "jev 404"(재시도 없음)
        가 되어, 헤드 좌석이면 분석가 합의로 물러서 보유를 팔았고 시작 점검이면
        "llm.base_url 을 확인하세요" 로 데스크를 껐습니다 — 같은 유실이 200 안의
        JSON-RPC 오류로 오면 503 으로 재시도되던 것과도 달랐습니다. `complete()`
        가 백오프 뒤 다시 부르면 그 시도는 또 한 번 새로 엽니다.
        """
        for attempt in range(2):
            generation = await self._jev_connect()
            session = self._jev_session
            rid = next(self._jev_ids)
            r = await self._jev_post({
                "jsonrpc": "2.0", "id": rid, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }, session, self._jev_protocol)
            if (r.status_code >= 400
                    and ((r.status_code == 404 and session)
                         or _session_lost(_jev_error_text(r)))):
                self._jev_drop(generation)
                if attempt == 0:
                    log.info("jev 세션이 만료되어 다시 엽니다 (%d)", r.status_code)
                    continue
                raise _session_lost_failure(r, "tools/call")
            # 3xx·4xx·5xx 는 꼬리표를 붙여 올립니다. 2xx 만 본문으로 갑니다.
            _jev_raise_for_status(r)
            message = _jev_message(r, rid)
            error = message.get("error")
            if error is not None:
                text = _rpc_error_text(error)
                if _session_lost(text):
                    self._jev_drop(generation)
                    if attempt == 0:
                        log.info("jev 세션이 만료되어 다시 엽니다: %s", text[:120])
                        continue
                    raise LLMError(f"jev 503: 세션을 다시 열었지만 또 잃었습니다 "
                                   f"(tools/call): {text[:300]}")
                # 요청이 틀렸다는 코드(-32602 …)만 422 — 재시도하지 않습니다.
                # 내부·서버 오류(-32603, -32000~-32099)는 503 으로 적어
                # `complete()` 가 한 번 더 묻게 하고, 한도를 말하면 429 입니다.
                raise _rpc_failure(error)
            return self._jev_result(message)
        raise LLMError("jev: 세션을 다시 열었지만 또 거부되었습니다")  # pragma: no cover

    def _jev_result(self, message: dict) -> dict:
        """JSON-RPC result → Jev 의 답 dict. 사용량은 여기서 셉니다."""
        result = message.get("result")
        if not isinstance(result, dict):
            raise LLMError(f"jev: 응답에 result 가 없습니다: {str(message)[:200]}")
        texts = [c.get("text", "") for c in (result.get("content") or [])
                 if isinstance(c, dict) and c.get("type") == "text"]
        if result.get("isError"):
            # 도구는 돌았습니다 — 청구됐을 수 있으니 호출은 셉니다(토큰은 모름).
            # 한도가 과소계상되는 쪽이 일찍 멈추는 쪽보다 나쁩니다.
            self.usage.add(0, 0)
            # 스펙은 API 실패(윗단 게이트웨이의 장애)도 여기로 보내라고 합니다.
            # 그래서 전부 "요청이 틀렸다" 로 읽지 않고 글로 가릅니다.
            raise _tool_failure(" ".join(texts) or "tool error")
        payload = result.get("structuredContent")
        if not isinstance(payload, dict):
            payload = None
            if texts:
                try:
                    payload = json.loads(texts[0])
                except ValueError:
                    payload = None
        # 오류가 `isError` 없이 **보통 결과** 로 올 수 있습니다 — `{"error": …}`
        # 객체나 JSON 이 아닌 글. 예전에는 그 둘이 꼬리표 없는 오류("JSON 객체가
        # 아닙니다", map_answers 의 "answers 가 없습니다")가 되어 분류를 건너뛰었
        # 습니다: 잔액 소진이 `QuotaExhausted` 가 되지 않아 뒷좌석마다 세 번씩
        # 실패했고, 헤드가 분석가 합의(매도)로 물러서 보유를 팔았으며, 데스크는
        # 켜진 채 다음 봉에 또 돌았습니다. `isError` 와 같은 분류를 태웁니다.
        if not isinstance(payload, dict):
            self.usage.add(0, 0)
            text = texts[0] if texts else str(result)
            raise _tool_failure(f"도구 응답이 JSON 객체가 아닙니다: {text}")
        if "answers" not in payload:
            error = payload.get("error") or payload.get("message")
            if error:
                self.usage.add(0, 0)
                raise _tool_failure(_rpc_error_text(error))
        u = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        latency = payload.get("latency_ms")
        latency_ms = (float(latency) if isinstance(latency, (int, float))
                      and not isinstance(latency, bool) and 0 <= latency < 1e9 else 0.0)
        self.usage.add(_as_int(u.get("inputTokens")), _as_int(u.get("outputTokens")),
                       latency_ms)
        return payload

    async def list_models(self) -> list[str]:
        """Model ids this provider will accept for generation. Best effort."""
        if self.config.provider != "google":
            return []
        base = self.config.base_url or "https://generativelanguage.googleapis.com/v1beta"
        key = _header_key(self.config, "google")
        header = ({"Authorization": f"Bearer {key}"} if key.startswith("ya29.")
                  else {"x-goog-api-key": key})
        r = await self._client.get(f"{base}/models", headers=header,
                                   params={"pageSize": 200})
        if r.status_code != 200:
            return []
        return [
            m["name"].replace("models/", "")
            for m in r.json().get("models", [])
            if "generateContent" in (m.get("supportedGenerationMethods") or [])
            and m["name"].replace("models/", "").startswith("gemini")
            and not any(x in m["name"] for x in ("image", "tts", "embedding",
                                                 "vision", "robotics"))
        ]

    async def close(self) -> None:
        await self._client.aclose()


def _to_google_schema(schema: dict) -> dict:
    """Gemini rejects JSON-Schema keywords it does not implement."""
    allowed = {"type", "properties", "items", "required", "enum", "description", "nullable"}
    if not isinstance(schema, dict):
        return schema
    out = {k: v for k, v in schema.items() if k in allowed}
    if "properties" in out:
        out["properties"] = {k: _to_google_schema(v) for k, v in out["properties"].items()}
    if "items" in out:
        out["items"] = _to_google_schema(out["items"])
    return out
