"""스크린샷 → 포트폴리오 자동 추출 (Gemini Vision).

지원 화면:
- 토스증권 (Toss): 보유종목 + 평가금액 + 수량 (소수점) + 변동률
- 키움 영웅문 / 미래에셋 / 한국투자증권 등 일반 증권사
- Robinhood / Webull / 야후 등 영문 화면

추출 정보:
- symbol: 한국 6자리 또는 미국 티커
- name: 종목명 (한글 그대로 보존)
- shares: 보유 수량 (소수점 가능)
- entry_price: 평균 매수가 (1주당)
- krw_invested: 총 투자/평가 금액 (UI 표기 통화)

정확도 강화 (v2):
- 모델: gemini-2.5-flash (vision 정확도 ↑)
- MIME 자동 감지 (JPEG/PNG/WebP/HEIC)
- 한국 종목명 → 6자리 코드 매핑 50개 + 미국 한글 표기 매핑 80개
- 1차 실패 시 stricter 프롬프트로 재시도
- 한글명만 알고 티커 모를 때 Naver 자동완성으로 폴백
- 합리성 검증 (수량/금액 범위)
"""
from __future__ import annotations
import os
import json
import base64
import logging
import re
import httpx

log = logging.getLogger("app.ocr")

# 모델 — env로 오버라이드 가능
MODEL = os.environ.get("OCR_GEMINI_MODEL", "gemini-2.5-flash")


# ─── 한글 표기 → 미국 티커 (대폭 확장) ─────────────────────────────
KR_TO_US_TICKER = {
    # Mega-cap tech
    "마이크로소프트": "MSFT", "엠에스": "MSFT",
    "애플": "AAPL", "에플": "AAPL",
    "구글": "GOOGL", "알파벳": "GOOGL", "알파벳A": "GOOGL", "알파벳C": "GOOG",
    "아마존": "AMZN",
    "메타": "META", "페이스북": "META", "메타플랫폼스": "META",
    "테슬라": "TSLA",
    "엔비디아": "NVDA",
    "넷플릭스": "NFLX",
    # Semiconductors
    "AMD": "AMD", "에이엠디": "AMD",
    "인텔": "INTC",
    "TSMC": "TSM", "타이완반도체": "TSM",
    "퀄컴": "QCOM",
    "브로드컴": "AVGO",
    "마이크론": "MU",
    "ASML": "ASML",
    "어플라이드머티어리얼즈": "AMAT",
    "램리서치": "LRCX",
    "텍사스인스트루먼트": "TXN",
    "ARM": "ARM",
    # Consumer
    "코스트코": "COST",
    "월마트": "WMT",
    "타겟": "TGT",
    "맥도날드": "MCD",
    "스타벅스": "SBUX",
    "치폴레": "CMG",
    "나이키": "NKE",
    "아디다스": "ADDYY",
    "룰루레몬": "LULU",
    "디즈니": "DIS",
    "코카콜라": "KO", "코카-콜라": "KO",
    "펩시": "PEP", "펩시코": "PEP",
    "P&G": "PG", "프록터앤갬블": "PG",
    "유니레버": "UL",
    "에스티로더": "EL",
    # Finance
    "JP모건": "JPM", "제이피모건": "JPM", "JP모건체이스": "JPM",
    "버크셔": "BRK.B", "버크셔해서웨이": "BRK.B",
    "비자": "V",
    "마스터카드": "MA",
    "뱅크오브아메리카": "BAC", "BoA": "BAC",
    "골드만삭스": "GS",
    "모건스탠리": "MS",
    "웰스파고": "WFC",
    "씨티그룹": "C",
    "찰스슈왑": "SCHW",
    "페이팔": "PYPL",
    "블록": "SQ", "스퀘어": "SQ",
    # Healthcare / Pharma
    "존슨앤존슨": "JNJ", "존슨앤드존슨": "JNJ", "J&J": "JNJ",
    "유나이티드헬스": "UNH",
    "화이자": "PFE",
    "일라이릴리": "LLY", "릴리": "LLY",
    "노보노디스크": "NVO",
    "머크": "MRK",
    "애브비": "ABBV",
    "써모피셔": "TMO",
    "다나허": "DHR",
    "애보트": "ABT",
    "암젠": "AMGN",
    "길리어드": "GILD",
    "모더나": "MRNA",
    # Industry / Energy / Defense
    "보잉": "BA",
    "캐터필러": "CAT",
    "엑손모빌": "XOM",
    "셰브론": "CVX",
    "코노코필립스": "COP",
    "록히드마틴": "LMT",
    "RTX": "RTX", "레이시온": "RTX",
    "노스롭그루먼": "NOC",
    "GE": "GE", "제너럴일렉트릭": "GE",
    "허니웰": "HON",
    "유니온퍼시픽": "UNP",
    # Tech / SaaS
    "오라클": "ORCL",
    "세일즈포스": "CRM",
    "어도비": "ADBE",
    "시스코": "CSCO",
    "IBM": "IBM",
    "팔란티어": "PLTR",
    "스노우플레이크": "SNOW",
    "데이터독": "DDOG",
    "쇼피파이": "SHOP",
    "서비스나우": "NOW",
    "워크데이": "WDAY",
    "크라우드스트라이크": "CRWD",
    "지스케일러": "ZS",
    "옥타": "OKTA",
    # Mobility / Travel
    "우버": "UBER",
    "리프트": "LYFT",
    "에어비앤비": "ABNB",
    "부킹홀딩스": "BKNG",
    # Auto
    "포드": "F",
    "GM": "GM", "제너럴모터스": "GM",
    "리비안": "RIVN",
    "루시드": "LCID",
    "니오": "NIO",
    "BYD": "BYDDY",
    # China / 기타
    "알리바바": "BABA",
    "JD닷컴": "JD",
    "바이두": "BIDU",
    "핀둬둬": "PDD",
    "테무": "PDD",
    # Streaming / Media
    "스포티파이": "SPOT",
    "로블록스": "RBLX",
    # Crypto-adjacent
    "코인베이스": "COIN",
    "마이크로스트래티지": "MSTR", "마스트": "MSTR",
    # ETF (popular in KR)
    "SCHD": "SCHD", "QQQ": "QQQ", "SPY": "SPY", "VOO": "VOO", "VTI": "VTI",
    "TQQQ": "TQQQ", "SOXL": "SOXL", "ARKK": "ARKK",
    "JEPI": "JEPI", "JEPQ": "JEPQ",
    "QYLD": "QYLD", "TLT": "TLT", "GLD": "GLD",
}


