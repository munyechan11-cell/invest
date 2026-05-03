"""고차 분석 — 경쟁사 AI 플랫폼 핵심 기능 통합.

1. TOSS Score (0-100) — Danelfin/Tickeron 스타일 단일 종합 점수
2. Move Explainer — MarketReader 스타일 "왜 움직였나" 자동 설명
3. Multi-Timeframe Consensus — Tickeron의 1H/1D/1W 시그널 일치도
4. Chart Pattern Detection — Trade Ideas 스타일 패턴 자동 인식
5. Earnings Calendar (Finnhub) — Yahoo/Bloomberg 스타일 D-day
6. Sector Relative Strength — Bloomberg 스타일 벤치마크 대비 강세
"""
from __future__ import annotations
import logging
log = logging.getLogger("intel")


# ───────────────────────────────────────────────────────────────────
# 1. TOSS SCORE (0-100 종합 점수)
# ───────────────────────────────────────────────────────────────────
def compute_toss_score(snap: dict, ana: dict) -> dict:
    """5차원 합산 — 기술 / 모멘텀 / 수급 / 뉴스 / 신뢰도.

    각 차원의 부호 있는 raw score를 -50~+50 범위로 합산 → 50 중심 정규화.
    """
    ind = snap.get("indicators") or {}
    q = snap.get("quote") or {}
    rsi = float(ind.get("rsi14") or 50)
    macd_hist = float(ind.get("macd_hist") or 0)
    rv = float(q.get("relative_volume") or 1.0)
    above_vwap = bool(ind.get("above_vwap"))
    change_pct = float(q.get("change_pct") or 0)
    price = float(q.get("price") or 0)
    ma20 = float(ind.get("ma20") or price)
    bb_up = float(ind.get("bb_upper") or price * 1.05)
    bb_dn = float(ind.get("bb_lower") or price * 0.95)

    # 기술 (-30 ~ +30)
    tech = 0.0
    if rsi <= 30: tech += 15
    elif rsi >= 70: tech -= 15
    elif 40 <= rsi <= 60: tech += 4

    if macd_hist > 0: tech += 8
    elif macd_hist < 0: tech -= 8

    if bb_up > bb_dn:
        bb_pos = (price - bb_dn) / (bb_up - bb_dn)
        if bb_pos < 0.20: tech += 7
        elif bb_pos > 0.80: tech -= 7

    if price > ma20 * 1.005: tech += 5
    elif price < ma20 * 0.995: tech -= 5

    # 모멘텀 (-25 ~ +25)
    mom = 0.0
    if change_pct >= 2: mom += 10
    elif change_pct >= 1: mom += 5
    elif change_pct <= -2: mom -= 10
    elif change_pct <= -1: mom -= 5

    if rv >= 2.0: mom += 10
    elif rv >= 1.5: mom += 5
    elif rv < 0.5: mom -= 4

    if above_vwap: mom += 5
    else: mom -= 5

    # 수급 (-20 ~ +20)
    flow = 0.0
    ft = ana.get("flow_table")
    if ft:
        smart_net = ft.get("smart_net", 0) or 0
        retail_net = ft.get("retail_only_net", 0) or 0
        flow += max(-15, min(15, smart_net / 100000))
        if smart_net < 0 and retail_net > 0:
            flow -= 5  # 분배 패턴
    else:
        if rv >= 1.5 and above_vwap: flow += 10
        elif rv >= 1.5 and not above_vwap: flow -= 10

    # 뉴스 (-15 ~ +15)
    news_s = 0.0
    pos = len(ana.get("news_positive") or [])
    neg = len(ana.get("news_negative") or [])
    news_s += min(pos * 4, 12) - min(neg * 4, 12)

    # 신뢰도 (0 ~ +10)
    conf = 5
    if pos + neg >= 3: conf += 2
    if rv > 0.8: conf += 2
    if 30 < rsi < 70: conf += 1

    raw = tech + mom + flow + news_s
    score = max(0, min(100, raw + 50))

    if score >= 80: grade, label, color = "A+", "강력 매수", "🟢"
    elif score >= 70: grade, label, color = "A", "매수", "🟢"
    elif score >= 60: grade, label, color = "B+", "분할 매수", "🟢"
    elif score >= 45: grade, label, color = "B", "관망", "🟡"
    elif score >= 35: grade, label, color = "C", "분할 매도", "🟠"
    elif score >= 25: grade, label, color = "D", "매도", "🔴"
    else: grade, label, color = "F", "강력 매도", "🔴"

    return {
        "score": round(score, 1),
        "grade": grade,
        "label": label,
        "color": color,
        "breakdown": {
            "기술 분석": round(tech, 1),
            "단기 모멘텀": round(mom, 1),
            "수급 흐름": round(flow, 1),
            "뉴스 센티먼트": round(news_s, 1),
            "데이터 신뢰도": conf,
        },
    }


