"""Scale-outs are exits, and the trade ledger has to say so.

A ClosedTrade used to be written only when a position crossed zero, so every
trim in between was settled in cash and then dropped on the floor. The book was
right and the trade list was wrong, which is the worst combination: profit
factor, expectancy, the protections that count recent losers, and the 실현손익 /
거래세 totals a Korean filing needs are all derived from that list.

The invariant every test here defends: on a flat book,
``sum(t.pnl) == equity - starting_cash`` and ``sum(t.fees) == total_fees``.
"""
import random
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.core.account import Portfolio
from quant.core.types import UTC, Fill, OrderSide, Symbol

SYM = Symbol("TEST", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))
KRX = Symbol("005930", venue="KRX", quote_currency="KRW",
             tick_size=Decimal("1"), lot_size=Decimal("1"))
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def fill(side, qty, price, fee=0.0, minutes=0, symbol=SYM, tag=""):
    return Fill("o1", symbol, side, Decimal(str(qty)), price, fee,
                T0 + timedelta(minutes=minutes), tag=tag)


def assert_ledger_foots(pf):
    """The two identities that must hold once nothing is left open."""
    assert not pf.open_positions, "book must be flat for this reconciliation"
    assert sum(t.pnl for t in pf.closed_trades) == pytest.approx(
        pf.equity - pf.starting_cash)
    assert sum(t.fees for t in pf.closed_trades) == pytest.approx(pf.total_fees)


# ── the reported failures ────────────────────────────────────────────────
def test_scaling_out_books_every_slice():
    """The crypto-swing repro: two sells, two trades, all 200 of the profit."""
    pf = Portfolio(10_000.0, "USDT")
    pf.apply_fill(fill(OrderSide.BUY, 10, 100.0))
    pf.apply_fill(fill(OrderSide.SELL, 5, 120.0, minutes=1))
    pf.apply_fill(fill(OrderSide.SELL, 5, 120.0, minutes=2))

    assert len(pf.closed_trades) == 2
    assert [float(t.quantity) for t in pf.closed_trades] == [5.0, 5.0]
    assert_ledger_foots(pf)
    assert sum(t.pnl for t in pf.closed_trades) == pytest.approx(200.0)


def test_sell_side_costs_all_reach_the_ledger():
    """거래세 is charged on every sell, so every sell must appear as a trade."""
    pf = Portfolio(1_000_000.0, "KRW")
    pf.apply_fill(fill(OrderSide.BUY, 100, 1_000.0, fee=150.0, symbol=KRX))
    trimmed = pf.apply_fill(fill(OrderSide.SELL, 50, 1_200.0, fee=200.0,
                                 minutes=1, symbol=KRX))
    assert trimmed is not None, "a partial sell is a closed trade, not a no-op"
    assert len(pf.closed_trades) == 1

    pf.apply_fill(fill(OrderSide.SELL, 50, 1_200.0, fee=200.0, minutes=2, symbol=KRX))
    assert len(pf.closed_trades) == 2
    assert_ledger_foots(pf)
    assert sum(t.pnl for t in pf.closed_trades) == pytest.approx(19_450.0)
    assert sum(t.fees for t in pf.closed_trades) == pytest.approx(550.0)


def test_a_trim_keeps_the_original_entry_context():
    """Rebasing on a trim would restart hold time and corrupt the next slice."""
    pf = Portfolio(10_000.0)
    pf.apply_fill(fill(OrderSide.BUY, 10, 100.0, minutes=0))
    pf.apply_fill(fill(OrderSide.SELL, 4, 110.0, minutes=60))
    pf.apply_fill(fill(OrderSide.SELL, 6, 120.0, minutes=180))

    first, second = pf.closed_trades
    assert first.entry_ts == T0 and second.entry_ts == T0
    assert first.duration == timedelta(minutes=60)
    assert second.duration == timedelta(minutes=180)
    # the surviving quantity keeps the basis it was bought at
    assert first.entry_price == pytest.approx(100.0)
    assert second.entry_price == pytest.approx(100.0)


