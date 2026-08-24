"""The config must not lie about what it accepted.

Three ways it used to: a mis-spelled key deleted a whole block in silence, a cap
saved from the setup screen was overwritten by a looser one in the YAML, and a
backtest paired with a venue adapter produced no fills, no cost and no warning.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
import yaml
from pydantic import ValidationError

from quant.config.loader import load_config
from quant.config.schema import (
    BrokerConfig,
    CostConfig,
    LimitsConfig,
    ModelSpec,
    PortfolioConfig,
    StrategyConfig,
    SymbolSpec,
)
from quant.core.types import AssetClass, OrderSide, RunMode, Symbol
from quant.strategy.builder import build_brokerage, build_costs, build_engine

KRX = Symbol("005930", venue="kis", asset_class=AssetClass.EQUITY, quote_currency="KRW")


def _write(tmp_path, raw: dict, name: str = "c.yaml") -> str:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return str(path)


def _minimal(**overrides) -> dict:
    raw = {"name": "t", "alpha": [{"type": "ema_cross"}]}
    raw.update(overrides)
    return raw


# ── (a) unknown keys ─────────────────────────────────────────────────────
def test_mis_spelled_section_is_rejected_not_dropped(tmp_path):
    path = _write(tmp_path, _minimal(rsik={"models": [{"type": "max_positions"}]}))
    with pytest.raises(ValidationError, match="rsik"):
        load_config(path)


def test_unknown_key_names_the_nearest_valid_one(tmp_path):
    path = _write(tmp_path, _minimal(portfolio={"starting_cah": 555_000}))
    with pytest.raises(ValidationError, match="did you mean 'starting_cash'"):
        load_config(path)


def test_mis_spelled_limits_block_cannot_reach_live(tmp_path):
    # The 1-of-4 guard is satisfied by the one key spelled correctly, so the
    # other three vanishing has to be caught as an unknown key or not at all.
    path = _write(tmp_path, _minimal(
        mode="live",
        broker={"type": "kis", "live_trading_confirmed": True},
        limits={"max_daily_orders": 20, "max_daily_notinal": 5_000_000,
                "max_daily_los": 200_000},
    ))
    with pytest.raises(ValidationError) as exc:
        load_config(path)
    assert "max_daily_notinal" in str(exc.value)
    assert "max_daily_los" in str(exc.value)


def test_unknown_key_inside_a_list_entry_is_rejected():
    with pytest.raises(ValidationError, match="did you mean 'ticker'"):
        SymbolSpec.model_validate({"tickr": "005930"})


def test_model_params_stay_free_form():
    # `params` is the plug-in boundary — its keys belong to the model, not here.
    spec = ModelSpec.model_validate({"type": "ema_cross", "params": {"anything": 1}})
    assert spec.params == {"anything": 1}


@pytest.mark.parametrize("path", [
    "configs/demo.yaml", "configs/demo_flow.yaml", "configs/kr_equity.yaml",
    "configs/kr_toss.yaml", "configs/kr_desk_gemini.yaml", "configs/live_crypto.yaml",
    "configs/us_equity.yaml",
])
def test_shipped_configs_have_no_unknown_keys(path):
    load_config(path)


# ── (b) which limit source wins ──────────────────────────────────────────
def _budget(monkeypatch, configured: float, from_env: str | None):
    monkeypatch.delenv("QUANT_LIMIT_DAILY_NOTIONAL", raising=False)
    if from_env is not None:
        monkeypatch.setenv("QUANT_LIMIT_DAILY_NOTIONAL", from_env)
    cfg = StrategyConfig(name="t", alpha=[ModelSpec(type="ema_cross")],
                         limits=LimitsConfig(max_daily_notional=configured))
    engine, _ = build_engine(cfg)
    return engine.budget


def test_setup_screen_cap_is_not_overwritten_by_a_looser_yaml(monkeypatch):
    assert _budget(monkeypatch, 5_000_000, "500000").max_notional == 500_000


def test_yaml_cap_is_not_overwritten_by_a_looser_setup_screen(monkeypatch):
    assert _budget(monkeypatch, 500_000, "5000000").max_notional == 500_000


def test_the_discarded_cap_is_logged_not_silent(monkeypatch, caplog):
    with caplog.at_level("WARNING", logger="quant.builder"):
        _budget(monkeypatch, 5_000_000, "500000")
    assert "5,000,000" in caplog.text and "500,000" in caplog.text


def test_zero_still_means_no_cap(monkeypatch):
    assert _budget(monkeypatch, 0.0, None).max_notional == 0.0
    assert _budget(monkeypatch, 0.0, "500000").max_notional == 500_000
    assert _budget(monkeypatch, 500_000, "0").max_notional == 500_000


def test_non_numeric_env_cap_is_ignored_not_fatal(monkeypatch):
    assert _budget(monkeypatch, 500_000, "오십만").max_notional == 500_000


# ── (c) backtest must simulate, and 거래세 must be settable ───────────────
def test_backtest_with_a_venue_adapter_is_rejected(tmp_path):
    path = _write(tmp_path, _minimal(mode="backtest", broker={"type": "kis"}))
    with pytest.raises(ValidationError, match="cannot simulate fills"):
        load_config(path)


def test_forcing_backtest_onto_a_live_config_simulates_instead_of_dry_running(caplog):
    from quant.brokerage.paper import PaperBrokerage
    from quant.core.account import Portfolio
    from quant.execution.costs import PRESETS

    cfg = StrategyConfig(name="t", mode=RunMode.DRY_RUN,
                         alpha=[ModelSpec(type="ema_cross")],
                         broker=BrokerConfig(type="kis", params={"product_code": "01"}))
    cfg.mode = RunMode.BACKTEST          # what `quant backtest <live config>` does
    fee, slippage = PRESETS["kr_equity"]()
    with caplog.at_level("WARNING", logger="quant.builder"):
        brokerage = build_brokerage(cfg, Portfolio(1_000_000, "KRW"), fee, slippage, None)
    assert isinstance(brokerage, PaperBrokerage)
    assert brokerage.fees is fee          # the cost model must survive the swap
    assert "kis" in caplog.text


def test_shipped_kr_example_pairs_backtest_with_the_simulator():
    cfg = load_config("configs/kr_equity.yaml")
    assert cfg.mode is RunMode.BACKTEST
    assert cfg.broker.type == "paper"
    assert cfg.costs.preset == "kr_equity"


def test_sell_tax_rate_is_settable_on_the_preset():
    cfg = StrategyConfig(name="t", alpha=[ModelSpec(type="ema_cross")],
                         costs=CostConfig(preset="kr_equity", sell_tax_bps=15.0))
    fee, _, _ = build_costs(cfg)
    notional = 100 * 70_000.0
    sell = fee.for_side(OrderSide.SELL).fee(KRX, Decimal(100), 70_000.0, False)
    buy = fee.for_side(OrderSide.BUY).fee(KRX, Decimal(100), 70_000.0, False)
    assert sell == pytest.approx(buy + notional * 0.0015)


def test_sell_tax_defaults_to_the_rate_that_actually_applies():
    """세율을 적지 않으면 그 해에 실제로 물리는 값을 씁니다.

    예전에는 18bp 로 굳어 있었습니다 — 2024년에는 맞았지만 2025년에 15bp 로
    내렸다가 2026년에 20bp 로 다시 올랐습니다. 숫자를 여기 박아두면 그 해가
    지나는 순간 테스트가 틀린 값을 지키게 됩니다.
    """
    from quant.execution.costs import krx_sell_tax_bps

    cfg = StrategyConfig(name="t", alpha=[ModelSpec(type="ema_cross")],
                         costs=CostConfig(preset="kr_equity"))
    fee, _, _ = build_costs(cfg)
    notional = 100 * 70_000.0
    at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    sell = fee.for_side(OrderSide.SELL).fee(KRX, Decimal(100), 70_000.0, False, at)
    buy = fee.for_side(OrderSide.BUY).fee(KRX, Decimal(100), 70_000.0, False, at)
    expected = krx_sell_tax_bps(at) / 10_000.0
    assert sell == pytest.approx(buy + notional * expected)


def test_sell_tax_on_a_foreign_preset_is_refused():
    with pytest.raises(ValidationError, match="preset: kr_equity"):
        CostConfig(preset="us_equity", sell_tax_bps=15.0)


def test_nested_cost_models_become_instances_not_dicts():
    from quant.execution.costs import KoreanEquitySellTax, SideAwareFeeModel

    cfg = StrategyConfig(name="t", alpha=[ModelSpec(type="ema_cross")], costs=CostConfig(
        preset="custom",
        fee=ModelSpec(type="SideAwareFeeModel", params={
            "base": {"type": "KoreanEquityFeeModel", "params": {"commission_bps": 1.5}},
            "sell_extra": {"type": "KoreanEquitySellTax",
                           "params": {"sell_tax_bps": 15.0}}}),
        slippage=ModelSpec(type="SpreadPlusImpactSlippage",
                           params={"base_spread_bps": 8}),
    ))
    fee, _, _ = build_costs(cfg)
    assert isinstance(fee, SideAwareFeeModel)
    assert isinstance(fee.sell_extra, KoreanEquitySellTax)
    # 7,000,000 * 0.00015 commission + 7,000,000 * 0.0015 tax
    assert fee.for_side(OrderSide.SELL).fee(
        KRX, Decimal(100), 70_000.0, False) == pytest.approx(11_550.0)


def test_nested_cost_model_typo_is_named():
    cfg = StrategyConfig(name="t", alpha=[ModelSpec(type="ema_cross")], costs=CostConfig(
        preset="custom",
        fee=ModelSpec(type="SideAwareFeeModel",
                      params={"base": {"type": "KoreanEquityFeModel"}}),
        slippage=ModelSpec(type="NoSlippage"),
    ))
    with pytest.raises(KeyError, match="KoreanEquityFeModel"):
        build_costs(cfg)


def test_a_kr_backtest_charges_commission_and_sell_tax(tmp_path):
    import asyncio

    from quant.backtest.runner import run_backtest

    cfg = load_config("configs/kr_equity.yaml")
    cfg.data.provider, cfg.data.params = "synthetic", {"seed": 7}
    cfg.portfolio = PortfolioConfig(starting_cash=10_000_000, base_currency="KRW",
                                    model=cfg.portfolio.model,
                                    max_position_weight=0.35, cash_reserve_pct=0.05)
    result = asyncio.run(run_backtest(cfg))
    assert result.report.trades > 0
    assert result.report.total_fees > 0
