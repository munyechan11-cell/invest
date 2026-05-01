"""티커 검색 — 한국어/영어/숫자 모두 지원, 한·미 시장 통합.

설계:
- 정적 매핑 파일 없음 (Naver Finance autocomplete + Finnhub /search 라이브 호출)
- 한글 입력 → KR 위주, 영문 입력 → 한+미 동시 (영문 한국 종목 영명 매칭 위해)
- 5분 메모리 캐시로 중복 호출 차단
"""
from __future__ import annotations
import os, time, logging, asyncio
import httpx

log = logging.getLogger("search")

_cache: dict[str, tuple[float, list]] = {}
_TTL = 300  # 5분
_MAX_CACHE = 500


def _is_korean(s: str) -> bool:
    return any("가" <= c <= "힣" for c in s)


def _cur(market: str) -> str:
    return "₩" if market == "KR" else "$"


async def search_symbols(query: str, limit: int = 10) -> list[dict]:
    """통합 검색. 결과: [{symbol, name, market, exchange, currency}]"""
    q = (query or "").strip()
    if len(q) < 1:
        return []

    key = q.lower()
    now = time.time()
    if key in _cache:
        ts, data = _cache[key]
        if now - ts < _TTL:
            return data[:limit]

    is_kr_input = _is_korean(q) or q.isdigit()

    tasks = [_search_kr(q)]
    if not _is_korean(q):  # 한글이면 미국 검색 의미 없음
        tasks.append(_search_us(q))

    results_lists = await asyncio.gather(*tasks, return_exceptions=True)

    merged: list[dict] = []
    for r in results_lists:
        if isinstance(r, list):
            merged.extend(r)

    # 한글 입력 → KR 우선 / 영문 입력 → US 우선 (정확도 향상)
    merged.sort(key=lambda x: (
        0 if (is_kr_input and x["market"] == "KR") else
        0 if (not is_kr_input and x["market"] == "US") else 1,
        len(x.get("name", "")),  # 짧은 이름 우선
    ))

    # 중복 제거
    seen = set()
    unique = []
    for it in merged:
        k = (it["market"], it["symbol"])
        if k not in seen:
            seen.add(k)
            unique.append(it)

    if len(_cache) > _MAX_CACHE:
        for k in list(_cache.keys())[: _MAX_CACHE // 2]:
            _cache.pop(k, None)
    _cache[key] = (now, unique)

    return unique[:limit]


async def _search_kr(query: str) -> list[dict]:
    """네이버 금융 자동완성 — 무료, 키 불필요, 실시간."""
    try:
        async with httpx.AsyncClient(
            timeout=5,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"},
        ) as c:
            r = await c.get(
                "https://ac.stock.naver.com/ac",
                params={"q": query, "target": "stock,index,marketindicator,coin,ipo"},
            )
            if r.status_code != 200:
                return []
            data = r.json()
    except Exception as e:
        log.debug(f"naver search fail: {e}")
        return []

    out: list[dict] = []
    for it in (data.get("items") or []):
        if it.get("category") != "stock":
            continue
        nation = (it.get("nationCode") or "").upper()
        code = it.get("code") or ""
        type_code = (it.get("typeCode") or "").upper()

        if nation == "KOR" and code.isdigit() and len(code) == 6:
            exchange = "KOSDAQ" if "KOSDAQ" in type_code else "KOSPI" if "KOSPI" in type_code else "KRX"
            out.append({
                "symbol": code, "name": it.get("name") or "",
                "name_en": "", "market": "KR",
                "exchange": exchange, "currency": "₩",
            })
        elif nation == "USA" and code:
            # 한국어로 미국주식 검색됨 ("테슬라" → TSLA). 우리만의 차별점.
            # Finnhub 결과와 중복되면 후단에서 제거
            if "." in code or ":" in code or "-" in code:
                continue  # ADR/접미사 종목 제외
            exchange = type_code if type_code in ("NASDAQ", "NYSE", "AMEX") else "NASDAQ/NYSE"
            out.append({
                "symbol": code, "name": it.get("name") or "",  # 한국어 이름 유지!
                "name_en": "", "market": "US",
                "exchange": exchange, "currency": "$",
            })
    return out


async def _search_us(query: str) -> list[dict]:
    """Finnhub 종목 검색 — 미국 + 글로벌 ADR."""
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(
                "https://finnhub.io/api/v1/search",
                params={"q": query, "token": key},  # exchange 필터는 free tier에서 무시됨
            )
            if r.status_code != 200:
                return []
            data = r.json()
    except Exception as e:
        log.debug(f"finnhub search fail: {e}")
        return []

    out: list[dict] = []
    for it in (data.get("result") or []):
        sym = it.get("symbol") or ""
        if not sym:
            continue
        # 다른 거래소 접미사(TSLA.DE, BABA-N 등) 제외 — 미국 본주만
        if "." in sym or ":" in sym or "-" in sym:
            continue
        if it.get("type") not in ("Common Stock", "ADR", "ETF", None, ""):
            continue
        out.append({
            "symbol": sym,
            "name": it.get("description") or "",
            "name_en": it.get("description") or "",
            "market": "US",
            "exchange": "NASDAQ/NYSE",
            "currency": "$",
        })
    return out
