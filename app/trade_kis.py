"""KIS Open API 실제 주문 — 한국주식 + 미국주식 통합.

⚠️ SAFETY DEFAULT: 모의투자(Paper) 모드.
실전 주문 활성화는 KIS_LIVE_ENABLED=true 환경변수 명시 필요.

필수 환경변수:
- KIS_APP_KEY / KIS_APP_SECRET (시세 조회와 동일)
- KIS_ACCOUNT_NO (계좌번호 8자리)
- KIS_ACCOUNT_PRD_CD (상품코드, 일반은 "01")
- KIS_LIVE_ENABLED (true/false, 기본 false=모의투자)
- KIS_PAPER (KIS_LIVE_ENABLED=false면 자동 true)

선택 환경변수 (안전장치):
- MAX_ORDER_AMOUNT_KRW (한국주식 1회 한도, 기본 300,000원)
- MAX_ORDER_AMOUNT_USD (미국주식 1회 한도, 기본 $200)

KIS API 공식 문서:
- 국내 주문: /uapi/domestic-stock/v1/trading/order-cash
- 해외 주문: /uapi/overseas-stock/v1/trading/order
"""
from __future__ import annotations
import os, time, logging
import httpx

log = logging.getLogger("trade_kis")

_KIS_REAL = "https://openapi.koreainvestment.com:9443"
_KIS_MOCK = "https://openapivts.koreainvestment.com:29443"
_token_cache: dict = {"value": None, "exp": 0}


def is_live() -> bool:
    """실전 주문 모드 여부. 기본값 False (모의투자)."""
    return os.environ.get("KIS_LIVE_ENABLED", "false").lower() == "true"


def _base() -> str:
    return _KIS_REAL if is_live() else _KIS_MOCK


def _account() -> tuple[str, str]:
    no = os.environ.get("KIS_ACCOUNT_NO", "").strip()
    pd = os.environ.get("KIS_ACCOUNT_PRD_CD", "01").strip()
    if not no:
        raise RuntimeError("KIS_ACCOUNT_NO 환경변수가 비어있습니다. 계좌번호 8자리를 .env에 설정하세요.")
    return no, pd


def _token() -> str:
    if _token_cache["value"] and _token_cache["exp"] > time.time() + 60:
        return _token_cache["value"]
    app_key = os.environ.get("KIS_APP_KEY")
    app_sec = os.environ.get("KIS_APP_SECRET")
    if not app_key or not app_sec:
        raise RuntimeError("KIS_APP_KEY / KIS_APP_SECRET 환경변수 필요")
    r = httpx.post(
        f"{_base()}/oauth2/tokenP",
        json={"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_sec},
        timeout=15,
    )
    r.raise_for_status()
    d = r.json()
    _token_cache["value"] = d["access_token"]
    _token_cache["exp"] = time.time() + int(d.get("expires_in", 21600))
    return _token_cache["value"]


def _hashkey(body: dict) -> str:
    """KIS 실전 주문 시 필수 — 위변조 방지 해시."""
    r = httpx.post(
        f"{_base()}/uapi/hashkey", json=body,
        headers={
            "content-type": "application/json; charset=utf-8",
            "appkey": os.environ["KIS_APP_KEY"],
            "appsecret": os.environ["KIS_APP_SECRET"],
        },
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["HASH"]


def calc_fee(market: str, amount: float, fee_rate: float | None = None) -> float:
    """KR 0.015% / US 0.07% 기본. 사용자 설정 우선."""
    if fee_rate is not None and fee_rate >= 0:
        return amount * fee_rate
    rate = 0.00015 if market == "KR" else 0.0007
    return amount * rate


# ─── 안전장치 ──────────────────────────────────────────────────────
def check_safety(market: str, qty: int, price: float) -> str | None:
    """주문 전 검증 (1회 한도). None=통과, 문자열=거부 사유."""
    if qty <= 0:
        return "주문 수량은 1주 이상이어야 합니다."
    if price < 0:
        return "주문 가격은 0(시장가) 또는 양수여야 합니다."
    amount = qty * price if price > 0 else 0
    if market == "KR":
        cap = float(os.environ.get("MAX_ORDER_AMOUNT_KRW", "300000"))
        if amount > cap:
            return f"주문 금액 ₩{amount:,.0f}이 1회 한도 ₩{cap:,.0f}을 초과합니다."
    else:
        cap = float(os.environ.get("MAX_ORDER_AMOUNT_USD", "200"))
        if amount > cap:
            return f"주문 금액 ${amount:,.2f}이 1회 한도 ${cap:,.2f}을 초과합니다."
    return None


async def check_daily_limit(user_id: int, market: str, amount: float) -> str | None:
    """일일 누적 한도 검증. None=통과, str=거부 사유."""
    from server import db
    settings = await db.get_user_settings(user_id)
    daily_cap = settings.get(
        "daily_max_order_krw" if market == "KR" else "daily_max_order_usd",
        300000 if market == "KR" else 200
    )
    used = await db.daily_used(user_id, market)
    if used + amount > daily_cap:
        cur = "₩" if market == "KR" else "$"
        return (f"일일 한도 초과: 오늘 {cur}{used:,.0f} 사용 + 이번 {cur}{amount:,.0f} → "
                f"{cur}{used + amount:,.0f} > 한도 {cur}{daily_cap:,.0f}")
    return None


# ─── 한국주식 주문 ────────────────────────────────────────────────
def order_kr(symbol: str, side: str, qty: int, price: float = 0) -> dict:
    """한국주식 매수/매도.

    Args:
        symbol: 6자리 종목코드 (예: "005930")
        side: "buy" 또는 "sell"
        qty: 주문 수량
        price: 지정가 (0이면 시장가)
    """
    err = check_safety("KR", qty, price)
    if err:
        return {"ok": False, "error": err}

    try:
        cano, prdt = _account()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    body = {
        "CANO": cano,
        "ACNT_PRDT_CD": prdt,
        "PDNO": symbol,
        "ORD_DVSN": "00" if price > 0 else "01",  # 00=지정가, 01=시장가
        "ORD_QTY": str(qty),
        "ORD_UNPR": str(int(price)) if price > 0 else "0",
    }

    live = is_live()
    if side == "buy":
        tr_id = "TTTC0802U" if live else "VTTC0802U"
    elif side == "sell":
        tr_id = "TTTC0801U" if live else "VTTC0801U"
    else:
        return {"ok": False, "error": f"side는 'buy' 또는 'sell'이어야 합니다 (받음: {side})"}

    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {_token()}",
        "appkey": os.environ["KIS_APP_KEY"],
        "appsecret": os.environ["KIS_APP_SECRET"],
        "tr_id": tr_id,
        "custtype": "P",
    }
    if live:
        try:
            headers["hashkey"] = _hashkey(body)
        except Exception as e:
            log.warning(f"hashkey 생성 실패: {e}")

    try:
        r = httpx.post(
            f"{_base()}/uapi/domestic-stock/v1/trading/order-cash",
            json=body, headers=headers, timeout=15,
        )
    except Exception as e:
        return {"ok": False, "error": f"네트워크 오류: {e}"}

    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:300]}"}
    d = r.json()
    if d.get("rt_cd") != "0":
        return {"ok": False, "error": d.get("msg1", "주문 실패")}

    out = d.get("output") or {}
    return {
        "ok": True,
        "mode": "LIVE" if live else "PAPER",
        "market": "KR",
        "symbol": symbol, "side": side, "qty": qty, "price": price,
        "order_no": out.get("ODNO"),
        "order_time": out.get("ORD_TMD"),
        "msg": d.get("msg1"),
    }


