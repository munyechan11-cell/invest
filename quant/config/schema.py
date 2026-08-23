"""Declarative configuration.

A whole trading system — universe, alpha stack, sizing, risk, costs, venue — is
one YAML file. That matters operationally: the config is the artifact you
version, diff, and attach to a set of results, so "which exact system produced
this equity curve" always has an answer.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from quant.core.types import RunMode, timeframe_seconds


class ModelSpec(BaseModel):
    """`{type: ..., params: {...}}` — one entry per pluggable model."""

    type: str
    params: dict[str, Any] = Field(default_factory=dict)


class SymbolSpec(BaseModel):
    ticker: str
    venue: str = "SIM"
    asset_class: str = "equity"
    quote_currency: str = "USD"
    lot_size: float = 1.0
    tick_size: float = 0.01
    min_notional: float = 0.0
    multiplier: float = 1.0


class DataConfig(BaseModel):
    provider: str = "synthetic"
    params: dict[str, Any] = Field(default_factory=dict)
    timeframe: str = "1d"
    warmup_bars: int = 250
    cache: bool = True
    #: trading calendar: always_open (crypto) | krx | us_equity, or "auto" to
    #: infer from the universe's venue. A live bot without one polls a closed
    #: book all night and reports every rejection as an API error.
    calendar: str = "auto"

    @field_validator("timeframe")
    @classmethod
    def _valid_timeframe(cls, v: str) -> str:
        timeframe_seconds(v)      # raises on nonsense
        return v


class FlowConfig(BaseModel):
    """Investor supply/demand feed (수급) — currently KIS for Korean equities.

    Kept separate from `data` because flow is a different feed with a different
    cadence and different failure modes: losing it should degrade the signal,
    not stop the bars arriving.
    """

    provider: str = "none"          # none | kis | synthetic
    params: dict[str, Any] = Field(default_factory=dict)
    history_sessions: int = 120
    refresh_every_bars: int = 1


class UniverseConfig(BaseModel):
    symbols: list[SymbolSpec] = Field(default_factory=list)
    #: optional dynamic selection, e.g. {type: top_volume, params: {n: 20}}
    selection: Optional[ModelSpec] = None
    benchmark: Optional[str] = None


class CostConfig(BaseModel):
    preset: Literal["crypto_spot", "us_equity", "kr_equity", "zero_cost", "custom"] = "us_equity"
    fee: Optional[ModelSpec] = None
    slippage: Optional[ModelSpec] = None
    fill: Optional[ModelSpec] = None


class PortfolioConfig(BaseModel):
    starting_cash: float = 100_000.0
    base_currency: str = "USD"
    model: ModelSpec = Field(default_factory=lambda: ModelSpec(type="vol_target"))
    max_position_weight: float = 0.25
    max_gross_leverage: float = 1.0
    cash_reserve_pct: float = 0.02
    allow_short: bool = False
    min_trade_weight: float = 0.005


class RiskConfig(BaseModel):
    models: list[ModelSpec] = Field(default_factory=list)
    protections: list[ModelSpec] = Field(default_factory=list)


class ExecutionConfig(BaseModel):
    model: ModelSpec = Field(default_factory=lambda: ModelSpec(type="immediate"))
    min_order_notional: float = 10.0


class BrokerConfig(BaseModel):
    type: Literal["paper", "ccxt", "kis", "alpaca"] = "paper"
    params: dict[str, Any] = Field(default_factory=dict)
    #: hard ceiling on a single order's notional; the last line of defence
    #: between a sizing bug and the account
    max_order_notional: float = 10_000.0
    #: refuse to place any live order at all until this is explicitly true
    live_trading_confirmed: bool = False


class BacktestConfig(BaseModel):
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    #: how many parameter variants were evaluated — feeds the deflated Sharpe
    trials: int = 1
    risk_free_rate: float = 0.0


class NotifyConfig(BaseModel):
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    on_events: list[str] = Field(
        default_factory=lambda: ["order_filled", "trade_closed", "protection", "error"]
    )


class StrategyConfig(BaseModel):
    name: str = "unnamed"
    description: str = ""
    mode: RunMode = RunMode.BACKTEST
    data: DataConfig = Field(default_factory=DataConfig)
    flow: FlowConfig = Field(default_factory=FlowConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    alpha: list[ModelSpec] = Field(default_factory=list)
    portfolio: PortfolioConfig = Field(default_factory=PortfolioConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    costs: CostConfig = Field(default_factory=CostConfig)
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)

    @model_validator(mode="after")
    def _live_needs_confirmation(self) -> "StrategyConfig":
        if self.mode is RunMode.LIVE and not self.broker.live_trading_confirmed:
            raise ValueError(
                "mode: live requires broker.live_trading_confirmed: true. "
                "Run in dry_run first and read the results."
            )
        if self.mode is RunMode.LIVE and self.broker.type == "paper":
            raise ValueError("mode: live with broker.type: paper is contradictory")
        return self

    @property
    def warmup_delta(self) -> timedelta:
        return timedelta(seconds=timeframe_seconds(self.data.timeframe)
                         * self.data.warmup_bars)
