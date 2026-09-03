"""Assembles a runnable `Engine` from a `StrategyConfig`.

One place where every string in the YAML becomes a real object, with clear
errors when a name is wrong. Nothing else in the codebase does name→class
lookup, so adding a model means registering it here and nowhere else.
"""
from __future__ import annotations

import logging
import os
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
from quant.data.calendar import calendar_for_venue, create_calendar
from quant.data.flow import FlowFeed, NullFlowProvider, create_flow_provider
from quant.data.provider import CachingProvider, DataProvider, create_provider

# import for side effects: each module registers its providers
from quant.data.providers import local as _local_providers  # noqa: F401
from quant.data.universe import (
    BUILTIN_UNIVERSE_FILTERS,
    BUILTIN_UNIVERSE_SOURCES,
    StaticSource,
    UniverseSelector,
)
from quant.execution.costs import PRESETS, FeeModel, FillModel, SlippageModel
from quant.execution.models import BUILTIN_EXECUTION_MODELS
from quant.live.limits import TradingBudget
from quant.live.manual import ManualControl
from quant.portfolio.base import PortfolioConstructionModel
from quant.portfolio.models import BUILTIN_PORTFOLIO_MODELS
from quant.risk.base import RiskManagementModel
from quant.risk.models import BUILTIN_RISK_MODELS, TradingLockGate
from quant.risk.protections import BUILTIN_PROTECTIONS, ProtectionManager

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
    elif config.data.provider in ("toss",):
        from quant.brokerage import toss_broker as _t  # noqa: F401
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
    elif config.flow.provider in ("toss",):
        from quant.data.providers import toss_flow as _tf  # noqa: F401
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


def build_calendar(config: StrategyConfig, universe: list[Symbol]):
    """Resolve the trading calendar, inferring from the venue when set to auto."""
    if config.data.calendar != "auto":
        return create_calendar(config.data.calendar)
    if not universe:
        return create_calendar("always_open")
    first = universe[0]
    calendar = calendar_for_venue(first.venue, first.asset_class.value)
    venues = {s.venue for s in universe}
    if len(venues) > 1:
        log.warning(
            "universe spans %s but one calendar applies to all of it (%s). "
            "Set data.calendar explicitly, or split into one strategy per venue.",
            sorted(venues), calendar.name,
        )
    return calendar


def build_universe_selector(config: StrategyConfig, symbols: list[Symbol]
                            ) -> UniverseSelector:
    """The universe chain. With no filters configured this is a passthrough."""
    spec = config.universe.source
    if spec.type == "static":
        source = StaticSource(symbols)
    else:
        cls = BUILTIN_UNIVERSE_SOURCES.get(spec.type)
        if cls is None:
            raise KeyError(
                f"unknown universe source {spec.type!r}; "
                f"available: {sorted(BUILTIN_UNIVERSE_SOURCES)}"
            )
        source = cls(**spec.params)
    filters = [
        _resolve(BUILTIN_UNIVERSE_FILTERS, f, "universe filter")
        for f in config.universe.filters
    ]
    return UniverseSelector(
        source, filters,
        refresh_every_bars=config.universe.refresh_every_bars,
        warmup_bars=config.data.warmup_bars,
    )


def build_costs(config: StrategyConfig) -> tuple[FeeModel, SlippageModel, FillModel | None]:
    from quant.execution import costs as cost_mod

    if config.costs.preset != "custom":
        fee, slip = PRESETS[config.costs.preset]()
        if config.costs.sell_tax_bps is not None:
            fee = _with_sell_tax(cost_mod, fee, config.costs.sell_tax_bps)
    else:
        fee = _resolve_cost(cost_mod, config.costs.fee, "fee")
        slip = _resolve_cost(cost_mod, config.costs.slippage, "slippage")
    fill = _resolve_cost(cost_mod, config.costs.fill, "fill") if config.costs.fill else None
    return fee, slip, fill


