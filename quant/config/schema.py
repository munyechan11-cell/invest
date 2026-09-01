"""Declarative configuration.

A whole trading system — universe, alpha stack, sizing, risk, costs, venue — is
one YAML file. That matters operationally: the config is the artifact you
version, diff, and attach to a set of results, so "which exact system produced
this equity curve" always has an answer.
"""
from __future__ import annotations

import difflib
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quant.core.types import RunMode, timeframe_seconds


class ConfigBlock(BaseModel):
    """Base for every config section: a key it does not recognise is an error.

    Pydantic's default is to drop unknown keys, which turns a one-character typo
    into a silent deletion — `rsik:` removes every stop and kill-switch, and the
    run proceeds as if the operator had asked for none. Nothing downstream can
    detect that, because by then the block simply is not there.
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _reject_unknown_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        known = sorted(cls.model_fields)
        unknown = [k for k in data if k not in cls.model_fields]
        if not unknown:
            return data
        named = []
        for key in unknown:
            near = difflib.get_close_matches(str(key), known, n=1, cutoff=0.6)
            named.append(f"{key!r}" + (f" (did you mean {near[0]!r}?)" if near else ""))
        raise ValueError(
            f"unknown config key{'s' if len(named) > 1 else ''}: {', '.join(named)}. "
            f"valid keys here: {known}"
        )


class ModelSpec(ConfigBlock):
    """`{type: ..., params: {...}}` — one entry per pluggable model."""

    type: str
    params: dict[str, Any] = Field(default_factory=dict)


class SymbolSpec(ConfigBlock):
    ticker: str
    venue: str = "SIM"
    asset_class: str = "equity"
    quote_currency: str = "USD"
    lot_size: float = 1.0
    tick_size: float = 0.01
    min_notional: float = 0.0
    multiplier: float = 1.0


class DataConfig(ConfigBlock):
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


class FlowConfig(ConfigBlock):
    """Investor supply/demand feed (수급) — KIS or Toss, for Korean equities.

    Kept separate from `data` because flow is a different feed with a different
    cadence and different failure modes: losing it should degrade the signal,
    not stop the bars arriving.

    두 소스는 같은 자료가 아닙니다. 토스는 거래**량**만 주고(금액 축 없음)
    `foreigner` 가 등록외국인만이라, 한 종목의 시계열을 둘로 이어 붙이면
    경계에서 계단이 생깁니다 — 자세한 것은 `providers/toss_flow.py`.
    """

    provider: str = "none"          # none | kis | toss | synthetic
    params: dict[str, Any] = Field(default_factory=dict)
    history_sessions: int = 120
    refresh_every_bars: int = 1


class UniverseConfig(ConfigBlock):
    """What to trade.

    `symbols` alone is a fixed book. Add `filters` to narrow it each cycle, or
    switch `source` to `exchange` to start from every market the venue lists.
    The chain only ever removes, so a filter can never introduce an instrument
    the source did not offer.
    """

    symbols: list[SymbolSpec] = Field(default_factory=list)
    source: ModelSpec = Field(default_factory=lambda: ModelSpec(type="static"))
    filters: list[ModelSpec] = Field(default_factory=list)
    refresh_every_bars: int = 24
    benchmark: str | None = None
    #: kept for older configs; a bare selection spec is treated as the source
    selection: ModelSpec | None = None

    @model_validator(mode="after")
    def _fold_legacy_selection(self) -> UniverseConfig:
        if self.selection is not None and self.source.type == "static":
            self.source = self.selection
        return self


class CostConfig(ConfigBlock):
    preset: Literal["crypto_spot", "us_equity", "kr_equity", "zero_cost", "custom"] = "us_equity"
    fee: ModelSpec | None = None
    slippage: ModelSpec | None = None
    fill: ModelSpec | None = None
    #: 매도 증권거래세율 (bps) — `preset: kr_equity` 의 기본값을 덮어쓴다.
    #: 법정 세율은 한 번의 백테스트 구간 안에서도 여러 차례 바뀌었으므로,
    #: 프리셋에 박힌 하나의 값은 구간의 일부에서만 맞다.
    sell_tax_bps: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _sell_tax_belongs_to_kr_preset(self) -> CostConfig:
        if self.sell_tax_bps is not None and self.preset != "kr_equity":
            raise ValueError(
                f"costs.sell_tax_bps applies to preset: kr_equity, not "
                f"{self.preset!r}. With preset: custom, set the rate on the "
                "sell-side model instead: fee: {type: SideAwareFeeModel, params: "
                "{base: {type: KoreanEquityFeeModel, params: {commission_bps: 1.5}}, "
                "sell_extra: {type: KoreanEquitySellTax, params: {sell_tax_bps: 15.0}}}}"
            )
        return self


class PortfolioConfig(ConfigBlock):
    starting_cash: float = 100_000.0
    base_currency: str = "USD"
    model: ModelSpec = Field(default_factory=lambda: ModelSpec(type="vol_target"))
    max_position_weight: float = 0.25
    max_gross_leverage: float = 1.0
    cash_reserve_pct: float = 0.02
    allow_short: bool = False
    min_trade_weight: float = 0.005


class RiskConfig(ConfigBlock):
    models: list[ModelSpec] = Field(default_factory=list)
    protections: list[ModelSpec] = Field(default_factory=list)


class ExecutionConfig(ConfigBlock):
    model: ModelSpec = Field(default_factory=lambda: ModelSpec(type="immediate"))
    min_order_notional: float = 10.0


class BrokerConfig(ConfigBlock):
    type: Literal["paper", "ccxt", "kis", "alpaca", "toss"] = "paper"
    params: dict[str, Any] = Field(default_factory=dict)
    #: hard ceiling on a single order's notional; the last line of defence
    #: between a sizing bug and the account
    max_order_notional: float = 10_000.0
    #: refuse to place any live order at all until this is explicitly true
    live_trading_confirmed: bool = False


class LimitsConfig(ConfigBlock):
    """하루 거래 한도 — the caps that assume the strategy is misbehaving.

    Every other limit here is a strategy limit and assumes it is not. These sit
    at the brokerage, below everything, and see the actual orders. Zero means
    "no cap", but leaving all four at zero on a live account is a decision, not
    a default.
    """

    max_daily_notional: float = Field(default=0.0, allow_inf_nan=False)
    # 하루 총 거래대금 한도
    max_daily_orders: int = 0           # 하루 주문 건수 한도
    max_daily_loss: float = Field(default=0.0, allow_inf_nan=False)
    # 하루 실현손실 한도 (통화 단위)
    max_daily_loss_pct: float = Field(default=0.0, allow_inf_nan=False)
    # 하루 실현손실 한도 (자산 대비 비율)
    #: when "today" rolls over. Defaults to KST so the reset does not land in
    #: the middle of the KRX session it is meant to be bounding.
    timezone_offset_hours: float = Field(
        default=9.0, ge=-24.0, le=24.0, allow_inf_nan=False,
    )
    halt_until_next_day: bool = True


class BacktestConfig(ConfigBlock):
    start: datetime | None = None
    end: datetime | None = None
    #: how many parameter variants were evaluated — feeds the deflated Sharpe
    trials: int = 1
    #: 시행별 1기간 샤프의 표본분산. Deflated Sharpe 의 기준선을 정합니다.
    #: 탐색이 끝나야 알 수 있으므로 hyperopt 가 승자를 다시 돌릴 때 채웁니다.
    #: 비어 있으면 Lo(2002) 의 점근분산으로 대신합니다 — 예전처럼 1.0 을
    #: 가정하면 기준선이 연 샤프 49 가 되어 지표가 통째로 0 이 됩니다.
    variance_of_trials: float | None = None
    risk_free_rate: float = 0.0


class NotifyConfig(ConfigBlock):
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    on_events: list[str] = Field(
        default_factory=lambda: ["order_filled", "trade_closed", "protection", "error"]
    )


class StrategyConfig(ConfigBlock):
    name: str = "unnamed"
    #: 화면에 뜨는 한국어 이름. 비면 `name` 을 그대로 씁니다.
    #:
    #: `name` 은 설정 파일과 로그가 쓰는 식별자라 영어로 둡니다. 그런데 전략을
    #: 고르는 화면에 `kr-toss-desk · dry_run` 만 뜨면, 그게 뭔지 이미 아는
    #: 사람만 자기 돈을 넣을 수 있습니다.
    #:
    #: 여기에 모드("과거 검증용" 같은 말)를 적지 마세요. 아래 `mode` 를 바꾸는
    #: 순간 이 문자열은 조용히 거짓말이 되고, 화면은 그 거짓말을 자신 있게
    #: 띄웁니다. 모드는 `mode` 하나에서만 읽습니다.
    label_ko: str = ""
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
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)

    @model_validator(mode="after")
    def _live_needs_confirmation(self) -> StrategyConfig:
        if self.mode is RunMode.LIVE and not self.broker.live_trading_confirmed:
            raise ValueError(
                "mode: live requires broker.live_trading_confirmed: true. "
                "Run in dry_run first and read the results."
            )
        if self.mode is RunMode.LIVE and self.broker.type == "paper":
            raise ValueError("mode: live with broker.type: paper is contradictory")
        if self.mode is RunMode.LIVE and not (
            self.limits.max_daily_notional or self.limits.max_daily_orders
            or self.limits.max_daily_loss or self.limits.max_daily_loss_pct
        ):
            raise ValueError(
                "mode: live requires at least one daily cap under `limits:` "
                "(max_daily_notional / max_daily_orders / max_daily_loss / "
                "max_daily_loss_pct). A bug in a signal costs whatever you let it."
            )
        return self

    @model_validator(mode="after")
    def _warmup_must_cover_the_age_filter(self) -> StrategyConfig:
        """워밍업이 age 필터가 요구하는 봉 수를 담는가.

        담지 못하면 아무 오류 없이 유니버스가 비고, 봇은 종목 하나 없이 돌면서
        화면에는 "대기 중" 만 남습니다. 실제로 그렇게 하루를 잃었습니다 —
        키도 맞고 시세도 오는데 볼 종목이 없었습니다.

        시작을 막는 이유는, 이 조합이 "덜 좋은 설정" 이 아니라 **절대 매매하지
        않는 설정** 이기 때문입니다. 돌려 봐야 알 수 있는 것이 아닙니다.
        """
        wanted = max((int(f.params.get("min_bars", 0) or 0)
                      for f in self.universe.filters if f.type == "age"), default=0)
        if not wanted:
            return self
        seconds = timeframe_seconds(self.data.timeframe)
        if seconds < 86400:
            # 분봉은 하루에 몇 개가 들어오는지가 장 길이에 달려 있어서 날짜를
            # 세는 방식으로는 알 수 없습니다. 잘못 세느니 안 세는 편이 낫습니다.
            return self
        try:
            calendar = self._calendar()
        except KeyError as exc:
            # 오타 난 캘린더 이름. pydantic 은 ValueError 만 감싸므로 그대로
            # 두면 검증이 아니라 500 으로 터집니다.
            raise ValueError(str(exc)) from None

        from datetime import date as _date
        from datetime import timedelta as _td

        cursor = _date(2025, 1, 1)
        end = cursor + self.warmup_delta
        sessions = 0
        while cursor <= end:
            sessions += bool(calendar.sessions_on(cursor))
            cursor += _td(days=1)
        # 거래일이 아니라 **봉** 을 셉니다. 주봉이면 5거래일이 봉 하나입니다 —
        # 여기서 거래일을 세면 주봉 설정을 안전하다고 도장 찍어 주면서 실제로는
        # 유니버스가 비는, 가드가 오히려 거짓 보증을 하는 상태가 됩니다.
        bars = int(sessions / max(seconds / 86400.0, 1.0))
        if bars >= wanted:
            return self

        # 권고값은 **봉 개수** 입니다. warmup_delta 가 달력 환산을 이미 하므로
        # 여기서 또 곱하면 두 배로 잡으라고 안내하게 됩니다.
        raise ValueError(
            f"warmup_bars: {self.data.warmup_bars} 로는 {calendar.name} 에서 약 "
            f"{bars}봉밖에 받지 못하는데 age 필터가 {wanted}봉을 요구합니다 — "
            f"모든 종목이 걸러져 유니버스가 비고, 봇은 아무것도 하지 않으면서 "
            f"오류도 내지 않습니다. warmup_bars 를 {wanted + 20} 이상으로 "
            f"올리거나 age.min_bars 를 낮추세요."
        )

    @model_validator(mode="after")
    def _backtest_needs_the_simulator(self) -> StrategyConfig:
        # A file that declares both is asking for something impossible: a venue
        # adapter has no fill simulation, so the run places orders that never
        # fill, charges no commission and no 거래세, and reports a flat curve.
        # (Forcing backtest onto a dry_run/live file from the CLI is a different
        # thing — build_brokerage substitutes the simulator there, on purpose.)
        if self.mode is RunMode.BACKTEST and self.broker.type != "paper":
            raise ValueError(
                f"mode: backtest with broker.type: {self.broker.type} — a venue "
                "adapter cannot simulate fills, so the backtest would end with "
                "zero trades and zero cost. Use broker.type: paper to backtest, "
                "or mode: dry_run to run against the venue."
            )
        return self

    @property
    def warmup_delta(self) -> timedelta:
        """워밍업에 필요한 **달력** 시간.

        `warmup_bars` 는 봉 개수인데 시세 조회는 기간으로 합니다. 일봉에서 이
        둘을 1:1 로 놓으면 조용히 모자랍니다 — KRX 는 달력 260일에 장이 173번
        서므로, "260봉" 을 요청하면 173봉이 옵니다. 200봉을 요구하는 age 필터가
        그 위에 있으면 유니버스가 통째로 비고, 봇은 종목 하나 없이 돌면서
        화면에는 "대기 중" 만 띄웁니다. 아무 데도 오류가 안 뜹니다.

        분봉은 다릅니다 — 하루에 몇 개가 들어오는지가 장 길이에 달려 있어서
        같은 방식으로 나눌 수 없습니다. 그쪽은 지금처럼 두고, 실제로 물린
        일봉만 캘린더로 환산합니다.
        """
        seconds = timeframe_seconds(self.data.timeframe)
        if seconds < 86400:
            return timedelta(seconds=seconds * self.data.warmup_bars)
        # 봉 하나가 며칠치인가 — 일봉 1, 주봉 5(거래일). 이걸 넘기지 않으면
        # 주봉 전략의 창이 5분의 1로 줄어듭니다.
        bar_days = seconds / 86400.0
        return self._calendar().calendar_span(self.data.warmup_bars, bar_days)

    def _calendar(self):
        """이 설정이 실제로 쓸 캘린더. `build_calendar` 과 같은 규칙입니다."""
        from quant.data.calendar import calendar_for_venue, create_calendar

        if self.data.calendar != "auto":
            return create_calendar(self.data.calendar)
        if not self.universe.symbols:
            return create_calendar("always_open")
        first = self.universe.symbols[0]
        return calendar_for_venue(first.venue, getattr(first, "asset_class", "") or "")
