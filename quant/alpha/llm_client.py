"""Minimal multi-provider LLM client for the research council.

Deliberately dependency-light: one `httpx` call per provider rather than three
vendor SDKs, because the council only ever needs "send messages, get structured
JSON back". Structured output uses each provider's native mechanism (Anthropic
tool-use, OpenAI json_schema, Gemini response_schema) so the council never has
to regex a JSON blob out of prose.
"""
from __future__ import annotations

import asyncio
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
}


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def add(self, i: int, o: int) -> None:
        self.input_tokens += i
        self.output_tokens += o
        self.calls += 1


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
        self._client = httpx.AsyncClient(timeout=config.timeout)
        if not config.resolved_key():
            raise LLMError(
                f"no API key for provider {config.provider!r} — set the matching env var"
            )

    async def complete(self, system: str, user: str, schema: dict | None = None) -> Any:
        """Return parsed JSON when `schema` is given, else raw text."""
        last: Exception | None = None
        for attempt in range(self.config.max_retries):
            await self._limiter.wait()
            try:
                if self.config.provider == "anthropic":
                    return await self._anthropic(system, user, schema)
                if self.config.provider in ("openai", "openai_compatible"):
                    return await self._openai(system, user, schema)
                if self.config.provider == "google":
                    return await self._google(system, user, schema)
                raise LLMError(f"unsupported provider {self.config.provider!r}")
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
    async def _anthropic(self, system: str, user: str, schema: dict | None):
        body: dict[str, Any] = {
            "model": self.config.resolved_model(),
            "max_tokens": self.config.max_tokens,
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
        if schema:
            for b in blocks:
                if b.get("type") == "tool_use":
                    return b.get("input") or {}
            return _extract_json("".join(b.get("text", "") for b in blocks))
        return "".join(b.get("text", "") for b in blocks)

    async def _openai(self, system: str, user: str, schema: dict | None):
        body: dict[str, Any] = {
            "model": self.config.resolved_model(),
            "temperature": self.config.temperature,
            "max_completion_tokens": self.config.max_tokens,
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
        text = (data["choices"][0]["message"].get("content") or "").strip()
        return _extract_json(text) if schema else text

    async def _google(self, system: str, user: str, schema: dict | None):
        base = self.config.base_url or "https://generativelanguage.googleapis.com/v1beta"
        gen: dict[str, Any] = {
            "temperature": self.config.temperature,
            "maxOutputTokens": self.config.max_tokens,
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
        parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
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
