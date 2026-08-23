"""수급 알파 — investor-flow signals.

Two hypotheses, both well documented in Korean equity data and both
implementable without a language model:

1. **Persistence.** Foreign and institutional net buying persists over days to
   weeks, because those desks work orders rather than firing them. A streak is
   therefore evidence about tomorrow, not just a description of yesterday.

2. **Divergence.** When smart money accumulates into price weakness — or
   distributes into strength — the flow tends to be the side that is right. The
   divergence case gets the higher conviction here for exactly that reason.

The signal is deliberately scale-free (participation ratios, streaks, z-scores
against the instrument's own history) so the same thresholds work on a ₩600tn
large cap and a small cap.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from quant.alpha.base import AlphaModel
from quant.core.context import Context
from quant.core.types import Bar, Direction, Insight, Symbol
from quant.data.flow import FlowFeed, FlowSummary

log = logging.getLogger("quant.alpha.flow")


class InvestorFlowAlpha(AlphaModel):
    """Trades foreign/institutional accumulation against retail."""

    name = "investor_flow"

    def __init__(
        self,
        feed: FlowFeed,
        window: int = 20,
        min_streak: int = 3,
        min_participation: float = 0.01,
        min_accumulation_days: int = 8,
        divergence_bonus: float = 0.15,
        hold_bars: int = 10,
        allow_short: bool = False,
        refresh_every_bars: int = 1,
    ):
        self.feed = feed
        self.window = window
        self.min_streak = min_streak
        self.min_participation = min_participation
        self.min_accumulation_days = min_accumulation_days
        self.divergence_bonus = divergence_bonus
        self.hold = hold_bars
        self.allow_short = allow_short
        self.refresh_every = max(refresh_every_bars, 1)
        self.warmup_bars = window + 5
        self._bar_count = 0
        self.last_summaries: dict[str, FlowSummary] = {}

    async def on_start(self, ctx: Context) -> None:
        if not self.feed.has_data:
            log.info("flow feed is empty at start — will populate on the first refresh")

    async def update(self, ctx: Context, bars: dict[str, Bar]) -> list[Insight]:
        self._bar_count += 1
        if (self._bar_count - 1) % self.refresh_every == 0:
            # The feed owns the decision about whether a fetch is appropriate at
            # all; in a backtest this returns immediately.
            await self.feed.refresh([b.symbol for b in bars.values()])

        out: list[Insight] = []
        for bar in bars.values():
            summary = self.feed.summary(bar.symbol, ctx.now, self.window)
            if summary is None or summary.window < max(self.min_streak, 5):
                continue
            self.last_summaries[bar.symbol.key] = summary
            insight = self._signal(ctx, bar.symbol, summary)
            if insight is not None:
                out.append(insight)
        return out

    def _signal(self, ctx: Context, symbol: Symbol, s: FlowSummary) -> Insight | None:
        # Volume-normalised participation is the gate: a big share count on a
        # thin day is not the same message as the same count on a heavy one.
        if abs(s.turnover_ratio) < self.min_participation:
            return None

        buying = (
            s.foreign_streak >= self.min_streak
            or s.institution_streak >= self.min_streak
            or s.accumulation_days >= self.min_accumulation_days
        ) and s.smart_money_net_value > 0

        selling = (
            s.foreign_streak <= -self.min_streak
            or s.institution_streak <= -self.min_streak
            or s.distribution_days >= self.min_accumulation_days
        ) and s.smart_money_net_value < 0

        if not (buying or selling):
            return None
        if selling and not self.allow_short:
            # Still worth saying: a FLAT insight is a hard veto downstream, so
            # this closes an existing long rather than doing nothing.
            return self._insight(
                ctx, symbol, Direction.FLAT,
                period=ctx.bar_delta * max(self.hold // 2, 1),
                confidence=0.7,
                tag=f"수급 이탈: 외국인 {s.foreign_streak:+d}일 / 기관 {s.institution_streak:+d}일",
                flow=s.to_dict(),
            )

        streak = max(abs(s.foreign_streak), abs(s.institution_streak))
        both_sides = s.foreign_streak * s.institution_streak > 0     # aligned desks
        conf = 0.45
        conf += min(streak / 12.0, 0.20)
        conf += min(abs(s.turnover_ratio) / 0.05, 1.0) * 0.15
        conf += 0.08 if both_sides else 0.0
        conf += min(abs(s.participation_zscore) / 4.0, 1.0) * 0.07
        if (buying and s.divergence == "bullish_divergence") or \
           (selling and s.divergence == "bearish_divergence"):
            conf += self.divergence_bonus

        direction = Direction.UP if buying else Direction.DOWN
        who = []
        if abs(s.foreign_streak) >= self.min_streak:
            who.append(f"외국인 {s.foreign_streak:+d}일")
        if abs(s.institution_streak) >= self.min_streak:
            who.append(f"기관 {s.institution_streak:+d}일")
        if not who:
            who.append(f"누적 {s.accumulation_days if buying else s.distribution_days}일")

        return self._insight(
            ctx, symbol, direction,
            period=ctx.bar_delta * self.hold,
            confidence=min(conf, 0.93),
            magnitude=min(abs(s.turnover_ratio) * 3, 0.12),
            tag=f"수급 {'유입' if buying else '이탈'}: {', '.join(who)} · {s.divergence}",
            flow=s.to_dict(),
        )


class RetailContrarianAlpha(AlphaModel):
    """Fade extreme retail crowding.

    Separate from `InvestorFlowAlpha` on purpose: this is a *contrarian* claim
    about one group, not a *momentum* claim about another, and keeping them
    apart means you can see which one is actually paying.
    """

    name = "retail_contrarian"

    def __init__(self, feed: FlowFeed, window: int = 20, min_zscore: float = 1.8,
                 hold_bars: int = 8, allow_short: bool = False):
        self.feed = feed
        self.window = window
        self.min_z = min_zscore
        self.hold = hold_bars
        self.allow_short = allow_short
        self.warmup_bars = window + 10

    async def update(self, ctx, bars):
        out: list[Insight] = []
        for bar in bars.values():
            flows = self.feed.get(bar.symbol, ctx.now)
            if len(flows) < self.window:
                continue
            import statistics

            ratios = [f.retail_qty / f.volume if f.volume > 0 else 0.0 for f in flows]
            recent = ratios[-1]
            mean, sd = statistics.fmean(ratios), statistics.pstdev(ratios)
            if sd <= 1e-9:
                continue
            z = (recent - mean) / sd
            if abs(z) < self.min_z:
                continue
            # Heavy retail buying is the bearish side of this trade.
            direction = Direction.DOWN if z > 0 else Direction.UP
            if direction is Direction.DOWN and not self.allow_short:
                direction = Direction.FLAT
            out.append(self._insight(
                ctx, bar.symbol, direction,
                period=ctx.bar_delta * self.hold,
                confidence=min(0.42 + (abs(z) - self.min_z) * 0.12, 0.80),
                tag=f"개인 수급 극단 z={z:+.1f} — 반대 방향",
                retail_zscore=round(z, 2),
            ))
        return out
