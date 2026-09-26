"""운영자의 Jev 토큰은 오류 문장·로그·설정 응답 어디에도 나오지 않습니다.

실패 문장은 좌석 오류 → 사전 점검 사유 → `/api/evaluate` 503 → `/api/desk` 를
거쳐 **가입자 화면까지** 갑니다. 거기에 운영자 토큰이 통째로 실린 적이 있습니다:
호스팅 대시보드에 붙여 넣은 키 끝의 줄바꿈 하나 때문에 h11 이
`Illegal header value b'Bearer <토큰>\\n'` 으로 거절했고, 그 문장이 그대로
올라갔습니다. 여기서 고정하는 것:

* 키 앞뒤의 공백·줄바꿈은 떼고 보냅니다(`resolved_key`).
* 키 **안쪽** 의 공백·제어 문자는 요청 전에 거절하고, 위치·코드포인트만 적습니다.
* 서버 오류 본문이 토큰을 되울려도 `complete()` 밖으로 나가는 예외와 로그에는
  토큰이 없습니다(두 번째 그물).
* `http://` 로 이 컴퓨터 안의 Jev 에 갈 때는 프록시를 쓰지 않습니다 — 쓰면
  토큰이 평문으로 프록시에 갑니다.
* `/api/config` 는 데스크 슬롯의 `base_url` 을 돌려주지 않습니다.
* 계정에 저장하는 키 값도 안쪽 공백·제어 문자를 거절합니다.

네트워크는 나가지 않습니다(가짜 Jev = `httpx.MockTransport`).
"""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from quant.alpha.llm_client import LLMClient, LLMConfig, LLMError
from quant.api.server import _credential_fields, _redact
from quant.config.loader import load_config
from quant.live.credentials import value_rejection_reason
from tests.test_jev import FakeJev, ask_technical

#: 길고 눈에 띄는 가짜 토큰. 조각이라도 문장에 나오면 새어 나간 것입니다.
TOKEN = "OPERATOR-JEV-TOKEN-0123456789abcdef"


def _chain_text(exc: BaseException | None) -> str:
    """예외와 그 사슬(`__cause__`·`__context__`)의 글 전부 — traceback 이 적는 것."""
    seen: set[int] = set()
    parts: list[str] = []
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        parts.append(f"{type(exc).__name__}: {exc}")
        exc = exc.__cause__ or exc.__context__
    return "\n".join(parts)


def _operator_client(fake, **config) -> LLMClient:
    """키를 **환경** 에서 읽는 클라이언트 — 운영자 키가 들어오는 실제 길."""
    client = LLMClient(LLMConfig(provider="jev", **config))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return client


# ── 키 앞뒤의 공백·줄바꿈 ──────────────────────────────────────────────────────
@pytest.mark.parametrize("tail", ["\n", "\r\n", " ", "\t"])
def test_a_key_pasted_with_a_trailing_newline_or_space_is_sent_clean(monkeypatch, tail):
    monkeypatch.setenv("JEV_API_KEY", TOKEN + tail)
    fake = FakeJev()
    client = _operator_client(fake)
    assert ask_technical(client)["stance"] == "bullish"
    headers = fake.log[0][1]
    assert headers.get("authorization") == f"Bearer {TOKEN}"


