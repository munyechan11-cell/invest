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
    # 미시구조 좌석만 체결 가능성을 하나 더 묻습니다(위 표는 단계별 마지막 좌석).
    assert sorted(request("microstructure").questions) == [
        "data_sufficient", "execution", "stance"]


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
    # 진입 방식·분할 수는 각자 칸에 있고 화면이 앞에 붙입니다 — 설명에 또 적지 않습니다.
    assert out["execution_note"].startswith("매수(buy) 80%. ")
    assert "분할 진입" not in out["execution_note"] and "회 분할" not in out["execution_note"]
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
    assert "60%" in out["rationale"]                  # 얼마나 가까웠나는 근거가 말합니다
    # 확신도는 **최종 행동(관망)** 이 속한 쪽의 확률 — 트레이더와 같습니다.
    # "관망 · 확신 60%" 는 관망에 60% 를 건 것으로 읽힙니다(실제로는 10%).
    assert out["conviction"] == pytest.approx(0.1)


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
        "analyst": {"stance": choice({"bullish": 1.0}), "data_sufficient": boolean(1),
                    "execution": choice({"conditional": 1.0})},
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
    "analyst": {"stance": choice({"bullish": 1.0}), "data_sufficient": boolean(1),
                "execution": choice({"executable": 1.0})},
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
        # 단계의 질문은 모두 그대로입니다. 좌석에만 있는 질문(미시구조의 체결
        # 가능성)은 좌석을 알아봐야 붙습니다.
        assert set(req.questions) == set(jev._QUESTIONS[seat.stage](None)), seat.key
        extra = set(known.questions) - set(req.questions)
        assert extra == ({"execution"} if seat.key == "microstructure" else set()), seat.key
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

#: 시작 점검(`jev.preflight_arguments`)에 대한 정상 답.
PREFLIGHT_ANSWERS = {
    "check_choice": choice({"blue": 0.97, "other": 0.03}),
    "check_boolean": boolean(0.98),
    "check_score": score([0.0, 0.05, 0.95]),
}


def is_preflight(args: dict) -> bool:
    return set((args or {}).get("questions") or {}) == set(jev.PREFLIGHT_QUESTIONS)


def seat_calls(fake) -> list:
    """좌석의 `jev_evaluate` 만 — 시작 점검을 뺀 것."""
    return [c for c in fake.tool_calls("jev_evaluate")
            if not is_preflight(c["params"]["arguments"])]


class FakeJev:
    """MCP streamable HTTP 서버 흉내. 요청을 전부 적어 둡니다."""

    def __init__(self, answer=None, *, sessions=("sess-1", "sess-2", "sess-3"),
                 sse=False, structured=False, expire_first_call=False):
        self.answer = answer or (lambda name, args: {
            "answers": PREFLIGHT_ANSWERS if is_preflight(args) else ANALYST_ANSWERS})
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
    "MCP error -32602: Invalid arguments for tool jev_evaluate: questions",
    "Unknown tool: jev_evalute",
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


def test_the_preflight_is_one_small_call_in_the_seats_own_format():
    """시작 점검은 좌석과 같은 도구·같은 질문 모양 — 선택, 예/아니오, 단계 점수."""
    fake = FakeJev()
    client = jev_client(fake)
    assert asyncio.run(client.complete("Reply with the single word OK.", "ping", None)) == "OK"
    [call] = fake.tool_calls()
    assert call["params"]["name"] == "jev_evaluate"
    assert call["params"]["arguments"] == jev.preflight_arguments()
    types = sorted(q["type"] for q in call["params"]["arguments"]["questions"].values())
    assert types == ["boolean", "choice", "score"]
    # 좌석이 쓰는 것과 같은 빌더로 만든 질문입니다 — 형식이 갈라질 수 없습니다.
    seat_questions = request("risk_neutral").questions
    for mine, seats in (("check_choice", "veto_reason"), ("check_boolean", "veto"),
                        ("check_score", "scale")):
        q = call["params"]["arguments"]["questions"][mine]
        assert set(q) == set(seat_questions[seats]), mine
        assert type(q["criteria"]) is type(seat_questions[seats]["criteria"]), mine
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
        if is_preflight(args):
            return {"answers": PREFLIGHT_ANSWERS}
        q = args["questions"]
        seat = args["state"]["seat"]
        if "stance" in q:
            lean = ({"bullish": 0.03, "neutral": 0.07, "bearish": 0.9} if bearish
                    else {"bullish": 0.9, "neutral": 0.07, "bearish": 0.03})
            out = {"stance": choice(lean), "data_sufficient": boolean(0.9)}
            if "execution" in q:                             # 미시구조 좌석
                out["execution"] = choice({"executable": 0.8, "conditional": 0.15,
                                           "not_executable": 0.05})
            return {"answers": out}
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
    preflight = [c for c in fake.tool_calls() if is_preflight(c["params"]["arguments"])]
    assert len(preflight) == 1                             # 사전 점검
    evaluations = seat_calls(fake)
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
    assert '"entry_style": "limit_patient"' in head_state["evidence"]
    assert "매수(buy) 90%" in head_state["evidence"]
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
    # 예전의 넓은 "입력 오류" 낱말("too long", "exceeds", "validation")이 걸리던 문장,
    # 그리고 서버 문장 속의 상태 코드 — 둘 다 422 로 읽혀 재시도 없이 팔았습니다.
    tool_error("The upstream model took too long to respond, please try again"),
    tool_error("Upstream error: output validation failed, retry"),
    rpc_error(-32603, "Internal error: upstream returned 400: overloaded"),
], ids=["isError", "rpc-32603", "isError-too-long", "isError-validation",
        "rpc-embedded-400"])
def test_one_transient_head_failure_does_not_sell_a_held_position(failure, no_backoff):
    """보유 중 · 분석가는 약세 · 헤드는 반반(보류 → 관망) 인 자리.

    헤드 호출이 일시 장애 한 번을 받았을 때 "요청이 틀렸다(422)" 로 읽으면
    재시도 없이 분석가 합의로 물러서고, 그 합의가 sell 이라 보유를 닫았습니다.
    같은 장애가 HTTP 503 으로 왔으면 한 번 더 묻고 관망했을 자리입니다.
    """
    bearish, split = desk_answers("bearish"), desk_answers("split")
    head_calls: list[int] = []

    def answer(name, args):
        if name != "jev_evaluate" or args["state"].get("seat") != "Head of Desk":
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
        if is_preflight(args):
            return {"answers": PREFLIGHT_ANSWERS}
        return tool_error("Daily quota exceeded for this token")

    fake = FakeJev(answer)
    desk = TradingDesk(LLMConfig(provider="jev", api_key="test"), memory=False)
    desk.client._client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    assert run_desk(desk, make_ctx()) == []
    status = desk.status()
    assert status["enabled"] is False
    assert "Jev" in status["disabled_reason"] and "한도" in status["disabled_reason"]
    assert len(seat_calls(fake)) <= len(desk.analyst_seats)
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


