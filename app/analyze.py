from __future__ import annotations
import os, json, logging, httpx, time as _time

log = logging.getLogger("analyze")


SYSTEM = """# ROLE
당신은 월스트리트 헤지펀드(예: Renaissance Technologies, Citadel)의 시니어 퀀트 전략가이자 매크로 트레이딩 책임자다.
데이터의 이면에 숨겨진 '의도'와 '흐름'을 읽어내며, 기관투자자의 관점에서 단기 매매 기회를 포착한다.

# EVALUATION FRAMEWORKS (Advanced)
공신력 있는 기관의 퀀트 팩터 모델과 기술적 분석을 복합 적용한다:

1. **Market Microstructure & Order Flow**
   - **Liquidity Profile**: 현재 가격대 주변의 유동성 공급/수요 추정.
   - **Order Flow Imbalance**: 거래량과 가격 변동을 통해 대형 기관의 'Buy/Sell Wall' 존재 여부 판별.
   - **IEX Feed Bias**: 미국주의 경우 IEX 등 실시간 시세의 미세한 변화를 통해 가격 우위(Edge) 탐색.

2. **Advanced Technicals**
   - **Mean Reversion vs Trend Following**: 현재 시장 상황이 박스권인지 추세장인지 명확히 구분.
   - **Relative Strength (RS)**: 시장 지수(S&P 500, KOSPI) 대비 해당 종목의 탄력성 평가.
   - **Squeeze Momentum**: 변동성 압축 후 분출 시점 포착 (BB/Keltner Channel 기반 추론).

3. **Sentiment & Event-Driven**
   - **News Impact Score**: 단순 키워드가 아닌, 실질적인 EPS 영향도나 시장 기대치와의 괴리(Surprise) 분석.
   - **Options Gamma Exposure (추론)**: 변동성 뉴스를 통해 옵션 델타/감마 헤지 물량이 쏟아질 가격대 예측.

4. **Institutional Flow (Market Specific)**
   - **KR Market**: 외국인/기관의 누적 순매수 평단가 추정과 해당 가격대에서의 지지/저항력.
   - **US Market**: 13F 보고서 흐름과 최근 대형 블록딜 가능성 검토.

# STRATEGY GUIDELINES
- **보수적 목표**: 기대수익률보다 최대 손실액(Maximum Drawdown) 관리에 우선순위를 둔다.
- **R-Multiple**: 최소 1:2 (위험 1 대비 수익 2) 이상의 자리가 아니면 '관망'을 권고한다.
- **포지션 가이드**: '분할 매수'는 분산 진입 가격대를, '적극 매수'는 즉각적인 모멘텀 탑승을 의미한다.

# CORE RULES
1. **Fact-First**: 제공된 데이터(Price/Indicators/Flow/News)가 없는 추측은 엄격히 금지.
2. **Context-Aware**: 단일 지표(예: RSI 30)만으로 판단하지 말고, 거래량과 뉴스 배경을 결합한 '복합 시그널'로만 판단.
3. **Specifics**: 목표가(Target)와 재진입/손절가(Stop)를 소수점(미국) 또는 정수(한국)로 정확히 제시.

# OUTPUT (Strict JSON Schema)
반드시 아래 JSON 스키마로만 응답(마크다운/주석/코드펜스 금지):

{
  "position": "적극 매수|분할 매수|관망|분할 매도|적극 매도",
  "position_emoji": "🟢|🟡|⚪|🟠|🔴",
  "rationale": "포지션 선정의 핵심 근거 3문장 이내 (전문 용어 활용)",
  "frameworks_triggered": ["예: Squeeze Momentum 돌파", "외국인 평단가 지지", "Order Flow Imbalance 확인"],
  "target_price": 숫자,
  "reentry_or_stop_label": "재진입가|손절가",
  "reentry_or_stop_price": 숫자,
  "r_multiple": "예: 1:2.8",
  "holding_period": "초단타|단기|스윙",
  "holding_period_reason": "시간 단위 근거",
  "flow_institutional": "기관 우위|중립|개인 우위",
  "flow_institutional_reason": "데이터 기반 근거",
  "flow_special": "특이 수급/옵션 등 특이사항",
  "macro_regime": "현재 거시경제 테마 반영",
  "market_context": "24시간 내 핵심 리스크 및 기회 요인 요약",
  "confidence": 0~100
}
"""

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-lite:generateContent"


def analyze(symbol: str, snapshot: dict, news: list[dict],
            flow: dict, profile: dict) -> dict:
    from .analyze_rules import analyze_rules

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        log.warning("GEMINI_API_KEY 미설정 — 룰 기반 분석으로 대체")
        return analyze_rules(symbol, snapshot, news, flow, profile)

    payload = {
        "ticker": f"{symbol} ({profile.get('name','')})",
        "market_data": {
            "price": snapshot["quote"]["price"],
            "day_low": snapshot["quote"]["day_low"],
            "day_high": snapshot["quote"]["day_high"],
            "day_open": snapshot["quote"]["day_open"],
            "prev_close": snapshot["quote"]["prev_close"],
            "change_pct": snapshot["quote"]["change_pct"],
            "quote_ts": snapshot["quote"]["ts"],
        },
        "live_news": news,
        "market_flow": {
            "today_volume": snapshot["quote"]["today_volume"],
            "avg_volume_20d": snapshot["quote"]["avg_volume_20d"],
            "relative_volume": snapshot["quote"]["relative_volume"],
            **flow,
            **(snapshot.get("flow_kr") and {"flow_kr": snapshot["flow_kr"]} or {}),
        },
        "technical_indicators": snapshot["indicators"],
        "recent_closes_10d": snapshot["recent_closes"],
        "company": {
            "industry": profile.get("finnhubIndustry"),
            "marketCap_musd": profile.get("marketCapitalization"),
            "country": profile.get("country"),
        },
    }

    body = {
        "system_instruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2000},
    }

    # AI 호출 — 실패 시 빠르게 룰 기반 폴백 (최대 ~5초 대기)
    data = None
    last_err = None
    for attempt in range(2):  # 1회 + 1회 재시도
        try:
            with httpx.Client(timeout=20) as c:
                r = c.post(f"{GEMINI_URL}?key={api_key}", json=body)
                if r.status_code == 429:
                    log.warning(f"Gemini 429 (attempt {attempt+1}/2) — quota 소진 가능성")
                    last_err = "429 quota"
                    if attempt == 0:
                        _time.sleep(3)
                        continue
                    break
                if r.status_code >= 500:
                    log.warning(f"Gemini {r.status_code} — 재시도")
                    last_err = f"{r.status_code}"
                    if attempt == 0:
                        _time.sleep(2)
                        continue
                    break
                r.raise_for_status()
                data = r.json()
                break
        except Exception as e:
            log.error(f"Gemini 호출 예외: {e}")
            last_err = str(e)
            if attempt == 0:
                _time.sleep(2)
                continue
            break

    if data is None:
        log.warning(f"AI 분석 실패({last_err}) — 룰 기반 분석으로 자동 대체")
        result = analyze_rules(symbol, snapshot, news, flow, profile)
        result["fallback_reason"] = last_err
        return result

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        parsed = json.loads(text)
        parsed.setdefault("engine", "ai")
        return parsed
    except Exception as e:
        log.error(f"AI 응답 파싱 실패: {e} — 룰 기반으로 대체")
        result = analyze_rules(symbol, snapshot, news, flow, profile)
        result["fallback_reason"] = f"parse: {e}"
        return result
