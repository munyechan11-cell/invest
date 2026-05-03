"""DART OpenAPI 통합 클라이언트 — 한국 상장사 재무·공시 풀 활용.

기능:
1. corp_code 자동 매핑 (lazy cache)
2. 분기별 재무 (매출/영업이익/순이익) + YoY 성장률
3. 주요 재무비율 (영업이익률, ROE, 부채비율)
4. 공시 분류 + 자동 한국어 해석
"""
from __future__ import annotations
import os
import logging
import asyncio
from datetime import datetime
import httpx

log = logging.getLogger("dart")

DART_BASE = "https://opendart.fss.or.kr/api"

_corp_code_cache: dict[str, str] = {}
_financial_cache: dict[str, tuple[float, dict]] = {}  # stock_code → (ts, data)
_FIN_TTL = 86400  # 재무는 분기별이라 24시간 캐시

# 보고서 코드: 1분기보고서, 반기보고서, 3분기보고서, 사업보고서(연간)
_REPORT_CODES = ["11013", "11012", "11014", "11011"]


def _key() -> str | None:
    return os.environ.get("DART_API_KEY") or None


# ─── corp_code 매핑 ──────────────────────────────────────────────
async def get_corp_code(stock_code: str) -> str:
    """6자리 종목코드 → 8자리 corp_code. 최근 공시 목록에서 추출 (lazy)."""
    if stock_code in _corp_code_cache:
        return _corp_code_cache[stock_code]
    key = _key()
    if not key:
        return ""

    from datetime import timedelta
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{DART_BASE}/list.json", params={
                "crtfc_key": key, "stock_code": stock_code,
                "bgn_de": start, "end_de": end, "page_count": 1,
            })
            if r.status_code != 200:
                return ""
            data = r.json()
            if data.get("status") != "000":
                return ""
            items = data.get("list") or []
            if items and items[0].get("corp_code"):
                cc = items[0]["corp_code"]
                _corp_code_cache[stock_code] = cc
                return cc
    except Exception as e:
        log.warning(f"corp_code {stock_code}: {e}")
    return ""


# ─── 분기 재무 fetch ─────────────────────────────────────────────
async def _fetch_acnt(corp_code: str, year: int, reprt: str) -> list[dict]:
    """주요계정 단일조회 (개별/연결 모두 시도)."""
    key = _key()
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{DART_BASE}/fnlttSinglAcnt.json", params={
                "crtfc_key": key, "corp_code": corp_code,
                "bsns_year": str(year), "reprt_code": reprt,
            })
            if r.status_code != 200:
                return []
            data = r.json()
            if data.get("status") != "000":
                return []
            return data.get("list") or []
    except Exception:
        return []


def _parse_amount(s: str) -> float:
    """DART 금액 문자열 → float (음수 부호 처리)."""
    if not s:
        return 0.0
    try:
        return float(str(s).replace(",", "").replace(" ", ""))
    except (ValueError, TypeError):
        return 0.0


def _extract_key_metrics(items: list[dict]) -> dict:
    """주요계정 응답에서 매출/영업이익/순이익 추출 (당기/전기)."""
    out = {}
    # 우선순위: 연결재무제표(CFS) > 별도(OFS)
    target_fs = "CFS"
    if not any(it.get("fs_div") == "CFS" for it in items):
        target_fs = "OFS"

    for it in items:
        if it.get("fs_div") != target_fs:
            continue
        nm = it.get("account_nm", "")
        # account_nm 매칭 (DART 표준 표기)
        if "매출액" in nm and "원가" not in nm:
            out["revenue_curr"] = _parse_amount(it.get("thstrm_amount"))
            out["revenue_prev"] = _parse_amount(it.get("frmtrm_amount"))
        elif "영업이익" in nm and "전" not in nm:
            out["operating_income_curr"] = _parse_amount(it.get("thstrm_amount"))
            out["operating_income_prev"] = _parse_amount(it.get("frmtrm_amount"))
        elif nm in ("당기순이익", "분기순이익", "반기순이익"):
            out["net_income_curr"] = _parse_amount(it.get("thstrm_amount"))
            out["net_income_prev"] = _parse_amount(it.get("frmtrm_amount"))
        elif "자본총계" in nm:
            out["equity"] = _parse_amount(it.get("thstrm_amount"))
        elif "부채총계" in nm:
            out["liabilities"] = _parse_amount(it.get("thstrm_amount"))
        elif "자산총계" in nm:
            out["assets"] = _parse_amount(it.get("thstrm_amount"))
    return out


