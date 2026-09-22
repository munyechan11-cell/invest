"""알파가 죽었다고 그 종목을 팔면 안 된다.

포트폴리오 층은 **없음을 지시로 읽습니다** — 보유 중인데 활성 인사이트가 없으면
0 타깃, 곧 청산입니다. 모델이 보고 나서 "이제 아니다" 라고 했다면 맞는 규칙입니다.

**모델이 보지도 못했을 때는 틀립니다.** `CompositeAlphaModel` 은 예외를 잡아
`log.exception` 한 줄만 남기고 나머지 모델의 결과를 돌려줬습니다. 그 모델이 내던
인사이트는 각자 지평선이 지나면 만료되고, 그 다음 봉에 그 종목들이 **시장가로
팔립니다.** KRX 에서는 나가는 길에 거래세를 냅니다. 아무도 내리지 않은 결정에.

저장소 전체에 그 실패의 흔적은 `log.exception` 한 줄뿐이었습니다 — 이벤트도,
`status()` 도, 알림도 없습니다. `accf346` 이 사흘을 들인 바로 그 모양입니다.

다만 **본 적 없는 것** 만 붙잡습니다. 활성 인사이트가 있는데 가중치가 0 이면
그건 침묵이 아니라 판단(FLAT 거부권, 유지 문턱 미달)이고 그대로 따릅니다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

from quant.alpha.base import AlphaModel, CompositeAlphaModel
from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus, EventType
from quant.core.types import UTC, Bar, Direction, Insight, Symbol
from quant.portfolio.models import EqualWeighting

SYM = Symbol("AAA", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))
T0 = datetime(2024, 1, 1, tzinfo=UTC)


class Broken(AlphaModel):
    name = "broken"

    async def update(self, ctx, bars):
        raise RuntimeError("feature store unreachable")


class Quiet(AlphaModel):
    """멀쩡히 돌지만 이번 봉에 할 말이 없는 모델."""

    name = "quiet"

    def __init__(self):
        self.calls = 0

    async def update(self, ctx, bars):
        self.calls += 1
        return []


def _ctx(held: int = 100) -> Context:
    ctx = Context(SimClock(T0), Portfolio(100_000.0), EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    ctx.push_bar(Bar(SYM, T0, 100.0, 100.0, 100.0, 100.0, 1_000))
    ctx.clock.set(T0 + timedelta(days=1))
    if held:
        pos = ctx.portfolio.position(SYM)
        pos.quantity, pos.avg_price = Decimal(held), 100.0
        pos.mark(100.0)
    assert ctx.price(SYM) == 100.0
    return ctx


def _run(model, ctx):
    return asyncio.run(model.update(ctx, {}))


# ── 보고 ────────────────────────────────────────────────────────────────

def test_a_crash_reaches_the_event_bus():
    ctx = _ctx()
    seen = []
    ctx.bus.on(EventType.ERROR, seen.append)

    _run(CompositeAlphaModel(Broken()), ctx)

    assert len(seen) == 1
    payload = seen[0].payload
    assert payload["component"] == "alpha"
    assert "feature store unreachable" in payload["reason"]


def test_the_state_flag_is_set_and_cleared():
    ctx = _ctx()
    quiet = Quiet()
    composite = CompositeAlphaModel(Broken(), quiet)

    _run(composite, ctx)
    assert ctx.state("alpha")["degraded"]

    _run(CompositeAlphaModel(quiet), ctx)
    assert not ctx.state("alpha")["degraded"]


def test_the_working_models_still_run():
    ctx = _ctx()
    quiet = Quiet()
    _run(CompositeAlphaModel(Broken(), quiet), ctx)

    assert quiet.calls == 1


# ── 결과 ────────────────────────────────────────────────────────────────

def _targets(ctx, insights):
    return EqualWeighting().create_targets(ctx, insights)


def test_a_held_name_is_sold_when_the_alpha_layer_is_healthy():
    """기존 규칙은 그대로다 — 없으면 청산."""
    ctx = _ctx()
    _run(CompositeAlphaModel(Quiet()), ctx)

    out = _targets(ctx, [])
    assert [t.quantity for t in out] == [Decimal("0")]


def test_a_held_name_is_left_alone_when_a_model_crashed():
    ctx = _ctx()
    _run(CompositeAlphaModel(Broken()), ctx)

    assert _targets(ctx, []) == [], "죽은 알파 때문에 팔렸다"
    # 보유는 그대로 남아 있어야 한다.
    assert ctx.portfolio.quantity(SYM) == Decimal("100")


def test_an_explicit_flat_still_closes_even_while_degraded():
    """거부권은 침묵이 아니다 — 불완전해도 나가라는 지시는 따른다."""
    ctx = _ctx()
    _run(CompositeAlphaModel(Broken()), ctx)

    veto = Insight(SYM, Direction.FLAT, timedelta(days=5), ctx.now,
                   confidence=1.0, source="regime")
    out = _targets(ctx, [veto])

    assert [t.quantity for t in out] == [Decimal("0")]


def test_a_live_view_still_opens_while_another_model_is_down():
    ctx = _ctx(held=0)
    _run(CompositeAlphaModel(Broken()), ctx)

    up = Insight(SYM, Direction.UP, timedelta(days=5), ctx.now,
                 confidence=1.0, source="momentum")
    out = _targets(ctx, [up])

    assert out and out[0].quantity > 0
