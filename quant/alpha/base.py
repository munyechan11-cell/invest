"""Alpha models — the only place in the engine allowed to have an opinion
about direction.

An alpha model turns market state into `Insight`s. It knows nothing about
account size, leverage, or existing positions; that separation (straight from
LEAN) is what lets you swap a rule-based model for an LLM research council
without touching a line of the sizing or execution code.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import timedelta

from quant.core.context import Context
from quant.core.events import EventType
from quant.core.types import Bar, Direction, Insight, Symbol, one_line_error

log = logging.getLogger("quant.alpha")


class AlphaModel(ABC):
    name = "alpha"
    #: bars of history the model needs before its output is trustworthy
    warmup_bars = 0

    async def on_start(self, ctx: Context) -> None:
        return None

    def on_universe_changed(self, ctx: Context, added: list[Symbol], removed: list[Symbol]) -> None:
        return None

    @abstractmethod
    async def update(self, ctx: Context, bars: dict[str, Bar]) -> list[Insight]:
        """Called once per closed bar batch. Return zero or more insights."""

    # -- helpers shared by concrete models -------------------------------
    def _insight(
        self,
        ctx: Context,
        symbol: Symbol,
        direction: Direction,
        period: timedelta | None = None,
        confidence: float = 0.5,
        magnitude: float | None = None,
        tag: str = "",
        **meta,
    ) -> Insight:
        return Insight(
            symbol=symbol,
            direction=direction,
            period=period or ctx.bar_delta * 5,
            generated_at=ctx.now,
            magnitude=magnitude,
            confidence=confidence,
            source=self.name,
            tag=tag,
            meta=meta,
        )


class CompositeAlphaModel(AlphaModel):
    """Runs several alpha models and forwards everything they emit.

    Conflicting insights are *not* resolved here — the portfolio construction
    model nets them. That is deliberate: netting at the alpha layer throws away
    the disagreement signal (two models fighting is itself information).
    """

    name = "composite"

    def __init__(self, *models: AlphaModel):
        if not models:
            raise ValueError("CompositeAlphaModel needs at least one model")
        self.models = list(models)
        self.warmup_bars = max(m.warmup_bars for m in self.models)

    async def on_start(self, ctx):
        for m in self.models:
            await m.on_start(ctx)

    def on_universe_changed(self, ctx, added, removed):
        for m in self.models:
            m.on_universe_changed(ctx, added, removed)

    #: `ctx.state` 의 소유자 이름. 포트폴리오 층이 이 칸을 읽습니다.
    STATE = "alpha"

    async def update(self, ctx, bars):
        """Run every model. A crash is reported, never read as silence.

        Absence of an insight is an instruction here: the portfolio layer turns
        a held symbol with no active view into a zero target, which closes the
        position. That is right when a model looked and had nothing to say. It
        is wrong when the model never got to look — and a model that keeps
        raising will, once its last insight passes its horizon, have every name
        it was responsible for sold at market, paying 거래세 on the way out, for
        a decision nobody made.

        This repository has already lost three days to that shape once. So a
        failure is published where the operator's notifier can see it, and the
        portfolio layer is told the view is incomplete so it holds those names
        instead of closing them.
        """
        out: list[Insight] = []
        failed: list[str] = []
        for model in self.models:
            try:
                out.extend(await model.update(ctx, bars))
            except Exception as exc:
                # One broken model must not silence the rest of the book.
                failed.append(f"{model.name}: {one_line_error(exc)}")
                log.exception("알파 모델 %s 이(가) 이번 봉을 보지 못했습니다", model.name)
        await self._report(ctx, failed)
        return out

    async def _report(self, ctx, failed: list[str]) -> None:
        state = ctx.state(self.STATE)
        was = state.get("degraded") or []
        state["degraded"] = failed
        if not failed:
            if was:
                log.info("알파 모델이 다시 전부 동작합니다")
                await ctx.bus.publish(
                    EventType.ERROR,
                    {"component": "alpha", "recovered": True,
                     "reason": "알파 모델이 다시 전부 동작합니다"},
                    source=self.name,
                )
            return
        reason = ("알파 모델 " + str(len(failed)) + "개가 이번 봉을 보지 못했습니다 — "
                  + "; ".join(failed)
                  + " · 이 모델들이 보던 종목은 **팔지 않고 그대로 둡니다**")
        log.error(reason)
        await ctx.bus.publish(
            EventType.ERROR,
            {"component": "alpha", "failed": failed, "reason": reason},
            source=self.name,
        )


class InsightCollection:
    """Keeps the live set of insights, expiring and decaying them over time.

    Without this, a strategy that fires once and then goes quiet would have its
    stale opinion held forever by the portfolio model.
    """

    def __init__(self, decay_half_life_frac: float = 0.5):
        self._insights: list[Insight] = []
        self.decay = decay_half_life_frac

    def add(self, insights: list[Insight]) -> None:
        for ins in insights:
            # newest insight from a source supersedes its own older one
            self._insights = [
                i for i in self._insights
                if not (i.symbol.key == ins.symbol.key and i.source == ins.source)
            ]
            self._insights.append(ins)

    def expire(self, now) -> list[Insight]:
        expired = [i for i in self._insights if not i.is_active(now)]
        if expired:
            self._insights = [i for i in self._insights if i.is_active(now)]
        return expired

    def active(self, now) -> list[Insight]:
        self.expire(now)
        return list(self._insights)

    def for_symbol(self, symbol: Symbol, now) -> list[Insight]:
        return [i for i in self.active(now) if i.symbol.key == symbol.key]

    def net_score(self, symbol: Symbol, now) -> float:
        """Decay-weighted consensus in [-1, 1] for one symbol."""
        items = self.for_symbol(symbol, now)
        if not items:
            return 0.0
        total = sum(
            int(i.direction) * i.decayed_confidence(now, self.decay) * (1 + abs(i.magnitude or 0))
            for i in items
        )
        weight = sum(i.decayed_confidence(now, self.decay) * (1 + abs(i.magnitude or 0))
                     for i in items)
        return total / weight if weight > 0 else 0.0

    def symbols(self, now) -> list[Symbol]:
        seen: dict[str, Symbol] = {}
        for i in self.active(now):
            seen.setdefault(i.symbol.key, i.symbol)
        return list(seen.values())

    def clear(self, symbol: Symbol | None = None) -> None:
        if symbol is None:
            self._insights.clear()
        else:
            self._insights = [i for i in self._insights if i.symbol.key != symbol.key]

    def __len__(self) -> int:
        return len(self._insights)
