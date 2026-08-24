"""Concrete portfolio construction models.

Ordered roughly by how much they assume:
  EqualWeighting      — assumes nothing beyond direction
  InsightWeighting    — trusts the alpha's confidence calibration
  VolatilityTargeting — assumes vol is forecastable (it is, far more than returns)
  RiskParity          — assumes vol and correlation are forecastable
  KellyFractional     — assumes magnitude *and* hit-rate are calibrated
  MeanVariance        — assumes all of the above

The default is `VolatilityTargeting`, because forecasting volatility is a much
better-supported claim than forecasting returns, and equalising risk (not
capital) across positions is where most of the practical Sharpe improvement in
a multi-asset book actually comes from.
"""
from __future__ import annotations

import math
import statistics

from quant.core.context import Context
from quant.core.types import Insight, Symbol, periods_per_year
from quant.portfolio.base import PortfolioConstructionModel
from quant.portfolio.optimizer import (
    inverse_variance_weights,
    max_sharpe_weights,
    min_variance_weights,
    risk_parity_weights,
)


def _returns(ctx: Context, symbol: Symbol, lookback: int) -> list[float]:
    closes = ctx.closes(symbol, lookback + 1)
    return [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]


def _annual_vol(ctx: Context, symbol: Symbol, lookback: int) -> float:
    rets = _returns(ctx, symbol, lookback)
    if len(rets) < 5:
        return 0.0
    return statistics.pstdev(rets) * math.sqrt(periods_per_year(ctx.timeframe))


class EqualWeighting(PortfolioConstructionModel):
    """Spread capital evenly across every symbol with a non-zero view.

    Hard to beat out of sample. Use it as the honest baseline any fancier model
    has to actually outperform.
    """

    name = "equal_weight"

    def weights(self, ctx, insights):
        scores = self._net_scores(insights, ctx)
        active = {k: (s, v) for k, (s, v) in scores.items() if abs(v) > 1e-9}
        if not active:
            return {}
        each = self.max_gross_leverage / len(active)
        return {k: math.copysign(each, v) for k, (_, v) in active.items()}


class InsightWeighting(PortfolioConstructionModel):
    """Weight proportional to decayed conviction. Confidence must be calibrated."""

    name = "insight_weight"

    def weights(self, ctx, insights):
        scores = self._net_scores(insights, ctx)
        total = sum(abs(v) for _, v in scores.values())
        if total <= 0:
            return {}
        return {k: (v / total) * self.max_gross_leverage for k, (_, v) in scores.items()}


class VolatilityTargeting(PortfolioConstructionModel):
    """Size each position so it contributes an equal share of a target portfolio
    volatility, then scale the book to hit that target.

    This is what stops a single high-beta name from quietly becoming 80% of the
    portfolio's risk while looking like 20% of its capital.
    """

    name = "vol_target"

    def __init__(self, target_annual_vol: float = 0.15, lookback: int = 60,
                 min_vol: float = 0.05, max_vol: float = 2.0, **kwargs):
        super().__init__(**kwargs)
        self.target_vol = target_annual_vol
        self.lookback = lookback
        self.min_vol, self.max_vol = min_vol, max_vol

    def weights(self, ctx, insights):
        scores = self._net_scores(insights, ctx)
        active = {k: (s, v) for k, (s, v) in scores.items() if abs(v) > 1e-9}
        if not active:
            return {}
        raw: dict[str, float] = {}
        for key, (symbol, score) in active.items():
            vol = _annual_vol(ctx, symbol, self.lookback)
            if vol <= 0:
                vol = self.target_vol
            vol = min(max(vol, self.min_vol), self.max_vol)
            # per-name budget scaled by conviction, inverse to its own vol
            raw[key] = math.copysign(self.target_vol / vol, score) * abs(score)

        gross = sum(abs(w) for w in raw.values())
        if gross <= 0:
            return {}
        # Normalise to the leverage budget; the individual inverse-vol tilt is
        # what matters, the absolute level is set by max_gross_leverage.
        scale = min(1.0, self.max_gross_leverage / gross)
        return {k: w * scale for k, w in raw.items()}