# ───────────────────────────────────────────────────────────────────
# 2. MOVE EXPLAINER — 왜 움직였나
# ───────────────────────────────────────────────────────────────────
def explain_move(snap: dict, news: list[dict], ana: dict) -> str:
    q = snap.get("quote") or {}
    ind = snap.get("indicators") or {}
    change = float(q.get("change_pct") or 0)
    rv = float(q.get("relative_volume") or 1.0)
    rsi = float(ind.get("rsi14") or 50)

    direction = "상승" if change > 0.1 else "하락" if change < -0.1 else "보합"
    sign = "+" if change > 0 else ""
    head = f"{sign}{change:.2f}% {direction}"

    reasons = []
    pos_news = ana.get("news_positive") or []
    neg_news = ana.get("news_negative") or []

    # 뉴스 우위 (방향 일치)
    if change > 0.5 and pos_news:
        reasons.append(f"{pos_news[0][:35]} 호재")
    elif change < -0.5 and neg_news:
        reasons.append(f"{neg_news[0][:35]} 악재")

    # 거래량 시그널
    if rv >= 2.0:
        reasons.append(f"거래량 {rv:.1f}x 폭발")
    elif rv >= 1.5:
        reasons.append(f"거래량 {rv:.1f}x 급증")

    # 수급 (한국주식 우선)
    ft = ana.get("flow_table")
    if ft:
        sn = ft.get("smart_net", 0) or 0
        if abs(sn) >= 100000:
            reasons.append(
                f"외국인+기관 +{sn:,}주 매집" if sn > 0 else f"외국인+기관 {sn:,}주 매도"
            )

    # RSI 극단
    if rsi <= 30:
        reasons.append("RSI 과매도 반등 자리")
    elif rsi >= 70:
        reasons.append("RSI 과매수 차익 매물")

    if not reasons:
        reasons.append("뚜렷한 단기 재료 부재 — 기술적 흐름")

    return f"{head} | " + " · ".join(reasons[:3])


# ───────────────────────────────────────────────────────────────────
# 3. MULTI-TIMEFRAME CONSENSUS (1H / 1D / 1W)
# ───────────────────────────────────────────────────────────────────
def _tf_signal(rsi: float, macd_hist: float, price: float, ma: float) -> tuple[str, float]:
    score = 0
    if rsi <= 30: score += 30
    elif rsi <= 40: score += 12
    elif rsi >= 70: score -= 30
    elif rsi >= 60: score -= 12
    if macd_hist > 0: score += 20
    elif macd_hist < 0: score -= 20
    if ma and price > ma * 1.01: score += 15
    elif ma and price < ma * 0.99: score -= 15
    if score >= 25: return "BUY", min(abs(score), 100)
    if score <= -25: return "SELL", min(abs(score), 100)
    return "HOLD", abs(score)


