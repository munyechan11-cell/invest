"""Portfolio state: cash, positions, equity curve, closed-trade ledger.

This is the single source of truth the portfolio-construction, risk and
protection layers all read from. It is deliberately *not* a database model —
persistence is a separate concern (`quant.live.state`), so a backtest never
touches disk.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from quant.core.types import (
    ClosedTrade,
    Fill,
    OrderSide,
    Position,
    Symbol,
)


@dataclass
class EquityPoint:
    ts: datetime
    equity: float
    cash: float
    exposure: float          # gross market value / equity
    drawdown: float          # fraction below high-water mark


class Portfolio:
    """Cash + positions + history for one trading account."""

    def __init__(self, starting_cash: float, base_currency: str = "USD"):
        self.starting_cash = float(starting_cash)
        self.cash = float(starting_cash)
        self.base_currency = base_currency
        self.positions: dict[str, Position] = {}
        self.closed_trades: list[ClosedTrade] = []
        self.equity_curve: list[EquityPoint] = []
        self.high_water_mark = float(starting_cash)
        self.total_fees = 0.0
        # entry context kept per symbol so a round trip can be reconstructed
        self._entry: dict[str, tuple[datetime, float, str]] = {}

    # ── position access ──────────────────────────────────────────────────
    def position(self, symbol: Symbol) -> Position:
        pos = self.positions.get(symbol.key)
        if pos is None:
            pos = Position(symbol=symbol)
            self.positions[symbol.key] = pos
        return pos

    def quantity(self, symbol: Symbol) -> Decimal:
        pos = self.positions.get(symbol.key)
        return pos.quantity if pos else Decimal("0")

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if not p.is_flat]

    @property
    def open_symbols(self) -> list[Symbol]:
        return [p.symbol for p in self.open_positions]

    # ── valuation ────────────────────────────────────────────────────────
    @property
    def holdings_value(self) -> float:
        return sum(p.market_value for p in self.open_positions)

    @property
    def gross_exposure(self) -> float:
        return sum(abs(p.market_value) for p in self.open_positions)

    @property
    def net_exposure(self) -> float:
        return sum(p.market_value for p in self.open_positions)

    @property
    def equity(self) -> float:
        return self.cash + self.holdings_value

    @property
    def leverage(self) -> float:
        eq = self.equity
        return self.gross_exposure / eq if eq > 0 else 0.0

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.open_positions)

    @property
    def total_return(self) -> float:
        return self.equity / self.starting_cash - 1.0 if self.starting_cash else 0.0

    @property
    def drawdown(self) -> float:
        if self.high_water_mark <= 0:
            return 0.0
        return max(0.0, 1.0 - self.equity / self.high_water_mark)

    def free_cash(self, reserve_pct: float = 0.0) -> float:
        """Cash available for new entries after holding back a reserve."""
        return max(0.0, self.cash - self.equity * reserve_pct)

    # ── mutation ─────────────────────────────────────────────────────────
    def mark(self, symbol: Symbol, price: float) -> None:
        pos = self.positions.get(symbol.key)
        if pos is not None and not pos.is_flat:
            pos.mark(price)

    def apply_fill(self, fill: Fill) -> ClosedTrade | None:
        """Settle a fill against cash and the position book.

        Returns a ClosedTrade whenever the fill takes a position back to flat
        (or flips it), which is what the protections and analytics layers key on.
        """
        pos = self.position(fill.symbol)
        was_flat = pos.is_flat
        prior_qty = pos.quantity
        prior_avg = pos.avg_price
        prior_dir = pos.direction

        notional = fill.notional * float(fill.symbol.multiplier)
        self.cash -= notional * fill.side.sign
        self.cash -= fill.fee
        self.total_fees += fill.fee

        pos.apply(fill)

        if was_flat and not pos.is_flat:
            self._entry[fill.symbol.key] = (fill.ts, fill.price, "")

        closed: ClosedTrade | None = None
        crossed_zero = prior_dir != 0 and (pos.is_flat or pos.direction != prior_dir)
        if crossed_zero:
            entry_ts, entry_px, entry_tag = self._entry.get(
                fill.symbol.key, (fill.ts, prior_avg, "")
            )
            closed_qty = min(abs(fill.quantity), abs(prior_qty))
            gross = float(closed_qty) * (fill.price - prior_avg) * prior_dir
            gross *= float(fill.symbol.multiplier)
            basis = abs(float(closed_qty) * prior_avg * float(fill.symbol.multiplier))
            closed = ClosedTrade(
                symbol=fill.symbol,
                side=OrderSide.BUY if prior_dir > 0 else OrderSide.SELL,
                quantity=closed_qty,
                entry_price=prior_avg,
                exit_price=fill.price,
                entry_ts=entry_ts,
                exit_ts=fill.ts,
                pnl=gross - fill.fee,
                pnl_pct=(gross - fill.fee) / basis if basis > 0 else 0.0,
                fees=fill.fee,
                entry_tag=entry_tag,
                exit_tag=fill.tag or fill.liquidity,
            )
            self.closed_trades.append(closed)
            if pos.is_flat:
                self._entry.pop(fill.symbol.key, None)
            else:
                self._entry[fill.symbol.key] = (fill.ts, fill.price, "")

        return closed

    def record_equity(self, ts: datetime) -> EquityPoint:
        eq = self.equity
        self.high_water_mark = max(self.high_water_mark, eq)
        point = EquityPoint(
            ts=ts,
            equity=eq,
            cash=self.cash,
            exposure=self.gross_exposure / eq if eq > 0 else 0.0,
            drawdown=self.drawdown,
        )
        # Collapse duplicate timestamps so a multi-symbol bar batch writes once.
        if self.equity_curve and self.equity_curve[-1].ts == ts:
            self.equity_curve[-1] = point
        else:
            self.equity_curve.append(point)
        return point

    # ── reporting ────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        return {
            "equity": round(self.equity, 2),
            "cash": round(self.cash, 2),
            "holdings_value": round(self.holdings_value, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "total_return_pct": round(self.total_return * 100, 3),
            "drawdown_pct": round(self.drawdown * 100, 3),
            "leverage": round(self.leverage, 3),
            "open_positions": len(self.open_positions),
            "closed_trades": len(self.closed_trades),
            "total_fees": round(self.total_fees, 2),
            "currency": self.base_currency,
            "positions": [
                {
                    "symbol": p.symbol.ticker,
                    "venue": p.symbol.venue,
                    "quantity": float(p.quantity),
                    "avg_price": round(p.avg_price, 6),
                    "last_price": round(p.last_price, 6),
                    "market_value": round(p.market_value, 2),
                    "unrealized_pnl": round(p.unrealized_pnl, 2),
                    "unrealized_pct": round(p.unrealized_pct * 100, 3),
                }
                for p in self.open_positions
            ],
        }

    def returns(self) -> list[float]:
        """Period-over-period simple returns of the equity curve."""
        curve = self.equity_curve
        out: list[float] = []
        for prev, cur in zip(curve, curve[1:]):
            if prev.equity > 0:
                r = cur.equity / prev.equity - 1.0
                out.append(r if math.isfinite(r) else 0.0)
        return out
