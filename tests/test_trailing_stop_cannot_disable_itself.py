"""ATR 스케일링이 트레일링 스톱을 **산술적으로 불가능하게** 만들지 않습니다.

롱의 발동 조건은 `price <= peak * (1 - trail)` 입니다. `trail` 이 1.0 을 넘으면
우변이 0 이하가 되고, 양수인 가격은 어떤 값도 그 조건을 만족시키지 못합니다 —
트레일링 스톱이 예외도 로그도 없이 사라집니다. 그리고 하필 **변동성이 치솟은
순간**, 그게 필요한 바로 그때 사라집니다.

실측(수정 전): `atr_multiple=5.0`(출하 설정 전부)에서 봉 변동폭이 ±10% 면
trail=1.00 → 임계가 0, 고점 대비 -50% 인 보유가 청산되지 않습니다.

**이 수정이 하는 일과 안 하는 일.** 불가능한 상태를 없애고 그 사실을 말합니다.
95% 트레일이 실효 보호가 된다는 뜻은 아닙니다 — 그 구간의 실제 보호는
`max_dd_per_security`(진입가 기준, 상한이 걸려 있음)가 하고, 트레일이 물게
하려면 그 종목의 `atr_multiple` 을 낮춰야 합니다. 그건 전략 결정이라 코드가
정하지 않고 경고로 알립니다.
"""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, Bar, Symbol
from quant.risk.models import TrailingStopRiskModel

T0 = datetime(2024, 6, 3, tzinfo=UTC)
SYM = Symbol("VOL", venue="SIM", tick_size=Decimal("1"), lot_size=Decimal("1"))


