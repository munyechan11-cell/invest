"""LLM research council — a multi-agent alpha model.

Structure follows the TradingAgents pattern (analysts → bull/bear debate →
research manager → risk panel), adapted to plug into this engine as *just
another* `AlphaModel`: it emits `Insight`s and has no authority over sizing,
stops, or order routing. The rule-based models and the risk layer keep their
veto regardless of how confident the language model sounds.

Two design constraints drive everything here:

1. **No look-ahead.** A language model's weights encode the future relative to
   any historical backtest date. Running the council over history therefore
   produces spectacular, entirely fake results. It is disabled in backtests
   unless you explicitly opt in with a point-in-time news source, and it says so
   loudly.

2. **Cost control.** A full council run is a dozen LLM calls. It runs on a
   cadence, only over a shortlist, and caches by (symbol, bar timestamp), so a
   100-symbol universe does not become a four-figure API bill.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import statistics
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from quant.alpha.base import AlphaModel
from quant.alpha.llm_client import LLMClient, LLMConfig, LLMError
from quant.core.aio import LazySemaphore
from quant.core.context import Context
from quant.core.types import Bar, Direction, Insight, Symbol, periods_per_year
from quant.indicators.streaming import ATR, MACD, RSI, SMA, IndicatorSet, RollingReturn

log = logging.getLogger("quant.alpha.council")

RATING_TO_DIRECTION = {
    "strong_buy": (Direction.UP, 1.0),
    "buy": (Direction.UP, 0.8),
    "hold": (Direction.FLAT, 0.0),
    "sell": (Direction.DOWN, 0.8),
    "strong_sell": (Direction.DOWN, 1.0),
}

_ANALYST_SCHEMA = {
    "type": "object",
    "properties": {
        "stance": {"type": "string", "enum": ["bullish", "neutral", "bearish"]},
        "conviction": {"type": "number", "description": "0.0 to 1.0"},
        "key_points": {"type": "array", "items": {"type": "string"},
                       "description": "2-4 specific, evidence-backed observations"},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["stance", "conviction", "key_points"],
}

_DEBATE_SCHEMA = {
    "type": "object",
    "properties": {
        "argument": {"type": "string", "description": "The case, in 3-6 sentences"},
        "strongest_counterpoint_rebuttal": {"type": "string"},
        "conviction": {"type": "number"},
    },
    "required": ["argument", "conviction"],
}

_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "rating": {"type": "string",
                   "enum": ["strong_buy", "buy", "hold", "sell", "strong_sell"]},
        "conviction": {"type": "number", "description": "0.0 to 1.0"},
        "expected_move_pct": {"type": "number",
                              "description": "Expected % move over the horizon, signed"},
        "horizon_bars": {"type": "integer"},
        "rationale": {"type": "string"},
        "invalidation": {"type": "string",
                         "description": "What observable event would prove this wrong"},
    },
    "required": ["rating", "conviction", "rationale"],
}

_RISK_SCHEMA = {
    "type": "object",
    "properties": {
        "adjusted_conviction": {"type": "number"},
        "position_scale": {"type": "number",
                           "description": "0.0-1.0 multiplier on the proposed size"},
        "veto": {"type": "boolean"},
        "reasoning": {"type": "string"},
    },
    "required": ["adjusted_conviction", "position_scale", "veto", "reasoning"],
}


@dataclass
class CouncilVerdict:
    symbol_key: str
    rating: str
    conviction: float
    expected_move_pct: float
    horizon_bars: int
    rationale: str
    invalidation: str = ""
    position_scale: float = 1.0
    vetoed: bool = False
    analyst_reports: dict = field(default_factory=dict)
    debate: dict = field(default_factory=dict)
    risk_review: dict = field(default_factory=dict)
    elapsed_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol_key, "rating": self.rating,
            "conviction": round(self.conviction, 3),
            "expected_move_pct": round(self.expected_move_pct, 3),
            "horizon_bars": self.horizon_bars, "rationale": self.rationale,
            "invalidation": self.invalidation,
            "position_scale": round(self.position_scale, 3), "vetoed": self.vetoed,
            "analysts": self.analyst_reports, "debate": self.debate,
            "risk": self.risk_review, "elapsed_s": round(self.elapsed_s, 2),
        }


#: Signature for an optional point-in-time context source. It receives the
#: symbol and the "as of" timestamp and must return only information that
#: existed at that moment — this is what makes a historical council run honest.
ContextSource = Callable[[Symbol, "object"], Awaitable[str]]


class ResearchCouncilAlpha(AlphaModel):
    name = "council"

    def __init__(
        self,
        llm: LLMConfig | LLMClient,
        *,
        cadence_bars: int = 5,
        max_symbols_per_run: int = 5,
        debate_rounds: int = 1,
        analysts: Sequence[str] = ("technical", "fundamental", "news", "macro"),
        min_conviction: float = 0.55,
        default_horizon_bars: int = 10,
        allow_in_backtest: bool = False,
        context_source: ContextSource | None = None,
        shortlist: Callable[[Context, dict[str, Bar]], list[Symbol]] | None = None,
        language: str = "en",
        concurrency: int = 4,
    ):
        # duck-typed: an LLMConfig, an LLMClient, or any object exposing
        # `await complete(system, user, schema)` and a `.usage` counter
        self.client = llm if hasattr(llm, "complete") else LLMClient(llm)
        self.cadence = max(cadence_bars, 1)
        self.max_symbols = max_symbols_per_run
        self.debate_rounds = max(debate_rounds, 0)
        self.analysts = list(analysts)
        self.min_conviction = min_conviction
        self.default_horizon = default_horizon_bars
        self.allow_in_backtest = allow_in_backtest
        self.context_source = context_source
        self.shortlist_fn = shortlist
        self.language = language
        self._sem = LazySemaphore(concurrency)
        self._bar_count = 0
        self._cache: dict[str, CouncilVerdict] = {}
        self.verdicts: dict[str, CouncilVerdict] = {}
        self.warmup_bars = 210
        self._sets: dict[str, IndicatorSet] = {}
        self._disabled_reason = ""

    # ── lifecycle ────────────────────────────────────────────────────────
    async def on_start(self, ctx: Context) -> None:
        from quant.core.types import RunMode

        if ctx.run_mode is RunMode.BACKTEST and not self.allow_in_backtest:
            self._disabled_reason = (
                "council disabled in backtest: an LLM's training data postdates historical "
                "bars, so its 'predictions' would be hindsight. Pass allow_in_backtest=True "
                "with a point-in-time context_source if you have one."
            )
            log.warning(self._disabled_reason)
        elif ctx.run_mode is RunMode.BACKTEST and self.context_source is None:
            log.warning(
                "council running in backtest without a point-in-time context_source — "
                "results are optimistically biased and are not evidence of edge."
            )

    # ── deterministic brief (what the agents actually see) ───────────────
    def _indicators(self, ctx: Context, symbol: Symbol) -> IndicatorSet:
        iset = self._sets.get(symbol.key)
        if iset is None:
            iset = IndicatorSet(
                rsi=RSI(14), sma50=SMA(50), sma200=SMA(200), atr=ATR(14),
                macd=MACD(), ret20=RollingReturn(20), ret60=RollingReturn(60),
            )
            hist = ctx.history(symbol)
            if hist:
                iset.prime(hist)
            self._sets[symbol.key] = iset
        return iset

    def _market_brief(self, ctx: Context, symbol: Symbol) -> dict:
        bars = ctx.history(symbol, 260)
        if not bars:
            return {}
        iset = self._indicators(ctx, symbol)
        last = bars[-1]
        closes = [b.close for b in bars]
        rets = [b / a - 1 for a, b in zip(closes, closes[1:]) if a > 0]
        vol_ann = (
            statistics.pstdev(rets) * (periods_per_year(ctx.timeframe) ** 0.5)
            if len(rets) > 5 else 0.0
        )
        avg_vol = statistics.fmean([b.volume for b in bars[-20:]]) if len(bars) >= 20 else 0.0
        pos = ctx.portfolio.positions.get(symbol.key)
        return {
            "symbol": symbol.ticker,
            "venue": symbol.venue,
            "as_of": ctx.now.isoformat(),
            "timeframe": ctx.timeframe,
            "last_close": round(last.close, 6),
            "return_20_bars_pct": round((iset.ret20.value or 0) * 100, 2),
            "return_60_bars_pct": round((iset.ret60.value or 0) * 100, 2),
            "rsi_14": round(iset.rsi.value, 1) if iset.rsi.value else None,
            "sma_50": round(iset.sma50.value, 4) if iset.sma50.value else None,
            "sma_200": round(iset.sma200.value, 4) if iset.sma200.value else None,
            "above_200sma": bool(iset.sma200.value and last.close > iset.sma200.value),
            "macd_histogram": round(iset.macd.histogram, 6) if iset.macd.histogram else None,
            "atr_pct": round(iset.atr.percent_of(last.close) * 100, 2),
            "annualized_vol_pct": round(vol_ann * 100, 1),
            "volume_vs_20d_avg": round(last.volume / avg_vol, 2) if avg_vol > 0 else None,
            "52w_high_distance_pct": round((last.close / max(closes[-252:]) - 1) * 100, 2),
            "52w_low_distance_pct": round((last.close / min(closes[-252:]) - 1) * 100, 2),
            "current_position": {
                "quantity": float(pos.quantity), "unrealized_pct": round(pos.unrealized_pct * 100, 2)
            } if pos and not pos.is_flat else None,
            "portfolio_equity": round(ctx.equity, 2),
            "open_position_count": len(ctx.portfolio.open_positions),
        }

    # ── the council ──────────────────────────────────────────────────────
    async def _run_analyst(self, role: str, brief: dict, external: str) -> dict:
        system = _ANALYST_SYSTEM[role].format(language=_LANG[self.language])
        user = (
            f"Market brief (deterministic, computed from price data — treat as ground truth):\n"
            f"{json.dumps(brief, ensure_ascii=False, indent=2)}\n\n"
        )
        if external:
            user += f"External context available as of {brief.get('as_of')}:\n{external}\n\n"
        user += (
            "Give your read. Be specific and cite numbers from the brief. If the data does "
            "not support a view, say neutral with low conviction rather than inventing one."
        )
        try:
            async with self._sem:
                return await self.client.complete(system, user, _ANALYST_SCHEMA)
        except LLMError as exc:
            log.warning("analyst %s failed: %s", role, exc)
            return {"stance": "neutral", "conviction": 0.0, "key_points": [f"unavailable: {exc}"]}

    async def _debate(self, brief: dict, reports: dict) -> dict:
        history: list[str] = []
        out: dict = {}
        for round_no in range(self.debate_rounds + 1):
            for side in ("bull", "bear"):
                system = _DEBATE_SYSTEM[side].format(language=_LANG[self.language])
                user = (
                    f"Brief:\n{json.dumps(brief, ensure_ascii=False)}\n\n"
                    f"Analyst reports:\n{json.dumps(reports, ensure_ascii=False)}\n\n"
                )
                if history:
                    user += "Debate so far:\n" + "\n\n".join(history[-4:]) + "\n\n"
                user += f"Round {round_no + 1}. Make the {side} case and rebut the other side."
                try:
                    async with self._sem:
                        res = await self.client.complete(system, user, _DEBATE_SCHEMA)
                except LLMError as exc:
                    log.warning("%s debater failed: %s", side, exc)
                    res = {"argument": f"unavailable: {exc}", "conviction": 0.0}
                out[side] = res
                history.append(f"[{side.upper()}] {res.get('argument', '')}")
        return out

    async def _verdict(self, brief: dict, reports: dict, debate: dict) -> dict:
        system = _MANAGER_SYSTEM.format(language=_LANG[self.language],
                                        default_horizon=self.default_horizon)
        user = (
            f"Brief:\n{json.dumps(brief, ensure_ascii=False)}\n\n"
            f"Analyst reports:\n{json.dumps(reports, ensure_ascii=False)}\n\n"
            f"Bull/bear debate:\n{json.dumps(debate, ensure_ascii=False)}\n\n"
            "Decide. Reserve 'hold' for genuinely balanced evidence — a hold on every "
            "name is not risk management, it is abdication. Conviction must reflect how "
            "much the evidence actually supports the call, not how confident you feel."
        )
        async with self._sem:
            return await self.client.complete(system, user, _VERDICT_SCHEMA)

    async def _risk_review(self, brief: dict, verdict: dict) -> dict:
        system = _RISK_SYSTEM.format(language=_LANG[self.language])
        user = (
            f"Brief:\n{json.dumps(brief, ensure_ascii=False)}\n\n"
            f"Proposed decision:\n{json.dumps(verdict, ensure_ascii=False)}\n\n"
            "Stress-test it from an aggressive, a conservative and a neutral seat, then "
            "return the reconciled position scale. Veto only for a concrete, named hazard."
        )
        try:
            async with self._sem:
                return await self.client.complete(system, user, _RISK_SCHEMA)
        except LLMError as exc:
            log.warning("risk panel failed: %s", exc)
            return {"adjusted_conviction": verdict.get("conviction", 0.5),
                    "position_scale": 0.5, "veto": False,
                    "reasoning": f"risk panel unavailable ({exc}); halving size as a precaution"}

    async def deliberate(self, ctx: Context, symbol: Symbol) -> CouncilVerdict | None:
        started = time.monotonic()
        brief = self._market_brief(ctx, symbol)
        if not brief:
            return None
        external = ""
        if self.context_source is not None:
            try:
                external = await self.context_source(symbol, ctx.now)
            except Exception as exc:
                log.warning("context_source failed for %s: %s", symbol.ticker, exc)

        reports = dict(zip(
            self.analysts,
            await asyncio.gather(*(self._run_analyst(a, brief, external) for a in self.analysts)),
        ))
        debate = await self._debate(brief, reports) if self.debate_rounds >= 0 else {}
        try:
            verdict = await self._verdict(brief, reports, debate)
        except LLMError as exc:
            log.warning("council verdict failed for %s: %s", symbol.ticker, exc)
            return None
        risk = await self._risk_review(brief, verdict)

        return CouncilVerdict(
            symbol_key=symbol.key,
            rating=str(verdict.get("rating", "hold")).lower(),
            conviction=float(risk.get("adjusted_conviction", verdict.get("conviction", 0.5)) or 0),
            expected_move_pct=float(verdict.get("expected_move_pct") or 0.0),
            horizon_bars=int(verdict.get("horizon_bars") or self.default_horizon),
            rationale=str(verdict.get("rationale", "")),
            invalidation=str(verdict.get("invalidation", "")),
            position_scale=max(0.0, min(1.0, float(risk.get("position_scale", 1.0) or 0))),
            vetoed=bool(risk.get("veto")),
            analyst_reports=reports,
            debate=debate,
            risk_review=risk,
            elapsed_s=time.monotonic() - started,
        )

    # ── AlphaModel interface ─────────────────────────────────────────────
    def _shortlist(self, ctx: Context, bars: dict[str, Bar]) -> list[Symbol]:
        if self.shortlist_fn is not None:
            return self.shortlist_fn(ctx, bars)[: self.max_symbols]
        # Default: the most *unusual* names — largest absolute 20-bar move,
        # because that is where fresh information is most likely to exist.
        scored: list[tuple[float, Symbol]] = []
        for bar in bars.values():
            iset = self._indicators(ctx, bar.symbol)
            move = abs(iset.ret20.value or 0.0)
            held = 0.5 if ctx.is_invested(bar.symbol) else 0.0   # always re-review holdings
            scored.append((move + held, bar.symbol))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in scored[: self.max_symbols]]

    async def update(self, ctx: Context, bars: dict[str, Bar]) -> list[Insight]:
        if self._disabled_reason:
            return []
        for bar in bars.values():
            self._indicators(ctx, bar.symbol).update(bar)

        self._bar_count += 1
        if (self._bar_count - 1) % self.cadence != 0:
            return []

        targets = self._shortlist(ctx, bars)
        if not targets:
            return []

        results = await asyncio.gather(
            *(self._deliberate_cached(ctx, s) for s in targets), return_exceptions=True
        )

        insights: list[Insight] = []
        for symbol, verdict in zip(targets, results):
            if isinstance(verdict, BaseException) or verdict is None:
                if isinstance(verdict, BaseException):
                    log.warning("council failed for %s: %s", symbol.ticker, verdict)
                continue
            self.verdicts[symbol.key] = verdict

            direction, base = RATING_TO_DIRECTION.get(verdict.rating, (Direction.FLAT, 0.0))
            conviction = verdict.conviction * base * verdict.position_scale
            if verdict.vetoed:
                direction, conviction = Direction.FLAT, 0.9   # flat = close it
            elif conviction < self.min_conviction:
                continue

            insights.append(Insight(
                symbol=symbol,
                direction=direction,
                period=ctx.bar_delta * max(verdict.horizon_bars, 1),
                generated_at=ctx.now,
                magnitude=abs(verdict.expected_move_pct) / 100.0 or None,
                confidence=min(conviction, 0.95),
                source=self.name,
                tag=f"{verdict.rating}: {verdict.rationale[:160]}",
                meta=verdict.to_dict(),
            ))
        return insights

    async def _deliberate_cached(self, ctx: Context, symbol: Symbol) -> CouncilVerdict | None:
        last = ctx.latest(symbol)
        if last is None:
            return None
        key = hashlib.sha1(
            f"{symbol.key}|{last.ts.isoformat()}|{ctx.timeframe}|{self.debate_rounds}".encode()
        ).hexdigest()
        if key in self._cache:
            return self._cache[key]
        verdict = await self.deliberate(ctx, symbol)
        if verdict is not None:
            self._cache[key] = verdict
            if len(self._cache) > 2000:
                for k in list(self._cache)[:1000]:
                    self._cache.pop(k, None)
        return verdict

    @property
    def usage(self):
        return self.client.usage


_LANG = {"en": "English", "ko": "Korean (한국어)", "ja": "Japanese", "zh": "Chinese"}

_ANALYST_SYSTEM = {
    "technical": (
        "You are a senior technical analyst on a systematic trading desk. You read price "
        "structure, trend, momentum and volatility. You are sceptical of indicator "
        "coincidence and you never claim a pattern the numbers do not show. "
        "Write in {language}."
    ),
    "fundamental": (
        "You are a fundamental analyst. From the available context assess valuation, "
        "earnings trajectory, balance-sheet quality and business momentum. If the brief "
        "contains no fundamental data, say so plainly and return neutral with low "
        "conviction rather than speculating. Write in {language}."
    ),
    "news": (
        "You are a news and event analyst. Assess catalysts, their durability, and whether "
        "the move is already priced in. Distinguish a genuine repricing event from noise. "
        "If no news context is supplied, return neutral with low conviction. Write in {language}."
    ),
    "macro": (
        "You are a macro strategist. Assess how the rate, liquidity, currency and sector "
        "backdrop bears on this instrument over the stated horizon. Be explicit about what "
        "you are inferring versus what you observe. Write in {language}."
    ),
    "sentiment": (
        "You are a positioning and sentiment analyst. Assess crowding, flow and whether "
        "sentiment is a contrarian or confirming signal here. Write in {language}."
    ),
}

_DEBATE_SYSTEM = {
    "bull": (
        "You are the bull-side researcher. Argue the strongest honest case for a long "
        "position using the evidence given. Attack the bear case where it is weak, and "
        "concede where it is strong — an argument that concedes nothing is worthless to "
        "the desk. Write in {language}."
    ),
    "bear": (
        "You are the bear-side researcher. Argue the strongest honest case against a long "
        "position, including the case for an outright short. Attack the bull case where it "
        "is weak and concede where it is strong. Write in {language}."
    ),
}

_MANAGER_SYSTEM = (
    "You are the research manager. You weigh the analyst reports and the bull/bear debate "
    "and issue one decision. You are measured on calibration, not enthusiasm: a 0.9 "
    "conviction should be right about nine times in ten. Default horizon is "
    "{default_horizon} bars unless the setup argues otherwise. Always state what would "
    "invalidate the call. Write in {language}."
)

_RISK_SYSTEM = (
    "You are the risk committee, reconciling an aggressive, a conservative and a neutral "
    "seat into one position scale between 0 and 1. Consider volatility, drawdown "
    "potential, concentration, and how much of the thesis rests on inference rather than "
    "observation. Veto only for a concrete named hazard, never for general unease — a "
    "reflexive veto is as costly as a reckless entry. Write in {language}."
)
