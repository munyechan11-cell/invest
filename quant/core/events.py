"""A tiny typed event bus.

Everything interesting in the engine is an event: a bar closed, an order filled,
a risk model liquidated something. Routing them through one bus means the API
server, the persistence layer and the notifier all observe the same stream with
no extra plumbing in the trading path.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, Union

from quant.core.types import utcnow

log = logging.getLogger("quant.events")


class EventType(str, Enum):
    BAR = "bar"
    QUOTE = "quote"
    INSIGHT = "insight"
    TARGET = "target"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_FILLED = "order_filled"
    ORDER_CANCELED = "order_canceled"
    ORDER_REJECTED = "order_rejected"
    TRADE_CLOSED = "trade_closed"
    RISK_ACTION = "risk_action"
    PROTECTION = "protection"
    EQUITY = "equity"
    STATE = "state"
    ERROR = "error"
    LOG = "log"


@dataclass
class Event:
    type: EventType
    payload: Any = None
    ts: datetime = field(default_factory=utcnow)
    source: str = ""


Handler = Callable[[Event], Union[Awaitable[None], None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[EventType, list[Handler]] = defaultdict(list)
        self._wildcard: list[Handler] = []

    def on(self, event_type: EventType | None, handler: Handler) -> Callable[[], None]:
        """Subscribe. Pass `None` to receive every event. Returns an unsubscribe."""
        bucket = self._wildcard if event_type is None else self._handlers[event_type]
        bucket.append(handler)
        return lambda: bucket.remove(handler) if handler in bucket else None

    async def emit(self, event: Event) -> None:
        for handler in (*self._handlers.get(event.type, ()), *self._wildcard):
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                # A misbehaving observer must never break the trading loop.
                log.exception("event handler failed for %s", event.type)

    async def publish(self, event_type: EventType, payload: Any = None, source: str = "") -> None:
        await self.emit(Event(event_type, payload, source=source))