# ─── 한국 종목명 → 6자리 코드 (KOSPI/KOSDAQ Top 시총 + 인기) ─────
KR_NAME_TO_CODE = {
    "삼성전자": "005930",
    "SK하이닉스": "000660", "에스케이하이닉스": "000660",
    "LG에너지솔루션": "373220", "엘지에너지솔루션": "373220",
    "삼성바이오로직스": "207940",
    "현대차": "005380", "현대자동차": "005380",
    "기아": "000270",
    "셀트리온": "068270",
    "POSCO홀딩스": "005490", "포스코홀딩스": "005490",
    "NAVER": "035420", "네이버": "035420",
    "카카오": "035720",
    "삼성SDI": "006400",
    "LG화학": "051910", "엘지화학": "051910",
    "KB금융": "105560",
    "신한지주": "055550",
    "하나금융지주": "086790",
    "메리츠금융지주": "138040",
    "삼성생명": "032830",
    "삼성화재": "000810",
    "삼성물산": "028260",
    "SK이노베이션": "096770",
    "한화솔루션": "009830",
    "포스코퓨처엠": "003670",
    "에코프로비엠": "247540",
    "에코프로": "086520",
    "엔켐": "348370",
    "리노공업": "058470",
    "HLB": "028300",
    "알테오젠": "196170",
    "두산에너빌리티": "034020",
    "한화에어로스페이스": "012450",
    "현대모비스": "012330",
    "LG전자": "066570", "엘지전자": "066570",
    "삼성SDS": "018260",
    "SK텔레콤": "017670", "에스케이텔레콤": "017670",
    "KT&G": "033780",
    "고려아연": "010130",
    "한미반도체": "042700",
    "JYP Ent.": "035900", "JYP": "035900",
    "에스엠": "041510", "SM엔터": "041510", "SM엔터테인먼트": "041510",
    "와이지엔터테인먼트": "122870", "YG": "122870", "YG엔터테인먼트": "122870",
    "하이브": "352820", "HYBE": "352820",
    "두산": "000150",
    "LG": "003550",
    "SK": "034730",
    "롯데지주": "004990",
    "현대건설": "000720",
    "두산밥캣": "241560",
    "삼성중공업": "010140",
    "한국전력": "015760", "한전": "015760",
    "KT": "030200",
    "대한항공": "003490",
    "CJ제일제당": "097950",
    "오리온": "271560",
    "한미약품": "128940",
    "유한양행": "000100",
    "SK바이오팜": "326030",
    "LG생활건강": "051900",
    "아모레퍼시픽": "090430",
    "한국조선해양": "009540", "HD한국조선해양": "009540",
    "현대중공업": "329180", "HD현대중공업": "329180",
    "현대로템": "064350",
    "셀트리온헬스케어": "091990",
    "엔씨소프트": "036570",
    "넷마블": "251270",
    "크래프톤": "259960",
    "펄어비스": "263750",
    "위메이드": "112040",
    "카카오게임즈": "293490",
    "카카오뱅크": "323410",
    "카카오페이": "377300",
    "F&F": "383220",
    "한미사이언스": "008930",
    "포스코인터내셔널": "047050",
    "삼성E&A": "028050",
    "DB하이텍": "000990",
    "솔브레인": "357780",
    "원익IPS": "240810",
    "케이엠더블유": "032500",
    "코스맥스": "192820",
    "현대글로비스": "086280",
    "기업은행": "024110", "IBK기업은행": "024110",
    "우리금융지주": "316140",
    "BNK금융지주": "138930",
    "DGB금융지주": "139130",
    "SK스퀘어": "402340",
    "넥슨게임즈": "225570",
    "더존비즈온": "012510",
    "셀트리온제약": "068760",
    "씨에스윈드": "112610",
    "삼성전기": "009150",
    "LG디스플레이": "034220", "엘지디스플레이": "034220",
    "한온시스템": "018880",
    "효성중공업": "298040",
    "에스원": "012750",
    "하이브": "352820",
    "두산로보틱스": "454910",
    "리가켐바이오": "141080",
}


