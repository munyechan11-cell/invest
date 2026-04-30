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
    return items


async def fetch_market_flow(symbol: str) -> dict:
    from .market import market_of
    if market_of(symbol) == "KR":
        # KIS 투자자별 매매동향은 market_kr에서 snapshot에 이미 포함됨.
        # 여기서는 추가 메타만 비워서 반환.
        return {"market": "KR"}
    return await _fetch_market_flow_us(symbol)


async def _fetch_market_flow_us(symbol: str) -> dict:
    """기관/애널리스트 권고 등 수급 근사 (다크풀은 무료 소스 부재)."""
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return {}
    out: dict = {}
    async with httpx.AsyncClient(timeout=15) as c:
        rec = await c.get(f"{FINNHUB}/stock/recommendation",
                          params={"symbol": symbol.upper(), "token": key})
        if rec.status_code == 200 and rec.json():
            out["analyst_recommendation"] = rec.json()[0]
        ins = await c.get(f"{FINNHUB}/stock/insider-transactions",
                          params={"symbol": symbol.upper(), "token": key})
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
