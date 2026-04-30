import os
import json
import base64
import requests
import logging

log = logging.getLogger("app.ocr")

def extract_portfolio_from_image(image_bytes: bytes) -> list[dict]:
    """Gemini Vision API를 사용하여 스크린샷에서 포트폴리오 데이터를 추출합니다."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        log.error("GEMINI_API_KEY가 설정되지 않았습니다.")
        return []

    # 이미지를 Base64로 인코딩
    encoded_image = base64.b64encode(image_bytes).decode('utf-8')

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
    
    prompt = """
    이 이미지는 주식 포트폴리오(보보유 종목) 스크린샷입니다.
    이미지에서 각 종목의 [티커 또는 종목명], [매수 평단가], [총 투자금액(원화 또는 달러)]을 찾아내어 JSON 배열로 반환하세요.
    
    - 한국 주식은 6자리 숫자로, 미국 주식은 영문 티커(예: AAPL)로 변환하세요.
    - 가격 정보에서 콤마(,)와 통화 기호는 제거하고 숫자만 추출하세요.
    - 알 수 없는 항목은 제외하세요.
    
    응답 형식: [{"symbol": "AAPL", "entry_price": 150.5, "krw_invested": 1000000}, ...]
    반드시 순수 JSON 형식으로만 응답하세요. (마크다운 금지)
    """

    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {
                    "inline_data": {
                        "mime_type": "image/jpeg",
                        "data": encoded_image
                    }
                }
            ]
        }],
        "generationConfig": {
            "response_mime_type": "application/json"
        }
    }

    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        # 마크다운 코드 블록 제거 (혹시 있는 경우)
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        
        return json.loads(text)
    except Exception as e:
        log.error(f"이미지 분석 중 오류 발생: {e}")
        return []
