"""쿨다운이 적어 놓은 봉 수만큼만 잠가야 한다 — 두 배가 아니라.

`Protection.apply` 는 봉마다 돕니다. 그런데 만료를 `ctx.now + stop_bars` 로
**매번 다시 계산**했고, `Context.lock` 은 더 먼 만료를 채택합니다. 쿨다운은
대기 중인 모든 봉에서 `check` 가 True 를 돌려주므로, 잠금 끝이 한 봉씩 앞으로
밀렸습니다.

`stop_bars=3` 이면 청산 후 3봉이 아니라 5봉을 잠급니다. 전략이 되사려고 의도한
자리를 조용히 건너뜁니다 — 로그에는 "cooling down" 만 찍히니 눈에 띄지도
않습니다.

만료는 **청산 시각** 에 묶여야 합니다. 그래야 다시 계산해도 같은 값이 나옵니다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, ClosedTrade, OrderSide, Symbol
from quant.risk.protections import CooldownPeriod

SYM = Symbol("AAA", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def _ctx() -> Context:
    ctx = Context(SimClock(T0), Portfolio(100_000.0), EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    return ctx


def _exit_at(ts: datetime, *, pnl: float = -50.0) -> ClosedTrade:
    return ClosedTrade(symbol=SYM, side=OrderSide.SELL, quantity=Decimal("10"),
                       entry_price=100.0, exit_price=95.0,
                       entry_ts=ts - timedelta(days=5), exit_ts=ts,
                       pnl=pnl, pnl_pct=pnl / 1000.0, fees=0.0)


def _locked_bars(guard: CooldownPeriod, ctx: Context, bars: int = 8) -> list[int]:
    """청산 후 n 번째 봉마다 잠겨 있었는지. 잠긴 봉 번호만 돌려준다."""
    locked = []
    for n in range(1, bars + 1):
        ctx.clock.set(T0 + timedelta(days=n))
        guard.apply(ctx)
        if ctx.is_locked(SYM)[0]:
            locked.append(n)
    return locked


def test_cooldown_lasts_exactly_stop_bars():
    ctx = _ctx()
    ctx.portfolio.closed_trades.append(_exit_at(T0))

    # 3봉 쿨다운: 청산 후 1·2봉은 잠기고, 3봉째에는 풀려 있어야 한다.
    assert _locked_bars(CooldownPeriod(stop_bars=3), ctx) == [1, 2]


def test_cooldown_expiry_is_anchored_to_the_exit():
    """만료를 다시 계산해도 값이 움직이지 않아야 한다."""
    ctx = _ctx()
    ctx.portfolio.closed_trades.append(_exit_at(T0))
    guard = CooldownPeriod(stop_bars=3)

    seen = set()
    for n in (1, 2):
        ctx.clock.set(T0 + timedelta(days=n))
        seen.add(guard.lock_until(ctx, SYM))

    assert seen == {T0 + timedelta(days=3)}


def test_a_trim_does_not_start_a_cooldown():
    """부분 청산은 나간 것이 아니다 — 들고 있는 종목을 잠그면 안 된다."""
    ctx = _ctx()
    trim = _exit_at(T0)
    trim.closes_position = False
    ctx.portfolio.closed_trades.append(trim)

    assert _locked_bars(CooldownPeriod(stop_bars=3), ctx) == []
