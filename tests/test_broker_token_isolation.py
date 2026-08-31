"""OAuth token caches must never cross user or environment boundaries."""
from __future__ import annotations

import httpx

from quant.data.providers import kis as K


async def test_kis_token_cache_uses_the_complete_credentials(monkeypatch):
    K._TOKENS.clear()
    real_async_client = httpx.AsyncClient
    issued: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        issued.append(payload)
        return httpx.Response(200, json={
            "access_token": f"token-{len(issued)}", "expires_in": 21_600,
        })

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self._client = real_async_client(transport=httpx.MockTransport(handler))

        async def __aenter__(self):
            return self._client

        async def __aexit__(self, *_args):
            await self._client.aclose()

    monkeypatch.setattr(K.httpx, "AsyncClient", FakeAsyncClient)
    prefix = "same-kis-prefix"

    first = await K.kis_token(prefix + "-A", "secret-A", paper=False)
    second = await K.kis_token(prefix + "-B", "secret-B", paper=False)
    paper = await K.kis_token(prefix + "-A", "secret-A", paper=True)

    assert (first, second, paper) == ("token-1", "token-2", "token-3")
    assert len(issued) == 3
    assert len(K._TOKENS) == 3
    assert all(isinstance(key, bytes) and b"same-kis" not in key for key in K._TOKENS)
