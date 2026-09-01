"""Read-only near-real-time market and account snapshot contracts.

No test in this file contacts Toss or submits an order.  The market shapes are
copied from the official REST OpenAPI v1.2.14 examples.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from quant.api.server import ReadBusy, ReadCoalescer, _template_config, create_app
from quant.brokerage import toss_broker as T
from quant.core.account import Portfolio
from quant.core.types import UTC, Bar, Quote, Symbol
from quant.live.state import StateStore

KRX = Symbol(
    "005930", venue="toss", quote_currency="KRW",
    tick_size=Decimal("100"), lot_size=Decimal("1"),
)


class FakeMarketClient:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.calls: list[tuple[str, dict | None]] = []

    async def request(self, method, path, *, params=None, **_kwargs):
        assert method == "GET"
        self.calls.append((path, params))
        value = self.responses[path]
        if isinstance(value, BaseException):
            raise value
        return value

    async def close(self):
        return None


def provider_with(responses: dict[str, object]) -> T.TossProvider:
    provider = object.__new__(T.TossProvider)
    provider.client = FakeMarketClient(responses)
    return provider


def now() -> str:
    return datetime.now(UTC).isoformat()


def test_recent_trades_path_matches_openapi_v1_2_14():
    assert T._FIELDS["trades_path"] == "/api/v1/trades"


@pytest.mark.asyncio
async def test_official_depth_and_trades_are_exposed_without_inventing_side():
    ts = now()
    provider = provider_with({
        T._FIELDS["orderbook_path"]: {
            "timestamp": ts, "currency": "KRW",
            # The official example conflicts with its prose about ask order.
            # Best ask/bid must be selected by price, not array index zero.
            "asks": [
                {"price": "72300", "volume": "1200"},
                {"price": "72100", "volume": "8500"},
                {"price": "72200", "volume": "3400"},
            ],
            "bids": [
                {"price": "71800", "volume": "2700"},
                {"price": "72000", "volume": "5200"},
                {"price": "71900", "volume": "4100"},
            ],
        },
        T._FIELDS["price_path"]: [{
            "symbol": "005930", "timestamp": ts,
            "lastPrice": "72000", "currency": "KRW",
        }],
        T._FIELDS["trades_path"]: [{
            "price": "72000", "volume": "120",
            "timestamp": ts, "currency": "KRW",
        }],
    })

    body = await provider.market_snapshot(KRX, depth=2, trade_count=1)

    assert body["quote"] == {
        "price": 72000.0,
        "price_kind": "last",
        "bid": 72000.0,
        "ask": 72100.0,
        "bid_quantity": 5200.0,
        "ask_quantity": 8500.0,
        "change": None,
        "change_pct": None,
        "ts": ts,
        "source": "toss_rest_price+orderbook",
    }
    assert [row["price"] for row in body["depth"]["asks"]] == [72100.0, 72200.0]
    assert [row["price"] for row in body["depth"]["bids"]] == [72000.0, 71900.0]
    assert body["recent_trades"][0]["side"] is None
    assert body["capabilities"]["websocket_available"] is True
    assert body["capabilities"]["websocket_active"] is False
    assert [path for path, _ in provider.client.calls] == [
        T._FIELDS["orderbook_path"],
        T._FIELDS["price_path"],
        T._FIELDS["trades_path"],
    ]


@pytest.mark.asyncio
async def test_trade_count_slice_displays_the_row_used_for_tape_freshness():
    fresh = datetime.now(UTC)
    old = fresh - timedelta(hours=3)
    provider = provider_with({
        T._FIELDS["orderbook_path"]: {
            "timestamp": None, "currency": "KRW", "asks": [], "bids": [],
        },
        T._FIELDS["price_path"]: [],
        # Deliberately oldest-first: venue array order is not a freshness
        # guarantee and the fresh row used for the label must remain visible.
        T._FIELDS["trades_path"]: [
            {"price": "70000", "volume": "1", "timestamp": old.isoformat(),
             "currency": "KRW"},
            {"price": "70100", "volume": "2", "timestamp": fresh.isoformat(),
             "currency": "KRW"},
        ],
    })

    body = await provider.market_snapshot(KRX, trade_count=1)

    assert len(body["recent_trades"]) == 1
    assert body["recent_trades"][0]["ts"] == fresh.isoformat()
    assert body["freshness"]["components"]["trades"]["ts"] == fresh.isoformat()


@pytest.mark.asyncio
async def test_candle_history_stops_when_next_before_does_not_advance():
    current = datetime.now(UTC)
    ts = current - timedelta(days=2)
    provider = provider_with({
        T._FIELDS["candles_path"]: {
            "candles": [{
                "timestamp": ts.isoformat(),
                "openPrice": "70000", "highPrice": "71000",
                "lowPrice": "69000", "closePrice": "70500", "volume": "10",
            }],
            "nextBefore": "stuck-cursor",
        },
    })

    bars = await provider.history(
        KRX, "1d", current - timedelta(days=20), current,
    )

    assert len(bars) == 1
    assert len(provider.client.calls) == 2


@pytest.mark.asyncio
async def test_nonfinite_or_economically_impossible_candle_never_reaches_models():
    current = datetime.now(UTC)
    ts = current - timedelta(days=2)
    provider = provider_with({
        T._FIELDS["candles_path"]: {
            "candles": [
                {"timestamp": ts.isoformat(), "openPrice": "NaN",
                 "highPrice": "Infinity", "lowPrice": "-1",
                 "closePrice": "0", "volume": "-7"},
                {"timestamp": (ts - timedelta(days=1)).isoformat(),
                 "openPrice": "100", "highPrice": "99", "lowPrice": "98",
                 "closePrice": "100", "volume": "1"},
            ],
            "nextBefore": None,
        },
    })

    bars = await provider.history(
        KRX, "1d", current - timedelta(days=20), current,
    )

    assert bars == []


@pytest.mark.asyncio
async def test_empty_orderbook_is_valid_and_last_price_is_not_a_fake_spread():
    ts = now()
    provider = provider_with({
        T._FIELDS["orderbook_path"]: {
            "timestamp": None, "currency": "KRW", "asks": [], "bids": [],
        },
        T._FIELDS["price_path"]: [{
            "symbol": "005930", "timestamp": ts,
            "lastPrice": "70000", "currency": "KRW",
        }],
        T._FIELDS["trades_path"]: [],
    })

    body = await provider.market_snapshot(KRX)

    assert body["quote"]["price"] == 70000.0
    assert body["quote"]["bid"] is None and body["quote"]["ask"] is None
    assert body["depth"] == {"asks": [], "bids": [], "ts": None}
    assert body["capabilities"]["top_of_book"] is False
    assert body["capabilities"]["depth_available"] is True


@pytest.mark.asyncio
async def test_valid_book_midpoint_is_labelled_when_last_price_fails():
    ts = now()
    provider = provider_with({
        T._FIELDS["orderbook_path"]: {
            "timestamp": ts, "currency": "KRW",
            "asks": [{"price": "70100", "volume": "10"}],
            "bids": [{"price": "69900", "volume": "12"}],
        },
        T._FIELDS["price_path"]: T._TossHTTPError(503, "price down"),
        T._FIELDS["trades_path"]: [],
    })

    body = await provider.market_snapshot(KRX)

    assert body["quote"]["price"] == 70000.0
    assert body["quote"]["price_kind"] == "midpoint"
    assert body["quote"]["source"] == "toss_rest_orderbook"


@pytest.mark.asyncio
async def test_duplicate_price_rows_fall_back_to_the_validated_book_midpoint():
    ts = now()
    provider = provider_with({
        T._FIELDS["orderbook_path"]: {
            "timestamp": ts, "currency": "KRW",
            "asks": [{"price": "70100", "volume": "10"}],
            "bids": [{"price": "69900", "volume": "12"}],
        },
        T._FIELDS["price_path"]: [
            {"symbol": "005930", "timestamp": ts,
             "lastPrice": "1", "currency": "KRW"},
            {"symbol": "005930", "timestamp": ts,
             "lastPrice": "70000", "currency": "KRW"},
        ],
        T._FIELDS["trades_path"]: [],
    })

    body = await provider.market_snapshot(KRX)

    assert body["quote"]["price"] == 70000.0
    assert body["quote"]["price_kind"] == "midpoint"
    assert "중복" in " ".join(body["issues"])


@pytest.mark.asyncio
async def test_fresh_depth_cannot_disguise_a_stale_displayed_last_price():
    fresh = datetime.now(UTC)
    stale = fresh - timedelta(hours=3)
    provider = provider_with({
        T._FIELDS["orderbook_path"]: {
            "timestamp": fresh.isoformat(), "currency": "KRW",
            "asks": [{"price": "70100", "volume": "10"}],
            "bids": [{"price": "69900", "volume": "12"}],
        },
        T._FIELDS["price_path"]: [{
            "symbol": "005930", "timestamp": stale.isoformat(),
            "lastPrice": "70000", "currency": "KRW",
        }],
        T._FIELDS["trades_path"]: [{
            "price": "70000", "volume": "1",
            "timestamp": fresh.isoformat(), "currency": "KRW",
        }],
    })

    body = await provider.market_snapshot(KRX)

    assert body["quote"]["price"] == 70000.0
    assert body["quote"]["ts"] == stale.isoformat()
    assert body["freshness"]["status"] == "stale"
    assert body["freshness"]["age_ms"] >= 3 * 60 * 60 * 1000


@pytest.mark.asyncio
async def test_fresh_price_cannot_disguise_stale_displayed_depth_and_tape():
    fresh = datetime.now(UTC)
    stale = fresh - timedelta(hours=3)
    provider = provider_with({
        T._FIELDS["orderbook_path"]: {
            "timestamp": stale.isoformat(), "currency": "KRW",
            "asks": [{"price": "70100", "volume": "10"}],
            "bids": [{"price": "69900", "volume": "12"}],
        },
        T._FIELDS["price_path"]: [{
            "symbol": "005930", "timestamp": fresh.isoformat(),
            "lastPrice": "70000", "currency": "KRW",
        }],
        T._FIELDS["trades_path"]: [{
            "price": "70000", "volume": "1",
            "timestamp": stale.isoformat(), "currency": "KRW",
        }],
    })

    body = await provider.market_snapshot(KRX)

    assert body["quote"]["price"] == 70000.0
    assert body["freshness"]["status"] == "stale"
    assert body["freshness"]["age_ms"] >= 3 * 60 * 60 * 1000
    assert body["freshness"]["components"]["quote"]["status"] == "fresh"
    assert body["freshness"]["components"]["depth"]["status"] == "stale"
    assert body["freshness"]["components"]["trades"]["status"] == "stale"


@pytest.mark.asyncio
async def test_old_last_trade_does_not_slow_a_fresh_quote_and_orderbook():
    fresh = datetime.now(UTC)
    old_trade = fresh - timedelta(hours=3)
    provider = provider_with({
        T._FIELDS["orderbook_path"]: {
            "timestamp": fresh.isoformat(), "currency": "KRW",
            "asks": [{"price": "70100", "volume": "10"}],
            "bids": [{"price": "69900", "volume": "12"}],
        },
        T._FIELDS["price_path"]: [{
            "symbol": "005930", "timestamp": fresh.isoformat(),
            "lastPrice": "70000", "currency": "KRW",
        }],
        T._FIELDS["trades_path"]: [{
            "price": "70000", "volume": "1",
            "timestamp": old_trade.isoformat(), "currency": "KRW",
        }],
    })

    body = await provider.market_snapshot(KRX)

    assert body["freshness"]["status"] == "fresh"
    assert body["freshness"]["poll_after_ms"] == 2_500
    tape = body["freshness"]["components"]["trades"]
    assert tape["status"] == "stale"
    assert tape["affects_overall"] is False


@pytest.mark.asyncio
async def test_crossed_or_cross_currency_market_evidence_is_rejected():
    ts = now()
    provider = provider_with({
        T._FIELDS["orderbook_path"]: {
            "timestamp": ts, "currency": "KRW",
            "asks": [{"price": "99", "volume": "10"}],
            "bids": [{"price": "101", "volume": "12"}],
        },
        T._FIELDS["price_path"]: [{
            "symbol": "005930", "timestamp": ts,
            "lastPrice": "100", "currency": "USD",
        }],
        T._FIELDS["trades_path"]: [{
            "price": "100", "volume": "1", "timestamp": ts, "currency": "JPY",
        }],
    })

    body = await provider.market_snapshot(KRX)

    assert body["currency"] == "KRW"
    assert body["quote"]["price"] is None
    assert body["quote"]["bid"] is None and body["quote"]["ask"] is None
    assert body["depth"] is None and body["recent_trades"] == []
    assert body["freshness"]["status"] == "unavailable"
    assert body["capabilities"]["depth_available"] is False
    assert body["capabilities"]["recent_trades_available"] is False


@pytest.mark.asyncio
async def test_malformed_market_shapes_fail_closed_but_keep_a_valid_last_price():
    ts = now()
    provider = provider_with({
        T._FIELDS["orderbook_path"]: {
            "timestamp": ts, "currency": "KRW",
            "asks": [{"price": "NaN", "volume": "1"}],
            "bids": [{"price": "70000", "volume": "1"}],
        },
        T._FIELDS["price_path"]: [{
            "symbol": "005930", "timestamp": ts,
            "lastPrice": "70000", "currency": "KRW",
        }],
        T._FIELDS["trades_path"]: {"trades": []},
    })

    body = await provider.market_snapshot(KRX)

    assert body["quote"]["price"] == 70000.0
    assert body["quote"]["bid"] is None and body["quote"]["ask"] is None
    assert body["depth"] is None
    assert body["recent_trades"] == []
    assert body["freshness"]["status"] == "degraded"


@pytest.mark.asyncio
async def test_market_snapshot_surfaces_401_without_fabricating_empty_prices():
    unauthorized = T._TossHTTPError(401, "expired")
    provider = provider_with({
        T._FIELDS["orderbook_path"]: unauthorized,
        T._FIELDS["price_path"]: unauthorized,
        T._FIELDS["trades_path"]: unauthorized,
    })

    body = await provider.market_snapshot(KRX)

    assert body["quote"]["price"] is None
    assert body["depth"] is None and body["recent_trades"] == []
    assert body["freshness"]["status"] == "unavailable"
    assert "인증이 만료" in body["freshness"]["message"]


@pytest.mark.asyncio
async def test_market_snapshot_slows_polling_after_429():
    throttled = T._TossHTTPError(429, "slow down", retry_after=20)
    provider = provider_with({
        T._FIELDS["orderbook_path"]: throttled,
        T._FIELDS["price_path"]: throttled,
        T._FIELDS["trades_path"]: throttled,
    })

    body = await provider.market_snapshot(KRX)

    assert body["freshness"]["status"] == "unavailable"
    assert body["freshness"]["poll_after_ms"] == 20_000
    assert "조회 한도" in body["freshness"]["message"]


def test_non_finite_retry_after_is_never_used_as_a_timer():
    response = httpx.Response(429, headers={"Retry-After": "Infinity"})

    assert T._retry_after_seconds(response, 0) is None
    assert T._TossHTTPError(
        429, "rate limited", retry_after=float("inf"),
    ).retry_after is None


@pytest.mark.asyncio
async def test_trading_quote_requires_real_timestamped_paired_orderbook():
    ts = datetime.now(UTC).isoformat()
    provider = provider_with({
        T._FIELDS["orderbook_path"]: {
            "timestamp": ts,
            "currency": "KRW",
            "asks": [{"price": "70100", "volume": "10"}],
            "bids": [{"price": "69900", "volume": "12"}],
        },
    })

    quote = await provider.quote(KRX)

    assert quote is not None
    assert quote.ts.isoformat() == ts
    assert quote.bid == 69900.0 and quote.ask == 70100.0
    assert [path for path, _params in provider.client.calls] == [
        T._FIELDS["orderbook_path"],
    ]


@pytest.mark.asyncio
async def test_trading_quote_never_falls_back_to_a_synthetic_last_price_spread():
    provider = provider_with({
        T._FIELDS["orderbook_path"]: RuntimeError("orderbook down"),
        T._FIELDS["price_path"]: [{
            "symbol": "005930", "timestamp": now(),
            "lastPrice": "70000", "currency": "KRW",
        }],
    })

    assert await provider.quote(KRX) is None
    assert [path for path, _params in provider.client.calls] == [
        T._FIELDS["orderbook_path"],
    ]


@pytest.mark.parametrize("depth,trade_count", [(0, 1), (21, 1), (1, -1), (1, 51)])
@pytest.mark.asyncio
async def test_provider_bounds_market_snapshot_inputs(depth, trade_count):
    provider = provider_with({})
    with pytest.raises(ValueError):
        await provider.market_snapshot(KRX, depth=depth, trade_count=trade_count)


async def mock_http_client(monkeypatch, handler) -> T._TossClient:
    async def fake_token(*_args, **_kwargs):
        return "test-token"

    monkeypatch.setattr(T, "toss_token", fake_token)
    T._RATE_GATES.clear()
    client = T._TossClient(
        "test-client", "test-secret", requests_per_second=1_000_000,
    )
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


@pytest.mark.asyncio
async def test_401_read_is_not_retried(monkeypatch):
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={
            "error": {"code": "invalid-token", "message": "expired"},
        })

    client = await mock_http_client(monkeypatch, handler)
    try:
        with pytest.raises(T._TossHTTPError) as caught:
            await client.request("GET", T._FIELDS["price_path"])
    finally:
        await client.close()
    assert caught.value.status_code == 401
    assert calls == 1


@pytest.mark.asyncio
async def test_401_evicts_only_the_revoked_cached_bearer():
    key = T._token_cache_key("revoked-client", "revoked-secret")
    other = T._token_cache_key("other-client", "other-secret")
    T._TOKENS[key] = ("revoked-token", T.time.time() + 3600)
    T._TOKENS[other] = ("other-token", T.time.time() + 3600)

    def handler(request):
        assert request.headers["Authorization"] == "Bearer revoked-token"
        return httpx.Response(401, json={"error": "revoked"})

    client = T._TossClient("revoked-client", "revoked-secret")
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(T._TossHTTPError):
            await client.request("GET", T._FIELDS["price_path"])
    finally:
        await client.close()

    assert key not in T._TOKENS
    assert T._TOKENS[other][0] == "other-token"


@pytest.mark.asyncio
async def test_503_preserves_and_shares_server_retry_after(monkeypatch):
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, headers={"Retry-After": "120"})

    client = await mock_http_client(monkeypatch, handler)
    try:
        with pytest.raises(T._TossHTTPError) as caught:
            await client.request("GET", T._FIELDS["price_path"])
    finally:
        await client.close()

    assert calls == 1
    assert caught.value.status_code == 503
    assert caught.value.retry_after == 120.0


@pytest.mark.asyncio
async def test_naive_iso_market_timestamp_is_not_fresh_evidence():
    naive = datetime.now(UTC).replace(tzinfo=None).isoformat()
    provider = provider_with({
        T._FIELDS["orderbook_path"]: {
            "timestamp": naive, "currency": "KRW",
            "asks": [{"price": "70100", "volume": "10"}],
            "bids": [{"price": "69900", "volume": "12"}],
        },
        T._FIELDS["price_path"]: [{
            "symbol": "005930", "timestamp": naive,
            "lastPrice": "70000", "currency": "KRW",
        }],
        T._FIELDS["trades_path"]: [],
    })

    body = await provider.market_snapshot(KRX)
    quote = await provider.quote(KRX)

    assert body["quote"]["price"] == 70000.0
    assert body["quote"]["ts"] is None
    assert body["freshness"]["components"]["quote"]["status"] == "unknown"
    assert quote is None


@pytest.mark.asyncio
async def test_429_read_surfaces_and_shares_retry_after_without_retrying(monkeypatch):
    calls = 0
    sleeps: list[float] = []

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            429, headers={"Retry-After": "1"},
            json={"error": {"code": "rate-limit-exceeded"}},
        )

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(T.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(T.random, "uniform", lambda *_args: 0.0)
    client = await mock_http_client(monkeypatch, handler)
    try:
        with pytest.raises(T._TossHTTPError) as caught:
            await client.request("GET", T._FIELDS["price_path"])
    finally:
        await client.close()
    assert calls == 1
    assert caught.value.retry_after == 1.0
    assert sleeps == []


@pytest.mark.asyncio
async def test_persistent_429_stops_after_three_read_attempts(monkeypatch):
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            429, headers={"Retry-After": "9"},
            json={"error": {"code": "rate-limit-exceeded"}},
        )

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(T.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(T.random, "uniform", lambda *_args: 0.0)
    client = await mock_http_client(monkeypatch, handler)
    try:
        with pytest.raises(T._TossHTTPError) as caught:
            await client.request("GET", T._FIELDS["price_path"])
    finally:
        await client.close()
    # The venue told us to wait nine seconds. Do not retry after the internal
    # two-second latency budget and burn the same quota twice; surface it so the
    # browser schedules the next poll from Retry-After.
    assert calls == 1
    assert caught.value.status_code == 429
    assert caught.value.retry_after == 9.0


@pytest.mark.asyncio
async def test_retry_after_is_shared_across_clients_without_a_second_http_call(
    monkeypatch,
):
    calls = 0

    async def fake_token(*_args, **_kwargs):
        return "test-token"

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            429, headers={"Retry-After": "120"},
            json={"error": {"code": "rate-limit-exceeded"}},
        )

    monkeypatch.setattr(T, "toss_token", fake_token)
    first = T._TossClient("shared-client", "shared-secret")
    second = T._TossClient("shared-client", "shared-secret")
    await first._http.aclose()
    await second._http.aclose()
    first._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    second._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(T._TossHTTPError) as one:
            await first.request("GET", T._FIELDS["price_path"])
        with pytest.raises(T._TossHTTPError) as two:
            await second.request("GET", T._FIELDS["holdings_path"])
    finally:
        await first.close()
        await second.close()

    assert calls == 1
    assert one.value.retry_after == 120.0
    assert 119.0 <= two.value.retry_after <= 120.0


@pytest.mark.asyncio
async def test_short_lived_clients_keep_the_same_cooldown_gate():
    first = T._TossClient("short-lived", "same-secret")
    gate = first._rate_gate
    await gate.defer(120)
    await first.close()

    second = T._TossClient("short-lived", "same-secret")
    try:
        assert second._rate_gate is gate
        assert 119.0 <= await second._rate_gate.wait() <= 120.0
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_waiting_cadence_slot_rechecks_a_new_shared_cooldown(monkeypatch):
    clock = 0.0
    sleeping = asyncio.Event()
    release = asyncio.Event()

    def monotonic():
        return clock

    async def sleep(_seconds):
        sleeping.set()
        await release.wait()

    monkeypatch.setattr(T.time, "monotonic", monotonic)
    monkeypatch.setattr(T.asyncio, "sleep", sleep)
    gate = T._TossRateGate(gap=1.0)
    assert await gate.wait() == 0.0
    waiter = asyncio.create_task(gate.wait())
    await sleeping.wait()
    await gate.defer(10.0)
    release.set()

    assert await waiter == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_same_poll_is_coalesced_and_queue_is_bounded():
    reads = 0
    release = asyncio.Event()
    coalescer = ReadCoalescer(
        ttl_seconds=1, max_concurrent=1, max_inflight=1, max_entries=2,
    )

    async def load():
        nonlocal reads
        reads += 1
        await release.wait()
        return {"value": 1}

    first = asyncio.create_task(coalescer.get(("same",), load))
    second = asyncio.create_task(coalescer.get(("same",), load))
    await asyncio.sleep(0)
    with pytest.raises(ReadBusy):
        await coalescer.get(("different",), load)
    release.set()
    assert await asyncio.gather(first, second) == [{"value": 1}, {"value": 1}]
    assert reads == 1


@pytest.mark.asyncio
async def test_canceling_one_waiter_does_not_cancel_the_shared_read():
    started = asyncio.Event()
    release = asyncio.Event()
    coalescer = ReadCoalescer(
        ttl_seconds=1, max_concurrent=1, max_inflight=2,
    )

    async def load():
        started.set()
        await release.wait()
        return "done"

    abandoned = asyncio.create_task(coalescer.get(("same",), load))
    await started.wait()
    survivor = asyncio.create_task(coalescer.get(("same",), load))
    abandoned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await abandoned
    release.set()
    assert await survivor == "done"
    assert coalescer._inflight == {}


@pytest.mark.asyncio
async def test_abandoned_failing_singleflight_consumes_its_task_exception():
    coalescer = ReadCoalescer(
        ttl_seconds=1, max_concurrent=1, max_inflight=2,
    )
    started = asyncio.Event()
    release = asyncio.Event()
    unhandled: list[dict] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

    async def loader():
        started.set()
        await release.wait()
        raise RuntimeError("provider failed after browser disconnected")

    abandoned = asyncio.create_task(coalescer.get(("account", 1), loader))
    await started.wait()
    abandoned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await abandoned
    release.set()
    try:
        for _ in range(5):
            await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)

    assert coalescer._inflight == {}
    assert unhandled == []


@pytest.mark.asyncio
async def test_a_failed_shared_read_is_cleaned_up_for_the_next_poll():
    coalescer = ReadCoalescer(
        ttl_seconds=1, max_concurrent=1, max_inflight=1,
    )
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("upstream down")
        return "recovered"

    with pytest.raises(RuntimeError, match="upstream down"):
        await coalescer.get(("same",), load)
    assert coalescer._inflight == {}
    assert await coalescer.get(("same",), load) == "recovered"


@pytest.mark.asyncio
async def test_expired_cache_entries_are_removed_when_another_key_is_read():
    coalescer = ReadCoalescer(
        ttl_seconds=0.001, max_concurrent=1, max_inflight=2,
    )

    assert await coalescer.get(("account", 1), lambda: asyncio.sleep(0, result={
        "investable_assets": {"KRW": 420000},
    }))
    await asyncio.sleep(0.01)
    await coalescer.get(("market", 1), lambda: asyncio.sleep(0, result="fresh"))

    assert ("account", 1) not in coalescer._cache


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_SECRET_KEY", "m" * 48)
    monkeypatch.setenv("QUANT_USERS_DB", str(tmp_path / "users.db"))
    client = TestClient(create_app(None, state_path=str(tmp_path / "state.db")))
    assert client.post("/api/auth/register", json={
        "email": "market@example.com",
        "password": "correct-horse-9",
        "display_name": "market",
    }).status_code == 201
    return client


def test_market_endpoint_is_no_store_and_bounded(client):
    response = client.get("/api/market/snapshot", params={
        "ticker": "AAA", "strategy": "demo", "depth": 10,
    })
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    body = response.json()
    assert body["ticker"] == "AAA"
    assert body["quote"]["change"] is None
    assert body["market"]["state"] is None
    assert body["orders"] == [] and body["position"] is None
    assert body["capabilities"]["orders_source"] == "unavailable"
    assert body["capabilities"]["orders_complete"] is False
    assert body["capabilities"]["position_source"] == "latest_bot_ledger"
    assert body["capabilities"]["position_authoritative"] is False

    assert client.get("/api/market/snapshot", params={
        "ticker": "AAA", "strategy": "demo", "depth": 0,
    }).status_code == 422
    assert client.get("/api/market/snapshot", params={
        "ticker": "AAA", "strategy": "demo", "trade_count": 51,
    }).status_code == 422


@pytest.mark.parametrize("path", [
    "/api/status", "/api/equity", "/api/trades", "/api/pnl", "/api/tradelog",
])
def test_authenticated_ledger_reads_are_never_http_cached(client, path):
    response = client.get(path)

    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert "Cookie" in response.headers["vary"]


def test_market_position_uses_the_latest_run_without_reopening_it(client):
    state_path = client.app.state.registry.state_path(1)
    store = StateStore(state_path)
    symbol = Symbol("AAA", venue="SIM", quote_currency="USD")
    portfolio = Portfolio(100_000, "USD")
    position = portfolio.position(symbol)
    position.quantity = Decimal("3")
    position.avg_price = 101.25
    run_id = store.start_run("demo-multi-alpha", "backtest", 100_000)
    store.snapshot_positions(portfolio)
    stopped_at = "2026-09-01T01:02:03+00:00"
    store.conn.execute(
        "UPDATE runs SET stopped_at=?, requires_reconciliation=1 WHERE id=?",
        (stopped_at, run_id),
    )
    store.conn.commit()
    store.close()

    market = client.get("/api/market/snapshot", params={
        "ticker": "AAA", "strategy": "demo",
    })
    candles = client.get("/api/candles", params={
        "ticker": "AAA", "strategy": "demo", "count": 20,
    })

    assert market.status_code == candles.status_code == 200
    assert market.json()["position"]["quantity"] == 3.0
    assert candles.json()["position"]["quantity"] == 3.0
    reader = StateStore(state_path)
    row = reader.conn.execute(
        "SELECT stopped_at, requires_reconciliation FROM runs WHERE id=?",
        (run_id,),
    ).fetchone()
    reader.close()
    assert row["stopped_at"] == stopped_at
    assert row["requires_reconciliation"] == 1


@pytest.mark.parametrize(
    ("path", "result_key"),
    [("/api/equity", "points"), ("/api/trades", "trades")],
)
def test_legacy_dashboard_reads_do_not_reopen_a_stopped_run(
        client, path, result_key):
    """A browser refresh must not turn a terminal run back into a live one."""
    config = _template_config("demo")
    assert config is not None
    client.app.state.quant.config = config
    state_path = client.app.state.registry.state_path(1)
    store = StateStore(state_path)
    run_id = store.start_run(config.name, config.mode.value, 100_000)
    store.record_equity(datetime.now(UTC), 101_000, 99_000, 0.01)
    store.record_closed_trade({
        "symbol": "AAA", "side": "sell", "quantity": 1,
        "entry_price": 100, "exit_price": 101,
        "entry_ts": "2026-09-01T00:00:00+00:00",
        "exit_ts": "2026-09-01T01:00:00+00:00",
        "pnl": 1, "pnl_pct": 1, "fees": 0, "exit_tag": "test",
    })
    stopped_at = "2026-09-01T01:02:03+00:00"
    store.conn.execute(
        "UPDATE runs SET stopped_at=?, requires_reconciliation=1 WHERE id=?",
        (stopped_at, run_id),
    )
    store.conn.commit()
    store.close()

    response = client.get(path)

    assert response.status_code == 200, response.text
    assert len(response.json()[result_key]) == 1
    reader = StateStore(state_path)
    row = reader.conn.execute(
        "SELECT stopped_at, requires_reconciliation FROM runs WHERE id=?",
        (run_id,),
    ).fetchone()
    reader.close()
    assert row["stopped_at"] == stopped_at
    assert row["requires_reconciliation"] == 1


def test_broker_account_is_no_store_and_shortly_coalesced(client, monkeypatch):
    import quant.webapp.registry as registry_module

    calls = 0

    async def account(_self, _user_id, _config):
        nonlocal calls
        calls += 1
        return {"supported": True, "cash_buying_power": {"KRW": 420000}}

    monkeypatch.setattr(registry_module.UserRegistry, "broker_account", account)
    first = client.get("/api/account/broker", params={"strategy": "demo"})
    second = client.get("/api/account/broker", params={"strategy": "demo"})

    assert first.status_code == second.status_code == 200
    assert first.headers["cache-control"] == "private, no-store, max-age=0"
    assert second.json()["cash_buying_power"]["KRW"] == 420000
    assert calls == 1

    # Same adapter type does not prove same routed account/exchange.
    assert client.get("/api/account/broker", params={
        "strategy": "kr_toss",
    }).status_code == 200
    assert client.get("/api/account/broker", params={
        "strategy": "kr_toss_desk",
    }).status_code == 200
    assert calls == 3


def test_unsupported_broker_account_adapter_is_still_closed(client, monkeypatch):
    import quant.strategy.builder as builder_module

    closed = 0

    class UnsupportedBroker:
        async def close(self):
            nonlocal closed
            closed += 1

    monkeypatch.setattr(
        builder_module,
        "build_brokerage",
        lambda *_args, **_kwargs: UnsupportedBroker(),
    )
    config = _template_config("demo")
    assert config is not None

    result = asyncio.run(client.app.state.registry.broker_account(1, config))

    assert result["supported"] is False
    assert closed == 1


def test_broker_account_disables_startup_reconciliation_before_connect(
    client, monkeypatch,
):
    import quant.strategy.builder as builder_module

    calls = {"connect": 0, "overview": 0, "close": 0}

    class ReadOnlyBroker:
        reconcile_on_start = True

        async def connect(self):
            calls["connect"] += 1
            assert self.reconcile_on_start is False

        async def account_overview(self):
            calls["overview"] += 1
            return {"source": "test"}

        async def close(self):
            calls["close"] += 1

    monkeypatch.setattr(
        builder_module,
        "build_brokerage",
        lambda *_args, **_kwargs: ReadOnlyBroker(),
    )
    config = _template_config("demo")
    assert config is not None

    result = asyncio.run(client.app.state.registry.broker_account(1, config))

    assert result == {"source": "test", "supported": True}
    assert calls == {"connect": 1, "overview": 1, "close": 1}


def test_generic_market_snapshot_reports_real_quote_age(client, monkeypatch):
    import quant.webapp.registry as registry_module

    closed = 0

    class GenericProvider:
        name = "generic-test"

        async def quote(self, symbol):
            return Quote(
                symbol,
                datetime.now(UTC) - timedelta(hours=2),
                bid=99.0,
                ask=101.0,
            )

        async def describe_many(self, _tickers):
            return {}

        async def close(self):
            nonlocal closed
            closed += 1

    monkeypatch.setattr(
        registry_module.UserRegistry,
        "data_provider",
        lambda _self, _user_id, _config: GenericProvider(),
    )

    response = client.get("/api/market/snapshot", params={
        "ticker": "AAA", "strategy": "demo",
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["quote"]["price"] == 100.0
    assert body["freshness"]["status"] == "stale"
    assert body["freshness"]["age_ms"] >= 2 * 60 * 60 * 1000
    assert body["freshness"]["components"]["quote"]["status"] == "stale"
    assert closed == 1


def test_generic_crossed_quote_is_not_displayed_as_fresh(client, monkeypatch):
    import quant.webapp.registry as registry_module

    class CrossedProvider:
        name = "generic-test"

        async def quote(self, symbol):
            return Quote(symbol, datetime.now(UTC), bid=101.0, ask=99.0)

        async def describe_many(self, _tickers):
            return {}

        async def close(self):
            return None

    monkeypatch.setattr(
        registry_module.UserRegistry,
        "data_provider",
        lambda _self, _user_id, _config: CrossedProvider(),
    )

    response = client.get("/api/market/snapshot", params={
        "ticker": "AAA", "strategy": "demo",
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["quote"]["price"] is None
    assert body["freshness"]["status"] == "unknown"
    assert body["capabilities"]["top_of_book"] is False


def test_candles_cannot_reintroduce_a_wrong_symbol_quote(client, monkeypatch):
    import quant.webapp.registry as registry_module

    class Provider:
        async def latest_bars(self, symbol, timeframe, _count):
            return [Bar(
                symbol, datetime.now(UTC) - timedelta(days=2),
                99.0, 102.0, 98.0, 100.0, 10.0, timeframe,
            )]

        async def quote(self, _symbol):
            return Quote(
                Symbol("WRONG", venue="SIM"), datetime.now(UTC),
                bid=998.0, ask=1000.0,
            )

        async def describe_many(self, _tickers):
            return {}

        async def close(self):
            return None

    monkeypatch.setattr(
        registry_module.UserRegistry,
        "data_provider",
        lambda _self, _user_id, _config: Provider(),
    )

    response = client.get("/api/candles", params={
        "ticker": "AAA", "strategy": "demo", "count": 20,
    })

    assert response.status_code == 200, response.text
    assert response.json()["quote"]["price"] == 100.0
    assert response.json()["quote"]["price_kind"] == "bar_close"


def test_generic_negative_quote_size_fails_closed(client, monkeypatch):
    import quant.webapp.registry as registry_module

    class Provider:
        name = "generic-test"

        async def quote(self, symbol):
            return Quote(
                symbol, datetime.now(UTC), bid=99.0, ask=101.0,
                bid_size=-1.0, ask_size=2.0,
            )

        async def describe_many(self, _tickers):
            return {}

        async def close(self):
            return None

    monkeypatch.setattr(
        registry_module.UserRegistry,
        "data_provider",
        lambda _self, _user_id, _config: Provider(),
    )

    body = client.get("/api/market/snapshot", params={
        "ticker": "AAA", "strategy": "demo",
    }).json()

    assert body["quote"]["price"] is None
    assert body["freshness"]["status"] == "unknown"
    assert body["capabilities"]["top_of_book"] is False


@pytest.mark.parametrize(("path", "params"), [
    ("/api/equity", {"strategy": "missing", "mode": "live"}),
    ("/api/trades", {"strategy": "missing", "mode": "live"}),
    ("/api/account/broker", {"strategy": "missing"}),
    ("/api/candles", {"ticker": "AAA", "strategy": "missing"}),
    ("/api/market/snapshot", {"ticker": "AAA", "strategy": "missing"}),
    ("/api/lookup", {"q": "AAA", "strategy": "missing"}),
])
def test_explicit_missing_strategy_never_falls_back_to_another_account(
    client, path, params,
):
    response = client.get(path, params=params)

    assert response.status_code == 400, response.text


def test_stopped_equity_and_trades_honor_explicit_strategy_and_mode(client):
    config = _template_config("demo")
    assert config is not None
    state_path = client.app.state.registry.state_path(1)
    store = StateStore(state_path)
    store.start_run(config.name, "live", 100_000)
    store.record_equity(datetime.now(UTC), 123_456, 120_000, 0.02)
    store.close()

    equity = client.get("/api/equity", params={
        "strategy": "demo", "mode": "live",
    })
    trades = client.get("/api/trades", params={
        "strategy": "demo", "mode": "live",
    })

    assert equity.status_code == trades.status_code == 200
    assert equity.json()["strategy"] == config.name
    assert equity.json()["mode"] == "live"
    assert equity.json()["points"][-1]["equity"] == 123_456
    assert trades.json()["strategy"] == config.name
    assert trades.json()["mode"] == "live"


def test_broker_account_preserves_venue_retry_after(client, monkeypatch):
    import quant.webapp.registry as registry_module

    async def limited(_self, _user_id, _config):
        raise T._TossHTTPError(429, "quota", retry_after=120)

    monkeypatch.setattr(registry_module.UserRegistry, "broker_account", limited)

    response = client.get("/api/account/broker", params={"strategy": "demo"})

    assert response.status_code == 200
    assert response.json()["retry_after_ms"] == 120_000


def test_broker_account_preserves_retry_after_from_a_503(client, monkeypatch):
    import quant.webapp.registry as registry_module

    async def unavailable(_self, _user_id, _config):
        raise T._TossHTTPError(503, "maintenance", retry_after=120)

    monkeypatch.setattr(registry_module.UserRegistry, "broker_account", unavailable)

    response = client.get("/api/account/broker", params={"strategy": "demo"})

    assert response.status_code == 200
    assert response.json()["retry_after_ms"] == 120_000


def test_candles_closes_its_per_request_provider(client, monkeypatch):
    import quant.webapp.registry as registry_module

    closed = 0

    class GenericProvider:
        async def latest_bars(self, _symbol, _timeframe, _count):
            return []

        async def quote(self, _symbol):
            return None

        async def describe_many(self, _tickers):
            return {}

        async def close(self):
            nonlocal closed
            closed += 1

    monkeypatch.setattr(
        registry_module.UserRegistry,
        "data_provider",
        lambda _self, _user_id, _config: GenericProvider(),
    )

    response = client.get("/api/candles", params={
        "ticker": "AAA", "strategy": "demo", "count": 20,
    })

    assert response.status_code == 200, response.text
    assert closed == 1


def test_candles_closes_provider_when_state_store_initialization_fails(
        client, monkeypatch):
    import quant.api.server as server_module
    import quant.webapp.registry as registry_module

    closed = 0

    class GenericProvider:
        async def latest_bars(self, _symbol, _timeframe, _count):
            return []

        async def quote(self, _symbol):
            return None

        async def close(self):
            nonlocal closed
            closed += 1

    monkeypatch.setattr(
        registry_module.UserRegistry,
        "data_provider",
        lambda _self, _user_id, _config: GenericProvider(),
    )

    def fail_state(_path):
        raise RuntimeError("state database unavailable")

    monkeypatch.setattr(server_module, "StateStore", fail_state)

    with pytest.raises(RuntimeError, match="state database unavailable"):
        client.get("/api/candles", params={
            "ticker": "AAA", "strategy": "demo", "count": 20,
        })
    assert closed == 1