def test_a_key_that_is_only_whitespace_is_no_key(monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", " \n")
    with pytest.raises(LLMError, match="no API key"):
        LLMClient(LLMConfig(provider="jev"))


# ── 키 안쪽의 공백·제어 문자 ──────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["\x0b", "\x0c", " ", "\t", "\x7f"])
def test_a_control_character_inside_the_key_is_refused_without_echoing_it(
        monkeypatch, bad):
    head, tail = TOKEN[:12], TOKEN[12:]
    monkeypatch.setenv("JEV_API_KEY", head + bad + tail)
    fake = FakeJev()
    client = _operator_client(fake)
    with pytest.raises(LLMError) as err:
        ask_technical(client)
    text = _chain_text(err.value)
    assert head not in text and tail not in text
    assert "13번째 글자" in text                       # 위치만 말합니다
    assert fake.log == []                              # 요청은 나가지 않았습니다


# ── 서버가 토큰을 되울려도 ─────────────────────────────────────────────────────
def test_a_server_error_that_echoes_the_token_does_not_carry_it_out(monkeypatch, caplog):
    monkeypatch.setenv("JEV_API_KEY", TOKEN)

    def echo(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={
            "error": f"upstream rejected header {request.headers['authorization']}"})

    client = _operator_client(echo, max_retries=1)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(LLMError) as err:
        ask_technical(client)
    assert TOKEN not in _chain_text(err.value)
    assert "***" in str(err.value)                     # 지운 자리는 표시가 남습니다
    assert TOKEN not in caplog.text


def test_a_retried_failure_that_echoes_the_token_is_not_logged(monkeypatch, caplog):
    """재시도 로그(`… 재시도 2/2 — (원인)`)도 같은 문장을 적습니다."""
    monkeypatch.setenv("JEV_API_KEY", TOKEN)
    monkeypatch.setattr("quant.alpha.llm_client.asyncio.sleep", _no_sleep)

    def echo(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": f"overloaded for {TOKEN}"})

    client = _operator_client(echo, max_retries=2)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(LLMError) as err:
        ask_technical(client)
    assert "재시도" in caplog.text                     # 재시도 로그가 실제로 찍혔고
    assert TOKEN not in caplog.text                    # 거기에 토큰은 없습니다
    assert TOKEN not in _chain_text(err.value)


async def _no_sleep(_seconds: float) -> None:
    return None


def test_a_failure_without_the_token_passes_through_unchanged(monkeypatch):
    """그물은 토큰이 있을 때만 씁니다 — 평범한 오류 문장은 그대로입니다."""
    monkeypatch.setenv("JEV_API_KEY", TOKEN)

    def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_token"})

    client = _operator_client(fail, max_retries=1)
    with pytest.raises(LLMError) as err:
        ask_technical(client)
    assert "invalid_token" in str(err.value) and "***" not in str(err.value)


# ── 이 컴퓨터 안의 http:// 는 프록시를 쓰지 않는다 ──────────────────────────────
def test_a_local_http_endpoint_never_goes_through_a_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    local = LLMClient(LLMConfig(provider="jev", api_key=TOKEN,
                                base_url="http://127.0.0.1:8123/mcp"))
    assert local._client.trust_env is False
    assert not local._client._mounts                   # 프록시 전송이 걸리지 않았습니다

    # 같은 환경에서 https 는 프록시를 그대로 씁니다(CONNECT 터널 안이라 안전하고,
    # 회사 프록시가 필요한 운영자가 있습니다). 위 검사가 헛돌지 않는다는 대조군이기도 합니다.
    remote = LLMClient(LLMConfig(provider="jev", api_key=TOKEN,
                                 base_url="https://jev.example.invalid/api/mcp"))
    assert remote._client.trust_env is True
    assert remote._client._mounts


# ── /api/config 는 데스크 주소를 돌려주지 않는다 ───────────────────────────────
def test_the_config_api_does_not_return_the_desk_address():
    cfg = load_config("configs/kr_desk_gemini.yaml")
    spec = next(m for m in cfg.alpha if m.type in ("desk", "council"))
    spec.params["llm"]["base_url"] = "https://jev.example.invalid/s3cr3t-path/mcp?tenant=s3cr3t"
    body = _redact(json.loads(cfg.model_dump_json()), _credential_fields(cfg))
    assert "s3cr3t" not in json.dumps(body, ensure_ascii=False)
    llm = next(m for m in body["alpha"] if m["type"] in ("desk", "council"))["params"]["llm"]
    assert llm["provider"] == "jev"                    # 나머지는 그대로 보입니다
    assert llm["base_url"] == "***"


# ── 계정에 저장하는 키 값 ──────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [" ", "\t", "\x0b", "\x0c"])
def test_a_stored_key_with_whitespace_inside_is_refused(bad):
    reason = value_rejection_reason("JEV_API_KEY", TOKEN[:12] + bad + TOKEN[12:])
    assert "13번째 글자" in reason
    assert TOKEN[:12] not in reason


def test_a_clean_key_and_a_non_key_value_with_spaces_are_accepted():
    assert value_rejection_reason("JEV_API_KEY", TOKEN) == ""
    assert value_rejection_reason("OPERATOR_NAME", "홍 길동") == ""