def test_the_last_slice_does_not_rebook_the_earlier_ones():
    """Each record covers its own quantity — no slice is counted twice."""
    pf = Portfolio(10_000.0)
    pf.apply_fill(fill(OrderSide.BUY, 10, 100.0))
    for i, qty in enumerate((2, 3, 5), start=1):
        pf.apply_fill(fill(OrderSide.SELL, qty, 110.0, minutes=i))

    assert [float(t.quantity) for t in pf.closed_trades] == [2.0, 3.0, 5.0]
    assert sum(float(t.quantity) for t in pf.closed_trades) == 10.0
    assert_ledger_foots(pf)
    assert sum(t.pnl for t in pf.closed_trades) == pytest.approx(100.0)


# ── fee attribution ──────────────────────────────────────────────────────
def test_entry_fees_are_amortised_across_the_slices_they_bought():
    pf = Portfolio(100_000.0)
    pf.apply_fill(fill(OrderSide.BUY, 10, 100.0, fee=10.0))
    pf.apply_fill(fill(OrderSide.SELL, 5, 110.0, fee=5.0, minutes=1))

    trim = pf.closed_trades[0]
    assert trim.fees == pytest.approx(10.0)         # half the entry + this exit
    assert trim.pnl == pytest.approx(5 * 10 - 10.0)

    pf.apply_fill(fill(OrderSide.SELL, 5, 110.0, fee=5.0, minutes=2))
    assert pf.closed_trades[1].fees == pytest.approx(10.0)
    assert_ledger_foots(pf)


def test_scaling_in_then_out_still_foots():
    pf = Portfolio(100_000.0)
    pf.apply_fill(fill(OrderSide.BUY, 10, 100.0, fee=10.0))
    pf.apply_fill(fill(OrderSide.SELL, 4, 130.0, fee=6.0, minutes=1))
    pf.apply_fill(fill(OrderSide.BUY, 20, 200.0, fee=40.0, minutes=2))
    pf.apply_fill(fill(OrderSide.SELL, 10, 190.0, fee=20.0, minutes=3))
    pf.apply_fill(fill(OrderSide.SELL, 16, 210.0, fee=35.0, minutes=4))

    assert len(pf.closed_trades) == 3
    assert_ledger_foots(pf)


def test_opening_and_adding_book_nothing():
    pf = Portfolio(100_000.0)
    assert pf.apply_fill(fill(OrderSide.BUY, 10, 100.0, fee=10.0)) is None
    assert pf.apply_fill(fill(OrderSide.BUY, 10, 120.0, fee=10.0, minutes=1)) is None
    assert pf.closed_trades == []


# ── shorts, flips, multipliers ───────────────────────────────────────────
def test_covering_a_short_in_slices_is_booked_per_slice():
    pf = Portfolio(100_000.0)
    pf.apply_fill(fill(OrderSide.SELL, 10, 100.0, fee=10.0))
    pf.apply_fill(fill(OrderSide.BUY, 6, 90.0, fee=5.0, minutes=1))
    pf.apply_fill(fill(OrderSide.BUY, 4, 95.0, fee=4.0, minutes=2))

    assert [t.side for t in pf.closed_trades] == [OrderSide.SELL, OrderSide.SELL]
    assert pf.closed_trades[0].pnl == pytest.approx(6 * 10 - (5.0 + 6.0))
    assert_ledger_foots(pf)


def test_a_flip_books_only_the_closed_side_and_carries_the_rest_forward():
    """One fill, two jobs: it closes the long and opens the short. The fee
    splits with it, or the short's entry cost disappears from the ledger."""
    pf = Portfolio(100_000.0)
    pf.apply_fill(fill(OrderSide.BUY, 10, 100.0))
    pf.apply_fill(fill(OrderSide.SELL, 15, 110.0, fee=15.0, minutes=1))

    flip = pf.closed_trades[0]
    assert float(flip.quantity) == 10.0
    assert flip.fees == pytest.approx(10.0)         # 10 of the 15 units closed
    assert flip.pnl == pytest.approx(90.0)
    assert pf.quantity(SYM) == Decimal("-5")

    cover = pf.apply_fill(fill(OrderSide.BUY, 5, 90.0, fee=5.0, minutes=2))
    assert cover.entry_price == pytest.approx(110.0)   # rebased on the flip
    assert cover.entry_ts == T0 + timedelta(minutes=1)
    assert cover.fees == pytest.approx(10.0)           # the short's 5 + this 5
    assert_ledger_foots(pf)


