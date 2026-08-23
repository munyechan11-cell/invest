"""Core value types shared by the whole engine.

Design notes
------------
LEAN's insight is that a *backtest* and a *live session* should differ only in
which clock and which brokerage are plugged in — every type below is therefore
timezone-aware, immutable where practical, and carries no notion of "simulated
vs real".  Freqtrade's insight is that an order needs an operational paper
trail (why it was placed, what protection allowed it) — hence the `tag` and
`meta` fields that ride along from signal to fill.

All money amounts are `Decimal` at the boundary with a brokerage and `float`
inside the numeric core.  Quantities are `Decimal` everywhere because crypto
lot sizes are fractional and float rounding silently breaks exchange filters.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────
class AssetClass(str, Enum):
    EQUITY = "equity"
    CRYPTO = "crypto"
    FUTURE = "future"
    FOREX = "forex"
    ETF = "etf"


class Direction(int, Enum):
    """Insight direction. Integer-valued so it can be used arithmetically."""

    DOWN = -1
    FLAT = 0
    UP = 1


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is OrderSide.BUY else -1


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(str, Enum):
    GTC = "gtc"
    DAY = "day"
    IOC = "ioc"
    FOK = "fok"


class OrderStatus(str, Enum):
    NEW = "new"
    SUBMITTED = "submitted"
    PARTIAL = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def is_open(self) -> bool:
        return self in (OrderStatus.NEW, OrderStatus.SUBMITTED, OrderStatus.PARTIAL)

    @property
    def is_done(self) -> bool:
        return not self.is_open


class RunMode(str, Enum):
    BACKTEST = "backtest"
    DRY_RUN = "dry_run"      # live data, simulated fills — freqtrade's dry-run
    LIVE = "live"            # real money


# ─────────────────────────────────────────────────────────────────────────────
# Instruments
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Symbol:
    """A tradable instrument, unique across venues.

    `ticker` is the venue-native string ("005930", "BTC/USDT", "AAPL");
    `venue` disambiguates the same ticker on different exchanges.
    """

    ticker: str
    venue: str = "SIM"
    asset_class: AssetClass = AssetClass.EQUITY
    quote_currency: str = "USD"
    lot_size: Decimal = Decimal("1")          # min tradable increment
    tick_size: Decimal = Decimal("0.01")      # min price increment
    min_notional: Decimal = Decimal("0")
    multiplier: Decimal = Decimal("1")        # contract multiplier (futures)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.ticker}@{self.venue}"

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.ticker}"

    @property
    def is_fractional(self) -> bool:
        return self.lot_size < Decimal("1")

    def round_qty(self, qty: Decimal | float) -> Decimal:
        """Floor a quantity onto the venue's lot grid. Never rounds up —
        rounding up is how you get 'insufficient balance' rejections."""
        q = Decimal(str(qty))
        if self.lot_size <= 0:
            return q
        steps = (q.copy_abs() / self.lot_size).to_integral_value(rounding="ROUND_FLOOR")
        return (steps * self.lot_size).copy_sign(q)

    def round_price(self, price: Decimal | float, side: OrderSide | None = None) -> Decimal:
        """Snap a price onto the tick grid, biased *away* from crossing the book
        when a side is given (buy rounds down, sell rounds up)."""
        p = Decimal(str(price))
        if self.tick_size <= 0:
            return p
        mode = "ROUND_HALF_EVEN"
        if side is OrderSide.BUY:
            mode = "ROUND_FLOOR"
        elif side is OrderSide.SELL:
            mode = "ROUND_CEILING"
        return (p / self.tick_size).to_integral_value(rounding=mode) * self.tick_size


# ─────────────────────────────────────────────────────────────────────────────
# Market data
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Bar:
    """One OHLCV candle. `ts` is the candle's *open* time, always tz-aware UTC."""

    symbol: Symbol
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: str = "1d"

    @property
    def end_ts(self) -> datetime:
        return self.ts + timeframe_delta(self.timeframe)

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    @property
    def range(self) -> float:
        return self.high - self.low


@dataclass(frozen=True)
class Quote:
    """Top-of-book snapshot. Used for realistic fill pricing and spread checks."""

    symbol: Symbol
    ts: datetime
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_pct(self) -> float:
        m = self.mid
        return (self.ask - self.bid) / m if m > 0 else math.inf


