"""Transaction cost models: fees, slippage, and fill simulation.

The single biggest reason backtests lie is that they fill at the close, for
free, in unlimited size. These models exist so a simulated fill costs roughly
what a real one does — spread, commission, market impact, and the possibility
that a limit order simply does not fill.

Defaults are deliberately pessimistic. A strategy that only works with
optimistic costs does not work.
"""
from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod
from decimal import Decimal

from quant.core.types import Bar, Order, OrderSide, OrderType, Quote, Symbol


# ─── fees ────────────────────────────────────────────────────────────────
class FeeModel(ABC):
    @abstractmethod
    def fee(self, symbol: Symbol, quantity: Decimal, price: float, is_maker: bool) -> float: ...


class PercentFeeModel(FeeModel):
    """Basis-point commission, the crypto-exchange norm."""

    def __init__(self, taker_bps: float = 10.0, maker_bps: float | None = None,
                 minimum: float = 0.0):
        self.taker = taker_bps / 10_000.0
        self.maker = (maker_bps if maker_bps is not None else taker_bps) / 10_000.0
        self.minimum = minimum

    def fee(self, symbol, quantity, price, is_maker):
        rate = self.maker if is_maker else self.taker
        return max(abs(float(quantity)) * price * float(symbol.multiplier) * rate, self.minimum)


class PerShareFeeModel(FeeModel):
    """US-equity style: cents per share with a floor and a percentage cap."""

    def __init__(self, per_share: float = 0.005, minimum: float = 1.0,
                 max_pct_of_notional: float = 0.005):
        self.per_share = per_share
        self.minimum = minimum
        self.cap_pct = max_pct_of_notional

    def fee(self, symbol, quantity, price, is_maker):
        qty = abs(float(quantity))
        raw = max(qty * self.per_share, self.minimum)
        return min(raw, qty * price * self.cap_pct) if self.cap_pct else raw


class KoreanEquityFeeModel(FeeModel):
    """KRX: brokerage commission both ways plus a sell-side transaction tax.

    The asymmetry matters — a round trip costs materially more than 2× the
    commission, which quietly kills high-turnover strategies on Korean equities.
    """

    def __init__(self, commission_bps: float = 1.5, sell_tax_bps: float = 18.0):
        self.commission = commission_bps / 10_000.0
        self.sell_tax = sell_tax_bps / 10_000.0

    def fee(self, symbol, quantity, price, is_maker):
        notional = abs(float(quantity)) * price
        return notional * self.commission


class KoreanEquitySellTax(FeeModel):
    def __init__(self, sell_tax_bps: float = 18.0):
        self.rate = sell_tax_bps / 10_000.0

    def fee(self, symbol, quantity, price, is_maker):
        return abs(float(quantity)) * price * self.rate


class CompositeFeeModel(FeeModel):
    def __init__(self, *models: FeeModel):
        self.models = models

    def fee(self, symbol, quantity, price, is_maker):
        return sum(m.fee(symbol, quantity, price, is_maker) for m in self.models)


class SideAwareFeeModel(FeeModel):
    """Applies an extra model on one side only (e.g. Korean sell tax)."""

    def __init__(self, base: FeeModel, sell_extra: FeeModel | None = None,
                 buy_extra: FeeModel | None = None):
        self.base, self.sell_extra, self.buy_extra = base, sell_extra, buy_extra
        self.side: OrderSide | None = None

    def for_side(self, side: OrderSide) -> FeeModel:
        extra = self.sell_extra if side is OrderSide.SELL else self.buy_extra
        return CompositeFeeModel(self.base, extra) if extra else self.base

    def fee(self, symbol, quantity, price, is_maker):
        return self.base.fee(symbol, quantity, price, is_maker)


# ─── slippage ────────────────────────────────────────────────────────────
class SlippageModel(ABC):
    @abstractmethod
    def slippage(self, order: Order, bar: Bar, quote: Quote | None) -> float:
        """Fractional adverse price move, always >= 0."""


