"""Concrete execution models."""
from __future__ import annotations

import statistics
from decimal import Decimal

from quant.core.types import Order, OrderType, TimeInForce
from quant.execution.base import ExecutionModel


class ImmediateExecution(ExecutionModel):
    """Cross the spread now. Correct whenever signal decay beats spread cost."""

    name = "immediate"

    def execute(self, ctx, targets):
        return [self._order(t, d) for t, d, _ in self._deltas(ctx, targets)]


class LimitExecution(ExecutionModel):
    """Post inside the spread and wait, converting to market on urgency.

    Saves the spread on the fills that happen, at the cost of missing some
    moves. Worth it for slow signals and mid-cap liquidity; wrong for anything
    that decays inside a bar.
    """

    name = "limit"

    def __init__(self, offset_bps: float = 5.0, urgent_after_bars: int = 3,
                 min_order_notional: float = 1.0):
        super().__init__(min_order_notional)
        self.offset = offset_bps / 10_000.0
        self.urgent_after = urgent_after_bars
        self._pending: dict[str, int] = {}

    def execute(self, ctx, targets):
        orders: list[Order] = []
        for t, delta, price in self._deltas(ctx, targets):
            key = t.symbol.key
            waited = self._pending.get(key, 0)
            if waited >= self.urgent_after:
                self._pending.pop(key, None)
                orders.append(self._order(t, delta, tag_suffix=" | urgent→market"))
                continue
            self._pending[key] = waited + 1
            quote = ctx.quote(t.symbol)
            ref = quote.mid if quote else price
            limit = ref * (1 - self.offset) if delta > 0 else ref * (1 + self.offset)
            orders.append(self._order(t, delta, OrderType.LIMIT, limit,
                                      TimeInForce.DAY, f" | post {self.offset:.2%}"))
        # a symbol that reached its target is no longer waiting on anything
        wanted = {t.symbol.key for t, _, _ in self._deltas(ctx, targets)}
        for key in list(self._pending):
            if key not in wanted:
                self._pending.pop(key, None)
        return orders


class TwapExecution(ExecutionModel):
    """Slice each delta into equal child orders across N bars.

    The point is not price improvement, it is *variance* reduction: one bad
    print on a thin instrument can cost more than the whole signal was worth.
    """

    name = "twap"

    def __init__(self, slices: int = 4, min_order_notional: float = 1.0):
        super().__init__(min_order_notional)
        self.slices = max(slices, 1)
        self._remaining: dict[str, tuple[Decimal, int]] = {}

    def execute(self, ctx, targets):
        orders: list[Order] = []
        seen: set[str] = set()
        for t, delta, _ in self._deltas(ctx, targets):
            key = t.symbol.key
            seen.add(key)
            rem, left = self._remaining.get(key, (delta, self.slices))
            # target moved materially → restart the schedule
            if (delta > 0) != (rem > 0) or abs(delta) > abs(rem) * Decimal("1.5"):
                rem, left = delta, self.slices
            left = max(left, 1)
            slice_qty = t.symbol.round_qty(rem / Decimal(left))
            if slice_qty == 0:
                slice_qty = rem
            orders.append(self._order(t, slice_qty, tag_suffix=f" | twap {self.slices - left + 1}/{self.slices}"))
            leftover = rem - slice_qty
            if left <= 1 or leftover == 0:
                self._remaining.pop(key, None)
            else:
                self._remaining[key] = (leftover, left - 1)
        for key in list(self._remaining):
            if key not in seen:
                self._remaining.pop(key, None)
        return orders


class VolumeParticipationExecution(ExecutionModel):
    """Cap each child order at a fraction of recent average volume.

    Below ~10% of volume, market impact is roughly linear in participation and
    small; above it, impact grows fast and the fill you measured in the backtest
    stops resembling the one you get.
    """

    name = "pov"

    def __init__(self, participation: float = 0.08, lookback: int = 20,
                 min_order_notional: float = 1.0):
        super().__init__(min_order_notional)
        self.participation = participation
        self.lookback = lookback

    def execute(self, ctx, targets):
        orders: list[Order] = []
        for t, delta, _ in self._deltas(ctx, targets):
            bars = ctx.history(t.symbol, self.lookback)
            volumes = [b.volume for b in bars if b.volume > 0]
            if not volumes:
                orders.append(self._order(t, delta))
                continue
            adv = statistics.fmean(volumes)
            cap = t.symbol.round_qty(Decimal(str(adv * self.participation)))
            qty = delta if abs(delta) <= cap else cap.copy_sign(delta)
            if qty == 0:
                continue
            pct = abs(float(qty)) / adv
            orders.append(self._order(t, qty, tag_suffix=f" | pov {pct:.1%} of ADV"))
        return orders


class StandardDeviationExecution(ExecutionModel):
    """Only trade when price is a favourable distance from its short-term mean.

    A patient model: it waits for the market to come to it, and skips the bar
    entirely otherwise. Pairs well with slow signals, badly with fast ones.
    """

    name = "std_dev"

    def __init__(self, period: int = 20, deviations: float = 1.0,
                 max_wait_bars: int = 5, min_order_notional: float = 1.0):
        super().__init__(min_order_notional)
        self.period = period
        self.deviations = deviations
        self.max_wait = max_wait_bars
        self._waiting: dict[str, int] = {}

    def execute(self, ctx, targets):
        orders: list[Order] = []
        for t, delta, price in self._deltas(ctx, targets):
            key = t.symbol.key
            closes = ctx.closes(t.symbol, self.period)
            waited = self._waiting.get(key, 0)
            if len(closes) < self.period or waited >= self.max_wait:
                self._waiting.pop(key, None)
                orders.append(self._order(t, delta, tag_suffix=" | patience exhausted"))
                continue
            mean, sd = statistics.fmean(closes), statistics.pstdev(closes)
            if sd <= 0:
                orders.append(self._order(t, delta))
                continue
            z = (price - mean) / sd
            favourable = z <= -self.deviations if delta > 0 else z >= self.deviations
            if favourable:
                self._waiting.pop(key, None)
                orders.append(self._order(t, delta, tag_suffix=f" | z={z:+.2f}"))
            else:
                self._waiting[key] = waited + 1
        return orders


BUILTIN_EXECUTION_MODELS = {
    "immediate": ImmediateExecution,
    "limit": LimitExecution,
    "twap": TwapExecution,
    "pov": VolumeParticipationExecution,
    "std_dev": StandardDeviationExecution,
}