# ─────────────────────────────────────────────────────────────────────────────
# Alpha layer — Insight (LEAN's currency of prediction)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Insight:
    """A time-bounded, decaying prediction about one symbol.

    Separating *prediction* (Insight) from *allocation* (PortfolioTarget) is the
    single most valuable idea borrowed from LEAN: it lets several unrelated
    alpha models — a momentum rule, a mean-reversion rule, an LLM research
    council — vote on the same universe without any of them knowing the
    portfolio's size, leverage, or existing positions.
    """

    symbol: Symbol
    direction: Direction
    period: timedelta
    generated_at: datetime
    magnitude: float | None = None      # expected fractional return over `period`
    confidence: float = 0.5             # 0..1
    weight: float | None = None         # optional explicit portfolio weight hint
    source: str = "unknown"             # which alpha model emitted it
    tag: str = ""                       # human-readable reason
    meta: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("ins_"))

    def __post_init__(self) -> None:
        self.confidence = float(min(max(self.confidence, 0.0), 1.0))
        if self.generated_at.tzinfo is None:
            self.generated_at = self.generated_at.replace(tzinfo=UTC)

    @property
    def close_time(self) -> datetime:
        return self.generated_at + self.period

    def is_active(self, now: datetime) -> bool:
        return now < self.close_time

    def decayed_confidence(self, now: datetime, half_life_frac: float = 0.5) -> float:
        """Exponential decay so a stale insight stops dominating the book.

        `half_life_frac` is expressed as a fraction of the insight's period, so
        a 5-day insight with 0.5 has a 2.5-day half life.
        """
        if not self.is_active(now):
            return 0.0
        elapsed = (now - self.generated_at).total_seconds()
        total = max(self.period.total_seconds(), 1.0)
        half_life = max(total * half_life_frac, 1.0)
        return self.confidence * (0.5 ** (elapsed / half_life))

    @property
    def score(self) -> float:
        """Signed conviction: direction × confidence × (1 + |magnitude|)."""
        mag = abs(self.magnitude) if self.magnitude is not None else 0.0
        return int(self.direction) * self.confidence * (1.0 + mag)

    def with_source(self, source: str) -> "Insight":
        return replace(self, source=source, id=new_id("ins_"))


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio layer
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PortfolioTarget:
    """Desired *end state* for a symbol, expressed in signed quantity.

    Targets are absolute, not deltas. The execution layer diffs them against the
    live position, which makes the whole pipeline idempotent — replaying the
    same targets twice never doubles a position.
    """

    symbol: Symbol
    quantity: Decimal
    tag: str = ""
    source: str = ""

    @staticmethod
    def from_weight(
        symbol: Symbol, weight: float, portfolio_value: float, price: float
    ) -> "PortfolioTarget":
        if price <= 0 or portfolio_value <= 0:
            return PortfolioTarget(symbol, Decimal("0"), tag="invalid price/value")
        raw = Decimal(str(weight * portfolio_value / price))
        return PortfolioTarget(symbol, symbol.round_qty(raw), tag=f"w={weight:.4f}")


# ─────────────────────────────────────────────────────────────────────────────
# Orders & fills
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Order:
    symbol: Symbol
    side: OrderSide
    quantity: Decimal
    type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None
    tif: TimeInForce = TimeInForce.GTC
    reduce_only: bool = False
    tag: str = ""
    source: str = ""
    id: str = field(default_factory=lambda: new_id("ord_"))
    broker_id: str | None = None
    status: OrderStatus = OrderStatus.NEW
    filled_qty: Decimal = Decimal("0")
    avg_fill_price: float = 0.0
    fees: float = 0.0
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    reject_reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def remaining(self) -> Decimal:
        return max(self.quantity - self.filled_qty, Decimal("0"))

    @property
    def signed_filled(self) -> Decimal:
        return self.filled_qty * self.side.sign

    def apply_fill(self, fill: "Fill") -> None:
        prev_notional = float(self.filled_qty) * self.avg_fill_price
        self.filled_qty += fill.quantity
        new_notional = prev_notional + float(fill.quantity) * fill.price
        self.avg_fill_price = new_notional / float(self.filled_qty) if self.filled_qty else 0.0
        self.fees += fill.fee
        self.status = OrderStatus.FILLED if self.remaining <= 0 else OrderStatus.PARTIAL
        self.updated_at = fill.ts


@dataclass(frozen=True)
class Fill:
    order_id: str
    symbol: Symbol
    side: OrderSide
    quantity: Decimal
    price: float
    fee: float
    ts: datetime
    liquidity: str = "taker"
    slippage: float = 0.0
    #: why this order was sent, carried from the target that produced it. Not
    #: cosmetic: protections key off it to tell a stop-out from a take-profit.
    tag: str = ""
    id: str = field(default_factory=lambda: new_id("fil_"))

    @property
    def notional(self) -> float:
        return float(self.quantity) * self.price