def compute_multi_tf(snap: dict) -> dict:
    ind = snap.get("indicators") or {}
    q = snap.get("quote") or {}
    closes = snap.get("recent_closes") or []
    price = float(q.get("price") or 0)
    if price <= 0:
        return {"h1": {"signal": "HOLD", "strength": 0},
                "d1": {"signal": "HOLD", "strength": 0},
                "w1": {"signal": "HOLD", "strength": 0},
                "verdict": "데이터 부족", "signal": "MIXED",
                "buy_count": 0, "sell_count": 0}

    rsi = float(ind.get("rsi14") or 50)
    macd_hist = float(ind.get("macd_hist") or 0)
    ma20 = float(ind.get("ma20") or price)

    # 1D — 직접 계산
    sig_d, str_d = _tf_signal(rsi, macd_hist, price, ma20)

    # 1W — 5일 단위 모멘텀 근사
    if len(closes) >= 10:
        rets = [(closes[i] / closes[max(0, i - 5)] - 1) for i in range(5, len(closes))]
        avg = sum(rets) / max(len(rets), 1)
        wkly_rsi = max(0, min(100, 50 + avg * 500))
        wkly_macd = closes[-1] / closes[-min(5, len(closes))] - 1
        wkly_ma = sum(closes[-5:]) / 5
        sig_w, str_w = _tf_signal(wkly_rsi, wkly_macd, price, wkly_ma)
    else:
        sig_w, str_w = "HOLD", 0

    # 1H — 일중 변동률 + RSI
    change = float(q.get("change_pct") or 0)
    if change > 1.5 and rsi < 65:
        sig_h, str_h = "BUY", min(abs(change) * 15, 80)
    elif change < -1.5 and rsi > 35:
        sig_h, str_h = "SELL", min(abs(change) * 15, 80)
    else:
        sig_h, str_h = sig_d, str_d * 0.6

    sigs = [sig_h, sig_d, sig_w]
    buy = sigs.count("BUY")
    sell = sigs.count("SELL")

    if buy == 3:
        verdict, signal = "전 시간대 매수 일치 — 강한 매수", "STRONG_BUY"
    elif sell == 3:
        verdict, signal = "전 시간대 매도 일치 — 강한 매도", "STRONG_SELL"
    elif buy >= 2:
        verdict, signal = f"단기 매수 우위 ({buy}/3)", "BUY"
    elif sell >= 2:
        verdict, signal = f"단기 매도 우위 ({sell}/3)", "SELL"
    else:
        verdict, signal = "시간대별 시그널 충돌 — 관망", "MIXED"

    return {
        "h1": {"signal": sig_h, "strength": round(str_h)},
        "d1": {"signal": sig_d, "strength": round(str_d)},
        "w1": {"signal": sig_w, "strength": round(str_w)},
        "verdict": verdict,
        "signal": signal,
        "buy_count": buy,
        "sell_count": sell,
    }


