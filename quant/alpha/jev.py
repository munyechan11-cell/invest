"""Jev 좌석 어댑터 — 16석 데스크의 판단을 Jev 에게 묻는다.

Jev 는 판단 모델입니다. 상태를 읽고 **확률이 붙은 유형 답** — 예/아니오,
N개 중 하나, 등급 점수 — 만 돌려줍니다. 글을 쓰지 못하고 계산도 못 합니다.
그래서 좌석 하나를 Jev 호출(`jev_evaluate`) 하나로 바꾸되, 일을 셋으로 나눕니다:

  · Jev       좁은 질문 몇 개에 확률로 답한다 (방향, 크기, 거부 여부 …)
  · 이 모듈    그 확률로 숫자를 계산한다 (확신도, 배율, 보유기간, 기대 변동)
  · 템플릿     자유 서술 칸을 **Jev 의 확률을 그대로 적은** 한국어 문장으로 채운다

템플릿이 빈칸 채우기가 아닌 이유: 데스크의 뒷좌석은 앞좌석의 출력을 읽고
판단합니다(토론은 분석가 리포트를, 헤드는 전부를). 칸이 비면 그 연결이 끊기고,
화면의 말풍선도 비어 "좌석이 응답하지 않았다" 와 구별되지 않습니다. 반대로
Jev 가 하지 않은 말 — 시장 근거, 무효화 사유 — 을 지어 넣으면 그것은 판단이
아니라 창작입니다. 그래서 템플릿은 확률과 그 확률로 계산한 값만 말합니다.

데스크 구조는 그대로입니다: 16석, 같은 단계 순서, 뒷좌석이 앞좌석을 읽는다.
데스크가 아는 것은 `await client.complete(system, user, schema)` 뿐이므로, 좌석은
`system` 프롬프트가 `seats.py` 의 어느 좌석과 **정확히** 같은지로 알아봅니다.
프롬프트가 한 글자라도 다르면 좌석은 못 알아보지만, 스키마가 데스크의 것이면
**단계** 는 스키마로 알아봅니다 — 그래서 헤드의 판단 보류 규칙과 리스크 거부
기준은 좌석을 못 알아봐도 빠지지 않습니다.

모두 순수 함수입니다 — 네트워크는 `LLMClient._jev` 가 맡고, 여기는 질문을
만들고 답을 스키마로 옮기는 일만 합니다. 그래서 오프라인으로 전부 시험할 수
있습니다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from quant.alpha.llm_client import LLMError
from quant.alpha.seats import (
    ALL_SEATS,
    ANALYST_SCHEMA,
    DEBATE_SCHEMA,
    HEAD_SCHEMA,
    RESEARCH_PLAN_SCHEMA,
    RISK_DEBATE_SCHEMA,
    RISK_VERDICT_SCHEMA,
    SEATS_BY_KEY,
    TRADER_SCHEMA,
    VETO_BLOCK_BARS,
    Seat,
)

#: 이 아래로는 "결정하지 못함" 으로 읽는 확률. `LLMConfig.extra["undecided_below"]`.
#:
#: Jev 의 운영 지침이 0.35~0.65 를 미결로 읽으라고 합니다. 그 구간의 답을
#: 그대로 주문으로 옮기면 동전 던지기에 돈을 거는 셈이라, 방향은 관망으로,
#: 거부는 "거부하지 않되 사이즈 절반 이하" 로 바꿉니다. 0 으로 두면 이 규칙이
#: 꺼집니다(가장 큰 쪽을 그대로 따르고, 거부는 P ≥ 0.5).
#:
#: **올리면 거부가 약해집니다.** 같은 값이 거부 문턱(P(거부) ≥ 이 값)이라, "덜
#: 사고팔게" 0.8 로 올리면 70% 거부는 청산 대신 배율 절반이 됩니다.
#: `MAX_UNDECIDED_BELOW` 이상은 시작할 때 거절합니다.
DEFAULT_UNDECIDED_BELOW = 0.65


# ─────────────────────────────────────────────────────────────────────────────
# 좌석별 영어 렌즈
# ─────────────────────────────────────────────────────────────────────────────
#: `seats.py` 의 한국어 시스템 프롬프트를 영어로 옮긴 핵심 규칙.
#:
#: 번역하는 이유는 Jev 가 영어에서 가장 정확하기 때문입니다(한국어도 받지만
#: 정확도가 떨어진다고 스스로 밝힙니다). 프롬프트를 고치면 여기도 같이 고쳐야
#: 합니다 — `tests/test_jev.py` 가 16석 전부에 렌즈가 있는지 확인합니다.
LENSES: dict[str, str] = {
    "technical": (
        "Senior technical analyst. First decide the regime from ADX: 25 or above is a "
        "trend, 20 or below is a range, in between is a transition. The same indicator "
        "means opposite things by regime: RSI 70 confirms strength in a trend but warns "
        "of a pullback in a range. Evidence: alignment and slope of the 20/50/200 moving "
        "averages and price versus the 200-day line; MACD histogram sign and slope; "
        "consistency of 5/20/60-bar returns; ATR ratio, Bollinger %B and squeezes; "
        "distance from the 52-week high and low. Correlated indicators pointing the same "
        "way are one piece of evidence, not three. Never claim chart patterns the numbers "
        "do not show. Confidence should track how clear the regime is. Needs the price "
        "and technical-indicator sections."
    ),
    "flow": (
        "Investor-flow analyst for the Korean market: reads net buying by foreigners, "
        "institutions, retail and program trading. Persistence is the signal: foreigners "
        "and institutions split orders over days to weeks, so a single day is noise and "
        "a streak shorter than 3 sessions says nothing. Judge strength only relative to "
        "participation (net volume as a share of traded volume) and the stock's own "
        "history (z-score), never raw share counts. Price/flow divergence is the "
        "strongest signal: price falling while foreigners and institutions buy is "
        "accumulation (bullish), price rising while they sell is distribution (bearish); "
        "price and flow moving together is confirmation, not new information. Retail "
        "net buying at an extreme (z of +2 or more) is roughly a contrarian sign, but it "
        "mirrors foreign and institutional flow, so do not count it twice. Program "
        "trading (index rebalancing, arbitrage, baskets) is not a view on the stock; "
        "discount the signal when program flow explains most of the foreign flow. "
        "Foreigners and institutions on opposite sides lowers conviction. If the flow "
        "section is empty, missing or has too few sessions, the data is insufficient; "
        "never infer flow that is not shown."
    ),
    "fundamental": (
        "Fundamental analyst: valuation, earnings trend, margins, balance-sheet health, "
        "business momentum. The most important rule: if the evidence contains no "
        "financial-statement data (earnings, margins, cash flow, debt, valuation "
        "multiples), the data is insufficient and the stance is neutral. Never use "
        "remembered earnings or any company fact from outside the evidence; that is "
        "look-ahead leakage, not analysis. With data, judge earnings direction and "
        "quality first, then margins, cash flow versus earnings, leverage and interest "
        "cover, and only then valuation multiples."
    ),
    "news": (
        "News and disclosure analyst. Three questions only: is this a real repricing "
        "event or just a headline, how long will it last, and is it already priced in "
        "(volume at 2x or more of average after a large move suggests it largely is). "
        "Information after the as-of time does not exist. If no external news or "
        "disclosure context is included in the evidence, the data is insufficient; "
        "never invent news from memory."
    ),
    "sentiment": (
        "Positioning and sentiment analyst: where is the crowd, and is that a "
        "confirming or a contrarian signal? Sentiment confirms early in a trend and is "
        "contrarian at extremes. Attention volume alone is not a signal. Judge extremes "
        "only against the stock's own history. Without social data, retail investor "
        "flow may serve as a weaker proxy (with lower conviction); with no basis at all "
        "the data is insufficient."
    ),
    "macro": (
        "Macro strategist: how rates, liquidity, FX, the index and the sector backdrop "
        "act on this stock over the holding period. For Korean stocks the KRW/USD rate "
        "is the biggest driver of foreign flow; a weakening won makes sustained foreign "
        "buying unlikely. Separate observation from inference. Macro is the most "
        "inference-heavy seat, so its conviction should usually be lower than other "
        "seats', and it explains little over holding periods of only a few bars."
    ),
    "microstructure": (
        "Market-microstructure analyst. This seat does NOT call direction. It judges "
        "whether the idea can actually be executed at target size and whether costs eat "
        "the expected edge: spread versus expected return (a round trip costs at least "
        "twice the spread), target size versus average volume (above about 10% market "
        "impact grows non-linearly), and whether lot and tick sizes allow the intended "
        "order (for Korean stocks, orders off the price-band tick ladder are rejected). "
        "Execution problems make the stance neutral, not bearish: they are conditions "
        "on size and method, not a view on direction."
    ),
    "quant": (
        "Quantitative researcher and the desk's statistical skeptic: is the claimed "
        "effect distinguishable from noise? Fewer than about 20 observations is usually "
        "chance; an effect smaller than about 0.3x the daily volatility does not survive "
        "costs; scanning many indicators and keeping the one that fits is multiple "
        "testing; high correlation with existing holdings is concentration, not "
        "diversification. The stance is usually neutral, and bullish or bearish only "
        "when the numbers statistically support it."
    ),
    "bull": (
        "Bull researcher: makes the strongest honest case for buying, using only numbers "
        "the analysts reported; inventing new facts makes the debate worthless. The "
        "strength of the case must reflect the evidence, not the role: a bull that is "
        "always confident is a constant and carries no information."
    ),
    "bear": (
        "Bear researcher: makes the strongest honest case against buying (for selling "
        "or staying out), using only numbers the analysts reported. 'It could be risky' "
        "is not an argument; a real case names what loses how much under which "
        "condition. When the evidence is bullish, the honest bear case is weak."
    ),
    "risk_aggressive": (
        "Aggressive risk seat: argues for adequate size when the setup qualifies; the "
        "cost it represents is profit lost by entering too small. Size must be "
        "justified concretely: a clear and tight stop, sufficient liquidity, low "
        "correlation with existing holdings, a good ratio of expected return to the "
        "loss limit. 'The opportunity is big' alone never justifies size."
    ),
    "risk_conservative": (
        "Conservative risk seat: represents capital preservation, but reflexive "
        "rejection is forbidden; cutting size from vague unease makes this seat a "
        "constant the desk ignores. Only concrete, nameable hazards count: an imminent "
        "earnings or disclosure event, thin liquidity, correlation with existing "
        "holdings, a stop too wide relative to ATR, or a case resting on inference "
        "rather than observation. The larger the share of the case that is inference, "
        "the smaller the size."
    ),
    "risk_neutral": (
        "Neutral risk seat: reconciles the aggressive and conservative arguments into "
        "one position-size multiplier between 0 and 1. Never take a mechanical "
        "midpoint; weight toward whichever side gave more concrete, nameable evidence. "
        "On this desk a veto overrides the head of desk: it closes the whole position "
        "if one is held and blocks every model from buying this stock for the next "
        f"{VETO_BLOCK_BARS} bars. This seat answers before the direction is decided, so "
        "a veto is not a veto of one new order. "
        "Veto only if the loss limit cannot be determined, liquidity cannot absorb the "
        "target size, a portfolio concentration limit is breached, or the whole case is "
        "inference with no observation. Any other discomfort, including a concern that "
        "only argues for adding less, is expressed by a smaller size; veto and size "
        "reduction are different tools."
    ),
    "research_manager": (
        "Research manager: turns the eight analyst reports and the bull/bear debate "
        "into one directional plan, and says which side carried and why. Hold only when "
        "the evidence is genuinely balanced; holding everything is dereliction, not risk "
        "management. In Korea investor flow often knows more than the desk, so a flow "
        "seat opposing the direction matters."
    ),
    "trader": (
        "Trader: translates the research plan into an executable order. Direction was "
        "decided in the research plan and must not be re-argued. Judge only from what "
        "the evidence shows: recent returns, distance from the 52-week high, volume, "
        "the spread, the estimated round-trip cost and the position's share of daily "
        "volume. Entry style: market now when costs are small and waiting gains little; "
        "a patient limit order when the spread or round-trip cost is material; scale in "
        "if the size is large relative to liquidity; wait for a pullback if the price "
        "has just run up sharply near its 52-week high. Use more tranches (1 to 4) for "
        "larger recent moves or a larger share of daily volume. Heed the execution-cost "
        "section of the evidence."
    ),
    "head": (
        "Head of desk: combines all seats into the final decision, which becomes a real "
        "order. Judged on calibration, not enthusiasm: 0.9 means right 9 times in 10. "
        "Hold only when the evidence is balanced. This desk cannot cut a position "
        "partially: both reduce and sell close the whole position; reduce is for a case "
        "that has weakened but not reversed, sell for evidence that points to a fall. "
        "Hold places no order and leaves the stock to the desk's other models, which "
        "may still open or close a position; on a stock that is not held, reduce, sell "
        "and strong sell block every model from buying it for about half the holding "
        "horizon. Lower confidence when investor flow "
        "(foreigners and institutions) opposes the direction. If outcomes of past "
        "decisions are given, learn from them; repeating the same mistake is the most "
        "common failure in this seat."
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# 용어집 — 브리프와 데스크 프롬프트의 한국어 → 영어
# ─────────────────────────────────────────────────────────────────────────────
#: 증거(`user`)는 **원문 그대로** 보냅니다. 번역하면 숫자와 키가 어긋날 위험이
#: 있고, 그 어긋남은 아무도 못 봅니다. 대신 용어집을 옆에 붙여 Jev 가 한국어
#: 키를 영어로 읽게 합니다. 한 번 호출할 때 **증거에 실제로 나온 용어만**
#: 보냅니다 — Jev 는 입력 토큰으로 과금되고, 안 나온 용어는 판단에 쓸모가
#: 없습니다.
GLOSSARY: dict[str, str] = {
    # 브리프 머리
    "종목": "ticker",
    "거래소": "exchange / venue",
    "통화": "quote currency",
    "기준시각": "as-of time (anything after it does not exist)",
    "봉주기": "bar timeframe",
    # 가격
    "가격": "price section",
    "종가": "close",
    "고가": "high",
    "저가": "low",
    "5봉수익률%": "5-bar return %",
    "20봉수익률%": "20-bar return %",
    "60봉수익률%": "60-bar return %",
    "52주고점대비%": "% from the 52-week high (negative = below the high)",
    "52주저점대비%": "% above the 52-week low",
    # 기술지표
    "기술지표": "technical indicators section",
    "200일선위": "close is above the 200-day SMA (true/false)",
    "MACD히스토그램": "MACD histogram",
    "ADX14": "ADX(14), trend strength",
    "국면": "regime, derived from ADX",
    "추세": "trending regime (ADX >= 25)",
    "박스권": "range-bound regime (ADX <= 20)",
    "전환": "transitional regime (ADX between 20 and 25)",
    "볼린저%B": "Bollinger %B",
    "ATR비율%": "ATR as % of price",
    "연환산변동성%": "annualised volatility %",
    # 유동성
    "유동성": "liquidity section",
    "당일거래량": "this bar's volume",
    "20일평균거래량": "20-day average volume",
    "거래량배수": "volume as a multiple of the 20-day average",
    "호가스프레드%": "bid-ask spread %",
    # 체결비용
    "체결비용": "execution cost section",
    "최소주문단위": "lot size",
    "호가단위": "tick size",
    "최소주문금액": "minimum order value",
    "왕복비용추정%": "estimated round-trip cost %",
    "1%포지션의거래량비중%": "a 1%-of-equity position as % of average daily volume",
    # 포트폴리오
    "포트폴리오": "portfolio section",
    "전략 장부 평가액": "strategy book equity (not the whole brokerage account)",
    "보유종목수": "number of open positions",
    "현금비중%": "cash as % of equity",
    "현재낙폭%": "current drawdown %",
    "이종목보유": "current holding in this stock (null = none)",
    "수량": "quantity",
    "평단": "average entry price",
    "평가손익%": "unrealised P&L %",
    "거래잠금": "trading lock reason (null = not locked)",
    # 통계
    "통계": "statistics section",
    "관측봉수": "number of bars observed",
    "일별변동성%": "daily volatility % (ATR based)",
    "최근20봉승률%": "% of up bars among the last 20",
    "포트폴리오누적수익%": "portfolio cumulative return %",
    "누적매매수": "number of closed trades",
    # 수급
    "수급": "investor flow section (net buying by foreigners, institutions, "
            "retail, program trading)",
    "요약20일": "20-session flow summary",
    "최근5일": "the last 5 sessions",
    "데이터없음": "no flow data (the value is the reason)",
    # 뒷단계 프롬프트의 절 이름
    "브리프": "brief (deterministic market facts)",
    "분석가 리포트": "analyst reports, keyed by seat",
    "분석가": "analyst seats",
    "강세/약세 토론": "bull/bear debate",
    "지금까지의 토론": "debate so far",
    "리스크 토론": "risk debate (aggressive versus conservative)",
    "리스크 판정": "risk verdict (neutral risk seat)",
    "리서치 계획": "research plan",
    "트레이더 실행안": "trader's execution plan",
    "과거 판단 결과": "outcomes of past desk decisions on this stock",
    "라운드": "round",
    "강세론자": "bull researcher",
    "약세론자": "bear researcher",
    "공격형": "aggressive risk seat",
    "보수형": "conservative risk seat",
    "중립형": "neutral risk seat",
    "좌석 응답 실패": "the seat failed to answer (carries no information)",
    "응답 실패": "the seat failed to answer (carries no information)",
    # 좌석이 실패했을 때 데스크가 대신 적는 말(`desk.py` 의 대체값). "축소" 만
    # 있으면 헤드 행동의 뜻(전량 청산)으로 풀립니다 — 리스크 좌석 대체값의
    # "사이즈 축소" 는 배율을 낮췄다는 말이지 청산이 아닙니다.
    "사이즈 축소": "a smaller position size (not a close of the position)",
    "다음 사이클 재평가": "re-evaluate at the next deliberation",
    "공매도": "short selling",
    "허용": "allowed",
    "불가": "not allowed",
    "기본 보유기간": "default holding period",
    # 과거 판단 결과(교훈)
    "적중": "the call was right",
    "실패": "failed (in past-decision outcomes: the call was wrong)",
    "확신": "conviction",
    "벤치": "benchmark return",
    "초과": "excess return over the benchmark",
    "근거": "rationale",
    "캘리브레이션": "calibration record",
    # 이 모듈의 템플릿 — 뒷좌석이 앞좌석의 Jev 출력을 읽으므로
    "강세": "bullish",
    "중립": "neutral",
    "약세": "bearish",
    "판단 재료 부족": "insufficient data for this seat",
    "판단 재료 충분 확률": "probability the seat's data is sufficient",
    "판단 보류": "undecided (no side reached the threshold)",
    "매수 측": "buy side",
    "매도 측": "sell side",
    "축소·청산 측": "reduce / exit side",
    "관망": "hold / wait",
    "배율": "position-size multiplier",
    "거부 확률": "probability of a risk veto",
    "기대값": "expected value",
    "Jev 확률 판정": "Jev probability judgment",
    # 토론 좌석의 출력 — 논거 강도 (`CASE_LEVELS_KO`)
    "논거": "case / argument",
    "강도": "strength",
    "단계": "level on the graded scale",
    "트레이더": "trader seat",
    "매수 반대": "against buying",
    "논거 없음": "no credible case",
    "약함": "weak",
    "보통": "moderate",
    "강함": "strong",
    "매우 강함": "very strong",
    # 리스크 토론 좌석의 출력 — 제안 배율과 위험 (`SIZE_LEVELS_KO`, `HAZARD_KO`)
    "제안 배율": "proposed position-size multiplier",
    "가장 유력": "most likely level",
    "가장 뚜렷한 위험": "most evident hazard",
    "진입 안 함(0%)": "no position (0% of the intended size)",
    "1/4 사이즈": "quarter of the intended size",
    "1/2 사이즈": "half of the intended size",
    "3/4 사이즈": "three quarters of the intended size",
    "풀 사이즈": "the full intended size",
    "실적·공시 이벤트 임박": "an earnings or disclosure event is imminent",
    "유동성 부족": "liquidity is too thin for the intended size",
    "기존 보유와 높은 상관": "highly correlated with existing holdings",
    "손절폭이 ATR 대비 과도": "the stop is too wide relative to ATR",
    "관측보다 추론 의존": "relies on inference rather than observation",
    "없음": "none",
    # 리스크 판정의 출력 (`VETO_REASON_KO`)
    "거부": "risk veto",
    "기준": "threshold",
    "제한": "capped",
    "사유 확률": "probability of that veto reason",
    "손실 한도를 계산할 수 없음": "the loss limit cannot be determined",
    "유동성이 목표 사이즈를 감당하지 못함": "liquidity cannot absorb the target size",
    "포트폴리오 집중도 한도를 넘음": "a portfolio concentration limit would be breached",
    "근거 전체가 관측 없는 추론": "the whole case is inference with no observation",
    "해당 사유 없음": "none of the veto conditions applies",
    # 미시구조 좌석의 출력 — 체결 가능성 (`EXECUTION_KO`)
    "체결 가능성": "executability of the intended order (microstructure seat)",
    "목표 사이즈로 체결 가능": "executable at the target size; costs are small next to a "
                        "typical move",
    "조건부 체결 가능": "executable only with a smaller size, limit orders or split orders",
    "체결 곤란": "not executable as intended; costs or market impact would eat the edge, "
             "or order-size constraints prevent it",
    # 계획·트레이더·헤드의 출력 (`WINNER_KO`, `ENTRY_KO`, `ACTION_KO`)
    "토론": "debate",
    "강세 측 우세": "the bull side carried the debate",
    "약세 측 우세": "the bear side carried the debate",
    "팽팽함": "the debate was balanced",
    "방향": "direction",
    "신규 주문": "new order",
    "신규 진입": "new entry",
    "정리": "close the position",
    "진입 방식": "entry style",
    "분할": "split into several orders",
    "즉시 시장가": "market order now",
    "지정가 대기": "patient limit order",
    "분할 진입": "scale in",
    "눌림 대기": "wait for a pullback",
    "적극 매수": "strong buy",
    "매수": "buy",
    "축소": "reduce (closes the whole position)",
    "매도": "sell",
    "적극 매도": "strong sell",
    "기대 변동": "expected price move",
    "보유": "holding / held",
    "봉": "bar",
    "재심의": "the next deliberation on this stock",
    "무효": "invalidated",
    "반대": "dissents",
}

#: 수급 요약·일별 기록의 영어 키. 이름만으로는 부호·기준이 안 보이는 것들입니다.
FLOW_TERMS: dict[str, str] = {
    "foreign_streak": "consecutive sessions of foreign net buying (+) or selling (-)",
    "institution_streak": "consecutive sessions of institutional net buying (+) or "
                          "selling (-)",
    "participation_zscore": "latest smart-money participation versus this stock's own "
                            "history (z-score)",
    "avg_participation_pct": "average net foreign+institution volume as % of volume",
    "turnover_ratio_pct": "net foreign+institution volume as % of total volume",
    "smart_money_net_value": "foreign + institution net value (null = source gives "
                             "quantities only)",
    "accumulation_days": "sessions where foreigners and institutions bought while "
                         "retail sold",
    "distribution_days": "sessions where foreigners and institutions sold while "
                         "retail bought",
    "divergence": "flow versus price: bullish_divergence = smart money buying a "
                  "falling price; bearish_divergence = selling a rising price; "
                  "confirmed_* = flow and price agree",
    "program_qty": "program-trading net quantity (index/arbitrage baskets, not a view "
                   "on the stock; null = the source does not report program trading)",
    "participation_pct": "net foreign+institution volume as % of that session's volume",
    "pattern": "accumulation / distribution / mixed for that session",
}


def glossary_for(evidence: str) -> dict[str, str]:
    """증거에 실제로 나온 용어만."""
    terms = {**GLOSSARY, **FLOW_TERMS}
    return {k: v for k, v in terms.items() if k in evidence}


# ─────────────────────────────────────────────────────────────────────────────
# 선택지와 한국어 이름
# ─────────────────────────────────────────────────────────────────────────────
STANCES = ("bullish", "neutral", "bearish")
STANCE_KO = {"bullish": "강세", "neutral": "중립", "bearish": "약세"}

CASE_LEVELS_KO = ("논거 없음", "약함", "보통", "강함", "매우 강함")

SIZE_LEVELS_KO = ("진입 안 함(0%)", "1/4 사이즈", "1/2 사이즈", "3/4 사이즈", "풀 사이즈")
SIZE_LEVELS = (
    "No position (0% of the intended size): the setup does not qualify, or a concrete "
    "hazard makes any size unjustified",
    "Quarter size (25%): the idea may qualify, but the loss limit, liquidity or the "
    "quality of the evidence is doubtful",
    "Half size (50%): a reasonable setup with at least one concrete, nameable hazard",
    "Three-quarter size (75%): a clear setup with a well-defined stop and adequate "
    "liquidity; the remaining hazards are minor",
    "Full size (100%): a clear stop that is tight relative to volatility, ample "
    "liquidity, low correlation with existing holdings, and a case resting on "
    "observations rather than inference",
)

HAZARDS = {
    "event_imminent": "A scheduled event such as earnings or a disclosure is imminent",
    "thin_liquidity": "Liquidity is too thin for the intended size (low volume or a "
                      "wide spread)",
    "correlated_with_holdings": "The position would be highly correlated with existing "
                                "holdings (the same bet twice)",
    "stop_too_wide_vs_atr": "The stop the setup needs is too wide relative to ATR / "
                            "volatility",
    "inference_heavy": "The case relies mainly on inference rather than observed data",
    "none": "No concrete, nameable hazard is evident in the evidence",
}
HAZARD_KO = {
    "event_imminent": "실적·공시 이벤트 임박",
    "thin_liquidity": "유동성 부족",
    "correlated_with_holdings": "기존 보유와 높은 상관",
    "stop_too_wide_vs_atr": "손절폭이 ATR 대비 과도",
    "inference_heavy": "관측보다 추론 의존",
    "none": "없음",
}

#: 미시구조 좌석의 고유 질문. 이 좌석은 방향을 부르지 않습니다(렌즈·프롬프트) —
#: 그런데 분석가 공통 질문(방향·재료)만 받아, 리포트가 "중립 80%" 뿐이었고
#: 뒷좌석은 체결 가능성에 대한 판단을 한 번도 읽지 못했습니다.
EXECUTION = {
    "executable": "Executable at the target size: the round-trip cost and the "
                  "position's share of average daily volume are small next to a "
                  "typical recent move, and lot and tick sizes allow the order",
    "conditional": "Executable only with conditions (a smaller size, limit orders or "
                   "splitting the order), because the spread, the round-trip cost or "
                   "the position's share of daily volume is material",
    "not_executable": "Not executable as intended: costs or market impact would "
                      "consume the expected edge, or lot, tick or minimum-order "
                      "constraints prevent the intended order",
}
EXECUTION_KO = {"executable": "목표 사이즈로 체결 가능", "conditional": "조건부 체결 가능",
                "not_executable": "체결 곤란"}

VETO_REASONS = {
    "loss_limit_unknown": "The loss limit for this position cannot be determined from "
                          "the evidence",
    "illiquid": "Liquidity cannot absorb the target size",
    "concentration_breach": "The position would breach a portfolio concentration limit",
    "inference_only": "The whole case is inference with no supporting observation",
    "none_applies": "None of these four conditions applies",
}
VETO_REASON_KO = {
    "loss_limit_unknown": "손실 한도를 계산할 수 없음",
    "illiquid": "유동성이 목표 사이즈를 감당하지 못함",
    # "초과" 는 쓰지 않습니다 — 용어집이 그 말을 과거 판단 결과의 "벤치 대비 초과
    # 수익" 으로 풀어 줘서, 뒷좌석이 이 거부 사유를 수익 얘기로 읽었습니다.
    "concentration_breach": "포트폴리오 집중도 한도를 넘음",
    "inference_only": "근거 전체가 관측 없는 추론",
    "none_applies": "해당 사유 없음",
}

RATINGS = {
    "strong_buy": "Strong buy: the evidence clearly and independently supports buying, "
                  "with little credible counter-evidence",
    "buy": "Buy: the evidence on balance supports buying, with some counter-evidence",
    "hold": "Hold: the evidence is genuinely balanced; neither buying nor selling is "
            "better supported",
    "sell": "Sell: the evidence on balance supports selling or staying out, with some "
            "counter-evidence",
    "strong_sell": "Strong sell: the evidence clearly supports selling or exiting, with "
                   "little credible counter-evidence",
}
WINNERS = {
    "bull": "The bull side (buy) carried: its evidence was more concrete and better "
            "supported by observed numbers",
    "bear": "The bear side (sell or stay out) carried: its evidence was more concrete "
            "and better supported by observed numbers",
    "balanced": "Neither side carried: the evidence is genuinely balanced",
}
WINNER_KO = {"bull": "강세 측 우세", "bear": "약세 측 우세", "balanced": "팽팽함"}

TRADE_ACTIONS = {
    "buy": "Buy: the research plan's direction is to buy or add",
    "hold": "Hold: the plan gives no direction to act on, so place no order",
    "sell": "Sell: the plan's direction is to sell or exit",
}
#: 트레이더의 증거는 가격·유동성·체결비용·포트폴리오 절과 계획·리스크 판정뿐입니다
#: (`TRADER_SEAT.brief_sections`) — 변동성·RSI·볼린저가 없고, Jev 계획에는
#: 신호의 수명도 적혀 있지 않습니다. 예전 선택지는 "변동성", "과열", "신호가 빨리
#: 사라지면" 을 물어서 Jev 는 없는 것을 보고 답해야 했습니다. 증거에 **있는**
#: 대용치(최근 수익률, 52주 고점 대비, 스프레드, 왕복 비용, 거래량 비중)로 묻습니다.
ENTRY_STYLES = {
    "market_now": "Market order now: the spread and the estimated round-trip cost are "
                  "small, so waiting for a better price gains little; fill immediately",
    "limit_patient": "Patient limit order: the spread or the estimated round-trip cost "
                     "is material, so post inside the spread and wait",
    "scale_in": "Scale in: the size is large relative to liquidity (the position is a "
                "noticeable share of average daily volume), so enter in several pieces",
    "wait_for_pullback": "Wait for a pullback: the price has just run up sharply (large "
                         "recent 5-bar and 20-bar returns) and sits near its 52-week "
                         "high, so wait for a retracement before entering",
}
ENTRY_KO = {"market_now": "즉시 시장가", "limit_patient": "지정가 대기",
            "scale_in": "분할 진입", "wait_for_pullback": "눌림 대기"}
TRANCHE_LEVELS = (
    "One order: recent 5-bar and 20-bar returns are small and the position is a tiny "
    "share of average daily volume",
    "Two orders: recent returns are moderate, or the position is a noticeable share of "
    "average daily volume",
    "Three orders: recent returns are large, or the position is a large share of "
    "average daily volume",
    "Four orders: recent returns are very large, or the position would move the price "
    "if sent at once",
)

#: 선택지 설명은 **데스크가 그 답으로 실제로 하는 일** 이어야 합니다. Jev 의
#: 확률은 이 문장에 대한 답이기 때문입니다. 데스크에는 부분 축소가 없어서
#: reduce 도 sell 처럼 보유 전체를 청산합니다(`desk._to_insight`). 예전 설명은
#: "일부는 남긴다" 였는데, 그러면 Jev 는 "다 팔자" 에 25%만 걸었는데 reduce
#: 45% 가 청산 쪽 묶음을 65% 위로 올려 전량 청산이 나갔습니다 — 묻지 않은
#: 질문에 답한 셈입니다. 부분 축소를 만들면 이 설명과 `_HEAD_GROUPS` 를 같이
#: 고쳐야 합니다.
#:
#: **다른 모델에게 미치는 효과도** 데스크가 하는 일입니다. hold 는 인사이트를
#: 내지 않아 종목을 규칙 알파(investor_flow·ema_cross …)에게 맡깁니다 — 그들이
#: 살 수도 있습니다. reduce·sell·strong_sell 은 FLAT 을 보유기간의 절반(최소
#: 2봉) 동안 내고, 포트폴리오 층은 FLAT 을 **모든 모델의 매수를 0 으로 만드는
#: 거부권** 으로 읽습니다(`portfolio/base.py`). 보유가 없는 종목에서도 그렇습니다.
#: 심의 후보는 대부분 보유가 없는 종목이라, "청산" 만 말하는 설명은 대부분의
#: 자리에서 묻는 것과 하는 것이 달랐습니다. 공매도 데스크는 sell 로 공매도를
#: 열지만, Jev 데스크는 공매도를 켤 수 없게 막아 두었습니다(`TradingDesk`).
HEAD_ACTIONS = {
    "strong_buy": "Strong buy: open or add a full position; the evidence strongly and "
                  "independently supports a rise",
    "buy": "Buy: open or add a position; the evidence on balance supports a rise",
    "hold": "Hold: this desk places no order and leaves the stock to the desk's other "
            "models, which may still open or close a position; only when the evidence "
            "is genuinely balanced",
    "reduce": "Reduce: close the whole position if one is held (this desk cannot cut "
              "a position partially); if none is held, block every model from buying "
              "this stock for about half the holding horizon; the case for holding "
              "has weakened but not reversed",
    "sell": "Sell: close the whole position if one is held; if none is held, block "
            "every model from buying this stock for about half the holding horizon; "
            "the evidence on balance points to a fall",
    "strong_sell": "Strong sell: close the whole position immediately if one is held; "
                   "if none is held, block every model from buying this stock for "
                   "about half the holding horizon; the evidence strongly points to a "
                   "fall",
}
HORIZON_LEVELS = (
    "1 to 2 bars: a very short-lived setup that plays out almost immediately",
    "3 to 5 bars: a short swing",
    "6 to 10 bars: a medium swing",
    "11 to 20 bars: a longer swing",
    "More than 20 bars: a slow, position-type thesis",
)
HORIZON_VALUES = (2, 4, 8, 15, 30)
MOVE_LEVELS = (
    "A fall of more than 8%",
    "A fall of 3% to 8%",
    "A fall of 1% to 3%",
    "Roughly flat: within plus or minus 1%",
    "A rise of 1% to 3%",
    "A rise of 3% to 8%",
    "A rise of more than 8%",
)
MOVE_VALUES = (-10.0, -5.0, -2.0, 0.0, 2.0, 5.0, 10.0)

#: 방향 판정의 묶음. 이름, 선택지(동률이면 **덜 극단적인 쪽** 이 앞).
#:
#: 묶는 이유: "strong_buy 40% · buy 35% · hold 25%" 는 선택지 하나씩 보면 어느
#: 것도 과반이 아니지만, 데스크가 묻는 것은 "사는가" 이고 그 답은 75% 입니다.
#: 하나씩 보고 미결로 처리하면 분명한 매수 판단을 관망으로 뭉갭니다.
_HEAD_GROUPS = (("hold", ("hold",)),
                ("exit", ("reduce", "sell", "strong_sell")),
                ("buy", ("buy", "strong_buy")))
_PLAN_GROUPS = (("hold", ("hold",)),
                ("exit", ("sell", "strong_sell")),
                ("buy", ("buy", "strong_buy")))
_TRADE_GROUPS = (("hold", ("hold",)), ("exit", ("sell",)), ("buy", ("buy",)))
_GROUP_KO = {"buy": "매수 측", "hold": "관망", "exit": "매도 측"}
#: 행동의 한국어 이름 — 화면(`ACTION_STYLE`)과 같은 말. 템플릿에는 뒷좌석이
#: 영어 선택지와 맞춰 읽도록 `매수(buy)` 처럼 괄호에 원래 값을 붙입니다.
ACTION_KO = {"strong_buy": "적극 매수", "buy": "매수", "hold": "관망",
             "reduce": "축소", "sell": "매도", "strong_sell": "적극 매도"}
_HEAD_GROUP_KO = {"buy": "매수 측", "hold": "관망", "exit": "축소·청산 측"}

_SOURCE = "Jev 확률 판정"


# ─────────────────────────────────────────────────────────────────────────────
# 요청
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class JevRequest:
    """한 좌석 호출 = `jev_evaluate` 한 번."""

    #: analyst | debate | risk_debate | risk_verdict | plan | trade | head | generic
    kind: str
    #: 좌석을 못 알아보고 단계만 스키마로 알아본 호출, 또는 generic 이면 None.
    seat: Seat | None
    schema: dict
    state: dict
    questions: dict = field(default_factory=dict)

    @property
    def arguments(self) -> dict:
        return {"state": self.state, "questions": self.questions}


#: 좌석은 시스템 프롬프트로 알아봅니다 — 데스크가 넘기는 것은 그것뿐입니다.
_SEAT_BY_SYSTEM: dict[str, Seat] = {s.system: s for s in ALL_SEATS}


def seat_for(system: str) -> Seat | None:
    return _SEAT_BY_SYSTEM.get(system)


#: 데스크 단계별 응답 스키마. 좌석을 못 알아봐도 단계는 스키마로 압니다.
_STAGE_SCHEMAS: tuple[tuple[str, dict], ...] = (
    ("analyst", ANALYST_SCHEMA),
    ("debate", DEBATE_SCHEMA),
    ("risk_debate", RISK_DEBATE_SCHEMA),
    ("risk_verdict", RISK_VERDICT_SCHEMA),
    ("plan", RESEARCH_PLAN_SCHEMA),
    ("trade", TRADER_SCHEMA),
    ("head", HEAD_SCHEMA),
)


def stage_for(schema: dict) -> str | None:
    """스키마가 데스크의 어느 단계 것인가. 값이 **같으면** 같은 스키마입니다."""
    for stage, stage_schema in _STAGE_SCHEMAS:
        if schema == stage_schema:
            return stage
    return None


def build_request(system: str, user: str, schema: dict) -> JevRequest:
    """좌석 호출을 Jev 질문으로 바꾼다. 채울 수 없는 스키마면 부르기 전에 실패."""
    seat = seat_for(system)
    if seat is not None and schema == seat.schema:
        state = {
            "seat": seat.title_en,
            "role": LENSES[seat.key],
            "glossary": glossary_for(user),
            "evidence": user,
        }
        questions = _QUESTIONS[seat.stage](seat)
        return JevRequest(seat.stage, seat, schema, state, questions)

    stage = stage_for(schema)
    if stage is not None:
        # 좌석은 모르지만(프롬프트를 고쳤거나 다른 단계의 좌석이 이 스키마를
        # 넘겼다) 스키마가 데스크 단계의 것입니다. 일반 경로로 보내면 헤드는
        # 판단 보류 없이 가장 큰 선택지를, 리스크는 기준 없는 거부를 받습니다 —
        # 돈이 걸린 칸이 규칙 없이 채워집니다. 그래서 단계의 질문과 규칙을
        # 그대로 쓰고, 역할은 받은 프롬프트를, 좌석에 따라 갈리는 말(강세/약세,
        # 공격/보수)은 어느 쪽도 아닌 말을 씁니다.
        state = {
            "seat": "unknown",
            "role": system,
            "glossary": glossary_for(user),
            "evidence": user,
        }
        return JevRequest(stage, None, schema, state, _QUESTIONS[stage](None))

    # 데스크 좌석이 아닌 호출(예: 옛 council). 스키마의 선택지·예/아니오 칸만
    # 물을 수 있고, 나머지는 템플릿으로 채웁니다.
    questions = _generic_questions(schema)
    state = {
        "seat": "unknown",
        "role": system,
        "glossary": glossary_for(user),
        "evidence": user,
    }
    return JevRequest("generic", None, schema, state, questions)


# ── 좌석 단계별 질문 ─────────────────────────────────────────────────────────
def _choice(instructions: str, options: dict) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": dict(options)}


def _boolean(instructions: str, true: str, false: str) -> dict:
    return {"type": "boolean", "instructions": instructions,
            "criteria": {"true": true, "false": false}}


def _score(instructions: str, levels) -> dict:
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


def _analyst_questions(seat: Seat | None) -> dict:
    # 보유기간은 증거에 적혀 있습니다(`TradingDesk._run_analyst`). 예전에는 헤드
    # 에게만 적혀 있어서, 분석가는 "보유기간 동안" 을 짐작으로 채웠습니다 —
    # 매크로처럼 기간에 따라 답이 갈리는 좌석이 서로 다른 기간을 가정했습니다.
    questions = {
        "stance": _choice(
            "From this seat's perspective only, which way does the evidence this seat "
            "is responsible for point over the desk's default holding period (stated "
            "in the evidence)?",
            {
                "bullish": "The evidence this seat covers points to the price rising "
                           "over that holding period",
                "neutral": "Mixed or balanced evidence, or this seat's job is not to "
                           "call direction, or the data this seat needs is missing",
                "bearish": "The evidence this seat covers points to the price falling "
                           "over that holding period",
            },
        ),
        "data_sufficient": _boolean(
            "Does the evidence contain the specific data this seat needs (see the "
            "role)? Missing, empty or too-short sections mean no.",
            "The data this seat needs is present and long enough to judge",
            "The data this seat needs is missing, empty or too short",
        ),
    }
    if seat is not None and seat.key == "microstructure":
        # 이 좌석의 **본업**. 방향 질문은 그대로 두어(투표·합의가 달라지지
        # 않게) 한 호출 안에서 하나를 더 묻습니다.
        questions["execution"] = _choice(
            "This seat does not call direction. Can an order of the intended size be "
            "executed without costs eating the edge? Judge the spread, the estimated "
            "round-trip cost, the position's share of average daily volume, and the lot "
            "and tick sizes against a typical recent move (the 5-bar and 20-bar "
            "returns).", EXECUTION)
    return questions


def _debate_questions(seat: Seat | None) -> dict:
    if seat is None:
        # 강세인지 약세인지 모릅니다. 어느 편도 들지 않고 "역할이 펴는 논거" 를 묻습니다.
        side = "the case this seat argues (see the role)"
        levels = (
            "No credible case: nothing in the evidence supports the position the role "
            "argues, or it points clearly the other way",
            "A weak case: one or two supportive observations, contradicted by others or "
            "resting mostly on inference",
            "A moderate case: several supportive observations, with material "
            "counter-evidence or open questions",
            "A strong case: most observed evidence supports the position the role "
            "argues and the counter-evidence is minor",
            "A very strong case: several independent observations agree, with little "
            "credible counter-evidence",
        )
    elif seat.key == "bull":
        side = "the case FOR buying"
        levels = (
            "No credible case for buying: nothing in the evidence supports a rise, or "
            "it points clearly the other way",
            "A weak case for buying: one or two supportive observations, contradicted "
            "by others or resting mostly on inference",
            "A moderate case for buying: several supportive observations, with "
            "material counter-evidence or open questions",
            "A strong case for buying: most observed evidence supports a rise and the "
            "counter-evidence is minor",
            "A very strong case for buying: several independent observations agree, "
            "with little credible counter-evidence",
        )
    else:
        side = "the case AGAINST buying (for selling or staying out)"
        levels = (
            "No credible case against buying: nothing in the evidence supports a fall "
            "or staying out",
            "A weak case against buying: one or two concerns, contradicted by others or "
            "resting mostly on inference",
            "A moderate case against buying: several concrete concerns, with material "
            "evidence on the other side",
            "A strong case against buying: most observed evidence supports a fall or "
            "staying out and the counter-evidence is minor",
            "A very strong case against buying: several independent observations agree "
            "that the price will fall or the risk is not worth it",
        )
    return {"case": _score(
        f"Using only the numbers in the evidence, how strong is {side}? Judge the "
        "evidence, not the role.", levels)}


def _risk_debate_questions(seat: Seat | None) -> dict:
    if seat is None:
        view = ("From this risk seat's view (see the role; size must be justified by "
                "concrete, nameable evidence, never by general unease or enthusiasm)")
    elif seat.key == "risk_aggressive":
        view = ("From the aggressive risk seat's view (the cost of entering too small "
                "is real, but size must be justified concretely)")
    else:
        view = ("From the conservative risk seat's view (capital preservation through "
                "concrete, nameable hazards only, never reflexive rejection)")
    return {
        "scale": _score(f"{view}, what share of the intended position size is "
                        "justified?", SIZE_LEVELS),
        "hazard": _choice("Which concrete, nameable hazard is most evident in the "
                          "evidence?", HAZARDS),
    }


def _risk_verdict_questions(seat: Seat | None) -> dict:
    return {
        # 거부가 **무엇을 하는지** 를 묻는 말에 적습니다. 거부는 헤드보다 앞서
        # 보유를 전량 청산하고 모든 모델의 매수를 막습니다(`desk._to_insight`,
        # FLAT 은 포트폴리오의 거부권). 리스크 좌석은 방향이 정해지기 **전에**
        # 답하므로 "이 거래" 는 아직 없습니다. 예전 문장("veto this trade?")은
        # 새 주문 하나를 막는 일로 읽혀, 얇은 유동성에서 "더 사지 말자" 가 보유를
        # 그 유동성 속으로 파는 일이 될 수 있었습니다. 기준(네 가지)은 그대로입니다.
        "veto": _boolean(
            "Should the neutral risk seat veto? On this desk a veto overrides the head "
            "of desk: it closes the whole position if one is held (see the current "
            "holding in this stock) and blocks every model from buying this stock for "
            f"the next {VETO_BLOCK_BARS} bars. The risk seat answers before the "
            "direction is decided, so this is not a veto of one new order. Veto ONLY if "
            "the loss limit cannot be determined, liquidity cannot absorb the target "
            "size, a portfolio concentration limit is breached, or the whole case is "
            "inference with no observation. Other discomfort, including a concern that "
            "only argues for adding less, is not a veto: the size multiplier handles it.",
            "At least one of the four veto conditions is true",
            "None of the four veto conditions is true; any concern is a matter of size",
        ),
        "scale": _score(
            "What reconciled position size is justified? Weigh the aggressive and "
            "conservative arguments by which gave more concrete, nameable evidence, "
            "not by a mechanical midpoint.", SIZE_LEVELS),
        "veto_reason": _choice(
            "Which of these veto conditions best describes the evidence?",
            VETO_REASONS),
    }


def _plan_questions(seat: Seat | None) -> dict:
    return {
        "rating": _choice(
            "As research manager, which directional rating do the analyst reports and "
            "the bull/bear debate support? Hold only if the evidence is genuinely "
            "balanced.", RATINGS),
        "winner": _choice(
            "Which side of the bull/bear debate carried, judged by the evidence each "
            "side cited rather than by volume?", WINNERS),
    }


def _trade_questions(seat: Seat | None) -> dict:
    return {
        "action": _choice(
            "Translate the research plan into an order direction. Do not re-argue the "
            "direction; follow the plan (and a risk veto, if any).", TRADE_ACTIONS),
        "entry_style": _choice(
            "Which entry style fits the spread and the estimated round-trip cost, the "
            "size relative to liquidity, and how far the price has recently run (the "
            "5-bar and 20-bar returns and the distance from the 52-week high)?",
            ENTRY_STYLES),
        "tranches": _score(
            "Into how many orders should the entry be split, given the size of recent "
            "5-bar and 20-bar returns and the position's share of average daily "
            "volume?", TRANCHE_LEVELS),
    }


def _head_questions(seat: Seat | None) -> dict:
    # 헤드 단계의 반대 좌석 후보는 헤드를 뺀 15석 — `_map_head` 가 읽는 목록과 같습니다.
    others = {s.key: s.title_en for s in ALL_SEATS if s.key != "head"}
    others["none"] = "No seat's report clearly contradicts the majority view"
    return {
        "action": _choice(
            "As head of desk, weighing every seat's report, both debates, the risk "
            "verdict, the research plan, the trader's plan and any past-decision "
            "outcomes, which final action does the evidence support? Be calibrated.",
            HEAD_ACTIONS),
        "horizon": _score(
            "Over how many bars is the desk's view expected to play out? The default "
            "holding period is stated at the end of the evidence.", HORIZON_LEVELS),
        "move": _score(
            "What price change over that horizon, from the latest close, does the "
            "evidence most support?", MOVE_LEVELS),
        "dissent": _choice(
            "Which seat's report most contradicts the majority view in the evidence?",
            others),
    }


_QUESTIONS = {
    "analyst": _analyst_questions,
    "debate": _debate_questions,
    "risk_debate": _risk_debate_questions,
    "risk_verdict": _risk_verdict_questions,
    "plan": _plan_questions,
    "trade": _trade_questions,
    "head": _head_questions,
}


# ── 시작 점검 ────────────────────────────────────────────────────────────────
#: 데스크가 켜질 때 한 번 묻는 것. 좌석이 쓰는 도구(`jev_evaluate`)와 세 가지
#: 질문 모양(선택·예아니오·단계 점수)을 그대로 씁니다.
#:
#: 예전 점검은 `jev_check` 하나였습니다 — 키와 연결만 확인하고, 좌석이 실제로
#: 보내는 질문 형식은 한 번도 확인하지 않았습니다. 서버가 그 형식을 거절하면
#: (-32602) 점검은 통과하고 데스크는 "켜짐" 인 채 봉마다 16석이 전부 실패했습니다.
#: 답은 매매에 쓰지 않습니다. 좌석과 같은 읽기(`read_choice` …)를 통과하는지,
#: 그리고 답이 정해진 두 질문(파랑, 예)의 확률이 **맞는 쪽** 인지 봅니다
#: (`read_preflight`).
PREFLIGHT_STATE = {
    "purpose": "Startup check of a trading desk: confirms the connection and the "
               "question format. The answers are not used for any decision.",
    "note": "The sky in this note is blue.",
}
PREFLIGHT_QUESTIONS = {
    "check_choice": _choice("Which colour does the note give the sky?", {
        "blue": "The note says the sky is blue",
        "other": "The note gives the sky another colour, or none",
    }),
    "check_boolean": _boolean("Is this a startup check?",
                              "The state describes a startup check",
                              "The state describes something else"),
    "check_score": _score("How clearly does the state describe a startup check?", (
        "Not at all: the state is about something else",
        "Partly: a check is mentioned but its purpose is unclear",
        "Clearly: the state says it is a startup check and what it confirms",
    )),
}


def preflight_arguments() -> dict:
    """시작 점검 한 번의 `jev_evaluate` 인자."""
    return {"state": dict(PREFLIGHT_STATE),
            "questions": {k: dict(v) for k, v in PREFLIGHT_QUESTIONS.items()}}


class PreflightAnswerFlipped(LLMError):
    """시작 점검의 답이 정해진 답과 반대다. 답의 **의미** 가 바뀌었을 수 있다."""


def read_preflight(payload: Any) -> None:
    """좌석과 같은 읽기로 답을 읽고, 답이 정해진 두 질문은 답까지 본다.

    모양이 어긋나면 `LLMError`, 답이 뒤집혔으면 `PreflightAnswerFlipped`.

    모양만 보던 때는 서버가 예/아니오의 `probability` 를 P(아니오)로 바꿔도
    점검을 통과했습니다. 그러면 리스크 좌석의 "거부 없음 5%" 가 P(거부)=0.95
    로 읽혀 **봉마다 보유가 청산되고 매수가 막힙니다.** 하늘의 색(파랑)과
    "시작 점검인가"(예)는 답이 정해져 있고 이미 묻고 있으니, 추가 비용 없이
    확률의 방향을 확인할 수 있습니다. 문턱은 느슨하게(과반) 둡니다. 단계
    점수는 모양만 봅니다 — "Partly" 와 "Clearly" 사이는 정당한 판단이라 그걸로
    데스크를 끄면 멀쩡한 날에도 꺼질 수 있습니다.
    """
    answers = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(answers, dict):
        raise LLMError(f"jev: 시작 점검 응답에 answers 가 없습니다: {str(payload)[:200]}")
    sky = read_choice(answers, "check_choice",
                      tuple(PREFLIGHT_QUESTIONS["check_choice"]["criteria"]))
    p_yes = read_boolean(answers, "check_boolean")
    read_score(answers, "check_score", len(PREFLIGHT_QUESTIONS["check_score"]["criteria"]))
    if sky["blue"] < 0.5 or p_yes < 0.5:
        # 422: 다시 물어도 같은 답이 옵니다(`complete()` 가 재시도하지 않습니다).
        raise PreflightAnswerFlipped(
            f"jev 422: 시작 점검의 답이 정해진 답과 반대입니다 — 하늘이 파랑 "
            f"{_pct(sky['blue'])}(정답: 파랑), '시작 점검인가' 예 {_pct(p_yes)}"
            f"(정답: 예). Jev 답의 확률 방향이 바뀌었을 수 있습니다")


# ── 데스크 밖의 호출 ─────────────────────────────────────────────────────────
def _is_enum(spec: dict) -> bool:
    return spec.get("type") == "string" and bool(spec.get("enum"))


def _is_string_list(spec: dict) -> bool:
    return (spec.get("type") == "array"
            and (spec.get("items") or {}).get("type") == "string")


def _generic_questions(schema: dict) -> dict:
    """스키마의 선택지 칸은 choice 로, 예/아니오 칸은 boolean 으로.

    필수 칸 가운데 이 둘과 서술 칸(템플릿)과 conviction(첫 선택지의 확률) 말고는
    채울 방법이 없습니다. 그런 칸이 있으면 **부르기 전에** 실패합니다 — 채우지
    못할 답을 받으려고 과금되는 호출을 보낼 이유가 없고, 호출한 쪽의 대체
    경로가 그 자리를 맡습니다. "422" 를 붙여 재시도하지 않게 합니다.
    """
    props = (schema or {}).get("properties") or {}
    questions: dict = {}
    for name, spec in props.items():
        desc = spec.get("description")
        hint = f" ({desc})" if desc else ""
        if _is_enum(spec):
            questions[name] = {
                "type": "choice",
                "instructions": f"Which value of the field '{name}' does the evidence "
                                f"best support, following the role?{hint}",
                "criteria": {str(v): None for v in spec["enum"]},
            }
        elif spec.get("type") == "boolean":
            questions[name] = {
                "type": "boolean",
                "instructions": f"Following the role, is the field '{name}' true for "
                                f"this evidence?{hint}",
            }
    has_enum = any(q["type"] == "choice" for q in questions.values())
    for name in (schema or {}).get("required") or []:
        spec = props.get(name) or {}
        if name in questions:
            continue
        if name == "conviction" and has_enum:
            continue
        if spec.get("type") == "string" or _is_string_list(spec):
            continue
        raise LLMError(f"jev 422: 필수 칸 '{name}' 은 Jev 의 판단으로 채울 수 없습니다 "
                       "(선택지·예/아니오·서술 칸만 가능)")
    if not questions:
        raise LLMError("jev 422: 이 응답 형식에는 Jev 에게 물을 선택지·예/아니오 칸이 없습니다")
    return questions


# ─────────────────────────────────────────────────────────────────────────────
# 답 → 스키마
# ─────────────────────────────────────────────────────────────────────────────
def map_answers(request: JevRequest, payload: Any, *,
                undecided_below: float = DEFAULT_UNDECIDED_BELOW) -> dict:
    """Jev 의 답을 좌석 스키마의 dict 로. 답이 빠지거나 깨졌으면 `LLMError`.

    `LLMError` 는 데스크가 이미 아는 실패입니다 — 좌석마다 정해 둔 안전한
    대체값(분석가는 투표권 없음, 리스크는 사이즈 축소 …)으로 넘어갑니다.
    반쯤 채운 dict 를 돌려주는 것보다 그쪽이 안전합니다.
    """
    answers = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(answers, dict):
        raise LLMError(f"jev: 응답에 answers 가 없습니다: {str(payload)[:200]}")
    u = undecided_threshold(undecided_below)
    mapper = _MAPPERS.get(request.kind, _map_generic)
    out = mapper(request, answers, u)
    _validate(request.schema, out)
    return out


#: `undecided_below` 가 이 값 이상이면 거절합니다. 이 값은 **거부 문턱이기도**
#: 합니다(`veto_band`: P(거부) ≥ u 일 때만 거부). 1.0 이면 99% 거부도 나가지
#: 않고, 0.95 면 94% 거부가 "배율 절반" 으로 끝납니다.
MAX_UNDECIDED_BELOW = 0.95


def undecided_threshold(value: Any) -> float:
    # bool 은 float 로 읽히면 1.0 입니다. YAML 의 `undecided_below: yes`·`true`·
    # `on` 이 그대로 "모든 거부를 무시" 가 되었습니다. `_prob` 도 bool 을
    # 확률로 받지 않습니다.
    if isinstance(value, bool):
        raise LLMError(f"jev: undecided_below 는 숫자여야 합니다(참/거짓이 아니라 "
                       f"예: 0.65): {value!r}")
    try:
        u = float(value)
    except (TypeError, ValueError) as exc:
        raise LLMError(f"jev: undecided_below 는 숫자여야 합니다: {value!r}") from exc
    if math.isnan(u):
        raise LLMError("jev: undecided_below 가 NaN 입니다")
    # 범위 밖은 잘라 쓰지 않고 거절합니다. 잘라 쓰면 백분율로 잘못 적은 65 가
    # 1.0 이 되어 **모든 거부가 무시** 되고(95% 거부도 배율 절반일 뿐), 부호를
    # 잘못 적은 -0.65 는 0 이 되어 규칙이 꺼집니다(34% 매도가 그대로 나갑니다).
    # 둘 다 시작 점검을 조용히 통과했습니다.
    if not 0.0 <= u <= 1.0:
        raise LLMError(f"jev: undecided_below 는 0~1 사이의 확률이어야 합니다 "
                       f"(예: 0.65, 끄려면 0): {value!r}")
    # 1.0 도 같은 결과였습니다. 이 값은 방향의 판단 보류 문턱이면서 **거부
    # 문턱** 이라, "덜 사고팔게" 올린 값이 거부를 약하게 합니다. 끝까지 올리면
    # 거부가 사라집니다.
    if u >= MAX_UNDECIDED_BELOW:
        raise LLMError(f"jev: undecided_below 가 {MAX_UNDECIDED_BELOW} 이상이면 리스크 "
                       f"거부가 사실상 나가지 않습니다 — 이 값은 거부 문턱이기도 합니다"
                       f"(P(거부) ≥ 이 값일 때만 거부): {value!r}")
    return u


# ── 답 읽기 ──────────────────────────────────────────────────────────────────
def _answer(answers: dict, qid: str) -> dict:
    a = answers.get(qid)
    if not isinstance(a, dict):
        raise LLMError(f"jev: 질문 '{qid}' 의 답이 없습니다")
    return a


def _prob(value: Any, qid: str) -> float:
    if isinstance(value, bool):
        raise LLMError(f"jev: 질문 '{qid}' 의 확률이 숫자가 아닙니다: {value!r}")
    try:
        p = float(value)
    except (TypeError, ValueError) as exc:
        raise LLMError(f"jev: 질문 '{qid}' 의 확률이 숫자가 아닙니다: {value!r}") from exc
    # 1 을 넘는 확률은 잘라 쓰지 않고 거절합니다. 잘라 쓰면 "1.7" 같은 깨진
    # 값이 확신 100% 가 되어 그대로 주문까지 갑니다.
    if math.isnan(p) or math.isinf(p) or p < 0 or p > 1.0 + 1e-9:
        raise LLMError(f"jev: 질문 '{qid}' 의 확률이 잘못되었습니다: {value!r}")
    return p


def _normalise(probs: dict, qid: str) -> dict:
    """합을 1 로. Jev 는 소수 둘째 자리에서 반올림해 합이 0.99·1.01 이 됩니다.

    **반올림 오차만 메웁니다.** 합이 그보다 멀리 벗어났다면 확률 일부가 우리가
    모르는 선택지 이름에 가 있다는 뜻입니다(대소문자·철자가 어긋난 키 등).
    그것을 버리고 나머지를 1 로 늘리면, 예컨대 "HOLD 0.9 · buy 0.1" 이
    **매수 100%** 로 읽힙니다. 실거래 경로라 그렇게 읽느니 실패로 처리해
    좌석의 안전한 대체값으로 넘깁니다.
    """
    total = sum(probs.values())
    if total <= 0:
        raise LLMError(f"jev: 질문 '{qid}' 의 확률이 전부 0 입니다")
    # 선택지마다 최대 0.005 의 반올림 오차 + 여유.
    tolerance = 0.005 * len(probs) + 0.01
    if abs(total - 1.0) > tolerance:
        raise LLMError(f"jev: 질문 '{qid}' 의 확률 합이 {total:.3f} 입니다 — "
                       "선택지 이름이 어긋났을 수 있어 쓰지 않습니다")
    return {k: v / total for k, v in probs.items()}


def read_choice(answers: dict, qid: str, options) -> dict[str, float]:
    """선택지 → 확률. 순서는 믿지 않습니다 — Jev 는 매번 다른 순서로 줍니다."""
    raw = _answer(answers, qid).get("probabilities")
    if not isinstance(raw, dict):
        raise LLMError(f"jev: 질문 '{qid}' 에 probabilities 가 없습니다")
    probs = {str(o): 0.0 for o in options}
    for key, value in raw.items():
        if str(key) in probs:
            probs[str(key)] = _prob(value, qid)
    return _normalise(probs, qid)


def read_boolean(answers: dict, qid: str) -> float:
    """P(예). `jev_evaluate` 는 `probability`, 다른 도구는 `probability_yes`."""
    a = _answer(answers, qid)
    for name in ("probability", "probability_yes"):
        if name in a:
            return _prob(a[name], qid)
    raise LLMError(f"jev: 질문 '{qid}' 에 probability 가 없습니다")


def read_score(answers: dict, qid: str, levels: int) -> tuple[list[float], float, float]:
    """(단계별 확률, 기대 단계(0 부터), 확신도). 키는 문자열 "0".."n-1"."""
    a = _answer(answers, qid)
    raw = a.get("probabilities")
    if not isinstance(raw, dict):
        raise LLMError(f"jev: 질문 '{qid}' 에 probabilities 가 없습니다")
    probs = {str(i): 0.0 for i in range(levels)}
    for key, value in raw.items():
        if str(key) in probs:
            probs[str(key)] = _prob(value, qid)
    probs = _normalise(probs, qid)
    ordered = [probs[str(i)] for i in range(levels)]
    # 기대값은 여기서 계산합니다. Jev 의 `score` 도 같은 값이지만, 셈은 Jev 가
    # 아니라 코드가 하는 것이 이 어댑터의 원칙입니다.
    expected = sum(i * p for i, p in enumerate(ordered))
    confidence = _confidence(a, max(ordered))
    return ordered, expected, confidence


def _confidence(answer: dict, fallback: float) -> float:
    value = answer.get("confidence")
    if value is None or isinstance(value, bool):
        return fallback
    try:
        c = float(value)
    except (TypeError, ValueError):
        return fallback
    return fallback if math.isnan(c) else min(max(c, 0.0), 1.0)


def _argmax(probs: dict, prefer) -> str:
    """가장 큰 쪽. 동률이면 `prefer` 에서 앞선 쪽 — 늘 덜 공격적인 쪽을 앞에 둡니다."""
    order = [o for o in prefer if o in probs] + [o for o in probs if o not in prefer]
    return max(order, key=lambda o: (probs[o], -order.index(o)))


def _half_up(x: float) -> int:
    """반올림. 파이썬 `round` 는 2.5 → 2 (은행가 반올림) 라 쓰지 않습니다."""
    return int(math.floor(x + 0.5))


def _clamp01(x: float) -> float:
    return min(max(float(x), 0.0), 1.0)


def _pct(p: float) -> str:
    return f"{p:.0%}"


def _grouped(probs: dict, groups, u: float) -> tuple[str, str, dict, bool]:
    """(행동, 가장 큰 묶음, 묶음별 확률, 판단 보류였나).

    가장 큰 묶음이 `u` 에 못 미치면 관망입니다(그래도 가장 큰 묶음은 그대로
    돌려줍니다 — 얼마나 가까웠는지가 화면에 남아야 합니다). 넘으면 그 묶음
    **안에서** 가장 큰 선택지를 고릅니다.
    """
    # 합은 소수 아홉째 자리에서 반올림합니다(`veto_band` 와 같습니다). 안 하면
    # 0.30 + 0.35 가 0.6499999999999999 가 되어, 딱 65% 인 묶음이 청산 쪽은
    # 보류되고 매수 쪽(0.35 + 0.30 = 0.65)은 결정되는 비대칭이 생깁니다.
    masses = {name: round(sum(probs[o] for o in options), 9) for name, options in groups}
    names = [name for name, _ in groups]              # 동률이면 앞(관망)이 이깁니다
    winner = max(names, key=lambda n: (masses[n], -names.index(n)))
    if masses[winner] < u:
        return "hold", winner, masses, True
    options = dict(groups)[winner]
    action = _argmax({o: probs[o] for o in options}, prefer=options)
    return action, winner, masses, False


def _act(action: str) -> str:
    """`매수(buy)` — 사람은 한국어를, 뒷좌석(Jev)은 괄호의 선택지 값을 읽습니다."""
    return f"{ACTION_KO.get(action, action)}({action})"


def _threshold_ko(u: float) -> str:
    return _pct(u) if u > 0.5 else "과반"


def _flip_ko(side: str, u: float) -> str:
    if u > 0.5:
        return f"{side}이 {_pct(u)} 이상이 되면"
    return f"{side}이 가장 우세해지면"


# ── 단계별 옮기기 ─────────────────────────────────────────────────────────────
def _map_analyst(request: JevRequest, answers: dict, u: float) -> dict:
    stance_p = read_choice(answers, "stance", STANCES)
    sufficient_p = read_boolean(answers, "data_sufficient")
    stance = _argmax(stance_p, prefer=("neutral", "bearish", "bullish"))
    conviction = stance_p[stance]
    spread = " · ".join(f"{STANCE_KO[s]} {_pct(stance_p[s])}" for s in STANCES)
    sufficient = sufficient_p >= 0.5
    if sufficient:
        key_points = [spread, f"판단 재료 충분 확률 {_pct(sufficient_p)} ({_SOURCE})"]
    else:
        # 좌석 프롬프트가 요구하는 것과 같습니다 — 재료가 없으면 중립, 확신 0.2 이하.
        # 데스크는 이 좌석을 투표에서 빼지만, 화면과 뒷좌석이 읽는 값도 정직해야
        # 합니다.
        key_points = [f"판단 재료 부족 (충분 확률 {_pct(sufficient_p)}) — 중립 처리",
                      f"참고 확률: {spread} ({_SOURCE})"]
        stance = "neutral"
        conviction = min(conviction, 0.2)
    out = {
        "stance": stance,
        "conviction": round(_clamp01(conviction), 3),
        "key_points": key_points,
        "data_sufficient": sufficient,
    }
    if "execution" in request.questions:
        # 미시구조 좌석. 방향·확신·투표는 위 그대로이고, 체결 판단은 서술 칸에만
        # 들어갑니다 — 첫 줄(화면 말풍선)과, 걸림이 있으면 `risks`(프롬프트가
        # "key_points 와 risks 에 비용 문제를 적어라" 라고 요구하는 자리).
        exec_p = read_choice(answers, "execution", EXECUTION)
        verdict = _argmax(exec_p, prefer=("conditional", "not_executable", "executable"))
        line = f"체결 가능성: {EXECUTION_KO[verdict]} {_pct(exec_p[verdict])} ({_SOURCE})"
        key_points.insert(0 if sufficient else 1, line)
        if verdict != "executable":
            out["risks"] = [f"{EXECUTION_KO[verdict]} ({_pct(exec_p[verdict])})"]
    return out


def _map_debate(request: JevRequest, answers: dict, u: float) -> dict:
    probs, expected, _ = read_score(answers, "case", 5)
    top = _argmax({str(i): p for i, p in enumerate(probs)},
                  prefer=[str(i) for i in range(5)])
    level = int(top)
    conviction = expected / 4
    if request.seat is None:
        side = "이 좌석의 논거"                 # 강세·약세를 모르면 편을 붙이지 않습니다
    elif request.seat.key == "bull":
        side = "매수 논거"
    else:
        side = "매수 반대(매도·관망) 논거"
    argument = (f"{side} 강도: {CASE_LEVELS_KO[level]} ({_pct(probs[level])}). "
                f"5단계 기대값 {expected:.1f}/4 → 확신도 {conviction:.2f} ({_SOURCE})")
    return {"argument": argument, "conviction": round(_clamp01(conviction), 3)}


def _size_summary(probs: list[float]) -> tuple[int, str]:
    top = int(_argmax({str(i): p for i, p in enumerate(probs)},
                      prefer=[str(i) for i in range(len(probs))]))
    return top, f"{SIZE_LEVELS_KO[top]} {_pct(probs[top])}"


def _map_risk_debate(request: JevRequest, answers: dict, u: float) -> dict:
    probs, expected, confidence = read_score(answers, "scale", 5)
    hazards = read_choice(answers, "hazard", HAZARDS)
    scale = expected / 4
    _, size_text = _size_summary(probs)
    hazard = _argmax(hazards, prefer=tuple(HAZARDS))
    who = request.seat.title_ko if request.seat else "리스크 좌석"
    argument = (f"{who}: 제안 배율 {scale:.2f} (가장 유력: {size_text}). "
                f"가장 뚜렷한 위험: {HAZARD_KO[hazard]} {_pct(hazards[hazard])} ({_SOURCE})")
    return {
        "argument": argument,
        "proposed_scale": round(_clamp01(scale), 3),
        "named_hazards": [] if hazard == "none" else [HAZARD_KO[hazard]],
        "conviction": round(confidence, 3),
    }


def veto_band(p_veto: float, u: float) -> str:
    """"veto" / "undecided" / "clear".

    거부는 보유를 **청산** 합니다(데스크 `_to_insight`). 그래서 확률이 애매한
    거부를 그대로 거부로 읽지도, 없던 일로 읽지도 않습니다 — 거부하지 않되
    사이즈를 절반 이하로 묶습니다. `u ≤ 0.5` 면 이 구간이 사라지고 과반이면
    거부입니다.
    """
    if u <= 0.5:
        return "veto" if p_veto >= 0.5 else "clear"
    if p_veto >= u:
        return "veto"
    if p_veto >= round(1.0 - u, 9):
        return "undecided"
    return "clear"


def _map_risk_verdict(request: JevRequest, answers: dict, u: float) -> dict:
    p_veto = read_boolean(answers, "veto")
    probs, expected, _ = read_score(answers, "scale", 5)
    reasons = read_choice(answers, "veto_reason", VETO_REASONS)
    scale = _clamp01(expected / 4)
    _, size_text = _size_summary(probs)
    band = veto_band(p_veto, u)
    veto = band == "veto"
    veto_reason = ""
    if veto:
        reason = _argmax(reasons, prefer=tuple(VETO_REASONS))
        # 거부 확률은 reasoning 이 첫 문장에 말합니다. 화면은 사유와 reasoning 을
        # 이어 붙이므로, 여기서 또 적으면 같은 숫자가 두 번 읽힙니다.
        if reason == "none_applies":
            veto_reason = (f"네 가지 거부 사유 중 뚜렷한 것은 없음 "
                           f"(해당 없음 {_pct(reasons[reason])})")
        else:
            veto_reason = f"{VETO_REASON_KO[reason]} (사유 확률 {_pct(reasons[reason])})"
        reasoning = (f"거부 확률 {_pct(p_veto)} ≥ 기준 {_threshold_ko(u)} → 거부. "
                     f"배율 기대값 {scale:.2f} ({_SOURCE})")
        position_scale = scale
    elif band == "undecided":
        position_scale = min(scale, 0.5)
        # 첫 문장은 짧게 — 화면 말풍선이 첫 문장만, 64자까지 보여 줍니다.
        reasoning = (f"판단 보류 — 거부하지 않고 배율 {position_scale:.2f} 로 제한. "
                     f"거부 확률 {_pct(p_veto)} 가 보류 구간"
                     f"({_pct(round(1 - u, 9))}~{_pct(u)}) 안 (기대값 {scale:.2f}) "
                     f"({_SOURCE})")
    else:
        position_scale = scale
        reasoning = (f"거부 확률 {_pct(p_veto)} → 거부 안 함. 배율 {scale:.2f} "
                     f"(가장 유력: {size_text}) ({_SOURCE})")
    return {
        "position_scale": round(position_scale, 3),
        "veto": veto,
        "veto_reason": veto_reason,
        "reasoning": reasoning,
    }


def _masses_ko(masses: dict, names: dict) -> str:
    return " · ".join(f"{names[g]} {_pct(masses[g])}" for g in ("buy", "hold", "exit"))


def _map_plan(request: JevRequest, answers: dict, u: float) -> dict:
    ratings = read_choice(answers, "rating", RATINGS)
    winners = read_choice(answers, "winner", WINNERS)
    rating, group, masses, undecided = _grouped(ratings, _PLAN_GROUPS, u)
    # 확신도는 **최종 등급이 속한** 묶음의 확률입니다 — 트레이더와 같습니다.
    # 예전에는 보류로 관망이 되어도 가장 큰 묶음의 확률을 적어서 "rating hold,
    # conviction 0.6" 이 되었고, 그 JSON 을 읽는 트레이더·헤드와 화면은 그것을
    # "관망에 60% 확신" 으로 읽었습니다. 얼마나 가까웠는지는 rationale 이
    # 묶음마다 적습니다.
    winner = _argmax(winners, prefer=("balanced", "bear", "bull"))
    debate = f"토론: {WINNER_KO[winner]} {_pct(winners[winner])}"
    spread = _masses_ko(masses, _GROUP_KO)
    if undecided:
        rationale = (f"판단 보류 — 어느 쪽도 {_threshold_ko(u)}에 못 미칩니다. "
                     f"{spread}. {debate} ({_SOURCE})")
        # "다음 봉" 이 아닙니다 — cadence_bars 가 3 인 설정은 두 봉을 쉬고,
        # 보유가 없는 종목은 후보에 다시 들어야 심의됩니다.
        actions = "방향 없음 — 신규 주문 없이 다음 심의에서 다시 판단"
    else:
        rationale = (f"{_GROUP_KO[group]} {_pct(masses[group])} → {_act(rating)}. "
                     f"{spread}. {debate} ({_SOURCE})")
        if group == "buy":
            actions = (f"방향 {_act(rating)} (매수 측 {_pct(masses[group])}). 진입 방식과 "
                       "분할은 트레이더 좌석이 유동성·체결비용을 보고 정한다")
        elif group == "exit":
            actions = (f"방향 {_act(rating)} (매도 측 {_pct(masses[group])}). 신규 진입 "
                       "없음, 보유 중이면 정리")
        else:
            actions = f"관망 ({_pct(masses[group])}) — 신규 주문 없음"
    return {
        "rating": rating,
        "rationale": rationale,
        "strategic_actions": actions,
        "conviction": round(_clamp01(masses["hold" if undecided else group]), 3),
    }


def _map_trade(request: JevRequest, answers: dict, u: float) -> dict:
    actions = read_choice(answers, "action", TRADE_ACTIONS)
    styles = read_choice(answers, "entry_style", ENTRY_STYLES)
    _, expected, _ = read_score(answers, "tranches", 4)
    action, group, masses, undecided = _grouped(actions, _TRADE_GROUPS, u)
    style = _argmax(styles, prefer=("limit_patient", "scale_in", "wait_for_pullback",
                                    "market_now"))
    tranches = min(max(_half_up(expected) + 1, 1), 4)
    spread = " · ".join(f"{ACTION_KO[name]} {_pct(actions[name])}"
                        for name in TRADE_ACTIONS)
    # 첫 문장은 짧게(화면 말풍선은 첫 문장만 보여 줍니다). 진입 방식과 분할
    # 수는 적지 않습니다 — 각자 칸(`entry_style`, `tranches`)이 있고 화면이
    # 앞에 붙입니다. 이름을 또 적으면 같은 말이 두 번 나옵니다.
    if undecided:
        note = (f"{_act('hold')} — 판단 보류. {spread} 중 {_threshold_ko(u)}에 이른 쪽 "
                f"없음. 진입 방식 확률 {_pct(styles[style])} ({_SOURCE})")
    else:
        note = (f"{_act(action)} {_pct(masses[group])}. 진입 방식 확률 "
                f"{_pct(styles[style])} ({_SOURCE})")
    return {
        "action": action,
        "entry_style": style,
        "tranches": tranches,
        "execution_note": note,
        # 트레이더는 **최종 행동이 속한** 묶음의 확률 — 보류면 관망의 확률입니다.
        "conviction": round(_clamp01(masses["hold" if undecided else group]), 3),
    }


def _map_head(request: JevRequest, answers: dict, u: float) -> dict:
    actions = read_choice(answers, "action", HEAD_ACTIONS)
    horizon_p, _, _ = read_score(answers, "horizon", len(HORIZON_VALUES))
    move_p, _, _ = read_score(answers, "move", len(MOVE_VALUES))
    seat_keys = [s.key for s in ALL_SEATS if s.key != "head"] + ["none"]
    dissent_p = read_choice(answers, "dissent", seat_keys)

    action, group, masses, undecided = _grouped(actions, _HEAD_GROUPS, u)
    horizon = max(1, _half_up(sum(p * v for p, v in zip(horizon_p, HORIZON_VALUES))))
    move = round(sum(p * v for p, v in zip(move_p, MOVE_VALUES)), 2)
    spread = _masses_ko(masses, _HEAD_GROUP_KO)
    outlook = f"기대 변동 {move:+.2f}% · 보유 {horizon}봉"

    if undecided:
        rationale = (f"판단 보류 — 어느 쪽도 {_threshold_ko(u)}에 못 미칩니다. "
                     f"{spread}. {outlook} ({_SOURCE})")
        invalidation = f"다음 재심의에서 {_flip_ko('어느 한쪽', u)} 관망 종료"
    else:
        rationale = (f"{_HEAD_GROUP_KO[group]} {_pct(masses[group])} → {_act(action)}. "
                     f"{spread}, 묶음 안에서 {ACTION_KO[action]} {_pct(actions[action])}. "
                     f"{outlook} ({_SOURCE})")
        # "다음 봉" 이라고 적지 않습니다 — 심의 주기(cadence_bars)가 1 이 아닐
        # 수 있고, 보유가 없는 종목은 후보에 다시 들어야 심의됩니다.
        if group == "buy":
            invalidation = f"다음 재심의에서 {_flip_ko('축소·청산 측', u)} 무효"
        elif group == "exit":
            invalidation = f"다음 재심의에서 {_flip_ko('매수 측', u)} 무효"
        else:
            invalidation = f"다음 재심의에서 {_flip_ko('어느 한쪽', u)} 관망 종료"

    dissenter = _argmax(dissent_p, prefer=["none"] + seat_keys[:-1])
    dissent = ("" if dissenter == "none" else
               f"{SEATS_BY_KEY[dissenter].title_ko} 반대 (Jev {_pct(dissent_p[dissenter])})")
    return {
        "action": action,
        # **최종 행동이 속한** 묶음의 확률 — 트레이더·계획과 같습니다. 보류로
        # 관망이 되면 관망 쪽의 확률입니다. 예전에는 가장 큰 묶음의 확률이라
        # 화면에 "관망 · 확신 60%" 가 떴습니다(실제로는 매수 측 60%, 관망 10%).
        # 관망은 주문이 되지 않고(`_to_insight`) 회고에도 들지 않습니다
        # (`DeskMemory.record`) — 바뀌는 것은 화면의 숫자뿐입니다. 얼마나
        # 가까웠는지는 rationale 이 묶음마다 적습니다.
        "conviction": round(_clamp01(masses["hold" if undecided else group]), 3),
        "expected_move_pct": move,
        "horizon_bars": horizon,
        "rationale": rationale,
        "invalidation": invalidation,
        "dissent": dissent,
    }


def _map_generic(request: JevRequest, answers: dict, u: float) -> dict:
    schema = request.schema or {}
    props = schema.get("properties") or {}
    out: dict = {}
    parts: list[str] = []
    first_enum_p: float | None = None
    for name, question in request.questions.items():
        if question["type"] == "choice":
            options = list(question["criteria"])
            probs = read_choice(answers, name, options)
            best = _argmax(probs, prefer=options)
            out[name] = best
            parts.append(f"{name}: {best} {_pct(probs[best])}")
            if first_enum_p is None:
                first_enum_p = probs[best]
        else:
            p = read_boolean(answers, name)
            out[name] = p >= 0.5
            parts.append(f"{name}: {'예' if p >= 0.5 else '아니오'} ({_pct(p)})")
    summary = f"{_SOURCE} — " + " · ".join(parts)
    if "conviction" in props and "conviction" not in out and first_enum_p is not None:
        out["conviction"] = round(first_enum_p, 3)
    for name in schema.get("required") or []:
        if name in out:
            continue
        spec = props.get(name) or {}
        if spec.get("type") == "string":
            out[name] = summary
        elif _is_string_list(spec):
            out[name] = [summary]
        else:
            raise LLMError(f"jev 422: 필수 칸 '{name}' 을 채울 수 없습니다")
    return out


_MAPPERS = {
    "analyst": _map_analyst,
    "debate": _map_debate,
    "risk_debate": _map_risk_debate,
    "risk_verdict": _map_risk_verdict,
    "plan": _map_plan,
    "trade": _map_trade,
    "head": _map_head,
    "generic": _map_generic,
}


_TYPES = {"string": str, "boolean": bool, "array": list}


def _validate(schema: dict, out: dict) -> None:
    """필수 칸과 선택지를 지켰는가. 어긋나면 이 모듈의 버그이므로 크게 실패합니다."""
    props = (schema or {}).get("properties") or {}
    for name in (schema or {}).get("required") or []:
        if name not in out:
            raise LLMError(f"jev: 필수 칸 '{name}' 이 빠졌습니다")
    for name, value in out.items():
        spec = props.get(name) or {}
        enum = spec.get("enum")
        if enum and value not in enum:
            raise LLMError(f"jev: '{name}' 값 {value!r} 이 선택지 밖입니다")
        kind = spec.get("type")
        if kind in ("number", "integer"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise LLMError(f"jev: '{name}' 이 숫자가 아닙니다: {value!r}")
        elif kind in _TYPES and not isinstance(value, _TYPES[kind]):
            raise LLMError(f"jev: '{name}' 의 형식이 {kind} 가 아닙니다: {value!r}")
