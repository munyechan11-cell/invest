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

5. **DART 재무 펀더멘털 (KR 종목, market_flow.dart_financials 있을 때)**
   - 매출/영업이익/순이익 YoY 성장률 → 단기 모멘텀과 일치 여부
   - 영업이익률 5% 미만이면 수익성 약함
   - ROE 분기값 5%+ 이면 양호, 음수면 위험 시그널
   - 부채비율 200%+ 이면 재무 부담 (단기 변동성 확대 가능)
   - 펀더멘털과 차트 시그널 충돌 시 → confidence 하향, 추가 검증 권고

# STRATEGY GUIDELINES (Flexible Short-term)
- **최적 보유 기간 (Optimal Horizon)**: 기술적 셋업에 따라 1일~1주일 사이의 가장 유리한 청산 시점을 스스로 판단한다.
- **매수 구간(Buy Zone) 포착**: 단순 가격 등락이 아닌, 지지선 확보·골든크로스·수급 유입 등 '매수 적기'의 근거가 명확할 때 높은 점수를 부여한다.
- **수익 극대화**: 모멘텀이 강할 경우 조기 청산보다 추세를 향유하는 스윙 전략을, 변동성이 일시적일 경우 빠른 단타 전략을 제시한다.

# CORE RULES
1. **Dynamic Strategy**: 데이터 흐름에 따라 데이트레이딩(Intraday) 또는 스윙(Swing) 전략 중 최적안 선택.
2. **Setup Identification**: 현재 위치가 바닥권 탈출인지, 눌림목인지, 돌파 구간인지 명확히 식별.
3. **Specifics**: 목표가와 손절가를 기술적 근거(피벗, 매물대)에 기반하여 정교하게 제시.

# OUTPUT (Strict JSON Schema)
모든 텍스트 필드는 반드시 한국어로 작성.
{
  "position": "적극 매수" | "분할 매수" | "관망" | "분할 매도" | "적극 매도",
  "position_emoji": "🟢" | "🟡" | "⚪" | "🟠" | "🔴",
  "news_positive": ["호재 1 (구체적 이유, 예: 1Q 매출 23% YoY 증가)", "호재 2", ...],
  "news_negative": ["악재 1 (구체적 이유, 예: 미국 관세 인상 리스크)", "악재 2", ...],
  "news_verdict": "단기 호재 우세" | "단기 악재 우세" | "호재·악재 혼재" | "재료 부재",
  "news_summary": "1~2문장 한국어 요약. '~때문에 좋다/나쁘다' 형식으로 명확히 (예: '1Q 깜짝 실적과 인도 진출 호재로 상승. 단, 관세 리스크 잔존으로 단기 변동성 확대 가능.')",
  "rationale": "포지션 선정 근거 (기술적·수급적·거시적, 한국어)",
  "entry_price": 0.0,
  "target_price": 0.0,
  "stop_price": 0.0,
  "r_multiple": "1:X.X",
  "holding_period": "초단타" | "단기" | "스윙" | "1주",
  "holding_period_reason": "근거 (한국어 1문장)",
  "confidence": 0-100
}

뉴스 분류 원칙:
- news_positive/negative는 비어있을 수 있다 (없으면 빈 배열). 추측 금지.
- 각 항목은 "키워드: 구체적 이유" 형식 (예: "실적 호조: 1Q 영업이익 35% 증가")
- news_summary는 사용자가 한 문장만 읽고도 매수/매도 의사결정 가능하도록.
"""

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-lite:generateContent"


def analyze(symbol: str, snapshot: dict, news: list[dict],
            flow: dict, profile: dict, risk_pct: float = 1.0) -> dict:
    from .analyze_rules import analyze_rules

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        log.warning("GEMINI_API_KEY 미설정 — 룰 기반 분석으로 대체")
        return analyze_rules(symbol, snapshot, news, flow, profile, risk_pct)

    payload = {
        "ticker": f"{symbol} ({profile.get('name','')})",
        "user_risk_tolerance": f"{risk_pct}% (손실 감수 수준)",
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
        result = analyze_rules(symbol, snapshot, news, flow, profile, risk_pct)
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
        result = analyze_rules(symbol, snapshot, news, flow, profile, risk_pct)
        result["fallback_reason"] = f"parse: {e}"
        return result