def _with_sell_tax(module, fee, sell_tax_bps: float):
    """Re-point a preset's sell-side tax at the rate the operator asked for."""
    if not isinstance(fee, module.SideAwareFeeModel):
        raise TypeError(
            "costs.sell_tax_bps needs a side-aware fee model, but this preset "
            f"builds {type(fee).__name__}"
        )
    log.info("매도 거래세 %.2fbp 적용 (프리셋 기본값 대신)", sell_tax_bps)
    return module.SideAwareFeeModel(
        fee.base,
        sell_extra=module.KoreanEquitySellTax(sell_tax_bps=sell_tax_bps),
        buy_extra=fee.buy_extra,
    )


def _resolve_cost(module, spec: ModelSpec | None, kind: str):
    if spec is None:
        raise ValueError(f"costs.preset is 'custom' but costs.{kind} is missing")
    cls = getattr(module, spec.type, None)
    if cls is None:
        raise KeyError(f"unknown {kind} model {spec.type!r} in quant.execution.costs")
    return cls(**{k: _resolve_nested_cost(module, v, kind)
                  for k, v in spec.params.items()})


def _resolve_nested_cost(module, value, kind: str):
    """Turn a `{type: ..., params: {...}}` nested inside params into an instance.

    Composite cost models take other models as arguments — the only way to
    configure a Korean sell tax without editing code. Left unresolved, the dict
    reaches the model as-is and the run dies on its first fill, hours in.
    """
    if isinstance(value, dict) and "type" in value:
        cls = getattr(module, value["type"], None)
        if cls is None:
            raise KeyError(
                f"unknown {kind} model {value['type']!r} in quant.execution.costs"
            )
        return cls(**{k: _resolve_nested_cost(module, v, kind)
                      for k, v in (value.get("params") or {}).items()})
    return value


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
    # A backtest is a simulation: it must never construct a venue adapter,
    # authenticate, or depend on broker.type. Only dry_run and live get the real
    # adapter. `quant backtest` / `optimize` / `walkforward` force this mode onto
    # whatever config they are handed, including live ones, and a venue adapter
    # there produces zero fills, zero cost and a flat curve rather than a result.
    if config.mode is RunMode.BACKTEST and config.broker.type != "paper":
        log.warning(
            "backtest 모드 — broker.type=%r 대신 시뮬레이션 브로커로 체결합니다 "
            "(해당 어댑터는 dryrun/live 에서만 쓰입니다)",
            config.broker.type,
        )
        return PaperBrokerage(
            portfolio, fee_model=fee, slippage_model=slippage, fill_model=fill,
            allow_short=config.portfolio.allow_short, run_mode=config.mode,
        )
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
    if config.broker.type == "toss":
        from quant.brokerage.toss_broker import TossBrokerage

        # `fee` 를 그대로 넘깁니다 — 토스 어댑터는 체결 비용을 스스로 계산해야
        # 하는데, 거기서 요율을 따로 정하면 같은 설정의 백테스트와 실거래가
        # 다른 비용을 뭅니다. `costs.sell_tax_bps` 를 실거래에서만 무시하는
        # 것이 가장 비싼 경우입니다.
        return TossBrokerage(
            portfolio, max_order_notional=config.broker.max_order_notional,
            live=config.mode is RunMode.LIVE, fee_model=fee,
            **config.broker.params,
        )
    if config.broker.type == "alpaca":
        from quant.brokerage.alpaca_broker import AlpacaBrokerage

        return AlpacaBrokerage(
            portfolio, max_order_notional=config.broker.max_order_notional,
            live=config.mode is RunMode.LIVE, **config.broker.params,
        )
    raise KeyError(f"unknown brokerage {config.broker.type!r}")


def apply_investor_profile(config: StrategyConfig,
                           path: str | None = None) -> StrategyConfig:
    """Fill in whatever the config left at its defaults from the saved profile.

    Explicit config always wins. A questionnaire quietly overwriting a value the
    operator chose deliberately is not help, it is a bug with a friendly face.

    A caller that names a path gets that path. The environment is only consulted
    when nobody named one — the profile belongs to a person, and once several
    people share a process, a process-global pointer to "the profile" is one
    interleaving away from sizing one user's positions off another's answers.
    """
    from quant.live.profile import ProfileStore, apply_profile

    store = ProfileStore(path or os.environ.get("QUANT_PROFILE_FILE",
                                                "investor_profile.json"))
    profile = store.load()
    if not profile.completed:
        return config
    tuned, touched = apply_profile(config, profile)
    if touched:
        log.info("투자 성향 '%s' 적용: %s", profile.name, ", ".join(touched))
    return tuned


