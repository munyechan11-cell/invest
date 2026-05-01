"""뉴스 헤드라인 한국어 번역 — Gemini 배치 번역 + 메모리 캐시.

- 입력 리스트를 한 번의 호출로 일괄 번역 (토큰 절약)
- URL 단위 캐시로 재호출 시 비용 0
- Gemini 실패/한도 초과 시 원문 그대로 반환 (사용자 경험 보장)
"""
from __future__ import annotations
import os, json, logging, hashlib
import httpx

log = logging.getLogger("translate")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-lite:generateContent"
_cache: dict[str, dict] = {}   # url_hash -> {"headline": ..., "summary": ...}
_MAX_CACHE = 500


def _key(url: str) -> str:
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()


async def translate_news_to_korean(items: list[dict]) -> list[dict]:
    """items: [{headline, summary, url, source, ts}, ...] — 영문 헤드라인을 한국어로."""
    if not items:
        return items
    api_key = os.environ.get("GEMINI_API_KEY", "")

    # 캐시 적중 항목 분리
    pending: list[tuple[int, dict]] = []
    result = [None] * len(items)
    for i, it in enumerate(items):
        k = _key(it.get("url", "") or it.get("headline", ""))
        if k in _cache:
            cached = _cache[k]
            new = dict(it)
            new["headline"] = cached["headline"]
            new["summary"] = cached["summary"]
            new["headline_en"] = it.get("headline", "")
            result[i] = new
        else:
            pending.append((i, it))

    if not pending:
        return result  # 전체 캐시 hit

    if not api_key:
        # API 키 없으면 원문 그대로 (캐시도 안 함)
        for i, it in pending:
            result[i] = it
        return result

    # ── Gemini 배치 번역
    lines = []
    for n, (_, it) in enumerate(pending, 1):
        h = (it.get("headline") or "").replace("\n", " ").strip()[:200]
        s = (it.get("summary") or "").replace("\n", " ").strip()[:300]
        lines.append(f"[{n}] HEADLINE: {h}\n    SUMMARY: {s}")
    body_text = "\n".join(lines)

    prompt = (
        "다음 영문 금융 뉴스를 한국어로 번역하라. 자연스러운 한국 경제 기사 톤으로.\n"
        "고유명사(기업·인명·지수)는 일반적인 한국 표기 사용. 숫자·통화는 원문 유지.\n"
        f"응답은 JSON 배열만, {len(pending)}개 항목 정확히, 다른 텍스트 없이:\n"
        '[{"headline": "한국어 헤드라인", "summary": "한국어 요약 (1~2문장)"}, ...]\n\n'
        "입력:\n" + body_text
    )

    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1500},
    }

    translations = None
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{GEMINI_URL}?key={api_key}", json=body)
            if r.status_code == 200:
                data = r.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                if text.startswith("```"):
                    text = text.strip("`")
                    if text.lower().startswith("json"):
                        text = text[4:]
                translations = json.loads(text)
            else:
                log.warning(f"translate http {r.status_code}: {r.text[:120]}")
    except Exception as e:
        log.warning(f"translate failed: {e}")

    # 결과 매핑 + 캐시 저장
    for idx, (orig_idx, it) in enumerate(pending):
        new = dict(it)
        new["headline_en"] = it.get("headline", "")
        if translations and idx < len(translations):
            tr = translations[idx] or {}
            kh = (tr.get("headline") or "").strip()
            ks = (tr.get("summary") or "").strip()
            if kh:
                new["headline"] = kh
            if ks:
                new["summary"] = ks
            # 캐시
            if len(_cache) > _MAX_CACHE:
                # 가장 오래된 절반 비우기
                for k in list(_cache.keys())[: _MAX_CACHE // 2]:
                    _cache.pop(k, None)
            _cache[_key(it.get("url", "") or it.get("headline", ""))] = {
                "headline": new["headline"], "summary": new["summary"],
            }
        result[orig_idx] = new

    return result
