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
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("quant.alpha.llm")

# Newest Claude generation. Override per-config if you need a cheaper tier for
# the high-volume analyst roles.
DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "openai": "gpt-5.5",
    "google": "gemini-3-pro",
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


class LLMClient:
    """One client, three wire protocols."""

    def __init__(self, config: LLMConfig):
        self.config = config
        self.usage = LLMUsage()
        self._client = httpx.AsyncClient(timeout=config.timeout)
        if not config.resolved_key():
            raise LLMError(
                f"no API key for provider {config.provider!r} — set the matching env var"
            )

    async def complete(self, system: str, user: str, schema: dict | None = None) -> Any:
        """Return parsed JSON when `schema` is given, else raw text."""
        last: Exception | None = None
        for attempt in range(self.config.max_retries):
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
                await asyncio.sleep(1.5 * (2 ** attempt))
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
        r = await self._client.post(
            f"{base}/models/{self.config.resolved_model()}:generateContent",
            params={"key": self.config.resolved_key()},
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
