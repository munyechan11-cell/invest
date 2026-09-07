"""체결 조회 채널 잠금은 스스로 풀릴 수 있어야 한다.

잠금이 생기는 경로 하나는 **로컬에 주문을 남기지 않습니다**: 토스 주문 POST 가
두 번 애매하게 실패하면(전송 오류·5xx·깨진 2xx) 같은 `clientOrderId` 가
접수됐을 수도 있어 채널을 잠그는데, 우리 주문 표는 비어 있습니다.

그러면 세 가지가 맞물려 잠금이 **영구** 가 됐습니다.
 1. `fill_channel_up()` 은 주문을 하나라도 조회했을 때만 불린다.
 2. `LiveTrader._poll_live_fills` 는 로컬 미결이 없으면 `poll_fills()` 를
    아예 부르지 않는다.
 3. `_guard` 는 방향을 가리지 않고 거절한다 — **손절까지**.

재시작해도 종료가 깨끗하지 않아 격리로 갑니다. 즉 사용자는 포지션을 든 채
아무 주문도 낼 수 없게 됩니다.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from quant.brokerage.base import BrokerageError
from quant.brokerage.toss_broker import TossBrokerage
from quant.core.account import Portfolio
from quant.core.types import Order, OrderSide, OrderType, Symbol

SYM = Symbol("005930", venue="toss", quote_currency="KRW",
             lot_size=Decimal("1"), tick_size=Decimal("100"))


class FakeTossClient:
    """미결 주문 목록만 답하는 최소 클라이언트."""

    def __init__(self, open_rows=None, fail=False):
        self.open_rows = list(open_rows or [])
        self.fail = fail
        self.requests: list[tuple] = []

    async def request(self, method, path, **kw):
        self.requests.append((method, path))
        if self.fail:
            raise BrokerageError("토스 미결 조회 503")
        # 공식 미결 목록 응답의 모양 그대로 (`_venue_open_orders` 가 요구합니다).
        return {"orders": list(self.open_rows), "hasNext": False,
                "nextCursor": None}

    async def close(self):
        return None


def broker(client) -> TossBrokerage:
    b = TossBrokerage.__new__(TossBrokerage)
    # 어댑터를 통째로 세우지 않고 이 경로가 쓰는 것만 채웁니다 — 네트워크는
    # `client` 하나로만 나갑니다.
    Portfolio.__init__  # noqa: B018 - 가독성용
    book = Portfolio(1_000_000.0, "KRW")
    from quant.brokerage.live_base import LiveBrokerage

    LiveBrokerage.__init__(b, book, live=True, max_order_notional=1e9)
    b.client = client
    return b


@pytest.mark.asyncio
async def test_a_locked_channel_reopens_when_the_venue_has_no_unknown_orders():
    b = broker(FakeTossClient(open_rows=[]))
    b.fill_channel_down("주문 응답을 두 번 받지 못했습니다")
    assert b.fill_channel_ok is False

    await b.poll_fills()

    assert b.fill_channel_ok is True, "확인이 끝났는데도 잠긴 채로 남았다"


@pytest.mark.asyncio
async def test_it_stays_locked_while_an_unknown_order_rests_at_the_venue():
    """유령 주문이 실제로 걸려 있으면 풀면 안 됩니다 — 잠금의 존재 이유입니다."""
    b = broker(FakeTossClient(open_rows=[{"orderId": "someone-elses"}]))
    b.fill_channel_down("주문 응답을 두 번 받지 못했습니다")

    await b.poll_fills()

    assert b.fill_channel_ok is False


@pytest.mark.asyncio
async def test_it_stays_locked_when_the_venue_cannot_be_read():
    """모르면 잠근 채로 둡니다 — 조회 실패는 '없음' 이 아닙니다."""
    b = broker(FakeTossClient(fail=True))
    b.fill_channel_down("주문 응답을 두 번 받지 못했습니다")

    await b.poll_fills()

    assert b.fill_channel_ok is False


@pytest.mark.asyncio
async def test_a_healthy_channel_is_not_probed():
    """정상일 때는 추가 요청을 만들지 않습니다 — 토스는 요청 간격이 있습니다."""
    client = FakeTossClient(open_rows=[])
    b = broker(client)

    await b.poll_fills()

    assert client.requests == []


@pytest.mark.asyncio
async def test_the_trader_gives_the_adapter_that_chance():
    """`_poll_live_fills` 가 로컬 미결이 없다고 그냥 돌아가면 위 경로는 죽습니다."""
    from quant.live.trader import LiveTrader

    class Adapter:
        venue_backed = True

        def __init__(self):
            self.fill_channel_ok = False
            self.polls = 0

        async def open_orders(self):
            return []

        def drain_pending_fills(self):
            return []

        async def poll_fills(self):
            self.polls += 1
            self.fill_channel_ok = True
            return []

    trader = LiveTrader.__new__(LiveTrader)
    trader.engine = SimpleNamespace(brokerage=Adapter())
    trader._next_fill_poll_at = 0.0
    trader._fill_poll_backoff_s = trader.FILL_POLL_S

    assert await trader._poll_live_fills() == []

    assert trader.engine.brokerage.polls == 1
    assert trader.engine.brokerage.fill_channel_ok is True


@pytest.mark.asyncio
async def test_a_healthy_adapter_is_not_polled_without_open_orders():
    """회귀 방지: 정상 상태에서 3초마다 쓸데없는 조회를 만들지 않습니다."""
    from quant.live.trader import LiveTrader

    class Adapter:
        venue_backed = True
        fill_channel_ok = True

        def __init__(self):
            self.polls = 0

        async def open_orders(self):
            return []

        def drain_pending_fills(self):
            return []

        async def poll_fills(self):        # pragma: no cover
            self.polls += 1
            return []

    trader = LiveTrader.__new__(LiveTrader)
    trader.engine = SimpleNamespace(brokerage=Adapter())
    trader._next_fill_poll_at = 0.0
    trader._fill_poll_backoff_s = trader.FILL_POLL_S

    assert await trader._poll_live_fills() == []
    assert trader.engine.brokerage.polls == 0


def test_the_lock_blocks_an_exit_which_is_why_it_must_be_escapable():
    """잠금이 손절까지 막는다는 사실 자체를 고정합니다 — 수정의 동기입니다."""
    b = broker(FakeTossClient(open_rows=[]))
    held = b.portfolio.position(SYM)
    held.quantity, held.avg_price = Decimal("10"), 70_000.0
    held.mark(70_000.0)
    b.fill_channel_down("주문 응답을 두 번 받지 못했습니다")

    exit_order = Order(SYM, OrderSide.SELL, Decimal("10"), OrderType.MARKET)
    with pytest.raises(BrokerageError, match="체결 조회 채널"):
        b._guard(exit_order)
