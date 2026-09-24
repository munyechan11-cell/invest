"""Jev 로 도는 데스크 — 판단 모델이 16석을 맡을 때.

Jev 는 글을 쓰지 않고 확률만 줍니다. 그래서 여기서 확인하는 것은 셋입니다:

* **옮기기** — 확률이 좌석 스키마의 숫자로 정확히 계산되는가(확신도, 배율,
  보유기간, 기대 변동, 분할 수), 필수 칸과 선택지를 지키는가.
* **미결 규칙** — 애매한 확률이 돈이 되지 않는가. 방향은 관망으로, 거부는
  "거부하지 않되 사이즈 절반 이하" 로.
* **전송** — MCP 핸드셰이크, 세션 헤더, SSE/JSON, 세션 만료, 인증 실패의
  빠른 실패, 사용량 계량.

네트워크는 한 번도 나가지 않습니다. 가짜 Jev 는 `httpx.MockTransport` 입니다.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from quant.alpha import jev, llm_client
from quant.alpha.desk import TradingDesk
from quant.alpha.llm_client import (
    _FALLBACK_PRICE,
    JEV_DEFAULT_URL,
    LLMClient,
    LLMConfig,
    LLMError,
    QuotaExhausted,
    billing_hint,
    price_for,
)
from quant.alpha.seats import ALL_SEATS, SEATS_BY_KEY
from quant.core.types import Direction
from tests.test_desk import SYM, ScriptedLLM, make_ctx, run_desk

USER = '결정론적으로 계산된 시장 브리프다.\n{"가격": {"종가": 70000}, "기술지표": {"국면": "추세"}}'


# ── 가짜 답 ──────────────────────────────────────────────────────────────────
def choice(probs: dict) -> dict:
    # 일부러 순서를 뒤집어 둡니다 — Jev 는 순서를 지키지 않습니다.
    shuffled = dict(reversed(list(probs.items())))
    return {"type": "choice", "choice": max(probs, key=probs.get),
            "probabilities": shuffled, "confidence": 0.8}


def boolean(p: float) -> dict:
    return {"type": "boolean", "probability": p}


def score(probs: list, confidence: float = 0.7) -> dict:
    return {"type": "score", "score": sum(i * p for i, p in enumerate(probs)),
            "probabilities": {str(i): p for i, p in reversed(list(enumerate(probs)))},
            "confidence": confidence}


def request(key: str):
    seat = SEATS_BY_KEY[key]
    return jev.build_request(seat.system, USER, seat.schema)


def mapped(key: str, answers: dict, **kw) -> dict:
    return jev.map_answers(request(key), {"answers": answers}, **kw)


def head_answers(actions: dict, *, horizon=(0, 0, 1, 0, 0), move=(0, 0, 0, 0, 0, 1, 0),
                 dissent=None) -> dict:
    full = dict.fromkeys(jev.HEAD_ACTIONS, 0.0)
    full.update(actions)
    keys = [s.key for s in ALL_SEATS if s.key != "head"] + ["none"]
    d = dict.fromkeys(keys, 0.0)
    d.update(dissent or {"none": 1.0})
    return {"action": choice(full), "horizon": score(list(horizon)),
            "move": score(list(move)), "dissent": choice(d)}


# ── 좌석 알아보기와 질문 ─────────────────────────────────────────────────────
def test_every_seat_is_recognised_and_has_an_english_lens():
    for seat in ALL_SEATS:
        req = jev.build_request(seat.system, USER, seat.schema)
        assert req.kind == seat.stage, seat.key
        assert req.seat is seat
        assert set(req.state) == {"seat", "role", "glossary", "evidence"}
        assert req.state["seat"] == seat.title_en
        assert req.state["evidence"] == USER          # 증거는 원문 그대로
        assert req.state["role"] == jev.LENSES[seat.key]
        assert req.state["role"].isascii(), seat.key
    assert set(jev.LENSES) == {s.key for s in ALL_SEATS}


def test_questions_are_english_and_every_option_is_described():
    """Jev 지침: 한 질문에 한 가지, 선택지와 단계는 말로 설명할 것."""
    for seat in ALL_SEATS:
        for qid, q in jev.build_request(seat.system, USER, seat.schema).questions.items():
            assert q["type"] in ("choice", "boolean", "score")
            assert q["instructions"].isascii() and len(q["instructions"]) > 20, (seat.key, qid)
            criteria = q["criteria"]
            descriptions = criteria.values() if isinstance(criteria, dict) else criteria
            for text in descriptions:
                assert isinstance(text, str) and text.isascii() and len(text) >= 5, (
                    seat.key, qid, text)


def test_question_ids_per_stage():
    ids = {seat.stage: sorted(jev.build_request(seat.system, USER, seat.schema).questions)
           for seat in ALL_SEATS}
    assert ids == {
        "analyst": ["data_sufficient", "stance"],
        "debate": ["case"],
        "risk_debate": ["hazard", "scale"],
        "risk_verdict": ["scale", "veto", "veto_reason"],
        "plan": ["rating", "winner"],
        "trade": ["action", "entry_style", "tranches"],
        "head": ["action", "dissent", "horizon", "move"],
    }
    # 점수 질문의 단계 수가 코드의 값 표와 맞아야 기대값 계산이 맞습니다.
    head = request("head").questions
    assert len(head["horizon"]["criteria"]) == len(jev.HORIZON_VALUES) == 5
    assert len(head["move"]["criteria"]) == len(jev.MOVE_VALUES) == 7
    assert "none" in head["dissent"]["criteria"] and "head" not in head["dissent"]["criteria"]
    assert len(head["dissent"]["criteria"]) == 16
    assert len(request("trader").questions["tranches"]["criteria"]) == 4


def test_glossary_covers_every_korean_brief_key_and_only_what_is_shown():
    desk = TradingDesk(ScriptedLLM(), memory=False)
    brief = desk.build_brief(make_ctx(invested=100), SYM)

    def keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from keys(v)
        elif isinstance(node, list):
            for v in node:
                yield from keys(v)

    korean = {k for k in keys(brief) if not k.isascii()}
    korean |= {"추세", "박스권", "전환", "수급", "요약20일", "최근5일", "데이터없음"}
    missing = korean - set(jev.GLOSSARY)
    assert not missing, missing
    for label in ("분석가 리포트", "강세/약세 토론", "리스크 토론", "리스크 판정", "리서치 계획",
                  "트레이더 실행안", "과거 판단 결과", "지금까지의 토론"):
        assert label in jev.GLOSSARY
    # 증거에 없는 용어는 보내지 않습니다 (입력 토큰 과금).
    g = jev.glossary_for(USER)
    assert {"가격", "종가", "기술지표", "국면", "추세"} <= set(g)
    assert "수급" not in g and "foreign_streak" not in g
    assert jev.glossary_for('{"수급": {"요약20일": {"foreign_streak": 4}}}')["foreign_streak"]


# ── 1. 분석가 ────────────────────────────────────────────────────────────────
def test_analyst_maps_argmax_and_its_probability():
    out = mapped("technical", {
        "stance": choice({"bullish": 0.72, "neutral": 0.2, "bearish": 0.08}),
        "data_sufficient": boolean(0.84),
    })
    assert out["stance"] == "bullish"
    assert out["conviction"] == pytest.approx(0.72)
    assert out["data_sufficient"] is True
    assert out["key_points"][0] == "강세 72% · 중립 20% · 약세 8%"
    assert "84%" in out["key_points"][1]


def test_an_analyst_without_data_is_neutral_and_barely_confident():
    """좌석 프롬프트와 같습니다 — 재료가 없으면 중립, 확신 0.2 이하."""
    out = mapped("fundamental", {
        "stance": choice({"bullish": 0.9, "neutral": 0.05, "bearish": 0.05}),
        "data_sufficient": boolean(0.3),
    })
    assert out["data_sufficient"] is False
    assert out["stance"] == "neutral"
    assert out["conviction"] <= 0.2
    assert out["key_points"][0].startswith("판단 재료 부족")
    assert len(out["key_points"][0]) <= 64                # 화면이 자르는 길이


def test_probability_yes_is_accepted_and_sums_off_by_rounding_are_normalised():
    out = mapped("flow", {
        # 반올림으로 합이 1.01
        "stance": choice({"bullish": 0.51, "neutral": 0.25, "bearish": 0.25}),
        "data_sufficient": {"type": "boolean", "probability_yes": 0.9},
    })
    assert out["stance"] == "bullish"
    assert out["conviction"] == pytest.approx(0.51 / 1.01, abs=1e-3)
    assert out["data_sufficient"] is True


@pytest.mark.parametrize("probabilities", [
    # 키 철자가 어긋나 확률 대부분이 모르는 이름에 간 경우. 버리고 늘리면
    # 매수 100% 가 됩니다.
    {"HOLD": 0.9, "buy": 0.1},
    {"Buy": 0.9, "hold": 0.1},
    # 1 을 넘거나 무한대인 값 — 잘라 쓰면 확신 100% 입니다.
    {"buy": 1.7, "hold": 0.0},
    {"buy": float("inf"), "hold": 0.0},
])
def test_a_distribution_that_does_not_add_up_is_refused_not_stretched(probabilities):
    with pytest.raises(llm_client.LLMError):
        jev.read_choice({"action": {"probabilities": probabilities}}, "action",
                        ("strong_buy", "buy", "hold", "reduce", "sell", "strong_sell"))


def test_rounding_across_many_options_is_still_accepted():
    # 16지선다(반대 좌석)를 둘째 자리에서 반올림하면 합이 1 에서 조금 벗어납니다.
    options = [f"o{i}" for i in range(16)]
    probs = {o: 0.07 for o in options}
    probs.update({o: 0.06 for o in options[:12]})          # 12×0.06 + 4×0.07 = 1.00
    probs["o0"] = 0.065                                   # 합 1.005
    out = jev.read_choice({"q": {"probabilities": probs}}, "q", options)
    assert sum(out.values()) == pytest.approx(1.0)


# ── 2. 토론 ──────────────────────────────────────────────────────────────────
def test_debate_conviction_is_the_expected_level_over_four():
    out = mapped("bull", {"case": score([0, 0, 0.18, 0.8, 0.02])})
    assert out["conviction"] == pytest.approx(2.84 / 4, abs=1e-3)
    assert "매수 논거" in out["argument"] and "강함" in out["argument"]
    bear = mapped("bear", {"case": score([0.7, 0.3, 0, 0, 0])})
    assert bear["conviction"] == pytest.approx(0.3 / 4, abs=1e-3)
    assert "매수 반대" in bear["argument"]
    # 두 좌석은 다른 질문을 받습니다 — 같은 스키마라도 편이 다릅니다.
    assert (request("bull").questions["case"]["instructions"]
            != request("bear").questions["case"]["instructions"])


# ── 3. 리스크 토론 ───────────────────────────────────────────────────────────
def test_risk_debate_scale_hazard_and_conviction():
    out = mapped("risk_conservative", {
        "scale": score([0, 0, 0.5, 0.5, 0], confidence=0.61),
        "hazard": choice({**dict.fromkeys(jev.HAZARDS, 0.0), "thin_liquidity": 0.6,
                          "none": 0.4}),
    })
    assert out["proposed_scale"] == pytest.approx(0.625)
    assert out["named_hazards"] == ["유동성 부족"]
    assert out["conviction"] == pytest.approx(0.61)
    assert out["argument"].startswith("보수형 리스크")
    calm = mapped("risk_aggressive", {
        "scale": score([0, 0, 0, 0, 1]),
        "hazard": choice({**dict.fromkeys(jev.HAZARDS, 0.0), "none": 1.0}),
    })
    assert calm["proposed_scale"] == 1.0 and calm["named_hazards"] == []


# ── 4. 리스크 판정 — 거부 문턱 ───────────────────────────────────────────────
def verdict(p_veto: float, *, u=None, reason="illiquid") -> dict:
    reasons = dict.fromkeys(jev.VETO_REASONS, 0.0)
    reasons[reason] = 1.0
    kw = {} if u is None else {"undecided_below": u}
    return mapped("risk_neutral", {"veto": boolean(p_veto), "scale": score([0, 0, 0, 0, 1]),
                                   "veto_reason": choice(reasons)}, **kw)


def test_a_clear_veto_vetoes():
    out = verdict(0.7)
    assert out["veto"] is True
    assert "유동성" in out["veto_reason"]
    assert "max_loss_pct" not in out                 # 어느 코드도 읽지 않습니다


def test_an_undecided_veto_does_not_close_but_caps_the_size():
    out = verdict(0.5)
    assert out["veto"] is False
    assert out["position_scale"] == 0.5               # 1.0 에서 절반으로
    assert "판단 보류" in out["reasoning"]


def test_an_unlikely_veto_leaves_the_size_alone():
    out = verdict(0.2)
    assert out["veto"] is False
    assert out["position_scale"] == 1.0
    assert out["veto_reason"] == ""


def test_undecided_below_zero_turns_the_rule_off():
    assert verdict(0.5, u=0)["veto"] is True          # 과반이면 거부
    assert verdict(0.49, u=0)["position_scale"] == 1.0  # 절반 제한도 없다
    out = mapped("head", head_answers({"strong_buy": 0.3, "buy": 0.25, "sell": 0.45}),
                 undecided_below=0)
    assert out["action"] == "strong_buy"


def test_veto_band_edges():
    assert jev.veto_band(0.65, 0.65) == "veto"
    assert jev.veto_band(0.35, 0.65) == "undecided"
    assert jev.veto_band(0.34, 0.65) == "clear"
    assert jev.veto_band(0.5, 0.5) == "veto"


# ── 5. 리서치 계획 ───────────────────────────────────────────────────────────
def test_research_plan_groups_ratings_before_deciding():
    out = mapped("research_manager", {
        "rating": choice({"strong_buy": 0.3, "buy": 0.5, "hold": 0.1, "sell": 0.1,
                          "strong_sell": 0.0}),
        "winner": choice({"bull": 0.7, "bear": 0.2, "balanced": 0.1}),
    })
    assert out["rating"] == "buy"
    assert out["conviction"] == pytest.approx(0.8)
    assert "강세 측 우세" in out["rationale"]
    assert out["strategic_actions"]
    split = mapped("research_manager", {
        "rating": choice({"strong_buy": 0.1, "buy": 0.4, "hold": 0.2, "sell": 0.3,
                          "strong_sell": 0.0}),
        "winner": choice({"bull": 0.4, "bear": 0.4, "balanced": 0.2}),
    })
    assert split["rating"] == "hold"
    assert "판단 보류" in split["rationale"]


# ── 6. 트레이더 ──────────────────────────────────────────────────────────────
def test_trader_action_entry_and_tranches():
    out = mapped("trader", {
        "action": choice({"buy": 0.8, "hold": 0.15, "sell": 0.05}),
        "entry_style": choice({"market_now": 0.1, "limit_patient": 0.2, "scale_in": 0.6,
                               "wait_for_pullback": 0.1}),
        "tranches": score([0, 1, 0, 0]),
    })
    assert (out["action"], out["entry_style"], out["tranches"]) == ("buy", "scale_in", 2)
    assert out["conviction"] == pytest.approx(0.8)
    assert "분할 진입" in out["execution_note"]
    # 기대값 1.5 는 반올림해 2 → 3회. 파이썬 round 의 은행가 반올림(→2회)이 아닙니다.
    half = mapped("trader", {
        "action": choice({"buy": 0.5, "hold": 0.3, "sell": 0.2}),
        "entry_style": choice({"market_now": 1.0, "limit_patient": 0, "scale_in": 0,
                               "wait_for_pullback": 0}),
        "tranches": score([0, 0.5, 0.5, 0]),
    })
    assert half["tranches"] == 3
    assert half["action"] == "hold"                   # 0.5 는 미결
    assert half["conviction"] == pytest.approx(0.3)   # 트레이더는 최종 행동(관망)의 확률
    assert "판단 보류" in half["execution_note"]


# ── 7. 헤드 — 돈이 되는 자리 ─────────────────────────────────────────────────
def test_head_undecided_group_holds():
    out = mapped("head", head_answers({"strong_buy": 0.3, "buy": 0.3, "hold": 0.1,
                                       "sell": 0.3}))
    assert out["action"] == "hold"
    assert "판단 보류" in out["rationale"]
    assert "60%" in out["rationale"]
    # 확신도는 가장 큰 묶음의 확률 — 관망은 주문이 되지 않으니 "얼마나 가까웠나" 의 기록.
    assert out["conviction"] == pytest.approx(0.6)


def test_head_decided_group_picks_the_best_option_inside_it():
    out = mapped("head", head_answers({"strong_buy": 0.2, "buy": 0.5, "hold": 0.3}))
    assert out["action"] == "buy"
    assert out["conviction"] == pytest.approx(0.7)


def test_head_a_split_buy_side_still_buys():
    """strong_buy 40% · buy 35% — 어느 선택지도 과반이 아니지만 '사는가' 는 75%."""
    out = mapped("head", head_answers({"strong_buy": 0.4, "buy": 0.35, "hold": 0.25}))
    assert out["action"] == "strong_buy"
    assert out["conviction"] == pytest.approx(0.75)


def test_head_reduce_and_sell_are_the_exit_side():
    out = mapped("head", head_answers({"reduce": 0.3, "sell": 0.4, "strong_sell": 0.1,
                                       "hold": 0.2}))
    assert out["action"] == "sell"
    assert out["conviction"] == pytest.approx(0.8)
    assert "매수 측" in out["invalidation"]


def _two_decimal_splits(total: int = 65):
    """`total`% 를 두 선택지에 소수 둘째 자리로 나누는 모든 방법."""
    for a in range(total + 1):
        yield a / 100, (total - a) / 100


def test_exactly_the_threshold_decides_on_both_sides():
    """"65% 에 못 미치면 관망" — 딱 65% 는 어느 쪽이든 결정입니다.

    0.30 + 0.35 는 부동소수로 0.6499999999999999 라, 반올림 없이 비교하면 청산
    쪽 65%(reduce 35 · sell 30)는 보류되고 매수 쪽 65%(buy 35 · strong_buy 30)는
    결정되었습니다. 보류된 헤드는 "어느 쪽도 65%에 못 미칩니다 … 축소·청산 측
    65%" 라는 스스로 모순된 문장을 남기고, 닫았어야 할 보유를 한 봉 더 둡니다.
    """
    head_pairs = (("reduce", "sell"), ("sell", "strong_sell"), ("reduce", "strong_sell"),
                  ("buy", "strong_buy"))
    for a, b in head_pairs:
        for x, y in _two_decimal_splits():
            out = mapped("head", head_answers({a: x, b: y, "hold": 0.35}))
            assert out["action"] != "hold", (a, x, b, y)
            assert "판단 보류" not in out["rationale"], (a, x, b, y)
    for a, b in (("sell", "strong_sell"), ("buy", "strong_buy")):
        for x, y in _two_decimal_splits():
            ratings = dict.fromkeys(jev.RATINGS, 0.0)
            ratings.update({a: x, b: y, "hold": 0.35})
            out = mapped("research_manager", {"rating": choice(ratings),
                                              "winner": choice({"balanced": 1.0})})
            assert out["rating"] != "hold", (a, x, b, y)
    for side in ("buy", "sell"):
        out = mapped("trader", {
            "action": choice({side: 0.65, "hold": 0.35}),
            "entry_style": choice({"market_now": 1.0}),
            "tranches": score([1, 0, 0, 0]),
        })
        assert out["action"] == side
    # 반올림이 문턱을 옮긴 것은 아닙니다 — 64% 는 여전히 보류입니다.
    below = mapped("head", head_answers({"reduce": 0.34, "sell": 0.30, "hold": 0.36}))
    assert below["action"] == "hold" and "판단 보류" in below["rationale"]


def test_reduce_is_asked_as_what_the_desk_does_with_it():
    """데스크에는 부분 축소가 없습니다 — reduce 도 sell 처럼 보유 전체를 닫습니다.

    Jev 의 확률은 선택지 **설명** 에 대한 답입니다. 설명이 "일부는 남긴다" 면
    hold 30 · reduce 45 · sell 25 는 "다 팔자" 에 25%만 건 답인데, 청산 쪽 묶음이
    70% 가 되어 전량 청산이 나갔습니다. 그래서 설명을 데스크가 하는 일에 맞춥니다.
    """
    text = request("head").questions["action"]["criteria"]["reduce"]
    assert "whole position" in text and "keep part" not in text
    assert "keep part" not in jev.LENSES["head"]
    assert "cut the weight" not in jev.LENSES["head"]
    # 그 설명이 참인지 — 데스크가 reduce 로 실제로 보유를 닫는지 — 도 봅니다.
    desk, fake = jev_desk("reduce")
    insights = run_desk(desk, make_ctx(invested=100))
    sent = fake.tool_calls("jev_evaluate")[-1]["params"]["arguments"]
    assert sent["questions"]["action"]["criteria"]["reduce"] == text
    assert desk.history[-1].action == "reduce"
    assert len(insights) == 1 and insights[0].direction is Direction.FLAT


def test_head_horizon_and_expected_move_are_computed_in_code():
    out = mapped("head", head_answers({"buy": 1.0}, horizon=(0.5, 0.5, 0, 0, 0),
                                      move=(0, 0, 0, 0, 0.25, 0.75, 0)))
    assert out["horizon_bars"] == 3                   # 0.5·2 + 0.5·4
    assert out["expected_move_pct"] == pytest.approx(4.25)
    down = mapped("head", head_answers({"sell": 1.0}, horizon=(1, 0, 0, 0, 0),
                                       move=(0.5, 0, 0, 0, 0, 0, 0.5)))
    assert down["horizon_bars"] == 2
    assert down["expected_move_pct"] == 0.0
    assert "target_weight_pct" not in out             # 어느 코드도 읽지 않습니다
    assert out["invalidation"] and "재심의" in out["invalidation"]


def test_head_dissent_names_the_seat_or_nobody():
    out = mapped("head", head_answers({"buy": 1.0}, dissent={"flow": 0.6, "none": 0.4}))
    assert out["dissent"].startswith("수급 분석가 반대")
    assert mapped("head", head_answers({"buy": 1.0}))["dissent"] == ""


def test_every_mapped_seat_satisfies_its_schema():
    answers = {
        "analyst": {"stance": choice({"bullish": 1.0}), "data_sufficient": boolean(1)},
        "debate": {"case": score([0, 0, 1, 0, 0])},
        "risk_debate": {"scale": score([0, 1, 0, 0, 0]),
                        "hazard": choice({"none": 1.0})},
        "risk_verdict": {"veto": boolean(0.1), "scale": score([0, 0, 1, 0, 0]),
                         "veto_reason": choice({"none_applies": 1.0})},
        "plan": {"rating": choice({"hold": 1.0}), "winner": choice({"balanced": 1.0})},
        "trade": {"action": choice({"hold": 1.0}),
                  "entry_style": choice({"market_now": 1.0}),
                  "tranches": score([1, 0, 0, 0])},
        "head": head_answers({"hold": 1.0}),
    }
    for seat in ALL_SEATS:
        out = jev.map_answers(jev.build_request(seat.system, USER, seat.schema),
                              {"answers": answers[seat.stage]})
        props = seat.schema["properties"]
        assert set(seat.schema["required"]) <= set(out), seat.key
        assert set(out) <= set(props), seat.key
        for name, value in out.items():
            if "enum" in props[name]:
                assert value in props[name]["enum"], (seat.key, name)
            if props[name].get("type") == "string":
                assert isinstance(value, str)
        for text_field in ("argument", "reasoning", "rationale", "execution_note"):
            if text_field in seat.schema["required"]:
                assert out[text_field].strip(), (seat.key, text_field)


# ── 깨진 답 ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("answers", [
    {"stance": choice({"bullish": 1.0})},                                 # 질문 하나 빠짐
    {"stance": {"type": "choice", "choice": "bullish"},                   # 확률 없음
     "data_sufficient": boolean(0.9)},
    {"stance": choice({"bullish": 0.0}), "data_sufficient": boolean(0.9)},  # 전부 0
    {"stance": choice({"bullish": "high"}), "data_sufficient": boolean(0.9)},
    {"stance": choice({"bullish": 1.0}), "data_sufficient": {"type": "boolean"}},
])
def test_a_missing_or_malformed_answer_is_an_llm_error(answers):
    with pytest.raises(LLMError):
        mapped("technical", answers)
    with pytest.raises(LLMError):
        jev.map_answers(request("technical"), {"no_answers": {}})


# ── 데스크 밖의 호출 ─────────────────────────────────────────────────────────
def test_an_unknown_system_gets_a_generic_mapping():
    from quant.alpha.council import _ANALYST_SCHEMA, _RISK_SCHEMA

    req = jev.build_request("You are a council analyst.", USER, _ANALYST_SCHEMA)
    assert req.kind == "generic" and list(req.questions) == ["stance"]
    out = jev.map_answers(req, {"answers": {
        "stance": choice({"bullish": 0.2, "neutral": 0.3, "bearish": 0.5})}})
    assert out["stance"] == "bearish"
    assert out["conviction"] == pytest.approx(0.5)
    assert out["key_points"] and "bearish 50%" in out["key_points"][0]
    # 숫자 필수 칸은 Jev 로 채울 수 없습니다 — 부르기 전에 실패(재시도 안 하는 422).
    with pytest.raises(LLMError, match="jev 422"):
        jev.build_request("You are a council risk manager.", USER, _RISK_SCHEMA)


# ── 좌석은 몰라도 단계는 안다 ───────────────────────────────────────────────
#: 프롬프트를 한 글자 고친 좌석. 정확히 같은지로 알아보므로 좌석은 못 알아봅니다.
EDITED = "당신은 데스크의 좌석이다. (프롬프트를 고쳤다)"

STAGE_ANSWERS = {
    "analyst": {"stance": choice({"bullish": 1.0}), "data_sufficient": boolean(1)},
    "debate": {"case": score([0, 0, 1, 0, 0])},
    "risk_debate": {"scale": score([0, 1, 0, 0, 0]), "hazard": choice({"none": 1.0})},
    "risk_verdict": {"veto": boolean(0.1), "scale": score([0, 0, 1, 0, 0]),
                     "veto_reason": choice({"none_applies": 1.0})},
    "plan": {"rating": choice({"hold": 1.0}), "winner": choice({"balanced": 1.0})},
    "trade": {"action": choice({"hold": 1.0}), "entry_style": choice({"market_now": 1.0}),
              "tranches": score([1, 0, 0, 0])},
    "head": head_answers({"hold": 1.0}),
}


def edited(stage: str):
    schema = next(s.schema for s in ALL_SEATS if s.stage == stage)
    return jev.build_request(EDITED, USER, schema)


def test_stage_for_matches_every_desk_schema_by_value_and_nothing_else():
    from quant.alpha.council import _ANALYST_SCHEMA

    for seat in ALL_SEATS:
        assert jev.stage_for(seat.schema) == seat.stage
        assert jev.stage_for(json.loads(json.dumps(seat.schema))) == seat.stage  # 사본도
    assert jev.stage_for(_ANALYST_SCHEMA) is None
    assert jev.stage_for({"type": "object", "properties": {}}) is None


def test_an_edited_prompt_with_a_desk_schema_keeps_its_stage():
    for seat in ALL_SEATS:
        known = jev.build_request(seat.system, USER, seat.schema)
        req = jev.build_request(EDITED, USER, seat.schema)
        assert req.kind == seat.stage, seat.key            # generic 이 아닙니다
        assert req.seat is None
        assert req.state == {"seat": "unknown", "role": EDITED,
                             "glossary": jev.glossary_for(USER), "evidence": USER}
        assert set(req.questions) == set(known.questions), seat.key
        for qid, q in req.questions.items():
            assert q["instructions"].isascii(), (seat.key, qid)
        out = jev.map_answers(req, {"answers": STAGE_ANSWERS[seat.stage]})
        assert set(seat.schema["required"]) <= set(out), seat.key


def test_a_known_prompt_with_another_stages_schema_follows_the_schema():
    """스키마가 데스크가 읽을 모양을 정합니다. 프롬프트가 헤드여도 분석가 스키마면 분석가."""
    head = SEATS_BY_KEY["head"]
    req = jev.build_request(head.system, USER, SEATS_BY_KEY["technical"].schema)
    assert req.kind == "analyst" and req.seat is None
    assert req.state["role"] == head.system


def test_an_unrecognised_head_still_holds_on_a_coin_flip():
    """좌석을 못 알아봐도 판단 보류는 빠지지 않습니다 — 일반 경로였다면 sell."""
    split = head_answers({"strong_buy": 0.3, "buy": 0.25, "sell": 0.45})
    out = jev.map_answers(edited("head"), {"answers": split})
    assert out["action"] == "hold"
    assert "판단 보류" in out["rationale"]
    # 같은 답을 기준 0 으로 읽으면 묶음으로 매수 측(55%)이 이깁니다 — 보류 규칙이
    # 실제로 일했고, 묶음 규칙도 살아 있습니다(선택지 하나씩이면 sell 45%).
    assert jev.map_answers(edited("head"), {"answers": split},
                           undecided_below=0)["action"] == "strong_buy"


def test_an_unrecognised_risk_verdict_keeps_the_veto_threshold():
    base = {"scale": score([0, 0, 0, 0, 1]), "veto_reason": choice({"illiquid": 1.0})}
    # 0.55 는 과반이지만 기준(0.65) 아래 — 거부하지 않고 배율을 절반으로 묶습니다.
    out = jev.map_answers(edited("risk_verdict"),
                          {"answers": {**base, "veto": boolean(0.55)}})
    assert out["veto"] is False and out["position_scale"] == pytest.approx(0.5)
    assert "판단 보류" in out["reasoning"]
    out = jev.map_answers(edited("risk_verdict"),
                          {"answers": {**base, "veto": boolean(0.7)}})
    assert out["veto"] is True and "유동성" in out["veto_reason"]


def test_an_unrecognised_debater_or_risk_seat_takes_no_side():
    debate = edited("debate").questions["case"]
    text = " ".join([debate["instructions"], *debate["criteria"]])
    assert "FOR buying" not in text and "AGAINST buying" not in text
    assert "the role" in debate["instructions"]
    out = jev.map_answers(edited("debate"), {"answers": STAGE_ANSWERS["debate"]})
    assert out["argument"].startswith("이 좌석의 논거 강도")

    risk = edited("risk_debate").questions["scale"]["instructions"]
    assert "aggressive" not in risk and "conservative" not in risk
    out = jev.map_answers(edited("risk_debate"), {"answers": STAGE_ANSWERS["risk_debate"]})
    assert out["argument"].startswith("리스크 좌석:")


# ─────────────────────────────────────────────────────────────────────────────
# 전송 — 가짜 Jev 서버
# ─────────────────────────────────────────────────────────────────────────────
ANALYST_ANSWERS = {
    "stance": choice({"bullish": 0.9, "neutral": 0.07, "bearish": 0.03}),
    "data_sufficient": boolean(0.9),
}


class FakeJev:
    """MCP streamable HTTP 서버 흉내. 요청을 전부 적어 둡니다."""

    def __init__(self, answer=None, *, sessions=("sess-1", "sess-2", "sess-3"),
                 sse=False, structured=False, expire_first_call=False):
        self.answer = answer or (lambda name, args: {"answers": ANALYST_ANSWERS})
        self.sessions = list(sessions)
        self.sse, self.structured = sse, structured
        self.expire_first_call = expire_first_call
        self.log: list[tuple[str, dict, dict]] = []
        self.urls: list[str] = []
        self.current = ""

    def tool_calls(self, name=None):
        return [body for method, _, body in self.log if method == "tools/call"
                and (name is None or body["params"]["name"] == name)]

    def methods(self):
        return [m for m, _, _ in self.log]

    def _reply(self, message: dict) -> httpx.Response:
        if self.sse:
            progress = {"jsonrpc": "2.0", "method": "notifications/progress",
                        "params": {"progress": 1}}
            body = (f"event: message\ndata: {json.dumps(progress)}\n\n"
                    f"event: message\ndata: {json.dumps(message)}\n\n")
            return httpx.Response(200, text=body,
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=message)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method", "")
        self.log.append((method, dict(request.headers), body))
        self.urls.append(str(request.url))
        if method == "initialize":
            self.current = self.sessions.pop(0) if self.sessions else ""
            headers = {"mcp-session-id": self.current} if self.current else {}
            return httpx.Response(200, headers=headers, json={
                "jsonrpc": "2.0", "id": body["id"],
                "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                           "serverInfo": {"name": "jev", "version": "1"}}})
        if method == "notifications/initialized":
            return httpx.Response(202)
        assert method == "tools/call", method
        if self.expire_first_call:
            self.expire_first_call = False
            return httpx.Response(404, json={"jsonrpc": "2.0", "id": None, "error": {
                "code": -32001, "message": "Session not found"}})
        params = body["params"]
        payload = self.answer(params["name"], params["arguments"])
        if isinstance(payload, httpx.Response):
            return payload
        if isinstance(payload, dict) and "jsonrpc" in payload:      # 날것의 JSON-RPC
            return self._reply({**payload, "id": body["id"]})
        payload = {"provider": "gateway", "model": "typesafe-ai/jev",
                   "usage": {"inputTokens": 677, "outputTokens": 76},
                   "latency_ms": 500, **payload}
        result = {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]}
        if self.structured:
            result["structuredContent"] = payload
        return self._reply({"jsonrpc": "2.0", "id": body["id"], "result": result})


def jev_client(fake, **config) -> LLMClient:
    client = LLMClient(LLMConfig(provider="jev", api_key="test-key", **config))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return client


def yielding(handler, delay: float = 0.0):
    """실제 네트워크처럼 답하기 전에 이벤트 루프에 자리를 내준다.

    동기 핸들러는 한 번도 양보하지 않아서, 첫 좌석이 핸드셰이크를 **끝까지**
    마친 뒤에야 나머지가 돕니다 — 그러면 잠금이 없어도 initialize 는 한 번이고,
    동시성 검사는 아무것도 검사하지 않습니다.
    """
    async def wrapped(request):
        await asyncio.sleep(delay)
        return handler(request)
    return wrapped


@pytest.fixture
def no_backoff(monkeypatch):
    """`complete()` 의 재시도 대기(1.5s, 3s …)를 기록만 하고 건너뛴다."""
    waits: list[float] = []
    real_sleep = asyncio.sleep

    async def sleep(seconds, *args, **kwargs):
        waits.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(llm_client.asyncio, "sleep", sleep)
    return waits


def tool_error(text: str) -> dict:
    return {"jsonrpc": "2.0", "result": {
        "isError": True, "content": [{"type": "text", "text": text}]}}


def rpc_error(code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "error": {"code": code, "message": message}}


def fail_once(failure: dict):
    """첫 도구 호출만 `failure` 로 답하고, 다음부터는 정상."""
    calls = []

    def answer(name, args):
        calls.append(name)
        return failure if len(calls) == 1 else {"answers": ANALYST_ANSWERS}
    return answer


def ask_technical(client):
    seat = SEATS_BY_KEY["technical"]
    return asyncio.run(client.complete(seat.system, USER, seat.schema))


def test_the_handshake_runs_once_and_later_calls_carry_the_session():
    fake = FakeJev()
    client = jev_client(fake)
    out = ask_technical(client)
    assert out["stance"] == "bullish"
    assert fake.methods() == ["initialize", "notifications/initialized", "tools/call"]
    init_headers = fake.log[0][1]
    assert "mcp-session-id" not in init_headers
    assert init_headers["authorization"] == "Bearer test-key"
    assert "application/json" in init_headers["accept"]
    assert "text/event-stream" in init_headers["accept"]
    init = fake.log[0][2]
    assert init["params"]["protocolVersion"] == "2025-06-18"
    assert init["params"]["clientInfo"]["name"] == "quant-desk"
    for _, headers, _ in fake.log[1:]:
        assert headers["mcp-session-id"] == "sess-1"
        assert headers["mcp-protocol-version"] == "2025-06-18"
    call = fake.tool_calls()[0]
    assert call["params"]["name"] == "jev_evaluate"
    assert set(call["params"]["arguments"]) == {"state", "questions"}
    assert fake.urls[0] == JEV_DEFAULT_URL

    ask_technical(client)                         # 두 번째는 핸드셰이크 없이
    assert fake.methods().count("initialize") == 1
    ids = [body["id"] for m, _, body in fake.log if "id" in body]
    assert len(ids) == len(set(ids))              # 요청 id 는 겹치지 않는다


def test_concurrent_seats_share_one_initialize():
    fake = FakeJev()
    client = jev_client(yielding(fake))
    seat = SEATS_BY_KEY["technical"]

    async def many():
        return await asyncio.gather(*(client.complete(seat.system, USER, seat.schema)
                                      for _ in range(8)))

    assert len(asyncio.run(many())) == 8
    assert fake.methods().count("initialize") == 1
    assert len(fake.tool_calls()) == 8


def test_a_burst_of_dead_session_reports_reopens_once():
    """좌석 여럿이 같은 세션에서 "Session not found" 를 받아도 다시 여는 것은 한 번.

    몇몇 거절은 누가 이미 다시 연 **뒤에** 도착합니다. 그 늦은 신고가 새 세션까지
    버리면(세대 번호를 안 보면) 좌석마다 initialize 가 한 번씩 더 나갑니다.
    """
    fake = FakeJev()
    stale: list[int] = []
    dead = []                                       # 세션이 죽은 뒤인가

    async def handler(request):
        body = json.loads(request.content)
        if (dead and body.get("method") == "tools/call"
                and request.headers.get("mcp-session-id") == "sess-1"):
            stale.append(1)
            # 첫 거절은 곧바로, 나머지는 다시 연 뒤에 하나씩 도착합니다.
            await asyncio.sleep(0.01 * (len(stale) - 1))
            fake.log.append(("tools/call", dict(request.headers), body))
            return httpx.Response(404, json={"jsonrpc": "2.0", "id": None, "error": {
                "code": -32001, "message": "Session not found"}})
        await asyncio.sleep(0)
        return fake(request)

    client = jev_client(handler)
    seat = SEATS_BY_KEY["technical"]
    ask_technical(client)                           # 세션 sess-1 을 연다
    dead.append(True)                               # 서버가 그 세션을 잊었다

    async def many():
        return await asyncio.gather(*(client.complete(seat.system, USER, seat.schema)
                                      for _ in range(6)))

    assert all(out["stance"] == "bullish" for out in asyncio.run(many()))
    assert len(stale) == 6                          # 여섯 좌석 모두 죽은 세션을 만났고
    assert fake.methods().count("initialize") == 2  # 다시 연 것은 한 번
    fresh = [h for m, h, _ in fake.log if m == "tools/call"
             and h.get("mcp-session-id") == "sess-2"]
    assert len(fresh) == 6
    assert client.usage.calls == 1 + 6              # 거절된 호출은 과금되지 않았다


def test_base_url_overrides_the_default_endpoint():
    fake = FakeJev()
    ask_technical(jev_client(fake, base_url="https://jev.example.test/api/mcp"))
    assert all(u == "https://jev.example.test/api/mcp" for u in fake.urls)


def test_sse_framed_responses_are_read_by_request_id():
    fake = FakeJev(sse=True)
    assert ask_technical(jev_client(fake))["stance"] == "bullish"


def test_structured_content_is_preferred_over_text():
    fake = FakeJev(structured=True)
    real = {"stance": choice({"bullish": 0.1, "neutral": 0.1, "bearish": 0.8}),
            "data_sufficient": boolean(0.9)}

    def answer(name, args):
        return {"answers": real}

    fake.answer = answer
    original = fake._reply

    def reply(message):
        # 텍스트 쪽에는 반대 답을 넣어 둡니다 — structuredContent 를 읽어야 bearish.
        result = message.get("result")
        if result:
            decoy = {"answers": ANALYST_ANSWERS, "usage": {}}
            result["content"] = [{"type": "text", "text": json.dumps(decoy)}]
        return original(message)

    fake._reply = reply
    assert ask_technical(jev_client(fake))["stance"] == "bearish"


def test_an_expired_session_reinitialises_once_and_succeeds():
    fake = FakeJev(expire_first_call=True)
    client = jev_client(fake)
    assert ask_technical(client)["stance"] == "bullish"
    assert fake.methods() == ["initialize", "notifications/initialized", "tools/call",
                              "initialize", "notifications/initialized", "tools/call"]
    assert fake.log[-1][1]["mcp-session-id"] == "sess-2"
    assert client.usage.calls == 1                # 거절된 호출은 과금되지 않았다


def test_a_json_rpc_session_error_also_reinitialises():
    calls = []

    def answer(name, args):
        calls.append(name)
        if len(calls) == 1:
            return {"jsonrpc": "2.0",
                    "error": {"code": -32000, "message": "Server not initialized"}}
        return {"answers": ANALYST_ANSWERS}

    fake = FakeJev(answer)
    assert ask_technical(jev_client(fake))["stance"] == "bullish"
    assert fake.methods().count("initialize") == 2


def test_a_missing_token_fails_fast_with_one_request():
    posts = []

    def handler(request):
        posts.append(request)
        return httpx.Response(401, json={"error": "missing_token"})

    client = jev_client(handler)
    with pytest.raises(LLMError) as err:
        ask_technical(client)
    assert "jev 401" in str(err.value)
    assert "missing_token" in str(err.value)
    assert len(posts) == 1                        # 재시도 없음
    assert client.usage.calls == 0


def test_a_json_rpc_error_fails_fast():
    fake = FakeJev(lambda name, args: rpc_error(-32602, "Invalid params: questions"))
    with pytest.raises(LLMError, match="jev 422"):
        ask_technical(jev_client(fake))
    assert len(fake.tool_calls()) == 1


@pytest.mark.parametrize("text", [
    "state too large",
    "MCP error -32602: Invalid arguments for tool jev_evaluate: questions",
])
def test_a_tool_error_about_the_input_fails_fast(text):
    fake = FakeJev(lambda name, args: tool_error(text))
    client = jev_client(fake)
    with pytest.raises(LLMError, match=f"jev 422: {text[:10]}"):
        ask_technical(client)
    assert len(fake.tool_calls()) == 1
    # 도구는 돌았습니다 — 청구됐을 수 있으니 호출 수는 셉니다(토큰은 모름).
    assert client.usage.calls == 1


@pytest.mark.parametrize("failure", [
    tool_error("upstream gateway timeout"),        # 스펙: API 실패는 isError 로
    tool_error("fetch failed"),
    rpc_error(-32603, "Internal error"),
    rpc_error(-32000, "Upstream model unavailable"),
], ids=["isError-timeout", "isError-fetch", "rpc-32603", "rpc-32000"])
def test_a_transient_jev_failure_is_asked_again(failure, no_backoff):
    """게이트웨이의 일시 장애는 요청이 틀린 게 아닙니다 — HTTP 503 과 같이 다시 묻습니다."""
    fake = FakeJev(fail_once(failure))
    client = jev_client(fake)
    assert ask_technical(client)["stance"] == "bullish"
    assert len(fake.tool_calls()) == 2
    assert no_backoff == [1.5]                     # 한 번 기다렸다가
    # 도구 오류는 돌았으니 세고, 프로토콜 오류는 도구가 안 돌았으니 안 셉니다.
    ran = "result" in failure
    assert client.usage.calls == (2 if ran else 1)


@pytest.mark.parametrize("failure", [
    tool_error("Daily quota exceeded for this token"),
    rpc_error(-32000, "Quota exceeded: 1000 calls per day"),
], ids=["isError", "rpc"])
def test_an_exhausted_daily_quota_is_quota_exhausted(failure, no_backoff):
    """하루 한도 소진은 평범한 오류가 아닙니다 — 데스크 전체가 멈춰야 합니다."""
    fake = FakeJev(lambda name, args: failure)
    with pytest.raises(QuotaExhausted, match="jev 429"):
        ask_technical(jev_client(fake))
    assert len(fake.tool_calls()) == 1
    assert no_backoff == []


def test_a_passing_rate_limit_in_a_tool_error_waits_and_retries(no_backoff):
    fake = FakeJev(fail_once(tool_error("Rate limit exceeded, retry in 2s")))
    assert ask_technical(jev_client(fake))["stance"] == "bullish"
    assert no_backoff == [2.0]                     # 제공자가 말한 대기


def test_a_non_json_tool_answer_is_counted_and_fails():
    fake = FakeJev(lambda name, args: {"jsonrpc": "2.0", "result": {
        "content": [{"type": "text", "text": "Jev is thinking about it"}]}})
    client = jev_client(fake, max_retries=1)
    with pytest.raises(LLMError, match="JSON 객체가 아닙니다"):
        ask_technical(client)
    assert len(fake.tool_calls()) == 1
    assert client.usage.calls == 1                 # 도구는 돌았으니 셉니다


def test_a_malformed_answer_is_an_ordinary_llm_error():
    fake = FakeJev(lambda name, args: {"answers": {"stance": choice({"bullish": 1.0})}})
    with pytest.raises(LLMError):
        ask_technical(jev_client(fake, max_retries=1))


def test_usage_is_counted_per_tool_call_and_priced_at_the_jev_rate():
    fake = FakeJev()
    client = jev_client(fake)
    ask_technical(client)
    ask_technical(client)
    assert client.usage.calls == 2
    assert client.usage.input_tokens == 2 * 677
    assert client.usage.output_tokens == 2 * 76
    assert client.usage.model == "typesafe-ai/jev"
    # 운영자가 알려 준 단가: 입력 1M 토큰당 $0.042, 출력 무료. 가장 비싼 기본
    # 요율($5/$25)로 떨어지면 `cost_limit_usd` 가 엉뚱하게 일찍 걸립니다.
    assert price_for("typesafe-ai/jev") == (0.042, 0.0)
    assert price_for("typesafe-ai/jev") != _FALLBACK_PRICE
    # 출력 토큰은 세되(76) 값은 0 — 비용은 입력만으로 정해집니다.
    assert client.usage.cost_usd == pytest.approx(2 * 677 / 1e6 * 0.042)
    assert client.usage.cost_usd > 0


def test_the_preflight_is_one_cheap_check():
    fake = FakeJev(lambda name, args: {"probability": 0.99})
    client = jev_client(fake)
    assert asyncio.run(client.complete("Reply with the single word OK.", "ping", None)) == "OK"
    [call] = fake.tool_calls()
    assert call["params"]["name"] == "jev_check"
    assert call["params"]["arguments"] == {"state": "connectivity check",
                                           "question": "Is this a connectivity check?"}
    assert client.usage.calls == 1


def test_no_key_is_refused_at_construction(monkeypatch):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    with pytest.raises(LLMError, match="no API key"):
        LLMClient(LLMConfig(provider="jev"))
    monkeypatch.setenv("JEV_API_KEY", "from-env")
    assert LLMClient(LLMConfig(provider="jev")).config.resolved_key() == "from-env"


def test_the_undecided_knob_reaches_the_transport():
    """설정의 `llm.extra.undecided_below` 가 실제 호출의 판정까지 가는가."""
    split = head_answers({"strong_buy": 0.3, "buy": 0.25, "sell": 0.45})
    head = SEATS_BY_KEY["head"]

    def action(**config):
        fake = FakeJev(lambda name, args: {"answers": split})
        client = jev_client(fake, **config)
        return asyncio.run(client.complete(head.system, USER, head.schema))["action"]

    assert action() == "hold"                                  # 기본 65%: 매수 측 55%
    assert action(extra={"undecided_below": 0.5}) == "strong_buy"
    assert action(extra={"undecided_below": 0}) == "strong_buy"  # 규칙 끔
    assert action(extra={"undecided_below": 0.9}) == "hold"


def test_config_contract():
    assert LLMConfig(provider="jev").resolved_model() == "typesafe-ai/jev"
    assert billing_hint("jev") == ("Jev", "")
    with pytest.raises(LLMError):
        LLMClient(LLMConfig(provider="jev", api_key="k",
                            extra={"undecided_below": "sometimes"}))


# ─────────────────────────────────────────────────────────────────────────────
# 끝에서 끝까지 — 데스크 16석이 전부 Jev 로
# ─────────────────────────────────────────────────────────────────────────────
def desk_answers(scenario: str):
    bearish = scenario == "bearish"

    def answer(name, args):
        if name == "jev_check":
            return {"probability": 0.99}
        q = args["questions"]
        seat = args["state"]["seat"]
        if "stance" in q:
            lean = ({"bullish": 0.03, "neutral": 0.07, "bearish": 0.9} if bearish
                    else {"bullish": 0.9, "neutral": 0.07, "bearish": 0.03})
            return {"answers": {"stance": choice(lean), "data_sufficient": boolean(0.9)}}
        if "case" in q:
            strong = (seat == "Bull Researcher") != bearish
            return {"answers": {"case": score([0, 0, 0.2, 0.6, 0.2] if strong
                                              else [0.6, 0.4, 0, 0, 0])}}
        if "hazard" in q:
            return {"answers": {"scale": score([0, 0, 0, 0.2, 0.8]),
                                "hazard": choice({**dict.fromkeys(jev.HAZARDS, 0.0),
                                                  "none": 1.0})}}
        if "veto" in q:
            vetoed = scenario == "veto"
            reason = dict.fromkeys(jev.VETO_REASONS, 0.0)
            reason["illiquid" if vetoed else "none_applies"] = 1.0
            return {"answers": {"veto": boolean(0.8 if vetoed else 0.05),
                                "scale": score([0, 0, 0, 0, 1]),
                                "veto_reason": choice(reason)}}
        if "rating" in q:
            rating = dict.fromkeys(jev.RATINGS, 0.0)
            rating["sell" if bearish else "buy"] = 1.0
            return {"answers": {"rating": choice(rating),
                                "winner": choice({"bull": 0.1, "bear": 0.9, "balanced": 0}
                                                 if bearish else
                                                 {"bull": 0.9, "bear": 0.1, "balanced": 0})}}
        if "entry_style" in q:
            return {"answers": {"action": choice({"buy": 0, "hold": 0.1, "sell": 0.9}
                                                 if bearish else
                                                 {"buy": 0.9, "hold": 0.1, "sell": 0}),
                                "entry_style": choice({"market_now": 0, "limit_patient": 1,
                                                       "scale_in": 0,
                                                       "wait_for_pullback": 0}),
                                "tranches": score([0, 1, 0, 0])}}
        assert "dissent" in q and seat == "Head of Desk"
        actions = {
            "bullish": {"strong_buy": 0.85, "buy": 0.1, "hold": 0.05},
            "veto": {"strong_buy": 0.85, "buy": 0.1, "hold": 0.05},
            "bearish": {"sell": 0.8, "strong_sell": 0.1, "hold": 0.1},
            "split": {"strong_buy": 0.3, "buy": 0.25, "sell": 0.45},
            "reduce": {"hold": 0.30, "reduce": 0.45, "sell": 0.25},
        }[scenario]
        return {"answers": head_answers(actions)}

    return answer


def jev_desk(scenario: str):
    fake = FakeJev(desk_answers(scenario))
    desk = TradingDesk(LLMConfig(provider="jev", api_key="test"), debate_rounds=1,
                       risk_debate_rounds=1, memory=False)
    desk.client._client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return desk, fake


def test_one_deliberation_is_sixteen_jev_calls_in_desk_order():
    desk, fake = jev_desk("bullish")
    insights = run_desk(desk, make_ctx())

    assert desk.decision_client is desk.client
    assert len(fake.tool_calls("jev_check")) == 1          # 사전 점검
    evaluations = fake.tool_calls("jev_evaluate")
    assert len(evaluations) == 16
    seats = [c["params"]["arguments"]["state"]["seat"] for c in evaluations]
    assert sorted(seats) == sorted(s.title_en for s in ALL_SEATS)
    # 단계 순서: 분석 8석(동시) → 강세 → 약세 → 리스크 2석(동시) → 중립 → 계획 → 트레이더 → 헤드
    assert seats[8:10] == ["Bull Researcher", "Bear Researcher"]
    assert set(seats[10:12]) == {"Aggressive Risk", "Conservative Risk"}
    assert seats[12:] == ["Neutral Risk", "Research Manager", "Trader", "Head of Desk"]
    # 뒷좌석은 앞좌석이 남긴 것을 읽습니다 — 약세론자는 강세론자의 논거를,
    # 헤드는 분석가의 확률과 트레이더의 실행안을.
    bear_evidence = evaluations[9]["params"]["arguments"]["state"]["evidence"]
    assert "지금까지의 토론" in bear_evidence and "매수 논거 강도" in bear_evidence
    head_state = evaluations[15]["params"]["arguments"]["state"]
    assert "강세 90%" in head_state["evidence"]
    assert "지정가 대기" in head_state["evidence"]
    assert "트레이더 실행안" in head_state["glossary"]

    decision = desk.history[-1]
    assert decision.llm_calls == 16 and not decision.degraded
    assert decision.action == "strong_buy"
    assert desk.status()["llm_calls"] == 17
    assert len(insights) == 1 and insights[0].direction is Direction.UP
    assert insights[0].confidence == pytest.approx(0.95)


def test_bearish_jev_desk_closes_the_position():
    desk, _ = jev_desk("bearish")
    insights = run_desk(desk, make_ctx(invested=100))
    assert desk.history[-1].action == "sell"
    assert len(insights) == 1 and insights[0].direction is Direction.FLAT


def test_a_coin_flip_head_is_a_hold_and_emits_nothing():
    desk, _ = jev_desk("split")
    assert run_desk(desk, make_ctx(invested=100)) == []
    decision = desk.history[-1]
    assert decision.action == "hold"
    assert "판단 보류" in decision.rationale


def test_a_jev_veto_closes_the_position():
    desk, _ = jev_desk("veto")
    insights = run_desk(desk, make_ctx(invested=100))
    assert len(insights) == 1 and insights[0].direction is Direction.FLAT
    assert "리스크 거부" in insights[0].tag
    assert desk.history[-1].vetoed


def test_a_rejected_key_disables_the_desk_with_the_jev_name():
    def handler(request):
        return httpx.Response(403, json={"error": "invalid_token"})

    desk = TradingDesk(LLMConfig(provider="jev", api_key="bad"), memory=False)
    desk.client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert run_desk(desk, make_ctx()) == []
    assert desk.status()["enabled"] is False
    assert "Jev" in desk.status()["disabled_reason"]


@pytest.mark.parametrize("failure", [
    tool_error("upstream gateway timeout"),
    rpc_error(-32603, "Internal error"),
], ids=["isError", "rpc-32603"])
def test_one_transient_head_failure_does_not_sell_a_held_position(failure, no_backoff):
    """보유 중 · 분석가는 약세 · 헤드는 반반(보류 → 관망) 인 자리.

    헤드 호출이 일시 장애 한 번을 받았을 때 "요청이 틀렸다(422)" 로 읽으면
    재시도 없이 분석가 합의로 물러서고, 그 합의가 sell 이라 보유를 닫았습니다.
    같은 장애가 HTTP 503 으로 왔으면 한 번 더 묻고 관망했을 자리입니다.
    """
    bearish, split = desk_answers("bearish"), desk_answers("split")
    head_calls: list[int] = []

    def answer(name, args):
        if name != "jev_evaluate" or args["state"]["seat"] != "Head of Desk":
            return bearish(name, args)
        head_calls.append(1)
        return failure if len(head_calls) == 1 else split(name, args)

    fake = FakeJev(answer)
    desk = TradingDesk(LLMConfig(provider="jev", api_key="test"), debate_rounds=1,
                       risk_debate_rounds=1, memory=False)
    desk.client._client = httpx.AsyncClient(transport=httpx.MockTransport(fake))

    assert run_desk(desk, make_ctx(invested=100)) == []
    decision = desk.history[-1]
    assert len(head_calls) == 2                    # 한 번 더 물었고
    assert decision.action == "hold" and not decision.degraded
    assert "판단 보류" in decision.rationale


def test_quota_reported_as_a_tool_error_stops_the_desk(no_backoff):
    """하루 한도를 도구 오류로 알려 와도 데스크는 멈춥니다 — 봉마다 16번 실패하지 않고."""
    def answer(name, args):
        if name == "jev_check":
            return {"probability": 0.99}
        return tool_error("Daily quota exceeded for this token")

    fake = FakeJev(answer)
    desk = TradingDesk(LLMConfig(provider="jev", api_key="test"), memory=False)
    desk.client._client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    assert run_desk(desk, make_ctx()) == []
    status = desk.status()
    assert status["enabled"] is False
    assert "Jev" in status["disabled_reason"] and "한도" in status["disabled_reason"]
    assert len(fake.tool_calls("jev_evaluate")) <= len(desk.analyst_seats)
    assert no_backoff == []                        # 기다리지도 않았다


@pytest.mark.parametrize("base_url", ["https://jev.example.test/wrong?k=secret", ""])
def test_a_wrong_jev_address_is_not_diagnosed_as_a_model_name(base_url):
    """Jev 에는 고를 모델이 없습니다. 404 는 주소 문제이지 모델 이름 문제가 아닙니다."""
    def handler(request):
        # Vercel 이 없는 경로에 돌려주는 모양 그대로(text/plain).
        return httpx.Response(404, text="The page could not be found\n\nNOT_FOUND")

    desk = TradingDesk(LLMConfig(provider="jev", api_key="k", base_url=base_url),
                       memory=False)
    desk.client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert run_desk(desk, make_ctx()) == []
    reason = desk.status()["disabled_reason"]
    assert desk.status()["enabled"] is False
    assert "Jev 주소를 찾을 수 없습니다" in reason and "llm.base_url" in reason
    assert "모델 이름" not in reason
    assert "NOT_FOUND" in reason                   # 원문도 남깁니다
    if base_url:
        assert "secret" not in reason              # 직접 적은 주소는 옮기지 않습니다
    else:
        assert JEV_DEFAULT_URL in reason
