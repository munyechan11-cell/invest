"""규칙 기반 결정론적 분석기 — AI 실패 시 폴백.

월가의 표준 기술적 분석(RSI, MACD, 볼린저, MA, VWAP, RV, 외국인 수급)을
가중 스코어로 종합해 같은 JSON 스키마로 반환한다.
AI보다 단순하지만 항상 동작하고, 룰이 명확해 검증 가능하다.
"""
from __future__ import annotations


def analyze_rules(symbol: str, snapshot: dict, news: list[dict],
                  flow: dict, profile: dict) -> dict:
    is_kr = symbol.isdigit() and len(symbol) == 6
    ind = snapshot.get("indicators") or {}
    q = snapshot.get("quote") or {}
    price = float(q.get("price") or 0)
    if price <= 0:
        return _neutral(symbol, price, is_kr)

    rsi = float(ind.get("rsi14") or 50)
    macd_hist = float(ind.get("macd_hist") or 0)
    above_vwap = bool(ind.get("above_vwap"))
    bb_up = float(ind.get("bb_upper") or price * 1.05)
    bb_dn = float(ind.get("bb_lower") or price * 0.95)
    ma20 = float(ind.get("ma20") or price)
    rv = float(q.get("relative_volume") or 1.0)

    score = 0.0
    triggers: list[str] = []

    # ── RSI (Wilder, 1978)
    if rsi <= 30:
        score += 25; triggers.append(f"RSI {rsi:.0f} 과매도")
    elif rsi <= 40:
        score += 10
    elif rsi >= 70:
        score -= 25; triggers.append(f"RSI {rsi:.0f} 과매수")
    elif rsi >= 60:
        score -= 10

    # ── MACD 히스토그램
    if macd_hist > 0:
        score += 15; triggers.append("MACD 골든")
    elif macd_hist < 0:
        score -= 15; triggers.append("MACD 데드")

    # ── 추세 (Price vs MA20)
    deviation = (price - ma20) / ma20 if ma20 else 0
    if deviation > 0.02:
        score += 12; triggers.append(f"MA20 +{deviation*100:.1f}% 상회")
    elif deviation < -0.02:
        score -= 12; triggers.append(f"MA20 {deviation*100:.1f}% 하회")

    # ── 볼린저 포지션 (Bollinger, 1980s)
    bb_pos = (price - bb_dn) / (bb_up - bb_dn) if bb_up > bb_dn else 0.5
    if bb_pos < 0.10:
        score += 20; triggers.append("BB 하단 근접 평균회귀")
    elif bb_pos < 0.30:
        score += 8
    elif bb_pos > 0.90:
        score -= 20; triggers.append("BB 상단 근접 과열")
    elif bb_pos > 0.70:
        score -= 8

    # ── VWAP (기관 평균단가 시그널)
    if above_vwap:
        score += 5
    else:
        score -= 5

    # ── 한국시장 외국인/기관 수급 (KRX 공식)
    flow_kr = snapshot.get("flow_kr") or {}
    foreign = int(flow_kr.get("foreign_net_qty") or 0)
    inst = int(flow_kr.get("institutional_net_qty") or 0)
    if foreign > 0:
        score += 15; triggers.append(f"외국인 순매수 +{foreign:,}")
    elif foreign < 0:
        score -= 12; triggers.append(f"외국인 순매도 {foreign:,}")
    if inst > 0:
        score += 8
    elif inst < 0:
        score -= 6

    # ── 거래량 강도 (RV 1.5x↑면 시그널 증폭)
    if rv >= 2.0:
        score *= 1.4; triggers.append(f"거래량 폭발 {rv:.1f}x")
    elif rv >= 1.5:
        score *= 1.2; triggers.append(f"거래량 급증 {rv:.1f}x")
    elif rv < 0.5:
        score *= 0.7  # 거래 빈약 → 시그널 신뢰도 ↓

    # ── 점수 → 포지션 매핑 (단기 투자자용 공격적 튜닝)
    if score >= 35:
        position, emoji = "적극 매수", "🟢"
    elif score >= 10:
        position, emoji = "분할 매수", "🟢"
    elif score <= -35:
        position, emoji = "적극 매도", "🔴"
    elif score <= -10:
        position, emoji = "분할 매도", "🟠"
    else:
        position, emoji = "관망", "⚪"

    # ── 타겟/스탑 — BB 폭 기반 변동성 정규화
    rnd = (lambda v: int(round(v))) if is_kr else (lambda v: round(v, 2))
    bb_half = max((bb_up - bb_dn) / 2, price * 0.005)

    if position in ("적극 매수", "분할 매수"):
        target = rnd(price + bb_half * 0.8)
        stop = rnd(max(price - bb_half * 0.5, bb_dn))
        sr_label = "손절가"
    elif position in ("적극 매도", "분할 매도"):
        target = rnd(price - bb_half * 0.8)
        stop = rnd(min(price + bb_half * 0.5, bb_up))
        sr_label = "재진입가"
    else:
        target = rnd(bb_up * 0.99)
        stop = rnd(bb_dn * 1.01)
        sr_label = "재진입가"

    risk = abs(price - stop)
    reward = abs(target - price)
    r_mult = f"1:{reward/risk:.1f}" if risk > 0.0001 else "1:1.0"

    # ── 보유기간 추정
    if abs(score) >= 45 and rv >= 1.5:
        horizon, hreason = "초단타", "강한 모멘텀 + 거래량 급증으로 1~2시간 내 결판"
    elif abs(score) >= 18:
        horizon, hreason = "단기", "기술적 시그널이 1~3일 안에 소화될 구간"
    else:
        horizon, hreason = "스윙", "방향성 약해 1주 추세 확인 권장"

    # ── 수급 라벨 (한국은 실제 데이터, 미국은 RV로 추론)
    if is_kr:
        flow_inst = ("기관 우위" if foreign + inst > 0
                     else "개인 우위" if foreign + inst < 0 else "중립")
        flow_inst_reason = f"외국인 {foreign:+,}주, 기관 {inst:+,}주 순매수 (KRX 공식)"
    else:
        flow_inst = ("기관 우위" if rv >= 1.5 and above_vwap
                     else "개인 우위" if rv >= 1.5 and not above_vwap
                     else "중립")
        flow_inst_reason = f"RV {rv:.2f}x · VWAP {'상회' if above_vwap else '하회'} 기반 추정"

    headlines = [n.get("headline", "")[:50] for n in (news or [])[:2] if n.get("headline")]
    market_ctx = (f"최신 헤드라인: {' / '.join(headlines)}. 기술적 신호 우선 판단."
                  if headlines else
                  "뉴스 데이터 부족 — 순수 기술 분석 기반. 24h 내 변동성 주의.")

    confidence = min(95, max(20, int(40 + abs(score) * 0.6)))

    return {
        "position": position,
        "position_emoji": emoji,
        "rationale": (f"기술지표 종합 점수 {score:+.0f}. "
                      f"{', '.join(triggers[:3]) if triggers else '뚜렷한 시그널 부재'}. "
                      f"BB 위치 {bb_pos:.0%}, RSI {rsi:.0f}."),
        "frameworks_triggered": triggers[:6] or ["기술적 중립"],
        "target_price": target,
        "reentry_or_stop_label": sr_label,
        "reentry_or_stop_price": stop,
        "r_multiple": r_mult,
        "holding_period": horizon,
        "holding_period_reason": hreason,
        "flow_institutional": flow_inst,
        "flow_institutional_reason": flow_inst_reason,
        "flow_special": (f"거래량 {rv:.2f}x · BB 위치 {bb_pos:.0%}"
                         if rv >= 1.3 else "특이사항 없음"),
        "macro_regime": "AI 분석 미사용 — 기술지표 단독 평가",
        "market_context": market_ctx,
        "confidence": confidence,
        "engine": "rules",   # ← 어떤 엔진이 분석했는지 표시
    }


def _neutral(symbol: str, price: float, is_kr: bool) -> dict:
    rnd = (lambda v: int(round(v))) if is_kr else (lambda v: round(v, 2))
    return {
        "position": "관망", "position_emoji": "⚪",
        "rationale": "시세 데이터 부족 — 분석 불가. 데이터 복구 후 재시도.",
        "frameworks_triggered": ["데이터 부족"],
        "target_price": rnd(price * 1.02),
        "reentry_or_stop_label": "재진입가",
        "reentry_or_stop_price": rnd(price * 0.98),
        "r_multiple": "1:1.0",
        "holding_period": "스윙", "holding_period_reason": "데이터 안정 후 재평가",
        "flow_institutional": "중립", "flow_institutional_reason": "데이터 없음",
        "flow_special": "특이사항 없음",
        "macro_regime": "데이터 부족",
        "market_context": "시세 또는 지표 데이터를 받지 못해 판단 불가.",
        "confidence": 10,
        "engine": "fallback_empty",
    }