# ─────────────────────────────────────────────────────────────────────────────
# Positions & portfolio state
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Position:
    symbol: Symbol
    quantity: Decimal = Decimal("0")        # signed: negative == short
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    last_price: float = 0.0
    opened_at: datetime | None = None
    peak_price: float = 0.0                 # for trailing stops
    trough_price: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def direction(self) -> int:
        return (self.quantity > 0) - (self.quantity < 0)

    @property
    def market_value(self) -> float:
        return float(self.quantity) * self.last_price * float(self.symbol.multiplier)

    @property
    def cost_basis(self) -> float:
        return float(self.quantity) * self.avg_price * float(self.symbol.multiplier)

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_basis

    @property
    def unrealized_pct(self) -> float:
        cb = abs(self.cost_basis)
        return self.unrealized_pnl / cb if cb > 0 else 0.0

    def mark(self, price: float) -> None:
        if price <= 0:
            return
        self.last_price = price
        if self.is_flat:
            self.peak_price = self.trough_price = 0.0
            return
        self.peak_price = price if self.peak_price == 0 else max(self.peak_price, price)
        self.trough_price = price if self.trough_price == 0 else min(self.trough_price, price)

    def apply(self, fill: Fill) -> float:
        """Fold a fill into the position; returns realized PnL from this fill."""
        signed = fill.quantity * fill.side.sign
        mult = float(self.symbol.multiplier)
        realized = 0.0

        if self.is_flat or (self.direction == (1 if signed > 0 else -1)):
            # opening or adding — weighted-average the cost basis
            total = self.quantity + signed
            if total != 0:
                self.avg_price = (
                    float(self.quantity) * self.avg_price + float(signed) * fill.price
                ) / float(total)
            self.quantity = total
            if self.opened_at is None:
                self.opened_at = fill.ts
        else:
            # reducing / closing / flipping
            closing = min(abs(signed), abs(self.quantity))
            realized = float(closing) * (fill.price - self.avg_price) * self.direction * mult
            self.realized_pnl += realized
            remainder = abs(signed) - closing
            self.quantity += signed
            if self.quantity == 0:
                self.avg_price = 0.0
                self.opened_at = None
                self.peak_price = self.trough_price = 0.0
            elif remainder > 0:
                # flipped through zero — new basis is the fill price
                self.avg_price = fill.price
                self.opened_at = fill.ts
                self.peak_price = self.trough_price = fill.price

        self.fees_paid += fill.fee
        self.realized_pnl -= fill.fee
        self.mark(fill.price)
        return realized - fill.fee


#: exit reasons that count as "the risk layer forced us out"
EXIT_STOP_REASONS = frozenset({
    "stop_loss", "trailing_stop", "max_dd_portfolio", "time_stop", "risk_veto",
})


@dataclass
class ClosedTrade:
    """A completed round trip, recorded for analytics and protections."""

    symbol: Symbol
    side: OrderSide
    quantity: Decimal
    entry_price: float
    exit_price: float
    entry_ts: datetime
    exit_ts: datetime
    pnl: float
    pnl_pct: float
    fees: float
    entry_tag: str = ""
    exit_tag: str = ""
    #: Did this take the position flat, or only trim it?
    #: Scaling out realises PnL and belongs in the trade log and the tax
    #: totals, but it is not an exit — and protections that ask "did we just
    #: leave this name" have to be able to tell the difference, or a rebalance
    #: that trims 30% locks the symbol the strategy still holds.
    closes_position: bool = True

    @property
    def duration(self) -> timedelta:
        return self.exit_ts - self.entry_ts

    @property
    def is_win(self) -> bool:
        return self.pnl > 0

    @property
    def exit_reason(self) -> str:
        """Canonical reason this trade closed.

        Risk models prefix their tags with a stable token (`stop_loss:`,
        `trailing_stop:`, …) precisely so protections can count *stop-outs*
        rather than merely losing trades — a distinction that matters, because
        a strategy can bleed steadily without ever tripping a stop.
        """
        head = (self.exit_tag or "").split(":", 1)[0].strip().lower()
        return head.replace(" ", "_") if head else "unknown"

    @property
    def was_stopped_out(self) -> bool:
        return self.exit_reason in EXIT_STOP_REASONS


# ─────────────────────────────────────────────────────────────────────────────
# Timeframe helpers
# ─────────────────────────────────────────────────────────────────────────────
_TF_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def timeframe_seconds(tf: str) -> int:
    tf = tf.strip().lower()
    unit = tf[-1]
    if unit not in _TF_UNITS:
        raise ValueError(f"unsupported timeframe: {tf!r}")
    return int(tf[:-1]) * _TF_UNITS[unit]


def timeframe_delta(tf: str) -> timedelta:
    return timedelta(seconds=timeframe_seconds(tf))


def floor_to_timeframe(ts: datetime, tf: str) -> datetime:
    secs = timeframe_seconds(tf)
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - epoch % secs, tz=UTC)


ANNUALIZATION = {"1m": 525_600, "5m": 105_120, "15m": 35_040, "1h": 8_760, "4h": 2_190,
                 "1d": 252, "1w": 52}


def periods_per_year(tf: str) -> float:
    if tf in ANNUALIZATION:
        return float(ANNUALIZATION[tf])
    return 365 * 24 * 3600 / timeframe_seconds(tf)
