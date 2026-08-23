"""Walk-forward and purged cross-validation.

A single in-sample backtest tells you almost nothing about a strategy's future.
Walk-forward tells you something real: fit on a window, trade the *next*
window, roll forward, and concatenate only the out-of-sample segments. If the
stitched OOS curve does not resemble the in-sample one, the strategy is a
curve-fit and you have just saved yourself the tuition.

`purge_bars` removes observations straddling a fold boundary. Without purging,
an indicator with a 200-bar lookback lets the test fold peek at training data
through its own warm-up window.
"""
from __future__ import annotations

import copy
import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from quant.backtest.runner import run_backtest
from quant.config.schema import StrategyConfig
from quant.core.types import timeframe_delta
from quant.optimize.hyperopt import ParamSpace, optimize, set_by_path

log = logging.getLogger("quant.optimize.walkforward")


@dataclass
class Fold:
    index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    best_params: dict = field(default_factory=dict)
    in_sample: dict = field(default_factory=dict)
    out_of_sample: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "fold": self.index,
            "train": [self.train_start.isoformat(), self.train_end.isoformat()],
            "test": [self.test_start.isoformat(), self.test_end.isoformat()],
            "params": self.best_params,
            "in_sample": self.in_sample,
            "out_of_sample": self.out_of_sample,
        }


@dataclass
class WalkForwardResult:
    folds: list[Fold]
    loss: str
    #: mean OOS Sharpe ÷ mean IS Sharpe. Below ~0.5 means the fit does not travel.
    efficiency: float = 0.0
    oos_sharpe_mean: float = 0.0
    oos_sharpe_std: float = 0.0
    oos_return_total: float = 0.0
    consistency: float = 0.0        # fraction of folds profitable out of sample
    verdict: str = ""

    def to_dict(self) -> dict:
        return {
            "loss": self.loss,
            "efficiency": round(self.efficiency, 4),
            "oos_sharpe_mean": round(self.oos_sharpe_mean, 4),
            "oos_sharpe_std": round(self.oos_sharpe_std, 4),
            "oos_return_total": round(self.oos_return_total, 4),
            "consistency": round(self.consistency, 4),
            "verdict": self.verdict,
            "folds": [f.to_dict() for f in self.folds],
        }

    def print_summary(self) -> None:
        print(f"\n{'=' * 68}\n  Walk-forward — {len(self.folds)} folds\n{'=' * 68}")
        for f in self.folds:
            i_s, o_s = f.in_sample.get("sharpe", 0), f.out_of_sample.get("sharpe", 0)
            print(f"  fold {f.index}  {f.test_start:%Y-%m-%d}→{f.test_end:%Y-%m-%d}  "
                  f"IS {i_s:+.2f} → OOS {o_s:+.2f}  "
                  f"ret {f.out_of_sample.get('total_return', 0):+.2%}")
        print(f"\n  walk-forward efficiency : {self.efficiency:.2f}")
        print(f"  OOS Sharpe              : {self.oos_sharpe_mean:.2f} "
              f"± {self.oos_sharpe_std:.2f}")
        print(f"  profitable folds        : {self.consistency:.0%}")
        print(f"\n  {self.verdict}\n{'=' * 68}\n")


def make_folds(start: datetime, end: datetime, folds: int, train_ratio: float = 0.7,
               anchored: bool = False, purge: timedelta = timedelta(0)) -> list[Fold]:
    """Split a period into rolling (or anchored) train/test folds."""
    total = end - start
    window = total / folds
    out: list[Fold] = []
    for i in range(folds):
        w_start = start if anchored else start + window * i
        w_end = start + window * (i + 1)
        span = w_end - w_start
        train_end = w_start + span * train_ratio
        out.append(Fold(
            index=i,
            train_start=w_start,
            train_end=train_end,
            test_start=train_end + purge,
            test_end=w_end,
        ))
    return out


