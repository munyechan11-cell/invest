"""스크린샷 → 포트폴리오 자동 추출 (Gemini Vision).

지원 화면:
- 토스증권 (Toss): 보유종목 + 평가금액 + 수량 (소수점) + 변동률
- 키움 영웅문 / 미래에셋 / 한국투자증권 등 일반 증권사
- 야후 / 인베스팅닷컴 등 영문 화면

추출 정보:
- symbol: 한국 6자리 또는 미국 티커
- name: 종목명 (디스플레이용)
- shares: 보유 수량 (소수점 가능)
- entry_price: 평균 매수가 (1주당)
- krw_invested: 총 투자 금액 (UI 표기 통화)
- current_value: 현재 평가 금액 (옵션)
"""
from __future__ import annotations
import os
import json
import base64
import logging
import re
import requests

log = logging.getLogger("app.ocr")


# 한국어 종목명 → 미국 티커 매핑 (자주 쓰이는 것들)
KR_TO_US_TICKER = {
    "마이크로소프트": "MSFT",
    "MS": "MSFT",
    "애플": "AAPL",
    "구글": "GOOGL",
    "알파벳": "GOOGL",
    "아마존": "AMZN",
    "메타": "META",
    "테슬라": "TSLA",
    "엔비디아": "NVDA",
    "넷플릭스": "NFLX",
    "코스트코": "COST",
    "비자": "V",
    "존슨앤드존슨": "JNJ",
    "월마트": "WMT",
    "디즈니": "DIS",
    "코카콜라": "KO",
    "맥도날드": "MCD",
}


def _clean_number(s) -> float:
    """문자열에서 첫 번째 숫자(부호+소수점)만 추출.

    '423,522원'         → 423522.0
    '+27,511 (6.9%)'    → 27511.0  (괄호 안 % 무시)
    '$245.30'           → 245.30
    """
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    txt = str(s)
    # 콤마 제거 후 첫 숫자(부호 가능)만 매칭
    txt_no_comma = txt.replace(",", "")
    m = re.search(r"[+-]?\d+\.?\d*", txt_no_comma)
    if not m:
        return 0.0
    try:
        return float(m.group())
    except ValueError:
        return 0.0


def _normalize_symbol(raw: str, name: str = "") -> str:
    """티커 정규화 — 한글 종목명도 매핑 시도.

    우선순위:
    1. 한글 종목명 매핑 (마이크로소프트 → MSFT)
    2. 6자리 숫자 (한국주식)
    3. 영문 1~6자 티커
    """
    raw = (raw or "").strip()
    name = (name or "").strip()

    # 1. 한국어 매핑 우선 (raw나 name 둘 중 하나에 포함되면)
    for kr, ticker in KR_TO_US_TICKER.items():
        if kr in raw or kr in name:
            return ticker

    # 2. 6자리 숫자 = 한국주식
    if raw.isdigit() and len(raw) == 6:
        return raw

    # 3. 영문 티커 (한글 포함 안 되어야 함)
    upper = raw.upper()
    has_korean = any("가" <= c <= "힣" for c in upper)
    if not has_korean and upper and 1 <= len(upper.replace(".", "").replace("-", "")) <= 6:
        # 알파벳/숫자/.-만 허용
        if all(c.isalnum() or c in ".-" for c in upper):
            return upper

    return upper or raw


def extract_portfolio_from_image(image_bytes: bytes) -> list[dict]:
    """Gemini Vision으로 포트폴리오 스크린샷 분석."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        log.error("GEMINI_API_KEY가 설정되지 않았습니다.")
        return []

    if not image_bytes:
        return []

    encoded = base64.b64encode(image_bytes).decode("utf-8")
    # 모델 — 2.0 Flash가 1.5보다 vision 정확도 높음
    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        "models/gemini-2.0-flash:generateContent?key=" + api_key
    )

    prompt = """이 이미지는 한국 또는 미국 증권사 앱(토스증권/키움/미래에셋 등)의
보유 종목(포트폴리오) 화면입니다.

화면에서 각 보유 종목의 다음 정보를 추출하세요:

1. **종목명** (한국어 또는 영문) — 예: "마이크로소프트", "삼성전자", "TSLA"
2. **보유 수량** — 소수점 가능 (예: 0.693214주)
3. **평가금액** (총 평가가, 콤마/통화기호 제거 후 숫자만) — 예: 423,522원 → 423522
4. **수익률 변동** (가능하면) — 예: +6.9%, -0.3%

⚠️ 중요 규칙:
- 한국주식은 6자리 코드 (예: 005930) 또는 한국어 종목명 그대로
- 미국주식은 영문 티커 (예: MSFT, AAPL, COST, SCHD, TSLA)
- "마이크로소프트" → MSFT, "코스트코" → COST 같이 한국어 표기를 영문 티커로 정규화
- 평균단가가 화면에 안 보이면 entry_price를 0으로 (서버에서 평가금액/수량으로 계산)
- 통화 기호(원, $, ₩, USD)는 제거하고 숫자만
- 콤마(,) 제거하고 숫자만

JSON 배열로 반환 (마크다운 금지):
[
  {"symbol": "MSFT", "name": "마이크로소프트", "shares": 0.693214, "current_value": 423522, "change_pct": 6.9},
  {"symbol": "SCHD", "name": "SCHD", "shares": 6.667994, "current_value": 313021, "change_pct": 1.4}
]

종목 정보를 못 찾으면 빈 배열 [] 반환.
"""

    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {
                    "mime_type": "image/jpeg",
                    "data": encoded,
                }}
            ]
        }],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.1,
            "maxOutputTokens": 1500,
        },
    }

    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code != 200:
            log.warning(f"Gemini Vision HTTP {r.status_code}: {r.text[:300]}")
            return []
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        # 코드펜스 제거 (혹시 모를)
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        items = json.loads(text)
        if not isinstance(items, list):
            return []
    except Exception as e:
        log.warning(f"Gemini Vision parse fail: {e}")
        return []

    # 후처리: 정규화 + 평균단가 자동 계산
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        raw_sym = str(it.get("symbol") or "").strip()
        name = str(it.get("name") or "").strip()
        sym = _normalize_symbol(raw_sym, name)
        if not sym:
            continue

        shares = _clean_number(it.get("shares"))
        current_value = _clean_number(it.get("current_value") or it.get("krw_invested"))
        entry_price = _clean_number(it.get("entry_price"))

        # 평균단가 = 평가금액 / 수량 (UI에 평단 안 보이는 경우 추정 — 정확하진 않음)
        if entry_price <= 0 and shares > 0 and current_value > 0:
            # 변동률 % 있으면 역산 (평단 = 현재 평가 / (1 + 수익률) / 수량)
            chg = it.get("change_pct")
            if chg is not None:
                try:
                    pct = float(chg) / 100.0
                    if -0.95 < pct < 5:  # 합리적 범위
                        entry_price = current_value / (1 + pct) / shares
                    else:
                        entry_price = current_value / shares
                except (ValueError, TypeError):
                    entry_price = current_value / shares
            else:
                entry_price = current_value / shares

        if shares <= 0 or entry_price <= 0:
            continue

        out.append({
            "symbol": sym,
            "name": name or sym,
            "shares": round(shares, 6),
            "entry_price": round(entry_price, 4),
            "krw_invested": round(current_value or shares * entry_price, 2),
        })

    log.info(f"OCR extracted {len(out)} holdings: {[h['symbol'] for h in out]}")
    return out
