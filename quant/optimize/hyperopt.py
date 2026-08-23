"""Parameter search over a strategy config.

Uses Optuna's TPE sampler when available and falls back to seeded random search
otherwise, so the feature works with no extra dependency.

The honest-results machinery is not optional here:
  * every trial's parameters are applied to a *copy* of the config;
  * the trial count is written into `backtest.trials`, so the winner's deflated
    Sharpe already discounts for how hard you looked;
  * `validate_out_of_sample` re-runs the winner on data the search never saw.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from quant.backtest.runner import BacktestResult, run_backtest
from quant.config.schema import StrategyConfig
from quant.optimize.losses import LOSS_FUNCTIONS

log = logging.getLogger("quant.optimize")


@dataclass
class ParamSpace:
    """One tunable parameter, addressed by a dotted path into the config.

    Example: `alpha.0.params.fast` targets the first alpha model's `fast`.
    """

    path: str
    kind: str          # "int" | "float" | "categorical" | "bool"
    low: float | None = None
    high: float | None = None
    step: float | None = None
    choices: list[Any] = field(default_factory=list)
    log_scale: bool = False

    def sample_random(self, rng: random.Random) -> Any:
        if self.kind == "int":
            step = int(self.step or 1)
            n = int((self.high - self.low) // step)
            return int(self.low) + rng.randint(0, max(n, 0)) * step
        if self.kind == "float":
            if self.log_scale and self.low > 0:
                import math
                return math.exp(rng.uniform(math.log(self.low), math.log(self.high)))
            return rng.uniform(self.low, self.high)
        if self.kind == "bool":
            return rng.random() < 0.5
        return rng.choice(self.choices)

    def sample_optuna(self, trial) -> Any:
        if self.kind == "int":
            return trial.suggest_int(self.path, int(self.low), int(self.high),
                                     step=int(self.step or 1))
        if self.kind == "float":
            return trial.suggest_float(self.path, self.low, self.high,
                                       log=self.log_scale)
        if self.kind == "bool":
            return trial.suggest_categorical(self.path, [True, False])
        return trial.suggest_categorical(self.path, self.choices)


def set_by_path(config: StrategyConfig, path: str, value: Any) -> None:
    """Assign into a nested config using `a.b.0.c` addressing."""
    parts = path.split(".")
    target: Any = config
    for part in parts[:-1]:
        target = _descend(target, part)
    last = parts[-1]
    if isinstance(target, dict):
        target[last] = value
    elif last.isdigit():
        target[int(last)] = value
    else:
        if not hasattr(target, last):
            raise AttributeError(f"config path {path!r}: {type(target).__name__} "
                                 f"has no attribute {last!r}")
        setattr(target, last, value)


def _descend(target: Any, part: str) -> Any:
    if isinstance(target, dict):
        return target[part]
    if isinstance(target, (list, tuple)):
        return target[int(part)]
    return getattr(target, part)


def get_by_path(config: StrategyConfig, path: str) -> Any:
    target: Any = config
    for part in path.split("."):
        target = _descend(target, part)
    return target


@dataclass
class OptimizationResult:
    best_params: dict
    best_loss: float
    best_report: dict
    trials: list[dict]
    loss_name: str
    elapsed_s: float
    out_of_sample: dict | None = None

    def to_dict(self) -> dict:
        return {
            "best_params": self.best_params,
            "best_loss": round(self.best_loss, 6),
            "best_report": self.best_report,
            "loss": self.loss_name,
            "trials_run": len(self.trials),
            "elapsed_s": round(self.elapsed_s, 1),
            "out_of_sample": self.out_of_sample,
            "trials": self.trials,
        }

    def print_summary(self) -> None:
        print(f"\n{'=' * 68}\n  Optimization — {self.loss_name} "
              f"({len(self.trials)} trials, {self.elapsed_s:.0f}s)\n{'=' * 68}")
        print(f"  best loss: {self.best_loss:.5f}")
        for k, v in self.best_params.items():
            print(f"    {k} = {v}")
        r = self.best_report
        print(f"\n  in-sample : return {r.get('total_return', 0):+.2%}  "
              f"sharpe {r.get('sharpe', 0):.2f}  maxDD {r.get('max_drawdown', 0):.2%}  "
              f"trades {r.get('trades', 0)}  DSR {r.get('deflated_sharpe', 0):.1%}")
        if self.out_of_sample:
            o = self.out_of_sample
            print(f"  out-sample: return {o.get('total_return', 0):+.2%}  "
                  f"sharpe {o.get('sharpe', 0):.2f}  maxDD {o.get('max_drawdown', 0):.2%}  "
                  f"trades {o.get('trades', 0)}")
            if o.get("sharpe", 0) < self.best_report.get("sharpe", 0) * 0.4:
                print("  ⚠ out-of-sample Sharpe collapsed — this is overfitting, "
                      "not a strategy")
        print(f"{'=' * 68}\n")


async def optimize(
    config: StrategyConfig,
    space: list[ParamSpace],
    trials: int = 50,
    loss: str = "multi_metric",
    seed: int = 0,
    concurrency: int = 1,
    on_trial: Callable[[int, float, dict], None] | None = None,
) -> OptimizationResult:
    import time

    started = time.monotonic()
    loss_fn = LOSS_FUNCTIONS.get(loss)
    if loss_fn is None:
        raise KeyError(f"unknown loss {loss!r}; available: {sorted(LOSS_FUNCTIONS)}")

    history: list[dict] = []
    sem = asyncio.Semaphore(max(concurrency, 1))

    async def evaluate(params: dict) -> tuple[float, BacktestResult | None]:
        trial_config = copy.deepcopy(config)
        # Tell the metrics layer how wide the search is, so the winner's
        # deflated Sharpe is computed against the right multiple-testing bar.
        trial_config.backtest.trials = trials
        for path, value in params.items():
            set_by_path(trial_config, path, value)
        async with sem:
            try:
                result = await run_backtest(trial_config)
            except Exception as exc:
                log.warning("trial failed: %s (%s)", exc, params)
                return 1e6, None
        return loss_fn(result), result

    best_loss, best_params, best_result = float("inf"), {}, None

    try:
        import optuna

        optuna.logging.set_verbosity(optuna.logging.WARNING)
        study = optuna.create_study(
            direction="minimize", sampler=optuna.samplers.TPESampler(seed=seed)
        )
        for i in range(trials):
            trial = study.ask()
            params = {p.path: p.sample_optuna(trial) for p in space}
            value, result = await evaluate(params)
            study.tell(trial, value)
            history.append({"trial": i, "loss": value, "params": params,
                            "report": result.report.to_dict() if result else None})
            if value < best_loss:
                best_loss, best_params, best_result = value, params, result
            if on_trial:
                on_trial(i, value, params)
    except ImportError:
        log.info("optuna not installed — using seeded random search "
                 "(pip install optuna for TPE)")
        rng = random.Random(seed)
        for i in range(trials):
            params = {p.path: p.sample_random(rng) for p in space}
            value, result = await evaluate(params)
            history.append({"trial": i, "loss": value, "params": params,
                            "report": result.report.to_dict() if result else None})
            if value < best_loss:
                best_loss, best_params, best_result = value, params, result
            if on_trial:
                on_trial(i, value, params)

    return OptimizationResult(
        best_params=best_params,
        best_loss=best_loss,
        best_report=best_result.report.to_dict() if best_result else {},
        trials=history,
        loss_name=loss,
        elapsed_s=time.monotonic() - started,
    )


async def validate_out_of_sample(
    config: StrategyConfig, params: dict, start: datetime, end: datetime
) -> dict:
    """Re-run one parameter set on a window the search never touched."""
    holdout = copy.deepcopy(config)
    for path, value in params.items():
        set_by_path(holdout, path, value)
    holdout.backtest.start, holdout.backtest.end = start, end
    holdout.backtest.trials = 1
    result = await run_backtest(holdout)
    return result.report.to_dict()


def parse_space(raw: list[dict]) -> list[ParamSpace]:
    return [ParamSpace(**item) for item in raw]