# ─────────────────────────────────────────────────────────────────────────────
# 1차 점검에서 고친 것들 — 각 테스트는 고치기 전 코드에서 실패합니다
# ─────────────────────────────────────────────────────────────────────────────
def held_desk(answer, **kw):
    """보유 100주 · 토론·리스크 1라운드 · 가짜 Jev 로 도는 데스크."""
    fake = FakeJev(answer)
    desk = TradingDesk(LLMConfig(provider="jev", api_key="test"), debate_rounds=1,
                       risk_debate_rounds=1, memory=False, **kw)
    desk.client._client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return desk, fake


# ── 오류 분류: 일시 장애는 다시 묻는다 ───────────────────────────────────────
@pytest.mark.parametrize("failure", [
    tool_error("The upstream model took too long to respond, please try again"),
    tool_error("Gateway timeout: response time exceeds the 25s limit"),
    tool_error("Upstream error: output validation failed, retry"),
    tool_error("Upstream provider returned 400: overloaded, retry later"),
    rpc_error(-32603, "Internal error: gateway status 404: upstream"),
], ids=["too-long", "exceeds", "validation", "embedded-400", "embedded-404"])
def test_transient_wording_is_asked_again_not_read_as_bad_input(failure, no_backoff):
    """입력 오류를 일시 장애로 읽으면 재시도 두 번이 더 들 뿐입니다. 반대로 읽으면
    헤드에서 한 번에 떨어져 분석가 합의로 물러서고, 그 합의가 매도였습니다."""
    fake = FakeJev(fail_once(failure))
    assert ask_technical(jev_client(fake))["stance"] == "bullish"
    assert len(fake.tool_calls()) == 2


def test_the_status_tag_is_read_only_at_the_front():
    assert llm_client._status_of("jev 503: Upstream provider returned 400: x") == 503
    assert llm_client._status_of("google 429: Quota exceeded") == 429
    assert llm_client._status_of("Upstream provider returned 400: x") is None


# ── 돈·할당량 소진: 곧바로 멈춘다 ────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "Quota exceeded for this token",
    "Insufficient funds: add credits to continue",
    "Payment required",
    "Your credit balance is too low",
    "Billing: card declined",
])
def test_money_or_quota_exhaustion_from_jev_stops_at_once(text, no_backoff):
    fake = FakeJev(lambda name, args: tool_error(text))
    with pytest.raises(QuotaExhausted):
        ask_technical(jev_client(fake))
    assert len(fake.tool_calls()) == 1              # 재시도하지 않았고
    assert no_backoff == []                          # 기다리지도 않았다


def test_http_402_is_quota_exhausted(no_backoff):
    fake = FakeJev(lambda name, args: httpx.Response(402, json={"error": "payment_required"}))
    with pytest.raises(QuotaExhausted, match="jev 402"):
        ask_technical(jev_client(fake))
    assert len(fake.tool_calls()) == 1 and no_backoff == []


def test_an_http_429_about_credits_is_quota_exhausted(no_backoff):
    fake = FakeJev(lambda name, args: httpx.Response(
        429, json={"error": {"message": "Insufficient credits"}}))
    with pytest.raises(QuotaExhausted):
        ask_technical(jev_client(fake))
    assert len(fake.tool_calls()) == 1


def test_a_per_minute_quota_is_still_waited_out(no_backoff):
    """짧은 창을 말하는 한도는 기다리면 풀립니다 — 데스크를 세울 일이 아닙니다."""
    fake = FakeJev(fail_once(tool_error("Quota exceeded: 60 requests per minute")))
    assert ask_technical(jev_client(fake))["stance"] == "bullish"
    assert len(fake.tool_calls()) == 2
    # 제미나이가 쓰는 공용 규칙은 그대로 — 분 단위 할당량은 일시 429 입니다.
    assert not llm_client._is_long_exhaustion(
        "google 429: Quota exceeded for quota metric 'requests' per minute")


def test_credits_running_out_mid_deliberation_stop_the_desk_without_selling(no_backoff):
    """분석가는 약세로 답했고, 그 뒤 잔액이 떨어졌습니다. 예전에는 뒷좌석이 각자
    세 번씩 실패하고 헤드도 실패해 분석가 합의(매도)로 보유를 닫았고, 데스크는
    켜진 채로 다음 봉에 또 16석을 돌렸습니다."""
    bearish = desk_answers("bearish")

    def answer(name, args):
        if (name == "jev_evaluate" and not is_preflight(args)
                and "stance" not in args["questions"]):
            return tool_error("Insufficient funds: add credits to continue")
        return bearish(name, args)

    desk, fake = held_desk(answer)
    assert run_desk(desk, make_ctx(invested=100)) == []
    assert desk.history == []
    status = desk.status()
    assert status["enabled"] is False
    assert "기다려도 풀리지 않습니다" in status["disabled_reason"]
    assert "다시 시작" in status["disabled_reason"]
    assert len(seat_calls(fake)) == 8 + 1                   # 분석가 8석 + 강세론자 한 번
    assert no_backoff == []


# ── 마지막 시도에서 온 한도 소진 ─────────────────────────────────────────────
def long_wait_429(name, args):
    return httpx.Response(429, json={"error": {"message": "Too many requests, retry in 60s"}})


def test_a_long_wait_429_with_one_attempt_is_quota_exhausted(no_backoff):
    fake = FakeJev(long_wait_429)
    with pytest.raises(QuotaExhausted):
        ask_technical(jev_client(fake, max_retries=1))


def test_a_long_wait_429_after_two_blips_is_quota_exhausted(no_backoff):
    calls = []

    def answer(name, args):
        calls.append(1)
        return (tool_error("upstream gateway timeout") if len(calls) < 3
                else long_wait_429(name, args))

    with pytest.raises(QuotaExhausted):
        ask_technical(jev_client(FakeJev(answer)))
    assert len(calls) == 3


def test_the_last_attempt_rule_holds_for_every_provider(no_backoff):
    def handler(request):
        return httpx.Response(429, json={"error": {
            "message": "Quota exceeded for quota metric per day"}})

    client = LLMClient(LLMConfig(provider="google", api_key="k", max_retries=1))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(QuotaExhausted):
        asyncio.run(client.complete("s", "u", {"type": "object", "properties": {}}))


def test_quota_at_the_head_after_two_blips_stops_the_desk_instead_of_selling(no_backoff):
    bearish = desk_answers("bearish")
    head: list[int] = []

    def answer(name, args):
        if name != "jev_evaluate" or args["state"].get("seat") != "Head of Desk":
            return bearish(name, args)
        head.append(1)
        return (tool_error("upstream gateway timeout") if len(head) < 3
                else tool_error("Daily quota exceeded for this token"))

    desk, _ = held_desk(answer)
    assert run_desk(desk, make_ctx(invested=100)) == []     # 매도하지 않았다
    assert len(head) == 3 and desk.history == []
    assert desk.status()["enabled"] is False                # 그리고 데스크가 섰다
    assert "한도" in desk.status()["disabled_reason"]


def test_quota_at_the_head_disables_the_desk():
    """헤드는 `_safe_ask` 를 거치지 않아, 헤드에서 온 소진은 데스크를 끄지 않았습니다."""
    split = desk_answers("split")

    def answer(name, args):
        if name == "jev_evaluate" and args["state"].get("seat") == "Head of Desk":
            return tool_error("Daily quota exceeded for this token")
        return split(name, args)

    desk, fake = held_desk(answer)
    assert run_desk(desk, make_ctx(invested=100)) == []
    assert desk.status()["enabled"] is False
    before = len(fake.tool_calls())
    ctx = make_ctx(invested=100)
    assert asyncio.run(desk.update(ctx, {SYM.key: ctx.history(SYM, 1)[0]})) == []
    assert len(fake.tool_calls()) == before                 # 다음 봉에 다시 돌지 않는다