def build_engine(
    config: StrategyConfig,
    clock: Clock | None = None,
    bus: EventBus | None = None,
    portfolio: Portfolio | None = None,
    brokerage: Brokerage | None = None,
) -> tuple[Engine, DataProvider]:
    """Returns the assembled engine and the data provider that feeds it.

    `brokerage` exists for the multi-agent case: several agents share one
    account, so each engine must receive a `SleeveBrokerage` view of that
    account rather than construct its own venue adapter. Four adapters on one
    credential would authenticate four times, poll four times against one rate
    budget, and each adopt the same cash as its own.

    It has to be a **constructor argument**, not something assigned afterwards.
    `Engine.__init__` wires `brokerage.budget`, `brokerage.portfolio` and
    `ctx.brokerage` from whatever it is given; replacing the attribute later
    leaves the sleeve's budget and book unset and leaves `ctx.brokerage`
    pointing at the account adapter, which is a path around every guarantee the
    sleeve exists to make.
    """
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
    calendar = build_calendar(config, ctx.universe)
    fee, slippage, fill = build_costs(config)
    execution = _resolve(
        BUILTIN_EXECUTION_MODELS,
        ModelSpec(type=config.execution.model.type,
                  params={"min_order_notional": config.execution.min_order_notional,
                          **config.execution.model.params}),
        "execution",
    )
    # Two sources set the same cap: the YAML, and the env vars the setup screen
    # writes so a cap entered in the UI survives a restart without rewriting the
    # operator's commented YAML. Whichever is tighter wins, and the loser is
    # named in the log. Letting either side silently raise the other's ceiling
    # is the one outcome nobody asks for: both numbers were typed by someone who
    # meant them as a maximum. Zero still means "no cap", so it never competes.
    def _cap(configured: float, env_var: str) -> float:
        try:
            from_env = float(os.environ.get(env_var, "") or 0)
        except ValueError:
            log.warning("%s 값이 숫자가 아닙니다 — 무시합니다", env_var)
            from_env = 0.0
        candidates = [v for v in (configured, from_env) if v]
        if not candidates:
            return 0.0
        chosen = min(candidates)
        if configured and from_env and configured != from_env:
            log.warning(
                "%s 한도: 설정 파일 %s, 설정 화면 %s — 더 낮은 %s 를 적용합니다",
                env_var, f"{configured:,.10g}", f"{from_env:,.10g}", f"{chosen:,.10g}",
            )
        return chosen

    budget = TradingBudget(
        max_daily_notional=_cap(config.limits.max_daily_notional,
                                "QUANT_LIMIT_DAILY_NOTIONAL"),
        max_daily_orders=int(_cap(config.limits.max_daily_orders,
                                  "QUANT_LIMIT_DAILY_ORDERS")),
        max_daily_loss=_cap(config.limits.max_daily_loss, "QUANT_LIMIT_DAILY_LOSS"),
        max_daily_loss_pct=_cap(config.limits.max_daily_loss_pct,
                                "QUANT_LIMIT_DAILY_LOSS_PCT"),
        timezone_offset_hours=config.limits.timezone_offset_hours,
        halt_until_next_day=config.limits.halt_until_next_day,
    )
    engine = Engine(
        ctx=ctx,
        alpha=build_alpha(config, flow_feed),
        portfolio_model=build_portfolio_model(config),
        execution_model=execution,
        brokerage=brokerage or build_brokerage(config, portfolio, fee, slippage, fill),
        risk_models=build_risk_models(config),
        protections=build_protections(config),
        budget=budget,
        manual=ManualControl(),
    )
    # Carried on the engine so the backtest runner and the live loop can
    # backfill it without threading another return value through everything.
    engine.flow_feed = flow_feed
    engine.calendar = calendar
    engine.universe_selector = build_universe_selector(config, ctx.universe)
    return engine, build_data_provider(config)
