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


# ── 엔진에 "판단만 다시" 길이 있는가 ────────────────────────────────────
#
# 데스크만 고쳐서는 소용이 없었습니다. 트레이더가 `if not bars: return` 으로
# 먼저 돌아서고, 엔진도 `on_bars` 첫 줄에서 같은 이유로 돌아섭니다. 데스크는
# 준비됐는데 **거기까지 도달을 못 했습니다** — 실제로 그렇게 배포됐고, 20분
# 뒤 두 번째 바퀴에 아무 일도 일어나지 않았습니다.

def test_the_engine_can_decide_without_new_bars():
    from quant.core.engine import Engine

    assert hasattr(Engine, "review"), "판단만 다시 도는 길이 없습니다"
    assert hasattr(Engine, "_decide"), "on_bars 뒤쪽 절반이 떨어져 있지 않습니다"


def test_review_does_not_touch_the_bar_ledger():
    """`push_bar` 는 **중복을 거르지 않습니다.** 같은 봉을 다시 넣으면 모든
    지표의 창이 영구히 어긋납니다 — 그래서 `on_bars` 를 다시 부르는 것으로는
    안 됩니다."""
    import inspect

    from quant.core.engine import Engine

    def code_only(fn) -> str:
        """설명문은 빼고 **실제로 도는 줄** 만. 독스트링에 그 이름이 나오는
        것과 그 함수를 부르는 것은 다릅니다."""
        src = inspect.getsource(fn)
        doc = inspect.getdoc(fn) or ""
        for line in doc.splitlines():
            src = src.replace(line, "")
        return src

    for fn in (Engine.review, Engine._decide):
        body = code_only(fn)
        assert "push_bar(" not in body, f"{fn.__name__} 이 봉을 다시 쌓습니다"
        assert "self._settle(" not in body, f"{fn.__name__} 이 체결을 다시 정산합니다"


def test_the_trader_actually_calls_it():
    import inspect

    from quant.live.trader import LiveTrader

    src = inspect.getsource(LiveTrader._tick)
    assert "self.engine.review()" in src
    assert "review_every_minutes" in src


def test_review_is_off_unless_the_config_asks():
    """켜지 않은 설정이 조용히 더 자주 돌면 안 됩니다 — 비용이 거기에
    비례합니다."""
    import inspect

    from quant.live.trader import LiveTrader

    src = inspect.getsource(LiveTrader._tick)
    assert "if self.config.data.review_every_minutes:" in src


def test_review_does_nothing_without_a_universe():
    import asyncio
    from types import SimpleNamespace

    from quant.core.engine import Engine

    stub = SimpleNamespace(ctx=SimpleNamespace(universe=[], latest=lambda s: None))
    asyncio.run(Engine.review(stub))      # 터지지 않으면 됩니다
