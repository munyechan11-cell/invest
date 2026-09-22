"""전체 정지는 더 짧은 정지가 덮어쓸 수 없어야 한다.

`Context.lock` 은 종목 잠금을 줄이지 못하게 막아 둡니다 — 더 먼 만료를
채택합니다. 그런데 바로 옆 `lock_all` 은 비교 없이 `_locks["*"]` 에 그냥
써 버렸습니다.

전체 정지를 거는 주체가 하나가 아닙니다. `MaxDrawdownProtection` 은 24봉,
`StoplossGuard` 는 (기본값이 전 종목 잠금입니다) 12봉. 둘 다 봉마다 평가됩니다.
드로다운이 깊은 구간은 손절이 연달아 터지는 구간이므로 겹치는 게 정상입니다.

그래서 24봉 정지가 걸린 다음 봉에 손절 가드가 발동하면, 12봉짜리가 24봉짜리를
밀어냅니다. **가장 강한 안전장치가, 그게 있어야 할 바로 그 구간에서 반으로
잘립니다.** 정지를 일찍 푸는 길은 `unlock_all` 하나여야 합니다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, Symbol
from quant.risk.protections import Protection

SYM = Symbol("AAA", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))
T0 = datetime(2024, 1, 1, tzinfo=UTC)


class Halt(Protection):
    """항상 발동하는 전 종목 정지. 봉 수만 다르게 둔다."""

    per_symbol = False

    def __init__(self, name: str, stop_bars: int):
        super().__init__(lookback_bars=60, stop_bars=stop_bars)
        self.name = name

    def check(self, ctx, symbol):
        return True, "test"


def _ctx() -> Context:
    ctx = Context(SimClock(T0), Portfolio(100_000.0), EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    return ctx


def test_a_shorter_halt_does_not_replace_a_longer_one():
    ctx = _ctx()
    Halt("deep_drawdown", stop_bars=24).apply(ctx)

    ctx.clock.set(T0 + timedelta(days=1))
    Halt("stop_run", stop_bars=12).apply(ctx)  # 만료는 T0+13일 — 더 가깝다

    assert ctx.export_locks()["*"][0] == T0 + timedelta(days=24)

    # 12봉짜리가 이겼다면 여기서 이미 풀려 있다.
    ctx.clock.set(T0 + timedelta(days=20))
    assert ctx.is_locked(SYM)[0]


def test_a_longer_halt_still_extends():
    """줄이지 못할 뿐, 늘리는 것은 되어야 한다."""
    ctx = _ctx()
    Halt("stop_run", stop_bars=12).apply(ctx)
    Halt("deep_drawdown", stop_bars=24).apply(ctx)

    assert ctx.export_locks()["*"][0] == T0 + timedelta(days=24)
    assert "deep_drawdown" in ctx.is_locked(SYM)[1]


def test_unlock_all_still_lifts_a_halt():
    """일찍 푸는 길은 하나 — 리스크 모델이 명시적으로 푸는 것."""
    ctx = _ctx()
    Halt("deep_drawdown", stop_bars=24).apply(ctx)
    assert ctx.is_locked(SYM)[0]

    ctx.unlock_all()
    assert not ctx.is_locked(SYM)[0]
