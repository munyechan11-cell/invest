"""Lazily-bound asyncio primitives.

Several components are constructed while no event loop is running — the config
builder assembles the whole engine synchronously, long before `asyncio.run`.
Creating an `asyncio.Semaphore` or `Lock` at that point is a portability trap:
older Pythons bind the primitive to whatever loop is current at construction,
so it either raises immediately or, worse, silently binds to a loop that is
never the one the code later runs on.

These wrappers defer creation to first use and rebind if the running loop
changes, which also makes a component safe to reuse across a `asyncio.run`
boundary — exactly what the test-suite does.
"""
from __future__ import annotations

import asyncio


class LazySemaphore:
    """`async with sem:` — created on first use, rebound if the loop changes."""

    def __init__(self, value: int = 1):
        if value < 1:
            raise ValueError("semaphore value must be >= 1")
        self._value = value
        self._sem: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _resolve(self) -> asyncio.Semaphore:
        loop = asyncio.get_event_loop()
        if self._sem is None or self._loop is not loop:
            self._sem = asyncio.Semaphore(self._value)
            self._loop = loop
        return self._sem

    async def __aenter__(self):
        self._acquired = self._resolve()
        await self._acquired.acquire()
        return self

    async def __aexit__(self, *exc):
        self._acquired.release()
        return False

    @property
    def value(self) -> int:
        return self._value


class LazyLock:
    """`async with lock:` with the same deferred binding."""

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _resolve(self) -> asyncio.Lock:
        loop = asyncio.get_event_loop()
        if self._lock is None or self._loop is not loop:
            self._lock = asyncio.Lock()
            self._loop = loop
        return self._lock

    async def __aenter__(self):
        self._acquired = self._resolve()
        await self._acquired.acquire()
        return self

    async def __aexit__(self, *exc):
        self._acquired.release()
        return False