# ── 판단 보류 기준값 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("value", [65, -0.65, 1.5, float("inf"), "65%"])
def test_an_out_of_range_undecided_knob_is_refused_at_startup(value):
    """65 (백분율로 잘못 적음) 를 1.0 으로 잘라 쓰면 95% 거부도 무시됩니다.
    -0.65 를 0 으로 잘라 쓰면 규칙이 꺼져 34% 매도가 그대로 나갑니다."""
    with pytest.raises(LLMError, match="undecided_below"):
        LLMClient(LLMConfig(provider="jev", api_key="k", extra={"undecided_below": value}))


@pytest.mark.parametrize("value", [0, 0.5, 0.65, 0.9, "0.7"])
def test_in_range_undecided_values_are_still_accepted(value):
    client = LLMClient(LLMConfig(provider="jev", api_key="k",
                                 extra={"undecided_below": value}))
    assert client._undecided_below == float(value)


@pytest.mark.parametrize("value", [1.0, 1, 0.99, 0.95, True, False])
def test_an_undecided_knob_that_silences_the_veto_is_refused_at_startup(value):
    """이 값은 거부 문턱이기도 합니다(P(거부) ≥ 이 값일 때만 거부).

    1.0 은 범위 안이라 통과했고, 그러면 99% 거부도 청산 대신 "배율 절반" 이었습니다.
    YAML 의 `undecided_below: yes`·`true` 는 bool 이라 `float()` 로 1.0 이 되어 같은
    결과였고(`no`·`off` 는 0 — 규칙이 조용히 꺼집니다), `_prob` 는 같은 bool 을
    확률로 받지 않습니다.
    """
    with pytest.raises(LLMError, match="undecided_below"):
        LLMClient(LLMConfig(provider="jev", api_key="k", extra={"undecided_below": value}))


def test_raising_undecided_below_raises_the_veto_bar_too():
    """방향 문턱과 거부 문턱은 같은 값입니다 — "덜 사고팔게" 올리면 거부도 약해집니다.
    이 결합은 사용자가 정한 설계(둘 다 0.65)이고, 문서와 이 테스트가 그것을 적어 둡니다."""
    assert verdict(0.7)["veto"] is True                       # 기본 0.65
    weakened = verdict(0.7, u=0.8)
    assert weakened["veto"] is False and weakened["position_scale"] == 0.5
    assert verdict(0.85, u=0.8)["veto"] is True


# ── 공매도와 위원회 ──────────────────────────────────────────────────────────
def test_a_jev_desk_cannot_turn_on_short_selling():
    """헤드는 sell 을 '보유 청산' 으로 묻는데, 공매도가 켜지면 빈 장부에서 공매도를 엽니다."""
    from quant.config.schema import ModelSpec
    from quant.strategy.builder import _build_desk

    with pytest.raises(ValueError, match="allow_short"):
        TradingDesk(LLMConfig(provider="jev", api_key="k"), allow_short=True)
    with pytest.raises(ValueError, match="allow_short"):
        TradingDesk(ScriptedLLM(), decision_llm=LLMConfig(provider="jev", api_key="k"),
                    allow_short=True)
    with pytest.raises(ValueError, match="allow_short"):
        _build_desk(ModelSpec(type="desk", params={
            "llm": {"provider": "jev", "api_key": "k"}, "allow_short": True}), None)
    TradingDesk(LLMConfig(provider="jev", api_key="k"), memory=False)     # 끄면 그대로
    TradingDesk(ScriptedLLM(), allow_short=True, memory=False)           # 다른 제공자도


def test_the_council_refuses_jev():
    """위원회의 리스크 검토는 숫자 칸을 요구해 Jev 로는 한 번도 거부하지 못합니다."""
    from quant.alpha.council import ResearchCouncilAlpha
    from quant.config.schema import ModelSpec
    from quant.strategy.builder import _build_council

    with pytest.raises(ValueError, match="council"):
        _build_council(ModelSpec(type="council",
                                 params={"llm": {"provider": "jev", "api_key": "k"}}))
    with pytest.raises(ValueError, match="council"):
        ResearchCouncilAlpha(jev_client(FakeJev()))
    ResearchCouncilAlpha(ScriptedLLM())                                   # 다른 제공자는 그대로


# ── 시간 초과는 시간 초과라고 말한다 ─────────────────────────────────────────
def test_a_timed_out_call_says_it_was_a_timeout(no_backoff):
    def handler(request):
        raise httpx.ReadTimeout("", request=request)

    with pytest.raises(LLMError) as err:
        ask_technical(jev_client(handler))
    assert str(err.value).endswith("attempts: ReadTimeout")
    assert isinstance(err.value.__cause__, httpx.ReadTimeout)


