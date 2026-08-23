"""데스크 비용 계산기 — 실측 토큰 기준.

한 번의 심의가 얼마인지, 한 번 사고파는 데 얼마인지 계산합니다.

토큰 수는 추정이 아니라 **실측값**입니다. `ScriptedLLM` 로 실제 심의를 한 바퀴
돌려 16석의 프롬프트를 그대로 캡처한 뒤 Gemini countTokens 로 셌습니다. 출력
토큰만 관측 1건(분석가 280)을 기준으로 단계별 추정치를 씁니다 — 구조화 출력이라
분산이 작습니다.

    python scripts/desk_cost.py                    # 기본 비교
    python scripts/desk_cost.py --hold-bars 20     # 20봉 보유 기준
    python scripts/desk_cost.py --symbols 3        # 매 봉 3종목 심의

**중요한 오해 하나.** "한 번 사고파는 비용"은 심의 2회가 아닙니다. 데스크는 보유
기간 내내 매 사이클 그 종목을 다시 검토합니다(보유 종목은 항상 shortlist 에
포함됩니다). 그러니 실제 왕복 비용은 **보유한 봉 수 × 심의 단가**입니다. 그게 이
계산기가 기본으로 보여주는 숫자입니다.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

BAR = "=" * 74

#: 실측: 16석 1라운드 심의의 단계별 입력 토큰 (프롬프트 캡처 + countTokens)
STAGE_INPUT = {
    "analyst": (8, 8_383),
    "debate": (2, 4_449),
    "risk_debate": (2, 4_343),
    "risk_verdict": (1, 2_307),
    "plan": (1, 2_259),
    "trade": (1, 783),
    "head": (1, 2_652),
}

#: 출력 토큰. analyst 는 실측(280), 나머지는 같은 스키마 크기 기준 추정.
STAGE_OUTPUT = {
    "analyst": 280, "debate": 400, "risk_debate": 350, "risk_verdict": 300,
    "plan": 350, "trade": 250, "head": 450,
}

#: 판정 좌석 — 여기만 더 큰 모델을 쓰는 구성이 일반적입니다.
DECISION_STAGES = {"risk_verdict", "plan", "trade", "head"}


@dataclass(frozen=True)
class Model:
    name: str
    input_per_m: float
    output_per_m: float
    note: str = ""


#: 2026-08 기준. 가격은 바뀌므로 실제 청구 전 각 제공사 페이지로 확인하세요.
MODELS = [
    Model("claude-opus-5", 5.00, 25.00),
    Model("claude-sonnet-5", 3.00, 15.00, "도입가 $2/$10 (2026-08-31까지)"),
    Model("claude-haiku-4-5", 1.00, 5.00),
    Model("gemini-3.1-pro-preview", 2.00, 12.00, "200k 초과 시 $4/$18"),
    Model("gemini-3.5-flash", 1.50, 9.00),
    Model("gemini-3.7-flash", 0.75, 3.75, "2026-12-31까지, 이후 $1.50/$7.50"),
    Model("gemini-3.5-flash-lite", 0.30, 2.50),
]
BY_NAME = {m.name: m for m in MODELS}


def tokens(debate_rounds: int, risk_rounds: int) -> tuple[int, int, int, int, int]:
    """(호출수, 전체입력, 전체출력, 판정단계입력, 판정단계출력)."""
    calls = total_in = total_out = dec_in = dec_out = 0
    for stage, (base_calls, base_in) in STAGE_INPUT.items():
        multiplier = (debate_rounds if stage == "debate"
                      else risk_rounds if stage == "risk_debate" else 1)
        n = base_calls * multiplier
        stage_in = base_in * multiplier
        stage_out = STAGE_OUTPUT[stage] * n
        calls += n
        total_in += stage_in
        total_out += stage_out
        if stage in DECISION_STAGES:
            dec_in += stage_in
            dec_out += stage_out
    return calls, total_in, total_out, dec_in, dec_out


def cost(model: Model, tin: int, tout: int) -> float:
    return tin / 1e6 * model.input_per_m + tout / 1e6 * model.output_per_m


def main() -> int:
    p = argparse.ArgumentParser(description="데스크 비용 계산기")
    p.add_argument("--debate-rounds", type=int, default=2)
    p.add_argument("--risk-rounds", type=int, default=1)
    p.add_argument("--hold-bars", type=int, default=12,
                   help="한 포지션을 몇 봉 들고 있는가 (왕복 비용 = 이 값 × 심의 단가)")
    p.add_argument("--symbols", type=int, default=1,
                   help="매 사이클 심의하는 종목 수")
    p.add_argument("--bars-per-day", type=float, default=1.0,
                   help="하루 봉 수 (일봉 1, 4시간봉 6, 1시간봉 6.5, 5분봉 78)")
    args = p.parse_args()

    calls, tin, tout, dec_in, dec_out = tokens(args.debate_rounds, args.risk_rounds)
    cheap_in, cheap_out = tin - dec_in, tout - dec_out

    print(f"\n{BAR}\n  데스크 심의 1회 — 실측 토큰"
          f"\n{BAR}")
    print(f"  토론 {args.debate_rounds}라운드 · 리스크 {args.risk_rounds}라운드")
    print(f"  LLM 호출 {calls}회 · 입력 {tin:,} 토큰 · 출력 약 {tout:,} 토큰")
    print(f"    이 중 판정 4석: 입력 {dec_in:,} · 출력 {dec_out:,}")

    print(f"\n{BAR}\n  ① 전 좌석 같은 모델\n{BAR}")
    print(f"  {'모델':<26}{'심의 1회':>10}{'왕복':>10}{'하루':>10}   비고")
    for m in MODELS:
        one = cost(m, tin, tout)
        trip = one * args.hold_bars * args.symbols
        daily = one * args.bars_per_day * args.symbols
        print(f"  {m.name:<26}{'$' + format(one, '.3f'):>10}"
              f"{'$' + format(trip, '.2f'):>10}{'$' + format(daily, '.2f'):>10}"
              f"   {m.note}")

    print(f"\n{BAR}\n  ② 혼합 — 분석·토론은 싼 모델, 판정 4석만 큰 모델\n{BAR}")
    print(f"  {'분석 / 판정':<44}{'심의 1회':>10}{'왕복':>10}{'하루':>10}")
    pairs = [
        ("gemini-3.7-flash", "gemini-3.1-pro-preview"),
        ("gemini-3.5-flash-lite", "gemini-3.1-pro-preview"),
        ("claude-haiku-4-5", "claude-opus-5"),
        ("claude-haiku-4-5", "claude-sonnet-5"),
        ("gemini-3.7-flash", "claude-opus-5"),
    ]
    for cheap, strong in pairs:
        c, s = BY_NAME[cheap], BY_NAME[strong]
        one = cost(c, cheap_in, cheap_out) + cost(s, dec_in, dec_out)
        trip = one * args.hold_bars * args.symbols
        daily = one * args.bars_per_day * args.symbols
        label = f"{cheap} / {strong}"
        print(f"  {label:<44}{'$' + format(one, '.3f'):>10}"
              f"{'$' + format(trip, '.2f'):>10}{'$' + format(daily, '.2f'):>10}")

    print(f"\n{BAR}")
    print(f"  '왕복' = 한 포지션을 {args.hold_bars}봉 보유 × {args.symbols}종목.")
    print(f"  심의 2회가 아닌 이유: 보유 종목은 매 사이클 다시 검토됩니다")
    print(f"  (shortlist 에 보유분이 항상 포함되므로).")
    print(f"  '하루' = 봉 {args.bars_per_day}개/일 × {args.symbols}종목 기준.")
    print(f"\n  줄이는 방법:")
    print(f"    · cadence_bars 를 올린다 (매 봉 대신 N봉마다 심의)")
    print(f"    · seats 로 분석 좌석을 줄인다 (8석 → 3석이면 입력 {STAGE_INPUT['analyst'][1]:,}"
          f" 토큰 중 약 {STAGE_INPUT['analyst'][1]*5//8:,} 절약)")
    print(f"    · debate_rounds 를 1로 낮춘다")
    print(f"    · max_symbols_per_run 을 줄인다")
    print(f"\n  가격은 2026-08 기준이며 바뀝니다. 실제 청구 전 제공사 페이지로 확인하세요.")
    print(BAR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
