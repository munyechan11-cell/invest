"""Alpaca 페이퍼 트레이딩 - AUTO_TRADE_ENABLED=true 일 때만 동작."""
from __future__ import annotations
import os
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce


def _enabled() -> bool:
    return os.environ.get("AUTO_TRADE_ENABLED", "false").lower() == "true"


def _client() -> TradingClient:
    return TradingClient(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
        paper=os.environ.get("ALPACA_PAPER", "true").lower() == "true",
    )


def place(symbol: str, side: str, qty: int) -> dict:
    """side: 'buy' or 'sell'."""
    if not _enabled():
        return {"skipped": True, "reason": "AUTO_TRADE_ENABLED=false"}
    order = _client().submit_order(MarketOrderRequest(
        symbol=symbol.upper(),
        qty=qty,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    ))
    return {"id": str(order.id), "status": str(order.status), "qty": qty, "side": side}
