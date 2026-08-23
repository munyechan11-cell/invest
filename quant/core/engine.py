"""The engine: one bar-processing pipeline used by backtest, dry run and live.

Per bar batch, in this exact order:

    1. resting orders fill against the *new* bar   (they were placed last bar)
    2. positions are marked, closed trades booked
    3. protections evaluate the fresh trade history and may set locks
    4. alpha models produce insights
    5. portfolio construction turns insights into target quantities
    6. risk models shrink or veto those targets
    7. execution diffs targets vs holdings and emits orders
    8. orders are submitted — to rest until the next bar

Step 1 preceding steps 4-8 is the whole no-look-ahead story: a decision made on
bar *t* can only ever be filled using data from bar *t+1*.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Sequence

from quant.alpha.attribution import InsightLedger
from quant.alpha.base import AlphaModel, InsightCollection
from quant.brokerage.base import Brokerage
from quant.brokerage.paper import PaperBrokerage
from quant.core.context import Context
from quant.core.events import EventBus, EventType
from quant.core.types import (
    Bar,
    ClosedTrade,
    Fill,
    Insight,
    Order,
    OrderStatus,
    PortfolioTarget,
    Symbol,
)
from quant.execution.base import ExecutionModel
from quant.live.limits import TradingBudget
from quant.live.manual import ManualControl
from quant.portfolio.base import PortfolioConstructionModel
from quant.risk.base import CompositeRiskModel, RiskManagementModel
from quant.risk.protections import ProtectionManager

log = logging.getLogger("quant.engine")


class Engine:
    def __init__(
        self,
        ctx: Context,
        alpha: AlphaModel,
        portfolio_model: PortfolioConstructionModel,
        execution_model: ExecutionModel,
        brokerage: Brokerage,
        risk_models: Sequence[RiskManagementModel] = (),
        protections: ProtectionManager | None = None,
        insight_decay: float = 0.5,
        budget: TradingBudget | None = None,
        manual: ManualControl | None = None,
    ):
        self.ctx = ctx
        self.alpha = alpha
        self.portfolio_model = portfolio_model
        self.execution_model = execution_model
        self.brokerage = brokerage
        self.risk = CompositeRiskModel(*risk_models)
        self.protections = protections or ProtectionManager()
        self.insights = InsightCollection(insight_decay)
        self.ledger = InsightLedger(benchmark=ctx.benchmark)
        self.budget = budget or TradingBudget()
        # Every budget call that omits `now` must read simulated time, not
        # the machine's — see TradingBudget.clock.
        self.budget.clock = ctx.clock
        self.manual = manual or ManualControl()
        brokerage.budget = self.budget
        brokerage.portfolio = ctx.portfolio
        self.bars_processed = 0
        self.orders: list[Order] = []
        self.protection_events: list[dict] = []
        self._started = False

    @property
    def bus(self) -> EventBus:
        return self.ctx.bus

    # ── lifecycle ────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._started:
            return
        await self.brokerage.connect()
        await self.alpha.on_start(self.ctx)
        self._started = True
        await self.bus.publish(EventType.STATE, {"state": "started",
                                                 "mode": self.ctx.run_mode.value})

    async def stop(self) -> None:
        for order in await self.brokerage.open_orders():
            await self.brokerage.cancel(order)
        await self.brokerage.close()
        self._started = False
        await self.bus.publish(EventType.STATE, {"state": "stopped"})

    def set_universe(self, symbols: list[Symbol]) -> None:
        previous = {s.key: s for s in self.ctx.universe}
        current = {s.key: s for s in symbols}
        added = [s for k, s in current.items() if k not in previous]
        removed = [s for k, s in previous.items() if k not in current]
        self.ctx.universe = list(symbols)
        if added or removed:
            self.alpha.on_universe_changed(self.ctx, added, removed)

    # ── the pipeline ─────────────────────────────────────────────────────
    async def on_bars(self, bars: dict[str, Bar], ts: datetime | None = None) -> None:
        if not bars:
            return
        ctx = self.ctx
        bar_ts = ts or max(b.end_ts for b in bars.values())
        if hasattr(ctx.clock, "set"):
            ctx.clock.set(bar_ts)

        # 1 — fills first, using the bar that just closed
        fills = await self._settle(bars)

        # 2 — mark the book. History is kept for *every* symbol in the batch,
        # not just the active universe: a name that drops out of the universe
        # and later re-enters needs an unbroken series, or its indicators warm
        # up from scratch and its first signals are noise.
        for bar in bars.values():
            ctx.push_bar(bar)
            ctx.portfolio.mark(bar.symbol, bar.close)
        ctx.portfolio.record_equity(bar_ts)
        await self.bus.publish(EventType.EQUITY, ctx.portfolio.snapshot())

        self.bars_processed += 1

        # 3 — protections react to the trade history including this bar's exits
        events = self.protections.apply(ctx)
        if events:
            self.protection_events.extend(events)
            for e in events:
                await self.bus.publish(EventType.PROTECTION, e)

        self.budget.roll(bar_ts, ctx.equity)

        if self.manual.paused:
            # Paused means "open nothing new". Risk-driven exits and the
            # operator's own orders keep flowing — a pause that traps the book
            # is a worse tool than no pause at all.
            await self._refresh_pending()
            exits = self.risk.manage(ctx, [])
            await self._submit(self.execution_model.execute(ctx, exits)
                               + self.manual.build_orders(ctx))
            return

        # 4 — alpha, over the active universe only
        active = self._active(bars)
        try:
            fresh = await self.alpha.update(ctx, active)
        except Exception:
            log.exception("alpha layer failed on %s", bar_ts)
            fresh = []
        if fresh:
            self.insights.add(fresh)
            self.ledger.record(ctx, fresh)
            for ins in fresh:
                await self.bus.publish(EventType.INSIGHT, _insight_dict(ins))
        self.insights.expire(ctx.now)
        # Settle anything whose horizon has now fully elapsed. Scoring happens
        # strictly after the fact — the ledger never sees an insight's future.
        self.ledger.settle(ctx)

        # 4.5 — refresh projected holdings so execution diffs against the
        # position we will have, not the one we have now
        await self._refresh_pending()

        # 5 — portfolio construction
        try:
            targets = self.portfolio_model.create_targets(ctx, self.insights.active(ctx.now))
        except Exception:
            log.exception("portfolio construction failed on %s", bar_ts)
            return

        # 6 — risk
        proposed = {t.symbol.key: t.quantity for t in targets}
        targets = self.risk.manage(ctx, targets)

        # A risk model that flattens a position must also cancel the insight
        # that asked for it. Otherwise the insight is still active next bar,
        # the portfolio model rebuilds the same target, and the strategy
        # oscillates in and out paying the spread each way — the stop appears
        # to "not work" when in fact it works every single bar.
        for t in targets:
            held = ctx.portfolio.quantity(t.symbol)
            # A symbol the portfolio model left out of the batch is one it was
            # content to hold as-is — so the baseline is the current position,
            # not "nothing". Reading a missing target as zero would make every
            # deadbanded hold look like a liquidation.
            was = proposed.get(t.symbol.key, held)
            if was == 0:
                continue
            if t.quantity == 0 or abs(t.quantity) < abs(was):
                self.insights.clear(t.symbol)
                await self.bus.publish(EventType.RISK_ACTION, {
                    "symbol": t.symbol.ticker,
                    "proposed": float(was),
                    "allowed": float(t.quantity),
                    "reason": t.tag,
                    "insights_cancelled": True,
                })

        for t in targets:
            await self.bus.publish(EventType.TARGET, _target_dict(t))

        # 7/8 — execution
        try:
            orders = self.execution_model.execute(ctx, targets)
        except Exception:
            log.exception("execution model failed on %s", bar_ts)
            return
        # Manual orders bypass alpha, portfolio and universe — that is the point
        # — but not the brokerage guard rails below.
        await self._submit(orders + self.manual.build_orders(ctx))

        _ = fills  # already published in _settle

    async def _submit(self, orders: list[Order]) -> None:
        for order in orders:
            submitted = await self.brokerage.submit(order)
            self.orders.append(submitted)
            if submitted.status is OrderStatus.REJECTED:
                await self.bus.publish(EventType.ORDER_REJECTED, _order_dict(submitted))
            else:
                await self.bus.publish(EventType.ORDER_SUBMITTED, _order_dict(submitted))

    async def _refresh_pending(self) -> None:
        from decimal import Decimal

        pending: dict[str, Decimal] = {}
        try:
            for order in await self.brokerage.open_orders():
                signed = order.remaining * order.side.sign
                pending[order.symbol.key] = pending.get(order.symbol.key,
                                                        Decimal("0")) + signed
        except Exception:
            log.exception("could not read resting orders — sizing off filled position")
            pending = {}
        self.ctx.set_pending(pending)

    def _active(self, bars: dict[str, Bar]) -> dict[str, Bar]:
        """The subset of this batch the models are allowed to act on.

        An empty universe means "no restriction" so a strategy that never
        configures universe selection behaves exactly as before.
        """
        if not self.ctx.universe:
            return bars
        keys = {s.key for s in self.ctx.universe}
        return {k: b for k, b in bars.items() if k in keys}

    async def _settle(self, bars: dict[str, Bar]) -> list[Fill]:
        """Fill resting orders and book the resulting position changes."""
        fills: list[Fill] = []
        if isinstance(self.brokerage, PaperBrokerage):
            seen_rejections = len(self.brokerage.rejections)
            for bar in bars.values():
                fills.extend(self.brokerage.process_bar(bar, self.ctx.quote(bar.symbol)))
            # Orders can also be rejected *during* a fill attempt (cash ran out
            # between submission and execution); surface those too.
            for rejection in self.brokerage.rejections[seen_rejections:]:
                await self.bus.publish(EventType.ORDER_REJECTED, rejection)
        else:
            fills.extend(await _drain_live_fills(self.brokerage))

        for fill in fills:
            closed = self.ctx.portfolio.apply_fill(fill)
            await self.bus.publish(EventType.ORDER_FILLED, _fill_dict(fill))
            self.budget.record_fill(fill)
            if closed is not None:
                self.budget.record_trade(closed.pnl)
                self.risk.on_trade_closed(self.ctx, closed)
                await self.bus.publish(EventType.TRADE_CLOSED, _trade_dict(closed))
        return fills

    # ── reporting ────────────────────────────────────────────────────────
    def summary(self) -> dict:
        return {
            "bars_processed": self.bars_processed,
            "orders": len(self.orders),
            "rejected": sum(1 for o in self.orders if o.status is OrderStatus.REJECTED),
            "active_insights": len(self.insights),
            "attribution": self.ledger.report(),
            "protection_events": len(self.protection_events),
            "locks": self.ctx.active_locks(),
            "pinned": self.ctx.pinned,
            "budget": self.budget.status() if self.budget.configured else None,
            "manual": self.manual.status(),
            "portfolio": self.ctx.portfolio.snapshot(),
        }


async def _drain_live_fills(brokerage: Brokerage) -> list[Fill]:
    getter = getattr(brokerage, "poll_fills", None)
    if getter is None:
        return []
    return await getter()


# ── serialisation helpers (used by the event stream and the API) ─────────
def _insight_dict(i: Insight) -> dict:
    return {
        "id": i.id, "symbol": i.symbol.ticker, "venue": i.symbol.venue,
        "direction": i.direction.name, "confidence": round(i.confidence, 3),
        "magnitude": i.magnitude, "source": i.source, "tag": i.tag,
        "generated_at": i.generated_at.isoformat(), "closes_at": i.close_time.isoformat(),
    }


def _target_dict(t: PortfolioTarget) -> dict:
    return {"symbol": t.symbol.ticker, "quantity": float(t.quantity),
            "tag": t.tag, "source": t.source}


def _order_dict(o: Order) -> dict:
    return {
        "id": o.id, "symbol": o.symbol.ticker, "side": o.side.value,
        "quantity": float(o.quantity), "type": o.type.value,
        "limit_price": o.limit_price, "status": o.status.value, "tag": o.tag,
        "reject_reason": o.reject_reason, "created_at": o.created_at.isoformat(),
    }


def _fill_dict(f: Fill) -> dict:
    return {
        "order_id": f.order_id, "symbol": f.symbol.ticker, "side": f.side.value,
        "quantity": float(f.quantity), "price": f.price, "fee": round(f.fee, 4),
        "slippage_bps": round(f.slippage * 10_000, 2), "liquidity": f.liquidity,
        "ts": f.ts.isoformat(),
    }


def _trade_dict(t: ClosedTrade) -> dict:
    return {
        "symbol": t.symbol.ticker, "side": t.side.value, "quantity": float(t.quantity),
        "entry_price": t.entry_price, "exit_price": t.exit_price,
        "pnl": round(t.pnl, 2), "pnl_pct": round(t.pnl_pct * 100, 3),
        "fees": round(t.fees, 4), "entry_ts": t.entry_ts.isoformat(),
        "exit_ts": t.exit_ts.isoformat(), "duration_hours": round(
            t.duration.total_seconds() / 3600, 2),
        "exit_tag": t.exit_tag,
    }