class RiskParity(PortfolioConstructionModel):
    """Equal *risk contribution* across the book, correlations included."""

    name = "risk_parity"

    def __init__(self, lookback: int = 90, **kwargs):
        super().__init__(**kwargs)
        self.lookback = lookback

    def weights(self, ctx, insights):
        scores = self._net_scores(insights, ctx)
        active = [(k, s, v) for k, (s, v) in scores.items() if abs(v) > 1e-9]
        if not active:
            return {}
        series = [_returns(ctx, s, self.lookback) for _, s, _ in active]
        n = min((len(r) for r in series), default=0)
        if n < 20:
            # not enough history for a covariance estimate worth trusting
            each = self.max_gross_leverage / len(active)
            return {k: math.copysign(each, v) for k, _, v in active}
        matrix = [r[-n:] for r in series]
        w = risk_parity_weights(matrix)
        return {
            k: math.copysign(w[i] * self.max_gross_leverage, v)
            for i, (k, _, v) in enumerate(active)
        }


class KellyFractional(PortfolioConstructionModel):
    """Fractional Kelly on the alpha's own expected move and confidence.

    Full Kelly is mathematically optimal for growth and operationally insane —
    it assumes your edge estimate is exact. `fraction` defaults to 0.25, which
    keeps ~90% of the growth for ~40% of the drawdown when the estimate is off.
    """

    name = "kelly"

    def __init__(self, fraction: float = 0.25, lookback: int = 60,
                 default_edge: float = 0.02, **kwargs):
        super().__init__(**kwargs)
        self.fraction = fraction
        self.lookback = lookback
        self.default_edge = default_edge

    def weights(self, ctx, insights):
        by_symbol: dict[str, list[Insight]] = {}
        for i in insights:
            by_symbol.setdefault(i.symbol.key, []).append(i)
        scores = self._net_scores(insights, ctx)

        out: dict[str, float] = {}
        for key, (symbol, score) in scores.items():
            if abs(score) < 1e-9:
                out[key] = 0.0
                continue
            items = by_symbol.get(key, [])
            mags = [abs(i.magnitude) for i in items if i.magnitude]
            edge = statistics.fmean(mags) if mags else self.default_edge
            vol = _annual_vol(ctx, symbol, self.lookback)
            # scale annual vol down to the insight's actual horizon
            horizon_bars = max(
                1.0,
                statistics.fmean([i.period / ctx.bar_delta for i in items]) if items else 1.0,
            )
            per_bar = vol / math.sqrt(periods_per_year(ctx.timeframe))
            horizon_vol = max(per_bar * math.sqrt(horizon_bars), 1e-4)
            kelly = (edge * abs(score)) / (horizon_vol ** 2)
            out[key] = math.copysign(min(kelly * self.fraction, self.max_position_weight), score)
        return out


