"""투자자 수급 (investor flow) — the data type, the summary, and the alpha."""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.alpha.flow import InvestorFlowAlpha, RetailContrarianAlpha
from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, Bar, Direction, Symbol
from quant.data.flow import FlowFeed, InvestorFlow, NullFlowProvider, summarize
from quant.data.providers.synthetic_flow import SyntheticFlowProvider

SYM = Symbol("005930", venue="kis", quote_currency="KRW", tick_size=Decimal("100"))
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def flow(day, foreign, institution, retail, close=50_000.0, volume=1_000_000.0, program=0.0):
    return InvestorFlow(
        symbol=SYM, ts=T0 + timedelta(days=day),
        foreign_qty=foreign, institution_qty=institution, retail_qty=retail,
        foreign_value=foreign * close, institution_value=institution * close,
        retail_value=retail * close, program_qty=program,
        close=close, volume=volume,
    )


# ── the data type ────────────────────────────────────────────────────────
def test_accumulation_and_distribution_are_mutually_exclusive():
    acc = flow(0, 10_000, 5_000, -15_000)
    dist = flow(1, -10_000, -5_000, 15_000)
    assert acc.is_accumulation and not acc.is_distribution
    assert dist.is_distribution and not dist.is_accumulation


def test_participation_normalises_by_volume():
    """The whole point: a share count means nothing without the tape it traded on."""
    big = flow(0, 10_000, 0, -10_000, volume=1_000_000)
    small = flow(1, 10_000, 0, -10_000, volume=100_000)
    assert big.participation == pytest.approx(0.01)
    assert small.participation == pytest.approx(0.10)


def test_participation_is_zero_when_volume_is_missing():
    assert flow(0, 10_000, 0, -10_000, volume=0).participation == 0.0


# ── the summary ──────────────────────────────────────────────────────────
def test_streak_counts_consecutive_same_sign_sessions():
    flows = [flow(i, 1_000, 500, -1_500) for i in range(5)]
    flows.append(flow(5, -800, 200, 600))          # foreign flips, institution does not
    s = summarize(SYM, flows, window=20)
    assert s.foreign_streak == -1
    assert s.institution_streak == 6


def test_streak_is_signed_for_selling():
    flows = [flow(i, -1_000, -500, 1_500) for i in range(4)]
    s = summarize(SYM, flows, window=20)
    assert s.foreign_streak == -4
    assert s.institution_streak == -4


def test_bullish_divergence_is_buying_into_weakness():
    flows = [flow(i, 2_000, 1_000, -3_000, close=50_000 - i * 400) for i in range(10)]
    assert summarize(SYM, flows, window=10).divergence == "bullish_divergence"


def test_bearish_divergence_is_selling_into_strength():
    flows = [flow(i, -2_000, -1_000, 3_000, close=50_000 + i * 400) for i in range(10)]
    assert summarize(SYM, flows, window=10).divergence == "bearish_divergence"


def test_summary_needs_at_least_two_sessions():
    assert summarize(SYM, [], window=20) is None
    assert summarize(SYM, [flow(0, 1, 1, -2)], window=20) is None


# ── the feed ─────────────────────────────────────────────────────────────
def test_feed_hides_sessions_that_have_not_happened_yet():
    """The same no-look-ahead rule bars obey."""
    feed = FlowFeed(NullFlowProvider())
    feed.seed(SYM, [flow(i, 1_000, 500, -1_500) for i in range(10)])
    assert len(feed.get(SYM, now=T0 + timedelta(days=4, hours=1))) == 5
    assert len(feed.get(SYM, now=T0 + timedelta(days=100))) == 10


def test_refresh_is_a_noop_outside_a_live_session():
    """A backtest is served from backfill; refreshing would ask the provider for
    'the latest' relative to wall clock, which is both slow and a look-ahead."""
    calls = []

    class Counting(NullFlowProvider):
        async def flows(self, symbol, start, end):
            calls.append(1)
            return []

    feed = FlowFeed(Counting(), live=False)
    assert asyncio.run(feed.refresh([SYM], force=True)) == 0
    assert not calls


def test_synthetic_provider_is_deterministic_and_window_stable():
    p = SyntheticFlowProvider(seed=3)
    a = asyncio.run(p.flows(SYM, T0, T0 + timedelta(days=60)))
    b = asyncio.run(SyntheticFlowProvider(seed=3).flows(
        SYM, T0 - timedelta(days=200), T0 + timedelta(days=60)))
    assert a
    tail = [f for f in b if f.ts >= T0]
    assert [f.foreign_qty for f in a] == [f.foreign_qty for f in tail]


