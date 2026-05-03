"""한국 주식 거래량 순위 — 네이버 금융 우선, KIS API 폴백.

매일 갱신되는 실시간 거래량 TOP — 정적 종목 리스트보다 훨씬 더 시의적절.
"""
from __future__ import annotations
import asyncio
import logging
import re
import time
import os
import httpx

log = logging.getLogger("volume_rank")

_cache: dict[str, tuple[float, list]] = {}
_TTL = 600  # 10분 캐시


_ETF_ETN_KEYWORDS = (
    "KODEX", "TIGER", "ARIRANG", "HANARO", "KBSTAR", "ACE", "PLUS",
    "KOSEF", "WOORI", "SOL", "NH-Amundi", "SMART", "ETN", "ETF",
    "인버스", "레버리지", "선물", "원유", "곱버스",
)


def _is_regular_stock(name: str) -> bool:
    """ETF/ETN/ELS 등 파생상품 제외, 일반 종목만 통과."""
    if not name:
        return False
    upper = name.upper()
    return not any(kw.upper() in upper for kw in _ETF_ETN_KEYWORDS)


async def _scrape_naver_volume(market: str, limit: int = 30,
                               exclude_etf: bool = True) -> list[dict]:
    """네이버 금융 거래량 페이지 스크래핑 (가장 안정적, 키 불필요).

    exclude_etf=True: KODEX/TIGER/ETN/인버스 등 파생상품 제외 (일반 종목만)
    """
    sosok = "0" if market == "KOSPI" else "1"  # 0=코스피, 1=코스닥
    # 페이지당 50개 → 필터링 후에도 충분히 남도록
    url = f"https://finance.naver.com/sise/sise_quant.naver?sosok={sosok}&page=1"
    try:
        async with httpx.AsyncClient(
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"},
        ) as c:
            r = await c.get(url)
            if r.status_code != 200:
                return []
            # 네이버 finance는 EUC-KR
            try:
                html = r.content.decode("euc-kr")
            except UnicodeDecodeError:
                html = r.text
    except Exception as e:
        log.warning(f"naver scrape fail {market}: {e}")
        return []

    # 종목 행 패턴: <a href="/item/main.naver?code=005930" ...>삼성전자</a>
    pattern = r'/item/main\.naver\?code=(\d{6})[^>]*>([^<]+?)</a>'
    matches = re.findall(pattern, html)

    seen = set()
    out = []
    for code, name in matches:
        if code in seen:
            continue
        seen.add(code)
        nm = name.strip()
        # ETF/ETN/파생상품 필터
        if exclude_etf and not _is_regular_stock(nm):
            continue
        out.append({
            "symbol": code,
            "name": nm,
            "exchange": market,
        })
        if len(out) >= limit:
            break
    return out


async def _kis_volume_rank(market: str, limit: int = 30) -> list[dict]:
    """KIS volume rank API (키가 있으면 더 정확)."""
    if not os.environ.get("KIS_APP_KEY"):
        return []
    try:
        from app.market_kr import _base, _headers
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0001" if market == "KOSPI" else "1001",
            "FID_DIV_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": "1",  # 거래량
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "0000000000",
            "FID_INPUT_PRICE_1": "",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_INPUT_DATE_1": "",
        }
        # KIS 거래량 순위 TR_ID
        with httpx.Client(timeout=10) as c:
            r = c.get(
                f"{_base()}/uapi/domestic-stock/v1/quotations/volume-rank",
                params=params, headers=_headers("FHPST01710000"),
            )
        if r.status_code != 200:
            return []
        d = r.json()
        if d.get("rt_cd") != "0":
            return []
        rows = d.get("output") or []
        out = []
        for row in rows[:limit]:
            code = row.get("mksc_shrn_iscd")
            name = row.get("hts_kor_isnm")
            if code and name:
                out.append({"symbol": code, "name": name.strip(), "exchange": market})
        return out
    except Exception as e:
        log.warning(f"kis volume rank fail: {e}")
        return []


async def get_top_volume_kr(limit_per_market: int = 20) -> dict:
    """KOSPI/KOSDAQ 거래량 상위 종목.

    Returns: {"kospi": [...], "kosdaq": [...], "source": "naver"|"kis"|"hybrid"}
    """
    cache_key = f"vol_kr_{limit_per_market}"
    now = time.time()
    if cache_key in _cache:
        ts, data = _cache[cache_key]
        if now - ts < _TTL:
            return data

    # KIS 우선 시도, 실패 시 Naver
    kospi, kosdaq = await asyncio.gather(
        _kis_volume_rank("KOSPI", limit_per_market),
        _kis_volume_rank("KOSDAQ", limit_per_market),
    )
    source = "kis"

    if not kospi:
        kospi = await _scrape_naver_volume("KOSPI", limit_per_market)
        source = "naver" if not kosdaq else "hybrid"
    if not kosdaq:
        kosdaq = await _scrape_naver_volume("KOSDAQ", limit_per_market)
        source = "naver" if source != "kis" else "hybrid"

    result = {"kospi": kospi, "kosdaq": kosdaq, "source": source,
              "fetched_at": now}
    _cache[cache_key] = (now, result)
    return result


async def get_kr_universe(per_market: int = 20) -> list[tuple[str, str, str]]:
    """KR 거래량 TOP 종목들을 (symbol, name, exchange) 튜플 리스트로 반환."""
    data = await get_top_volume_kr(per_market)
    universe = []
    for r in data["kospi"]:
        universe.append((r["symbol"], r["name"], "KOSPI"))
    for r in data["kosdaq"]:
        universe.append((r["symbol"], r["name"], "KOSDAQ"))
    return universe
