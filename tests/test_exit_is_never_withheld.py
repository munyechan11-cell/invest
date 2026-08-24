"""청산은 어떤 이유로도 미루지 않습니다.

주문 나이 정책이 들어오면서, 취소를 부탁한 주문이 하나라도 있으면 그 종목의
**모든** 주문이 보류됐습니다 — 신규 진입만이 아니라 손절과 리스크 청산과
사용자의 수동 매도까지. 그리고 취소는 성공한다는 보장이 없습니다: KIS 어댑터의
취소는 지금 무조건 실패하므로, 그 종목은 영영 잠겼습니다.

비용을 아끼려고 만든 장치가 포지션을 가두면, 없느니만 못합니다.
"""
from datetime import datetime
from decimal import Decimal

from quant.core.types import UTC, Order, OrderSide, OrderType, Symbol
from quant.execution.base import CANCEL_PATIENCE_BARS, _Resting

SYM = Symbol("005930", venue="kis", quote_currency="KRW")


def _resting(*, reducing: bool, cancel: bool = False, age: int = 0) -> _Resting:
    order = Order(symbol=SYM, side=OrderSide.BUY, quantity=Decimal("10"),
                  type=OrderType.LIMIT, limit_price=70_000)
    rec = _Resting(order=order, reducing=reducing,
                   placed_at=datetime(2026, 8, 24, tzinfo=UTC))
    rec.cancel_requested = cancel
    rec.cancel_age = age
    return rec


def test_a_pending_cancel_does_not_go_stale_immediately():
    """정상적으로 취소가 돌아오는 동안에는 기다립니다."""
    assert not _resting(reducing=False, cancel=True, age=0).cancel_stale
    assert not _resting(reducing=False, cancel=True,
                        age=CANCEL_PATIENCE_BARS - 1).cancel_stale


def test_a_cancel_that_never_lands_stops_blocking():
    """거래소가 취소를 거절할 수도 있습니다. 무한정 기다리면 종목이 잠깁니다."""
    assert _resting(reducing=False, cancel=True, age=CANCEL_PATIENCE_BARS).cancel_stale
    assert _resting(reducing=False, cancel=True,
                    age=CANCEL_PATIENCE_BARS * 10).cancel_stale


def test_an_order_with_no_cancel_pending_is_never_stale():
    assert not _resting(reducing=False, cancel=False, age=99).cancel_stale


# ── 실제 보류 판정 ───────────────────────────────────────────────────────
class _Ctx:
    """`_withheld` 가 실제로 만지는 표면만."""

    def __init__(self):
        self.bar_index = 0


def _model_with_pending_cancel():
    from quant.execution.models import ImmediateExecution

    model = ImmediateExecution()
    rec = _resting(reducing=False, cancel=True)
    model._resting[rec.order.id] = rec
    return model


def test_an_exit_goes_out_while_a_cancel_is_pending():
    """이것이 이 파일의 요점입니다 — 손절이 취소 대기에 갇히면 안 됩니다."""
    model = _model_with_pending_cancel()
    assert model._withheld(_Ctx(), SYM, reducing=True) is False


def test_an_entry_waits_while_a_cancel_is_pending():
    """진입은 기다려도 됩니다 — 같은 생각에 두 번 베팅하지 않기 위해서."""
    model = _model_with_pending_cancel()
    assert model._withheld(_Ctx(), SYM, reducing=False) is True


def test_an_entry_stops_waiting_once_the_cancel_has_gone_stale():
    model = _model_with_pending_cancel()
    for rec in model._resting.values():
        rec.cancel_age = CANCEL_PATIENCE_BARS
    assert model._withheld(_Ctx(), SYM, reducing=False) is False


def test_another_symbol_is_never_affected():
    model = _model_with_pending_cancel()
    other = Symbol("000660", venue="kis", quote_currency="KRW")
    assert model._withheld(_Ctx(), other, reducing=False) is False
    assert model._withheld(_Ctx(), other, reducing=True) is False
