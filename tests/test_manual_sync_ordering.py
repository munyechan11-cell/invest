"""Operator-triggered reconciliation follows the live exact-once ordering."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from quant.api.server import Desk
from quant.core.engine import Engine, UnsafeShutdownError


class _Broker:
    def __init__(self, calls: list[str]):
        self.calls = calls

    async def sync(self) -> dict:
        self.calls.append("sync")
        return {"ok": True}


class _Engine:
    def __init__(self, calls: list[str]):
        self.calls = calls
        self.brokerage = _Broker(calls)

    async def settle_live_fills(self) -> None:
        self.calls.append("settle")


class _Desk(Desk):
    def __init__(self, engine: _Engine):
        self._trader = SimpleNamespace(engine=engine)

    def require_trader(self, agent_id: str = ""):
        return self._trader


async def test_manual_sync_books_pending_fills_before_adopting_venue_truth():
    calls: list[str] = []
    desk = _Desk(_Engine(calls))

    assert await desk.sync() == {"ok": True}
    assert calls == ["settle", "sync"]


class _ShutdownBroker:
    def __init__(
        self,
        calls: list[str],
        *,
        cancel_result: bool = True,
        remote_count: int = 0,
        remote_error: Exception | None = None,
    ):
        self.calls = calls
        self.cancel_result = cancel_result
        self.remote_count = remote_count
        self.remote_error = remote_error
        self.orders = [
            SimpleNamespace(symbol=SimpleNamespace(ticker="005930")),
        ]

    async def open_orders(self):
        self.calls.append("open_orders")
        return list(self.orders)

    async def cancel(self, _order):
        self.calls.append("cancel")
        if self.cancel_result:
            self.orders.clear()
        return self.cancel_result

    async def shutdown_remote_open_order_count(self):
        self.calls.append("remote_open_orders")
        if self.remote_error is not None:
            raise self.remote_error
        return self.remote_count

    async def close(self):
        self.calls.append("close")


class _Bus:
    def __init__(self, calls: list[str]):
        self.calls = calls

    async def publish(self, *_args, **_kwargs):
        self.calls.append("stopped_event")


async def test_shutdown_books_cancel_race_fills_before_closing_the_broker():
    calls: list[str] = []
    engine = Engine.__new__(Engine)
    engine.ctx = SimpleNamespace(bus=_Bus(calls))
    engine.brokerage = _ShutdownBroker(calls)
    engine._started = True

    async def settle():
        calls.append("settle")

    engine.settle_live_fills = settle

    await engine.stop()

    assert calls == [
        "open_orders", "cancel", "settle", "open_orders",
        "remote_open_orders", "close", "stopped_event",
    ]
    assert engine._started is False


async def test_shutdown_still_closes_broker_when_final_fill_lookup_fails():
    calls: list[str] = []
    engine = Engine.__new__(Engine)
    engine.ctx = SimpleNamespace(bus=_Bus(calls))
    engine.brokerage = _ShutdownBroker(calls)
    engine._started = True

    async def fail_settle():
        calls.append("settle")
        raise RuntimeError("order detail unavailable")

    engine.settle_live_fills = fail_settle

    with pytest.raises(UnsafeShutdownError, match="마지막 체결 조회"):
        await engine.stop()

    assert calls == [
        "open_orders", "cancel", "settle", "open_orders",
        "remote_open_orders", "close",
    ]
    assert engine._started is False


async def test_shutdown_cancel_false_is_unsafe_even_after_fill_drain_and_close():
    calls: list[str] = []
    engine = Engine.__new__(Engine)
    engine.ctx = SimpleNamespace(bus=_Bus(calls))
    engine.brokerage = _ShutdownBroker(calls, cancel_result=False)
    engine._started = True

    async def settle():
        calls.append("settle")

    engine.settle_live_fills = settle

    with pytest.raises(UnsafeShutdownError, match="취소 완료") as exc_info:
        await engine.stop()

    assert "토스 앱" in str(exc_info.value)
    assert calls == [
        "open_orders", "cancel", "settle", "open_orders",
        "remote_open_orders", "close",
    ]
    assert "stopped_event" not in calls
    assert engine._started is False


async def test_shutdown_remote_order_remaining_is_unsafe_after_local_cancel():
    calls: list[str] = []
    engine = Engine.__new__(Engine)
    engine.ctx = SimpleNamespace(bus=_Bus(calls))
    engine.brokerage = _ShutdownBroker(calls, remote_count=1)
    engine._started = True

    async def settle():
        calls.append("settle")

    engine.settle_live_fills = settle

    with pytest.raises(UnsafeShutdownError, match="증권사에 미결 주문 1건"):
        await engine.stop()

    assert calls[-1] == "close"
    assert "stopped_event" not in calls


async def test_shutdown_remote_order_lookup_failure_is_unsafe():
    calls: list[str] = []
    engine = Engine.__new__(Engine)
    engine.ctx = SimpleNamespace(bus=_Bus(calls))
    engine.brokerage = _ShutdownBroker(
        calls,
        remote_error=RuntimeError("orders endpoint unavailable"),
    )
    engine._started = True

    async def settle():
        calls.append("settle")

    engine.settle_live_fills = settle

    with pytest.raises(UnsafeShutdownError, match="최종 확인 실패"):
        await engine.stop()

    assert calls[-1] == "close"
    assert "stopped_event" not in calls


async def test_shutdown_cannot_clear_an_unrecovered_fill_channel_lock():
    calls: list[str] = []
    engine = Engine.__new__(Engine)
    engine.ctx = SimpleNamespace(bus=_Bus(calls))
    broker = _ShutdownBroker(calls)
    broker.sends_orders = True
    broker.fill_channel_ok = False
    broker.fill_channel_error = "accepted order response was ambiguous"
    engine.brokerage = broker
    engine._started = True

    async def settle():
        calls.append("settle")

    engine.settle_live_fills = settle

    with pytest.raises(UnsafeShutdownError, match="체결 조회 채널"):
        await engine.stop()

    assert calls[-1] == "close"
    assert "stopped_event" not in calls
