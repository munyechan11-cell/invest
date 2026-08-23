"""Concrete risk management models."""
from __future__ import annotations

import logging
import math
import statistics
from decimal import Decimal

from quant.core.context import Context
from quant.core.types import PortfolioTarget, periods_per_year
from quant.risk.base import RiskManagementModel

log = logging.getLogger("quant.risk.models")


class MaximumDrawdownPerSecurity(RiskManagementModel):
    """Stop-loss per position, measured from average entry price.

    A fixed percentage stop is the most common way a sound signal is turned
    into a losing strategy. The stop has to clear the noise the signal's own
    horizon generates: an instrument with 2% daily volatility moves roughly
    2%*sqrt(20) ~ 9% over a 20-bar hold *by chance*, so a 10% stop sits about
    one standard deviation away and will be hit perhaps a quarter of the time
    even when the direction is right.

    Set `atr_multiple` to scale the stop to the instrument instead, and treat
    `max_drawdown_pct` as the absolute ceiling. As a rule of thumb the multiple
    wants to be near `2 * sqrt(holding_bars)` — about 8-9 ATR for a 20-bar
    signal, not 5.
    """

    name = "max_dd_per_security"

    def __init__(self, max_drawdown_pct: float = 0.08, lock_bars: int = 5,
                 atr_multiple: float | None = None, atr_period: int = 14,
                 min_pct: float = 0.02):
        self.limit = abs(max_drawdown_pct)
        self.lock_bars = lock_bars
        self.atr_multiple = atr_multiple
        self.atr_period = atr_period
        self.min_pct = min_pct

    def _limit_for(self, ctx: Context, symbol) -> float:
        if not self.atr_multiple:
            return self.limit
        bars = ctx.history(symbol, self.atr_period + 1)
        if len(bars) < 3:
            return self.limit
        trs = [max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
               for p, b in zip(bars, bars[1:])]
        atr = statistics.fmean(trs[-self.atr_period:])
        price = bars[-1].close
        if price <= 0:
            return self.limit
        scaled = atr / price * self.atr_multiple
        # the configured percentage becomes the ceiling, not the target
        return min(max(scaled, self.min_pct), self.limit)

    def manage(self, ctx, targets):
        by_key = {t.symbol.key: t for t in targets}
        for pos in ctx.portfolio.open_positions:
            limit = self._limit_for(ctx, pos.symbol)
            if pos.unrealized_pct >= -limit:
                continue
            reason = f"stop_loss: {pos.unrealized_pct:+.2%} < -{limit:.2%}"
            by_key[pos.symbol.key] = self._flatten(
                by_key.get(pos.symbol.key, PortfolioTarget(pos.symbol, Decimal("0"))), reason
            )
            if self.lock_bars:
                ctx.lock(pos.symbol, ctx.now + ctx.bar_delta * self.lock_bars, reason)
        return list(by_key.values())


class TrailingStopRiskModel(RiskManagementModel):
    """Trail a stop behind the best price achieved since entry.

    Optionally ATR-scaled: a fixed 5% trail is far too tight on a name that
    routinely moves 6% a day and far too loose on one that moves 0.5%.
    """

    name = "trailing_stop"

    def __init__(self, trail_pct: float = 0.06, activate_at_pct: float = 0.0,
                 atr_multiple: float | None = None, atr_period: int = 14):
        self.trail = abs(trail_pct)
        self.activate_at = activate_at_pct
        self.atr_multiple = atr_multiple
        self.atr_period = atr_period

    def _trail_for(self, ctx: Context, symbol) -> float:
        if not self.atr_multiple:
            return self.trail
        bars = ctx.history(symbol, self.atr_period + 1)
        if len(bars) < 3:
            return self.trail
        trs = [max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
               for p, b in zip(bars, bars[1:])]
        atr = statistics.fmean(trs[-self.atr_period:])
        price = bars[-1].close
        return max(atr * self.atr_multiple / price, 0.005) if price > 0 else self.trail

    def manage(self, ctx, targets):
        by_key = {t.symbol.key: t for t in targets}
        for pos in ctx.portfolio.open_positions:
            price = ctx.price(pos.symbol)
            if price <= 0:
                continue
            pos.mark(price)
            if pos.unrealized_pct < self.activate_at:
                continue
            trail = self._trail_for(ctx, pos.symbol)
            if pos.is_long:
                hit = pos.peak_price > 0 and price <= pos.peak_price * (1 - trail)
                extreme = pos.peak_price
            else:
                hit = pos.trough_price > 0 and price >= pos.trough_price * (1 + trail)
                extreme = pos.trough_price
            if hit:
                by_key[pos.symbol.key] = self._flatten(
                    by_key.get(pos.symbol.key, PortfolioTarget(pos.symbol, Decimal("0"))),
                    f"trailing_stop: {trail:.2%} from {extreme:.4f}",
                )
        return list(by_key.values())