# ── 선택지 설명은 데스크가 하는 일이다 ───────────────────────────────────────
def test_head_options_say_what_the_desk_does_to_other_models():
    """보유가 없는 종목이 대부분입니다. hold 는 다른 모델에게 맡기고, 청산 쪽은
    그 종목의 매수를 모든 모델에게서 막습니다(FLAT 은 포트폴리오의 거부권)."""
    criteria = request("head").questions["action"]["criteria"]
    assert "no order" in criteria["hold"] and "other models" in criteria["hold"]
    for exit_ in ("reduce", "sell", "strong_sell"):
        assert "whole position" in criteria[exit_]
        assert "block every model from buying" in criteria[exit_]
    assert "not held" in jev.LENSES["head"]

    ctx = make_ctx()                                        # 보유 없음
    desk, _ = jev_desk("bearish")
    insights = run_desk(desk, ctx)
    assert len(insights) == 1 and insights[0].direction is Direction.FLAT
    horizon = desk.history[-1].horizon_bars
    assert insights[0].period == ctx.bar_delta * max(horizon // 2, 2)
    desk, _ = jev_desk("split")                             # 관망 → 아무것도 내지 않는다
    assert run_desk(desk, make_ctx()) == []


def test_the_tick_ladder_rule_is_conditional_on_korean_stocks():
    lens = jev.LENSES["microstructure"]
    assert "for Korean stocks" in lens
    assert "(the Korean tick ladder)" not in lens


# ── 용어집 ───────────────────────────────────────────────────────────────────
def test_every_template_word_later_seats_read_is_glossed():
    """뒷좌석은 앞좌석의 Jev 출력(한국어 템플릿)을 증거로 읽습니다."""
    terms = [*jev.CASE_LEVELS_KO, *jev.SIZE_LEVELS_KO, *jev.HAZARD_KO.values(),
             *jev.VETO_REASON_KO.values(), *jev.ENTRY_KO.values(),
             *jev.WINNER_KO.values(), *jev.STANCE_KO.values(), *jev.ACTION_KO.values(),
             *jev._GROUP_KO.values(), *jev._HEAD_GROUP_KO.values(),
             *jev.EXECUTION_KO.values(), "체결 가능성",
             "실패", "논거", "강도", "제안 배율", "가장 유력", "거부", "재심의",
             # 좌석이 실패했을 때 데스크가 대신 적는 말(`desk.py`) — 뒷좌석이 읽습니다.
             "좌석 응답 실패", "응답 실패", "사이즈 축소", "다음 사이클 재평가"]
    missing = [t for t in terms if t not in jev.GLOSSARY]
    assert not missing, missing


def test_a_concentration_veto_is_not_glossed_as_excess_return():
    """"초과" 는 용어집에서 '벤치 대비 초과 수익' 입니다. 거부 사유에 쓰면 뒷좌석이
    그 뜻으로 읽습니다."""
    assert all("초과" not in text for text in jev.VETO_REASON_KO.values())
    out = verdict(0.9, reason="concentration_breach")
    g = jev.glossary_for(json.dumps(out, ensure_ascii=False))
    assert "초과" not in g
    assert "포트폴리오 집중도 한도를 넘음" in g


# ── 판단 보류의 확신도 ───────────────────────────────────────────────────────
def test_an_undecided_plan_states_the_hold_sides_probability():
    """계획 JSON 은 트레이더·헤드의 증거입니다. "rating hold, conviction 0.6" 은
    관망에 60% 를 건 것으로 읽힙니다 — 실제로는 관망 20%."""
    out = mapped("research_manager", {
        "rating": choice({"strong_buy": 0.1, "buy": 0.5, "hold": 0.2, "sell": 0.2,
                          "strong_sell": 0.0}),
        "winner": choice({"bull": 0.4, "bear": 0.4, "balanced": 0.2}),
    })
    assert out["rating"] == "hold" and "판단 보류" in out["rationale"]
    assert out["conviction"] == pytest.approx(0.2)
    assert "60%" in out["rationale"]                        # 얼마나 가까웠나는 근거에


# ── 다음 봉이라고 약속하지 않는다 ───────────────────────────────────────────
def test_no_template_promises_the_next_bar():
    """live_crypto 는 cadence_bars: 3 — 두 봉은 심의하지 않습니다."""
    outs = [mapped("head", head_answers(a)) for a in (
        {"buy": 1.0}, {"sell": 1.0}, {"hold": 1.0},
        {"strong_buy": 0.3, "buy": 0.25, "sell": 0.45})]
    outs.append(mapped("research_manager", {
        "rating": choice({"buy": 0.5, "hold": 0.2, "sell": 0.3}),
        "winner": choice({"balanced": 1.0})}))
    for out in outs:
        text = json.dumps(out, ensure_ascii=False)
        assert "다음 봉" not in text, text
    assert "재심의" in outs[0]["invalidation"]


# ── 화면의 말풍선 ────────────────────────────────────────────────────────────
def one_line(text: str) -> str:
    """index.html `oneLine` 과 같습니다 — 첫 문장만, 64자에서 자릅니다."""
    import re

    t = text.strip()
    m = re.search(r"[.!?。]\s|[.!?。]$", t)
    first = t[:m.start() + 1] if m and m.start() > 0 else t
    return first if len(first) <= 64 else first[:63].rstrip() + "…"


def risk_bubble(out: dict) -> str:
    """index.html 의 중립 리스크 말풍선과 같은 조립."""
    if out["veto"]:
        reason = out["veto_reason"].strip()
        joint = "" if not reason else " " if reason[-1] in ".!?。" else ". "
        return "⛔ 거부: " + reason + joint + out["reasoning"]
    return f"최종 배율 {round(out['position_scale'] * 100)}% — " + out["reasoning"]


def test_risk_and_trader_bubbles_have_a_short_first_sentence():
    html = open("quant/api/static/index.html", encoding="utf-8").read()
    assert '"⛔ 거부: " + reason + joint' in html          # 사유와 설명 사이를 띄운다

    cases = [verdict(0.9), verdict(0.9, reason="none_applies"), verdict(0.5),
             verdict(0.1)]
    for out in cases:
        assert "거부 확률" not in out["veto_reason"]          # 설명이 이미 말합니다
        bubble = risk_bubble(out)
        assert bubble.count("거부 확률") <= 1, bubble
        assert not one_line(bubble).endswith("…"), bubble

    ui_entry = {"market_now": "지금 시장가", "limit_patient": "지정가로 기다림",
                "scale_in": "나눠서 진입", "wait_for_pullback": "눌림목 기다림"}
    for action in ({"buy": 0.34, "hold": 0.33, "sell": 0.33}, {"buy": 0.8, "hold": 0.2}):
        out = mapped("trader", {"action": choice(action),
                                "entry_style": choice({"limit_patient": 1.0}),
                                "tranches": score([0, 0, 0, 1])})
        note = out["execution_note"]
        assert "지정가 대기" not in note and "회 분할" not in note   # 화면이 앞에 붙입니다
        bubble = f"{ui_entry[out['entry_style']]} · {out['tranches']}분할 — {note}"
        assert not one_line(bubble).endswith("…"), bubble


def test_templates_name_actions_in_korean_with_the_value_in_parentheses():
    out = mapped("head", head_answers({"strong_sell": 0.7, "hold": 0.3}))
    assert "적극 매도(strong_sell)" in out["rationale"]
    assert "→ strong_sell" not in out["rationale"]
    plan = mapped("research_manager", {"rating": choice({"sell": 1.0}),
                                       "winner": choice({"bear": 1.0})})
    assert "매도(sell)" in plan["strategic_actions"]
    assert "방향 sell" not in plan["strategic_actions"]


# ── 동시 심의의 호출 수 ──────────────────────────────────────────────────────
def test_each_concurrent_decision_counts_only_its_own_sixteen_calls():
    """출하 설정은 4종목을 동시에 심의합니다. 누적의 앞뒤 차이로 세면 종목마다
    64회로 적혔습니다 — 화면의 "AI 호출" 과 로그가 그 숫자를 보여 줬습니다."""
    from datetime import timedelta
    from decimal import Decimal

    from quant.core.account import Portfolio
    from quant.core.clock import SimClock
    from quant.core.context import Context
    from quant.core.events import EventBus
    from quant.core.types import Bar, RunMode, Symbol
    from quant.live.spend import SpendMeter
    from tests.test_desk import T0

    symbols = [Symbol(t, venue="kis", quote_currency="KRW", tick_size=Decimal("100"))
               for t in ("005930", "000660", "035420", "051910")]
    ctx = Context(SimClock(T0 + timedelta(days=260)), Portfolio(10_000_000.0, "KRW"),
                  EventBus(), timeframe="1d", run_mode=RunMode.DRY_RUN)
    ctx.universe = list(symbols)
    for i in range(260):
        for k, sym in enumerate(symbols):
            p = 70_000.0 * (1 + 0.0004 * i) * (1 + 0.1 * k)
            ctx.push_bar(Bar(sym, T0 + timedelta(days=i), p, p * 1.012, p * 0.988, p,
                             1e6, "1d"))

    fake = FakeJev(desk_answers("bullish"))
    desk = TradingDesk(LLMConfig(provider="jev", api_key="test"), debate_rounds=1,
                       risk_debate_rounds=1, memory=False, concurrent_symbols=4,
                       max_symbols_per_run=4)
    desk.client._client = httpx.AsyncClient(transport=httpx.MockTransport(yielding(fake)))
    metered: list[tuple[int, float]] = []
    desk.meter = SpendMeter(allow=lambda: (True, ""),
                            record=lambda c, s: metered.append((c, s)))
    asyncio.run(desk.on_start(ctx))
    asyncio.run(desk.update(ctx, {s.key: ctx.history(s, 1)[0] for s in symbols}))

    assert len(seat_calls(fake)) == 64
    assert sorted(d.symbol_key for d in desk.history) == sorted(s.key for s in symbols)
    assert [d.llm_calls for d in desk.history] == [16] * 4
    per_symbol = 16 * 677 / 1e6 * 0.042
    assert all(d.cost_usd == pytest.approx(per_symbol) for d in desk.history)
    assert metered == [(64, pytest.approx(4 * per_symbol))]   # 봉 계량은 합


# ── MCP 전송 — 시험되지 않던 길 ─────────────────────────────────────────────
def test_a_400_server_not_initialized_reinitialises_once():
    """상태를 가진 서버의 새 인스턴스는 404 가 아니라 400 "Server not initialized"
    를 돌려줍니다. 이 길이 없으면 좌석마다 재시도 없는 'jev 400' 입니다."""
    calls = []

    def answer(name, args):
        calls.append(name)
        if len(calls) == 1:
            return httpx.Response(400, json={"jsonrpc": "2.0", "id": None, "error": {
                "code": -32000, "message": "Bad Request: Server not initialized"}})
        return {"answers": ANALYST_ANSWERS}

    fake = FakeJev(answer)
    assert ask_technical(jev_client(fake))["stance"] == "bullish"
    assert fake.methods().count("initialize") == 2
    assert len(fake.tool_calls()) == 2


def test_the_negotiated_protocol_version_is_sent_afterwards():
    fake = FakeJev()

    def handler(request):
        response = fake(request)
        if json.loads(request.content).get("method") == "initialize":
            message = response.json()
            message["result"]["protocolVersion"] = "2025-03-26"
            return httpx.Response(200, headers={"mcp-session-id": fake.current},
                                  json=message)
        return response

    ask_technical(jev_client(handler))
    assert [m for m, _, _ in fake.log] == ["initialize", "notifications/initialized",
                                           "tools/call"]
    for _, headers, _ in fake.log[1:]:
        assert headers["mcp-protocol-version"] == "2025-03-26"


def test_an_initialize_error_fails_without_calling_the_tool():
    fake = FakeJev()

    def handler(request):
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            fake.log.append(("initialize", dict(request.headers), body))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {
                "code": -32602, "message": "Unsupported protocol version"}})
        return fake(request)

    with pytest.raises(LLMError, match="jev 422: initialize"):
        ask_technical(jev_client(handler))
    assert fake.tool_calls() == [] and fake.methods() == ["initialize"]