class MeanVarianceOptimization(PortfolioConstructionModel):
    """Markowitz with the sharp edges filed off.

    Expected returns come from the insights (not from historical means, which is
    the classic way to get a portfolio that is 100% whatever went up last year);
    the covariance is shrunk toward its diagonal to keep the inverse stable.
    """

    name = "mean_variance"

    def __init__(self, lookback: int = 120, objective: str = "max_sharpe",
                 risk_free_rate: float = 0.02, shrinkage: float = 0.2, **kwargs):
        super().__init__(**kwargs)
        self.lookback = lookback
        self.objective = objective
        self.rf = risk_free_rate
        self.shrinkage = shrinkage

    def weights(self, ctx, insights):
        scores = self._net_scores(insights, ctx)
        active = [(k, s, v) for k, (s, v) in scores.items() if abs(v) > 1e-9]
        if len(active) < 2:
            return {k: math.copysign(min(abs(v), self.max_position_weight), v)
                    for k, _, v in active}
        series = [_returns(ctx, s, self.lookback) for _, s, _ in active]
        n = min((len(r) for r in series), default=0)
        if n < 30:
            each = self.max_gross_leverage / len(active)
            return {k: math.copysign(each, v) for k, _, v in active}
        matrix = [r[-n:] for r in series]

        by_symbol: dict[str, list[Insight]] = {}
        for i in insights:
            by_symbol.setdefault(i.symbol.key, []).append(i)
        expected = []
        ppy = periods_per_year(ctx.timeframe)
        for key, _, score in active:
            mags = [abs(i.magnitude) for i in by_symbol.get(key, []) if i.magnitude]
            horizon = statistics.fmean(
                [i.period / ctx.bar_delta for i in by_symbol.get(key, [])] or [5.0]
            )
            per_bar = (statistics.fmean(mags) if mags else 0.01) / max(horizon, 1.0)
            expected.append(score * per_bar * ppy)          # annualised, signed

        if self.objective == "min_variance":
            w = min_variance_weights(matrix, self.shrinkage)
        elif self.objective == "inverse_variance":
            w = inverse_variance_weights(matrix)
        else:
            w = max_sharpe_weights(matrix, expected, self.rf, self.shrinkage)

        total = sum(abs(x) for x in w) or 1.0
        return {
            k: w[i] / total * self.max_gross_leverage
            for i, (k, _, _) in enumerate(active)
        }


class FixedStakePortfolio(PortfolioConstructionModel):
    """Freqtrade-style flat stake per position, with an ATR-scaled variant.

    Simple, predictable, and the right default when the account is small enough
    that exchange minimums, not risk math, are the binding constraint.
    """

    name = "fixed_stake"

    def __init__(self, stake_amount: float | None = None, max_open_positions: int = 5,
                 risk_per_trade: float = 0.01, atr_stop_multiple: float = 2.0,
                 atr_lookback: int = 14, **kwargs):
        super().__init__(**kwargs)
        self.stake_amount = stake_amount
        self.max_open = max_open_positions
        self.risk_per_trade = risk_per_trade
        self.atr_multiple = atr_stop_multiple
        self.atr_lookback = atr_lookback

    def _atr_pct(self, ctx: Context, symbol: Symbol) -> float:
        bars = ctx.history(symbol, self.atr_lookback + 1)
        if len(bars) < 3:
            return 0.02
        trs = [
            max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
            for p, b in zip(bars, bars[1:])
        ]
        atr = statistics.fmean(trs[-self.atr_lookback:])
        price = bars[-1].close
        return atr / price if price > 0 else 0.02

    def weights(self, ctx, insights):
        scores = self._net_scores(insights, ctx)
        ranked = sorted(
            [(k, s, v) for k, (s, v) in scores.items() if abs(v) > 1e-9],
            key=lambda x: abs(x[2]), reverse=True,
        )[: self.max_open]
        if not ranked:
            return {}
        out: dict[str, float] = dict.fromkeys(scores, 0.0)
        for key, symbol, score in ranked:
            if self.stake_amount is not None:
                w = self.stake_amount / max(ctx.equity, 1e-9)
            else:
                # size so an ATR-multiple adverse move costs `risk_per_trade`
                stop_dist = max(self._atr_pct(ctx, symbol) * self.atr_multiple, 0.005)
                w = self.risk_per_trade / stop_dist
            out[key] = math.copysign(min(w, self.max_position_weight), score)
        return out


BUILTIN_PORTFOLIO_MODELS = {
    "equal_weight": EqualWeighting,
    "insight_weight": InsightWeighting,
    "vol_target": VolatilityTargeting,
    "risk_parity": RiskParity,
    "kelly": KellyFractional,
    "mean_variance": MeanVarianceOptimization,
    "fixed_stake": FixedStakePortfolio,
}