class MaximumUnrealizedProfit(RiskManagementModel):
    """Take profit at a fixed unrealized gain.

    Off by default in the shipped config: capping winners while leaving losers
    uncapped inverts the payoff asymmetry trend-following depends on. Enable it
    only for genuinely mean-reverting strategies.
    """

    name = "take_profit"

    def __init__(self, target_pct: float = 0.15):
        self.target = abs(target_pct)

    def manage(self, ctx, targets):
        by_key = {t.symbol.key: t for t in targets}
        for pos in ctx.portfolio.open_positions:
            if pos.unrealized_pct >= self.target:
                by_key[pos.symbol.key] = self._flatten(
                    by_key.get(pos.symbol.key, PortfolioTarget(pos.symbol, Decimal("0"))),
                    f"take_profit: {pos.unrealized_pct:+.2%}",
                )
        return list(by_key.values())


class MaximumDrawdownPortfolio(RiskManagementModel):
    """Kill switch: flatten everything and stop trading past a drawdown limit.

    The subtlety is *un*-tripping. Measuring recovery against the all-time
    high-water mark makes the switch permanent — a flattened book cannot earn
    its way back, so the system halts once and never trades again. Instead the
    halt runs for `halt_bars`, then the high-water mark is rebased to current
    equity and trading resumes on a fresh baseline.

    `max_trips` is the real kill switch: repeatedly hitting the limit means the
    strategy is broken, not unlucky, and it stops for good until an operator
    looks at it.
    """

    name = "max_dd_portfolio"

    def __init__(self, max_drawdown_pct: float = 0.20, halt_bars: int = 20,
                 max_trips: int = 3):
        self.limit = abs(max_drawdown_pct)
        self.halt_bars = halt_bars
        self.max_trips = max_trips
        self.tripped = False
        self.trips = 0
        self.halted_permanently = False
        self._resume_at = None

    def manage(self, ctx, targets):
        if self.halted_permanently:
            return [self._flatten(t, "halted: drawdown limit hit too often") for t in targets]

        if self.tripped:
            if self._resume_at is not None and ctx.now >= self._resume_at:
                self.tripped = False
                self._resume_at = None
                ctx.unlock_all()
                # fresh baseline so the strategy is not judged against a peak it
                # can no longer reach from a flat book
                ctx.portfolio.high_water_mark = ctx.portfolio.equity
                log.info("drawdown halt lifted; high-water mark rebased to %.2f",
                         ctx.portfolio.equity)
            else:
                return [self._flatten(t, "max_dd_portfolio: drawdown halt") for t in targets]

        dd = ctx.portfolio.drawdown
        if dd >= self.limit:
            self.tripped = True
            self.trips += 1
            self._resume_at = ctx.now + ctx.bar_delta * self.halt_bars
            reason = (f"max_dd_portfolio: drawdown {dd:.2%} >= {self.limit:.2%} — flattening "
                      f"(trip {self.trips}/{self.max_trips})")
            log.warning(reason)
            if self.trips >= self.max_trips:
                self.halted_permanently = True
                reason += " — permanent halt, operator review required"
            ctx.lock_all(ctx.now + ctx.bar_delta * self.halt_bars, reason)
            return [self._flatten(t, reason) for t in targets]
        return targets


class PortfolioVolatilityCap(RiskManagementModel):
    """Scale the whole book down when realized portfolio vol runs hot.

    Applied *after* construction, so it composes with any sizing model rather
    than requiring one that happens to be vol-aware.
    """

    name = "vol_cap"

    def __init__(self, max_annual_vol: float = 0.25, lookback: int = 30,
                 min_scale: float = 0.25):
        self.max_vol = max_annual_vol
        self.lookback = lookback
        self.min_scale = min_scale

    def manage(self, ctx, targets):
        rets = ctx.portfolio.returns()[-self.lookback:]
        if len(rets) < 10:
            return targets
        realized = statistics.pstdev(rets) * math.sqrt(periods_per_year(ctx.timeframe))
        if realized <= self.max_vol:
            return targets
        scale = max(self.min_scale, self.max_vol / realized)
        out = []
        for t in targets:
            scaled = t.symbol.round_qty(t.quantity * Decimal(str(scale)))
            out.append(PortfolioTarget(
                t.symbol, scaled,
                tag=f"{t.tag} | vol cap x{scale:.2f} (realized {realized:.1%})",
                source=t.source,
            ))
        return out


