"""종목 비교 — 2~5종목 동시 분석.

각 종목의 핵심 지표를 같은 기준으로 추출해 side-by-side 비교.
"""
from __future__ import annotations
import asyncio
import logging

log = logging.getLogger("compare")


async def _analyze_quick(symbol: str) -> dict | None:
    """단일 종목 빠른 분석 — 룰 엔진만."""
    from .market import get_snapshot
    from .analyze_rules import analyze_rules
    from .intelligence import compute_toss_score, compute_relative_strength, get_benchmark_symbol
    from .risk_analytics import grade_volatility
    try:
        snap = await asyncio.to_thread(get_snapshot, symbol)
    except Exception as e:
        log.warning(f"compare snap fail {symbol}: {e}")
        return None

    ana = analyze_rules(symbol, snap, [], {}, {}, 1.0)
    score = compute_toss_score(snap, ana)
    vol = grade_volatility(snap)

    # 벤치마크 RS (옵션)
    rs = None
    try:
        bsym, bname = get_benchmark_symbol(symbol)
        bench = await asyncio.to_thread(get_snapshot, bsym)
        rs = compute_relative_strength(snap, bench, bname)
    except Exception:
        pass

    q = snap.get("quote") or {}
    ind = snap.get("indicators") or {}
    return {
        "symbol": symbol,
        "market": "KR" if symbol.isdigit() and len(symbol) == 6 else "US",
        "price": q.get("price"),
        "change_pct": q.get("change_pct"),
        "rv": q.get("relative_volume"),
        "rsi": ind.get("rsi14"),
        "macd_hist": ind.get("macd_hist"),
        "ma20": ind.get("ma20"),
        "above_vwap": ind.get("above_vwap"),
        "position": ana.get("position"),
        "position_emoji": ana.get("position_emoji"),
        "target_price": ana.get("target_price"),
        "stop_price": ana.get("stop_price"),
        "r_multiple": ana.get("r_multiple"),
        "holding_period": ana.get("holding_period"),
        "toss_score": score["score"],
        "grade": score["grade"],
        "label": score["label"],
        "score_breakdown": score["breakdown"],
        "volatility_stars": vol["stars"],
        "volatility_label": vol["label"],
        "atr_pct": vol["atr_pct"],
        "relative_strength": rs,
    }


async def compare_symbols(symbols: list[str]) -> dict:
    """종목 비교 — 2~5개. 가장 높은 TOSS Score 우선 정렬 + 승자 표시."""
    if not symbols or len(symbols) < 2:
        return {"ok": False, "msg": "최소 2종목 이상 필요"}
    if len(symbols) > 5:
        return {"ok": False, "msg": "최대 5종목까지"}

    # 정규화 + 중복 제거
    syms = []
    seen = set()
    for s in symbols:
        u = s.strip().upper()
        if u and u not in seen:
            seen.add(u)
            syms.append(u)

    results = await asyncio.gather(
        *[_analyze_quick(s) for s in syms], return_exceptions=False
    )
    valid = [r for r in results if r]

    if not valid:
        return {"ok": False, "msg": "데이터 수집 실패"}

    # 카테고리별 승자
    winners = {}
    if valid:
        winners["best_score"] = max(valid, key=lambda x: x.get("toss_score") or 0)["symbol"]
        winners["best_change"] = max(valid, key=lambda x: x.get("change_pct") or 0)["symbol"]
        winners["best_rv"] = max(valid, key=lambda x: x.get("rv") or 0)["symbol"]
        # 가장 안정 (변동성 낮음 = atr_pct 작음)
        winners["most_stable"] = min(valid, key=lambda x: x.get("atr_pct") or 999)["symbol"]
        # RSI 가장 매수 자리 (낮을수록)
        winners["most_oversold"] = min(valid, key=lambda x: x.get("rsi") or 100)["symbol"]

    # 종합 추천 (toss_score 기준)
    sorted_results = sorted(valid, key=lambda x: x.get("toss_score") or 0, reverse=True)
    recommendation = (
        f"종합 추천: <b>{sorted_results[0]['symbol']}</b> "
        f"(TOSS {sorted_results[0]['toss_score']:.0f} {sorted_results[0]['grade']})"
    )

    return {
        "ok": True,
        "count": len(valid),
        "results": sorted_results,
        "winners": winners,
        "recommendation": recommendation,
    }
