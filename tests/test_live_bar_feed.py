"""라이브 루프가 받은 봉을 잃어버리거나 지어내지 않는가.

세 가지가 조용히 틀려 있었고, 셋 다 화면에는 아무 표시도 나지 않았습니다.

  1. 한 사이클에 새 확정봉이 여러 개 밀려 있으면 **가장 최근 것 하나만**
     엔진에 갔습니다. 나머지는 "봤다" 로 표시되고 사라져서, 지표는 건너뛴
     봉들을 연속봉으로 계산했습니다.
  2. 조회 창(3봉)보다 큰 구멍은 요청조차 하지 않았습니다.
  3. 아직 만들어지는 중인 봉이 그 시각의 확정봉 자리를 차지했습니다 — 나중에
     온 진짜 확정본은 "이미 본 시각" 이라 버려집니다.

검사하는 것은 구현식이 아니라 **성질**입니다: 생긴 확정봉은 전부 시계열에
남는가, 확정값이 진행 중 값을 이기는가, 못 본 구간은 조용히 지나가지 않는가.

네트워크는 어디서도 건드리지 않습니다 — `latest_bars` / `history` 를 갈아
끼웁니다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from quant.config.schema import (
    BrokerConfig,
    CostConfig,
    DataConfig,
    ExecutionConfig,
    ModelSpec,
    PortfolioConfig,
    RiskConfig,
    StrategyConfig,
    SymbolSpec,
    UniverseConfig,
)
from quant.core.types import UTC, AssetClass, Bar, RunMode, Symbol
from quant.data.feed import LiveBarFeed
from quant.live.trader import LiveTrader

STEP = timedelta(days=1)


def live_config() -> StrategyConfig:
    """한 종목·짧은 워밍업. 이 파일이 보는 것은 시세 배관뿐입니다."""
    return StrategyConfig(
        name="feed-test",
        mode=RunMode.DRY_RUN,
        data=DataConfig(provider="synthetic", params={"seed": 3}, timeframe="1d",
                        warmup_bars=40, cache=False),
        universe=UniverseConfig(symbols=[SymbolSpec(ticker="AAA", venue="SIM")]),
        alpha=[ModelSpec(type="ema_cross")],
        portfolio=PortfolioConfig(starting_cash=100_000, max_gross_leverage=1.0,
                                  cash_reserve_pct=0.0, min_trade_weight=0.0),
        risk=RiskConfig(),
        execution=ExecutionConfig(min_order_notional=1.0),
        costs=CostConfig(preset="zero_cost"),
        broker=BrokerConfig(type="paper"),
    )


@pytest.fixture
def trader(tmp_path):
    tr = LiveTrader(live_config(), state_path=str(tmp_path / "feed.db"))
    asyncio.run(tr.warmup())
    return tr


def only(trader) -> Symbol:
    return list(trader.engine.ctx.universe)[0]


def stored(trader, symbol) -> list[Bar]:
    """엔진이 실제로 들고 있는 시계열.

    `ctx.history()` 는 벽시계 기준으로 미래 봉을 감추므로 저장된 것을 그대로
    봅니다 — 여기서 알고 싶은 것은 "무엇이 들어갔는가" 입니다.
    """
    return list(trader.engine.ctx._bars[symbol.key])


def rewind(trader, bars_back: int) -> datetime:
    """워밍업 기준선을 N봉 과거로 옮기고, 그 시각을 돌려줍니다.

    "방금 닫힌 봉" 은 정의상 아직 닫히지 않았습니다. 새 **확정봉**을 주입해
    보려면 기준선이 과거여야 하고, 그렇지 않으면 주입한 봉이 전부 "아직
    만들어지는 중" 으로 걸립니다(그건 그것대로 맞는 동작입니다).
    """
    sym = only(trader)
    keep = stored(trader, sym)[:-bars_back]
    trader.engine.ctx.seed_history(sym, keep)
    trader._seen = {sym.key: keep[-1].ts}
    return keep[-1].ts


def feed_bars(trader, make) -> None:
    async def latest_bars(symbol, timeframe, count=1):
        return make(symbol, timeframe, count)
    trader.provider.latest_bars = latest_bars


# ── (1) 한꺼번에 밀린 봉 ──────────────────────────────────────────────────
def test_no_closed_bar_is_dropped_when_several_arrive_in_one_cycle(trader):
    """3봉이 한 번에 와도 3봉 다 시계열에 남아야 합니다.

    고치기 전: 3개 중 1개만 남고 `_seen` 은 가장 최근 봉을 가리켰습니다.
    건너뛴 두 봉은 다시 요청되지도 않습니다 — 영구 결손이고, 그 위의 20봉
    이동평균은 실제로 23봉을 덮습니다.
    """
    sym = only(trader)
    base = rewind(trader, 8)
    before = len(stored(trader, sym))
    feed_bars(trader, lambda s, tf, n: [
        Bar(s, base + STEP * i, 100.0, 101.0, 99.0, 100.0 + i, 10.0, tf)
        for i in (1, 2, 3)])

    asyncio.run(trader._tick())

    got = stored(trader, sym)
    assert len(got) - before == 3, "확정봉이 조용히 사라졌습니다"
    tail = [b.ts for b in got[-3:]]
    assert tail == [base + STEP, base + STEP * 2, base + STEP * 3]
    assert all(b - a == STEP for a, b in zip(tail, tail[1:])), "이어 붙인 흔적"


def test_a_pile_up_still_decides_only_once(trader):
    """밀린 봉을 하나씩 재생하지는 않습니다.

    지나간 봉에서 나온 주문은 지나간 가격에 체결되지 않고, 데스크가 붙어
    있으면 LLM 호출도 봉 수만큼 늘어납니다. 지표는 전부 보되 판단은 한 번.
    """
    base = rewind(trader, 8)
    feed_bars(trader, lambda s, tf, n: [
        Bar(s, base + STEP * i, 100.0, 101.0, 99.0, 100.0 + i, 10.0, tf)
        for i in (1, 2, 3)])

    calls: list[list[datetime]] = []
    inner = trader.engine.on_bars

    async def spy(bars, ts=None):
        calls.append([b.ts for b in bars.values()])
        return await inner(bars, ts)

    trader.engine.on_bars = spy
    asyncio.run(trader._tick())

    assert len(calls) == 1, f"봉마다 판단이 돌았습니다: {calls}"
    assert calls[0] == [base + STEP * 3], "판단은 가장 최근 확정봉으로"


# ── (2) 조회 창보다 큰 구멍 ──────────────────────────────────────────────
def test_a_hole_bigger_than_the_poll_window_is_refetched(trader):
    """10봉 끊겼는데 폴링이 3봉만 준다면, 나머지 7봉을 되물어야 합니다.

    고치기 전: `history()` 는 한 번도 불리지 않았고, 시계열은 그 7봉을
    건너뛴 채 이어졌습니다.
    """
    sym = only(trader)
    base = rewind(trader, 14)
    feed_bars(trader, lambda s, tf, n: [
        Bar(s, base + STEP * (10 - (n - 1 - i)), 100.0, 101.0, 99.0, 100.0, 10.0, tf)
        for i in range(n)])

    asked: list[tuple] = []

    async def history(symbol, timeframe, start, end):
        asked.append((start, end))
        out, ts = [], start
        while ts < end:
            out.append(Bar(symbol, ts, 100.0, 101.0, 99.0, 100.0, 10.0, timeframe))
            ts += STEP
        return out

    trader.provider.history = history
    asyncio.run(trader._tick())

    assert asked, "못 본 구간을 되묻지 않았습니다"
    fresh = [b.ts for b in stored(trader, sym) if b.ts > base]
    assert fresh == [base + STEP * i for i in range(1, 11)], f"구멍이 남았습니다: {fresh}"


def test_a_window_we_could_not_look_at_is_reported_not_hidden(trader):
    """되묻다 실패하면 "못 봤다" 가 위로 올라와야 합니다.

    조용히 이어 붙이면 없는 봉이 생기고, 그 위에서 나온 판단을 사람은 그대로
    믿습니다.
    """
    sym = only(trader)
    base = rewind(trader, 14)
    feed_bars(trader, lambda s, tf, n: [
        Bar(s, base + STEP * 10, 100.0, 101.0, 99.0, 100.0, 10.0, tf)])

    async def broken(symbol, timeframe, start, end):
        raise RuntimeError("시세 서버가 응답하지 않습니다")

    trader.provider.history = broken
    asyncio.run(trader._tick())

    feed = trader.status()["feed"]
    assert feed["unseen_windows"], "못 본 구간이 조용히 사라졌습니다"
    hole = feed["unseen_windows"][0]
    assert hole["ticker"] == sym.ticker
    assert hole["bars"] == 9
    assert feed["degraded"] is True


def test_a_holiday_is_not_reported_as_a_hole(trader):
    """되물었더니 아무것도 없더라 — 그건 휴장이지 결손이 아닙니다.

    간격만 보고 구멍이라고 우기면 주말마다 경고가 나고, 사람은 곧 그 경고를
    안 읽게 됩니다. 그러면 진짜 결손도 같이 안 읽힙니다.
    """
    base = rewind(trader, 8)
    feed_bars(trader, lambda s, tf, n: [
        Bar(s, base + STEP * 3, 100.0, 101.0, 99.0, 100.0, 10.0, tf)])

    async def empty(symbol, timeframe, start, end):
        return []

    trader.provider.history = empty
    asyncio.run(trader._tick())

    assert trader.status()["feed"]["unseen_windows"] == []


# ── (3) 미완성 봉 ────────────────────────────────────────────────────────
def test_a_bar_still_forming_never_reaches_the_engine(trader):
    """진행 중인 봉은 엔진에 가지 않고, "봤다" 로 표시되지도 않습니다.

    고치기 전: 진행 중 봉이 그대로 들어가고 `_seen` 이 그 시각으로 올라가서,
    나중에 온 확정본은 "이미 본 시각" 이라 버려졌습니다. 진행 중 봉의
    고가·저가·거래량은 확정값보다 언제나 좁습니다.
    """
    sym = only(trader)
    was = trader._seen[sym.key]
    forming_ts = datetime.now(UTC)          # end_ts 가 미래 = 아직 만들어지는 중
    feed_bars(trader, lambda s, tf, n: [
        Bar(s, forming_ts, 100.0, 100.0, 100.0, 100.0, 1.0, tf)])

    asyncio.run(trader._tick())

    assert [b for b in stored(trader, sym) if b.ts == forming_ts] == [], \
        "만들어지는 중인 봉이 확정봉으로 나갔습니다"
    assert trader._seen[sym.key] == was, "넘긴 봉을 봤다고 표시하면 확정본이 막힙니다"
    assert trader.status()["feed"]["held_partial_bars"] >= 1


def test_the_confirmed_bar_wins_over_the_one_that_was_still_forming():
    """같은 시각의 확정본이 진행 중 값을 이겨야 합니다.

    푸시 피드에서는 이게 **기본 동작**입니다 — 웹소켓은 진행 중인 봉을 계속
    갱신해서 보냅니다. 시계는 `admit()` 에 넘겨 고정합니다.
    """
    sym = Symbol("AAA", venue="SIM", asset_class=AssetClass.EQUITY)
    ts = datetime(2026, 1, 5, tzinfo=UTC)
    partial = Bar(sym, ts, 100.0, 100.0, 100.0, 100.0, 1.0, "1d")
    final = Bar(sym, ts, 100.0, 130.0, 90.0, 128.0, 999.0, "1d")

    class Nothing:
        name = "nothing"
        supports_streaming = False

    feed = LiveBarFeed(Nothing(), "1d")
    during = asyncio.run(feed.admit(sym, [partial], now=ts + timedelta(hours=6)))
    after = asyncio.run(feed.admit(sym, [final], now=ts + timedelta(days=1, seconds=1)))

    assert during == [], "창이 안 지난 봉이 확정봉으로 나갔습니다"
    assert [b.close for b in after] == [128.0]
    assert [b.high for b in after] == [130.0]


# ── 화면이 읽는 시세 상태 ────────────────────────────────────────────────
def test_the_feed_does_not_claim_to_be_realtime_while_it_is_polling(trader):
    """"실시간" 배지는 거래소가 봉을 밀어 줄 때만 참입니다."""
    feed = trader.status()["feed"]
    assert feed["mode"] == "polled"
    assert "폴링" in feed["mode_ko"]
    assert feed["provider"]


def test_the_badge_follows_the_provider_not_a_hard_coded_constant():
    """푸시가 붙는 날에는 저절로 실시간이 되고, 폴백 날에는 저절로 내려가야."""
    class Pushy:
        name = "pushy"
        supports_streaming = True

    class Polly:
        name = "polly"
        supports_streaming = False

    assert LiveBarFeed(Pushy(), "1m").mode == "realtime"
    assert LiveBarFeed(Polly(), "1m").mode == "polled"


# ── 한 종목이 죽어도 나머지는 산다 ───────────────────────────────────────
def test_one_dead_symbol_does_not_take_the_others_down():
    """들고 있는 종목의 손절이 남의 사정으로 평가되지 않으면 그게 더 비쌉니다."""
    good = Symbol("AAA", venue="SIM", asset_class=AssetClass.EQUITY)
    bad = Symbol("BBB", venue="SIM", asset_class=AssetClass.EQUITY)
    ts = datetime.now(UTC) - timedelta(days=2)

    class Flaky:
        name = "flaky"
        supports_streaming = False

        async def latest_bars(self, symbol, timeframe, count=1):
            if symbol.key == bad.key:
                raise RuntimeError("no data")
            return [Bar(symbol, ts, 1.0, 2.0, 0.5, 1.5, 10.0, timeframe)]

    feed = LiveBarFeed(Flaky(), "1d")
    got = asyncio.run(feed.pending([good, bad]))

    assert [b.symbol.key for b in got] == [good.key]
    assert feed.health()["fetch_failures"] == [bad.ticker]


# ── 토스 웹소켓: 스펙이 없으면 지어내지 않는다 ───────────────────────────
def test_the_toss_websocket_protocol_is_not_invented():
    """실시간 소켓 프로토콜은 우리가 가진 스펙 파일에 없습니다.

    REST 스펙(`toss_openapi.json`)에는 소켓 주소와 "AsyncAPI 문서를 보라" 는
    안내만 있고, 구독 메시지·인증·메시지 종류·keepalive·구독 한도는 그 별도
    문서에 있습니다. 추측으로 채우면 "연결은 되는데 아무것도 안 오는" 모양이
    되고, 그건 호가창이 멈춘 것과 구분되지 않습니다.

    그래서 지금은 폴링이고, 배지도 그렇게 말해야 합니다.
    """
    from quant.brokerage import toss_broker as T

    assert T.TossProvider.supports_streaming is False
    assert "stream" not in vars(T.TossProvider), "구독 경로를 추측으로 넣었습니다"
    invented = [name for name, value in vars(T).items()
                if isinstance(value, str) and value.startswith(("ws://", "wss://"))]
    assert not invented, f"확인하지 않은 소켓 주소가 상수로 박혔습니다: {invented}"
