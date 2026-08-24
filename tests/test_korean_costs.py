"""한국 매도 거래세 — 해마다 바뀌는 값.

두 가지가 겹쳐 있었습니다. 세율이 2024년 값(18bp)으로 굳어 있었는데 2026년에
20bp 로 다시 올랐고, `KoreanEquityFeeModel(sell_tax_bps=...)` 은 그 값을
**저장만 하고 쓰지 않았습니다** — 세율을 지정한 사람은 부과된다고 믿을 수밖에
없었고, 실제로는 0원이었습니다.

둘 다 회전율이 높은 전략일수록 백테스트를 실제보다 좋아 보이게 만듭니다.
"""
from datetime import date, datetime, timezone

import pytest

from quant.core.types import OrderSide, Symbol
from quant.execution.costs import (
    KRX_SELL_TAX_BPS,
    KoreanEquityFeeModel,
    KoreanEquitySellTax,
    PRESETS,
    krx_sell_tax_bps,
)

SYM = Symbol("005930", venue="kis", quote_currency="KRW")
UTC = timezone.utc


@pytest.mark.parametrize("year,bps", [(2023, 20.0), (2024, 18.0),
                                      (2025, 15.0), (2026, 20.0)])
def test_the_rate_is_the_one_that_applied_that_year(year, bps):
    assert krx_sell_tax_bps(date(year, 6, 1)) == bps


def test_a_year_before_the_table_uses_the_oldest_known_rate():
    """모르는 해를 0 으로 두면 그 구간만 비용이 사라집니다."""
    assert krx_sell_tax_bps(date(2019, 6, 1)) == KRX_SELL_TAX_BPS[min(KRX_SELL_TAX_BPS)]


def test_a_future_year_uses_the_latest_known_rate():
    assert krx_sell_tax_bps(date(2099, 6, 1)) == KRX_SELL_TAX_BPS[max(KRX_SELL_TAX_BPS)]


def test_the_2026_rise_is_not_missed():
    """내린 줄 알고 옛 값을 쓰면 매도마다 2bp 씩 실제보다 싸집니다."""
    assert krx_sell_tax_bps(date(2026, 1, 1)) > krx_sell_tax_bps(date(2025, 12, 31))


# ── 실제로 부과되는가 ────────────────────────────────────────────────────
def test_the_sell_tax_is_charged_at_the_rate_of_the_fill():
    tax = KoreanEquitySellTax()
    ten_million = dict(symbol=SYM, quantity=100, price=100_000, is_maker=False)
    assert tax.fee(when=datetime(2026, 6, 1, tzinfo=UTC), **ten_million) == 20_000
    assert tax.fee(when=datetime(2025, 6, 1, tzinfo=UTC), **ten_million) == 15_000


def test_an_explicit_rate_overrides_the_year():
    """직접 지정하면 그 값으로 고정됩니다 — 다른 나라 계좌나 우대 요율용."""
    tax = KoreanEquitySellTax(sell_tax_bps=5.0)
    assert tax.fee(SYM, 100, 100_000, False, datetime(2026, 6, 1, tzinfo=UTC)) == 5_000


def test_the_commission_model_no_longer_accepts_a_tax_it_ignores():
    """조용히 0원을 물리는 인자는 없느니만 못합니다."""
    with pytest.raises(TypeError):
        KoreanEquityFeeModel(commission_bps=1.5, sell_tax_bps=18.0)


# ── 프리셋 전체 ─────────────────────────────────────────────────────────
def test_the_preset_charges_tax_on_sells_and_not_on_buys():
    fee, _ = PRESETS["kr_equity"]()
    at = datetime(2026, 6, 1, tzinfo=UTC)
    sell = fee.for_side(OrderSide.SELL).fee(SYM, 100, 100_000, False, at)
    buy = fee.for_side(OrderSide.BUY).fee(SYM, 100, 100_000, False, at)
    assert buy == pytest.approx(1_500)             # 수수료 1.5bp
    assert sell == pytest.approx(1_500 + 20_000)   # 수수료 + 거래세 20bp


def test_a_round_trip_costs_more_than_twice_the_commission():
    """이 비대칭이 회전율 높은 전략을 죽입니다 — 구조에 드러나야 합니다."""
    fee, _ = PRESETS["kr_equity"]()
    at = datetime(2026, 6, 1, tzinfo=UTC)
    buy = fee.for_side(OrderSide.BUY).fee(SYM, 100, 100_000, False, at)
    sell = fee.for_side(OrderSide.SELL).fee(SYM, 100, 100_000, False, at)
    assert buy + sell > 4 * buy


def test_a_multi_year_backtest_does_not_use_one_rate_throughout():
    fee, _ = PRESETS["kr_equity"]()
    side = fee.for_side(OrderSide.SELL)
    charged = {y: side.fee(SYM, 100, 100_000, False, datetime(y, 6, 1, tzinfo=UTC))
               for y in (2024, 2025, 2026)}
    assert len(set(charged.values())) == 3, charged
