"""한국 뉴스 — 네이버 검색 API + DART 공시."""
from __future__ import annotations
import os, re, html
import httpx


async def fetch_news_kr(symbol: str, name: str = "", limit: int = 8) -> list[dict]:
    cid = os.environ.get("NAVER_CLIENT_ID")
    csec = os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not csec:
        return []
    query = name or symbol
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(
            "https://openapi.naver.com/v1/search/news.json",
            params={"query": query, "display": limit, "sort": "date"},
            headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec},
        )
        if r.status_code != 200:
            return []
        items = r.json().get("items", [])
    out = []
    for n in items:
        out.append({
            "headline": _strip(n.get("title", "")),
            "summary": _strip(n.get("description", ""))[:300],
            "source": _origin(n.get("originallink") or n.get("link", "")),
            "url": n.get("originallink") or n.get("link", ""),
            "ts": n.get("pubDate", ""),
        })
    return out


async def fetch_dart_recent(symbol: str, days: int = 7) -> list[dict]:
    """DART 최근 공시 (실적/주요사항/지분변동 등)."""
    key = os.environ.get("DART_API_KEY")
    if not key:
        return []
    from datetime import datetime, timedelta
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    async with httpx.AsyncClient(timeout=10) as c:
        # DART는 종목코드 → corp_code 매핑이 필요. 실전에선 corp_code 캐시 권장.
        r = await c.get("https://opendart.fss.or.kr/api/list.json", params={
            "crtfc_key": key, "stock_code": symbol,
            "bgn_de": start, "end_de": end, "page_count": 20,
        })
    if r.status_code != 200:
        return []
    return [{"report": x.get("report_nm"), "date": x.get("rcept_dt"),
             "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={x.get('rcept_no')}"}
            for x in (r.json().get("list") or [])]


_kr_name_cache: dict[str, dict] = {}


async def fetch_profile_kr(symbol: str) -> dict:
    """한국주식 프로필 — Naver 검색으로 실제 회사명·거래소 획득.

    캐시: 회사명은 자주 바뀌지 않으므로 프로세스 메모리에 영구 캐시.
    """
    if symbol in _kr_name_cache:
        return _kr_name_cache[symbol]

    # Naver 자동완성으로 종목 코드 → 회사명
    try:
        from .search import _search_kr
        results = await _search_kr(symbol)
        for r in results:
            if r.get("symbol") == symbol:
                profile = {
                    "name": r.get("name") or symbol,
                    "country": "KR",
                    "exchange": r.get("exchange", "KRX"),
                    # AI 프롬프트가 finnhubIndustry/marketCap 키를 보므로 호환 매핑
                    "finnhubIndustry": "한국 상장기업",
                    "marketCapitalization": None,
                }
                _kr_name_cache[symbol] = profile
                return profile
    except Exception:
        pass

    # 폴백
    return {"name": symbol, "country": "KR", "exchange": "KRX",
            "finnhubIndustry": "한국 상장기업"}


def _strip(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _origin(url: str) -> str:
    m = re.match(r"https?://([^/]+)/?", url or "")
    return m.group(1) if m else ""
