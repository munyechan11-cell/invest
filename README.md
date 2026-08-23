# Quant Engine

An event-driven algorithmic trading engine. One pipeline runs a backtest, a
paper session and live execution — the only things that change are which clock
and which brokerage are plugged in.

```
universe → alpha (insights) → portfolio construction (targets)
         → risk (veto/shrink) → execution (orders) → brokerage
```

Rebuilt from scratch on ideas taken from three reference systems:

| Source | What it contributed |
|---|---|
| **QuantConnect LEAN** | The framework split: insights are separate from allocation, which is separate from risk and execution. One engine for backtest and live. |
| **Freqtrade** | The operational layer that decides whether you can actually run a strategy: dry-run parity, protections, pair locks, exchange precision handling, hyperopt with risk-aware losses. |
| **TradingAgents** | The multi-agent LLM research council — analysts, a bull/bear debate, a risk panel — wired in as *one alpha model among several*, with no authority over sizing or execution. |

---

## Quick start

```bash
pip install -r requirements.txt
python -m quant backtest configs/demo.yaml
```

That runs with no API keys at all — synthetic data, simulated broker, realistic
costs. Then:

```bash
python -m quant models                            # every pluggable model
python -m quant validate configs/us_equity.yaml   # check a config without running it
python -m quant backtest configs/us_equity.yaml   # real data, free Yahoo feed
python -m quant serve   configs/demo.yaml         # dashboard on :8000
```

---

## The pipeline

Each closed bar goes through eight steps, in this order:

1. **Resting orders fill** against the new bar — they were placed on the previous one
2. Positions are marked, closed round-trips booked
3. **Protections** evaluate the fresh trade history and may lock symbols
4. **Alpha models** emit `Insight`s — direction, confidence, magnitude, horizon
5. **Portfolio construction** turns insights into absolute target quantities
6. **Risk models** shrink or veto those targets (they can only ever reduce)
7. **Execution** diffs targets against holdings and emits orders
8. Orders are submitted, to rest until the next bar

Step 1 coming before steps 4–8 is the whole no-look-ahead story: a decision made
on bar *t* can only be filled with data from bar *t+1*. The `Context` object
enforces the same rule for history access — `ctx.history()` physically cannot
return a bar that has not closed yet.

---

## Configuration

A whole system is one YAML file. `${VAR}` interpolates from the environment, so
secrets stay out of the file.

```yaml
name: crypto-trend-4h
mode: dry_run                       # backtest | dry_run | live

data:    {provider: ccxt, params: {exchange: binance}, timeframe: 4h, warmup_bars: 400}
universe:
  symbols:
    - {ticker: "BTC/USDT", venue: binance, asset_class: crypto,
       lot_size: 0.00001, tick_size: 0.01, min_notional: 10}

alpha:
  - {type: donchian_breakout, params: {entry_period: 30, hold_bars: 40}}
  - {type: squeeze}
  - {type: regime_filter, params: {period: 200}}      # a veto, not a signal

portfolio: {model: {type: vol_target, params: {target_annual_vol: 0.35}}}
risk:
  models:      [{type: trailing_stop, params: {atr_multiple: 3.5}}]
  protections: [{type: stoploss_guard, params: {trade_limit: 4}}]
execution: {model: {type: limit, params: {offset_bps: 8}}}
costs:     {preset: crypto_spot}
broker:    {type: ccxt, max_order_notional: 3000, live_trading_confirmed: false}
```

Shipped configs: `demo.yaml` (no keys needed), `us_equity.yaml` (Yahoo),
`live_crypto.yaml` (Binance), `kr_equity.yaml` (KIS, 국내 수수료·거래세 반영).

---

## Models

**Alpha** — `ema_cross`, `macd`, `rsi_reversion`, `donchian_breakout`,
`squeeze`, `xs_momentum` (cross-sectional 12-1), `pairs` (stat-arb),
`regime_filter` (veto), `investor_flow` (수급), `retail_contrarian`,
`desk` (16-seat LLM desk), `council` (its 4-seat predecessor).
Combine freely; they are netted at the portfolio layer, and a `FLAT` insight is
a hard veto rather than one vote among many.

**Portfolio** — `equal_weight`, `insight_weight`, `vol_target` (default),
`risk_parity`, `kelly` (fractional), `mean_variance` (shrunk covariance),
`fixed_stake`.

**Risk** — `max_dd_per_security`, `trailing_stop` (ATR-scaled),
`max_dd_portfolio` (kill switch), `vol_cap`, `max_positions`, `sector_cap`,
`time_stop`, `take_profit`, `lock_gate`.

**Protections** (circuit breakers that lock trading after bad outcomes) —
`stoploss_guard`, `cooldown`, `low_profit`, `max_drawdown`.

