"""실매매를 하는 전략은 전부 AI 데스크를 거치는가.

규칙 신호는 "무엇을" 만 말하고 "왜" 는 말하지 못합니다. `ema_cross` 가 샀다는
기록만 남으면, 그 매매가 틀렸을 때 배울 것이 없습니다 — 파라미터를 바꿔 보는
것 말고는 할 수 있는 게 없고, 그건 배우는 게 아니라 과거에 맞추는 것입니다.

데스크는 그 자리에 근거를 남깁니다. 그리고 데스크가 꺼져도(키 없음·한도 초과·
오류) 규칙 신호는 그대로 돕니다 — LLM 이 죽었다고 엔진까지 멈추지는 않습니다.
그러니 붙여서 잃는 것이 없습니다.

**백테스트는 반대입니다.** 거기엔 데스크를 넣으면 안 됩니다.

첫째는 비용입니다. 5년 일봉이면 1,250봉이고 봉마다 19번씩 부르면 23,750회입니다.

둘째가 진짜 이유입니다 — **미래를 압니다.** LLM 은 2020년 3월이라는 날짜에
대해 그 뒤에 무슨 일이 일어났는지 알고 있습니다. 그 지식이 섞인 백테스트는
실제보다 훨씬 좋은 숫자를 냅니다. 그 숫자를 믿고 실거래에 넣으면 그대로
잃습니다. 백테스트가 거짓말을 하면 그 뒤의 모든 판단이 거짓말 위에 섭니다.
"""
from __future__ import annotations

from quant.api.server import strategy_catalog
from quant.config.loader import load_config


def _configs():
    for name, path in strategy_catalog().items():
        try:
            yield name, load_config(str(path))
        except Exception:
            continue          # 전략이 아닌 YAML(파라미터 공간 등)


def _has_desk(cfg) -> bool:
    return any(m.type in ("desk", "council") for m in cfg.alpha)


def test_every_strategy_that_can_trade_goes_through_the_desk():
    naked = [name for name, cfg in _configs()
             if cfg.mode.value in ("dry_run", "live") and not _has_desk(cfg)]
    assert not naked, (
        f"실매매 전략인데 데스크를 거치지 않습니다: {naked} — "
        "이 전략이 낸 주문은 왜 냈는지 남지 않습니다.")


def test_a_backtest_never_calls_the_desk():
    """LLM 은 과거 날짜의 미래를 압니다. 그 위에 선 성적표는 거짓말입니다."""
    contaminated = [name for name, cfg in _configs()
                    if cfg.mode.value == "backtest" and _has_desk(cfg)]
    assert not contaminated, (
        f"백테스트 전략에 데스크가 붙어 있습니다: {contaminated} — "
        "LLM 이 그 날짜 이후를 알고 있어 성적표가 실제보다 좋게 나옵니다.")


def test_the_desk_is_not_the_only_thing_deciding():
    """데스크가 꺼져도 매매는 이어져야 합니다.

    LLM 은 키가 만료되고, 쿼터가 마르고, 5xx 를 냅니다. 그때 봇이 통째로
    멈추면 들고 있던 포지션의 손절도 같이 멈춥니다 — 그건 데스크가 없는 것보다
    나쁩니다.
    """
    for name, cfg in _configs():
        if not _has_desk(cfg):
            continue
        rules = [m.type for m in cfg.alpha if m.type not in ("desk", "council")]
        assert rules, (
            f"{name}: 데스크 말고 신호가 없습니다 — LLM 이 멈추면 이 전략은 "
            "아무것도 하지 않습니다.")


def test_the_desk_does_not_speak_every_bar_on_fast_timeframes():
    """봉이 자주 닫히는 전략에서 매 봉 심의는 비용이 그대로 배수가 됩니다.

    4시간봉은 하루 여섯 번 닫힙니다. `cadence_bars: 1` 이면 일봉 전략의 여섯
    배가 나갑니다. 숫자를 못박지는 않고 "일봉보다 촘촘하면 주기를 늘렸는가"
    만 봅니다 — 적정값은 시장마다 다르고, 여기서 정할 것이 아닙니다.
    """
    from quant.core.types import timeframe_seconds

    for name, cfg in _configs():
        spec = next((m for m in cfg.alpha if m.type in ("desk", "council")), None)
        if spec is None:
            continue
        if timeframe_seconds(cfg.data.timeframe) >= 86400:
            continue
        cadence = int(spec.params.get("cadence_bars", 1) or 1)
        assert cadence > 1, (
            f"{name}: {cfg.data.timeframe} 봉인데 매 봉 심의합니다 "
            "(cadence_bars: 1) — 일봉 전략보다 몇 배가 나갑니다.")
