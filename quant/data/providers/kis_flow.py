"""KIS investor-flow provider — 국내주식 투자자별 매매동향 + 프로그램매매.

Two endpoints, merged by session date:

  inquire-investor            개인 / 외국인 / 기관계 순매수 (수량 + 대금)
  program-trade-by-stock-daily  프로그램 순매수 (수량 + 대금)

Session volume is filled from the daily chart, because participation ratios —
"how much of today's tape was foreign net buying" — are the only form of these
numbers that is comparable across a ₩600tn large cap and a small cap.

The program-trade endpoint is treated as optional: it is not enabled on every
KIS account tier, and losing it should degrade the signal, not break the run.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta

import httpx

from quant.core.aio import LazyLock
from quant.core.types import UTC, Symbol
from quant.data.flow import FlowProvider, InvestorFlow, register_flow_provider
from quant.data.providers.kis import kis_host, kis_token

log = logging.getLogger("quant.data.kis_flow")


def _f(row: dict, *keys: str) -> float:
    """First parseable numeric field among `keys`. KIS renames fields between
    endpoints and occasionally returns '' for a zero."""
    for k in keys:
        raw = row.get(k)
        if raw in (None, ""):
            continue
        try:
            return float(str(raw).replace(",", ""))
        except ValueError:
            continue
    return 0.0


@register_flow_provider("kis")
class KisFlowProvider(FlowProvider):
    name = "kis_flow"

    def __init__(self, app_key: str = "", app_secret: str = "", paper: bool = True,
                 requests_per_second: float = 6.0, include_program: bool = True,
                 timeout: float = 20.0, allow_env_credentials: bool = True):
        self.app_key = (app_key or os.environ.get("KIS_APP_KEY", "")
                        if allow_env_credentials else app_key)
        self.app_secret = (app_secret or os.environ.get("KIS_APP_SECRET", "")
                           if allow_env_credentials else app_secret)
        self.paper = paper
        self.include_program = include_program
        self._client = httpx.AsyncClient(timeout=timeout)
        self._gap = 1.0 / requests_per_second
        self._next_at = 0.0
        self._lock = LazyLock()
        self._program_supported = include_program
        if not (self.app_key and self.app_secret):
            raise RuntimeError("KIS_APP_KEY / KIS_APP_SECRET are required for kis_flow")

    async def _get(self, path: str, tr_id: str, params: dict) -> dict:
        async with self._lock:
            wait = self._next_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_at = time.monotonic() + self._gap
        token = await kis_token(self.app_key, self.app_secret, self.paper)
        r = await self._client.get(
            f"{kis_host(self.paper)}{path}",
            headers={
                "authorization": f"Bearer {token}",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
                "tr_id": tr_id,
                "custtype": "P",
            },
            params=params,
        )
        r.raise_for_status()
        data = r.json()
        if str(data.get("rt_cd", "0")) != "0":
            raise RuntimeError(f"KIS {path}: {data.get('msg1') or data}")
        return data

    # ── individual feeds ─────────────────────────────────────────────────
    async def _investor(self, symbol: Symbol) -> dict[datetime, dict]:
        """개인/외국인/기관 순매수. Returns roughly the last 30 sessions."""
        data = await self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-investor",
            "FHKST01010900",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol.ticker},
        )
        out: dict[datetime, dict] = {}
        for row in data.get("output") or []:
            raw_date = row.get("stck_bsop_date")
            if not raw_date:
                continue
            try:
                ts = datetime.strptime(raw_date, "%Y%m%d").replace(tzinfo=UTC)
            except ValueError:
                continue
            out[ts] = {
                "retail_qty": _f(row, "prsn_ntby_qty"),
                "foreign_qty": _f(row, "frgn_ntby_qty"),
                "institution_qty": _f(row, "orgn_ntby_qty"),
                # KIS reports net value in units of ₩1,000 (천원)
                "retail_value": _f(row, "prsn_ntby_tr_pbmn") * 1_000,
                "foreign_value": _f(row, "frgn_ntby_tr_pbmn") * 1_000,
                "institution_value": _f(row, "orgn_ntby_tr_pbmn") * 1_000,
                "close": _f(row, "stck_clpr"),
            }
        return out

    async def _program(self, symbol: Symbol) -> dict[datetime, dict]:
        if not self._program_supported:
            return {}
        try:
            data = await self._get(
                "/uapi/domestic-stock/v1/quotations/program-trade-by-stock-daily",
                "FHPPG04650201",
                {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol.ticker},
            )
        except Exception as exc:
            # Not available on every account tier — degrade, do not fail.
            log.info("program-trade feed unavailable (%s); continuing without it", exc)
            self._program_supported = False
            return {}
        out: dict[datetime, dict] = {}
        for row in data.get("output") or []:
            raw_date = row.get("stck_bsop_date")
            if not raw_date:
                continue
            try:
                ts = datetime.strptime(raw_date, "%Y%m%d").replace(tzinfo=UTC)
            except ValueError:
                continue
            out[ts] = {
                "program_qty": _f(row, "whol_ntby_qty", "ntby_qty"),
                "program_value": _f(row, "whol_ntby_tr_pbmn", "ntby_tr_pbmn") * 1_000,
            }
        return out

    async def _volumes(self, symbol: Symbol, start: datetime, end: datetime
                       ) -> dict[datetime, tuple[float, float]]:
        """(volume, close) per session, so participation ratios are computable."""
        out: dict[datetime, tuple[float, float]] = {}
        cursor_end = end
        while cursor_end > start:
            cursor_start = max(start, cursor_end - timedelta(days=140))
            try:
                data = await self._get(
                    "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                    "FHKST03010100",
                    {
                        "FID_COND_MRKT_DIV_CODE": "J",
                        "FID_INPUT_ISCD": symbol.ticker,
                        "FID_INPUT_DATE_1": cursor_start.strftime("%Y%m%d"),
                        "FID_INPUT_DATE_2": cursor_end.strftime("%Y%m%d"),
                        "FID_PERIOD_DIV_CODE": "D",
                        "FID_ORG_ADJ_PRC": "0",
                    },
                )
            except Exception as exc:
                log.debug("volume lookup failed for %s: %s", symbol.ticker, exc)
                break
            rows = data.get("output2") or []
            if not rows:
                break
            for row in rows:
                raw_date = row.get("stck_bsop_date")
                if not raw_date:
                    continue
                try:
                    ts = datetime.strptime(raw_date, "%Y%m%d").replace(tzinfo=UTC)
                except ValueError:
                    continue
                out[ts] = (_f(row, "acml_vol"), _f(row, "stck_clpr"))
            cursor_end = cursor_start - timedelta(days=1)
        return out

    # ── FlowProvider interface ───────────────────────────────────────────
    async def flows(self, symbol: Symbol, start: datetime, end: datetime
                    ) -> list[InvestorFlow]:
        investor, program, volumes = await asyncio.gather(
            self._investor(symbol),
            self._program(symbol),
            self._volumes(symbol, start, end),
        )
        if not investor:
            return []

        out: list[InvestorFlow] = []
        for ts in sorted(investor):
            if not (start <= ts < end):
                continue
            row = investor[ts]
            vol, chart_close = volumes.get(ts, (0.0, 0.0))
            out.append(InvestorFlow(
                symbol=symbol,
                ts=ts,
                foreign_qty=row["foreign_qty"],
                institution_qty=row["institution_qty"],
                retail_qty=row["retail_qty"],
                foreign_value=row["foreign_value"],
                institution_value=row["institution_value"],
                retail_value=row["retail_value"],
                close=row["close"] or chart_close,
                volume=vol,
                **program.get(ts, {}),
            ))
        return out

    async def close(self) -> None:
        await self._client.aclose()
