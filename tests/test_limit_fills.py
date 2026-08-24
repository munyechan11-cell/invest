"""지정가가 언제 체결됐다고 볼 것인가.

봉의 저가가 마침 내 매수 지정가와 **같다**는 것은, 그 가격에 거래가 있었다는
뜻이지 내 주문이 체결됐다는 뜻이 아닙니다. 그 값이 봉의 극단이면 거기서 오간
물량은 사실상 없고, 있었더라도 나보다 먼저 서 있던 주문들 몫입니다.

백테스트가 이걸 체결로 세면, 실거래에서는 안 걸릴 주문으로 돈을 벌었다고
기록됩니다. 지정가에 기대는 전략일수록 그 차이가 큽니다 — 배포된 국내 전략
셋이 전부 지정가를 씁니다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant.core.types import Bar, Order, OrderSide, OrderType, Symbol, utcnow
from quant.execution.costs import RealisticFillModel

UTC = timezone.utc
#: 틱 0.01 — 한 틱이 눈에 보이는 크기여서 경계를 짚기 쉽습니다.
SYM = Symbol("AAA", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))
#: 호가 단위 개념이 없는 합성 상품. 관통 규칙의 기준을 세울 수 없습니다.
NOTICK = Symbol("SYN", venue="SIM", tick_size=Decimal("0"), lot_size=Decimal("1"))


def bar(o=100.0, h=101.0, lo=99.0, c=100.0, v=2000.0, symbol=SYM) -> Bar:
    return Bar(symbol, datetime(2026, 1, 2, tzinfo=UTC), o, h, lo, c, v)


def order(limit, side=OrderSide.BUY, qty="1000", symbol=SYM,
          kind=OrderType.LIMIT, stop=None) -> Order:
    return Order(symbol=symbol, side=side, quantity=Decimal(qty), type=kind,
                 limit_price=limit, stop_price=stop, created_at=utcnow())


# ── 관통 ────────────────────────────────────────────────────────────────
def test_a_buy_limit_sitting_exactly_at_the_low_does_not_fill():
    """봉이 거기서 돌아섰다는 것은 내 주문 차례가 오지 않았다는 뜻입니다."""
    m = RealisticFillModel()
    assert m.fill_price(order(99.0), bar(), None, 0.0) == (None, False)


def test_a_sell_limit_sitting_exactly_at_the_high_does_not_fill():
    m = RealisticFillModel()
    assert m.fill_price(order(101.0, OrderSide.SELL), bar(), None, 0.0) == (None, False)


@pytest.mark.parametrize("limit,side", [
    (99.01, OrderSide.BUY),      # 봉이 이 값을 한 틱 지나갔습니다
    (100.99, OrderSide.SELL),
])
def test_a_limit_the_bar_traded_through_fills_at_its_own_price(limit, side):
    m = RealisticFillModel()
    price, is_maker = m.fill_price(order(limit, side), bar(), None, 0.0)
    assert price == pytest.approx(limit)
    assert is_maker, "호가에 걸려 있던 주문은 메이커입니다"


def test_a_price_the_bar_never_reached_does_not_fill():
    m = RealisticFillModel()
    assert m.fill_price(order(98.99), bar(), None, 0.0) == (None, False)


def test_zero_through_ticks_restores_the_old_touch_rule():
    """관통 규칙을 끄고 싶은 쪽이 끌 수 있어야 합니다."""
    m = RealisticFillModel(limit_through_ticks=0)
    price, _ = m.fill_price(order(99.0), bar(), None, 0.0)
    assert price == pytest.approx(99.0)


def test_a_symbol_without_a_tick_ladder_falls_back_to_touching():
    """틱이 0 이면 '한 틱 지났는가' 를 물을 수 없습니다."""
    m = RealisticFillModel()
    price, _ = m.fill_price(order(99.0, symbol=NOTICK), bar(symbol=NOTICK), None, 0.0)
    assert price == pytest.approx(99.0)


def test_a_stop_limit_gets_the_same_rule():
    """스톱이 걸린 봉의 극단이 마침 지정가면, 그건 우연입니다.

    매도 스톱은 저가가 스톱 이하로 내려가면 발동합니다. 발동한 뒤의 지정가가
    봉 고가와 같으면 — 봉이 거기서 돌아섰다는 뜻이니 — 체결로 보지 않습니다.
    """
    m = RealisticFillModel()
    fired = order(101.0, kind=OrderType.STOP_LIMIT, stop=99.5, side=OrderSide.SELL)
    assert m.fill_price(fired, bar(), None, 0.0) == (None, False)
    # 한 틱 안쪽이면 봉이 지나갔으므로 체결입니다.
    through = order(100.99, kind=OrderType.STOP_LIMIT, stop=99.5, side=OrderSide.SELL)
    price, _ = m.fill_price(through, bar(), None, 0.0)
    assert price == pytest.approx(100.99)


# ── 얼마나 받을 수 있는가 ────────────────────────────────────────────────
def test_a_limit_near_the_low_cannot_take_the_whole_bars_volume():
    """저가 근처의 매수가 봉 거래량의 10% 를 통째로 받는다는 가정을 버립니다.

    봉 안의 거래량이 고가–저가에 고르게 퍼져 있다고 보고, 지정가 **너머**에서
    오간 만큼만 셉니다. 거친 가정이지만 봉 전체를 쓰는 것보다 훨씬 덜 틀립니다.
    """
    m = RealisticFillModel()
    big = bar(v=1_000_000.0)
    near_low = m.max_fillable(order(99.5, qty="9999999"), big)
    mid = m.max_fillable(order(100.0, qty="9999999"), big)
    assert near_low < mid, "저가에 가까울수록 받을 수 있는 물량이 적어야 합니다"
    # 봉의 25% 구간이므로 전체 참여 한도(10만)의 4분의 1.
    assert near_low == Decimal("25000")


def test_a_limit_above_the_whole_bar_may_take_the_full_participation():
    """봉 전체가 내 지정가 아래에서 거래됐으면 다 받을 수 있는 게 맞습니다.

    이건 예외가 아니라 같은 규칙의 끝값입니다 — 구간 지분이 1.0 입니다.
    """
    m = RealisticFillModel()
    big = bar(v=1_000_000.0)
    assert m.max_fillable(order(101.0, qty="9999999"), big) == Decimal("100000")
    assert m.max_fillable(order(105.0, qty="9999999"), big) == Decimal("100000")


def test_the_sell_side_is_the_mirror_image():
    """매도 분기가 따로 있습니다 — 테스트가 매수만 덮으면 그쪽은 검사되지 않습니다."""
    m = RealisticFillModel()
    big = bar(v=1_000_000.0)
    near_high = m.max_fillable(order(100.5, OrderSide.SELL, "9999999"), big)
    mid = m.max_fillable(order(100.0, OrderSide.SELL, "9999999"), big)
    assert near_high < mid
    assert near_high == Decimal("25000")


def test_the_boundary_does_not_lose_a_lot_to_float_error():
    """(99.05 - 99.0) 이 0.04999999999999716 로 나와 lot 격자에서 한 단위를 잃었습니다."""
    m = RealisticFillModel()
    assert m.max_fillable(order(99.05), bar()) == Decimal("5")


def test_market_orders_still_use_the_whole_bar():
    """관통 추정은 지정가에만 적용됩니다 — 시장가는 어디서든 체결됩니다."""
    m = RealisticFillModel()
    o = Order(symbol=SYM, side=OrderSide.BUY, quantity=Decimal("9999999"),
              type=OrderType.MARKET, created_at=utcnow())
    assert m.max_fillable(o, bar(v=1_000_000.0)) == Decimal("100000")


def test_an_optimistic_model_stays_optimistic():
    """`limit_fill_requires_touch=False` 는 "다 채운다" 는 탈출구입니다.

    그 모드에서까지 구간 추정을 적용하면, 봉 범위 밖의 지정가가 지분 0 으로
    잘려 **1주만** 체결됩니다 — 낙관과 정반대이고, 그 플래그를 쓰던 쪽은
    이유도 모르고 물량을 잃습니다.
    """
    m = RealisticFillModel(limit_fill_requires_touch=False)
    big = bar(v=1_000_000.0)
    o = order(90.0)                      # 봉 저가보다 9 아래
    price, is_maker = m.fill_price(o, big, None, 0.0)
    assert price == pytest.approx(90.0) and is_maker
    assert m.max_fillable(o, big) == Decimal("1000"), "주문 전량을 받아야 합니다"


def test_a_separate_participation_rate_for_limits_is_honoured():
    m = RealisticFillModel(max_volume_participation=0.1,
                           limit_volume_participation=0.02)
    big = bar(v=1_000_000.0)
    assert m.max_fillable(order(101.0, qty="9999999"), big) == Decimal("20000")
