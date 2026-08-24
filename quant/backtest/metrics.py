"""Performance analytics.

Beyond the usual ratios, this module reports the things that decide whether a
result is *believable*: deflated Sharpe (which discounts for how many variants
you tried), the probabilistic Sharpe ratio, turnover, and cost drag. A backtest
with a 3.0 Sharpe and 400% annual turnover is a statement about your fee model,
not about your edge.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field, fields
from datetime import datetime

from quant.core.account import EquityPoint, Portfolio
from quant.core.types import ClosedTrade, periods_per_year


def _safe(x: float, default: float = 0.0) -> float:
    return x if isinstance(x, (int, float)) and math.isfinite(x) else default


@dataclass
class PerformanceReport:
    start: datetime | None = None
    end: datetime | None = None
    duration_days: float = 0.0
    starting_equity: float = 0.0
    ending_equity: float = 0.0
    total_return: float = 0.0
    cagr: float = 0.0
    annual_volatility: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_duration_days: float = 0.0
    ulcer_index: float = 0.0
    trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    avg_hold_hours: float = 0.0
    exposure: float = 0.0
    turnover: float = 0.0
    total_fees: float = 0.0
    fee_drag: float = 0.0
    psr: float = 0.0
    deflated_sharpe: float = 0.0
    skew: float = 0.0
    kurtosis: float = 0.0
    var_95: float = 0.0
    cvar_95: float = 0.0
    tail_ratio: float = 0.0
    longest_win_streak: int = 0
    longest_loss_streak: int = 0
    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {}
        for f in fields(self):
            key = f.name
            value = getattr(self, key)
            if isinstance(value, datetime):
                out[key] = value.isoformat()
            elif isinstance(value, float):
                out[key] = round(value, 6)
            else:
                out[key] = value
        return out

    def summary_lines(self) -> list[str]:
        return [
            f"Period            {self.start:%Y-%m-%d} → {self.end:%Y-%m-%d}"
            if self.start and self.end else "Period            n/a",
            f"Equity            {self.starting_equity:,.0f} → {self.ending_equity:,.0f}"
            f"  ({self.total_return:+.2%})",
            f"CAGR              {self.cagr:+.2%}",
            f"Volatility        {self.annual_volatility:.2%}",
            f"Sharpe / Sortino  {self.sharpe:.2f} / {self.sortino:.2f}",
            f"Calmar            {self.calmar:.2f}",
            f"Max drawdown      {self.max_drawdown:.2%}"
            f"  ({self.max_drawdown_duration_days:.0f}d underwater)",
            f"Trades            {self.trades}  win {self.win_rate:.1%}"
            f"  PF {self.profit_factor:.2f}  expectancy {self.expectancy:+.3%}",
            f"Exposure          {self.exposure:.1%}   turnover {self.turnover:.2f}x/yr",
            f"Fees              {self.total_fees:,.0f}  ({self.fee_drag:.2%} of starting equity)",
            f"PSR / DSR         {self.psr:.1%} / {self.deflated_sharpe:.1%}",
        ]


def _drawdown_stats(curve: list[EquityPoint]) -> tuple[float, float, float]:
    """(max drawdown, longest underwater stretch in days, ulcer index)."""
    if not curve:
        return 0.0, 0.0, 0.0
    peak = curve[0].equity
    peak_ts = curve[0].ts
    max_dd = 0.0
    longest = 0.0
    squares = 0.0
    for point in curve:
        if point.equity >= peak:
            peak, peak_ts = point.equity, point.ts
        dd = 1.0 - point.equity / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        squares += dd * dd
        longest = max(longest, (point.ts - peak_ts).total_seconds() / 86400)
    return max_dd, longest, math.sqrt(squares / len(curve))


def _streaks(trades: list[ClosedTrade]) -> tuple[int, int]:
    best = worst = cur_w = cur_l = 0
    for t in trades:
        if t.is_win:
            cur_w, cur_l = cur_w + 1, 0
        else:
            cur_l, cur_w = cur_l + 1, 0
        best, worst = max(best, cur_w), max(worst, cur_l)
    return best, worst


def probabilistic_sharpe(sharpe: float, n: int, skew: float, kurtosis: float,
                         benchmark: float = 0.0) -> float:
    """Probability the true Sharpe exceeds `benchmark`, given non-normality.

    Bailey & López de Prado. Fat tails and negative skew — exactly what trading
    returns have — inflate the naive Sharpe, and this corrects for it.
    """
    if n < 3:
        return 0.0
    denom = 1.0 - skew * sharpe + ((kurtosis - 1.0) / 4.0) * sharpe ** 2
    if denom <= 0:
        return 0.0
    z = (sharpe - benchmark) * math.sqrt(n - 1) / math.sqrt(denom)
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def deflated_sharpe(sharpe: float, n: int, skew: float, kurtosis: float,
                    trials: int = 1,
                    variance_of_trials: float | None = None) -> float:
    """PSR against the Sharpe you would expect from `trials` random variants.

    This is the honest answer to "I tried 200 parameter sets and the best had a
    Sharpe of 2.1". The expected maximum of 200 draws from a zero-edge
    distribution is not zero, and this subtracts it.

    `sharpe` and `variance_of_trials` are both in **per-period** units — the
    caller divides an annual Sharpe by sqrt(periods per year) before calling.
    Mixing the two units is not a rounding error: this used to default
    `variance_of_trials` to 1.0, which in per-period units on daily bars means
    the trial Sharpes had a standard deviation of 15.9 annualised. The hurdle
    that produced was an annual Sharpe of **49**, so every strategy that will
    ever exist scored exactly 0.000000 and the metric said nothing at all.

    When the trial set is available, pass its actual variance — that is the
    quantity Bailey & López de Prado define. When it is not, we fall back to
    Lo (2002)'s asymptotic variance of the Sharpe estimator, (1 + SR²/2)/n,
    which is the distribution the trials would have under the null of no edge.
    It is an approximation, but it is the right order of magnitude, which the
    old default was not.
    """
    if trials <= 1:
        return probabilistic_sharpe(sharpe, n, skew, kurtosis, 0.0)
    if variance_of_trials is None:
        variance_of_trials = (1.0 + sharpe * sharpe / 2.0) / max(n, 2)
    euler = 0.5772156649
    e = math.e
    # expected max of `trials` standard normals
    z1 = _inv_norm(1.0 - 1.0 / trials)
    z2 = _inv_norm(1.0 - 1.0 / (trials * e))
    expected_max = (1 - euler) * z1 + euler * z2
    benchmark = math.sqrt(variance_of_trials) * expected_max
    return probabilistic_sharpe(sharpe, n, skew, kurtosis, benchmark)


def _inv_norm(p: float) -> float:
    """Acklam's inverse normal CDF approximation (|error| < 1.15e-9)."""
    if not 0 < p < 1:
        return 0.0
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q, r = p - 0.5, (p - 0.5) ** 2
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def analyze(
    portfolio: Portfolio,
    timeframe: str = "1d",
    risk_free_rate: float = 0.0,
    trials: int = 1,
    variance_of_trials: float | None = None,
) -> PerformanceReport:
    curve = portfolio.equity_curve
    rep = PerformanceReport()
    rep.starting_equity = portfolio.starting_cash
    rep.ending_equity = portfolio.equity
    rep.total_return = portfolio.total_return
    rep.total_fees = portfolio.total_fees
    rep.fee_drag = portfolio.total_fees / portfolio.starting_cash if portfolio.starting_cash else 0.0
    rep.trades = len(portfolio.closed_trades)
    if not curve:
        return rep

    rep.start, rep.end = curve[0].ts, curve[-1].ts
    rep.duration_days = max((rep.end - rep.start).total_seconds() / 86400, 1e-9)
    years = rep.duration_days / 365.25
    if years > 0 and rep.starting_equity > 0 and rep.ending_equity > 0:
        rep.cagr = (rep.ending_equity / rep.starting_equity) ** (1 / years) - 1

    rets = portfolio.returns()
    ppy = periods_per_year(timeframe)
    if len(rets) > 2:
        mean, sd = statistics.fmean(rets), statistics.pstdev(rets)
        rep.annual_volatility = sd * math.sqrt(ppy)
        rf_per = risk_free_rate / ppy
        rep.sharpe = _safe((mean - rf_per) / sd * math.sqrt(ppy)) if sd > 0 else 0.0
        downside = [r for r in rets if r < rf_per]
        dsd = statistics.pstdev(downside) if len(downside) > 1 else 0.0
        rep.sortino = _safe((mean - rf_per) / dsd * math.sqrt(ppy)) if dsd > 0 else 0.0
        rep.skew = _safe(_skew(rets))
        rep.kurtosis = _safe(_kurtosis(rets))
        ordered = sorted(rets)
        idx = max(int(len(ordered) * 0.05) - 1, 0)
        rep.var_95 = ordered[idx]
        tail = ordered[:idx + 1]
        rep.cvar_95 = statistics.fmean(tail) if tail else 0.0
        upper = sorted(rets, reverse=True)[:max(idx + 1, 1)]
        rep.tail_ratio = _safe(
            abs(statistics.fmean(upper) / statistics.fmean(tail)) if tail else 0.0
        )
        rep.psr = probabilistic_sharpe(rep.sharpe / math.sqrt(ppy), len(rets),
                                       rep.skew, rep.kurtosis)
        # 시행별 샤프의 분산은 탐색이 끝나야 알 수 있습니다. 없으면
        # deflated_sharpe 가 Lo 의 점근분산으로 대신합니다.
        rep.deflated_sharpe = deflated_sharpe(
            rep.sharpe / math.sqrt(ppy), len(rets), rep.skew, rep.kurtosis, trials,
            variance_of_trials
        )

    rep.max_drawdown, rep.max_drawdown_duration_days, rep.ulcer_index = _drawdown_stats(curve)
    rep.calmar = _safe(rep.cagr / rep.max_drawdown) if rep.max_drawdown > 0 else 0.0
    rep.exposure = statistics.fmean([p.exposure for p in curve]) if curve else 0.0

    trades = portfolio.closed_trades
    if trades:
        wins = [t for t in trades if t.is_win]
        losses = [t for t in trades if not t.is_win]
        rep.win_rate = len(wins) / len(trades)
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        rep.profit_factor = _safe(gross_win / gross_loss, math.inf if gross_win else 0.0) \
            if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)
        rep.avg_win = statistics.fmean([t.pnl_pct for t in wins]) if wins else 0.0
        rep.avg_loss = statistics.fmean([t.pnl_pct for t in losses]) if losses else 0.0
        rep.expectancy = statistics.fmean([t.pnl_pct for t in trades])
        rep.best_trade = max(t.pnl_pct for t in trades)
        rep.worst_trade = min(t.pnl_pct for t in trades)
        rep.avg_hold_hours = statistics.fmean(
            [t.duration.total_seconds() / 3600 for t in trades]
        )
        rep.longest_win_streak, rep.longest_loss_streak = _streaks(trades)
        traded_notional = sum(abs(float(t.quantity)) * t.entry_price for t in trades) * 2
        avg_equity = statistics.fmean([p.equity for p in curve]) or 1.0
        rep.turnover = _safe(traded_notional / avg_equity / max(years, 1e-9))

    return rep


