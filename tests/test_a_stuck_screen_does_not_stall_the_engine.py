"""화면 하나가 막혀도 엔진은 계속 돌아야 한다.

`Hub.publish` 를 부르는 것은 `EventBus.emit` 이고, 그것을 부르는 것은 봉을
처리하는 엔진입니다. 예전에는 여기서 화면마다 `await ws.send_text(...)` 를
했으므로 **소켓 하나가 막히면 그 사용자의 봉 처리와 봉 사이 손절이 함께
멈췄습니다.** 화면을 끈 채 잠긴 폰이나 끊어진 TCP 경로(재전송 타임아웃 수 분)
하나면 충분하고, 그룹이면 엔진 넷이 함께 섭니다. 심의 이벤트는 토론 전문을
담아 수십 KB 라 송신 버퍼가 금방 찹니다.

이제 화면마다 큐와 전용 송신 태스크가 있습니다. 막히는 것은 그 화면뿐이고,
큐가 차면 오래된 것부터 버립니다 — 밀린 화면에 옛 이벤트를 마저 보내려고
엔진을 세울 수는 없습니다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from quant.api.server import Hub
from quant.core.events import Event, EventType
from quant.core.types import UTC


class StuckSocket:
    """영영 돌아오지 않는 소켓. 잠긴 폰이 이렇게 보입니다."""

    def __init__(self):
        self.sends = 0

    async def send_text(self, _text):
        self.sends += 1
        await asyncio.Event().wait()          # 영원히


class FastSocket:
    def __init__(self):
        self.received: list[str] = []

    async def send_text(self, text):
        self.received.append(text)


def event(n: int) -> Event:
    return Event(EventType.EQUITY, {"n": n}, source="test")


@pytest.fixture
async def hub():
    """붙인 화면을 끝에 반드시 떼어 냅니다 — 남으면 송신 태스크가 샙니다.

    비동기 fixture 여야 합니다. 정리가 이벤트 루프 **안에서** 돌아야
    `task.cancel()` 이 닫힌 루프를 만나지 않습니다.
    """
    made = Hub()
    yield made
    for ws in list(made.clients):
        made.detach(ws)
    await asyncio.sleep(0)          # 취소가 실제로 전달되게 한 번 양보


@pytest.mark.asyncio
async def test_publishing_to_a_stuck_screen_returns_immediately(hub):
    hub.attach(StuckSocket())

    await asyncio.wait_for(hub.publish(event(1)), timeout=1.0)


@pytest.mark.asyncio
async def test_a_stuck_screen_does_not_starve_a_healthy_one(hub):
    hub.attach(StuckSocket())
    fast = FastSocket()
    hub.attach(fast)

    for i in range(5):
        await hub.publish(event(i))
    await asyncio.sleep(0.05)                 # 송신 태스크가 큐를 비울 틈

    assert len(fast.received) == 5, "막힌 화면 때문에 멀쩡한 화면이 굶었다"


@pytest.mark.asyncio
async def test_a_backed_up_queue_is_bounded(hub):
    """무한히 쌓이면 메모리가 대신 죽습니다."""
    stuck = StuckSocket()
    hub.attach(stuck)

    for i in range(Hub.QUEUE_MAX * 3):
        await hub.publish(event(i))

    assert hub._queues[stuck].qsize() <= Hub.QUEUE_MAX


@pytest.mark.asyncio
async def test_the_newest_events_are_the_ones_kept(hub):
    """버릴 때는 오래된 것부터 — 화면에 필요한 것은 최신 상태입니다."""
    slow = StuckSocket()
    hub.attach(slow)
    for i in range(Hub.QUEUE_MAX * 2):
        await hub.publish(event(i))

    queued = []
    queue = hub._queues[slow]
    while not queue.empty():
        queued.append(queue.get_nowait())

    assert f'"n": {Hub.QUEUE_MAX * 2 - 1}' in queued[-1]


@pytest.mark.asyncio
async def test_events_reach_a_screen_in_order(hub):
    """화면별 순서는 그대로여야 합니다 — 체결 뒤에 주문이 오면 안 됩니다."""
    fast = FastSocket()
    hub.attach(fast)

    for i in range(20):
        await hub.publish(event(i))
    await asyncio.sleep(0.05)

    order = [int(text.split('"n": ')[1].split("}")[0]) for text in fast.received]
    assert order == list(range(20))


@pytest.mark.asyncio
async def test_detaching_stops_the_sender(hub):
    fast = FastSocket()
    hub.attach(fast)
    hub.detach(fast)

    await hub.publish(event(1))
    await asyncio.sleep(0.02)

    assert fast.received == []
    assert fast not in hub.clients


@pytest.mark.asyncio
async def test_the_ring_still_holds_recent_events_for_a_new_screen(hub):
    """회귀 방지: 붙자마자 최근 이벤트를 받는 경로는 그대로여야 합니다."""
    for i in range(3):
        await hub.publish(event(i))

    assert len(hub.recent(10)) == 3


@pytest.mark.asyncio
async def test_the_engine_event_bus_is_not_blocked_either(hub):
    """실제 경로로 확인합니다 — 엔진은 `bus.publish` 를 await 합니다."""
    from quant.core.events import EventBus

    hub.attach(StuckSocket())
    bus = EventBus()
    bus.on(None, hub.publish)

    ts = datetime.now(UTC)
    await asyncio.wait_for(
        bus.publish(EventType.ORDER_FILLED, {"ts": ts.isoformat()}), timeout=1.0)