async def walk_forward(
    config: StrategyConfig,
    space: list[ParamSpace],
    folds: int = 5,
    trials_per_fold: int = 25,
    train_ratio: float = 0.7,
    loss: str = "multi_metric",
    anchored: bool = False,
    purge_bars: int = 0,
    seed: int = 0,
) -> WalkForwardResult:
    start = config.backtest.start
    end = config.backtest.end
    if start is None or end is None:
        raise ValueError("walk-forward needs explicit backtest.start and backtest.end")

    purge = timeframe_delta(config.data.timeframe) * purge_bars
    plan = make_folds(start, end, folds, train_ratio, anchored, purge)
    log.info("walk-forward: %d folds, %d trials each, purge %d bars",
             folds, trials_per_fold, purge_bars)

    for fold in plan:
        train_cfg = copy.deepcopy(config)
        train_cfg.backtest.start, train_cfg.backtest.end = fold.train_start, fold.train_end
        opt = await optimize(train_cfg, space, trials=trials_per_fold, loss=loss,
                             seed=seed + fold.index)
        fold.best_params = opt.best_params
        fold.in_sample = opt.best_report

        test_cfg = copy.deepcopy(config)
        for path, value in opt.best_params.items():
            set_by_path(test_cfg, path, value)
        test_cfg.backtest.start, test_cfg.backtest.end = fold.test_start, fold.test_end
        test_cfg.backtest.trials = 1
        try:
            fold.out_of_sample = (await run_backtest(test_cfg)).report.to_dict()
        except Exception as exc:
            log.warning("fold %d out-of-sample run failed: %s", fold.index, exc)
            fold.out_of_sample = {}
        log.info("fold %d: IS sharpe %.2f → OOS sharpe %.2f", fold.index,
                 fold.in_sample.get("sharpe", 0), fold.out_of_sample.get("sharpe", 0))

    return _summarize(plan, loss)


def _summarize(folds: list[Fold], loss: str) -> WalkForwardResult:
    is_sharpes = [f.in_sample.get("sharpe", 0.0) for f in folds if f.in_sample]
    oos_sharpes = [f.out_of_sample.get("sharpe", 0.0) for f in folds if f.out_of_sample]
    oos_returns = [f.out_of_sample.get("total_return", 0.0) for f in folds if f.out_of_sample]

    result = WalkForwardResult(folds=folds, loss=loss)
    if oos_sharpes:
        result.oos_sharpe_mean = statistics.fmean(oos_sharpes)
        result.oos_sharpe_std = statistics.pstdev(oos_sharpes) if len(oos_sharpes) > 1 else 0.0
        result.consistency = sum(1 for r in oos_returns if r > 0) / len(oos_returns)
    if is_sharpes and statistics.fmean(is_sharpes) != 0:
        result.efficiency = result.oos_sharpe_mean / statistics.fmean(is_sharpes)
    result.oos_return_total = 1.0
    for r in oos_returns:
        result.oos_return_total *= (1 + r)
    result.oos_return_total -= 1

    if not oos_sharpes:
        result.verdict = "no out-of-sample results — the folds produced nothing to judge"
    elif result.oos_sharpe_mean <= 0:
        result.verdict = "REJECT: out-of-sample Sharpe is not positive. No evidence of edge."
    elif result.efficiency < 0.4:
        result.verdict = (
            "REJECT: out-of-sample performance is well under half the in-sample. "
            "The parameter search is fitting noise."
        )
    elif result.consistency < 0.5:
        result.verdict = (
            "MARGINAL: fewer than half the folds were profitable. Any live result "
            "will depend heavily on when you happen to start."
        )
    elif result.efficiency >= 0.7 and result.consistency >= 0.6:
        result.verdict = (
            "PASS: out-of-sample performance holds up and most folds are profitable. "
            "Worth a dry run — which is still not the same as evidence it will work."
        )
    else:
        result.verdict = "MARGINAL: some out-of-sample signal, but not a robust one."
    return result