**Execution** — `immediate`, `limit`, `twap`, `pov` (volume participation),
`std_dev`.

**Brokerages** — `paper` (backtest + dry run), `ccxt` (100+ crypto venues),
`kis` (한국투자증권), `alpaca` (US equities/crypto).

---

## Honest measurement

The engine tries hard not to flatter itself:

- **Fills happen on the next bar**, at its open, capped at a share of its volume.
  Nothing fills at the close of the bar that generated the signal.
- **Costs are on by default** — commission, spread, and √(size/ADV) market
  impact. The `zero_cost` preset exists for unit tests and prints a warning.
- **Deflated Sharpe** discounts for how many parameter sets you tried. Run 200
  variants and the reported figure knows it.
- **Walk-forward with purging** is a first-class command, not an afterthought:

```bash
python -m quant walkforward configs/demo.yaml --space configs/space.yaml \
       --folds 5 --trials 30 --purge 20
```

It fits on each training window, trades the *next* window, and reports
walk-forward efficiency (OOS Sharpe ÷ IS Sharpe). Below ~0.4 it tells you
plainly that you have fitted noise, and the command exits non-zero.

---

## Live trading

Three separate things must be true before real money can move:

1. the config says `mode: live`
2. the config says `broker.live_trading_confirmed: true`
3. you type the strategy name at the CLI prompt (or pass `--yes`)

On top of that, every live order passes through: a per-order notional ceiling,
an orders-per-minute limit, duplicate suppression (a retry after a network
timeout cannot become a second position), and position reconciliation against
the venue on every cycle — where the venue always wins.

```bash
python -m quant dryrun configs/live_crypto.yaml    # live prices, simulated fills
python -m quant live   configs/live_crypto.yaml    # real money
```

Dry run shares the fill, fee and rejection logic with the backtest, so its
results are directly comparable rather than a separate code path that happens to
look similar.

State is persisted to SQLite, so a restart after a deploy or a crash resumes
with its positions rather than buying everything a second time.

---

## 실시간 16인 트레이딩 데스크

`desk` 알파는 매 사이클마다 16개 좌석이 실제로 토론해서 하나의 매매 결정을
만들어냅니다. 구조는 TradingAgents에서, "그 결정이 규칙 기반 리스크 계층 아래에
머무른다"는 점은 LEAN과 Freqtrade에서 가져왔습니다.

```
분석 8석 (동시)  기술적 · 수급 · 펀더멘털 · 뉴스 · 센티먼트 · 매크로 · 미시구조 · 퀀트
      ↓
토론 2석 (N라운드)  강세론자 ⚔ 약세론자      — 서로의 직전 주장을 보고 반박
      ↓
리스크 3석         공격형 ⇄ 보수형 → 중립형이 포지션 배율로 수렴 (또는 거부)
      ↓
결정 3석           리서치 매니저 → 트레이더(체결 계획) → 데스크 헤드(최종 결정)
```

각 좌석은 **자기 몫의 브리프만** 봅니다. 펀더멘털 데이터를 못 보는 좌석은
펀더멘털에 대해 의견을 가질 수 없고, 그것이 프롬프트가 경고하는 실패 모드를
구조적으로 막습니다. 좌석 정의와 프롬프트는 전부
[quant/alpha/seats.py](quant/alpha/seats.py) 한 곳에 있습니다.

데스크의 출력은 다른 알파와 똑같은 `Insight` 하나입니다. 즉 손절, 트레일링 스톱,
프로텍션, 주문 한도는 모델이 아무리 확신에 차 있어도 그대로 적용됩니다.

**회고(memory).** 데스크는 과거 판단을 기억하고, 보유기간이 지나면 실제 가격으로
채점합니다. 확신 0.7 이상 판단의 적중률이 낮으면 다음 심의에서 헤드에게 그 사실을
그대로 알려줍니다 — 프롬프트 체인은 스스로 "이 종목에서 여섯 번 연속 틀렸다"를
알아차릴 수 없기 때문입니다.

**의도적으로 불편한 두 가지:**

- **백테스트에서는 기본 비활성화.** LLM의 학습 데이터는 어떤 과거 봉보다 뒤에
  있습니다. 과거에 돌리면 화려하고 완전히 가짜인 결과가 나옵니다. 켜려면
  `allow_in_backtest: true`가 필요하고, 의미 있으려면 시점 기준 `context_source`도
  필요합니다.
- **비용 통제.** 16석 한 바퀴는 LLM 호출 약 19회입니다. cadence로 주기를 두고,
  후보를 추려서, 봉 단위로 캐시하며, 누적 비용이 한도에 닿으면 심의를 멈춥니다.

