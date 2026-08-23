"""체결 비용 모델.

A cost model that is too punitive is not "conservative" — it is wrong in the
direction that makes every strategy look unprofitable, which is exactly as
misleading as a backtest that fills for free. These tests pin the magnitudes
against what real execution costs, in both directions.
"""
import math
from datetime import datetime
from decimal import Decimal

import pytest

from quant.core.types import UTC, Bar, Order, OrderSide, Symbol
from quant.execution.costs import (
    KoreanEquityFeeModel, PercentFeeModel, PerShareFeeModel, SpreadPlusImpactSlippage,
)

SYM = Symbol("AAA", venue="SIM")
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def bar(rng_pct=0.02, volume=1_000_000.0, close=100.0):
    half = close * rng_pct / 2
    return Bar(SYM, T0, close, close + half, close - half, close, volume, "1d")


def bps(model, qty, b=None, quote=None):
    order = Order(SYM, OrderSide.BUY, Decimal(str(qty)))
    return model.slippage(order, b or bar(), quote) * 10_000


# ── magnitudes ───────────────────────────────────────────────────────────
def test_a_tiny_order_costs_about_half_the_spread():
    """100 shares in a million-share tape has essentially no market impact.
    The regression: this used to be charged 41bp one-way."""
    model = SpreadPlusImpactSlippage(base_spread_bps=3)
    assert bps(model, 100) < 5.0


def test_impact_grows_with_the_square_root_of_participation():
    model = SpreadPlusImpactSlippage(base_spread_bps=0)
    small = bps(model, 1_000)          # 0.1% of volume
    big = bps(model, 100_000)          # 10%   of volume
    # 100x the size should cost ~10x the impact, not 100x and not 1x
    assert 8.0 < big / small < 12.0


def test_impact_scales_with_volatility():
    """Moving 1% of the tape in a placid large cap costs a fraction of what it
    costs in something that swings 8% a day. An impact term that ignores this
    is not conservative, it is simply wrong."""
    model = SpreadPlusImpactSlippage(base_spread_bps=0)
    calm = bps(model, 10_000, bar(rng_pct=0.01))
    wild = bps(model, 10_000, bar(rng_pct=0.08))
    assert wild > calm * 4


def test_a_large_order_is_expensive_but_not_absurd():
    model = SpreadPlusImpactSlippage(base_spread_bps=3)
    round_trip = bps(model, 100_000) * 2       # 10% of the day's volume
    assert 40 < round_trip < 200


def test_a_live_quote_overrides_the_assumed_spread():
    from quant.core.types import Quote

    model = SpreadPlusImpactSlippage(base_spread_bps=3)
    wide = Quote(SYM, T0, bid=99.0, ask=101.0)          # 2% spread
    assert bps(model, 100, quote=wide) > 90             # ~half of 200bp


def test_no_double_counting_of_volatility_by_default():
    """sigma already scales the impact term; a separate volatility charge on
    top would count it twice."""
    assert SpreadPlusImpactSlippage().vol_coef == 0.0


def test_a_zero_range_bar_leaves_only_the_spread():
    model = SpreadPlusImpactSlippage(base_spread_bps=4)
    flat = Bar(SYM, T0, 100, 100, 100, 100, 1e6, "1d")
    assert bps(model, 1_000, flat) == pytest.approx(2.0, abs=0.01)


def test_slippage_is_never_negative():
    model = SpreadPlusImpactSlippage(base_spread_bps=0)
    assert bps(model, 0, Bar(SYM, T0, 100, 100, 100, 100, 0, "1d")) >= 0


# ── fees ─────────────────────────────────────────────────────────────────
def test_percent_fee_is_on_notional():
    fee = PercentFeeModel(taker_bps=10)
    assert fee.fee(SYM, Decimal("100"), 50.0, is_maker=False) == pytest.approx(5.0)


def test_maker_and_taker_differ():
    fee = PercentFeeModel(taker_bps=10, maker_bps=2)
    taker = fee.fee(SYM, Decimal("100"), 50.0, is_maker=False)
    maker = fee.fee(SYM, Decimal("100"), 50.0, is_maker=True)
    assert maker == pytest.approx(taker / 5)


def test_per_share_fee_respects_its_floor_and_cap():
    fee = PerShareFeeModel(per_share=0.005, minimum=1.0, max_pct_of_notional=0.005)
    assert fee.fee(SYM, Decimal("10"), 100.0, False) == pytest.approx(1.0)      # floor
    # cap: 10000 shares at $1 -> 0.5% of $10,000 = $50, below the $50 per-share cost
    assert fee.fee(SYM, Decimal("10000"), 1.0, False) == pytest.approx(50.0)


def test_korean_commission_is_charged_on_notional():
    fee = KoreanEquityFeeModel(commission_bps=1.5)
    assert fee.fee(SYM, Decimal("10"), 70_000.0, False) == pytest.approx(105.0)


def test_korean_sell_tax_only_applies_on_the_sell_side():
    """The asymmetry matters: a round trip costs far more than twice the
    commission, which quietly kills high-turnover Korean equity strategies."""
    from quant.execution.costs import KoreanEquitySellTax, SideAwareFeeModel

    model = SideAwareFeeModel(KoreanEquityFeeModel(commission_bps=1.5),
                              sell_extra=KoreanEquitySellTax(sell_tax_bps=18))
    notional = Decimal("10"), 70_000.0
    buy = model.for_side(OrderSide.BUY).fee(SYM, *notional, False)
    sell = model.for_side(OrderSide.SELL).fee(SYM, *notional, False)
    assert sell > buy * 10
    assert sell - buy == pytest.approx(700_000 * 0.0018)
