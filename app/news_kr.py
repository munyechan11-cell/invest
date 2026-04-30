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


async def fetch_profile_kr(symbol: str) -> dict:
    """간이 프로필 — KIS 종목 마스터 호출은 별도 인증 필요. 여기선 비워두고 모델이 뉴스에서 추론."""
    return {"name": symbol, "country": "KR"}


def _strip(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _origin(url: str) -> str:
    m = re.match(r"https?://([^/]+)/?", url or "")
    return m.group(1) if m else ""
