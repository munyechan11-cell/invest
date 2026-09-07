"""KIS 목록 조회는 끝까지 읽어야 하고, 애매한 주문 실패에는 눈을 감아야 한다.

**페이징.** KIS 는 잔고·체결 목록을 나눠 주고 이어받기 키를 `ctx_area_nk###`,
"다음 장 있음" 을 응답 헤더 `tr_cont`(F/M)로 알립니다. 첫 장만 읽으면 둘째
장의 보유가 `_venue_positions()` 에 없고, `_sync_once` 는 그것을 "외부에서
청산됐다" 로 읽어 로컬 수량을 0 으로 만든 뒤 **원가만큼 현금을 지어냅니다.**
엔진은 그 종목이 비었다고 믿어 다시 사고, 사라진 수량에는 손절이 걸리지
않습니다.

**애매한 주문 실패.** 응답을 못 받은 것은 "주문이 안 나갔다" 가 아닙니다.
그대로 REJECTED 로 적으면 로컬에 주문이 남지 않아 다음 봉이 같은 주문을 또
보냅니다 — 지정가면 둘 다 체결되어 노출이 두 배가 되고, KIS 는 취소가 구현돼
있지 않아 되돌릴 수도 없습니다.
"""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from quant.brokerage.base import BrokerageError
from quant.brokerage.kis_broker import KisBrokerage
from quant.core.account import Portfolio
from quant.core.types import Order, OrderSide, OrderType, Symbol

SYM = Symbol("005930", venue="kis", quote_currency="KRW",
             lot_size=Decimal("1"), tick_size=Decimal("1"))


class FakeResponse:
    def __init__(self, payload, headers=None, status=200):
        self._payload = payload
        self.headers = headers or {}
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom", request=httpx.Request("GET", "http://x"),
                response=httpx.Response(self.status_code))


