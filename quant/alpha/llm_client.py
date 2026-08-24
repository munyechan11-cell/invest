"""Minimal multi-provider LLM client for the research council.

Deliberately dependency-light: one `httpx` call per provider rather than three
vendor SDKs, because the council only ever needs "send messages, get structured
JSON back". Structured output uses each provider's native mechanism (Anthropic
tool-use, OpenAI json_schema, Gemini response_schema) so the council never has
to regex a JSON blob out of prose.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
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
class CallMeter:
    """이 블록 안에서 나간 호출만 담는 누산기 — `metered()` 가 만듭니다."""

    calls: int = 0
    cost_usd: float = 0.0
    #: 바깥 계량기. 스코프가 겹칠 때 안쪽이 바깥을 가려 버리면 바깥은 자기 몫을
    #: 통째로 놓칩니다. 청구가 걸린 자리라 과다보다 조용한 누락이 더 나쁩니다.
    parent: CallMeter | None = None

    def add(self, calls: int, cost_usd: float) -> None:
        self.calls += calls
        self.cost_usd += cost_usd
        if self.parent is not None:
            self.parent.add(calls, cost_usd)


#: 지금 이 태스크의 LLM 호출을 누가 세고 있는가.
#:
#: 왜 클라이언트의 `usage` 차분이 아닌가: 데스크는 한 사이클의 종목들을
#: `asyncio.gather` 로 동시에 심의하고, 한 심의 안에서도 좌석 여럿이 동시에
#: 돕니다. `usage` 는 그 전부를 한 통에 담으므로 어느 한 심의의 before/after 를
#: 떠도 그 사이에 낀 형제 심의의 호출이 통째로 섞여 들어옵니다 — 단조 증가
#: 카운터라 과소가 아니라 **항상 과다**이고, 동시에 도는 종목 수만큼 부풉니다.
#: contextvar 는 태스크를 만들 때 복사되므로, 좌석 호출이 자기 심의에만 쌓입니다.
#:
#: 언제 이게 안 통하는가: 계량 스코프 **밖에서** 만들어져 살아남는 태스크의
#: 호출은 여기 안 잡힙니다. 지금 데스크·카운슬의 좌석 호출은 전부 심의를
#: 기다리는 `gather` 안에서 나가므로 해당 사항이 없지만, 나중에 배경 워커가
#: 대신 부르게 바뀌면 그 호출은 아무 계정에도 안 잡힙니다.
_meter: contextvars.ContextVar[CallMeter | None] = contextvars.ContextVar(
    "llm_call_meter", default=None)


@contextmanager
def metered() -> Iterator[CallMeter]:
    """이 블록 안에서 나간 LLM 호출과 비용만 세는 계량기를 연다."""
    meter = CallMeter(parent=_meter.get())
    token = _meter.set(meter)
    try:
        yield meter
    finally:
        _meter.reset(token)


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
        meter = _meter.get()
        if meter is not None:
            # 비용은 호출 시점의 단가로 잽니다. 데스크는 분석석과 결정석에 다른
            # 모델을 쓰므로, 나중에 합계 토큰을 한 단가로 환산하면 어느 쪽이든
            # 틀립니다 — `record_spend` 가 생긴 이유와 같은 이유입니다.
            pin, pout = price_for(self.model)
            meter.add(1, i / 1e6 * pin + o / 1e6 * pout)

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
             "google": "GOOGLE_API_KEY"}.get(self.provider, ""),
            "",
        )


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


def _is_long_exhaustion(message: str) -> bool:
    if any(w in message.lower() for w in ("per day", "perday", "daily", "quota_exceeded")):
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
    """One client, three wire protocols."""

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