class SpreadPlusImpactSlippage(SlippageModel):
    """Half-spread plus square-root market impact.

        slippage = spread/2 + eta * sigma * sqrt(Q / V)

    This is the Almgren-style law, and the two terms it *does not* have matter
    as much as the two it does.

    `sigma` is the bar's own volatility, estimated from its range with the
    Parkinson estimator (sigma = ln(H/L) / (2*sqrt(ln 2))). Impact has to scale
    with volatility: moving 1% of the daily volume in a placid large cap costs a
    fraction of what it costs in something that swings 8% a day. An impact term
    that ignores sigma is not conservative, it is simply wrong — and it is wrong
    in the direction that makes every strategy look unprofitable, which is just
    as misleading as a backtest that fills for free.

    `eta` defaults to 1.0, at the pessimistic end of the 0.3–1.0 range the
    empirical literature reports, so the default still errs toward caution.

    There is deliberately no separate "volatility charge" on top: that would
    double-count sigma, which already scales the impact term.
    """

    def __init__(self, base_spread_bps: float = 5.0, impact_coefficient: float = 1.0,
                 volatility_coefficient: float = 0.0, random_component: bool = False,
                 seed: int = 0, min_bps: float = 0.0):
        self.base_spread = base_spread_bps / 10_000.0
        self.impact = impact_coefficient
        #: retained so older configs still load; adds a flat charge proportional
        #: to the bar range. Leave at 0 — sigma is already in the impact term.
        self.vol_coef = volatility_coefficient
        self.random = random_component
        self.min_bps = min_bps / 10_000.0
        self._rng = random.Random(seed)

    @staticmethod
    def _parkinson_sigma(bar: Bar) -> float:
        """Per-bar volatility from the high-low range.

        Uses the range rather than close-to-close because a single bar gives
        only one close-to-close observation, and that estimator is far noisier.
        """
        if bar.high <= 0 or bar.low <= 0 or bar.high <= bar.low:
            return 0.0
        return math.log(bar.high / bar.low) / (2.0 * math.sqrt(math.log(2.0)))

    def slippage(self, order, bar, quote):
        half_spread = (
            quote.spread_pct / 2.0 if quote and math.isfinite(quote.spread_pct)
            else self.base_spread / 2.0
        )
        sigma = self._parkinson_sigma(bar)
        adv = max(bar.volume, 1.0)
        participation = min(abs(float(order.quantity)) / adv, 1.0)
        impact = self.impact * sigma * math.sqrt(participation)

        total = half_spread + impact
        if self.vol_coef:
            total += self.vol_coef * ((bar.range / bar.close) if bar.close > 0 else 0.0)
        if self.random:
            total *= max(0.0, self._rng.gauss(1.0, 0.3))
        return max(total, self.min_bps)


class FixedSlippage(SlippageModel):
    def __init__(self, bps: float = 5.0):
        self.rate = bps / 10_000.0

    def slippage(self, order, bar, quote):
        return self.rate


class NoSlippage(SlippageModel):
    """For unit tests only. Using this in a real backtest is self-deception."""

    def slippage(self, order, bar, quote):
        return 0.0


# ─── fills ───────────────────────────────────────────────────────────────
class FillModel(ABC):
    @abstractmethod
    def fill_price(self, order: Order, bar: Bar, quote: Quote | None,
                   slippage: float) -> tuple[float | None, bool]:
        """Return (price, is_maker). `None` price means the order did not fill."""

    def max_fillable(self, order: Order, bar: Bar) -> Decimal:
        """Cap a fill at a realistic share of the bar's volume."""
        return order.remaining


class RealisticFillModel(FillModel):
    """Fills against the *next* bar, respecting its high/low range.

    Two rules do most of the work:
      * a market order fills at the next open plus slippage — never at the close
        of the bar that generated the signal, which is the #1 backtest fiction;
      * a limit order only fills if the bar actually traded through its price.
    """

    def __init__(self, max_volume_participation: float = 0.1,
                 limit_fill_requires_touch: bool = True):
        self.participation = max_volume_participation
        self.requires_touch = limit_fill_requires_touch

    def max_fillable(self, order, bar):
        if self.participation <= 0 or bar.volume <= 0:
            return order.remaining
        cap = Decimal(str(bar.volume * self.participation))
        return min(order.remaining, order.symbol.round_qty(cap)) or order.symbol.lot_size

    def fill_price(self, order, bar, quote, slippage):
        sign = order.side.sign
        if order.type is OrderType.MARKET:
            price = bar.open * (1.0 + sign * slippage)
            return min(max(price, bar.low), bar.high), False

        if order.type is OrderType.LIMIT:
            limit = order.limit_price or bar.open
            if self.requires_touch:
                touched = bar.low <= limit if order.side is OrderSide.BUY else bar.high >= limit
                if not touched:
                    return None, False
            # A resting limit that gets hit fills at its own price, as a maker.
            price = min(limit, bar.open) if order.side is OrderSide.BUY else max(limit, bar.open)
            return price, True

        if order.type in (OrderType.STOP, OrderType.STOP_LIMIT):
            stop = order.stop_price or bar.open
            triggered = bar.high >= stop if order.side is OrderSide.BUY else bar.low <= stop
            if not triggered:
                return None, False
            if order.type is OrderType.STOP:
                # gapping through a stop fills at the open, not the stop price
                base = max(stop, bar.open) if order.side is OrderSide.BUY else min(stop, bar.open)
                return base * (1.0 + sign * slippage), False
            limit = order.limit_price or stop
            filled = bar.low <= limit if order.side is OrderSide.BUY else bar.high >= limit
            return (limit, True) if filled else (None, False)

        return None, False


class ImmediateFillModel(FillModel):
    """Fills everything at the current close plus slippage. Optimistic."""

    def fill_price(self, order, bar, quote, slippage):
        return bar.close * (1.0 + order.side.sign * slippage), False


PRESETS = {
    "crypto_spot": lambda: (PercentFeeModel(taker_bps=10, maker_bps=2),
                            SpreadPlusImpactSlippage(base_spread_bps=6)),
    "us_equity": lambda: (PerShareFeeModel(),
                          SpreadPlusImpactSlippage(base_spread_bps=3)),
    "kr_equity": lambda: (SideAwareFeeModel(KoreanEquityFeeModel(),
                                            sell_extra=KoreanEquitySellTax()),
                          SpreadPlusImpactSlippage(base_spread_bps=8)),
    "zero_cost": lambda: (PercentFeeModel(taker_bps=0), NoSlippage()),
}
