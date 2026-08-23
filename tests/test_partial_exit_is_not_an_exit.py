"""부분매도는 손익 실현이지 청산이 아닙니다.

분할매도를 거래 기록에 남기기 시작하자, 청산을 보고 동작하는 보호장치들이
리밸런싱 축소까지 청산으로 읽었습니다. 데모 백테스트에서 쿨다운이 매 분할매도
마다 걸려 수익률이 -14.5%에서 -24.3%로 떨어졌습니다 — 회계는 정확한데
거래 경로가 망가진 경우라, 손익 검증만으로는 잡히지 않습니다.
"""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.core.account import Portfolio
from quant.core.types import UTC, Fill, OrderSide, Symbol

SYM = Symbol("AAA", venue="test", quote_currency="USD")


def _fill(side, qty, px, n, fee=0.0):
    return Fill(order_id=f"o{n}", symbol=SYM, side=side, quantity=Decimal(str(qty)),
                price=px, fee=fee, ts=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=n))


def _scale_out():
    pf = Portfolio(100_000.0, "USD")
    pf.apply_fill(_fill(OrderSide.BUY, 100, 100.0, 1))
    trim = pf.apply_fill(_fill(OrderSide.SELL, 40, 110.0, 2))
    final = pf.apply_fill(_fill(OrderSide.SELL, 60, 120.0, 3))
    return pf, trim, final


def test_a_trim_is_recorded_but_is_not_a_close():
    _, trim, final = _scale_out()
    assert trim is not None, "부분매도가 거래 기록에서 사라졌습니다"
    assert not trim.closes_position, "부분매도가 청산으로 표시됐습니다"
    assert final.closes_position, "전량 청산이 청산으로 표시되지 않았습니다"


def test_the_pnl_of_a_scale_out_still_adds_up():
    """구분을 넣느라 회계를 망가뜨리지 않았는지."""
    pf, trim, final = _scale_out()
    assert pf.position(SYM).quantity == 0
    assert round(trim.pnl + final.pnl, 6) == round(pf.equity - 100_000.0, 6)


def test_cooldown_ignores_a_trim_but_honours_a_real_exit():
    from quant.risk.protections import CooldownPeriod

    class Ctx:
        """Protection 이 실제로 부르는 표면만 흉내냅니다."""
        bar_delta = timedelta(days=1)

        def __init__(self, trades, now):
            self._trades, self.now = trades, now

        def recent_trades(self, symbol, within):
            return [t for t in self._trades
                    if (symbol is None or t.symbol.key == symbol.key)
                    and self.now - t.exit_ts <= within]

    pf, trim, final = _scale_out()
    guard = CooldownPeriod(stop_bars=3)
    day4 = datetime(2026, 1, 5, tzinfo=UTC)

    # 분할매도만 있는 상태 — 아직 들고 있으므로 잠기면 안 됩니다.
    blocked, why = guard.check(Ctx([trim], day4), SYM)
    assert not blocked, f"부분매도로 잠겼습니다: {why}"

    # 진짜 청산 뒤에는 잠겨야 합니다.
    blocked, why = guard.check(Ctx([trim, final], day4), SYM)
    assert blocked and "cooling down" in why


@pytest.mark.parametrize("closed_first", [True, False])
def test_a_flip_through_zero_counts_as_a_close(closed_first):
    """롱에서 숏으로 뒤집는 것은 청산입니다."""
    pf = Portfolio(100_000.0, "USD")
    pf.apply_fill(_fill(OrderSide.BUY, 100, 100.0, 1))
    if closed_first:
        pf.apply_fill(_fill(OrderSide.SELL, 100, 110.0, 2))
        trade = pf.apply_fill(_fill(OrderSide.SELL, 50, 111.0, 3))
        assert trade is None or trade.closes_position
    else:
        trade = pf.apply_fill(_fill(OrderSide.SELL, 150, 110.0, 2))
        assert trade.closes_position, "포지션을 뒤집었는데 청산이 아니라고 합니다"
