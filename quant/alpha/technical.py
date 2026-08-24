"""Rule-based alpha models.

Each one is a distinct, well-documented edge hypothesis rather than an
indicator soup: trend-following (Donchian, EMA, MACD), mean-reversion (RSI,
Bollinger), cross-sectional momentum, volatility-squeeze breakout, and
statistical-arbitrage pairs. They are meant to be *combined* — their errors are
much less correlated than their signals.
"""
from __future__ import annotations

import math
import statistics

from quant.alpha.base import AlphaModel
from quant.core.context import Context
from quant.core.types import Bar, Direction, Insight, Symbol, periods_per_year
from quant.indicators.streaming import (
    ADX,
    ATR,
    EMA,
    MACD,
    RSI,
    SMA,
    BollingerBands,
    Donchian,
    IndicatorSet,
    Keltner,
    RollingReturn,
    RollingVolatility,
)


def _correlation(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation. Returns None when either series is constant."""
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    xs, ys = xs[-n:], ys[-n:]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx <= 0 or sy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


class IndicatorAlphaModel(AlphaModel):
    """Base that owns one `IndicatorSet` per symbol and keeps it fed.

    Priming from `ctx.history()` on first sight means a model added mid-session
    (or a symbol entering the universe) is immediately warm instead of blind for
    its whole warm-up window.
    """

    def __init__(self) -> None:
        self._sets: dict[str, IndicatorSet] = {}

    def build(self, ctx: Context, symbol: Symbol) -> IndicatorSet:  # pragma: no cover
        raise NotImplementedError

    def indicators(self, ctx: Context, symbol: Symbol) -> IndicatorSet:
        key = symbol.key
        iset = self._sets.get(key)
        if iset is None:
            iset = self.build(ctx, symbol)
            history = ctx.history(symbol)
            if history:
                iset.prime(history[:-1])   # last bar arrives through update()
            self._sets[key] = iset
        return iset

    def on_universe_changed(self, ctx, added, removed):
        for sym in removed:
            self._sets.pop(sym.key, None)

    async def update(self, ctx, bars):
        out: list[Insight] = []
        for _key, bar in bars.items():
            iset = self.indicators(ctx, bar.symbol)
            iset.update(bar)
            if not iset.is_ready:
                continue
            ins = self.signal(ctx, bar, iset)
            if ins is not None:
                out.extend(ins if isinstance(ins, list) else [ins])
        return out

    def signal(self, ctx: Context, bar: Bar, ind: IndicatorSet):  # pragma: no cover
        raise NotImplementedError


class EmaCrossAlpha(IndicatorAlphaModel):
    """Classic dual-EMA trend follower, gated by ADX.

    The ADX gate is what separates this from the textbook version: an EMA cross
    in a range-bound market is noise, and filtering by trend strength removes
    most of the whipsaw losses that make the naive version unprofitable.
    """

    name = "ema_cross"

    def __init__(self, fast: int = 20, slow: int = 60, adx_min: float = 20.0,
                 hold_bars: int = 10):
        super().__init__()
        self.fast, self.slow, self.adx_min, self.hold = fast, slow, adx_min, hold_bars
        self.warmup_bars = slow + 30

    def build(self, ctx, symbol):
        return IndicatorSet(fast=EMA(self.fast), slow=EMA(self.slow), adx=ADX(14),
                            atr=ATR(14))

    def signal(self, ctx, bar, ind):
        fast, slow, adx = ind.fast.value, ind.slow.value, ind.adx.value
        pf, ps = ind.fast.previous, ind.slow.previous
        if None in (fast, slow, pf, ps):
            return None
        crossed_up = pf <= ps and fast > slow
        crossed_down = pf >= ps and fast < slow
        if not (crossed_up or crossed_down):
            return None
        trending = (adx or 0) >= self.adx_min
        if not trending:
            return None
        spread = abs(fast - slow) / bar.close if bar.close else 0.0
        conf = min(0.95, 0.45 + (adx - self.adx_min) / 100.0 + spread * 4)
        atr_pct = ind.atr.percent_of(bar.close)
        return self._insight(
            ctx, bar.symbol,
            Direction.UP if crossed_up else Direction.DOWN,
            period=ctx.bar_delta * self.hold,
            confidence=conf,
            magnitude=atr_pct * 2.5,
            tag=f"EMA{self.fast}/{self.slow} cross, ADX {adx:.0f}",
            adx=round(adx, 2), atr_pct=round(atr_pct, 5),
        )


class MacdAlpha(IndicatorAlphaModel):
    """MACD histogram zero-cross, sized by histogram slope."""

    name = "macd"

    def __init__(self, fast: int = 12, slow: int = 26, signal_period: int = 9,
                 hold_bars: int = 8):
        super().__init__()
        self.f, self.s, self.sig, self.hold = fast, slow, signal_period, hold_bars
        self.warmup_bars = slow + signal_period + 10

    def build(self, ctx, symbol):
        return IndicatorSet(macd=MACD(self.f, self.s, self.sig), atr=ATR(14))

    def signal(self, ctx, bar, ind):
        macd: MACD = ind.macd  # type: ignore[assignment]
        if not (macd.crossed_up or macd.crossed_down):
            return None
        strength = abs(macd.histogram or 0) / bar.close if bar.close else 0.0
        return self._insight(
            ctx, bar.symbol,
            Direction.UP if macd.crossed_up else Direction.DOWN,
            period=ctx.bar_delta * self.hold,
            confidence=min(0.9, 0.5 + strength * 50),
            magnitude=ind.atr.percent_of(bar.close) * 2,
            tag="MACD histogram cross",
        )


class RsiMeanReversionAlpha(IndicatorAlphaModel):
    """Buy oversold / sell overbought — but only against a trend backdrop.

    Naive RSI reversion is a reliable way to catch falling knives. Requiring
    price to sit above a long SMA for longs (and below it for shorts) turns it
    into "buy the dip inside an uptrend", which is a genuinely different and far
    more robust hypothesis.
    """

    name = "rsi_reversion"

    def __init__(self, period: int = 14, oversold: float = 30.0, overbought: float = 70.0,
                 trend_period: int = 100, require_trend: bool = True, hold_bars: int = 5,
                 allow_short: bool = True):
        super().__init__()
        self.period, self.lo, self.hi = period, oversold, overbought
        self.trend_period, self.require_trend = trend_period, require_trend
        self.hold, self.allow_short = hold_bars, allow_short
        self.warmup_bars = max(trend_period, period) + 10

    def build(self, ctx, symbol):
        return IndicatorSet(rsi=RSI(self.period), trend=SMA(self.trend_period),
                            bb=BollingerBands(20, 2.0), atr=ATR(14))

    def signal(self, ctx, bar, ind):
        rsi, trend = ind.rsi.value, ind.trend.value
        if rsi is None or trend is None:
            return None
        above_trend = bar.close > trend
        pct_b = ind.bb.percent_b if ind.bb.percent_b is not None else 0.5

        if rsi <= self.lo and (above_trend or not self.require_trend):
            depth = (self.lo - rsi) / max(self.lo, 1)
            return self._insight(
                ctx, bar.symbol, Direction.UP,
                period=ctx.bar_delta * self.hold,
                confidence=min(0.9, 0.45 + depth * 0.8 + max(0.0, -pct_b) * 0.5),
                magnitude=ind.atr.percent_of(bar.close) * 1.5,
                tag=f"RSI {rsi:.0f} oversold{' in uptrend' if above_trend else ''}",
                rsi=round(rsi, 2), percent_b=round(pct_b, 3),
            )
        if rsi >= self.hi and self.allow_short and (not above_trend or not self.require_trend):
            depth = (rsi - self.hi) / max(100 - self.hi, 1)
            return self._insight(
                ctx, bar.symbol, Direction.DOWN,
                period=ctx.bar_delta * self.hold,
                confidence=min(0.9, 0.45 + depth * 0.8),
                magnitude=ind.atr.percent_of(bar.close) * 1.5,
                tag=f"RSI {rsi:.0f} overbought", rsi=round(rsi, 2),
            )
        return None


class DonchianBreakoutAlpha(IndicatorAlphaModel):
    """Turtle-style channel breakout with an ATR-scaled magnitude.

    The channel excludes the current bar (see `Donchian`), so "close above the
    20-bar high" means a genuine new high rather than a tautology.
    """

    name = "donchian_breakout"

    def __init__(self, entry_period: int = 20, exit_period: int = 10,
                 atr_period: int = 20, min_atr_pct: float = 0.005,
                 hold_bars: int = 20, allow_short: bool = True):
        super().__init__()
        self.entry, self.exit = entry_period, exit_period
        self.atr_period, self.min_atr_pct = atr_period, min_atr_pct
        self.hold, self.allow_short = hold_bars, allow_short
        self.warmup_bars = max(entry_period, atr_period) + 10

    def build(self, ctx, symbol):
        return IndicatorSet(entry=Donchian(self.entry), exit=Donchian(self.exit),
                            atr=ATR(self.atr_period))

    def signal(self, ctx, bar, ind):
        up, dn = ind.entry.upper, ind.entry.lower
        if up is None or dn is None:
            return None
        atr_pct = ind.atr.percent_of(bar.close)
        # A breakout in a dead-flat instrument has no follow-through to capture.
        if atr_pct < self.min_atr_pct:
            return None
        if bar.close > up:
            extension = (bar.close - up) / max(atr_pct * bar.close, 1e-9)
            return self._insight(
                ctx, bar.symbol, Direction.UP,
                period=ctx.bar_delta * self.hold,
                confidence=min(0.92, 0.55 + min(extension, 2.0) * 0.15),
                magnitude=atr_pct * 4,
                tag=f"{self.entry}-bar breakout",
                channel_high=round(up, 6), exit_level=ind.exit.lower,
            )
        if self.allow_short and bar.close < dn:
            extension = (dn - bar.close) / max(atr_pct * bar.close, 1e-9)
            return self._insight(
                ctx, bar.symbol, Direction.DOWN,
                period=ctx.bar_delta * self.hold,
                confidence=min(0.92, 0.55 + min(extension, 2.0) * 0.15),
                magnitude=atr_pct * 4,
                tag=f"{self.entry}-bar breakdown",
                channel_low=round(dn, 6), exit_level=ind.exit.upper,
            )
        return None


class SqueezeBreakoutAlpha(IndicatorAlphaModel):
    """Bollinger-inside-Keltner squeeze, then trade the release direction.

    Volatility is mean-reverting even where price is not; a compression regime
    reliably precedes an expansion, and the momentum sign at release is a
    better-than-coin-flip guess at which way it expands.
    """

    name = "squeeze"

    def __init__(self, bb_period: int = 20, kc_period: int = 20, kc_mult: float = 1.5,
                 momentum_period: int = 12, hold_bars: int = 12):
        super().__init__()
        self.bb_period, self.kc_period, self.kc_mult = bb_period, kc_period, kc_mult
        self.mom_period, self.hold = momentum_period, hold_bars
        self.warmup_bars = max(bb_period, kc_period) + 20
        self._squeezed: dict[str, bool] = {}

    def build(self, ctx, symbol):
        return IndicatorSet(bb=BollingerBands(self.bb_period, 2.0),
                            kc=Keltner(self.kc_period, 10, self.kc_mult),
                            mom=RollingReturn(self.mom_period), atr=ATR(14))

    def signal(self, ctx, bar, ind):
        if None in (ind.bb.upper, ind.kc.upper):
            return None
        in_squeeze = ind.bb.upper < ind.kc.upper and ind.bb.lower > ind.kc.lower
        was = self._squeezed.get(bar.symbol.key, False)
        self._squeezed[bar.symbol.key] = in_squeeze
        if not (was and not in_squeeze):        # only fire on release
            return None
        mom = ind.mom.value or 0.0
        if abs(mom) < 1e-6:
            return None
        return self._insight(
            ctx, bar.symbol,
            Direction.UP if mom > 0 else Direction.DOWN,
            period=ctx.bar_delta * self.hold,
            confidence=min(0.88, 0.5 + min(abs(mom) * 8, 0.35)),
            magnitude=ind.atr.percent_of(bar.close) * 3,
            tag="volatility squeeze release",
            momentum=round(mom, 5),
        )


class CrossSectionalMomentumAlpha(AlphaModel):
    """Rank the universe by risk-adjusted momentum; long the top, short the bottom.

    This is the only model here that is *relative* rather than absolute, and
    that is the point: it stays market-neutral-ish and keeps producing signal in
    regimes where every absolute trend model is flat.

    Momentum is measured over `lookback` bars but skips the most recent
    `skip` bars — the classic 12-1 construction, because the last month of
    returns reverses rather than persists.
    """

    name = "xs_momentum"

    def __init__(self, lookback: int = 126, skip: int = 21, top_n: int = 5,
                 bottom_n: int = 0, min_universe: int = 6, hold_bars: int = 21,
                 rebalance_every: int = 21, vol_adjust: bool = True):
        self.lookback, self.skip = lookback, skip
        self.top_n, self.bottom_n = top_n, bottom_n
        self.min_universe, self.hold = min_universe, hold_bars
        self.rebalance_every, self.vol_adjust = rebalance_every, vol_adjust
        self.warmup_bars = lookback + skip + 5
        self._bar_count = 0

    async def update(self, ctx, bars):
        self._bar_count += 1
        if self._bar_count % max(self.rebalance_every, 1) != 1 and self._bar_count > 1:
            return []
        scores: list[tuple[float, Symbol, float]] = []
        need = self.lookback + self.skip + 1
        for sym in ctx.universe:
            closes = ctx.closes(sym)
            if len(closes) < need:
                continue
            end_idx = len(closes) - 1 - self.skip
            start_idx = end_idx - self.lookback
            past, recent = closes[start_idx], closes[end_idx]
            if past <= 0:
                continue
            raw = recent / past - 1.0
            vol = 1.0
            if self.vol_adjust:
                window = closes[start_idx:end_idx + 1]
                rets = [
                    math.log(b / a) for a, b in zip(window, window[1:]) if a > 0 and b > 0
                ]
                if len(rets) > 2:
                    vol = statistics.pstdev(rets) * math.sqrt(periods_per_year(ctx.timeframe))
                vol = max(vol, 1e-4)
            scores.append((raw / vol, sym, raw))

        if len(scores) < self.min_universe:
            return []
        scores.sort(key=lambda x: x[0], reverse=True)
        out: list[Insight] = []
        period = ctx.bar_delta * self.hold
        span = max(abs(scores[0][0]), abs(scores[-1][0]), 1e-9)

        for rank, (score, sym, raw) in enumerate(scores[: self.top_n]):
            if score <= 0:
                continue
            out.append(self._insight(
                ctx, sym, Direction.UP, period=period,
                confidence=min(0.92, 0.55 + 0.35 * (abs(score) / span)),
                magnitude=abs(raw) * 0.25,
                tag=f"momentum rank {rank + 1}/{len(scores)}",
                score=round(score, 4), raw_return=round(raw, 4),
            ))
        if self.bottom_n:
            for rank, (score, sym, raw) in enumerate(scores[-self.bottom_n:]):
                if score >= 0:
                    continue
                out.append(self._insight(
                    ctx, sym, Direction.DOWN, period=period,
                    confidence=min(0.92, 0.55 + 0.35 * (abs(score) / span)),
                    magnitude=abs(raw) * 0.25,
                    tag=f"momentum rank bottom {rank + 1}",
                    score=round(score, 4),
                ))
        return out


class PairsTradingAlpha(AlphaModel):
    """Z-score reversion on the spread of a correlated pair.

    Correlation is recomputed on a rolling window and the pair is *dropped* when
    it decays below threshold — a broken cointegration relationship is the main
    way stat-arb books blow up.
    """

    name = "pairs"

    def __init__(self, pairs: list[tuple[Symbol, Symbol]], lookback: int = 90,
                 entry_z: float = 2.0, exit_z: float = 0.4, min_corr: float = 0.7,
                 hold_bars: int = 15):
        self.pairs = pairs
        self.lookback, self.entry_z, self.exit_z = lookback, entry_z, exit_z
        self.min_corr, self.hold = min_corr, hold_bars
        self.warmup_bars = lookback + 5

    @staticmethod
    def _log_returns(closes: list[float]) -> list[float]:
        return [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]

    async def update(self, ctx, bars):
        out: list[Insight] = []
        for a, b in self.pairs:
            ca, cb = ctx.closes(a, self.lookback + 1), ctx.closes(b, self.lookback + 1)
            n = min(len(ca), len(cb))
            if n < self.lookback:
                continue
            ca, cb = ca[-n:], cb[-n:]
            ra, rb = self._log_returns(ca), self._log_returns(cb)
            if len(ra) < 10 or len(rb) < 10:
                continue
            corr = _correlation(ra, rb)
            if corr is None:
                continue
            if abs(corr) < self.min_corr:
                continue
            spread = [math.log(x) - math.log(y) for x, y in zip(ca, cb) if x > 0 and y > 0]
            if len(spread) < 10:
                continue
            mu, sd = statistics.fmean(spread), statistics.pstdev(spread)
            if sd <= 1e-9:
                continue
            z = (spread[-1] - mu) / sd
            if abs(z) < self.entry_z:
                continue
            period = ctx.bar_delta * self.hold
            conf = min(0.9, 0.5 + (abs(z) - self.entry_z) * 0.12 + (abs(corr) - self.min_corr))
            long_leg, short_leg = (b, a) if z > 0 else (a, b)
            for sym, direction in ((long_leg, Direction.UP), (short_leg, Direction.DOWN)):
                out.append(self._insight(
                    ctx, sym, direction, period=period, confidence=conf,
                    magnitude=abs(z) * sd * 0.5,
                    tag=f"pair {a.ticker}/{b.ticker} z={z:.2f} corr={corr:.2f}",
                    zscore=round(z, 3), correlation=round(corr, 3),
                ))
        return out


class TrendRegimeFilter(AlphaModel):
    """Not an alpha source — a *veto*.

    Emits FLAT insights for any symbol whose long-term regime contradicts the
    book's directional exposure, and the portfolio model treats a FLAT as
    "close it". Wrapping a trend model in a regime filter is historically the
    single largest drawdown reduction available for a few lines of code.
    """

    name = "regime_filter"

    def __init__(self, period: int = 200, vol_period: int = 20,
                 max_annual_vol: float = 1.2):
        self.period, self.vol_period, self.max_vol = period, vol_period, max_annual_vol
        self.warmup_bars = period + 5
        self._sets: dict[str, IndicatorSet] = {}

    def _ind(self, ctx, symbol) -> IndicatorSet:
        iset = self._sets.get(symbol.key)
        if iset is None:
            iset = IndicatorSet(
                sma=SMA(self.period),
                vol=RollingVolatility(self.vol_period, periods_per_year(ctx.timeframe)),
            )
            hist = ctx.history(symbol)
            if hist:
                iset.prime(hist[:-1])
            self._sets[symbol.key] = iset
        return iset

    async def update(self, ctx, bars):
        out: list[Insight] = []
        for bar in bars.values():
            iset = self._ind(ctx, bar.symbol)
            iset.update(bar)
            if not iset.is_ready:
                continue
            sma, vol = iset.sma.value, iset.vol.value
            below_trend = sma is not None and bar.close < sma
            too_wild = vol is not None and vol > self.max_vol
            if below_trend or too_wild:
                reason = "below 200-period trend" if below_trend else f"vol {vol:.0%} too high"
                out.append(self._insight(
                    ctx, bar.symbol, Direction.FLAT,
                    period=ctx.bar_delta * 3, confidence=0.9,
                    tag=f"regime veto: {reason}",
                ))
        return out