# ── the alpha ────────────────────────────────────────────────────────────
def make_ctx(bars=40, price=50_000.0):
    pf = Portfolio(10_000_000.0, "KRW")
    ctx = Context(SimClock(T0 + timedelta(days=bars)), pf, EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    for i in range(bars):
        ts = T0 + timedelta(days=i)
        ctx.push_bar(Bar(SYM, ts, price, price * 1.01, price * 0.99, price, 1e6, "1d"))
    return ctx


def test_flow_alpha_goes_long_on_sustained_accumulation():
    feed = FlowFeed(NullFlowProvider())
    feed.seed(SYM, [flow(i, 20_000, 10_000, -30_000, close=50_000 - i * 200)
                    for i in range(30)])
    alpha = InvestorFlowAlpha(feed, min_streak=3, min_participation=0.005)
    ctx = make_ctx()
    bar = ctx.history(SYM, 1)[0]
    out = asyncio.run(alpha.update(ctx, {SYM.key: bar}))
    assert out and out[0].direction is Direction.UP
    assert out[0].confidence > 0.6          # streak + divergence should stack


def test_flow_alpha_emits_a_flat_veto_on_distribution_when_short_is_off():
    feed = FlowFeed(NullFlowProvider())
    feed.seed(SYM, [flow(i, -20_000, -10_000, 30_000) for i in range(30)])
    alpha = InvestorFlowAlpha(feed, min_streak=3, min_participation=0.005,
                              allow_short=False)
    ctx = make_ctx()
    out = asyncio.run(alpha.update(ctx, {SYM.key: ctx.history(SYM, 1)[0]}))
    assert out and out[0].direction is Direction.FLAT


def qty_only(day, foreign, institution, retail, close=50_000.0, volume=1_000_000.0):
    """금액 축이 없는 소스가 주는 모양 — 수량만 있고 `*_value` 는 전부 0.

    토스의 투자자별 매매동향이 이렇습니다(주 수만 제공). 종가를 곱해 금액을
    채우는 것은 추정치를 사실로 만드는 일이라 프로바이더가 하지 않습니다.
    """
    return InvestorFlow(
        symbol=SYM, ts=T0 + timedelta(days=day),
        foreign_qty=foreign, institution_qty=institution, retail_qty=retail,
        close=close, volume=volume,
    )


def test_flow_alpha_fires_when_the_venue_reports_no_won_value():
    """금액을 안 주는 소스에서도 수급 신호는 나와야 합니다.

    방향 판정이 순매수 **금액** 을 직접 보면, 그 축이 없는 소스에서는 값이
    언제나 0 이라 20일 연속 매집도 "매수 아님" 이 됩니다. 알파는 아무 신호도
    내지 않고, 화면에는 오류도 경고도 뜨지 않습니다 — 그냥 조용합니다.
    """
    feed = FlowFeed(NullFlowProvider())
    feed.seed(SYM, [qty_only(i, 20_000, 10_000, -30_000, close=50_000 - i * 200)
                    for i in range(30)])
    alpha = InvestorFlowAlpha(feed, min_streak=3, min_participation=0.005)
    ctx = make_ctx()
    out = asyncio.run(alpha.update(ctx, {SYM.key: ctx.history(SYM, 1)[0]}))
    assert out and out[0].direction is Direction.UP


def test_divergence_is_read_from_quantity_when_value_is_missing():
    """금액이 없다고 다이버전스가 없는 것은 아닙니다 — 수량의 부호로 봅니다."""
    flows = [qty_only(i, 2_000, 1_000, -3_000, close=50_000 - i * 400)
             for i in range(10)]
    assert summarize(SYM, flows, window=10).divergence == "bullish_divergence"


def test_a_missing_value_axis_is_reported_as_unknown_not_as_zero():
    """"순매수 0원" 은 아무도 측정하지 않은 숫자입니다.

    이 딕셔너리는 화면과 AI 데스크 프롬프트로 그대로 나갑니다. 0 을 흘리면
    수급 좌석은 "외국인이 금액으로는 안 샀다" 로 읽습니다.
    """
    row = qty_only(0, 20_000, 10_000, -30_000)
    assert not row.has_value_axis
    assert row.to_dict()["foreign_value"] is None
    assert row.to_dict()["foreign_qty"] == 20_000        # 아는 것은 그대로 뜹니다

    s = summarize(SYM, [qty_only(i, 20_000, 10_000, -30_000) for i in range(5)])
    assert s.to_dict()["smart_money_net_value"] is None
    assert s.to_dict()["foreign_net_qty"] == 100_000

    # 금액을 주는 소스에서는 아무것도 달라지지 않아야 합니다.
    priced = flow(0, 20_000, 10_000, -30_000)
    assert priced.has_value_axis
    assert priced.to_dict()["foreign_value"] == 20_000 * 50_000


def test_flow_alpha_stays_silent_below_the_participation_floor():
    feed = FlowFeed(NullFlowProvider())
    feed.seed(SYM, [flow(i, 100, 50, -150, volume=10_000_000) for i in range(30)])
    alpha = InvestorFlowAlpha(feed, min_streak=3, min_participation=0.01)
    ctx = make_ctx()
    assert asyncio.run(alpha.update(ctx, {SYM.key: ctx.history(SYM, 1)[0]})) == []


def test_flow_alpha_stays_silent_without_data():
    alpha = InvestorFlowAlpha(FlowFeed(NullFlowProvider()))
    ctx = make_ctx()
    assert asyncio.run(alpha.update(ctx, {SYM.key: ctx.history(SYM, 1)[0]})) == []


def test_retail_contrarian_fades_a_retail_crowd():
    feed = FlowFeed(NullFlowProvider())
    sessions = [flow(i, 500, 200, -700) for i in range(25)]
    sessions.append(flow(25, -30_000, -10_000, 40_000))     # retail piles in
    feed.seed(SYM, sessions)
    alpha = RetailContrarianAlpha(feed, window=20, min_zscore=1.5, allow_short=False)
    ctx = make_ctx(bars=40)
    out = asyncio.run(alpha.update(ctx, {SYM.key: ctx.history(SYM, 1)[0]}))
    assert out and out[0].direction is Direction.FLAT   # bearish, but shorting is off
