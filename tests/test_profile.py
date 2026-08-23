"""투자 성향 진단.

The thing that makes a personality quiz worth building is that its output is
wired to real parameters. So the tests here are less about the quiz and more
about the two promises around it:

  1. the derived settings genuinely differ between an aggressive and a
     defensive answer set — otherwise the whole exercise is decoration;
  2. no answer combination can weaken a safety limit. "Aggressive" must mean
     bigger inside the same rules, never fewer rules.
"""
import json
import os
import tempfile
from pathlib import Path

import pytest

from quant.live.profile import (
    AXIS_MAX, AXIS_META, ARCHETYPES, QUESTIONS, InvestorProfile, ProfileStore,
    apply_profile, questionnaire, score_answers,
)

AGGRESSIVE = {"q1": "c", "q2": "c", "q3": "a", "q4": "c", "q5": "a",
              "q6": "d", "q7": "b", "q8": "d"}
DEFENSIVE = {"q1": "a", "q2": "a", "q3": "d", "q4": "a", "q5": "d",
             "q6": "a", "q7": "a", "q8": "a"}
MIDDLE = {"q1": "b", "q2": "b", "q3": "b", "q4": "b", "q5": "b",
          "q6": "b", "q7": "d", "q8": "b"}


# ── 설문 구조 ─────────────────────────────────────────────────────────────
def test_every_question_is_answerable_and_scored():
    for q in QUESTIONS:
        assert len(q.options) >= 2, f"{q.id} 보기가 부족합니다"
        assert any(o.weights for o in q.options), f"{q.id} 는 어떤 축에도 기여하지 않습니다"
        assert len({o.id for o in q.options}) == len(q.options)


def test_every_axis_is_reachable_from_both_ends():
    """An axis you can only push one way is not an axis."""
    for axis in AXIS_META:
        deltas = [o.weights.get(axis, 0)
                  for q in QUESTIONS for o in q.options if axis in o.weights]
        assert any(d > 0 for d in deltas), f"{axis} 를 올릴 방법이 없습니다"
        assert any(d < 0 for d in deltas), f"{axis} 를 내릴 방법이 없습니다"


def test_all_sixteen_archetypes_are_named():
    assert len(ARCHETYPES) == 16
    for code, (name, desc) in ARCHETYPES.items():
        assert len(code) == 4 and name and desc


def test_axis_maxima_are_per_axis_not_a_shared_constant():
    """Axes touch different numbers of questions. A shared divisor leaves the
    thin axes stuck near neutral no matter how a user answers."""
    assert len(set(AXIS_MAX.values())) > 1
    assert all(v > 0 for v in AXIS_MAX.values())


# ── 진단 결과 ─────────────────────────────────────────────────────────────
def test_aggressive_and_defensive_land_on_opposite_sides():
    a = score_answers(AGGRESSIVE)
    d = score_answers(DEFENSIVE)
    assert a.normalized("R") > 0.5
    assert d.normalized("R") < -0.5
    assert a.name != d.name


def test_the_extremes_actually_reach_the_extremes():
    """If the most aggressive possible answers still land mid-scale, the quiz
    is not measuring anything."""
    a = score_answers(AGGRESSIVE)
    assert a.normalized("R") > 0.8, "공격 극단이 최대치 근처에 도달하지 못했습니다"
    assert score_answers(DEFENSIVE).normalized("R") < -0.8


def test_an_undiagnosed_profile_is_not_labelled_aggressive():
    """All-zero axes would otherwise produce the most aggressive four-letter
    code by the sign rule — the worst possible default for someone who has not
    answered anything."""
    blank = InvestorProfile()
    assert not blank.completed
    assert blank.name == "미진단"
    assert blank.code == "----"


def test_unknown_answers_are_ignored_not_fatal():
    profile = score_answers({"q1": "c", "nonexistent": "z", "q2": "zzz"})
    assert profile.answers == {"q1": "c"}


# ── 파생 설정 ─────────────────────────────────────────────────────────────
def test_settings_differ_meaningfully_by_risk_appetite():
    a = score_answers(AGGRESSIVE).settings()
    d = score_answers(DEFENSIVE).settings()
    assert a["target_annual_vol"] > d["target_annual_vol"] * 2
    assert a["max_position_weight"] > d["max_position_weight"] * 2
    assert a["max_positions"] < d["max_positions"]
    assert a["max_daily_loss_pct"] > d["max_daily_loss_pct"] * 3


def test_aggressive_gets_a_wider_stop_not_a_tighter_one():
    """A narrow stop is not safety, it is frequent loss. A bigger position needs
    more room, not less, or correct calls get cut out of noise."""
    a = score_answers(AGGRESSIVE).settings()
    d = score_answers(DEFENSIVE).settings()
    assert a["stop_atr_multiple"] > d["stop_atr_multiple"]


def test_the_label_and_the_timeframe_never_contradict():
    """Showing "스캘퍼" next to a daily bar leaves the user unsure which to
    believe."""
    for answers in (AGGRESSIVE, DEFENSIVE, MIDDLE):
        p = score_answers(answers)
        short_label = p.code[1] == "h"
        assert (p.settings()["timeframe"] != "1d") == short_label