def test_a_rejected_initialized_notification_is_an_error():
    fake = FakeJev()

    def handler(request):
        if json.loads(request.content).get("method") == "notifications/initialized":
            return httpx.Response(400, json={"error": "bad_notification"})
        return fake(request)

    with pytest.raises(LLMError, match="jev 400"):
        ask_technical(jev_client(handler))
    assert fake.tool_calls() == []


# ── 거절 규칙의 경계 ─────────────────────────────────────────────────────────
def test_a_small_but_real_share_on_an_unknown_key_is_refused():
    """10% 가 모르는 이름에 가 있으면 반올림 오차가 아닙니다."""
    with pytest.raises(LLMError):
        jev.read_choice({"q": {"probabilities": {"buy": 0.5, "hold": 0.4, "HOLD": 0.1}}},
                        "q", ("buy", "hold", "sell"))


def test_a_six_option_head_answer_summing_to_ninety_percent_is_refused():
    probs = {"strong_buy": 0.3, "buy": 0.3, "hold": 0.1, "reduce": 0.1, "sell": 0.05,
             "strong_sell": 0.05}
    with pytest.raises(LLMError):
        jev.read_choice({"action": {"probabilities": probs}}, "action",
                        tuple(jev.HEAD_ACTIONS))


@pytest.mark.parametrize("p", [1.2, 1.5])
def test_a_boolean_above_one_is_refused_not_clipped(p):
    """예/아니오에는 합 검사가 없습니다 — 1 을 넘는 거부 확률은 여기서만 걸립니다."""
    with pytest.raises(LLMError):
        jev.read_boolean({"veto": {"probability": p}}, "veto")
    with pytest.raises(LLMError):
        verdict(p)


# ── 운영자에게 가는 문장 ─────────────────────────────────────────────────────
def test_the_quota_message_counts_the_calls_this_desk_actually_makes():
    desk, fake = jev_desk("bullish")
    said = desk._exhausted_reason(QuotaExhausted("jev 429: Daily quota exceeded"))
    assert "호출 16회" in said and "19회" not in said
    assert "다시 시작" in said                               # 저절로 켜지지 않습니다
    run_desk(desk, make_ctx())
    assert desk.calls_per_symbol() == len(seat_calls(fake)) == 16
    assert TradingDesk(ScriptedLLM(), memory=False).calls_per_symbol() == 18  # 토론 2라운드


def test_a_jev_preflight_timeout_points_at_the_address_not_a_model():
    class Hang:
        usage = llm_client.LLMUsage()
        config = LLMConfig(provider="jev", api_key="x")

        async def complete(self, *args, **kwargs):
            await asyncio.sleep(5)

    desk = TradingDesk(Hang(), deadline_s=0.05, memory=False)
    asyncio.run(desk.on_start(make_ctx()))
    reason = desk.status()["disabled_reason"]
    assert reason.startswith("Jev 응답이 없습니다")
    assert "llm.base_url" in reason and "모델" not in reason


# ─────────────────────────────────────────────────────────────────────────────
# 2차 점검에서 고친 것들 — 각 테스트는 고치기 전 코드에서 실패합니다
# ─────────────────────────────────────────────────────────────────────────────
# ── 리디렉션은 주소 문제다 ───────────────────────────────────────────────────
@pytest.mark.parametrize("status", [301, 307, 308])
def test_a_redirect_is_not_retried_and_names_only_the_new_host(status, no_backoff):
    """httpx 는 리디렉션을 따라가지 않습니다(따라가도 다른 호스트면 인증 헤더를
    뗍니다). 예전에는 3xx 의 빈 본문을 JSON 으로 읽다가 꼬리표 없는 오류가 나서
    일시 장애처럼 세 번 재시도했고, 어디로 옮겼는지는 버렸습니다."""
    methods: list[str] = []

    def handler(request):
        methods.append(json.loads(request.content).get("method"))
        return httpx.Response(status, headers={
            "location": "https://jev.example.com/api/mcp?token=secret"})

    with pytest.raises(LLMError) as err:
        ask_technical(jev_client(handler))
    text = str(err.value)
    assert text.startswith("jev 404:"), text              # 재시도하지 않는 꼬리표
    assert f"({status} → https://jev.example.com)" in text
    assert "secret" not in text and "/api/mcp" not in text  # 호스트까지만
    assert "llm.base_url" in text
    assert methods == ["initialize"] and no_backoff == []