def _detect_mime(b: bytes) -> str:
    """매직 바이트로 MIME 추론 — Gemini가 정확한 MIME 받으면 인식률↑."""
    if not b or len(b) < 12:
        return "image/jpeg"
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    if b[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc"):
        return "image/heic"
    return "image/jpeg"


def _clean_number(s) -> float:
    """문자열에서 첫 번째 숫자(부호+소수점)만 추출.

    '423,522원'         → 423522.0
    '+27,511 (6.9%)'    → 27511.0
    '$245.30'           → 245.30
    '0.693214주'        → 0.693214
    """
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    txt = str(s).replace(",", "").replace("주", "").replace("원", "").strip()
    m = re.search(r"[+-]?\d+\.?\d*", txt)
    if not m:
        return 0.0
    try:
        return float(m.group())
    except ValueError:
        return 0.0


def _normalize_symbol(raw: str, name: str = "") -> str:
    """티커 정규화. 다중 매핑 + 부분일치 + 자체 형식 검증."""
    raw = (raw or "").strip()
    name = (name or "").strip()

    # 1) 한국 종목명 정확 매칭
    if raw in KR_NAME_TO_CODE:
        return KR_NAME_TO_CODE[raw]
    if name in KR_NAME_TO_CODE:
        return KR_NAME_TO_CODE[name]

    # 2) 미국 종목 한글 정확 매칭
    if raw in KR_TO_US_TICKER:
        return KR_TO_US_TICKER[raw]
    if name in KR_TO_US_TICKER:
        return KR_TO_US_TICKER[name]

    # 3) 부분 일치 — 긴 이름 우선 (오탐 방지)
    for kr_name, kr_code in sorted(KR_NAME_TO_CODE.items(), key=lambda x: -len(x[0])):
        if len(kr_name) >= 3 and (kr_name in raw or kr_name in name):
            return kr_code
    for kr, ticker in sorted(KR_TO_US_TICKER.items(), key=lambda x: -len(x[0])):
        if len(kr) >= 3 and (kr in raw or kr in name):
            return ticker

    # 4) 6자리 숫자 (한국 코드)
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 6:
        return digits

    # 5) 영문 티커 (한글 없음)
    upper = raw.upper()
    has_korean = any("가" <= c <= "힣" for c in upper)
    if not has_korean and upper:
        cleaned = re.sub(r"[^A-Z0-9.\-]", "", upper)
        if 1 <= len(cleaned) <= 8:
            return cleaned

    return ""


def _resolve_via_naver(name: str) -> str | None:
    """매핑 사전에 없는 한글명 → Naver 자동완성으로 마지막 시도."""
    if not name or len(name) < 2:
        return None
    try:
        with httpx.Client(
            timeout=5,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"},
        ) as c:
            r = c.get(
                "https://ac.stock.naver.com/ac",
                params={"q": name, "target": "stock"},
            )
            if r.status_code != 200:
                return None
            data = r.json()
        for it in (data.get("items") or []):
            if it.get("category") != "stock":
                continue
            code = (it.get("code") or "").strip()
            nation = (it.get("nationCode") or "").upper()
            if nation == "KOR" and code.isdigit() and len(code) == 6:
                return code
            if nation == "USA" and code and "." not in code and "-" not in code and ":" not in code:
                return code.upper()
        return None
    except Exception as e:
        log.debug(f"naver resolve fail '{name}': {e}")
        return None


PROMPT = """이 이미지는 한국 증권사 앱(토스증권/키움/미래에셋/한국투자/NH투자) 또는
미국/글로벌 브로커리지(Robinhood/Webull) 등의 보유 종목(포트폴리오) 화면입니다.

각 보유 종목에서 다음 6가지를 정확히 추출하세요:

1. **name** — 화면에 보이는 종목명 그대로 (한글이면 한글 유지). 예: "삼성전자", "마이크로소프트", "코스트코"
2. **symbol** — 한국주식이면 6자리 숫자 코드(예 005930), 미국주식이면 영문 1~6자 티커(예 MSFT, AAPL). 화면에 코드가 안 보이면 null.
3. **shares** — 보유 수량 (소수점 가능. 예 0.693214). 콤마/주 글자 제외.
4. **current_value** — 현재 평가금액 (콤마/통화기호 제거 순수 숫자, 화면에 표시된 그 값 그대로).
5. **currency** — "KRW" 또는 "USD". 평가금액 옆에 "원" 또는 "₩" 또는 "₩" 보이면 KRW, "$" 또는 "달러" 보이면 USD. ⚠️ 매우 중요: 토스증권은 미국주식도 원화로 표시합니다. 화면 통화 기호 그대로 판별.
6. **market** — "KR" 또는 "US". 종목 자체가 한국주식인지 미국주식인지. (예: 마이크로소프트는 currency=KRW여도 market=US)

⚠️ 절대 규칙:
- 종목명은 영문으로 임의 변환하지 말 것 (마이크로소프트를 그대로, MSFT로 바꾸지 말 것)
- 화면에 안 보이는 정보는 null (절대 추측 금지)
- 그래프/차트/배너는 무시. 보유 종목 카드/리스트만.
- 평균단가가 보이면 entry_price로 추가 (단위는 currency와 동일), 안 보이면 0 또는 생략

순수 JSON 배열만 출력 (마크다운 ```json``` 금지, 설명 금지):

예시 1 (토스증권 한국주식 — 원화 표시):
[
  {"name": "삼성전자", "symbol": "005930", "shares": 10, "current_value": 750000, "entry_price": 70000, "currency": "KRW", "market": "KR"}
]

예시 2 (토스증권 미국주식 — 원화 표시! market과 currency 다름!):
[
  {"name": "마이크로소프트", "symbol": "MSFT", "shares": 0.693214, "current_value": 423522, "entry_price": 0, "currency": "KRW", "market": "US"},
  {"name": "코스트코", "symbol": "COST", "shares": 0.283, "current_value": 421932, "entry_price": 0, "currency": "KRW", "market": "US"}
]

예시 3 (Robinhood 미국주식 — 달러 표시):
[
  {"name": "Apple", "symbol": "AAPL", "shares": 5, "current_value": 1100, "entry_price": 200, "currency": "USD", "market": "US"}
]

종목 정보를 못 찾으면 빈 배열 [] 반환.
"""


def _call_gemini(api_key: str, image_bytes: bytes, mime: str, retry_hint: str = "") -> list:
    """Gemini Vision 호출 1회. retry_hint 있으면 프롬프트에 추가."""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/"
        f"models/{MODEL}:generateContent?key={api_key}"
    )
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    prompt = PROMPT + (f"\n\n⚠️ 재시도: {retry_hint}" if retry_hint else "")

    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime, "data": encoded}},
            ]
        }],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.05,  # 결정론적
            "maxOutputTokens": 2048,
        },
    }
    try:
        r = httpx.post(url, json=payload, timeout=45)
        if r.status_code != 200:
            log.warning(f"Gemini Vision HTTP {r.status_code}: {r.text[:300]}")
            return []
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        items = json.loads(text)
        return items if isinstance(items, list) else []
    except Exception as e:
        log.warning(f"Gemini Vision parse fail: {e}")
        return []


