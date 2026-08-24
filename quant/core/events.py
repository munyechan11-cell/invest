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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

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
    DELIBERATION = "deliberation"
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


# 어노테이션이 아니라 **실행되는 할당**이라 `from __future__ import annotations`
# 가 미뤄주지 않습니다. `X | None` 로 적으면 파이썬 3.9 에서 import 자체가
# 실패하고, 그 한 줄이 패키지 전체를 끌고 내려갑니다. 배포는 3.13 이지만
# 개발 기기가 3.9 인 경우가 있어 여기만 옛 표기를 지킵니다.
Handler = Callable[[Event], Optional[Awaitable[None]]]  # noqa: UP007, UP045


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