def test_a_redirect_on_the_tool_call_is_not_retried_either(no_backoff):
    fake = FakeJev(lambda name, args: httpx.Response(
        308, headers={"location": "/moved"}))
    with pytest.raises(LLMError, match=r"^jev 404: .*308 → 같은 호스트의 다른 경로"):
        ask_technical(jev_client(fake))
    assert len(fake.tool_calls()) == 1 and no_backoff == []


def test_a_redirect_at_startup_points_at_the_address(no_backoff):
    """`http://` 로 적은 주소에 Vercel 은 308 → https 로 답합니다."""
    posts: list = []

    def handler(request):
        posts.append(request)
        return httpx.Response(308, headers={"location": "https://jev.example.com/api/mcp"})

    desk = TradingDesk(LLMConfig(provider="jev", api_key="k",
                                 base_url="http://jev.example.com/api/mcp"), memory=False)
    desk.client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert run_desk(desk, make_ctx()) == []
    reason = desk.status()["disabled_reason"]
    assert "Jev 주소를 찾을 수 없습니다" in reason and "llm.base_url" in reason
    assert "308 → https://jev.example.com" in reason
    assert len(posts) == 1 and no_backoff == []


# ── 거부가 무엇을 하는지 묻는다 ──────────────────────────────────────────────
def test_the_veto_question_says_what_a_veto_does_to_the_book():
    """리스크 좌석은 방향이 정해지기 전에 답합니다 — "이 거래" 는 아직 없습니다.
    거부는 헤드보다 앞서 보유를 전량 청산하고 모든 모델의 매수를 막습니다."""
    from quant.alpha.seats import VETO_BLOCK_BARS

    veto = request("risk_neutral").questions["veto"]
    for text in (veto["instructions"], jev.LENSES["risk_neutral"]):
        assert "closes the whole position" in text
        assert "blocks every model from buying" in text
        assert f"next {VETO_BLOCK_BARS} bars" in text
        assert "not a veto of one new order" in text
    assert "this trade" not in veto["instructions"]
    # 기준은 그대로 — 네 가지 사유(무엇을 묻는지만 바뀌었습니다).
    assert veto["criteria"] == {
        "true": "At least one of the four veto conditions is true",
        "false": "None of the four veto conditions is true; any concern is a matter "
                 "of size"}
    assert veto == edited("risk_verdict").questions["veto"]  # 못 알아본 좌석도 같게
    # 문장이 말하는 봉 수가 데스크가 실제로 막는 봉 수입니다.
    ctx = make_ctx(invested=100)
    desk, _ = jev_desk("veto")
    insights = run_desk(desk, ctx)
    assert len(insights) == 1 and insights[0].direction is Direction.FLAT
    assert insights[0].period == ctx.bar_delta * VETO_BLOCK_BARS


# ── 미시구조 좌석의 본업 ─────────────────────────────────────────────────────
MICRO = {"stance": choice({"bullish": 0.1, "neutral": 0.8, "bearish": 0.1}),
         "data_sufficient": boolean(0.9)}


def test_the_microstructure_seat_is_asked_whether_the_order_can_be_filled():
    """이 좌석은 방향을 부르지 않습니다. 공통 질문(방향·재료)만 받아 리포트가
    "중립 80%" 뿐이었고, 체결 비용 판단은 뒷좌석 어디에도 가지 않았습니다."""
    req = request("microstructure")
    assert set(req.questions) == {"stance", "data_sufficient", "execution"}
    assert set(req.questions["execution"]["criteria"]) == set(jev.EXECUTION)
    for seat in ALL_SEATS:                                  # 다른 좌석은 그대로
        if seat.key != "microstructure":
            assert "execution" not in jev.build_request(
                seat.system, USER, seat.schema).questions, seat.key

    out = mapped("microstructure", {**MICRO, "execution": choice(
        {"executable": 0.3, "conditional": 0.6, "not_executable": 0.1})})
    assert out["key_points"][0] == "체결 가능성: 조건부 체결 가능 60% (Jev 확률 판정)"
    assert out["risks"] == ["조건부 체결 가능 (60%)"]
    # 투표는 그대로입니다 — 방향·확신·재료는 체결 판단과 무관하게 같은 값.
    assert (out["stance"], out["conviction"], out["data_sufficient"]) == ("neutral", 0.8, True)
    fine = mapped("microstructure", {**MICRO, "execution": choice({"executable": 0.9,
                                                                    "conditional": 0.1})})
    assert fine["key_points"][0].startswith("체결 가능성: 목표 사이즈로 체결 가능 90%")
    assert "risks" not in fine
    thin = mapped("microstructure", {
        "stance": choice({"neutral": 1.0}), "data_sufficient": boolean(0.2),
        "execution": choice({"not_executable": 0.7, "conditional": 0.3})})
    assert thin["key_points"][0].startswith("판단 재료 부족")   # 재료 부족이 먼저
    assert thin["key_points"][1].startswith("체결 가능성: 체결 곤란 70%")
    # 뒷좌석이 읽는 말은 전부 용어집에 있습니다.
    glossary = jev.glossary_for(json.dumps([out, fine, thin], ensure_ascii=False))
    for term in ("체결 가능성", *jev.EXECUTION_KO.values()):
        assert term in glossary, term


def test_later_seats_read_the_execution_judgment():
    desk, fake = jev_desk("bullish")
    run_desk(desk, make_ctx())
    micro = desk.history[-1].analysts["microstructure"]
    assert micro["key_points"][0].startswith("체결 가능성:")
    bull = next(c for c in seat_calls(fake)
                if c["params"]["arguments"]["state"]["seat"] == "Bull Researcher")
    assert "체결 가능성" in bull["params"]["arguments"]["state"]["evidence"]


# ── 트레이더에게 증거에 있는 것으로 묻는다 ───────────────────────────────────
def test_the_trader_is_asked_only_about_what_its_evidence_shows():
    """트레이더의 증거에는 변동성·RSI·볼린저가 없고 신호의 수명도 없습니다."""
    seat = SEATS_BY_KEY["trader"]
    assert "기술지표" not in seat.brief_sections and "통계" not in seat.brief_sections
    q = request("trader").questions
    texts = [q["entry_style"]["instructions"], *q["entry_style"]["criteria"].values(),
             q["tranches"]["instructions"], *q["tranches"]["criteria"],
             jev.LENSES["trader"]]
    for text in texts:
        lowered = text.lower()
        for absent in ("volatility", "overheated", "signal speed", "fade",
                       "rsi", "bollinger", "atr"):
            assert absent not in lowered, (absent, text)
    joined = " ".join(texts)
    for present in ("5-bar", "52-week high", "round-trip cost", "daily volume", "spread"):
        assert present in joined, present
    # 그 대용치는 실제로 트레이더의 증거에 있습니다.
    brief = TradingDesk(ScriptedLLM(), memory=False).build_brief(make_ctx(), SYM)
    shown = json.dumps({k: brief[k] for k in seat.brief_sections}, ensure_ascii=False)
    for key in ("5봉수익률%", "20봉수익률%", "52주고점대비%", "왕복비용추정%",
                "1%포지션의거래량비중%", "호가스프레드%"):
        assert key in shown, key


