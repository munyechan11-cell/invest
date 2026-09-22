"""보호장치가 재는 값이 독스트링이 말하는 값과 같아야 한다.

두 군데가 어긋나 있었습니다.

`LowProfitPairs` 는 "aggregate P&L" 을 본다고 써 놓고 **퍼센트 수익률을 그냥
더했습니다.** 크기도 기간도 다른 거래의 퍼센트를 더한 값은 누구도 묻지 않은
질문의 답입니다 — 시드 물량에서 -40%, 정상 물량에서 +40% 면 합은 0 이지만
계좌는 한참 벌었습니다.

`StoplossGuard` 는 `required_profit` 을 받아 놓고 기본 경로에서 **한 번도 읽지
않았습니다.** 그 사이 `was_stopped_out` 은 트레일링 스탑을 스탑아웃으로 셉니다.
이익을 확정한 트레일링 스탑 세 번이면 — 출하 설정의 `trade_limit` 이 3 입니다 —
**잘 돌고 있는 계좌가 멈췄습니다.**
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, ClosedTrade, OrderSide, Symbol
from quant.risk.protections import LowProfitPairs, StoplossGuard

SYM = Symbol("AAA", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def _ctx(*trades: ClosedTrade) -> Context:
    ctx = Context(SimClock(T0), Portfolio(100_000.0), EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    ctx.portfolio.closed_trades.extend(trades)
    ctx.clock.set(T0 + timedelta(days=1))
    return ctx


def _trade(qty: int, pct: float, *, tag: str = "signal:take") -> ClosedTrade:
    """진입가 100 고정 — 투입 자본은 곧 수량이다."""
    entry, deployed = 100.0, qty * 100.0
    return ClosedTrade(symbol=SYM, side=OrderSide.SELL, quantity=Decimal(qty),
                       entry_price=entry, exit_price=entry * (1 + pct),
                       entry_ts=T0 - timedelta(days=5), exit_ts=T0,
                       pnl=deployed * pct, pnl_pct=pct, fees=0.0, exit_tag=tag)


# ── LowProfitPairs ──────────────────────────────────────────────────────

def test_a_winning_symbol_is_not_halted_for_a_negative_percent_sum():
    # 퍼센트 합 -5%. 실제로는 1,200 넣어 355 벌었다 — 자본 대비 +29.6%.
    ctx = _ctx(_trade(1, -0.40), _trade(10, +0.40), _trade(1, -0.05))

    triggered, reason = LowProfitPairs().check(ctx, SYM)
    assert not triggered, reason


def test_a_bleeding_symbol_is_halted_though_the_percent_sum_is_positive():
    # 퍼센트 합 +50%. 실제로는 1,200 넣어 40 잃었다 — 자본 대비 -3.3%.
    ctx = _ctx(_trade(10, -0.10), _trade(1, +0.30), _trade(1, +0.30))

    triggered, reason = LowProfitPairs().check(ctx, SYM)
    assert triggered
    assert "-3.33%" in reason


def test_low_profit_still_waits_for_min_trades():
    ctx = _ctx(_trade(10, -0.10), _trade(1, -0.10))

    assert not LowProfitPairs(min_trades=3).check(ctx, SYM)[0]


# ── StoplossGuard ───────────────────────────────────────────────────────

def _stopped(pct: float) -> ClosedTrade:
    return _trade(10, pct, tag="trailing_stop:locked in")


def test_profitable_trailing_stops_do_not_halt_the_book():
    """이익을 확정한 청산은 전략이 틀렸다는 증거가 아니다."""
    ctx = _ctx(_stopped(+0.08), _stopped(+0.05), _stopped(+0.11))

    assert not StoplossGuard(trade_limit=3).check(ctx, None)[0]

    StoplossGuard(trade_limit=3).apply(ctx)
    assert not ctx.is_locked(SYM)[0]


def test_losing_stop_outs_still_halt_the_book():
    """느슨해지면 안 된다 — 진짜 손절 연타는 그대로 잡아야 한다."""
    ctx = _ctx(_stopped(-0.03), _stopped(-0.02), _stopped(-0.04))

    triggered, reason = StoplossGuard(trade_limit=3).check(ctx, None)
    assert triggered
    assert "trailing_stop" in reason

    StoplossGuard(trade_limit=3).apply(ctx)
    assert ctx.is_locked(SYM)[0]


def test_required_profit_is_read_on_the_default_path():
    """기본값 stops_only=True 에서도 문턱이 살아 있어야 한다."""
    ctx = _ctx(_stopped(-0.03), _stopped(-0.02), _stopped(-0.04))

    # -5% 보다 나쁜 것만 센다 → 셋 다 해당 없음.
    assert not StoplossGuard(trade_limit=3, required_profit=-0.05).check(ctx, None)[0]
