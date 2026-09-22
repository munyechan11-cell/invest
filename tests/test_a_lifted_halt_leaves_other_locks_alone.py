"""드로다운 정지가 풀릴 때 남의 잠금까지 지우면 안 된다.

`MaximumDrawdownPortfolio` 는 자기 정지 시간이 끝나면 `ctx.unlock_all()` 을
불렀습니다. 그건 `_locks.clear()` 입니다 — 쿨다운, 종목별 손절 가드, 저수익
잠금까지 **전부** 사라집니다. 각자 자기 만료가 한참 남았는데도요.

하필 그 시점입니다. 드로다운 정지가 막 끝난 직후는 방금 연타로 손절당한
종목들이 잠겨 있는 때이고, 그 잠금이 제일 필요한 때입니다. 정지가 풀리면서
그 종목들이 같이 열립니다.

자기가 건 것만 풉니다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, PortfolioTarget, Symbol
from quant.risk.models import MaximumDrawdownPortfolio

SYM = Symbol("AAA", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def _ctx() -> Context:
    ctx = Context(SimClock(T0), Portfolio(100_000.0), EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    return ctx


def _tripped(ctx: Context, halt_bars: int = 2) -> MaximumDrawdownPortfolio:
    model = MaximumDrawdownPortfolio(max_drawdown_pct=0.10, halt_bars=halt_bars)
    ctx.portfolio.high_water_mark = 200_000.0        # 자산 10만 → 드로다운 50%
    model.manage(ctx, [PortfolioTarget(SYM, Decimal("10"))])
    assert model.tripped
    return model


def test_a_cooldown_survives_the_halt_being_lifted():
    ctx = _ctx()
    ctx.lock(SYM, T0 + timedelta(days=30), "cooldown: 청산 직후")
    model = _tripped(ctx)

    ctx.clock.set(T0 + timedelta(days=2))            # 정지 시간 종료
    ctx.portfolio.high_water_mark = ctx.portfolio.equity
    model.manage(ctx, [])

    assert not model.tripped, "정지는 풀려야 한다"
    locked, why = ctx.is_locked(SYM)
    assert locked and "cooldown" in why


def test_the_halt_itself_is_lifted():
    ctx = _ctx()
    model = _tripped(ctx)
    assert ctx.is_locked(SYM)[0]

    ctx.clock.set(T0 + timedelta(days=2))
    ctx.portfolio.high_water_mark = ctx.portfolio.equity
    model.manage(ctx, [])

    assert not ctx.is_locked(SYM)[0]


def test_unlock_book_only_touches_the_whole_book_lock():
    ctx = _ctx()
    ctx.lock(SYM, T0 + timedelta(days=30), "종목 잠금")
    ctx.lock_all(T0 + timedelta(days=30), "전체 정지")

    ctx.unlock_book()

    assert ctx.is_locked(SYM) == (True, "종목 잠금")
