"""Minimal multi-provider LLM client for the research council.

Deliberately dependency-light: one `httpx` call per provider rather than three
vendor SDKs, because the council only ever needs "send messages, get structured
JSON back". Structured output uses each provider's native mechanism (Anthropic
tool-use, OpenAI json_schema, Gemini response_schema) so the council never has
to regex a JSON blob out of prose.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from quant.core.aio import LazyLock

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

    def add(self, i: int, o: int) -> None:
        self.input_tokens += i
        self.output_tokens += o
        self.calls += 1

    @property
    def cost_usd(self) -> float:
        pin, pout = price_for(self.model)
        return self.input_tokens / 1e6 * pin + self.output_tokens / 1e6 * pout


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
        return os.environ.get(
            {"anthropic": "ANTHROPIC_API_KEY",
             "openai": "OPENAI_API_KEY",
             "google": "GOOGLE_API_KEY",
             "jev": "JEV_API_KEY"}.get(self.provider, ""),
            "",
        )


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

#: 한도·과금을 말하는 오류. 무엇이 "오늘 안에는 안 풀리는" 한도인지는
#: `_is_long_exhaustion` 이 정합니다 — 여기서는 그 경로에 태우기만 합니다.
_JEV_QUOTA_WORDS = ("quota", "rate limit", "rate-limit", "rate_limit", "ratelimit",
                    "too many requests", "per day", "daily", "credit", "billing",
                    "payment", "insufficient funds", "spending cap")

#: 도구 오류(`isError`) 가운데 **입력이 틀렸다** 는 말. 도구 오류에는 코드가
#: 없어서 글로 봅니다. SDK 는 입력 검증 실패를 "MCP error -32602: …" 로 적습니다.
#: "unexpected" 가 걸리지 않게 "expected" 같은 넓은 말은 넣지 않습니다.
_JEV_BAD_INPUT_WORDS = ("invalid argument", "invalid param", "invalid input",
                        "invalid request", "validation", "too large", "too long",
                        "too many questions", "exceeds", "must be", "unknown tool",
                        "-32700", "-32600", "-32601", "-32602")


def _jev_failure(text: str, *, bad_request: bool) -> LLMError:
    """Jev 가 돌려준 오류에 `complete()` 가 읽는 상태 꼬리표를 붙인다."""
    lowered = text.lower()
    if any(w in lowered for w in _JEV_QUOTA_WORDS):
        status = 429
    elif bad_request:
        status = 422
    else:
        status = 503
    return LLMError(f"jev {status}: {text[:400]}")


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
            raise LLMError(
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
            except (httpx.HTTPError, LLMError) as exc:
                last = exc
                # A 4xx is a bad request, not a blip. Retrying it three times
                # just triples the latency before the same failure.
                text = str(exc)
                if any(f" {code}:" in text for code in (400, 401, 403, 404, 422)):
                    raise
                if attempt == self.config.max_retries - 1:
                    break
                # A 429 usually carries the provider's own suggested delay.
                # Guessing a shorter one just burns another rejected request.
                if " 429:" in text and _is_long_exhaustion(text):
                    raise QuotaExhausted(text) from exc
                wait = _retry_after(text) if " 429:" in text else None
                await asyncio.sleep(wait if wait is not None else 1.5 * (2 ** attempt))
        raise LLMError(f"LLM call failed after {self.config.max_retries} attempts: {last}")

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
            headers={"x-api-key": self.config.resolved_key(),
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
            headers={"Authorization": f"Bearer {self.config.resolved_key()}"},
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
        key = self.config.resolved_key()
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
            # 할 수 없으니, 가장 싼 도구 호출 하나로 키와 연결만 확인합니다.
            await self._jev_tool("jev_check", {
                "state": "connectivity check",
                "question": "Is this a connectivity check?",
            })
            return "OK"
        request = jev.build_request(system, user, schema)
        payload = await self._jev_tool("jev_evaluate", request.arguments)
        return jev.map_answers(request, payload, undecided_below=self._undecided_below)

    def _jev_url(self) -> str:
        return self.config.base_url or JEV_DEFAULT_URL

    async def _jev_post(self, body: dict, session: str = "",
                        protocol: str = "") -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self.config.resolved_key()}",
            "Content-Type": "application/json",
            # 스펙이 둘 다 받겠다고 말하라고 요구합니다. 서버는 둘 중 하나로 답합니다.
            "Accept": "application/json, text/event-stream",
        }
        if session:
            headers["Mcp-Session-Id"] = session
        if protocol:
            headers["MCP-Protocol-Version"] = protocol
        return await self._client.post(self._jev_url(), json=body, headers=headers)

    async def _jev_connect(self) -> int:
        """세션이 없으면 한 번만 연다. 지금 세션의 세대 번호를 돌려준다."""
        if self._jev_ready:
            return self._jev_generation
        async with self._jev_lock:
            if self._jev_ready:                 # 기다리는 사이 누가 열었다
                return self._jev_generation
            rid = next(self._jev_ids)
            r = await self._jev_post({
                "jsonrpc": "2.0", "id": rid, "method": "initialize",
                "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
                           "clientInfo": {"name": "quant-desk", "version": "1"}},
            })
            _raise_for_status(r, "jev")
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
            _raise_for_status(ack, "jev")     # 200·202·204 무엇이든, 본문은 없다
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
        두 번째에도 거절되면 그때는 진짜 실패입니다.
        """
        for attempt in range(2):
            generation = await self._jev_connect()
            session = self._jev_session
            rid = next(self._jev_ids)
            r = await self._jev_post({
                "jsonrpc": "2.0", "id": rid, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }, session, self._jev_protocol)
            if r.status_code >= 400:
                if attempt == 0 and ((r.status_code == 404 and session)
                                     or _session_lost(_jev_error_text(r))):
                    log.info("jev 세션이 만료되어 다시 엽니다 (%d)", r.status_code)
                    self._jev_drop(generation)
                    continue
                _raise_for_status(r, "jev")
            message = _jev_message(r, rid)
            error = message.get("error")
            if error is not None:
                text = _rpc_error_text(error)
                if attempt == 0 and _session_lost(text):
                    log.info("jev 세션이 만료되어 다시 엽니다: %s", text[:120])
                    self._jev_drop(generation)
                    continue
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
        if not isinstance(payload, dict):
            self.usage.add(0, 0)
            raise LLMError(f"jev: 도구 응답이 JSON 객체가 아닙니다: "
                           f"{(texts[0] if texts else str(result))[:200]}")
        u = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        self.usage.add(_as_int(u.get("inputTokens")), _as_int(u.get("outputTokens")))
        return payload

    async def list_models(self) -> list[str]:
        """Model ids this provider will accept for generation. Best effort."""
        if self.config.provider != "google":
            return []
        base = self.config.base_url or "https://generativelanguage.googleapis.com/v1beta"
        key = self.config.resolved_key()
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
