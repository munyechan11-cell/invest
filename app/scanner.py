"""스마트 종목 스캐너 — 사전 정의된 인기 40종목 자동 분석 → TOP 후보 발굴.

룰 엔진만 사용 (AI 호출 X — 비용/속도 최적화).
모든 시세 소스 폴백 (KIS → Yahoo, Finnhub) 자동 적용.
"""
from __future__ import annotations
import asyncio
import logging

log = logging.getLogger("scanner")

# 한국 인기 20종목 (KOSPI 시총 기준 + 단기 인기)
POPULAR_KR = [
    ("005930", "삼성전자"), ("000660", "SK하이닉스"), ("373220", "LG에너지솔루션"),
    ("207940", "삼성바이오로직스"), ("005380", "현대차"), ("005490", "POSCO홀딩스"),
    ("035420", "NAVER"), ("035720", "카카오"), ("051910", "LG화학"),
    ("006400", "삼성SDI"), ("068270", "셀트리온"), ("105560", "KB금융"),
    ("055550", "신한지주"), ("066570", "LG전자"), ("000270", "기아"),
    ("012330", "현대모비스"), ("009150", "삼성전기"), ("032830", "삼성생명"),
    ("003550", "LG"), ("017670", "SK텔레콤"),
]

# 미국 인기 20종목 (S&P 500 대형주 + 단타 핫)
POPULAR_US = [
    ("AAPL", "Apple"), ("MSFT", "Microsoft"), ("GOOGL", "Alphabet"),
    ("AMZN", "Amazon"), ("META", "Meta"), ("TSLA", "Tesla"),
    ("NVDA", "NVIDIA"), ("AMD", "AMD"), ("NFLX", "Netflix"),
    ("JPM", "JPMorgan"), ("V", "Visa"), ("MA", "Mastercard"),
    ("JNJ", "Johnson & Johnson"), ("UNH", "UnitedHealth"),
    ("WMT", "Walmart"), ("DIS", "Disney"), ("KO", "Coca-Cola"),
    ("ORCL", "Oracle"), ("ADBE", "Adobe"), ("CRM", "Salesforce"),
]


async def scan_symbol(symbol: str, name: str, exchange: str = None) -> dict | None:
    """단일 종목 빠른 평가 — 룰 엔진 + TOSS Score만."""
    from .market import get_snapshot
    from .analyze_rules import analyze_rules
    from .intelligence import compute_toss_score
    try:
        snap = await asyncio.to_thread(get_snapshot, symbol)
        ana = analyze_rules(symbol, snap, [], {}, {}, 1.0)
        score = compute_toss_score(snap, ana)
        q = snap.get("quote") or {}
        ind = snap.get("indicators") or {}
        return {
            "symbol": symbol,
            "name": name,
            "market": "KR" if symbol.isdigit() else "US",
            "exchange": exchange or ("KRX" if symbol.isdigit() else "NASDAQ/NYSE"),
            "price": q.get("price", 0),
            "change_pct": q.get("change_pct", 0),
            "volume": q.get("today_volume", 0),
            "rv": q.get("relative_volume", 0),
            "rsi": ind.get("rsi14", 50),
            "position": ana.get("position", "관망"),
            "position_emoji": ana.get("position_emoji", "⚪"),
            "toss_score": score["score"],
            "grade": score["grade"],
            "label": score["label"],
        }
    except Exception as e:
        log.warning(f"scan {symbol} fail: {e}")
        return None


async def scan_universe(market: str = "BOTH", limit: int = 5,
                        min_score: float = 55,
                        kr_source: str = "volume") -> list[dict]:
    """전체 인기 종목 스캔 → TOSS Score 내림차순 TOP N.

    Args:
        market: KR / US / BOTH
        limit: 반환 개수
        min_score: TOSS Score 임계 (미달 시에도 폴백으로 상위 N개)
        kr_source: "volume" = 실시간 거래량 TOP20+20 (네이버) / "popular" = 정적 시총
    """
    universe: list[tuple[str, str, str]] = []  # (symbol, name, exchange)

    if market in ("KR", "BOTH"):
        if kr_source == "popular":
            universe += [(s, n, "KOSPI") for s, n in POPULAR_KR]
        else:
            # 다중 랭킹 지원: volume / value / rise / fall / foreign
            try:
                from .volume_rank import get_kr_universe_by_ranking
                kr_uni = await get_kr_universe_by_ranking(kr_source, per_market=20)
                if kr_uni:
                    universe += kr_uni
                else:
                    universe += [(s, n, "KOSPI") for s, n in POPULAR_KR]
            except Exception as e:
                log.warning(f"ranking {kr_source} fail, fall back: {e}")
                universe += [(s, n, "KOSPI") for s, n in POPULAR_KR]

    if market in ("US", "BOTH"):
        universe += [(s, n, "NASDAQ/NYSE") for s, n in POPULAR_US]

    # 동시 실행 5개 — Rate limit 보호
    sem = asyncio.Semaphore(5)

    async def _with_sem(s, n, ex):
        async with sem:
            return await scan_symbol(s, n, ex)

    results = await asyncio.gather(
        *[_with_sem(s, n, ex) for s, n, ex in universe],
        return_exceptions=False,
    )
    valid = [r for r in results if r]
    valid.sort(key=lambda x: x["toss_score"], reverse=True)
    # 점수 임계 + 상위 N개
    qualified = [r for r in valid if r["toss_score"] >= min_score][:limit]
    return qualified or valid[:limit]  # 임계 미달이면 그래도 상위 N개


# ── 스캔 결과 캐시 (시장별 5분) ─────────────────────────────
_cache: dict[str, tuple[float, list]] = {}


async def get_top_picks(force: bool = False, market: str = "BOTH",
                        limit: int = 5, kr_ranking: str = "volume") -> list[dict]:
    import time as _t
    key = f"{market}_{limit}_{kr_ranking}"
    if not force and key in _cache:
        ts, val = _cache[key]
        if _t.time() - ts < 300:
            return val
    picks = await scan_universe(market=market, limit=limit, kr_source=kr_ranking)
    _cache[key] = (_t.time(), picks)
    return picks
