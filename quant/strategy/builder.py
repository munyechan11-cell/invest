"""Assembles a runnable `Engine` from a `StrategyConfig`.

One place where every string in the YAML becomes a real object, with clear
errors when a name is wrong. Nothing else in the codebase does name→class
lookup, so adding a model means registering it here and nowhere else.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from quant.alpha.base import AlphaModel, CompositeAlphaModel
from quant.alpha.technical import (
    CrossSectionalMomentumAlpha,
    DonchianBreakoutAlpha,
    EmaCrossAlpha,
    MacdAlpha,
    PairsTradingAlpha,
    RsiMeanReversionAlpha,
    SqueezeBreakoutAlpha,
    TrendRegimeFilter,
)
from quant.brokerage.base import Brokerage
from quant.brokerage.paper import PaperBrokerage
from quant.config.schema import ModelSpec, StrategyConfig, SymbolSpec
from quant.core.account import Portfolio
from quant.core.clock import Clock, RealClock, SimClock
from quant.core.context import Context
from quant.core.engine import Engine
from quant.core.events import EventBus
from quant.core.types import UTC, AssetClass, RunMode, Symbol
from quant.data.flow import FlowFeed, NullFlowProvider, create_flow_provider
from quant.data.provider import CachingProvider, DataProvider, create_provider
from quant.execution.costs import PRESETS, FeeModel, FillModel, SlippageModel
from quant.execution.models import BUILTIN_EXECUTION_MODELS
from quant.portfolio.base import PortfolioConstructionModel
from quant.portfolio.models import BUILTIN_PORTFOLIO_MODELS
from quant.risk.base import RiskManagementModel
from quant.risk.models import BUILTIN_RISK_MODELS, TradingLockGate
from quant.risk.protections import BUILTIN_PROTECTIONS, ProtectionManager

# import for side effects: each module registers its providers
from quant.data.providers import local as _local_providers  # noqa: F401

log = logging.getLogger("quant.builder")

BUILTIN_ALPHA_MODELS = {
    "ema_cross": EmaCrossAlpha,
    "macd": MacdAlpha,
    "rsi_reversion": RsiMeanReversionAlpha,
    "donchian_breakout": DonchianBreakoutAlpha,
    "squeeze": SqueezeBreakoutAlpha,
    "xs_momentum": CrossSectionalMomentumAlpha,
    "pairs": PairsTradingAlpha,
    "regime_filter": TrendRegimeFilter,
}

#: alpha models that need the shared investor-flow feed injected
FLOW_ALPHA_MODELS = {"investor_flow", "retail_contrarian"}


def _resolve(registry: dict, spec: ModelSpec, kind: str):
    cls = registry.get(spec.type)
    if cls is None:
        raise KeyError(
            f"unknown {kind} model {spec.type!r}; available: {sorted(registry)}"
        )
    try:
        return cls(**spec.params)
    except TypeError as exc:
        raise TypeError(f"{kind} model {spec.type!r}: {exc}") from exc


def build_symbol(spec: SymbolSpec) -> Symbol:
    return Symbol(
        ticker=spec.ticker,
        venue=spec.venue,
        asset_class=AssetClass(spec.asset_class),
        quote_currency=spec.quote_currency,
        lot_size=Decimal(str(spec.lot_size)),
        tick_size=Decimal(str(spec.tick_size)),
        min_notional=Decimal(str(spec.min_notional)),
        multiplier=Decimal(str(spec.multiplier)),
    )


def build_data_provider(config: StrategyConfig) -> DataProvider:
    if config.data.provider in ("ccxt",):
        from quant.data.providers import ccxt_provider as _c  # noqa: F401
    elif config.data.provider in ("yahoo",):
        from quant.data.providers import yahoo as _y  # noqa: F401
    elif config.data.provider in ("kis",):
        from quant.data.providers import kis as _k  # noqa: F401
    provider = create_provider(config.data.provider, **config.data.params)
    return CachingProvider(provider) if config.data.cache else provider


def build_flow_feed(config: StrategyConfig) -> FlowFeed:
    """One feed per engine, shared by every model that reads 수급.

    Sharing matters: without it a bar costs one flow fetch per consumer, and
    KIS rate-limits hard enough that three consumers would be three times the
    chance of a throttled cycle.
    """
    if config.flow.provider in ("kis",):
        from quant.data.providers import kis_flow as _kf  # noqa: F401
    elif config.flow.provider in ("synthetic",):
        from quant.data.providers import synthetic_flow as _sf  # noqa: F401
    try:
        provider = create_flow_provider(config.flow.provider, **config.flow.params)
    except Exception as exc:
        log.warning("flow provider %r unavailable (%s) — continuing without 수급 data",
                    config.flow.provider, exc)
        provider = NullFlowProvider(str(exc))
    return FlowFeed(
        provider,
        history=config.flow.history_sessions,
        refresh_every_bars=config.flow.refresh_every_bars,
        live=config.mode is not RunMode.BACKTEST,
    )


def build_costs(config: StrategyConfig) -> tuple[FeeModel, SlippageModel, FillModel | None]:
    from quant.execution import costs as cost_mod

    if config.costs.preset != "custom":
        fee, slip = PRESETS[config.costs.preset]()
    else:
        fee = _resolve_cost(cost_mod, config.costs.fee, "fee")
        slip = _resolve_cost(cost_mod, config.costs.slippage, "slippage")
    fill = _resolve_cost(cost_mod, config.costs.fill, "fill") if config.costs.fill else None
    return fee, slip, fill


def _resolve_cost(module, spec: ModelSpec | None, kind: str):
    if spec is None:
        raise ValueError(f"costs.preset is 'custom' but costs.{kind} is missing")
    cls = getattr(module, spec.type, None)
    if cls is None:
        raise KeyError(f"unknown {kind} model {spec.type!r} in quant.execution.costs")
    return cls(**spec.params)


def build_portfolio_model(config: StrategyConfig) -> PortfolioConstructionModel:
    spec = config.portfolio.model
    params = {
        "max_position_weight": config.portfolio.max_position_weight,
        "max_gross_leverage": config.portfolio.max_gross_leverage,
        "cash_reserve_pct": config.portfolio.cash_reserve_pct,
        "allow_short": config.portfolio.allow_short,
        "min_trade_weight": config.portfolio.min_trade_weight,
        **spec.params,
    }
    return _resolve(BUILTIN_PORTFOLIO_MODELS, ModelSpec(type=spec.type, params=params),
                    "portfolio")


def build_risk_models(config: StrategyConfig) -> list[RiskManagementModel]:
    models = [_resolve(BUILTIN_RISK_MODELS, s, "risk") for s in config.risk.models]
    # The lock gate must run last so it can veto anything an earlier model
    # produced; append it automatically whenever protections are configured.
    if config.risk.protections and not any(isinstance(m, TradingLockGate) for m in models):
        models.append(TradingLockGate())
    return models


def build_protections(config: StrategyConfig) -> ProtectionManager:
    return ProtectionManager(
        *[_resolve(BUILTIN_PROTECTIONS, s, "protection") for s in config.risk.protections]
    )


def build_alpha(config: StrategyConfig, flow_feed: FlowFeed | None = None) -> AlphaModel:
    if not config.alpha:
        raise ValueError("no alpha models configured — the engine has nothing to trade on")
    models: list[AlphaModel] = []
    for spec in config.alpha:
        if spec.type == "desk":
            models.append(_build_desk(spec, flow_feed))
        elif spec.type == "council":
            models.append(_build_council(spec))
        elif spec.type == "pairs":
            models.append(_build_pairs(spec))
        elif spec.type in FLOW_ALPHA_MODELS:
            models.append(_build_flow_alpha(spec, flow_feed))
        else:
            models.append(_resolve(BUILTIN_ALPHA_MODELS, spec, "alpha"))
    return models[0] if len(models) == 1 else CompositeAlphaModel(*models)


def _build_flow_alpha(spec: ModelSpec, flow_feed: FlowFeed | None):
    from quant.alpha.flow import InvestorFlowAlpha, RetailContrarianAlpha

    if flow_feed is None:
        raise ValueError(
            f"alpha {spec.type!r} needs investor-flow data — set flow.provider "
            f"(e.g. 'kis' for Korean equities, 'synthetic' to try it out)"
        )
    cls = {"investor_flow": InvestorFlowAlpha,
           "retail_contrarian": RetailContrarianAlpha}[spec.type]
    return cls(feed=flow_feed, **spec.params)


def _build_desk(spec: ModelSpec, flow_feed: FlowFeed | None):
    from quant.alpha.desk import TradingDesk
    from quant.alpha.llm_client import LLMConfig

    params = dict(spec.params)
    llm = LLMConfig(**params.pop("llm", {}))
    decision_raw = params.pop("decision_llm", None)
    decision = LLMConfig(**decision_raw) if decision_raw else None
    return TradingDesk(llm, decision_llm=decision, flow_feed=flow_feed, **params)


def _build_council(spec: ModelSpec):
    from quant.alpha.council import ResearchCouncilAlpha
    from quant.alpha.llm_client import LLMConfig

    params = dict(spec.params)
    llm = LLMConfig(**params.pop("llm", {}))
    return ResearchCouncilAlpha(llm, **params)


def _build_pairs(spec: ModelSpec):
    params = dict(spec.params)
    raw_pairs = params.pop("pairs", [])
    pairs = [
        (build_symbol(SymbolSpec(**a)), build_symbol(SymbolSpec(**b)))
        for a, b in raw_pairs
    ]
    return PairsTradingAlpha(pairs=pairs, **params)


def build_brokerage(config: StrategyConfig, portfolio: Portfolio,
                    fee: FeeModel, slippage: SlippageModel,
                    fill: FillModel | None) -> Brokerage:
    if config.broker.type == "paper":
        return PaperBrokerage(
            portfolio, fee_model=fee, slippage_model=slippage, fill_model=fill,
            allow_short=config.portfolio.allow_short, run_mode=config.mode,
            **config.broker.params,
        )
    if config.broker.type == "ccxt":
        from quant.brokerage.ccxt_broker import CcxtBrokerage

        return CcxtBrokerage(
            portfolio, max_order_notional=config.broker.max_order_notional,
            live=config.mode is RunMode.LIVE, **config.broker.params,
        )
    if config.broker.type == "kis":
        from quant.brokerage.kis_broker import KisBrokerage

        return KisBrokerage(
            portfolio, max_order_notional=config.broker.max_order_notional,
            live=config.mode is RunMode.LIVE, **config.broker.params,
        )
    if config.broker.type == "alpaca":
        from quant.brokerage.alpaca_broker import AlpacaBrokerage

        return AlpacaBrokerage(
            portfolio, max_order_notional=config.broker.max_order_notional,
            live=config.mode is RunMode.LIVE, **config.broker.params,
        )
    raise KeyError(f"unknown brokerage {config.broker.type!r}")


def build_engine(
    config: StrategyConfig,
    clock: Clock | None = None,
    bus: EventBus | None = None,
    portfolio: Portfolio | None = None,
) -> tuple[Engine, DataProvider]:
    """Returns the assembled engine and the data provider that feeds it."""
    portfolio = portfolio or Portfolio(
        config.portfolio.starting_cash, config.portfolio.base_currency
    )
    if clock is None:
        clock = (
            SimClock(config.backtest.start or datetime.now(UTC) - timedelta(days=365))
            if config.mode is RunMode.BACKTEST else RealClock()
        )
    bus = bus or EventBus()
    ctx = Context(
        clock=clock, portfolio=portfolio, bus=bus,
        timeframe=config.data.timeframe, run_mode=config.mode,
    )
    ctx.universe = [build_symbol(s) for s in config.universe.symbols]
    if config.universe.benchmark:
        ctx.benchmark = Symbol(config.universe.benchmark, venue=config.data.provider)

    flow_feed = build_flow_feed(config)
    fee, slippage, fill = build_costs(config)
    execution = _resolve(
        BUILTIN_EXECUTION_MODELS,
        ModelSpec(type=config.execution.model.type,
                  params={"min_order_notional": config.execution.min_order_notional,
                          **config.execution.model.params}),
        "execution",
    )
    engine = Engine(
        ctx=ctx,
        alpha=build_alpha(config, flow_feed),
        portfolio_model=build_portfolio_model(config),
        execution_model=execution,
        brokerage=build_brokerage(config, portfolio, fee, slippage, fill),
        risk_models=build_risk_models(config),
        protections=build_protections(config),
    )
    # Carried on the engine so the backtest runner and the live loop can
    # backfill it without threading another return value through everything.
    engine.flow_feed = flow_feed
    return engine, build_data_provider(config)
