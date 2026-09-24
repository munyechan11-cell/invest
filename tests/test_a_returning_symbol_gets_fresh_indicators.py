"""유니버스를 떠났다 돌아온 종목의 브리프는 공백을 가로질러 계산되지 않는다.

`kr_toss`·`us_toss` 는 21봉마다 유니버스를 다시 고릅니다. 빠진 종목에는
엔진이 봉을 넘기지 않지만(`Engine._active`) `ctx.history` 에는 계속
쌓습니다. 데스크는 지표를 그대로 들고 있다가, 몇 주 뒤 돌아온 종목에 새 봉
**하나** 를 이어 붙였습니다. 그래서 실제 +12.5% 인 5봉 수익률이 +86% 로,
ATR 이 두 배로 적힌 브리프를 좌석들이 "사실로 간주하라" 는 말과 함께
읽었고, 후보 순위(`_shortlist`)도 그 숫자로 매겼습니다.

오라클은 구현식이 아니라 **처음부터 쌓은 데스크** 입니다 — 봉을 놓친 데스크는
한 번도 떠나지 않은 데스크와 같은 숫자를 읽어야 합니다.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from quant.alpha.base import CompositeAlphaModel
from quant.alpha.desk import TradingDesk
from quant.alpha.llm_client import LLMUsage
from quant.core.types import Bar
from tests.test_desk import SYM, make_ctx


class NeverCalledLLM:
    """부르면 실패합니다 — 여기서는 LLM 앞단(지표 적재)만 봅니다."""

    def __init__(self):
        self.usage = LLMUsage()

    async def complete(self, system, user, schema=None):   # pragma: no cover
        raise AssertionError("이 테스트에서 LLM 이 불리면 안 됩니다")


def desk_without_llm() -> TradingDesk:
    """비용 한도로 심의를 막습니다. 지표 적재는 그 검사보다 앞이라 그대로 돕니다."""
    desk = TradingDesk(NeverCalledLLM(), memory=False, cost_limit_usd=1e-9)
    desk.client.usage.add(1_000_000, 1_000_000)
    return desk


def feed(desk: TradingDesk, ctx, bar: Bar) -> None:
    assert asyncio.run(desk.update(ctx, {bar.symbol.key: bar})) == []


def rally_while_away(ctx, bars: int = 21) -> Bar:
    """데스크가 보지 못하는 동안 `bars` 봉(하루 +3%)이 ctx 에만 쌓인다."""
    last = ctx.history(SYM)[-1]
    price = last.close
    bar = last
    for i in range(1, bars + 2):
        ts = last.ts + timedelta(days=i)
        price = price * (1.03 if i <= bars else 1.0)
        ctx.clock.set(ts + timedelta(days=1))
        bar = Bar(SYM, ts, price, price * 1.01, price * 0.99, price, 1e6, "1d")
        ctx.push_bar(bar)
    return bar


def numbers(desk: TradingDesk) -> dict:
    ind = desk._sets[SYM.key]
    return {"ret5": ind.ret5.value, "ret20": ind.ret20.value, "atr": ind.atr.value,
            "adx": ind.adx.value, "vol": ind.vol.value, "sma20": ind.sma20.value,
            "rsi": ind.rsi.value}


def test_a_symbol_that_missed_bars_reads_the_same_numbers_as_one_that_never_left():
    ctx = make_ctx()
    away = desk_without_llm()
    feed(away, ctx, ctx.history(SYM)[-1])          # 유니버스 안에서 한 번 봤다
    back = rally_while_away(ctx)                    # 그 사이 21봉 — 데스크는 못 봤다
    feed(away, ctx, back)                           # 돌아와 새 봉 하나

    fresh = desk_without_llm()                      # 처음부터 쌓은 데스크
    feed(fresh, ctx, back)

    closes = [b.close for b in ctx.history(SYM)]
    assert away._sets[SYM.key].ret5.value == pytest.approx(closes[-1] / closes[-6] - 1)
    assert numbers(away) == pytest.approx(numbers(fresh))
    brief = away.build_brief(ctx, SYM)
    assert brief["가격"]["5봉수익률%"] == fresh.build_brief(ctx, SYM)["가격"]["5봉수익률%"]


def test_consecutive_bars_keep_the_same_indicator_set():
    """놓친 봉이 없으면 다시 쌓지 않습니다 — 봉마다 전체를 다시 쌓는 것은 낭비이고,
    스트리밍 지표의 상태를 버릴 이유도 없습니다."""
    ctx = make_ctx()
    desk = desk_without_llm()
    feed(desk, ctx, ctx.history(SYM)[-1])
    before = desk._sets[SYM.key]
    nxt = rally_while_away(ctx, bars=0)             # 바로 다음 봉 하나
    feed(desk, ctx, nxt)
    assert desk._sets[SYM.key] is before


def test_leaving_the_universe_drops_the_desks_indicators():
    """규칙 알파(`technical.py`)는 이미 이렇게 합니다. 데스크는 기본 no-op 을
    물려받아, 떠난 종목의 지표와 적재 기록을 그대로 들고 있었습니다."""
    ctx = make_ctx()
    desk = desk_without_llm()
    feed(desk, ctx, ctx.history(SYM)[-1])
    assert SYM.key in desk._sets and SYM.key in desk._ingested

    CompositeAlphaModel(desk).on_universe_changed(ctx, [], [SYM])   # 엔진이 부르는 길

    assert SYM.key not in desk._sets
    assert SYM.key not in desk._ingested
    # 돌아오면 ctx.history 로 처음부터 쌓습니다.
    back = rally_while_away(ctx)
    feed(desk, ctx, back)
    fresh = desk_without_llm()
    feed(fresh, ctx, back)
    assert numbers(desk) == pytest.approx(numbers(fresh))
