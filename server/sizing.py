"""포지션 사이징 — capital × risk% 를 (entry-stop)으로 나눠 주식수 산출."""
from __future__ import annotations
import math


def shares_for(capital: float, risk_pct: float, entry: float, stop: float) -> dict:
    if entry <= 0:
        return {"shares": 0, "max_loss": 0, "notional": 0, "reason": "invalid entry"}
    risk_per_share = max(abs(entry - stop), entry * 0.005) if stop else entry * 0.02
    risk_budget = capital * (risk_pct / 100.0)
    shares_by_risk = math.floor(risk_budget / risk_per_share)
    shares_by_capital = math.floor(capital / entry)
    shares = max(0, min(shares_by_risk, shares_by_capital))
    return {
        "shares": shares,
        "notional": round(shares * entry, 2),
        "max_loss": round(shares * risk_per_share, 2),
        "risk_per_share": round(risk_per_share, 2),
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
