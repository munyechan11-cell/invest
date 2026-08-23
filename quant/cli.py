"""Command line interface.

    quant backtest configs/demo.yaml
    quant optimize configs/demo.yaml --space configs/space.yaml --trials 60
    quant walkforward configs/demo.yaml --space configs/space.yaml --folds 5
    quant dryrun configs/live_crypto.yaml
    quant live configs/live_crypto.yaml        # requires explicit confirmation
    quant serve configs/demo.yaml --port 8000
    quant validate configs/demo.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from quant.config.loader import load_config
from quant.config.schema import StrategyConfig
from quant.core.types import UTC, RunMode


def _setup_logging(level: str, json_logs: bool) -> None:
    if json_logs:
        class JsonFormatter(logging.Formatter):
            def format(self, record):
                payload = {"ts": self.formatTime(record), "level": record.levelname,
                           "logger": record.name, "msg": record.getMessage()}
                if record.exc_info:
                    payload["exc"] = self.formatException(record.exc_info)
                return json.dumps(payload, ensure_ascii=False)

        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logging.basicConfig(level=level.upper(), handlers=[handler], force=True)
    else:
        logging.basicConfig(
            level=level.upper(),
            format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
            datefmt="%H:%M:%S",
        )


def _load(path: str, overrides: dict | None = None) -> StrategyConfig:
    config = load_config(path)
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        section, _, field = key.partition(".")
        target = getattr(config, section) if field else config
        setattr(target, field or section, value)
    return config


def _write_json(path: str | None, payload: dict) -> None:
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                          encoding="utf-8")
    print(f"→ written to {path}")


# ── commands ─────────────────────────────────────────────────────────────
async def cmd_backtest(args) -> int:
    from quant.backtest.runner import run_backtest

    config = _load(args.config, {
        "backtest.start": _parse_date(args.start),
        "backtest.end": _parse_date(args.end),
        "portfolio.starting_cash": args.cash,
    })
    config.mode = RunMode.BACKTEST

    def progress(done: int, total: int) -> None:
        pct = done / total * 100
        sys.stderr.write(f"\r  {done}/{total} bars ({pct:5.1f}%)")
        sys.stderr.flush()

    result = await run_backtest(config, on_progress=None if args.quiet else progress)
    if not args.quiet:
        sys.stderr.write("\r" + " " * 40 + "\r")
    result.print_summary()
    _write_json(args.out, result.to_dict())
    return 0


async def cmd_optimize(args) -> int:
    from quant.optimize.hyperopt import optimize, parse_space, validate_out_of_sample

    config = _load(args.config)
    config.mode = RunMode.BACKTEST
    space = parse_space(_read_space(args.space))

    def on_trial(i: int, loss: float, params: dict) -> None:
        sys.stderr.write(f"\r  trial {i + 1}/{args.trials}  loss {loss:+.5f}   ")
        sys.stderr.flush()

    result = await optimize(config, space, trials=args.trials, loss=args.loss,
                            seed=args.seed, concurrency=args.jobs,
                            on_trial=None if args.quiet else on_trial)
    sys.stderr.write("\r" + " " * 60 + "\r")

    if args.holdout_days and config.backtest.end:
        holdout_start = config.backtest.end
        holdout_end = holdout_start + _days(args.holdout_days)
        result.out_of_sample = await validate_out_of_sample(
            config, result.best_params, holdout_start, holdout_end
        )
    result.print_summary()
    _write_json(args.out, result.to_dict())
    return 0


async def cmd_walkforward(args) -> int:
    from quant.optimize.hyperopt import parse_space
    from quant.optimize.walkforward import walk_forward

    config = _load(args.config)
    config.mode = RunMode.BACKTEST
    result = await walk_forward(
        config, parse_space(_read_space(args.space)), folds=args.folds,
        trials_per_fold=args.trials, train_ratio=args.train_ratio, loss=args.loss,
        anchored=args.anchored, purge_bars=args.purge, seed=args.seed,
    )
    result.print_summary()
    _write_json(args.out, result.to_dict())
    return 0 if result.verdict.startswith("PASS") else 1


async def cmd_dryrun(args) -> int:
    from quant.live.trader import run_live

    config = _load(args.config)
    config.mode = RunMode.DRY_RUN
    print(f"Starting DRY RUN of {config.name} — live prices, simulated fills. "
          f"Ctrl-C to stop.\n")
    await run_live(config, args.state)
    return 0


async def cmd_live(args) -> int:
    from quant.live.trader import run_live

    config = _load(args.config)
    if config.mode is not RunMode.LIVE:
        print("error: the config's mode is not 'live'. Refusing to trade real money "
              "on a config that does not ask for it.", file=sys.stderr)
        return 2
    if not config.broker.live_trading_confirmed:
        print("error: broker.live_trading_confirmed must be true in the config.",
              file=sys.stderr)
        return 2
    if not args.yes:
        print(f"\n  ⚠  LIVE TRADING — REAL MONEY\n"
              f"     strategy : {config.name}\n"
              f"     broker   : {config.broker.type}\n"
              f"     universe : {len(config.universe.symbols)} symbols\n"
              f"     max order: {config.broker.max_order_notional:,.0f}\n")
        answer = input("  Type the strategy name to confirm: ").strip()
        if answer != config.name:
            print("  aborted.", file=sys.stderr)
            return 2
    await run_live(config, args.state)
    return 0


async def cmd_serve(args) -> int:
    import uvicorn

    from quant.api.server import create_app

    config = _load(args.config) if args.config else None
    app = create_app(config, state_path=args.state)
    server = uvicorn.Server(uvicorn.Config(
        app, host=args.host, port=args.port, log_level=args.log_level.lower()
    ))
    await server.serve()
    return 0


async def cmd_validate(args) -> int:
    try:
        config = _load(args.config)
    except Exception as exc:
        print(f"✗ invalid: {exc}", file=sys.stderr)
        return 1
    from quant.strategy.builder import build_engine

    try:
        engine, provider = build_engine(config)
    except Exception as exc:
        print(f"✗ config parses but does not assemble: {exc}", file=sys.stderr)
        return 1
    print(f"✓ {config.name}")
    print(f"  mode        {config.mode.value}")
    print(f"  data        {config.data.provider} @ {config.data.timeframe} "
          f"(warm-up {config.data.warmup_bars} bars)")
    print(f"  universe    {len(config.universe.symbols)} symbols")
    print(f"  alpha       {engine.alpha.name} "
          f"(needs {getattr(engine.alpha, 'warmup_bars', 0)} bars)")
    print(f"  portfolio   {engine.portfolio_model.name}")
    print(f"  risk        {[m.name for m in engine.risk.models] or 'none'}")
    print(f"  protections {[p.name for p in engine.protections.protections] or 'none'}")
    print(f"  execution   {engine.execution_model.name}")
    print(f"  broker      {engine.brokerage.name}")
    warmup_needed = getattr(engine.alpha, "warmup_bars", 0)
    if warmup_needed > config.data.warmup_bars:
        print(f"\n  ⚠ alpha needs {warmup_needed} warm-up bars but data.warmup_bars "
              f"is {config.data.warmup_bars} — early signals will be unreliable")
    await provider.close()
    return 0


def cmd_models(args) -> int:
    from quant.execution.models import BUILTIN_EXECUTION_MODELS
    from quant.portfolio.models import BUILTIN_PORTFOLIO_MODELS
    from quant.risk.models import BUILTIN_RISK_MODELS
    from quant.risk.protections import BUILTIN_PROTECTIONS
    from quant.strategy.builder import BUILTIN_ALPHA_MODELS
    from quant.data.provider import available_providers

    sections = {
        "alpha": sorted(BUILTIN_ALPHA_MODELS) + ["council"],
        "portfolio": sorted(BUILTIN_PORTFOLIO_MODELS),
        "risk": sorted(BUILTIN_RISK_MODELS),
        "protections": sorted(BUILTIN_PROTECTIONS),
        "execution": sorted(BUILTIN_EXECUTION_MODELS),
        "data providers": available_providers() + ["yahoo", "ccxt", "kis"],
    }
    for title, names in sections.items():
        print(f"\n{title}:")
        for n in sorted(set(names)):
            print(f"  · {n}")
    print()
    return 0


# ── helpers ──────────────────────────────────────────────────────────────
def _parse_date(value: str | None):
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _days(n: int):
    from datetime import timedelta
    return timedelta(days=n)


def _read_space(path: str) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8")
    if path.endswith((".yaml", ".yml")):
        import yaml
        raw = yaml.safe_load(text)
    else:
        raw = json.loads(text)
    return raw["space"] if isinstance(raw, dict) and "space" in raw else raw


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant", description="Event-driven algorithmic trading engine",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__,
    )
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    parser.add_argument("--json-logs", action="store_true",
                        default=os.environ.get("LOG_FORMAT", "").lower() == "json")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_config(p):
        p.add_argument("config", help="path to a strategy YAML/JSON file")
        p.add_argument("--out", help="write the full result as JSON here")
        p.add_argument("--quiet", action="store_true")

    bt = sub.add_parser("backtest", help="run a historical simulation")
    add_config(bt)
    bt.add_argument("--start"), bt.add_argument("--end")
    bt.add_argument("--cash", type=float)
    bt.set_defaults(fn=cmd_backtest)

    opt = sub.add_parser("optimize", help="search parameters")
    add_config(opt)
    opt.add_argument("--space", required=True, help="parameter space YAML/JSON")
    opt.add_argument("--trials", type=int, default=50)
    opt.add_argument("--loss", default="multi_metric")
    opt.add_argument("--seed", type=int, default=0)
    opt.add_argument("--jobs", type=int, default=1)
    opt.add_argument("--holdout-days", type=int, default=0,
                     help="re-test the winner on this many days after backtest.end")
    opt.set_defaults(fn=cmd_optimize)

    wf = sub.add_parser("walkforward", help="rolling out-of-sample validation")
    add_config(wf)
    wf.add_argument("--space", required=True)
    wf.add_argument("--folds", type=int, default=5)
    wf.add_argument("--trials", type=int, default=25)
    wf.add_argument("--train-ratio", type=float, default=0.7)
    wf.add_argument("--loss", default="multi_metric")
    wf.add_argument("--anchored", action="store_true")
    wf.add_argument("--purge", type=int, default=0, help="bars to purge at fold edges")
    wf.add_argument("--seed", type=int, default=0)
    wf.set_defaults(fn=cmd_walkforward)

    dry = sub.add_parser("dryrun", help="live data, simulated fills")
    dry.add_argument("config")
    dry.add_argument("--state", default="quant_state.db")
    dry.set_defaults(fn=cmd_dryrun)

    live = sub.add_parser("live", help="REAL MONEY trading")
    live.add_argument("config")
    live.add_argument("--state", default="quant_state.db")
    live.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    live.set_defaults(fn=cmd_live)

    serve = sub.add_parser("serve", help="dashboard + REST API")
    serve.add_argument("config", nargs="?")
    serve.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    serve.add_argument("--state", default="quant_state.db")
    serve.set_defaults(fn=cmd_serve)

    val = sub.add_parser("validate", help="check a config without running it")
    val.add_argument("config")
    val.set_defaults(fn=cmd_validate)

    models = sub.add_parser("models", help="list every pluggable model")
    models.set_defaults(fn=cmd_models, sync=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.log_level, args.json_logs)
    # Credentials live in .env; nothing else loads it, so every entry point must.
    from quant.live.credentials import load_env_file

    load_env_file(os.environ.get("QUANT_ENV_FILE", ".env"))
    try:
        if getattr(args, "sync", False):
            return args.fn(args)
        return asyncio.run(args.fn(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        logging.getLogger("quant.cli").exception("command failed")
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