def book(swing: float, *, peak: float = 2000.0, now: float = 1000.0,
         avg: float = 500.0) -> Context:
    """하루에 ±`swing` 씩 흔들리는 종목을, 고점 대비 반토막 난 상태로."""
    pf = Portfolio(1_000_000.0)
    ctx = Context(SimClock(T0), pf, EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    price = 1000.0
    for i in range(30):
        ctx.push_bar(Bar(SYM, T0 - timedelta(days=31 - i), price,
                         price * (1 + swing), price * (1 - swing), price,
                         1e6, "1d"))
    # 마지막 봉을 현재가로 닫습니다 — `manage` 는 포지션의 mark 가 아니라
    # `ctx.price()`(= 마지막으로 **닫힌** 봉의 종가)를 봅니다. `ctx.now` 이후에
    # 끝나는 봉은 Context 가 내주지 않으므로 하루 앞에 놓습니다.
    ctx.push_bar(Bar(SYM, T0 - timedelta(days=1), now, max(now, price),
                     min(now, price), now, 1e6, "1d"))
    pos = pf.position(SYM)
    pos.quantity, pos.avg_price = Decimal("10"), avg
    pos.mark(peak)
    pos.mark(now)
    return ctx


def model(**kw) -> TrailingStopRiskModel:
    return TrailingStopRiskModel(trail_pct=0.06, activate_at_pct=0.0,
                                 atr_multiple=5.0, **kw)


# ── 산술이 불가능해지지 않는다 ───────────────────────────────────────────
@pytest.mark.parametrize("swing", [0.10, 0.15, 0.20, 0.30, 0.60])
def test_a_violent_instrument_cannot_push_the_threshold_to_zero(swing):
    """임계가 0 이하가 되면 그 종목의 트레일링 스톱은 존재하지 않습니다."""
    ctx = book(swing)
    trail = model()._trail_for(ctx, SYM)
    assert trail < 1.0, f"trail={trail:.2f} → 임계 {1 - trail:.2f} × 고점"
    assert (1.0 - trail) > 0


@pytest.mark.parametrize("swing", [0.10, 0.15, 0.30, 0.60])
def test_the_stop_still_fires_somewhere_below_the_peak(swing):
    """천장에 걸린 트레일도 **도달 가능한** 가격이어야 합니다.

    정확한 임계가 아니라 "임계가 존재한다" 를 봅니다 — 폭락 봉 자체가 ATR 을
    바꾸므로 특정 숫자를 박으면 테스트가 그 숫자를 지키게 됩니다. 천장이
    0.95 이므로 임계는 아무리 낮아도 고점의 5% 이고, 그 아래면 반드시
    나가야 합니다.

    진입가를 낮게 둡니다 — 트레일링 스톱은 `activate_at` 위, 즉 아직 이익인
    포지션에만 적용되기 때문입니다. 손실 구간은 `max_dd_per_security` 의
    자리이고, 그 분업 자체가 "95% 트레일은 실효 보호가 아니다" 의 다른
    표현입니다.
    """
    deep = 2000.0 * 0.001                      # 어떤 임계보다도 아래
    ctx = book(swing, now=deep, avg=deep / 2)  # 그래도 아직 +100%
    assert ctx.portfolio.position(SYM).unrealized_pct > 0
    assert [t for t in model().manage(ctx, []) if t.quantity == 0], (
        "고점의 0.1% 까지 떨어졌는데도 트레일링 스톱이 나가지 않습니다")


def test_a_losing_position_is_not_the_trailing_stops_job():
    """진입가 아래로 내려간 보유는 `activate_at` 때문에 트레일링 스톱이
    보지 않습니다 — 그건 `max_dd_per_security` 의 자리입니다. 이 경계를
    모르면 위 천장이 손실 보호라고 오해하게 됩니다."""
    ctx = book(0.30, now=50.0, avg=500.0)              # -90%
    assert ctx.portfolio.position(SYM).unrealized_pct < 0
    assert [t for t in model().manage(ctx, []) if t.quantity == 0] == []


def test_the_ceiling_never_touches_a_trail_that_already_works():
    """지금 걸리던 손절이 더 자주 걸리기 시작하면 그건 안전 수정이 아니라
    전략 변경입니다. 천장 아래 값은 한 톨도 건드리지 않습니다."""
    for swing in (0.001, 0.01, 0.02, 0.05, 0.08):
        ctx = book(swing)
        raw = model()._trail_for(ctx, SYM)
        assert raw < TrailingStopRiskModel.MAX_TRAIL
        # 천장이 없던 시절의 식과 같은 값인지 직접 대조합니다.
        bars = ctx.history(SYM, 15)
        trs = [max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
               for p, b in zip(bars, bars[1:])]
        import statistics
        expected = max(statistics.fmean(trs[-14:]) * 5.0 / bars[-1].close, 0.005)
        assert raw == pytest.approx(expected)


def test_a_fixed_trail_is_clamped_too():
    """`atr_multiple` 없이 `trail_pct: 1.5` 를 적어도 같은 구멍입니다."""
    fixed = TrailingStopRiskModel(trail_pct=1.5, activate_at_pct=0.0)
    assert fixed._trail_for(book(0.01), SYM) < 1.0


def test_the_floor_still_holds():
    """움직이지 않는 종목이 트레일을 0 으로 만들면 매 봉 청산됩니다."""
    flat = book(0.0)
    assert model()._trail_for(flat, SYM) >= 0.005


# ── 조용히 넘어가지 않는다 ───────────────────────────────────────────────
def test_hitting_the_ceiling_names_the_symbol_and_the_parameter(caplog):
    with caplog.at_level("WARNING", logger="quant.risk.models"):
        model()._trail_for(book(0.30), SYM)
    assert any("VOL" in r.getMessage() for r in caplog.records), caplog.text
    assert "atr_multiple" in caplog.text


def test_it_says_so_once_not_every_bar(caplog):
    """봉마다 같은 줄을 찍으면 아무도 안 읽습니다."""
    m = model()
    with caplog.at_level("WARNING", logger="quant.risk.models"):
        for _ in range(5):
            m._trail_for(book(0.30), SYM)
    assert len([r for r in caplog.records if "atr_multiple" in r.getMessage()]) == 1


def test_a_healthy_symbol_says_nothing(caplog):
    with caplog.at_level("WARNING", logger="quant.risk.models"):
        model()._trail_for(book(0.01), SYM)
    assert "atr_multiple" not in caplog.text