def test_multiplier_scales_a_partial_exit():
    fut = Symbol("ESZ4", venue="SIM", multiplier=Decimal("50"), lot_size=Decimal("1"))
    pf = Portfolio(1_000_000.0)
    pf.apply_fill(fill(OrderSide.BUY, 4, 5_000.0, symbol=fut))
    trim = pf.apply_fill(fill(OrderSide.SELL, 1, 5_010.0, minutes=1, symbol=fut))
    assert trim.pnl == pytest.approx(1 * 10 * 50)
    pf.apply_fill(fill(OrderSide.SELL, 3, 5_010.0, minutes=2, symbol=fut))
    assert_ledger_foots(pf)


def test_a_trim_is_measured_against_its_own_basis():
    """pnl_pct feeds LowProfitPairs and expectancy; a slice's percentage is the
    slice's, not the whole position's."""
    pf = Portfolio(100_000.0)
    pf.apply_fill(fill(OrderSide.BUY, 10, 100.0))
    pf.apply_fill(fill(OrderSide.SELL, 5, 110.0, minutes=1))
    pf.apply_fill(fill(OrderSide.SELL, 5, 80.0, minutes=2))

    assert pf.closed_trades[0].pnl_pct == pytest.approx(0.10)
    assert pf.closed_trades[1].pnl_pct == pytest.approx(-0.20)
    assert pf.closed_trades[0].is_win and not pf.closed_trades[1].is_win


def test_a_trims_exit_tag_survives_into_the_ledger():
    """Protections count stop-outs; a partial stop is still a stop."""
    pf = Portfolio(100_000.0)
    pf.apply_fill(fill(OrderSide.BUY, 10, 100.0))
    trim = pf.apply_fill(fill(OrderSide.SELL, 5, 90.0, minutes=1,
                              tag="stop_loss: -10%"))
    assert trim.exit_reason == "stop_loss"
    assert trim.was_stopped_out


# ── the whole thing, on messy input ──────────────────────────────────────
def test_ledger_foots_over_a_long_random_fill_sequence():
    """Scale-ins, scale-outs, flips and shorts interleaved across symbols."""
    rng = random.Random(20240101)
    symbols = [SYM, KRX,
               Symbol("BTC/USDT", venue="BIN", lot_size=Decimal("0.001"))]
    pf = Portfolio(10_000_000.0)
    reducing_fills = 0
    minute = 0

    for _ in range(300):
        minute += 1
        sym = rng.choice(symbols)
        side = rng.choice((OrderSide.BUY, OrderSide.SELL))
        qty = Decimal(str(rng.randint(1, 12)))
        price = round(rng.uniform(50.0, 150.0), 2)
        prior = pf.quantity(sym)
        if prior != 0 and side.sign != (1 if prior > 0 else -1):
            reducing_fills += 1
        pf.apply_fill(Fill("o", sym, side, qty, price, round(qty * 3) / 100,
                           T0 + timedelta(minutes=minute)))

    for pos in list(pf.open_positions):
        minute += 1
        qty = abs(pos.quantity)
        side = OrderSide.SELL if pos.quantity > 0 else OrderSide.BUY
        reducing_fills += 1
        pf.apply_fill(Fill("o", pos.symbol, side, qty, round(rng.uniform(50, 150), 2),
                           round(qty * 3) / 100, T0 + timedelta(minutes=minute)))

    assert len(pf.closed_trades) == reducing_fills
    assert reducing_fills > 100, "sequence should exercise plenty of exits"
    assert_ledger_foots(pf)
