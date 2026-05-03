"""포트폴리오 리스크 분석 + ATR 기반 변동성 등급."""
from __future__ import annotations
import logging

log = logging.getLogger("risk_analytics")


def grade_volatility(snap: dict) -> dict:
    """ATR 근사 (BB 폭 / 가격) → 1~5★ 등급.

    BB는 20일 표준편차 기반 → 변동성 좋은 프록시.
    bb_width / price = 변동률 비율.
    """
    ind = snap.get("indicators") or {}
    q = snap.get("quote") or {}
    price = float(q.get("price") or 0)
    if price <= 0:
        return {"grade": 0, "stars": "-", "label": "데이터 없음", "atr_pct": 0}

    bb_up = float(ind.get("bb_upper") or price * 1.05)
    bb_dn = float(ind.get("bb_lower") or price * 0.95)
    bb_width = bb_up - bb_dn
    atr_pct = (bb_width / price * 100) if price > 0 else 0  # BB 폭의 가격 대비 %

    # 5단계 임계
    if atr_pct < 4:
        grade, label = 1, "매우 낮음 (대형주 안정)"
    elif atr_pct < 7:
        grade, label = 2, "낮음 (안정형)"
    elif atr_pct < 12:
        grade, label = 3, "보통 (일반)"
    elif atr_pct < 20:
        grade, label = 4, "높음 (모멘텀/단기)"
    else:
        grade, label = 5, "매우 높음 (위험)"

    return {
        "grade": grade,
        "stars": "★" * grade + "☆" * (5 - grade),
        "label": label,
        "atr_pct": round(atr_pct, 2),
    }


def analyze_portfolio_risk(holdings: list[dict], ticks: dict[str, float]) -> dict:
    """포트폴리오 분산도/집중도 분석.

    holdings: [{symbol, entry_price, shares, ...}]
    ticks: {symbol: current_price}
    """
    if not holdings:
        return {"ok": False, "msg": "보유 종목 없음"}

    positions = []
    total_value = 0.0
    for h in holdings:
        sym = h["symbol"]
        sh = float(h.get("shares") or 0)
        cp = float(ticks.get(sym) or h.get("entry_price") or 0)
        if sh <= 0 or cp <= 0:
            continue
        value = sh * cp
        total_value += value
        positions.append({
            "symbol": sym,
            "value": value,
            "shares": sh,
            "current_price": cp,
            "is_kr": sym.isdigit() and len(sym) == 6,
        })

    if not positions or total_value <= 0:
        return {"ok": False, "msg": "유효한 보유 데이터 없음"}

    # 비중 계산
    for p in positions:
        p["weight_pct"] = round(p["value"] / total_value * 100, 2)
    positions.sort(key=lambda x: x["weight_pct"], reverse=True)

    # 시장 분산
    kr_value = sum(p["value"] for p in positions if p["is_kr"])
    us_value = total_value - kr_value
    market_split = {
        "kr_pct": round(kr_value / total_value * 100, 1),
        "us_pct": round(us_value / total_value * 100, 1),
    }

    # 경고 생성
    warnings = []

    # 단일 종목 집중도
    top = positions[0]
    if top["weight_pct"] >= 50:
        warnings.append({
            "level": "danger",
            "msg": f"⚠️ {top['symbol']}이 전체의 {top['weight_pct']}% — 단일 종목 50%+ 집중. 분산 권장.",
        })
    elif top["weight_pct"] >= 30:
        warnings.append({
            "level": "warn",
            "msg": f"⚠️ {top['symbol']}이 전체의 {top['weight_pct']}% — 비중 큼.",
        })

    # 종목 수
    n = len(positions)
    if n == 1:
        warnings.append({"level": "danger",
                         "msg": "⚠️ 단일 종목 보유 — 최소 3~5종목 분산 권장."})
    elif n == 2:
        warnings.append({"level": "warn",
                         "msg": "⚠️ 2종목만 보유 — 추가 분산 고려."})

    # 시장 집중
    if market_split["kr_pct"] >= 90 and n > 1:
        warnings.append({"level": "info",
                         "msg": f"🇰🇷 한국 비중 {market_split['kr_pct']}% — 미주 분산 시 환율 헷지 효과."})
    elif market_split["us_pct"] >= 90 and n > 1:
        warnings.append({"level": "info",
                         "msg": f"🇺🇸 미주 비중 {market_split['us_pct']}% — 한국 분산 시 시간대 헷지."})

    # HHI (Herfindahl-Hirschman Index) — 집중도 지표
    hhi = sum((p["weight_pct"]) ** 2 for p in positions)
    if hhi < 1500:
        diversification = "양호"
    elif hhi < 2500:
        diversification = "보통"
    elif hhi < 5000:
        diversification = "집중"
    else:
        diversification = "고집중"

    return {
        "ok": True,
        "total_value": round(total_value, 2),
        "position_count": n,
        "market_split": market_split,
        "top_3": [
            {"symbol": p["symbol"], "weight_pct": p["weight_pct"], "value": round(p["value"], 2)}
            for p in positions[:3]
        ],
        "all_positions": [
            {"symbol": p["symbol"], "weight_pct": p["weight_pct"]}
            for p in positions
        ],
        "hhi": round(hhi, 0),
        "diversification": diversification,
        "warnings": warnings,
    }