@pytest.mark.parametrize("answers", [AGGRESSIVE, DEFENSIVE, MIDDLE])
def test_no_answer_set_can_disable_a_safety_limit(answers):
    """The rule the whole feature rests on: a personality result may make
    positions bigger inside the rules, never remove the rules."""
    s = score_answers(answers).settings()
    assert s["max_daily_loss_pct"] > 0, "하루 손실 한도가 0 이 되었습니다"
    assert s["max_daily_orders"] > 0
    assert 0 < s["stop_atr_multiple"] <= 12
    assert 0 < s["stop_ceiling_pct"] <= 0.30, "손절 상한이 30% 를 넘었습니다"
    assert s["max_gross_leverage"] <= 1.0, "레버리지가 1배를 넘었습니다"
    assert s["max_position_weight"] <= 0.40
    assert s["max_positions"] >= 2


def test_every_archetype_stays_within_the_safety_envelope():
    """Sweep the axes rather than trusting three sample answer sets."""
    for r in (-1, -0.5, 0, 0.5, 1):
        for h in (-1, 0, 1):
            for e in (-1, 1):
                for c in (-1, 1):
                    p = InvestorProfile(overrides={"R": r, "H": h, "E": e, "C": c})
                    s = p.settings()
                    assert s["max_gross_leverage"] <= 1.0
                    assert s["max_daily_loss_pct"] > 0
                    assert s["stop_ceiling_pct"] <= 0.30
                    assert s["max_positions"] >= 2


# ── 직접 조정 ─────────────────────────────────────────────────────────────
def test_an_override_wins_over_the_quiz_score():
    p = score_answers(AGGRESSIVE)
    assert p.normalized("R") > 0.8
    p.overrides["R"] = -0.8
    assert p.normalized("R") == pytest.approx(-0.8)
    assert p.settings()["max_position_weight"] < 0.2


def test_overrides_are_clamped():
    p = InvestorProfile(overrides={"R": 99.0})
    assert p.normalized("R") == 1.0


def test_axis_summary_flags_what_was_overridden():
    p = score_answers(MIDDLE)
    p.overrides["H"] = 0.9
    flags = {a["axis"]: a["overridden"] for a in p.axis_summary()}
    assert flags["H"] and not flags["R"]


# ── 저장 ──────────────────────────────────────────────────────────────────
def test_profile_round_trips_through_disk():
    path = Path(tempfile.mkdtemp()) / "p.json"
    store = ProfileStore(path)
    original = score_answers(AGGRESSIVE)
    original.overrides["H"] = 0.4
    store.save(original)

    loaded = ProfileStore(path).load()
    assert loaded.code == original.code
    assert loaded.answers == original.answers
    assert loaded.overrides == original.overrides


def test_a_corrupt_profile_file_does_not_crash_the_bot():
    path = Path(tempfile.mkdtemp()) / "p.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert ProfileStore(path).load().name == "미진단"


# ── 설정 반영 ─────────────────────────────────────────────────────────────
def test_the_profile_never_overwrites_an_explicit_config_value():
    """A questionnaire quietly replacing a number the operator chose on purpose
    is not help."""
    from quant.config.loader import load_config

    config = load_config("configs/demo.yaml")
    config.portfolio.max_position_weight = 0.11        # deliberately chosen
    tuned, _ = apply_profile(config, score_answers(AGGRESSIVE))
    assert tuned.portfolio.max_position_weight == 0.11


def test_the_profile_fills_in_values_left_at_their_defaults():
    from quant.config.schema import PortfolioConfig, StrategyConfig, SymbolSpec

    config = StrategyConfig(name="t")
    config.universe.symbols = [SymbolSpec(ticker="AAA")]
    config.alpha = []
    defaults = PortfolioConfig()
    tuned, touched = apply_profile(config, score_answers(DEFENSIVE))
    assert tuned.portfolio.max_position_weight != defaults.max_position_weight
    assert tuned.limits.max_daily_loss_pct > 0
    assert touched


def test_applying_a_profile_does_not_mutate_the_original_config():
    from quant.config.loader import load_config

    config = load_config("configs/demo.yaml")
    before = config.portfolio.max_position_weight
    apply_profile(config, score_answers(AGGRESSIVE))
    assert config.portfolio.max_position_weight == before


def test_a_config_without_stops_gets_them_from_the_profile():
    from quant.config.schema import StrategyConfig, SymbolSpec

    config = StrategyConfig(name="t")
    config.universe.symbols = [SymbolSpec(ticker="AAA")]
    config.risk.models = []
    tuned, _ = apply_profile(config, score_answers(AGGRESSIVE))
    kinds = {m.type for m in tuned.risk.models}
    assert "max_dd_per_security" in kinds, "손절 없는 설정에 손절이 추가되지 않았습니다"
    assert "trailing_stop" in kinds


def test_questionnaire_is_json_serialisable_for_the_api():
    payload = questionnaire()
    json.dumps(payload, ensure_ascii=False)
    assert len(payload) == len(QUESTIONS)
    assert all("options" in q and q["options"] for q in payload)
