"""화면에 뜨는 이름이 읽히는가.

`donchian_breakout` 만 뜨면 그게 뭔지 이미 아는 사람만 이 서비스를 씁니다.
실제로 돈을 넣는 사람이 못 읽는 이름은 이름이 아닙니다.

이 파일이 검사하는 것은 문구의 품질이 아니라 **빠진 것이 없는가** 입니다.
새 모델을 등록하면서 설명을 빼먹는 것이 이 종류의 사전이 낡는 유일한
방식이고, 그건 사람이 알아채기 전에 화면에 나갑니다.
"""
from __future__ import annotations

import pytest

from quant.execution.models import BUILTIN_EXECUTION_MODELS
from quant.risk.models import BUILTIN_RISK_MODELS
from quant.strategy.builder import BUILTIN_ALPHA_MODELS, FLOW_ALPHA_MODELS
from quant.strategy.glossary import (
    ALPHA,
    BROKER,
    EXECUTION,
    MODE,
    RISK,
    describe,
)

#: 설정 파일에서 실제로 쓸 수 있는 알파 전부. `desk` 와 그 옛 이름은
#: 레지스트리가 아니라 빌더가 따로 다루므로 손으로 더합니다.
ALL_ALPHA = set(BUILTIN_ALPHA_MODELS) | FLOW_ALPHA_MODELS | {"desk", "council"}


@pytest.mark.parametrize("name", sorted(ALL_ALPHA))
def test_every_alpha_has_a_korean_name(name):
    assert name in ALPHA, f"{name} 에 한국어 설명이 없습니다"
    assert ALPHA[name].ko and ALPHA[name].ko != name
    assert len(ALPHA[name].what) >= 40, f"{name} 설명이 너무 짧습니다"


@pytest.mark.parametrize("name", sorted(BUILTIN_EXECUTION_MODELS))
def test_every_execution_model_has_a_korean_name(name):
    assert name in EXECUTION, f"{name} 에 한국어 설명이 없습니다"
    assert EXECUTION[name].kind == "execution"


@pytest.mark.parametrize("name", sorted(BUILTIN_RISK_MODELS))
def test_every_risk_model_has_a_korean_name(name):
    assert name in RISK, f"{name} 에 한국어 설명이 없습니다"
    assert RISK[name].kind == "risk"


def test_the_regime_filter_is_not_described_as_a_buy_signal():
    """장세 필터는 사는 쪽이 아니라 막는 쪽입니다.

    이걸 신호로 소개하면 사용자는 "이것만 켜면 알아서 산다" 고 읽습니다.
    실제로는 아무것도 사지 않고 다른 신호를 막기만 합니다.
    """
    assert ALPHA["regime_filter"].kind == "guard"
    assert "신호가 아닙니다" in ALPHA["regime_filter"].what


@pytest.mark.parametrize("name", sorted(ALL_ALPHA))
def test_the_description_says_when_it_loses(name):
    """언제 안 통하는지 없는 설명은 설명이 아니라 광고입니다.

    자기 돈으로 이걸 켜는 사람은 그걸 알 자격이 있습니다. 문장을 기계로
    채점할 수는 없으니, 실패를 말할 때 쓰는 낱말이 하나라도 있는지만 봅니다.
    """
    if name == "council":            # 별칭 — 본체를 가리키기만 합니다
        return
    what = ALPHA[name].what
    hints = ("잃", "약", "실패", "놓치", "늦", "깎", "헛", "빠집")
    assert any(h in what for h in hints), \
        f"{name}: 언제 안 통하는지가 없습니다 — {what[:60]}"


def test_modes_and_brokers_are_named_in_korean():
    for mode in ("backtest", "dry_run", "live"):
        assert mode in MODE and MODE[mode] != mode
    # 실거래는 그 사실을 분명히 말해야 합니다.
    assert "실제 주문" in MODE["live"]
    for broker in ("paper", "kis", "toss", "ccxt"):
        assert broker in BROKER and BROKER[broker] != broker


def test_an_unknown_model_does_not_blank_the_screen():
    """사전에 없는 부품 하나 때문에 목록 전체가 사라지면 훨씬 나쁩니다."""
    out = describe(ALPHA, "some_model_added_next_week")
    assert out["id"] == out["ko"] == "some_model_added_next_week"
    assert out["what"] == ""
