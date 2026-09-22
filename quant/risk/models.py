"""Concrete risk management models."""
from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime
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

    #: ATR 스케일링이 만들 수 있는 트레일의 천장.
    #:
    #: 이게 없으면 `atr_multiple * ATR/price` 가 1.0 을 넘을 수 있고, 그러면
    #: 롱의 발동 조건 `price <= peak * (1 - trail)` 의 우변이 **0 이하** 가
    #: 되어 어떤 가격에서도 참이 되지 않습니다. 트레일링 스톱이 예외도 로그도
    #: 없이 사라지는 것이고, 하필 **변동성이 치솟은 순간** — 그게 필요한 바로
    #: 그때 — 사라집니다. 실측: `atr_multiple=5.0`(출하 설정 전부)에서 봉
    #: 변동폭이 ±10% 면 trail=1.00, 고점 대비 -50% 인 보유가 청산되지
    #: 않습니다. 숏은 `1 + trail` 이라 안 깨지는 대신 조용히 4배 느슨해집니다.
    #:
    #: 0.95 인 이유는 **지금 동작하는 값을 하나도 바꾸지 않기 위해서** 입니다.
    #: 더 조이면(예: trail_pct 를 천장으로) 멀쩡히 걸리던 손절이 훨씬 자주
    #: 걸리게 되고, 그건 안전 수정이 아니라 전략 변경입니다.
    MAX_TRAIL = 0.95

    def __init__(self, trail_pct: float = 0.06, activate_at_pct: float = 0.0,
                 atr_multiple: float | None = None, atr_period: int = 14):
        self.trail = abs(trail_pct)
        self.activate_at = activate_at_pct
        self.atr_multiple = atr_multiple
        self.atr_period = atr_period
        #: 천장에 닿았다고 이미 말한 종목. 봉마다 같은 줄을 찍으면 아무도
        #: 안 읽습니다.
        self._warned: set[str] = set()

    def _trail_for(self, ctx: Context, symbol) -> float:
        if not self.atr_multiple:
            return min(self.trail, self.MAX_TRAIL)
        bars = ctx.history(symbol, self.atr_period + 1)
        if len(bars) < 3:
            return min(self.trail, self.MAX_TRAIL)
        trs = [max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
               for p, b in zip(bars, bars[1:])]
        atr = statistics.fmean(trs[-self.atr_period:])
        price = bars[-1].close
        if price <= 0:
            return min(self.trail, self.MAX_TRAIL)
        scaled = max(atr * self.atr_multiple / price, 0.005)
        if scaled > self.MAX_TRAIL:
            if symbol.key not in self._warned:
                self._warned.add(symbol.key)
                log.warning(
                    "%s: atr_multiple %.1f × ATR/가격 = 트레일 %.0f%% 로 "
                    "천장 %.0f%% 를 넘었습니다 — 그대로 두면 트레일링 스톱이 "
                    "어떤 가격에서도 발동하지 않습니다. 이 종목에는 "
                    "atr_multiple 이 너무 큽니다",
                    symbol.ticker, self.atr_multiple, scaled * 100,
                    self.MAX_TRAIL * 100,
                )
            return self.MAX_TRAIL
        return scaled

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
                # 자기가 건 전체 정지만 푼다. `unlock_all` 은 쿨다운과 종목별
                # 손절 가드까지 같이 지우고, 그것들은 드로다운이 끝난 직후에
                # 가장 필요한 잠금이다.
                ctx.unlock_book()
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

    def state_payload(self) -> dict | None:
        """The trip count is the whole kill switch, and it lived only in memory.

        The halt's *lock* was already persisted, which is why this looked
        covered. It was not: the lock comes back after a restart and expires on
        schedule, while `trips` comes back at zero. A strategy that had spent
        two of its three lives gets three fresh ones, and a permanent halt that
        says an operator must look at it is undone by a redeploy.
        """
        if not (self.tripped or self.trips or self.halted_permanently):
            return None
        return {"tripped": self.tripped, "trips": self.trips,
                "halted_permanently": self.halted_permanently,
                "resume_at": self._resume_at.isoformat() if self._resume_at else ""}

    def load_state(self, payload: dict) -> None:
        self.tripped = bool(payload.get("tripped"))
        self.trips = int(payload.get("trips") or 0)
        self.halted_permanently = bool(payload.get("halted_permanently"))
        raw = payload.get("resume_at") or ""
        self._resume_at = datetime.fromisoformat(raw) if raw else None
        if self.halted_permanently:
            log.error("복원: 드로다운 한도를 %d회 넘겨 **영구 정지** 상태입니다 — "
                      "운영자가 직접 확인해야 풀립니다", self.trips)
        elif self.tripped:
            log.warning("복원: 드로다운 정지 중 (%d/%d회) — 해제 예정 %s",
                        self.trips, self.max_trips,
                        self._resume_at.isoformat() if self._resume_at else "미정")


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
        keyed = {t.symbol.key for t in targets}
        opening = [t for t in targets if t.quantity != 0 and t.symbol.key not in held]
        keeping = [t for t in targets if t.quantity != 0 and t.symbol.key in held]
        closing = [t for t in targets if t.quantity == 0]
        # A holding the portfolio model left out of this batch still occupies a
        # slot. The rebalance deadband drops any name whose weight barely moved,
        # so counting only the targets turns the cap into a per-bar entry rate:
        # the book grows without limit as long as the old names stay quiet.
        silent = held - keyed
        slots = max(0, self.max_positions - len(keeping) - len(silent))
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

    def _group_of(self, symbol) -> str | None:
        return self.groups.get(symbol.ticker) or self.groups.get(symbol.key)

    def manage(self, ctx, targets):
        equity = max(ctx.equity, 1e-9)
        keyed = {t.symbol.key for t in targets}
        exposure: dict[str, float] = {}

        def add(symbol, quantity) -> None:
            group = self._group_of(symbol)
            if not group:
                return
            exposure[group] = exposure.get(group, 0.0) + abs(
                float(quantity) * ctx.price(symbol) / equity
            )

        for t in targets:
            add(t.symbol, t.quantity)
        # A holding this batch left out still sits in the group. The rebalance
        # deadband drops any name whose weight barely moved, so summing only
        # the targets lets a group drift past its cap for as long as the old
        # names stay quiet — the same trap `MaxPositionCount` documents above,
        # and it was open here.
        #
        # 빠진 보유는 여기서 줄일 수 없으므로(이번 봉 목표가 없습니다) 한도는
        # **배치 쪽을 더 조이는 것** 으로 지켜집니다. 안 보고 통과시키는 것보다
        # 낫고, 들고 있던 종목을 강제로 팔지도 않습니다.
        for pos in ctx.portfolio.open_positions:
            if pos.symbol.key not in keyed:
                add(pos.symbol, pos.quantity)

        overweight = {g: self.limit / w for g, w in exposure.items() if w > self.limit}
        if not overweight:
            return targets
        out = []
        for t in targets:
            group = self._group_of(t.symbol)
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
