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
            # 자리(진입~청산) 수입니다. "Trades" 라고만 쓰면 나눠 판 체결 수로
            # 읽히는데, 그게 정확히 이 줄이 예전에 인쇄하던 잘못된 수입니다.
            f"Round trips       {self.trades}  win {self.win_rate:.1%}"
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


@dataclass
class _RoundTrip:
    """한 자리(진입~청산)로 되접은 조각들.

    장부는 포지션을 줄이는 체결마다 `ClosedTrade` 를 한 줄 씁니다 — 실현손익과
    거래세가 거기서 확정되니 그게 맞습니다. 하지만 성적표가 그 줄을 그대로 세면
    "거래 수 · 승률 · 기대값" 이 **매도를 몇 번에 나눴는가** 를 재게 됩니다.
    익절만 조금씩 덜어내고 손절은 한 번에 하는 장부는 조각 대부분이 이겨서,
    계좌가 -8% 인데 성적표가 "580거래 승률 74.7%" 를 인쇄합니다.

    한 자리는 진입부터 청산(`closes_position`)까지입니다. 뒤집기도 경계입니다 —
    롱을 닫고 숏을 여는 체결은 롱을 실제로 끝냈습니다.
    """

    symbol_key: str
    slices: list[ClosedTrade] = field(default_factory=list)
    #: 아직 안 닫힌 자리는 실현된 부분만 들어 있습니다. 성적표에서 빼지 않는
    #: 이유는 손익·수수료가 이미 계좌에 반영됐고, 빼면 "닫힌 자리가 0" 인
    #: 장부에서 `report.trades` 가 0 이 되어 목적함수가 후보를 통째로
    #: 탈락시키기 때문입니다. 대신 그 자리의 미실현분은 반영되지 않습니다.
    closed: bool = False

    @property
    def pnl(self) -> float:
        return sum(t.pnl for t in self.slices)

    @property
    def is_win(self) -> bool:
        return self.pnl > 0

    @property
    def basis(self) -> float:
        """이 자리가 한때 실제로 묶었던 순투입현금의 최대치.

        안 닫힌 자리에서는 **마지막 실현 시점까지의** 최대치입니다 — 그 뒤에
        더 담은 돈은 조각에 실려 오지 않습니다. 실현손익을 만들어 낸 자본만
        분모에 넣는 셈이라 뜻은 통하지만, "지금 얼마 묶여 있나" 와는 다릅니다.
        """
        peak = max((t.peak_invested for t in self.slices), default=0.0)
        if peak > 0:
            return peak
        # `Portfolio.apply_fill` 을 거치지 않은 장부에는 자리 단위 분모가 없어
        # 조각 원가 합으로 물러섭니다. 트림 후 재매수를 두 번 세므로 되접기의
        # 이점이 그만큼 사라집니다 — 지금 `analyze` 를 부르는 곳은 백테스트
        # 러너뿐이라 실제로는 닿지 않는 경로입니다.
        return sum(abs(float(t.quantity)) * abs(t.entry_price)
                   * float(t.symbol.multiplier) for t in self.slices)

    @property
    def pnl_pct(self) -> float:
        b = self.basis
        return self.pnl / b if b > 0 else 0.0

    @property
    def hold_hours(self) -> float:
        start = min(t.entry_ts for t in self.slices)
        end = max(t.exit_ts for t in self.slices)
        return (end - start).total_seconds() / 3600


def _round_trips(trades: list[ClosedTrade]) -> list[_RoundTrip]:
    """조각 장부를 자리 단위로 되접습니다. 순서는 자리가 처음 등장한 순서."""
    out: list[_RoundTrip] = []
    live: dict[str, _RoundTrip] = {}
    for t in trades:
        key = t.symbol.key
        trip = live.get(key)
        if trip is None:
            trip = _RoundTrip(symbol_key=key)
            live[key] = trip
            out.append(trip)
        trip.slices.append(t)
        if t.closes_position:
            trip.closed = True
            live.pop(key, None)
    return out


def _streaks(trades: list[_RoundTrip]) -> tuple[int, int]:
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
    # 조각이 아니라 자리를 셉니다 — 왜인지는 `_RoundTrip` 을 보세요. 모든 조각은
    # 정확히 한 자리에 속하므로 "자리 0" 과 "조각 0" 은 여전히 같은 뜻이고,
    # 러너의 "zero closed trades" 경고와 목적함수의 0 분기는 그대로 동작합니다.
    trips = _round_trips(portfolio.closed_trades)
    rep.trades = len(trips)
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
    if trips:
        wins = [t for t in trips if t.is_win]
        losses = [t for t in trips if not t.is_win]
        rep.win_rate = len(wins) / len(trips)
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        rep.profit_factor = _safe(gross_win / gross_loss, math.inf if gross_win else 0.0) \
            if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)
        rep.avg_win = statistics.fmean([t.pnl_pct for t in wins]) if wins else 0.0
        rep.avg_loss = statistics.fmean([t.pnl_pct for t in losses]) if losses else 0.0
        # 자리마다 같은 무게입니다. 작은 익절 자리와 큰 손절 자리를 똑같이 세는
        # 것은 기대값 정의의 한계이고, 이 티켓이 고치는 병(분할매도 지배) 과는
        # 다른 문제라 그대로 뒀습니다.
        rep.expectancy = statistics.fmean([t.pnl_pct for t in trips])
        rep.best_trade = max(t.pnl_pct for t in trips)
        rep.worst_trade = min(t.pnl_pct for t in trips)
        rep.avg_hold_hours = statistics.fmean([t.hold_hours for t in trips])
        rep.longest_win_streak, rep.longest_loss_streak = _streaks(trips)
        # 회전율만 조각 단위 그대로입니다 — "돈이 얼마나 오갔나" 를 묻는
        # 지표라, 나눠 판 체결도 각각 실제로 오간 명목금액입니다.
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