# ── 대체값의 "사이즈 축소" 는 청산이 아니다 ─────────────────────────────────
def test_the_risk_fallback_is_not_glossed_as_a_close():
    """리스크 좌석이 실패하면 데스크는 "안전을 위해 사이즈 축소" 라고 적습니다.
    용어집에 "축소" 만 있으면 그 말이 헤드 행동의 뜻(전량 청산)으로 풀렸습니다."""
    desk = TradingDesk(ScriptedLLM(fail_seats=("risk_verdict",)), memory=False)
    run_desk(desk, make_ctx())
    fallback = desk.history[-1].risk
    assert "사이즈 축소" in fallback["reasoning"] and fallback.get("error")
    g = jev.glossary_for(json.dumps(fallback, ensure_ascii=False))
    assert "not a close" in g["사이즈 축소"]
    plan = TradingDesk(ScriptedLLM(fail_seats=("plan",)), memory=False)
    run_desk(plan, make_ctx())
    actions = plan.history[-1].plan["strategic_actions"]
    assert actions in jev.GLOSSARY                            # "다음 사이클 재평가"


# ── 분석가도 보유기간을 안다 ─────────────────────────────────────────────────
def test_analysts_are_told_the_holding_period_they_are_asked_about():
    """방향 질문은 "보유기간 동안" 을 묻는데, 그 기간은 헤드에게만 적혀 있었습니다."""
    stance = request("macro").questions["stance"]["instructions"]
    assert "default holding period" in stance and "stated in the evidence" in stance
    desk, fake = jev_desk("bullish")
    run_desk(desk, make_ctx())
    analysts = [c for c in seat_calls(fake) if "stance" in c["params"]["arguments"]["questions"]]
    assert len(analysts) == 8
    for call in analysts:
        state = call["params"]["arguments"]["state"]
        assert f"기본 보유기간은 {desk.default_horizon}봉이다" in state["evidence"]
        assert "기본 보유기간" in state["glossary"] and "봉" in state["glossary"]


# ── 시작 점검은 좌석의 질문 형식을 본다 ─────────────────────────────────────
def test_a_rejected_question_format_turns_the_desk_off_at_startup():
    """서버가 좌석의 질문 형식을 거절하면(-32602) 예전 점검(`jev_check`)은
    통과했고, 데스크는 켜진 채 봉마다 16석이 실패했습니다."""
    def answer(name, args):
        if name == "jev_check":                   # 키와 연결은 멀쩡합니다
            return {"probability": 0.99}
        return rpc_error(-32602, "MCP error -32602: Invalid arguments for tool "
                                 "jev_evaluate: questions.check_choice.criteria: "
                                 "Expected array, received object")

    fake = FakeJev(answer)
    desk = TradingDesk(LLMConfig(provider="jev", api_key="k"), debate_rounds=1,
                       risk_debate_rounds=1, memory=False)
    desk.client._client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    assert run_desk(desk, make_ctx()) == []
    status = desk.status()
    assert status["enabled"] is False
    assert status["disabled_reason"].startswith("Jev 가 데스크의 질문 형식을 거부했습니다")
    assert "-32602" in status["disabled_reason"]
    assert len(fake.tool_calls()) == 1                       # 16석은 돌지 않았다


def test_a_changed_answer_shape_also_turns_the_desk_off_at_startup(no_backoff):
    fake = FakeJev(lambda name, args: {"probability": 0.99} if name == "jev_check"
                   else {"answers": {"verdicts": {}}})
    desk = TradingDesk(LLMConfig(provider="jev", api_key="k"), memory=False)
    desk.client._client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    assert run_desk(desk, make_ctx()) == []
    reason = desk.status()["disabled_reason"]
    assert reason.startswith("Jev ") and "check_choice" in reason
    assert seat_calls(fake) == []


# ── 사전 점검의 안내는 원인을 가리킨다 ──────────────────────────────────────
@pytest.mark.parametrize("response", [
    httpx.Response(406, json={"jsonrpc": "2.0", "id": None, "error": {
        "code": -32000, "message": "Not Acceptable: Client must accept both "
                                   "application/json and text/event-stream"}}),
    httpx.Response(405, text="Method Not Allowed"),
    httpx.Response(200, text="<!doctype html><title>Authentication Required</title>",
                   headers={"content-type": "text/html"}),
], ids=["406", "405", "html-page"])
def test_a_protocol_failure_at_startup_names_jev_and_the_address(response, no_backoff):
    """키도 한도도 아닌 실패(형식·주소·배포 보호 페이지)가 "LLM 사전 점검 실패"
    로 떨어져, 화면은 거기에 "LLM 키와 한도를 확인하세요" 를 붙였습니다."""
    desk = TradingDesk(LLMConfig(provider="jev", api_key="k"), memory=False)
    desk.client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: response))
    assert run_desk(desk, make_ctx()) == []
    reason = desk.status()["disabled_reason"]
    assert reason.startswith("Jev 연결 실패"), reason
    assert "llm.base_url" in reason and "키" not in reason
    assert "LLM 사전 점검 실패" not in reason


# ── 계량·한도 판정의 빈틈 (돌연변이가 살아남던 자리) ──────────────────────────
@pytest.mark.parametrize("response", [
    lambda: httpx.Response(402, json={"error": "payment_required"}),
    lambda: {"jsonrpc": "2.0", "result": {"isError": True, "content": [
        {"type": "text", "text": "Insufficient funds: top up your balance"}]}},
], ids=["http-402", "tool-error"])
def test_money_running_out_at_startup_says_waiting_will_not_help(response, no_backoff):
    """시작 점검에서 온 소진도 실행 중과 같은 문장이어야 합니다 — 기다리면 풀리는
    한도가 아니라고."""
    fake = FakeJev(lambda name, args: response())
    desk = TradingDesk(LLMConfig(provider="jev", api_key="k"), memory=False)
    desk.client._client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    asyncio.run(desk.on_start(make_ctx()))
    reason = desk.status()["disabled_reason"]
    assert "Jev" in reason and "기다려도 풀리지 않습니다" in reason, reason
    assert len(fake.tool_calls()) == 1 and no_backoff == []


@pytest.mark.parametrize("text, wait", [
    ("Credit rate limit: 100 credits per minute exceeded", 1.5),
    ("Quota exceeded, retry in 12s", 12.0),
], ids=["billing-word-per-minute", "quota-short-retry-hint"])
def test_a_short_window_limit_is_waited_out_not_read_as_exhaustion(text, wait, no_backoff):
    """돈·할당량 낱말이 들어 있어도 **짧은 창** 이면 기다리면 풀립니다. 이것을
    소진으로 읽으면 실거래 데스크가 잠깐의 제한에 꺼지고, 다시 시작할 때까지
    켜지지 않습니다."""
    fake = FakeJev(fail_once(tool_error(text)))
    client = jev_client(fake)
    assert ask_technical(client)["stance"] == "bullish"      # QuotaExhausted 가 아니다
    assert len(fake.tool_calls()) == 2
    assert no_backoff == [wait]