**지연 시간(latency).** 실시간이므로 심의는 한 봉 안에 끝나야 합니다. 마감시간을
넘기면 늦은 결정을 내는 대신 분석가 합의로 축약하고(사이즈는 절반으로 제한),
그 사실을 `degraded`에 기록합니다.

```yaml
alpha:
  - {type: investor_flow}          # 수급은 규칙 기반으로도 따로 본다
  - type: desk
    params:
      llm:          {provider: anthropic, model: claude-opus-5}
      decision_llm: {provider: anthropic, model: claude-opus-5}  # 판정 좌석용
      cadence_bars: 1
      max_symbols_per_run: 3
      debate_rounds: 2
      risk_debate_rounds: 1
      deadline_s: 120
      min_conviction: 0.6
      cost_limit_usd: 20
```

좌석을 줄이려면 `seats: [technical, flow, microstructure]` 처럼 분석 좌석만
고르면 됩니다. 토론·리스크·결정 좌석은 뺄 수 없습니다 — 빼면 제안은 하지만
결정은 못 하는 파이프라인이 됩니다.

`council`(4인 축약판)도 남아 있지만, 새 설정에는 `desk`를 쓰세요.

---

## 수급 — 외국인 · 기관 · 개인

한국 시장에서 가격 다음으로 정보량이 큰 데이터를 OHLCV와 같은 급의 1급 타입으로
다룹니다.

```yaml
flow:
  provider: kis                      # kis | synthetic | none
  params: {app_key: "${KIS_APP_KEY}", app_secret: "${KIS_APP_SECRET}", paper: true}
  history_sessions: 120

alpha:
  - {type: investor_flow, params: {min_streak: 3, min_participation: 0.01}}
  - {type: retail_contrarian, params: {min_zscore: 1.8}}
```

`InvestorFlowAlpha`가 보는 것:

- **지속성** — 외국인·기관은 주문을 며칠에 걸쳐 쪼개 집행합니다. 하루치는 노이즈,
  연속 순매수일이 신호입니다.
- **참여율 정규화** — 절대 수량이 아니라 거래량 대비 비중과, 그 종목 자신의 과거
  대비 z-score로 강도를 봅니다. 대형주와 소형주에 같은 임계값을 쓸 수 있는 이유입니다.
- **다이버전스** — 주가가 빠지는데 외국인·기관이 사면 매집, 오르는데 팔면 분산.
  가격과 수급이 같은 방향인 경우보다 확신도를 높게 잡습니다.
- **프로그램 분리** — 프로그램 순매수는 지수·차익거래 물량일 수 있어 종목 고유의
  뷰가 아닙니다.

수급 소스가 없으면 `NullFlowProvider`가 조용히 0을 반환하는 대신 **한 번 경고하고
빈 데이터를 반환**합니다. 수급을 읽는 줄 알았는데 0을 읽고 있는 전략은 자신 있게
헛소리를 하기 때문입니다.

---

## Dashboard

```bash
python -m quant serve configs/demo.yaml --port 8000
```

Read-only view of equity, positions, trades and a live event stream over
WebSocket. Starting and stopping a trader is CLI-only by design.

Set `QUANT_API_TOKEN` before exposing it anywhere: the API can start and stop a
live bot, and it will warn you loudly on startup if the token is unset. There
are no user accounts, plans, or tiers — one operator, one token.

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest -q
```

The suite covers position and cash accounting round trips (including shorts,
flips and multipliers), indicator correctness against hand-computed values, the
no-look-ahead guarantees, the "orders never fill on their signal bar" rule, that
a protection lock never blocks an exit, and that a zero-cost always-long book
tracks the underlying asset's own return — the check that catches money leaking
into or out of the accounting.

---

## Layout

```
quant/
  core/        types, clock, event bus, portfolio state, Context, Engine
  data/        provider interface + yahoo / ccxt / kis / csv / synthetic
  indicators/  incremental O(1) indicators — same values in backtest and live
  alpha/       AlphaModel + technical + 수급 models + the 16-seat LLM desk
  data/flow    investor flow (외국인/기관/개인) as a first-class data type
  portfolio/   construction models + optimizers (risk parity, HRP, mean-variance)
  risk/        risk models + protections
  execution/   execution models + fee / slippage / fill models
  brokerage/   paper + ccxt + kis + alpaca adapters
  backtest/    runner + performance analytics
  optimize/    hyperopt + walk-forward with purging
  live/        live loop, SQLite state, Telegram notifier
  api/         FastAPI control plane + dashboard
```

---

## Disclaimer

This is trading software, not trading advice. Backtested and simulated results
are not predictions — they are a description of what a set of rules would have
done on data that has already happened, which is a much weaker claim than it
looks. Live trading can lose money, including more than the amount deployed
where leverage or shorting is enabled. You are responsible for what you run.