def extract_portfolio_from_image(image_bytes: bytes, krw_rate: float = 1380.0) -> list[dict]:
    """포트폴리오 스크린샷 분석. 정확도 강화 + 재시도 + 검증.

    krw_rate: 현재 USD→KRW 환율. 토스가 미국주식을 원화로 표시하기 때문에
              미국주식 entry_price를 USD 기준으로 변환할 때 사용.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        log.error("GEMINI_API_KEY 미설정")
        return []
    if not image_bytes:
        return []
    if krw_rate <= 0:
        krw_rate = 1380.0  # 합리적 디폴트

    mime = _detect_mime(image_bytes)
    log.info(f"OCR start: {len(image_bytes)} bytes, mime={mime}, model={MODEL}, krw_rate={krw_rate}")

    # 1차 호출
    items = _call_gemini(api_key, image_bytes, mime)
    # 비어있으면 stricter 프롬프트로 재시도 (가끔 첫 응답이 빈 배열로 옴)
    if not items:
        log.info("OCR 1차 빈 응답 → 재시도")
        items = _call_gemini(
            api_key, image_bytes, mime,
            retry_hint="이전 응답이 비었습니다. 화면에 분명히 보유 종목 카드/리스트가 있을 겁니다. "
                       "각 카드의 종목명·수량·평가금액을 다시 한 번 자세히 보고 추출하세요.",
        )

    out = []
    seen_syms = set()
    for it in items:
        if not isinstance(it, dict):
            continue

        raw_sym = str(it.get("symbol") or "").strip()
        name = str(it.get("name") or "").strip()

        # 심볼 결정: Gemini 값 우선 정규화 → 매핑 → Naver 폴백
        sym = _normalize_symbol(raw_sym, name)
        if not sym and name:
            resolved = _resolve_via_naver(name)
            if resolved:
                sym = resolved
                log.info(f"OCR Naver 폴백 매칭: '{name}' → {sym}")
        if not sym:
            log.warning(f"OCR 심볼 미해결 — raw='{raw_sym}', name='{name}' (스킵)")
            continue

        # 중복 방지 (같은 종목 두번 잡힌 경우)
        if sym in seen_syms:
            log.info(f"OCR 중복 스킵: {sym}")
            continue

        shares = _clean_number(it.get("shares"))
        current_value = _clean_number(it.get("current_value") or it.get("krw_invested"))
        entry_price = _clean_number(it.get("entry_price"))
        currency = str(it.get("currency") or "").upper()
        is_kr_stock = sym.isdigit() and len(sym) == 6

        # 통화 자동 추론 (Gemini가 currency 빠뜨렸을 때):
        # - 한국주식이면 무조건 KRW
        # - 미국주식 + current_value > 100,000이면 KRW (1주에 $100k 넘는 종목 거의 없음)
        if not currency:
            if is_kr_stock:
                currency = "KRW"
            elif current_value > 100_000:
                currency = "KRW"
            else:
                currency = "USD"

        # 합리성 검증
        if shares <= 0 or shares > 10_000_000:
            log.warning(f"OCR 비정상 shares ({sym}): {shares}")
            continue
        if current_value < 0 or current_value > 10_000_000_000:
            log.warning(f"OCR 비정상 current_value ({sym}): {current_value}")
            continue

        # 평균단가 자동 도출 (현재 통화 기준)
        if entry_price <= 0 and shares > 0 and current_value > 0:
            chg = it.get("change_pct")
            if chg is not None:
                try:
                    pct = float(chg) / 100.0
                    if -0.95 < pct < 5:
                        entry_price = current_value / (1 + pct) / shares
                    else:
                        entry_price = current_value / shares
                except (ValueError, TypeError):
                    entry_price = current_value / shares
            else:
                entry_price = current_value / shares

        # 🔑 핵심 변환 — DB는 stock의 native currency로 저장
        # KR 종목 → KRW 그대로
        # US 종목인데 화면이 KRW였으면 → USD로 환산 (entry_price 단위)
        krw_invested_total = current_value if currency == "KRW" else current_value * krw_rate
        if not is_kr_stock and currency == "KRW":
            entry_price = entry_price / krw_rate  # KRW per share → USD per share
            log.info(f"{sym}: KRW→USD 환산 entry_price={entry_price:.4f} (rate={krw_rate})")

        if entry_price <= 0:
            log.warning(f"OCR 평단가 도출 실패 ({sym})")
            continue

        seen_syms.add(sym)
        out.append({
            "symbol": sym,
            "name": name or sym,
            "shares": round(shares, 6),
            # entry_price는 stock native currency (KR=KRW per share, US=USD per share)
            "entry_price": round(entry_price, 4),
            # krw_invested는 항상 KRW (총 투자금) — 통일된 단위로 저장 → 손익 계산 일관성↑
            "krw_invested": round(krw_invested_total, 2),
            "market": "KR" if is_kr_stock else "US",
        })

    log.info(f"OCR 완료: {len(out)}개 — {[h['symbol'] for h in out]}")
    return out