async def get_financials(stock_code: str) -> dict:
    """종목 최근 재무 — 가장 최신 보고서 기준.

    Returns: {
        revenue, operating_income, net_income (단위: 원),
        revenue_growth_pct, op_growth_pct, ni_growth_pct,
        operating_margin_pct, roe_pct, debt_ratio_pct,
        report_period (예: "2025_3Q"), as_of (날짜)
    }
    """
    import time as _t
    if stock_code in _financial_cache:
        ts, data = _financial_cache[stock_code]
        if _t.time() - ts < _FIN_TTL:
            return data

    cc = await get_corp_code(stock_code)
    if not cc:
        return {}

    # 최신 연도부터 거꾸로 시도, 가장 최근 보고서 채택
    current_year = datetime.now().year
    metrics = {}
    period_label = ""

    for year in [current_year, current_year - 1]:
        for reprt in _REPORT_CODES:  # 1Q, H, 3Q, 연간
            items = await _fetch_acnt(cc, year, reprt)
            if not items:
                continue
            m = _extract_key_metrics(items)
            if m.get("revenue_curr"):
                metrics = m
                lbl = {"11013": "1Q", "11012": "반기", "11014": "3Q", "11011": "연간"}[reprt]
                period_label = f"{year}_{lbl}"
                break
        if metrics:
            break

    if not metrics:
        return {}

    # 파생 지표 계산
    rev = metrics.get("revenue_curr", 0)
    rev_prev = metrics.get("revenue_prev", 0)
    op = metrics.get("operating_income_curr", 0)
    op_prev = metrics.get("operating_income_prev", 0)
    ni = metrics.get("net_income_curr", 0)
    ni_prev = metrics.get("net_income_prev", 0)
    eq = metrics.get("equity", 0)
    li = metrics.get("liabilities", 0)

    def _growth(curr, prev):
        if prev == 0:
            return None
        return round((curr / prev - 1) * 100, 1)

    def _ratio(num, den):
        if den == 0:
            return None
        return round(num / den * 100, 1)

    out = {
        "revenue": rev,
        "operating_income": op,
        "net_income": ni,
        "revenue_growth_pct": _growth(rev, rev_prev),
        "op_growth_pct": _growth(op, op_prev),
        "ni_growth_pct": _growth(ni, ni_prev),
        "operating_margin_pct": _ratio(op, rev) if rev else None,
        "net_margin_pct": _ratio(ni, rev) if rev else None,
        "roe_pct": _ratio(ni, eq) if eq else None,  # 분기 ROE 근사
        "debt_ratio_pct": _ratio(li, eq) if eq else None,
        "equity": eq,
        "liabilities": li,
        "assets": metrics.get("assets", 0),
        "report_period": period_label,
        "fetched_at": _t.time(),
    }
    _financial_cache[stock_code] = (_t.time(), out)
    return out