class MaxPositionCount(RiskManagementModel):
    """Cap concurrent positions, keeping the highest-conviction targets."""

    name = "max_positions"

    def __init__(self, max_positions: int = 10):
        self.max_positions = max_positions

    def manage(self, ctx, targets):
        held = {p.symbol.key for p in ctx.portfolio.open_positions}
        opening = [t for t in targets if t.quantity != 0 and t.symbol.key not in held]
        keeping = [t for t in targets if t.quantity != 0 and t.symbol.key in held]
        closing = [t for t in targets if t.quantity == 0]
        slots = max(0, self.max_positions - len(keeping))
        if len(opening) <= slots:
            return targets
        opening.sort(key=lambda t: abs(float(t.quantity)) * ctx.price(t.symbol), reverse=True)
        rejected = [
            PortfolioTarget(t.symbol, Decimal("0"),
                            tag=f"position cap {self.max_positions} reached", source="risk")
            for t in opening[slots:]
        ]
        return keeping + closing + opening[:slots] + rejected


class SectorExposureCap(RiskManagementModel):
    """Limit gross exposure per sector/group. Needs a symbol→group mapping."""

    name = "sector_cap"

    def __init__(self, groups: dict[str, str], max_group_weight: float = 0.4):
        self.groups = groups
        self.limit = max_group_weight

    def manage(self, ctx, targets):
        equity = max(ctx.equity, 1e-9)
        exposure: dict[str, float] = {}
        for t in targets:
            group = self.groups.get(t.symbol.ticker) or self.groups.get(t.symbol.key)
            if not group:
                continue
            exposure[group] = exposure.get(group, 0.0) + abs(
                float(t.quantity) * ctx.price(t.symbol) / equity
            )
        overweight = {g: self.limit / w for g, w in exposure.items() if w > self.limit}
        if not overweight:
            return targets
        out = []
        for t in targets:
            group = self.groups.get(t.symbol.ticker) or self.groups.get(t.symbol.key)
            scale = overweight.get(group or "")
            if scale is None:
                out.append(t)
                continue
            out.append(PortfolioTarget(
                t.symbol, t.symbol.round_qty(t.quantity * Decimal(str(scale))),
                tag=f"{t.tag} | {group} sector cap x{scale:.2f}", source=t.source,
            ))
        return out


class TimeStopRiskModel(RiskManagementModel):
    """Close positions that have gone nowhere for too long.

    Capital sitting in a thesis that has not played out is capital not available
    to the next one; opportunity cost is a real risk even when the P&L is flat.
    """

    name = "time_stop"

    def __init__(self, max_bars_held: int = 40, min_progress_pct: float = 0.01):
        self.max_bars = max_bars_held
        self.min_progress = min_progress_pct

    def manage(self, ctx, targets):
        by_key = {t.symbol.key: t for t in targets}
        for pos in ctx.portfolio.open_positions:
            if pos.opened_at is None:
                continue
            held_bars = (ctx.now - pos.opened_at) / ctx.bar_delta
            if held_bars < self.max_bars:
                continue
            if abs(pos.unrealized_pct) >= self.min_progress:
                continue
            by_key[pos.symbol.key] = self._flatten(
                by_key.get(pos.symbol.key, PortfolioTarget(pos.symbol, Decimal("0"))),
                f"time_stop: {held_bars:.0f} bars, {pos.unrealized_pct:+.2%}",
            )
        return list(by_key.values())


class TradingLockGate(RiskManagementModel):
    """Enforces `ctx` locks set by protections — blocks *new* entries only.

    Exits are never blocked. A protection that stops you closing a losing
    position is not a protection.
    """

    name = "lock_gate"

    def manage(self, ctx, targets):
        out = []
        for t in targets:
            current = ctx.portfolio.quantity(t.symbol)
            locked, reason = ctx.is_locked(t.symbol)
            if not locked:
                out.append(t)
                continue

            if t.quantity == 0:
                # Closing is always allowed. A lock that traps you in a losing
                # position is not a protection, it is a liability.
                out.append(t)
            elif current == 0:
                out.append(PortfolioTarget(
                    t.symbol, Decimal("0"), tag=f"entry blocked: {reason}",
                    source="lock_gate",
                ))
            elif (t.quantity > 0) != (current > 0):
                # A flip is a close plus a new entry; permit the close only.
                out.append(PortfolioTarget(
                    t.symbol, Decimal("0"), tag=f"flip blocked, closing: {reason}",
                    source="lock_gate",
                ))
            elif abs(t.quantity) > abs(current):
                out.append(PortfolioTarget(
                    t.symbol, current, tag=f"add blocked: {reason}", source="lock_gate"
                ))
            else:
                out.append(t)        # reducing
        return out


BUILTIN_RISK_MODELS = {
    "max_dd_per_security": MaximumDrawdownPerSecurity,
    "trailing_stop": TrailingStopRiskModel,
    "take_profit": MaximumUnrealizedProfit,
    "max_dd_portfolio": MaximumDrawdownPortfolio,
    "vol_cap": PortfolioVolatilityCap,
    "max_positions": MaxPositionCount,
    "sector_cap": SectorExposureCap,
    "time_stop": TimeStopRiskModel,
    "lock_gate": TradingLockGate,
}