def _skew(xs: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    m, sd = statistics.fmean(xs), statistics.pstdev(xs)
    if sd == 0:
        return 0.0
    return sum(((x - m) / sd) ** 3 for x in xs) / n


def _kurtosis(xs: list[float]) -> float:
    """Non-excess kurtosis (3.0 for a normal) — PSR's formula expects this form."""
    n = len(xs)
    if n < 4:
        return 3.0
    m, sd = statistics.fmean(xs), statistics.pstdev(xs)
    if sd == 0:
        return 3.0
    return sum(((x - m) / sd) ** 4 for x in xs) / n


def monthly_returns(portfolio: Portfolio) -> dict[str, float]:
    """Calendar-month returns, the format every allocator asks for first."""
    curve = portfolio.equity_curve
    if not curve:
        return {}
    out: dict[str, float] = {}
    month_start = curve[0]
    for prev, point in zip(curve, curve[1:]):
        if (point.ts.year, point.ts.month) != (prev.ts.year, prev.ts.month):
            key = f"{prev.ts.year}-{prev.ts.month:02d}"
            out[key] = prev.equity / month_start.equity - 1 if month_start.equity else 0.0
            month_start = prev
    last = curve[-1]
    out[f"{last.ts.year}-{last.ts.month:02d}"] = (
        last.equity / month_start.equity - 1 if month_start.equity else 0.0
    )
    return out