# ─────────────────────────────────────────────────────────────────────────────
# 3차 점검 — 각 테스트는 고치기 전 코드에서 실패합니다
# ─────────────────────────────────────────────────────────────────────────────
# ── 시작 점검은 원인을 꼬리표로만 읽는다 ────────────────────────────────────
@pytest.mark.parametrize("upstream", [
    "Upstream provider returned 401: invalid x-api-key (gateway)",
    "Upstream provider returned 403: forbidden by upstream policy",
    "Upstream provider returned 404: model route not deployed",
])
def test_an_upstream_status_inside_a_transient_failure_is_not_read_as_ours(upstream,
                                                                         no_backoff):
    """게이트웨이의 일시 장애는 "jev 503" 으로 세 번 재시도됩니다(`complete()` 는
    맨 앞 꼬리표만 읽습니다). 사전 점검은 싸인 글 **안** 에서 " 401:"·" 404:" 을
    찾아, 멀쩡한 키를 "거부되었습니다" 로, 멀쩡한 주소를 "llm.base_url 을
    확인하세요" 로 적었습니다."""
    fake = FakeJev(lambda name, args: tool_error(upstream))
    desk = TradingDesk(LLMConfig(provider="jev", api_key="k"), memory=False)
    desk.client._client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    asyncio.run(desk.on_start(make_ctx()))
    reason = desk.status()["disabled_reason"]
    assert len(fake.tool_calls()) == 3                        # 일시 장애로 재시도했다
    assert reason.startswith("Jev 연결 실패"), reason
    assert "키가 거부" not in reason and "주소를 찾을 수 없습니다" not in reason
    assert upstream[:30] in reason                            # 원문은 남깁니다


def test_a_real_401_at_startup_still_says_the_key_was_rejected(no_backoff):
    """회귀 방지: 꼬리표가 401 이면 여전히 키를 가리킵니다(재시도 없음)."""
    desk = TradingDesk(LLMConfig(provider="jev", api_key="k"), memory=False)
    desk.client._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(401, json={"error": {"message": "bad token"}})))
    asyncio.run(desk.on_start(make_ctx()))
    reason = desk.status()["disabled_reason"]
    assert reason.startswith("Jev API 키가 거부되었습니다"), reason
    assert no_backoff == []


def test_failure_status_reads_the_cause_of_a_retry_wrapper_not_its_text():
    cause = LLMError("jev 503: Upstream provider returned 401: invalid key")
    try:
        raise LLMError(f"LLM call failed after 3 attempts: {cause}") from cause
    except LLMError as wrapped:
        assert llm_client.failure_status(wrapped) == 503
    assert llm_client.failure_status(LLMError("google 403: API key not valid")) == 403
    assert llm_client.failure_status(LLMError("no tag: returned 401: x")) is None


# ── 시작 점검은 답이 정해진 질문의 답까지 본다 ──────────────────────────────
@pytest.mark.parametrize("flipped", [
    {"check_boolean": boolean(0.02)},                          # probability 가 P(아니오)로
    {"check_choice": choice({"blue": 0.02, "other": 0.98})},
], ids=["boolean", "choice"])
def test_a_flipped_answer_meaning_keeps_the_desk_off(flipped, no_backoff):
    """모양만 보던 점검은 서버가 예/아니오의 방향을 바꿔도 통과했습니다. 그러면
    리스크 좌석의 "거부 없음 5%" 가 P(거부)=0.95 로 읽혀, 봉마다 보유가 청산되고
    매수가 막힙니다."""
    answers = {**PREFLIGHT_ANSWERS, **flipped}
    fake = FakeJev(lambda name, args: {"answers": answers if is_preflight(args)
                                       else ANALYST_ANSWERS})
    desk = TradingDesk(LLMConfig(provider="jev", api_key="k"), memory=False)
    desk.client._client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    assert run_desk(desk, make_ctx(invested=100)) == []
    status = desk.status()
    assert status["enabled"] is False
    assert status["disabled_reason"].startswith("Jev 의 답이 정해진 답과 반대입니다")
    assert "질문 형식을 거부" not in status["disabled_reason"]
    assert len(fake.tool_calls()) == 1 and no_backoff == []    # 다시 묻지 않았다
    assert seat_calls(fake) == []


def test_the_known_answer_check_is_loose_enough_for_honest_answers():
    """과반이면 통과합니다. 단계 점수는 모양만 봅니다 — "Partly" 도 정당한 판단입니다."""
    jev.read_preflight({"answers": {
        "check_choice": choice({"blue": 0.55, "other": 0.45}),
        "check_boolean": boolean(0.5),
        "check_score": score([0.1, 0.8, 0.1]),
    }})
    with pytest.raises(jev.PreflightAnswerFlipped, match="jev 422"):
        jev.read_preflight({"answers": {**PREFLIGHT_ANSWERS,
                                        "check_boolean": boolean(0.49)}})


# ── 붙여 넣다 섞인 글자가 있는 키 ───────────────────────────────────────────
NON_ASCII_KEYS = [
    ("zero-width-space", "jev_TESTTOKEN\u200b", "14번째", "U+200B"),
    ("smart-quotes", "\u201cjev_TESTTOKEN\u201d", "1번째", "U+201C"),
]


@pytest.mark.parametrize("label, key, where, codepoint", NON_ASCII_KEYS,
                         ids=[k[0] for k in NON_ASCII_KEYS])
def test_a_key_with_a_non_ascii_character_fails_as_a_key_error_before_sending(
        label, key, where, codepoint, no_backoff):
    """httpx 는 헤더를 ASCII 로 인코딩합니다. 예전에는 요청이 나가기도 전에
    `UnicodeEncodeError` 가 났고, 그것은 `LLMError` 가 아니라 분류를 건너뛰었습니다
    (사전 점검은 'position 20' 만, `/api/evaluate` 는 키를 말하지 않는 502)."""
    sent = []

    def handler(request):
        sent.append(request)
        return FakeJev()(request)

    client = LLMClient(LLMConfig(provider="jev", api_key=key))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(llm_client.UnsendableKey) as err:
        asyncio.run(client.complete("Reply with the single word OK.", "ping", None))
    message = str(err.value)
    assert message.startswith("jev 401: JEV_API_KEY")
    assert where in message and codepoint in message
    assert "TESTTOKEN" not in message                         # 키의 다른 글자는 적지 않는다
    assert sent == [] and no_backoff == []                    # 보내지도, 재시도하지도 않았다

    desk = TradingDesk(LLMConfig(provider="jev", api_key=key), memory=False)
    desk.client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ctx = make_ctx()
    asyncio.run(desk.on_start(ctx))
    reason = desk.status()["disabled_reason"]
    assert reason.startswith("Jev API 키에 보낼 수 없는 글자가 있습니다"), reason
    assert "거부되었습니다" not in reason and where in reason

    # 사전 점검 없이 심의하는 길(`/api/evaluate`): 예외가 새지 않고 좌석 실패가 된다.
    fresh = TradingDesk(LLMConfig(provider="jev", api_key=key), memory=False)
    fresh.client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    decision = asyncio.run(fresh.deliberate(ctx, SYM))
    assert decision is not None and decision.action == "hold"
    assert decision.analysts["technical"]["error"].startswith("jev 401: JEV_API_KEY")
    assert sent == []


@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
def test_every_provider_checks_its_header_key_the_same_way(provider, no_backoff):
    sent = []
    client = LLMClient(LLMConfig(provider=provider, api_key="sk-TEST\u200b"))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: sent.append(request) or httpx.Response(500)))
    with pytest.raises(llm_client.UnsendableKey, match=f"^{provider} 401: "):
        asyncio.run(client.complete("s", "u", None))
    assert sent == []
