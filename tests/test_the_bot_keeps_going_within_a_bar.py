""""자동매매인데 계속 한 번씩 정지·재시작해야 해?"

그랬습니다. 데스크는 한 봉에 `max_symbols_per_run` 종목만 보고, 트레이더는
다음 봉이 닫힐 때까지 잤습니다. 일봉이면 **다음 기회가 내일** 입니다. 그래서
정지·재시작이 새 사이클을 억지로 돌리는 유일한 방법이었고, 실제로 그렇게
쓰이고 있었습니다.

문은 일부러 잠겨 있었습니다 — 같은 봉을 두 번 먹이면 지표가 어긋나고,
같은 판단이 회고 장부에 두 건으로 들어갑니다. 맞는 걱정입니다. 그런데 그건
**같은 종목을 다시 심의할 때** 의 걱정이지, 아직 한 번도 안 본 종목을
이어서 볼 때의 것이 아닙니다.

그래서 둘을 갈랐습니다: 봉 장부(지표·`_bar_count`·회고)는 새 봉에서만,
심의는 **아직 안 본 종목** 에 한해 이어서.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from quant.alpha.desk import TradingDesk
from tests.test_desk import SYM, ScriptedLLM, make_ctx


def desk(**kw) -> tuple[TradingDesk, ScriptedLLM]:
    llm = ScriptedLLM()
    return TradingDesk(llm, memory=False, **kw), llm


def feed(d: TradingDesk, ctx, bars: dict):
    return asyncio.run(d.update(ctx, bars))


def bars_of(ctx, symbols):
    return {s.key: ctx.history(s, 1)[0] for s in symbols}


# ── 기본은 예전 그대로 ───────────────────────────────────────────────────
def test_off_by_default_nothing_changes():
    d, llm = desk()
    assert d.continue_within_bar is False
    ctx = make_ctx()
    bars = bars_of(ctx, [SYM])
    feed(d, ctx, bars)
    before = len(llm.calls)
    assert feed(d, ctx, bars) == [], "새 봉 없이 또 심의했습니다"
    assert len(llm.calls) == before


# ── 켜면 이어서 봅니다 ───────────────────────────────────────────────────
def test_it_picks_up_symbols_it_has_not_seen_this_bar():
    d, llm = desk(continue_within_bar=True, max_symbols_per_run=1)
    ctx = make_ctx()
    bars = bars_of(ctx, [SYM])
    feed(d, ctx, bars)
    assert SYM.key in d._covered, "본 종목을 안 적었습니다"


def test_the_same_symbol_is_never_deliberated_twice_in_one_bar():
    """이게 원래 문을 잠가 둔 이유입니다 — 회고 장부가 한 판단을 두 건으로
    세면 적중률이 거짓말이 됩니다."""
    d, llm = desk(continue_within_bar=True)
    ctx = make_ctx()
    bars = bars_of(ctx, [SYM])
    feed(d, ctx, bars)
    before = len(llm.calls)
    assert feed(d, ctx, bars) == []
    assert len(llm.calls) == before, "같은 종목을 다시 심의했습니다"


def test_a_new_bar_opens_everything_again():
    d, llm = desk(continue_within_bar=True)
    ctx = make_ctx()
    feed(d, ctx, bars_of(ctx, [SYM]))
    assert d._covered
    later = ctx.history(SYM, 1)[0]
    moved = type(later)(later.symbol, later.ts + timedelta(days=1),
                        later.open, later.high, later.low, later.close,
                        later.volume, later.timeframe)
    feed(d, ctx, {SYM.key: moved})
    assert SYM.key in d._covered, "새 봉인데 안 봤습니다"


# ── 봉 장부는 한 번만 ────────────────────────────────────────────────────
def test_a_follow_up_pass_does_not_advance_the_bar_count():
    """`_bar_count` 가 두 번 오르면 cadence 가 어긋나 심의가 건너뛰어집니다."""
    d, llm = desk(continue_within_bar=True)
    ctx = make_ctx()
    bars = bars_of(ctx, [SYM])
    feed(d, ctx, bars)
    counted = d._bar_count
    feed(d, ctx, bars)
    assert d._bar_count == counted


def test_a_follow_up_pass_does_not_feed_the_indicators_again():
    """스트리밍 지표는 되감을 수 없습니다 — 같은 봉을 두 번 먹이면 창이
    영구히 어긋납니다."""
    d, llm = desk(continue_within_bar=True)
    ctx = make_ctx()
    bars = bars_of(ctx, [SYM])
    feed(d, ctx, bars)
    seen = dict(d._ingested)
    feed(d, ctx, bars)
    assert d._ingested == seen


def test_nothing_happens_before_the_first_bar():
    """첫 봉도 안 왔는데 이어서 볼 것은 없습니다."""
    d, llm = desk(continue_within_bar=True)
    ctx = make_ctx()
    d._ingested[SYM.key] = ctx.history(SYM, 1)[0].ts      # 이미 본 것처럼
    assert feed(d, ctx, bars_of(ctx, [SYM])) == []


# ── 트레이더가 실제로 깨우는가 ───────────────────────────────────────────
def test_the_loop_wakes_inside_the_bar_when_asked():
    import inspect

    from quant.live.trader import LiveTrader

    src = inspect.getsource(LiveTrader.run)
    assert "review_every_minutes" in src
    assert "if review and sleep_for > review" in src


def test_the_interval_is_bounded():
    """0 이면 예전 그대로, 그리고 하루를 넘는 값은 뜻이 없습니다."""
    from quant.config.schema import DataConfig

    assert DataConfig().review_every_minutes == 0
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        DataConfig(review_every_minutes=-1)
    with pytest.raises(ValidationError):
        DataConfig(review_every_minutes=721)


# ── 출하 설정이 실제로 켜져 있는가 ───────────────────────────────────────
@pytest.mark.parametrize("path", ["configs/kr_kis_paper.yaml",
                                  "configs/us_kis_paper.yaml"])
def test_the_practice_configs_keep_going(path):
    from quant.cli import _load

    config = _load(path)
    assert config.data.review_every_minutes > 0, f"{path}: 봉 마감만 기다립니다"
    spec = next(s for s in config.alpha if s.type == "desk")
    assert spec.params.get("continue_within_bar") is True, (
        f"{path}: 깨어나도 데스크가 돌아섭니다")


@pytest.mark.parametrize("path", ["configs/kr_kis_paper.yaml",
                                  "configs/us_kis_paper.yaml"])
def test_one_bar_can_cover_the_whole_candidate_list(path):
    """한 봉 안에 후보를 다 못 훑으면 남은 종목은 또 내일입니다."""
    from quant.cli import _load

    config = _load(path)
    spec = next(s for s in config.alpha if s.type == "desk")
    per_pass = spec.params.get("max_symbols_per_run", 4)
    cap = next((f.params.get("max_symbols") for f in config.universe.filters
                if f.type == "limit"), len(config.universe.symbols))
    passes_needed = -(-cap // per_pass)
    minutes = config.data.review_every_minutes
    assert passes_needed * minutes <= 6 * 60, (
        f"{path}: 후보 {cap}종목을 {per_pass}씩 {minutes}분 간격이면 "
        f"{passes_needed * minutes}분 — 장중에 다 못 봅니다")
