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
from datetime import date, datetime
from decimal import Decimal

from quant.core.types import Bar, Order, OrderSide, OrderType, Quote, Symbol


# ─── fees ────────────────────────────────────────────────────────────────
class FeeModel(ABC):
    @abstractmethod
    def fee(self, symbol: Symbol, quantity: Decimal, price: float, is_maker: bool,
            when: datetime | date | None = None) -> float:
        """체결 하나에 붙는 비용.

        `when` 은 체결 시각입니다. 대부분의 모델은 무시하지만, 한국 거래세처럼
        **해마다 요율이 바뀌는** 비용이 있어서 받습니다. 여러 해에 걸친
        백테스트에서 한 요율로 눌러버리면 그 구간의 성과가 통째로 틀어집니다.
        """
        raise NotImplementedError


class PercentFeeModel(FeeModel):
    """Basis-point commission, the crypto-exchange norm."""

    def __init__(self, taker_bps: float = 10.0, maker_bps: float | None = None,
                 minimum: float = 0.0):
        self.taker = taker_bps / 10_000.0
        self.maker = (maker_bps if maker_bps is not None else taker_bps) / 10_000.0
        self.minimum = minimum

    def fee(self, symbol, quantity, price, is_maker, when=None):
        rate = self.maker if is_maker else self.taker
        return max(abs(float(quantity)) * price * float(symbol.multiplier) * rate, self.minimum)


class PerShareFeeModel(FeeModel):
    """US-equity style: cents per share with a floor and a percentage cap."""

    def __init__(self, per_share: float = 0.005, minimum: float = 1.0,
                 max_pct_of_notional: float = 0.005):
        self.per_share = per_share
        self.minimum = minimum
        self.cap_pct = max_pct_of_notional

    def fee(self, symbol, quantity, price, is_maker, when=None):
        qty = abs(float(quantity))
        raw = max(qty * self.per_share, self.minimum)
        return min(raw, qty * price * self.cap_pct) if self.cap_pct else raw


#: 증권거래세 + 농어촌특별세, 매도 대금 기준 bp. 해마다 바뀝니다.
#:
#: 한 해에 한 값이지 시장별로 다르지 않습니다 — KOSPI 는 증권거래세를 낮추고
#: 농특세를 얹어 KOSDAQ 과 총부담을 맞춰 왔습니다(2026: KOSPI 0.05+0.15,
#: KOSDAQ 0.20). 그래서 시장이 아니라 **연도**로 찾습니다.
#:
#: 2026년에 다시 올랐습니다. 내린 줄 알고 2024년 값을 쓰면 매도마다 2bp 씩
#: 실제보다 싸게 계산되고, 회전율이 높은 전략일수록 백테스트가 실제보다
#: 좋아 보입니다.
KRX_SELL_TAX_BPS: dict[int, float] = {
    2023: 20.0,
    2024: 18.0,
    2025: 15.0,
    2026: 20.0,
}
_LATEST_TAX_YEAR = max(KRX_SELL_TAX_BPS)


def krx_sell_tax_bps(when: datetime | date | None = None) -> float:
    """그 시점에 실제로 물린 세율. 모르는 미래는 마지막으로 아는 값."""
    year = _LATEST_TAX_YEAR if when is None else when.year
    if year in KRX_SELL_TAX_BPS:
        return KRX_SELL_TAX_BPS[year]
    # 표보다 이전이면 가장 오래된 값, 이후면 가장 최근 값. 없는 해를 0 으로
    # 두면 그 구간만 비용이 사라져 백테스트가 조용히 좋아집니다.
    return KRX_SELL_TAX_BPS[min(KRX_SELL_TAX_BPS) if year < min(KRX_SELL_TAX_BPS)
                            else _LATEST_TAX_YEAR]


class KoreanEquityFeeModel(FeeModel):
    """KRX 위탁수수료 — 양방향으로 같은 요율.

    매도 거래세는 여기 없습니다. 예전에는 `sell_tax_bps` 를 받아서 **저장만
    하고 쓰지 않았는데**, 세율을 넣은 사람은 그것이 부과된다고 믿을 수밖에
    없었습니다. 조용히 0원을 물리는 인자는 없느니만 못합니다. 거래세는
    `KoreanEquitySellTax` 를 `SideAwareFeeModel` 의 매도 쪽에 붙여 씁니다 —
    그래야 왕복 비용이 수수료 2배보다 크다는 사실이 구조에 드러납니다.
    """

    def __init__(self, commission_bps: float = 1.5):
        self.commission = commission_bps / 10_000.0

    def fee(self, symbol, quantity, price, is_maker, when=None):
        return abs(float(quantity)) * price * self.commission


class KoreanEquitySellTax(FeeModel):
    """매도에만 붙는 세금. 연도별 실제 세율을 씁니다.

    `sell_tax_bps` 를 명시하면 그 값으로 고정되고, 비워두면 체결 시점의
    연도에서 찾습니다 — 여러 해에 걸친 백테스트에서 한 값으로 눌러버리면
    세율이 달랐던 구간의 성과가 통째로 틀어집니다.
    """

    def __init__(self, sell_tax_bps: float | None = None):
        self.fixed = None if sell_tax_bps is None else sell_tax_bps / 10_000.0

    def rate_at(self, when: datetime | date | None = None) -> float:
        return self.fixed if self.fixed is not None else krx_sell_tax_bps(when) / 10_000.0

    def fee(self, symbol, quantity, price, is_maker, when=None):
        return abs(float(quantity)) * price * self.rate_at(when)


class CompositeFeeModel(FeeModel):
    def __init__(self, *models: FeeModel):
        self.models = models

    def fee(self, symbol, quantity, price, is_maker, when=None):
        return sum(m.fee(symbol, quantity, price, is_maker, when) for m in self.models)


class SideAwareFeeModel(FeeModel):
    """Applies an extra model on one side only (e.g. Korean sell tax)."""

    def __init__(self, base: FeeModel, sell_extra: FeeModel | None = None,
                 buy_extra: FeeModel | None = None):
        self.base, self.sell_extra, self.buy_extra = base, sell_extra, buy_extra
        self.side: OrderSide | None = None

    def for_side(self, side: OrderSide) -> FeeModel:
        extra = self.sell_extra if side is OrderSide.SELL else self.buy_extra
        return CompositeFeeModel(self.base, extra) if extra else self.base

    def fee(self, symbol, quantity, price, is_maker, when=None):
        return self.base.fee(symbol, quantity, price, is_maker, when)


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
