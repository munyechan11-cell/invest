"""실시간 뉴스 - Finnhub 우선, Alpaca News 백업."""
from __future__ import annotations
import os
from datetime import datetime, timedelta, timezone
import httpx

FINNHUB = "https://finnhub.io/api/v1"


async def fetch_news(symbol: str, days: int = 3, limit: int = 8) -> list[dict]:
    from .market import market_of
    if market_of(symbol) == "KR":
        from .news_kr import fetch_news_kr
        return await fetch_news_kr(symbol, limit=limit)
    symbol = symbol.upper()
    key = os.environ.get("FINNHUB_API_KEY")
    items: list[dict] = []

    if key:
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=days)
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{FINNHUB}/company-news", params={
                "symbol": symbol,
                "from": start.isoformat(),
                "to": end.isoformat(),
                "token": key,
            })
            if r.status_code == 200:
                for n in r.json()[:limit]:
                    items.append({
                        "headline": n.get("headline", ""),
                        "summary": (n.get("summary") or "")[:400],
                        "source": n.get("source", ""),
                        "url": n.get("url", ""),
                        "ts": datetime.fromtimestamp(
                            n.get("datetime", 0), tz=timezone.utc
                        ).isoformat(),
                    })

    # Alpaca News 백업
    if not items:
        ak = os.environ.get("ALPACA_API_KEY")
        sk = os.environ.get("ALPACA_SECRET_KEY")
        if ak and sk:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(
                    "https://data.alpaca.markets/v1beta1/news",
                    params={"symbols": symbol, "limit": limit},
                    headers={"APCA-API-KEY-ID": ak, "APCA-API-SECRET-KEY": sk},
                )
                if r.status_code == 200:
                    for n in r.json().get("news", []):
                        items.append({
                            "headline": n.get("headline", ""),
                            "summary": (n.get("summary") or "")[:400],
                            "source": n.get("source", ""),
                            "url": n.get("url", ""),
                            "ts": n.get("created_at", ""),
                        })

    # 미국 뉴스 → 한국어 번역 (Gemini 사용, 실패 시 원문)
    if items:
        try:
            from .translate import translate_news_to_korean
            items = await translate_news_to_korean(items)
        except Exception:
            pass  # 번역 실패해도 원문 그대로 반환

    return items


async def fetch_market_flow(symbol: str) -> dict:
    from .market import market_of
    if market_of(symbol) == "KR":
        # KIS 투자자별 매매동향은 market_kr에서 snapshot에 이미 포함됨.
        # 여기서는 추가 메타만 비워서 반환.
        return {"market": "KR"}
    return await _fetch_market_flow_us(symbol)


async def _fetch_market_flow_us(symbol: str) -> dict:
    """기관/애널리스트 권고 + 어닝 캘린더 + 인사이더 거래 (Finnhub 무료)."""
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return {}
    sym = symbol.upper()
    out: dict = {}
    from datetime import datetime, timedelta

    async with httpx.AsyncClient(timeout=15) as c:
        # 1) 애널리스트 추천 분포
        rec = await c.get(f"{FINNHUB}/stock/recommendation",
                          params={"symbol": sym, "token": key})
        if rec.status_code == 200 and rec.json():
            latest = rec.json()[0]
            sb = int(latest.get("strongBuy") or 0)
            b = int(latest.get("buy") or 0)
            h = int(latest.get("hold") or 0)
            s = int(latest.get("sell") or 0)
            ss = int(latest.get("strongSell") or 0)
            total = sb + b + h + s + ss
            if total > 0:
                buy_pct = (sb + b) / total * 100
                if buy_pct >= 70:
                    consensus = "강력 매수"
                elif buy_pct >= 50:
                    consensus = "매수"
                elif buy_pct >= 30:
                    consensus = "보유"
                else:
                    consensus = "매도"
                out["analyst_consensus"] = {
                    "strong_buy": sb, "buy": b, "hold": h, "sell": s, "strong_sell": ss,
                    "total": total, "buy_pct": round(buy_pct, 1),
                    "consensus": consensus, "period": latest.get("period"),
                }

        # 2) 애널리스트 목표가
        pt = await c.get(f"{FINNHUB}/stock/price-target",
                         params={"symbol": sym, "token": key})
        if pt.status_code == 200:
            d = pt.json() or {}
            if d.get("targetMean"):
                out["price_target"] = {
                    "high": d.get("targetHigh"),
                    "low": d.get("targetLow"),
                    "mean": d.get("targetMean"),
                    "median": d.get("targetMedian"),
                    "last_updated": d.get("lastUpdated"),
                }

        # 3) 다음 어닝 발표일 (90일 이내)
        today = datetime.utcnow().date()
        end = today + timedelta(days=90)
        ec = await c.get(f"{FINNHUB}/calendar/earnings", params={
            "symbol": sym, "from": today.isoformat(),
            "to": end.isoformat(), "token": key,
        })
        if ec.status_code == 200:
            items = (ec.json() or {}).get("earningsCalendar") or []
            if items:
                nxt = items[0]
                date_str = nxt.get("date")
                if date_str:
                    try:
                        d_dt = datetime.strptime(date_str, "%Y-%m-%d").date()
                        days_until = (d_dt - today).days
                        out["earnings_next"] = {
                            "date": date_str,
                            "days_until": days_until,
                            "eps_estimate": nxt.get("epsEstimate"),
                            "revenue_estimate": nxt.get("revenueEstimate"),
                            "year": nxt.get("year"),
                            "quarter": nxt.get("quarter"),
                            "hour": nxt.get("hour"),  # bmo/amc
                        }
                    except Exception:
                        pass

        # 4) 인사이더 거래 (기존 유지)
        ins = await c.get(f"{FINNHUB}/stock/insider-transactions",
                          params={"symbol": sym, "token": key})
        if ins.status_code == 200:
            data = (ins.json() or {}).get("data", [])[:5]
            out["insider_recent"] = [
                {"name": x.get("name"), "share": x.get("share"),
                 "change": x.get("change"), "date": x.get("transactionDate")}
                for x in data
            ]
    return out


async def fetch_profile(symbol: str) -> dict:
    """기업 프로필(섹터/산업/시총 등). 한국주식은 KR 메타로 위임."""
    from .market import market_of
    if market_of(symbol) == "KR":
        from .news_kr import fetch_profile_kr
        return await fetch_profile_kr(symbol)
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return {}
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{FINNHUB}/stock/profile2",
                        params={"symbol": symbol.upper(), "token": key})
        return r.json() if r.status_code == 200 else {}
