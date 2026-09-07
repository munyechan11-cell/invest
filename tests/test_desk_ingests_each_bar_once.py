"""데스크는 같은 봉을 두 번 넣지 않는다.

`TradingDesk.update` 는 `on_bars` 말고도 불립니다 — 봇이 뜰 때 한 번
(`_opening_deliberation`), 그리고 **휴장 중 `closed_cadence_minutes` 마다**
(`_closed_market_deliberation`). 그때 넘어오는 봉은 새 봉이 아니라
`ctx.history()[-1]`, 이미 넣은 바로 그 봉입니다.

확인 없이 다시 넣으면 같은 종가가 지표 창에 쌓입니다. 주말이 지나면 20봉
수익률과 변동성이 0 에 수렴하고, 월요일 브리프가 그 숫자를 "결정론적으로
계산된 사실" 로 좌석들에게 건넵니다. LLM 은 캐시라 한 번도 안 불리므로
비용·로그 어디에도 흔적이 없습니다.

이 파일은 **성질** 을 검사합니다 — "반복 호출해도 지표가 한 봉만큼만
움직인다", "부작용(history·기억·이벤트)이 늘지 않는다". 구현식을 베끼지
않으므로 `_ingested` 를 지우면 실제로 실패합니다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.alpha.desk import TradingDesk
from quant.alpha.llm_client import LLMUsage
from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, Bar, RunMode, Symbol

SYM = Symbol("005930", venue="toss", quote_currency="KRW", tick_size=Decimal("100"))
T0 = datetime(2026, 1, 5, tzinfo=UTC)


def make_ctx(n: int = 260) -> Context:
    """추세와 변동성이 뚜렷한 시계열. 마지막 5봉이 하루 +1%."""
    pf = Portfolio(10_000_000.0, "KRW")
    ctx = Context(SimClock(T0 + timedelta(days=n)), pf, EventBus(),
                  timeframe="1d", run_mode=RunMode.DRY_RUN)
    ctx.universe = [SYM]
    for i in range(n):
        p = 70_000.0 * (1 + 0.0004 * i) * (1.01 ** max(0, i - (n - 6)))
        ctx.push_bar(Bar(SYM, T0 + timedelta(days=i), p * 0.995, p * 1.015,
                         p * 0.985, p, 1e6, "1d"))
    return ctx


class NeverCalledLLM:
    """부르면 실패합니다 — 이 파일은 LLM 앞단(지표 적재)만 검사합니다."""

    def __init__(self):
        self.usage = LLMUsage()

    async def complete(self, system, user, schema=None):   # pragma: no cover
        raise AssertionError("이 테스트에서 LLM 이 불리면 안 됩니다")


def desk_without_llm() -> TradingDesk:
    """심의는 막고 지표 적재만 봅니다 — 이 파일이 검사하는 것이 그것입니다.

    비용 한도로 막습니다. `update` 의 지표 적재는 그 검사보다 앞에 있어서
    이 상태에서도 그대로 실행됩니다 — 즉 결함이 있으면 여전히 드러납니다.
    """
    desk = TradingDesk(NeverCalledLLM(), memory=False, cost_limit_usd=1e-9)
    desk.client.usage.add(1_000_000, 1_000_000)   # 한도를 이미 넘긴 상태로
    assert desk.estimated_cost_usd >= desk.cost_limit_usd
    return desk


def snapshot(desk: TradingDesk) -> dict:
    ind = desk._sets[SYM.key]
    return {
        "ret20": ind.ret20.value,
        "vol": ind.vol.value,
        "rsi": ind.rsi.value,
        "sma20": ind.sma20.value,
    }


@pytest.mark.asyncio
async def test_the_same_bar_twice_moves_the_indicators_exactly_once():
    ctx = make_ctx()
    desk = desk_without_llm()
    last = {SYM.key: ctx.history(SYM)[-1]}

    await desk.update(ctx, last)
    after_first = snapshot(desk)

    for _ in range(48):                      # 주말 하나치 (한 시간에 한 번)
        await desk.update(ctx, last)

    assert snapshot(desk) == after_first, "휴장 중 반복 심의가 지표를 움직였다"


@pytest.mark.asyncio
async def test_a_weekend_of_repeats_does_not_flatten_the_return_and_vol_windows():
    """오염의 실제 모양: 20봉 수익률과 변동성이 0 으로 수렴한다."""
    ctx = make_ctx()
    desk = desk_without_llm()
    last = {SYM.key: ctx.history(SYM)[-1]}
    await desk.update(ctx, last)
    healthy = snapshot(desk)

    for _ in range(48):
        await desk.update(ctx, last)

    assert healthy["ret20"] > 0.02, "픽스처가 추세를 만들지 못했다"
    assert snapshot(desk)["ret20"] == pytest.approx(healthy["ret20"])
    assert snapshot(desk)["vol"] == pytest.approx(healthy["vol"])
    assert snapshot(desk)["vol"] > 0.0


@pytest.mark.asyncio
async def test_a_repeat_returns_nothing_and_advances_no_counter():
    """반복 호출은 아무 일도 하지 않아야 합니다.

    `_bar_count` 가 올라가면 cadence 가 어긋나 **실제 봉** 의 심의가 밀립니다.
    """
    ctx = make_ctx()
    desk = desk_without_llm()
    last = {SYM.key: ctx.history(SYM)[-1]}

    assert await desk.update(ctx, last) == []
    count_after_first = desk._bar_count
    history_after_first = len(desk.history)

    for _ in range(10):
        assert await desk.update(ctx, last) == []

    assert desk._bar_count == count_after_first
    assert len(desk.history) == history_after_first


@pytest.mark.asyncio
async def test_a_genuinely_new_bar_is_still_ingested():
    """회귀 방지: 새 봉은 반드시 들어가야 합니다."""
    ctx = make_ctx()
    desk = desk_without_llm()
    await desk.update(ctx, {SYM.key: ctx.history(SYM)[-1]})
    before = snapshot(desk)

    nxt = ctx.history(SYM)[-1]
    fresh = Bar(SYM, nxt.ts + timedelta(days=1), nxt.close, nxt.close * 1.08,
                nxt.close * 0.99, nxt.close * 1.07, 2e6, "1d")
    ctx.push_bar(fresh)
    await desk.update(ctx, {SYM.key: fresh})

    assert snapshot(desk) != before, "새 봉이 지표에 들어가지 않았다"


@pytest.mark.asyncio
async def test_an_older_bar_arriving_late_is_refused():
    """스트리밍 지표는 되감을 수 없습니다 — 뒤늦게 온 옛 봉은 거릅니다."""
    ctx = make_ctx()
    desk = desk_without_llm()
    history = ctx.history(SYM)
    await desk.update(ctx, {SYM.key: history[-1]})
    before = snapshot(desk)

    await desk.update(ctx, {SYM.key: history[-5]})

    assert snapshot(desk) == before


@pytest.mark.asyncio
async def test_the_first_call_still_primes_from_history():
    """시작 직후 심의(`_opening_deliberation`)는 여전히 첫 적재여야 합니다."""
    ctx = make_ctx()
    desk = desk_without_llm()

    await desk.update(ctx, {SYM.key: ctx.history(SYM)[-1]})

    assert SYM.key in desk._sets
    assert desk._sets[SYM.key].ret20.value is not None
