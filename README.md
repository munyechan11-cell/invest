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
`regime_filter` (veto), `council` (LLM multi-agent).
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

## LLM research council

`council` is an alpha model that runs a TradingAgents-style pipeline per symbol:
four analysts (technical, fundamental, news, macro) → a bull/bear debate → a
research manager verdict → a risk panel that can scale the position or veto it.
Its output is an `Insight` like any other, and the rule-based risk layer keeps
its veto regardless of how confident the model sounds.

Two things are deliberately awkward, because the alternative is lying to
yourself:

- **It is disabled in backtests by default.** A language model's training data
  postdates any historical bar, so running it over history produces spectacular
  and entirely fake results. Enabling it requires `allow_in_backtest: true` and,
  to be meaningful, a point-in-time `context_source`.
- **It is cost-controlled.** A full council run is a dozen LLM calls, so it runs
  on a cadence over a shortlist, with per-bar caching.

```yaml
alpha:
  - {type: donchian_breakout}
  - type: council
    params:
      llm: {provider: anthropic, model: claude-opus-5}
      cadence_bars: 5
      max_symbols_per_run: 3
      min_conviction: 0.6
      language: ko
```

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
  alpha/       AlphaModel + technical models + LLM research council
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