class FakeClient:
    """`tr_cont` 로 다음 장이 있다고 말하는 계좌."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls: list[dict] = []

    async def get(self, url, headers=None, params=None):
        self.calls.append({"url": url, "headers": dict(headers or {}),
                           "params": dict(params or {})})
        payload, more = self.pages[min(len(self.calls) - 1, len(self.pages) - 1)]
        return FakeResponse(payload, headers={"tr_cont": "M" if more else "D"})

    async def post(self, url, headers=None, json=None):     # pragma: no cover
        raise AssertionError("이 테스트는 주문을 내지 않습니다")


def broker(client, *, live=False) -> KisBrokerage:
    b = KisBrokerage(Portfolio(1_000_000.0, "KRW"), app_key="k",
                     app_secret="s", account_no="12345678", live=live,
                     max_order_notional=1e9, allow_env_credentials=False)
    b._client = client

    async def _headers(tr_id, body=None):
        return {"tr_id": tr_id}

    b._headers = _headers
    return b


def holdings_page(pdno, qty, nk="", deposit="1000"):
    return {
        "rt_cd": "0",
        "output1": [{"pdno": pdno, "hldg_qty": str(qty),
                     "pchs_avg_pric": "70000"}],
        "output2": [{"dnca_tot_amt": deposit}],
        "ctx_area_fk100": "FK", "ctx_area_nk100": nk,
    }


@pytest.mark.asyncio
async def test_every_holdings_page_is_read():
    client = FakeClient([
        (holdings_page("005930", 10, nk="NEXT"), True),
        (holdings_page("000660", 5), False),
    ])
    b = broker(client)

    out, costs = await b._domestic_balance()

    assert out == {"kis:005930": Decimal("10"), "kis:000660": Decimal("5")}
    assert set(costs) == {"kis:005930", "kis:000660"}
    assert len(client.calls) == 2, "둘째 장을 읽지 않았다"


@pytest.mark.asyncio
async def test_the_continuation_keys_are_sent_back():
    client = FakeClient([
        (holdings_page("005930", 10, nk="NEXT"), True),
        (holdings_page("000660", 5), False),
    ])
    b = broker(client)

    await b._domestic_balance()

    second = client.calls[1]
    assert second["params"]["CTX_AREA_NK100"] == "NEXT"
    assert second["headers"].get("tr_cont") == "N"


@pytest.mark.asyncio
async def test_a_missing_continuation_key_is_an_error_not_a_partial_answer():
    """절반만 아는 잔고는 없는 종목을 청산으로 읽습니다 — 실패가 낫습니다."""
    client = FakeClient([(holdings_page("005930", 10, nk=""), True)])
    b = broker(client)

    with pytest.raises(BrokerageError, match="이어받기 키"):
        await b._domestic_balance()


@pytest.mark.asyncio
async def test_an_endless_list_is_an_error_not_a_partial_answer():
    client = FakeClient([(holdings_page("005930", 10, nk="NEXT"), True)])
    b = broker(client)
    b.MAX_QUERY_PAGES = 3

    with pytest.raises(BrokerageError, match="장을 넘겨도"):
        await b._domestic_balance()

    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_a_single_page_account_still_makes_one_request():
    """회귀 방지: 평범한 계좌가 조회를 더 하면 KIS 호출 한도만 먹습니다."""
    client = FakeClient([(holdings_page("005930", 10), False)])
    b = broker(client)

    await b._domestic_balance()

    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_execution_rows_are_read_to_the_end_too():
    def page(odno, nk=""):
        return {"rt_cd": "0", "output1": [{"odno": odno}],
                "ctx_area_fk100": "FK", "ctx_area_nk100": nk}

    client = FakeClient([(page("A", nk="NEXT"), True), (page("B"), False)])
    b = broker(client)

    rows = await b._query_executions(
        "/p", "TR", "output1",
        {"CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})

    assert [row["odno"] for row in rows] == ["A", "B"]


# ── 애매한 주문 실패 ──────────────────────────────────────────────────────

class SubmitClient:
    def __init__(self, exc):
        self.exc = exc
        self.posts = 0

    async def post(self, url, headers=None, json=None):
        self.posts += 1
        raise self.exc

    async def get(self, url, headers=None, params=None):   # pragma: no cover
        raise AssertionError("이 테스트는 조회하지 않습니다")


def order():
    return Order(SYM, OrderSide.BUY, Decimal("10"), OrderType.LIMIT,
                 limit_price=70_000.0)


@pytest.mark.asyncio
async def test_a_transport_failure_closes_the_fill_channel():
    b = broker(SubmitClient(httpx.ConnectTimeout("timeout")))
    assert b.fill_channel_ok is True

    with pytest.raises(BrokerageError, match="응답을 받지 못했습니다"):
        await b._venue_submit(order())

    assert b.fill_channel_ok is False, "접수 여부를 모르는 채로 다음 주문을 허용했다"


@pytest.mark.asyncio
async def test_a_server_error_closes_the_fill_channel():
    exc = httpx.HTTPStatusError(
        "500", request=httpx.Request("POST", "http://x"),
        response=httpx.Response(503))
    b = broker(SubmitClient(exc))

    with pytest.raises(BrokerageError):
        await b._venue_submit(order())

    assert b.fill_channel_ok is False


@pytest.mark.asyncio
async def test_a_clean_rejection_does_not_close_the_channel():
    """4xx 는 서버가 읽고 거절한 것 — 주문은 나가지 않았습니다."""
    exc = httpx.HTTPStatusError(
        "400", request=httpx.Request("POST", "http://x"),
        response=httpx.Response(400))
    b = broker(SubmitClient(exc))

    with pytest.raises(httpx.HTTPStatusError):
        await b._venue_submit(order())

    assert b.fill_channel_ok is True, "확실한 거절에 눈을 감을 이유가 없다"


@pytest.mark.asyncio
async def test_the_closed_channel_blocks_the_next_order():
    """잠금이 실제로 다음 주문을 막는지 — 그것이 이 수정의 목적입니다.

    `live=True` 여야 합니다. 모의 실행은 주문을 보내지 않으므로 이 관문 자체가
    꺼져 있습니다(`sends_orders`).
    """
    b = broker(SubmitClient(httpx.ConnectTimeout("timeout")), live=True)
    b.portfolio.mark(SYM, 70_000.0)
    with pytest.raises(BrokerageError):
        await b._venue_submit(order())

    with pytest.raises(BrokerageError, match="체결 조회 채널"):
        b._guard(order())