# ─── 공시 분류 + 자동 해석 ───────────────────────────────────────
def classify_filing(report_name: str) -> dict:
    """공시명 → 분류 + 자동 해석 + 임팩트 점수.

    점수: -10 (강한 악재) ~ +10 (강한 호재)
    """
    n = report_name or ""

    # 강한 호재
    if "자기주식취득결정" in n or "자사주취득" in n:
        return {"icon": "🟢", "category": "자사주 취득", "impact": 8,
                "interpretation": "자사주 매입 결정 — 주가 부양 의지 + EPS 상승 효과"}
    if "현금배당" in n or "배당결정" in n:
        return {"icon": "🟢", "category": "배당", "impact": 5,
                "interpretation": "배당 지급 결정 — 주주환원 적극"}
    if "유상증자" in n and "결정" in n:
        return {"icon": "🟠", "category": "유상증자", "impact": -5,
                "interpretation": "유상증자 — 신규 자금 유입 vs 지분 희석 (단기 약세 가능)"}
    if "무상증자" in n and "결정" in n:
        return {"icon": "🟢", "category": "무상증자", "impact": 6,
                "interpretation": "무상증자 — 거래 활성화 + 호재로 작용 경향"}
    if "주식분할" in n:
        return {"icon": "🟢", "category": "액면분할", "impact": 4,
                "interpretation": "액면분할 — 거래 접근성 향상, 단기 호재"}
    if "단일판매" in n or "공급계약" in n:
        return {"icon": "🟢", "category": "수주", "impact": 7,
                "interpretation": "대규모 수주/공급계약 — 매출 가시성 향상"}
    if "타법인주식및출자증권취득결정" in n:
        return {"icon": "🟡", "category": "타법인 투자", "impact": 2,
                "interpretation": "타법인 투자 — 사업 확장 시그널"}

    # 강한 악재
    if "감자결정" in n or "자본감소" in n:
        return {"icon": "🔴", "category": "감자", "impact": -8,
                "interpretation": "자본감소 — 주식수 감소지만 부실 신호 가능, 신중 검토"}
    if "회생절차" in n or "파산" in n:
        return {"icon": "🔴", "category": "회생/파산", "impact": -10,
                "interpretation": "⚠️ 회생/파산 신청 — 즉시 매도 검토 강력 권고"}
    if "관리종목" in n or "상장폐지" in n:
        return {"icon": "🔴", "category": "관리/폐지", "impact": -9,
                "interpretation": "⚠️ 관리종목/상장폐지 위험 — 즉시 정리 검토"}
    if "소송" in n and "제기" in n:
        return {"icon": "🔴", "category": "피소", "impact": -3,
                "interpretation": "소송 피소 — 규모 확인 필요, 단기 노이즈"}
    if "제재" in n or "벌금" in n:
        return {"icon": "🔴", "category": "제재", "impact": -4,
                "interpretation": "규제 제재 — 사업 영향 확인 필요"}

    # 중립/정보성
    if "정정" in n or "철회" in n:
        return {"icon": "⚠️", "category": "정정공시", "impact": -1,
                "interpretation": "이전 공시 정정/철회 — 원본 확인 필요"}
    if "전환사채" in n or "신주인수권" in n:
        return {"icon": "🟡", "category": "전환사채/BW", "impact": -3,
                "interpretation": "CB/BW 발행 — 향후 지분 희석 가능성"}
    if "임원" in n and ("매수" in n or "매도" in n):
        return {"icon": "🟡", "category": "임원 매매", "impact": 2 if "매수" in n else -2,
                "interpretation": "임원 매매 — 인사이더 시그널"}
    if "최대주주변경" in n:
        return {"icon": "🟡", "category": "최대주주 변경", "impact": 0,
                "interpretation": "최대주주 변경 — 경영권 변동, 시그널 분석 필요"}
    if "사업보고서" in n or "분기보고서" in n or "반기보고서" in n:
        return {"icon": "📊", "category": "정기보고서", "impact": 0,
                "interpretation": "정기 재무 보고서 — 실적 확인 가능"}

    return {"icon": "📄", "category": "기타", "impact": 0,
            "interpretation": "기타 공시 — 원문 확인 권장"}


# ─── 임원/주요주주 매매 ──────────────────────────────────────────
async def get_insider_trades(stock_code: str, days: int = 30) -> list[dict]:
    """최근 N일 임원·주요주주 소유 변동."""
    cc = await get_corp_code(stock_code)
    if not cc:
        return []
    key = _key()
    if not key:
        return []
    from datetime import timedelta
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{DART_BASE}/elestock.json", params={
                "crtfc_key": key, "corp_code": cc,
                "bgn_de": start, "end_de": end,
            })
            if r.status_code != 200:
                return []
            data = r.json()
            if data.get("status") != "000":
                return []
    except Exception:
        return []

    out = []
    for it in (data.get("list") or [])[:15]:
        out.append({
            "name": it.get("repror"),
            "rel": it.get("isu_exctv_rgist_at"),  # 등록 임원
            "change_qty": _parse_amount(it.get("sp_stock_lmp_cnt", "0")),
            "report_date": it.get("rcept_dt"),
            "reason": it.get("change_on"),
        })
    return out
