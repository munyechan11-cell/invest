"""Indicator correctness and the warm-up contract."""
import math

import pytest

from quant.core.types import UTC, Bar, Symbol
from quant.indicators.streaming import (
    ATR, EMA, MACD, RSI, SMA, BollingerBands, Donchian, IndicatorSet,
    RollingReturn, StdDev,
)
from datetime import datetime, timedelta

SYM = Symbol("T", venue="SIM")


def bars(closes, highs=None, lows=None):
    out = []
    for i, c in enumerate(closes):
        h = highs[i] if highs else c * 1.01
        l = lows[i] if lows else c * 0.99
        out.append(Bar(SYM, datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=i),
                       c, h, l, c, 1000.0, "1d"))
    return out


def test_sma_matches_manual_mean():
    sma = SMA(3)
    for v in (1, 2, 3, 4, 5):
        sma.update(v)
    assert sma.value == pytest.approx(4.0)      # mean of 3,4,5


def test_indicator_is_not_ready_before_warmup():
    sma = SMA(5)
    for v in (1, 2, 3, 4):
        assert sma.update(v) is None
    assert not sma.is_ready
    assert sma.update(5) == pytest.approx(3.0)
    assert sma.is_ready


def test_ema_converges_to_a_constant_series():
    ema = EMA(10)
    for _ in range(200):
        ema.update(50.0)
    assert ema.value == pytest.approx(50.0, abs=1e-9)


def test_rsi_is_100_when_only_gains_and_0_when_only_losses():
    up = RSI(14)
    for i in range(40):
        up.update(100 + i)
    assert up.value == pytest.approx(100.0)

    down = RSI(14)
    for i in range(40):
        down.update(100 - i)
    assert down.value == pytest.approx(0.0, abs=1e-9)


def test_rsi_hovers_near_50_for_a_symmetric_oscillation():
    """Exactly 50 is not expected: Wilder smoothing is phase-sensitive, so the
    final value depends on whether the last move was up or down."""
    rsi = RSI(14)
    for i in range(400):
        rsi.update(100 + (1 if i % 2 else -1))
    assert rsi.value == pytest.approx(50.0, abs=3.0)


def test_stddev_uses_sample_variance():
    sd = StdDev(5)
    for v in (2, 4, 4, 4, 5):
        sd.update(v)
    manual = math.sqrt(sum((x - 3.8) ** 2 for x in (2, 4, 4, 4, 5)) / 4)
    assert sd.value == pytest.approx(manual)


def test_atr_on_a_constant_range():
    atr = ATR(5)
    for b in bars([100.0] * 30, highs=[102.0] * 30, lows=[98.0] * 30):
        atr.update_bar(b)
    assert atr.value == pytest.approx(4.0, abs=0.2)


def test_donchian_channel_excludes_the_current_bar():
    """A bar cannot break out of a channel that includes its own high."""
    don = Donchian(5)
    for b in bars([10, 11, 12, 13, 14, 20]):
        don.update_bar(b)
    # the 20-close bar must be compared against the previous five bars only
    assert don.upper == pytest.approx(14 * 1.01)
    assert 20 > don.upper


def test_bollinger_percent_b_bounds():
    bb = BollingerBands(20, 2.0)
    for i in range(60):
        bb.update(100 + math.sin(i / 3) * 5)
    assert bb.value is not None
    assert bb.lower < bb.value < bb.upper
    assert 0.0 <= bb.percent_b <= 1.0


def test_macd_cross_flags_are_mutually_exclusive():
    macd = MACD(3, 6, 3)
    seen_up = seen_down = False
    for i in range(200):
        macd.update(100 + math.sin(i / 5) * 10)
        assert not (macd.crossed_up and macd.crossed_down)
        seen_up |= macd.crossed_up
        seen_down |= macd.crossed_down
    assert seen_up and seen_down


def test_rolling_return_matches_definition():
    roc = RollingReturn(4)
    for v in (100, 101, 102, 103, 110):
        roc.update(v)
    assert roc.value == pytest.approx(0.10)


def test_indicator_set_priming_equals_incremental_feeding():
    data = bars([100 + i * 0.7 for i in range(120)])
    primed = IndicatorSet(sma=SMA(20), rsi=RSI(14), atr=ATR(14)).prime(data)
    incremental = IndicatorSet(sma=SMA(20), rsi=RSI(14), atr=ATR(14))
    for b in data:
        incremental.update(b)
    for key in ("sma", "rsi", "atr"):
        assert primed[key].value == pytest.approx(incremental[key].value)