# ───────────────────────────────────────────────────────────────────
# 4. CHART PATTERN DETECTION
# ───────────────────────────────────────────────────────────────────
def detect_patterns(snap: dict) -> list[dict]:
    ind = snap.get("indicators") or {}
    q = snap.get("quote") or {}
    closes = snap.get("recent_closes") or []
    price = float(q.get("price") or 0)
    rv = float(q.get("relative_volume") or 1.0)
    change = float(q.get("change_pct") or 0)
    if price <= 0:
        return []

    patterns: list[dict] = []
    ma20 = float(ind.get("ma20") or price)
    bb_up = float(ind.get("bb_upper") or price * 1.05)
    bb_dn = float(ind.get("bb_lower") or price * 0.95)

    # 골든/데드 크로스 (단기·장기 평균 비교)
    if len(closes) >= 10:
        early = sum(closes[:5]) / 5
        recent = sum(closes[-5:]) / 5
        if recent > early * 1.025 and price > ma20:
            patterns.append({
                "name": "골든크로스 형성", "type": "bullish",
                "desc": "단기 평균이 장기 평균을 상향 돌파 — 추세 전환 매수 시그널",
            })
        elif recent < early * 0.975 and price < ma20:
            patterns.append({
                "name": "데드크로스 형성", "type": "bearish",
                "desc": "단기 평균이 장기 평균을 하향 돌파 — 추세 약화 신호",
            })

    # 볼린저 돌파
    if bb_up > bb_dn:
        if price > bb_up * 0.995:
            patterns.append({
                "name": "볼린저 상단 돌파", "type": "neutral",
                "desc": "강한 상승 모멘텀이지만 단기 과열 — 추가 상승 vs 조정 분기점",
            })
        elif price < bb_dn * 1.005:
            patterns.append({
                "name": "볼린저 하단 터치", "type": "bullish",
                "desc": "단기 과매도 — 평균회귀 매수 자리 검토",
            })

    # 거래량 동반 돌파 (가장 신뢰도 높음)
    if rv >= 2.0 and change > 1.5:
        patterns.append({
            "name": "거래량 동반 돌파", "type": "bullish",
            "desc": f"RV {rv:.1f}x + {change:+.1f}% — 신규 매수세 강한 유입",
        })
    elif rv >= 2.0 and change < -1.5:
        patterns.append({
            "name": "거래량 동반 급락", "type": "bearish",
            "desc": f"RV {rv:.1f}x + {change:+.1f}% — 패닉셀 / 분배 의심",
        })

    # 이중 바닥 (W 패턴)
    if len(closes) >= 8:
        first_half_low = min(closes[: len(closes) // 2])
        second_half_low = min(closes[len(closes) // 2 :])
        diff = abs(first_half_low - second_half_low) / max(first_half_low, 1)
        if diff < 0.03 and price > second_half_low * 1.03:
            patterns.append({
                "name": "이중 바닥 (W) 형성", "type": "bullish",
                "desc": "두 저점 근접 후 반등 — 강한 지지선 형성, 매수 자리",
            })

    # RSI 다이버전스 근사 (최근 가격 신고점 vs RSI)
    if len(closes) >= 10:
        if price >= max(closes) * 0.99 and float(ind.get("rsi14") or 50) < 60:
            patterns.append({
                "name": "약세 다이버전스 의심", "type": "bearish",
                "desc": "가격은 신고점 근접하나 RSI는 약화 — 추세 둔화 경고",
            })

    return patterns


# ───────────────────────────────────────────────────────────────────
# 6. SECTOR RELATIVE STRENGTH (벤치마크 대비)
# ───────────────────────────────────────────────────────────────────
def compute_relative_strength(snap: dict, benchmark_snap: dict, benchmark_name: str) -> dict:
    """타겟 종목의 1일·5일 변동률 vs 벤치마크 비교."""
    q = snap.get("quote") or {}
    bq = benchmark_snap.get("quote") or {}
    target_1d = float(q.get("change_pct") or 0)
    bench_1d = float(bq.get("change_pct") or 0)
    diff_1d = target_1d - bench_1d

    target_closes = snap.get("recent_closes") or []
    bench_closes = benchmark_snap.get("recent_closes") or []
    if len(target_closes) >= 5 and len(bench_closes) >= 5:
        t5 = (target_closes[-1] / target_closes[-5] - 1) * 100
        b5 = (bench_closes[-1] / bench_closes[-5] - 1) * 100
        diff_5d = t5 - b5
    else:
        diff_5d = 0
        t5 = b5 = 0

    if diff_1d >= 1.5:
        label, emoji = "강한 상대 우위", "🟢"
    elif diff_1d >= 0.3:
        label, emoji = "약한 상대 우위", "🟢"
    elif diff_1d <= -1.5:
        label, emoji = "강한 상대 약세", "🔴"
    elif diff_1d <= -0.3:
        label, emoji = "약한 상대 약세", "🟠"
    else:
        label, emoji = "벤치마크 동조", "⚪"

    return {
        "benchmark_symbol": benchmark_name,
        "label": label,
        "emoji": emoji,
        "target_1d_pct": round(target_1d, 2),
        "bench_1d_pct": round(bench_1d, 2),
        "vs_1d_pct": round(diff_1d, 2),
        "target_5d_pct": round(t5, 2),
        "bench_5d_pct": round(b5, 2),
        "vs_5d_pct": round(diff_5d, 2),
    }


def get_benchmark_symbol(symbol: str) -> tuple[str, str]:
    """심볼 → 벤치마크 (티커, 표시명)."""
    if symbol.isdigit() and len(symbol) == 6:
        return ("069500", "KOSPI 200")  # KODEX 200
    return ("SPY", "S&P 500")
