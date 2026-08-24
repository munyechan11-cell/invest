"""Korea Investment & Securities (KIS) Open API — KOSPI/KOSDAQ market data.

Shares one OAuth token cache with `quant.brokerage.kis` so a session that both
reads prices and places orders authenticates once. KIS issues short-lived
tokens and rate-limits token requests aggressively, so the cache is mandatory,
not an optimisation.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from decimal import Decimal

import httpx

from quant.core.aio import LazyLock
from quant.core.types import UTC, AssetClass, Bar, Quote, Symbol
from quant.data.calendar import KRX_REGULAR, KST
from quant.data.provider import DataProvider, register_provider

log = logging.getLogger("quant.data.kis")

REAL_HOST = "https://openapi.koreainvestment.com:9443"
MOCK_HOST = "https://openapivts.koreainvestment.com:29443"

_TOKENS: dict[str, tuple[str, float]] = {}
_TOKEN_LOCK = asyncio.Lock()


def kis_host(paper: bool) -> str:
    return MOCK_HOST if paper else REAL_HOST


def _session_closed(label: datetime, now: datetime) -> bool:
    """`label` 이 가리키는 거래일의 정규장이 이미 끝났는가.

    KIS 는 장중에도 "오늘" 행을 실어 줍니다. 그 행의 종가 자리에는 그 순간의
    현재가가 들어 있어서, 09:30 에 읽은 값과 14:00 에 읽은 값이 다릅니다.
    그걸 확정 일봉으로 받으면 같은 봉 위에서 지표가 하루 종일 흔들리고, 신호가
    붙었다 떨어졌다 합니다 — 백테스트에서는 절대 재현되지 않는 방식으로.

    판정 기준이 `봉 시작 + 봉 길이` 가 **아닌** 이유: `stck_bsop_date` 는 UTC
    시각이 아니라 KST 거래일 날짜 라벨이고, 위에서 UTC 자정에 박아 둡니다.
    ccxt·토스가 쓰는 `end_ts <= now` 를 그대로 옮기면 마감이 다음 날 09:00 KST
    로 계산되어, 이미 확정된 당일 종가가 매일 17시간 30분씩 사라집니다.

    휴장일 표는 일부러 보지 않습니다. 표는 해마다 밀리고(`KrxCalendar.
    stale_after`), 표가 밀렸다는 이유로 KIS 가 실제로 준 확정 행을 숨기면
    화면에서 거래일 하나가 통째로 없어집니다. 휴장일에는 애초에 그 날짜 행이
    오지 않으므로 표 없이도 손해가 없습니다.

    **언제 이게 안 통하는가**

    · 주봉: KIS 가 주봉 라벨에 그 주의 첫 거래일을 쓰는지 마지막 거래일을
      쓰는지 실 응답 없이는 확인할 수 없습니다. 라벨이 오늘이면 확실히
      거르지만, 첫 거래일 규칙이라면 주 중반의 미완성 주봉은 그대로 지나갑니다.
      수정 전보다 나빠지지는 않되, 주봉은 여기서 닫히지 않습니다.
    · 종가 단일가(15:20~15:30) 결과가 15:30:00 뒤 몇 초 늦게 실린다면 그 몇 초
      동안 읽은 종가가 나중에 바뀔 수 있습니다. 여유 초를 얼마로 둘지는 실
      응답을 봐야 알 수 있고, 지어낸 여유 초만큼은 확정 종가가 또 가려집니다.
    · 시간외 단일가(15:40~18:00)는 `KRX_REGULAR` 자체가 정규장만 정의하므로
      모델에 없습니다.
    """
    close = datetime.combine(label.date(), KRX_REGULAR.close, tzinfo=KST)
    return now >= close


async def kis_token(app_key: str, app_secret: str, paper: bool) -> str:
    """Fetch-or-reuse an access token. Cached per (key, environment)."""
    cache_key = f"{app_key[:8]}:{paper}"
    cached = _TOKENS.get(cache_key)
    if cached and cached[1] > time.time() + 120:
        return cached[0]
    async with _TOKEN_LOCK:
        cached = _TOKENS.get(cache_key)
        if cached and cached[1] > time.time() + 120:
            return cached[0]
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{kis_host(paper)}/oauth2/tokenP",
                json={"grant_type": "client_credentials",
                      "appkey": app_key, "appsecret": app_secret},
            )
            r.raise_for_status()
            data = r.json()
        token = data["access_token"]
        _TOKENS[cache_key] = (token, time.time() + int(data.get("expires_in", 21600)))
        return token


@register_provider("kis")
class KisProvider(DataProvider):
    """Daily/weekly/monthly candles and L1 quotes for Korean equities."""

    name = "kis"

    _PERIOD = {"1d": "D", "1w": "W"}

    def __init__(
        self,
        app_key: str = "",
        app_secret: str = "",
        paper: bool = True,
        requests_per_second: float = 8.0,
    ):
        self.app_key = app_key or os.environ.get("KIS_APP_KEY", "")
        self.app_secret = app_secret or os.environ.get("KIS_APP_SECRET", "")
        self.paper = paper
        self._client = httpx.AsyncClient(timeout=20)
        self._gap = 1.0 / requests_per_second
        self._next_at = 0.0
        self._lock = LazyLock()
        if not (self.app_key and self.app_secret):
            raise RuntimeError("KIS_APP_KEY / KIS_APP_SECRET are required for the kis provider")

    async def _headers(self, tr_id: str) -> dict:
        token = await kis_token(self.app_key, self.app_secret, self.paper)
        return {
            "authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    async def _get(self, path: str, tr_id: str, params: dict) -> dict:
        async with self._lock:
            wait = self._next_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_at = time.monotonic() + self._gap
        r = await self._client.get(
            f"{kis_host(self.paper)}{path}", headers=await self._headers(tr_id), params=params
        )
        r.raise_for_status()
        data = r.json()
        if str(data.get("rt_cd", "0")) != "0":
            raise RuntimeError(f"KIS {path} error: {data.get('msg1') or data}")
        return data

    async def history(self, symbol, timeframe, start, end):
        period = self._PERIOD.get(timeframe)
        if period is None:
            raise ValueError(f"KIS provider serves {sorted(self._PERIOD)} only, got {timeframe!r}")
        bars: list[Bar] = []
        # The endpoint returns at most ~100 rows per call, so page backwards.
        cursor_end = end
        while cursor_end > start:
            cursor_start = max(start, cursor_end - timedelta(days=140))
            data = await self._get(
                "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                "FHKST03010100",
                {
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": symbol.ticker,
                    "FID_INPUT_DATE_1": cursor_start.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": cursor_end.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": period,
                    "FID_ORG_ADJ_PRC": "0",   # 0 = split/dividend adjusted
                },
            )
            rows = data.get("output2") or []
            if not rows:
                break
            for row in rows:
                raw_date = row.get("stck_bsop_date")
                if not raw_date:
                    continue
                try:
                    ts = datetime.strptime(raw_date, "%Y%m%d").replace(tzinfo=UTC)
                    bars.append(
                        Bar(symbol, ts,
                            float(row["stck_oprc"]), float(row["stck_hgpr"]),
                            float(row["stck_lwpr"]), float(row["stck_clpr"]),
                            float(row.get("acml_vol") or 0), timeframe)
                    )
                except (KeyError, ValueError):
                    continue
            cursor_end = cursor_start - timedelta(days=1)
        # 아직 장이 안 끝난 라벨은 버립니다 — 계약이 "closed bars" 입니다.
        # `end` 가 아니라 `now` 로 판정합니다: `end` 를 과거로 고정한 백테스트는
        # 수정 전과 같은 봉을 받아야 하고, 지금 문제인 것은 "지금 미완성 봉이
        # 나온다" 쪽입니다.
        now = datetime.now(UTC)
        uniq = {b.ts: b for b in bars
                if start <= b.ts < end and _session_closed(b.ts, now)}
        return [uniq[k] for k in sorted(uniq)]

    async def quote(self, symbol):
        try:
            data = await self._get(
                "/uapi/domestic-stock/v1/quotations/inquire-price",
                "FHKST01010100",
                {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol.ticker},
            )
        except Exception as exc:
            log.debug("kis quote failed for %s: %s", symbol.ticker, exc)
            return None
        out = data.get("output") or {}
        price = float(out.get("stck_prpr") or 0)
        if price <= 0:
            return None
        # KRW equities trade on a tick ladder; approximate L1 with one tick.
        tick = float(korean_tick_size(price))
        return Quote(symbol, datetime.now(UTC), price - tick, price + tick)

    async def resolve(self, ticker: str):
        code = "".join(ch for ch in ticker if ch.isdigit()).zfill(6)
        if len(code) != 6:
            return None
        try:
            probe = await self.quote(Symbol(code, venue="kis"))
        except Exception:
            probe = None
        if probe is None:
            return None
        return Symbol(
            code, venue="kis", asset_class=AssetClass.EQUITY, quote_currency="KRW",
            lot_size=1, tick_size=korean_tick_size(probe.mid),
        )

    async def describe(self, ticker: str) -> dict | None:
        """종목코드 하나를 사람이 읽을 수 있는 것으로 바꿉니다.

        한글 종목명은 시세 응답(`hts_kor_isnm`)에 이미 실려 옵니다 — 지금까지
        버리고 있었을 뿐입니다. 화면에 "005930" 만 띄우면 그게 무슨 회사인지
        외운 사람만 쓸 수 있고, 잘못 고르면 다른 회사를 삽니다.
        """
        code = "".join(ch for ch in ticker if ch.isdigit()).zfill(6)
        if len(code) != 6:
            return None
        try:
            data = await self._get(
                "/uapi/domestic-stock/v1/quotations/inquire-price",
                "FHKST01010100",
                {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
            )
        except Exception as exc:
            log.debug("kis describe failed for %s: %s", code, exc)
            return None
        out = data.get("output") or {}
        price = float(out.get("stck_prpr") or 0)
        if price <= 0:
            return None
        return {
            "ticker": code,
            "name": (out.get("hts_kor_isnm") or "").strip(),
            "price": price,
            "change_pct": float(out.get("prdy_ctrt") or 0.0),
            "venue": "kis",
            "currency": "KRW",
            # 상하한가는 국내 시장의 하드 제약입니다. 그 밖의 지정가는 거절됩니다.
            "upper_limit": float(out.get("stck_mxpr") or 0) or None,
            "lower_limit": float(out.get("stck_llam") or 0) or None,
            "tick_size": float(korean_tick_size(price)),
        }

    async def close(self):
        await self._client.aclose()


def korean_tick_size(price: float) -> Decimal:
    """KRX tick ladder (2023 revision). Orders off the ladder are rejected."""
    from decimal import Decimal

    for threshold, tick in (
        (2_000, "1"), (5_000, "5"), (20_000, "10"), (50_000, "50"),
        (200_000, "100"), (500_000, "500"),
    ):
        if price < threshold:
            return Decimal(tick)
    return Decimal("1000")