# ─── 미국주식 주문 ────────────────────────────────────────────────
def order_us(symbol: str, side: str, qty: int, price: float = 0,
             exchange: str = "NASD") -> dict:
    """미국주식 매수/매도. exchange: NASD/NYSE/AMEX.

    주의: KIS 미국주식 주문은 정규장(한국 23:30~06:00) 시간대에만 체결.
    """
    err = check_safety("US", qty, price)
    if err:
        return {"ok": False, "error": err}

    try:
        cano, prdt = _account()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    body = {
        "CANO": cano,
        "ACNT_PRDT_CD": prdt,
        "OVRS_EXCG_CD": exchange,
        "PDNO": symbol.upper(),
        "ORD_QTY": str(qty),
        "OVRS_ORD_UNPR": f"{price:.2f}" if price > 0 else "0",
        "ORD_SVR_DVSN_CD": "0",
        "ORD_DVSN": "00",  # 지정가 (시장가는 미국주식 미지원)
    }

    live = is_live()
    if side == "buy":
        tr_id = "TTTT1002U" if live else "VTTT1002U"
    elif side == "sell":
        tr_id = "TTTT1006U" if live else "VTTT1001U"
    else:
        return {"ok": False, "error": f"side는 'buy' 또는 'sell'"}

    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {_token()}",
        "appkey": os.environ["KIS_APP_KEY"],
        "appsecret": os.environ["KIS_APP_SECRET"],
        "tr_id": tr_id,
        "custtype": "P",
    }
    if live:
        try:
            headers["hashkey"] = _hashkey(body)
        except Exception as e:
            log.warning(f"hashkey 생성 실패: {e}")

    try:
        r = httpx.post(
            f"{_base()}/uapi/overseas-stock/v1/trading/order",
            json=body, headers=headers, timeout=15,
        )
    except Exception as e:
        return {"ok": False, "error": f"네트워크 오류: {e}"}

    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:300]}"}
    d = r.json()
    if d.get("rt_cd") != "0":
        return {"ok": False, "error": d.get("msg1", "주문 실패")}

    out = d.get("output") or {}
    return {
        "ok": True,
        "mode": "LIVE" if live else "PAPER",
        "market": "US",
        "symbol": symbol.upper(), "side": side, "qty": qty, "price": price,
        "order_no": out.get("ODNO"),
        "msg": d.get("msg1"),
    }


def auto_order(symbol: str, side: str, qty: int, price: float = 0) -> dict:
    """심볼 형태로 시장 자동 판별 후 주문."""
    is_kr = symbol.isdigit() and len(symbol) == 6
    if is_kr:
        return order_kr(symbol, side, qty, price)
    return order_us(symbol, side, qty, price)
