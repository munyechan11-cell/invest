"""토스 수급 — 투자자별 매매동향을 제대로 읽어 오는가.

저장소는 오랫동안 "수급은 KIS 만 준다 (토스는 미제공)" 이라고 적어 두었고,
그건 틀렸습니다. 토스 스펙에 종목별 투자자별 매매동향이 있습니다. 다만 KIS 와
모양이 달라서, 그냥 옮겨 붙이면 조용히 틀리는 자리가 넷 있습니다.

1. **페이징** — 기간이 아니라 커서(`until`/`nextUntil`)로 셉니다. 첫 페이지만
   읽고 멈추면 100 세션 넘는 창이 소리 없이 잘리고, 그 위에서 20일 평균이
   실제로는 며칠을 덮습니다.
2. **당일 잠정치** — 확정 전 행에는 개인(`individual`)이 없습니다. 그걸 0 으로
   채워 넣으면 "개인이 안 샀다" 가 되어 매집/분산 판정이 뒤집힙니다.
3. **부호** — `netBuyVolume` 은 음수면 순매도입니다. 절댓값을 쓰면 파는 것을
   사는 것으로 읽습니다.
4. **금액** — 토스는 거래량만 줍니다. 종가를 곱해 금액을 만들면 화면의 "수급
   강도" 가 추정치 위에서 계산되는데, 그건 아무도 측정하지 않은 숫자입니다.

네트워크는 `httpx.MockTransport` 로 대신합니다 — 실제 토스 API 는 부르지
않습니다. 토큰만 캐시에 미리 넣어 두는데, 그것 자체가 이 프로바이더가 브로커의
OAuth 경로를 **재사용** 한다는 증거이기도 합니다(자기 토큰 코드를 따로 들고
있었다면 이 캐시가 안 먹힙니다).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from quant.brokerage import toss_broker as T
from quant.core.types import UTC, Symbol
from quant.data.flow import create_flow_provider
from quant.data.providers.toss_flow import TossFlowProvider

SYM = Symbol("005930", venue="toss", quote_currency="KRW", tick_size=Decimal("100"))
CLIENT_ID = "test-client-id"
START = datetime(2026, 7, 1, tzinfo=UTC)
END = datetime(2026, 7, 18, tzinfo=UTC)


# ── 응답 조각 ────────────────────────────────────────────────────────────
def vol(net: int, buy: int = 0, sell: int = 0) -> dict:
    """`InvestorTradingVolume` — 모든 수량이 문자열입니다."""
    return {"buyVolume": str(buy), "sellVolume": str(sell), "netBuyVolume": str(net)}


def final(day: str, foreigner: int, institution: int, individual: int,
          breakdown: dict | None = None) -> dict:
    """확정 기록 — 개인까지 채워진 그날 저녁 이후의 모습."""
    return {
        "date": day, "updatedAt": f"{day}T18:10:00+09:00",
        "individual": vol(individual),
        "foreigner": vol(foreigner),
        "institution": {**vol(institution), "breakdown": breakdown},
        "otherCorporation": vol(0),
        "foreignerHolding": None, "cfd": None,
    }


def provisional(day: str, foreigner: int, institution: int) -> dict:
    """당일 장중 잠정 기록 — 스펙대로 개인·기타법인·기관세부가 null 입니다."""
    return {
        "date": day, "updatedAt": f"{day}T14:35:08+09:00",
        "individual": None,
        "foreigner": vol(foreigner),
        "institution": {**vol(institution), "breakdown": None},
        "otherCorporation": None,
        "foreignerHolding": None, "cfd": None,
    }


def candle(day: str, volume: float, close: float) -> dict:
    return {"timestamp": f"{day}T09:00:00+09:00", "openPrice": str(close),
            "highPrice": str(close), "lowPrice": str(close),
            "closePrice": str(close), "volume": str(volume), "currency": "KRW"}


class FakeToss:
    """토스 HTTP 표면. 요청을 기록하고 미리 짜 둔 페이지를 돌려줍니다."""

    def __init__(self, pages: list[dict], candles: list[dict] | None = None):
        self.pages = pages
        self.candles = candles if candles is not None else []
        self.investor_calls: list[dict] = []
        self.candle_calls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        assert request.headers["Authorization"] == "Bearer test-token"
        if "investor-trading" in request.url.path:
            assert request.url.path.endswith(f"/{SYM.ticker}/investor-trading")
            self.investor_calls.append(params)
            page = self.pages[min(len(self.investor_calls) - 1, len(self.pages) - 1)]
            return httpx.Response(200, json={"result": page})
        if request.url.path.endswith("/candles"):
            self.candle_calls.append(params)
            return httpx.Response(200, json={
                "result": {"candles": self.candles, "nextBefore": None}})
        raise AssertionError(f"예상하지 못한 호출: {request.url}")


def provider(fake: FakeToss) -> TossFlowProvider:
    # 토큰 캐시를 채워 두면 발급 왕복이 사라집니다. 이 프로바이더가 브로커의
    # `toss_token` 을 그대로 쓰기 때문에 가능한 일입니다.
    T._TOKENS[CLIENT_ID[:10]] = ("test-token", time.time() + 3600)
    p = TossFlowProvider(client_id=CLIENT_ID, client_secret="test-secret")
    p._client._http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return p


async def run(fake: FakeToss, start: datetime = START, end: datetime = END):
    p = provider(fake)
    try:
        return await p.flows(SYM, start, end)
    finally:
        await p.close()


# ── 등록 ─────────────────────────────────────────────────────────────────
def test_the_provider_is_registered_under_toss():
    """설정이 `flow.provider: toss` 라고 적을 수 있어야 의미가 있습니다."""
    T._TOKENS[CLIENT_ID[:10]] = ("test-token", time.time() + 3600)
    made = create_flow_provider("toss", client_id=CLIENT_ID, client_secret="s")
    assert isinstance(made, TossFlowProvider)


# ── 페이징 ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_paging_follows_next_until_to_the_older_page():
    """첫 페이지에서 멈추면 창이 조용히 잘립니다."""
    fake = FakeToss(
        pages=[
            {"nextUntil": "2026-07-14",
             "records": [final("2026-07-16", 100, 50, -150),
                         final("2026-07-15", 200, 60, -260)]},
            {"nextUntil": None,
             "records": [final("2026-07-14", 300, 70, -370)]},
        ],
        candles=[candle(d, 1_000_000, 70_000)
                 for d in ("2026-07-16", "2026-07-15", "2026-07-14")],
    )
    flows = await run(fake)

    assert [f.ts.date().isoformat() for f in flows] == \
        ["2026-07-14", "2026-07-15", "2026-07-16"], "시간순으로 나와야 합니다"
    assert len(fake.investor_calls) == 2, "두 번째 페이지를 부르지 않았습니다"
    # 커서는 응답이 준 값을 그대로 되돌려 보냅니다.
    assert fake.investor_calls[1]["until"] == "2026-07-14"
    assert fake.investor_calls[0]["until"] == END.date().isoformat()
    assert int(fake.investor_calls[0]["count"]) <= 100, "스펙 상한은 100 입니다"


@pytest.mark.asyncio
async def test_paging_stops_once_the_window_is_covered():
    """창을 다 덮었는데도 커서를 계속 따라가면 몇 년치를 끌어옵니다."""
    fake = FakeToss(
        pages=[{"nextUntil": "2026-06-01",
                "records": [final("2026-07-02", 100, 50, -150),
                            final("2026-06-30", 100, 50, -150)]}],
        candles=[candle("2026-07-02", 1_000_000, 70_000)],
    )
    await run(fake)
    assert len(fake.investor_calls) == 1, "창 밖까지 페이지를 더 받았습니다"


@pytest.mark.asyncio
async def test_a_cursor_that_does_not_advance_does_not_loop_forever():
    """같은 `until` 을 돌려주는 서버에 걸리면 영원히 같은 페이지를 받습니다."""
    fake = FakeToss(
        pages=[{"nextUntil": END.date().isoformat(),      # 진행하지 않는 커서
                "records": [final("2026-07-16", 100, 50, -150)]}],
        candles=[candle("2026-07-16", 1_000_000, 70_000)],
    )
    flows = await run(fake)
    assert len(fake.investor_calls) == 1
    assert len(flows) == 1


# ── 당일 잠정치 ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_provisional_session_is_not_reported_as_final():
    """확정 전 행을 내보내면 개인 순매수가 0 으로 읽힙니다."""
    fake = FakeToss(
        pages=[{"nextUntil": None,
                "records": [provisional("2026-07-17", 119_900, -113_200),
                            final("2026-07-16", -319_700, 37_900, 291_850)]}],
        candles=[candle("2026-07-17", 2_000_000, 71_000),
                 candle("2026-07-16", 1_500_000, 70_000)],
    )
    flows = await run(fake)

    assert [f.ts.date().isoformat() for f in flows] == ["2026-07-16"], \
        "잠정 기록이 확정치 행으로 섞여 나왔습니다"
    # 그리고 잠정치의 숫자가 확정 행에 새어 들어가지도 않아야 합니다.
    assert flows[0].foreign_qty == -319_700


@pytest.mark.asyncio
async def test_a_provisional_only_response_yields_nothing_rather_than_zeros():
    """장중에는 확정 행이 하나도 없을 수 있습니다 — 그때는 빈손이 정답입니다."""
    fake = FakeToss(
        pages=[{"nextUntil": None,
                "records": [provisional("2026-07-17", 119_900, -113_200)]}],
        candles=[candle("2026-07-17", 2_000_000, 71_000)],
    )
    assert await run(fake) == []


# ── 부호 ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_net_sell_stays_negative():
    """`netBuyVolume` 이 음수면 순매도입니다. 절댓값을 쓰면 방향이 뒤집힙니다."""
    fake = FakeToss(
        pages=[{"nextUntil": None,
                "records": [final("2026-07-16", -319_700, -37_900, 357_600)]}],
        candles=[candle("2026-07-16", 1_500_000, 70_000)],
    )
    f = (await run(fake))[0]
    assert f.foreign_qty == -319_700
    assert f.institution_qty == -37_900
    assert f.retail_qty == 357_600
    assert f.smart_money_qty < 0
    assert f.is_distribution and not f.is_accumulation


# ── 빈 응답 ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_an_empty_response_is_not_a_crash():
    """조회 범위에 자료가 없으면 `records` 는 빈 배열입니다 (스펙의 noData 예시)."""
    fake = FakeToss(pages=[{"nextUntil": None, "records": []}])
    assert await run(fake) == []
    assert fake.candle_calls == [], "받을 수급이 없는데 일봉까지 불렀습니다"


@pytest.mark.asyncio
async def test_records_outside_the_window_are_dropped():
    """`until` 은 inclusive 라 창 밖 세션이 딸려 옵니다."""
    fake = FakeToss(
        pages=[{"nextUntil": None,
                "records": [final("2026-07-18", 100, 50, -150),   # end 는 배타적
                            final("2026-07-16", 200, 60, -260)]}],
        candles=[candle("2026-07-18", 1_000_000, 70_000),
                 candle("2026-07-16", 1_000_000, 70_000)],
    )
    flows = await run(fake)
    assert [f.ts.date().isoformat() for f in flows] == ["2026-07-16"]


# ── 금액을 지어내지 않는다 ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_won_value_axis_is_left_empty_not_invented():
    """종가 × 수량은 체결 단가가 아닙니다. 없는 것은 없는 채로 둡니다."""
    fake = FakeToss(
        pages=[{"nextUntil": None,
                "records": [final("2026-07-16", 100_000, 50_000, -150_000)]}],
        candles=[candle("2026-07-16", 3_000_000, 70_000)],
    )
    f = (await run(fake))[0]
    assert f.foreign_value == 0.0 and f.institution_value == 0.0
    assert not f.has_value_axis
    # 화면·프롬프트로 나갈 때도 0 이 아니라 빈칸이어야 합니다.
    assert f.to_dict()["foreign_value"] is None
    # 그런데 수량 축은 살아 있고, 거래량으로 정규화도 됩니다.
    assert f.participation == pytest.approx(150_000 / 3_000_000)
    assert f.close == 70_000.0 and f.volume == 3_000_000.0


@pytest.mark.asyncio
async def test_the_institution_breakdown_comes_through_when_the_venue_sends_it():
    """기관 7분류는 확정 기록에만 있습니다 — 있을 때는 버리지 않습니다."""
    fake = FakeToss(
        pages=[{"nextUntil": None,
                "records": [final("2026-07-16", 100, -52_700, -150,
                                  breakdown={"pensionFund": vol(-52_700),
                                             "trust": vol(21_800)})]}],
        candles=[candle("2026-07-16", 1_000_000, 70_000)],
    )
    detail = (await run(fake))[0].institution_detail
    assert detail == {"pensionFund": -52_700.0, "trust": 21_800.0}


# ── 거래량이 아직 없는 세션 ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_session_without_a_closed_candle_is_held_back():
    """수급 확정치는 저녁에, 일봉은 다음 날 아침에 닫힙니다.

    그 사이에 거래량 없이 내보내면 참여율이 0 으로 계산되는데, 그 0 은 "수급이
    없었다" 와 구분되지 않습니다. 한 세션을 미루는 편이 낫습니다.
    """
    fake = FakeToss(
        pages=[{"nextUntil": None,
                "records": [final("2026-07-16", 100, 50, -150),
                            final("2026-07-15", 200, 60, -260)]}],
        candles=[candle("2026-07-15", 1_000_000, 70_000)],   # 16일 봉이 아직 없다
    )
    flows = await run(fake)
    assert [f.ts.date().isoformat() for f in flows] == ["2026-07-15"]


@pytest.mark.asyncio
async def test_a_dead_candle_endpoint_still_yields_direction():
    """반대로 일봉이 통째로 실패하면, 방향만이라도 남기는 편이 낫습니다."""
    class Broken(FakeToss):
        def __call__(self, request):
            if request.url.path.endswith("/candles"):
                return httpx.Response(500, json={"error": {"message": "nope"}})
            return super().__call__(request)

    fake = Broken(pages=[{"nextUntil": None,
                          "records": [final("2026-07-16", 100, 50, -150)]}])
    flows = await run(fake)
    assert len(flows) == 1
    assert flows[0].foreign_qty == 100
    assert flows[0].volume == 0.0        # 모르는 거래량은 0 이고 participation 도 0
    assert flows[0].participation == 0.0


# ── 창의 끝이 미래일 때 ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_future_window_end_sends_no_until_at_all():
    """미래 날짜를 `until` 로 보냈을 때의 동작은 스펙에 없습니다 — 안 보냅니다."""
    fake = FakeToss(pages=[{"nextUntil": None, "records": []}])
    now = datetime.now(UTC)
    await run(fake, start=now - timedelta(days=30), end=now + timedelta(days=1))
    assert "until" not in fake.investor_calls[0]
