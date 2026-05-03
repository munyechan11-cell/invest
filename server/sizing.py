"""포지션 사이징 — 사용자 입력 자금을 최대한 활용 + 리스크 정보 표시."""
from __future__ import annotations
import math


def shares_for(capital: float, risk_pct: float, entry: float, stop: float) -> dict:
    """사용자 입력한 capital을 최대한 활용해 매수 가능 주식수 산출.

    이전 동작: min(risk_budget/risk_per_share, capital/entry) → 사용자 자금 일부만 사용 (혼란)
    새 동작: 기본은 capital을 최대 활용. 실제 리스크가 사용자 한도 초과 시 경고만 표시.

    Args:
        capital: 사용자가 입력한 투자금
        risk_pct: 리스크 한도 % (참고 + 경고용)
        entry: 진입가
        stop: 손절가 (없으면 entry * 0.98 가정)
    """
    if entry <= 0 or capital <= 0:
        return {"shares": 0, "max_loss": 0, "notional": 0,
                "risk_per_share": 0, "actual_risk_pct": 0,
                "user_risk_pct": risk_pct, "warning": None}

    # 사용자 자금으로 살 수 있는 최대 주식수 (소수점 버림)
    shares = math.floor(capital / entry)

    # 손절 시 1주당 손실액
    risk_per_share = max(abs(entry - stop), entry * 0.005) if stop else entry * 0.02

    # 실제 발생 가능한 최대 손실 + 그 비율
    max_loss = shares * risk_per_share
    actual_risk_pct = (max_loss / capital * 100) if capital > 0 else 0

    # 사용자 설정 한도 초과 시 경고 + 안전 옵션 제안
    warning = None
    safe_shares = None
    if actual_risk_pct > risk_pct:
        # 사용자 리스크 한도 내에서 살 수 있는 최대 주식수
        risk_budget = capital * (risk_pct / 100.0)
        safe_shares = math.floor(risk_budget / risk_per_share) if risk_per_share > 0 else 0
        warning = (
            f"⚠️ 실제 리스크 {actual_risk_pct:.1f}%가 설정 한도 {risk_pct}%를 초과합니다. "
            f"안전을 원하면 {safe_shares}주만 매수 (리스크 정확히 {risk_pct}%)."
        )

    return {
        "shares": shares,
        "notional": round(shares * entry, 2),    # 실제 투입액 (capital과 거의 일치)
        "max_loss": round(max_loss, 2),
        "risk_per_share": round(risk_per_share, 2),
        "actual_risk_pct": round(actual_risk_pct, 2),
        "user_risk_pct": risk_pct,
        "safe_shares": safe_shares,               # 리스크 한도 내 권장 주식수
        "capital_input": round(capital, 2),       # 사용자 원본 입력
        "warning": warning,
    }


def split_plan(shares: int, splits: int = 3) -> list[int]:
    """분할 매수 청크. 3분할 기본."""
    if shares <= 0 or splits <= 1:
        return [shares] if shares else []
    base = shares // splits
    rem = shares - base * splits
    plan = [base] * splits
    for i in range(rem):
        plan[i] += 1
    return [x for x in plan if x > 0]
