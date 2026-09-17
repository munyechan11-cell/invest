"""Korea Investment & Securities (KIS) Open API — 국내 + 해외 시세.

Shares one OAuth token cache with `quant.brokerage.kis` so a session that both
reads prices and places orders authenticates once. KIS issues short-lived
tokens and rate-limits token requests aggressively, so the cache is mandatory,
not an optimisation.

**국내와 해외는 다른 엔드포인트입니다.** 예전에는 `domestic-stock` 만 불렀고,
그래서 주문 어댑터가 해외 모의투자까지 지원하는데도 미국 종목은 시세를 받을
길이 없었습니다 — 한투 키 하나로 미국을 돌리려면 토스나 야후 시세를 따로
붙여야 했고, 야후는 15분 지연이라 "연습에선 됐는데" 를 만드는 자리였습니다.

종목이 **6자리 숫자면 국내, 아니면 해외** 로 봅니다(토스 어댑터와 같은 규칙).

거래소 코드가 **주문 쪽과 시세 쪽이 다릅니다** — 주문은 `NASD/NYSE/AMEX`,
시세는 `NAS/NYS/AMS`. 한 글자 차이라 섞어 쓰면 "없는 종목" 으로 돌아오고,
그 답은 티커가 틀린 것과 구별되지 않습니다.

⚠️ **모의투자 도메인이 해외 시세를 주는지는 실호출로 확인해야 합니다.**
한투는 환경마다 제공 범위가 다르고, 저장소 안에서는 확인할 수 없습니다.
못 주면 여기서 `RuntimeError` 가 나며 그 사실을 말합니다 — 조용히 0 을
돌려주지 않습니다.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from datetime import datetime, timedelta
from decimal import Decimal

import httpx

from quant.core.aio import LazyLock
from quant.core.types import UTC, AssetClass, Bar, Quote, Symbol, krx_tick_size
from quant.data.provider import DataProvider, register_provider

log = logging.getLogger("quant.data.kis")

#: 시세용 거래소 코드 ← 주문용 코드. 한 글자씩 다릅니다.
OVERSEAS_QUOTE_EXCHANGE = {"NASD": "NAS", "NAS": "NAS", "NYSE": "NYS",
                           "NYS": "NYS", "AMEX": "AMS", "AMS": "AMS"}
#: 종목이 어느 거래소인지 응답이 말해 주지 않으므로 순서대로 물어봅니다.
#: 한 번 맞은 곳은 기억합니다 — 종목마다 매번 세 번 부를 이유가 없습니다.
OVERSEAS_SEARCH_ORDER = ("NAS", "NYS", "AMS")

REAL_HOST = "https://openapi.koreainvestment.com:9443"
MOCK_HOST = "https://openapivts.koreainvestment.com:29443"

_TOKENS: dict[bytes, tuple[str, float]] = {}
#: `LazyLock` 이어야 합니다. 이 줄은 **모듈을 import 하는 순간** 실행되는데,
#: 그 시점에 도는 이벤트 루프가 있다는 보장이 없습니다. 맨 `asyncio.Lock()` 은
#: 그때 루프를 붙잡으려 하고, 없으면 `RuntimeError: There is no current event
#: loop` 로 **import 자체가 실패** 합니다 — 봇을 세우는 `build_engine` 안에서
#: 터지므로 사용자에게는 "시작이 안 된다" 로만 보입니다. 있더라도 나중에 실제로
#: 도는 루프와 다른 루프에 묶이면 잠금이 아무것도 지키지 못합니다.
#: `quant/core/aio.py` 가 존재하는 이유가 이것이고, 토스 어댑터는 같은 자리에서
#: 이미 `LazyLock` 을 씁니다.
_TOKEN_LOCK = LazyLock()


def kis_host(paper: bool) -> str:
    return MOCK_HOST if paper else REAL_HOST


async def kis_token(app_key: str, app_secret: str, paper: bool) -> str:
    """Fetch-or-reuse an access token. Cached per (key, environment)."""
    # KIS app keys share provider-controlled prefixes. Prefix-only caching lets
    # one user's bearer token authenticate another user's account requests.
    # Include the complete credential pair and environment without retaining
    # either plaintext value as a dictionary key.
    cache_key = hashlib.sha256(
        app_key.encode("utf-8") + b"\0" + app_secret.encode("utf-8")
        + (b"\1" if paper else b"\0")
    ).digest()
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
    """일·주봉과 L1 호가 — 국내(KOSPI/KOSDAQ)와 해외(미국) 둘 다."""

    name = "kis"

    _PERIOD = {"1d": "D", "1w": "W"}

    def __init__(
        self,
        app_key: str = "",
        app_secret: str = "",
        paper: bool = False,
        requests_per_second: float = 8.0,
        overseas_exchange: str = "NASD",
        allow_env_credentials: bool = True,
    ):
        self.app_key = (app_key or os.environ.get("KIS_APP_KEY", "")
                        if allow_env_credentials else app_key)
        self.app_secret = (app_secret or os.environ.get("KIS_APP_SECRET", "")
                           if allow_env_credentials else app_secret)
        #: 시세는 **실계좌 호스트에만** 있습니다. 모의투자 호스트는 일봉
        #: 창구(`inquire-daily-itemchartprice`)에 500 을 돌려줍니다 — 현재가는
        #: 오는데 과거 봉만 안 옵니다. 그래서 기본값이 `True` 이던 시절에는
        #: `paper` 를 안 적은 설정이 조용히 시세 없는 문을 두드렸고, 돌아온
        #: 답은 "500" 이라 키가 틀린 것처럼 보였습니다.
        #:
        #: 환경변수 되돌림이 `KIS_APP_KEY`(실계좌 이름)를 읽는 것도 같은
        #: 사실을 이미 말하고 있었습니다 — 기본 호스트만 반대였습니다.
        self.paper = paper
        #: 해외 종목을 어느 거래소부터 찾아볼 것인가. 주문 어댑터의
        #: `overseas_exchange` 와 같은 값을 넣으면 시세와 주문이 같은 곳을
        #: 봅니다 — 다른 곳을 보면 "없는 종목" 과 "티커 오타" 가 구별되지
        #: 않습니다.
        self.overseas_exchange = OVERSEAS_QUOTE_EXCHANGE.get(
            str(overseas_exchange or "NASD").upper(), "NAS")
        #: 티커 → 맞았던 거래소. 종목마다 매번 세 번 부를 이유가 없습니다.
        self._exchange_of: dict[str, str] = {}
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

    # ── 국내인가 해외인가 ────────────────────────────────────────────
    @staticmethod
    def _domestic_code(ticker: str) -> str:
        """국내 종목코드, 해외면 빈 문자열.

        6자리 숫자면 국내입니다(토스 어댑터와 같은 규칙). 예전에는 어떤
        문자열이든 숫자만 뽑아 `zfill(6)` 했는데, 그러면 `AAPL` 이 `000000`
        이 되어 **없는 국내 종목을 조회** 하고 "그런 종목 없음" 으로 끝납니다.
        답은 맞지만 이유가 틀렸고, 그 이유는 화면에 안 나옵니다.
        """
        code = str(ticker or "").strip().upper()
        return code if code.isdigit() and len(code) == 6 else ""

    async def _overseas(self, path: str, tr_id: str, ticker: str,
                        extra: dict | None = None) -> tuple[dict, str]:
        """해외 조회 한 번. `(응답, 맞았던 거래소)`.

        어느 거래소인지 응답이 말해 주지 않으므로 순서대로 물어봅니다. 한 번
        맞은 곳은 기억해서 다음부터 바로 갑니다 — 종목마다 매번 세 번 부르면
        초당 호출 한도를 그것만으로 씁니다.
        """
        first = self._exchange_of.get(ticker) or self.overseas_exchange
        order = [first] + [x for x in OVERSEAS_SEARCH_ORDER if x != first]
        last: Exception | None = None
        for exchange in order:
            params = {"AUTH": "", "EXCD": exchange, "SYMB": ticker}
            params.update(extra or {})
            try:
                data = await self._get(path, tr_id, params)
            except Exception as exc:          # noqa: BLE001 — 다음 거래소를 봅니다
                last = exc
                continue
            if self._overseas_has_rows(data):
                self._exchange_of[ticker] = exchange
                return data, exchange
        if last is not None:
            # 기억해 둔 거래소가 오류를 내면 그 기억은 더 이상 맞지 않습니다.
            # 남겨 두면 다음 호출도 같은 곳부터 갑니다.
            self._exchange_of.pop(ticker, None)
            # 전부 오류면 종목 문제가 아니라 **창구 문제** 입니다. 모의투자
            # 도메인이 해외 시세를 안 주는 경우가 여기로 옵니다.
            # 어느 문을 두드렸는지 말해야 합니다. 예전에는 `paper` 값과
            # 무관하게 "모의투자 환경이…" 라고 적혀 있어서, 실계좌로 실패한
            # 사람에게 있지도 않은 원인을 가리켰습니다.
            hint = ("모의투자 호스트에는 시세 창구가 거의 없습니다 — 시세는 "
                    "실계좌 키로 받으세요"
                    if self.paper else
                    "해당 앱에 해외주식 시세 조회 권한이 있는지, 티커가 맞는지 "
                    "확인하세요")
            raise RuntimeError(
                f"KIS 해외 시세를 읽지 못했습니다 ({ticker}, {path}): {last}. "
                f"{'모의투자' if self.paper else '실계좌'} 환경입니다 — {hint}"
            ) from last
        return {}, ""

    @staticmethod
    def _overseas_has_rows(data: dict) -> bool:
        out = data.get("output") or {}
        if isinstance(out, dict) and str(out.get("last") or "").strip() not in ("", "0"):
            return True
        return bool(data.get("output2"))

    @staticmethod
    def _num(row: dict, key: str) -> float | None:
        """숫자 하나. 못 읽으면 **0 이 아니라 None** 입니다.

        0 은 "없음" 이지 "모름" 이 아닙니다. 못 읽은 종가를 0 으로 돌려주면
        그 봉이 지표에 들어가 전략이 폭락으로 읽습니다.
        """
        raw = row.get(key)
        if raw in (None, ""):
            return None
        try:
            return float(str(raw).replace(",", ""))
        except (TypeError, ValueError):
            return None

    async def history(self, symbol, timeframe, start, end):
        if not self._domestic_code(symbol.ticker):
            return await self._overseas_history(symbol, timeframe, start, end)
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
        # KIS may include today's still-forming daily row.  The provider
        # contract is closed bars, so compare the candle *end*, not only its
        # open date, before exposing it to a live strategy.
        uniq = {b.ts: b for b in bars if start <= b.ts and b.end_ts <= end}
        return [uniq[k] for k in sorted(uniq)]

    async def quote(self, symbol):
        if not self._domestic_code(symbol.ticker):
            return await self._overseas_quote(symbol)
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

    # ── 해외 ─────────────────────────────────────────────────────────
    _OVERSEAS_PERIOD = {"1d": "0", "1w": "1"}

    async def _overseas_history(self, symbol, timeframe, start, end):
        """해외 기간별시세. 한 번에 ~100행이라 뒤로 넘기며 읽습니다."""
        period = self._OVERSEAS_PERIOD.get(timeframe)
        if period is None:
            raise ValueError(
                f"KIS 해외 시세는 {sorted(self._OVERSEAS_PERIOD)} 만 됩니다 "
                f"(받은 값: {timeframe!r})"
            )
        bars: list[Bar] = []
        cursor = end
        seen: set[str] = set()
        while cursor > start:
            try:
                data, _exchange = await self._overseas(
                    "/uapi/overseas-price/v1/quotations/dailyprice",
                    "HHDFS76240000", symbol.ticker,
                    {"GUBN": period, "BYMD": cursor.strftime("%Y%m%d"), "MODP": "1"},
                )
            except RuntimeError:
                # **첫 장이 실패하면 말하고, 뒤로 넘기다 실패하면 거기까지.**
                # 상장일 이전을 물으면 창구가 오류로 답할 수 있는데, 그건
                # 그 종목의 역사가 끝난 지점이지 고장이 아닙니다. 반대로 한
                # 줄도 못 읽었으면 그건 "거래가 없었다" 가 아닙니다.
                if not bars:
                    raise
                log.debug("kis 해외 일봉 페이지 중단 %s @%s", symbol.ticker, cursor)
                break
            rows = data.get("output2") or []
            if not rows:
                break
            oldest = cursor
            fresh = 0
            for row in rows:
                day = str(row.get("xymd") or "").strip()
                if not day or day in seen:
                    continue
                values = [self._num(row, k) for k in ("open", "high", "low", "clos")]
                if any(v is None or v <= 0 for v in values):
                    # 못 읽은 값을 0 으로 채우면 그 봉이 폭락으로 보입니다.
                    continue
                try:
                    ts = datetime.strptime(day, "%Y%m%d").replace(tzinfo=UTC)
                except ValueError:
                    continue
                seen.add(day)
                fresh += 1
                oldest = min(oldest, ts)
                bars.append(Bar(symbol, ts, *values,
                                self._num(row, "tvol") or 0.0, timeframe))
            if not fresh:
                break            # 같은 장만 되돌아오면 무한히 돕니다
            cursor = oldest - timedelta(days=1)
        uniq = {b.ts: b for b in bars if start <= b.ts and b.end_ts <= end}
        return [uniq[k] for k in sorted(uniq)]

    async def _overseas_quote(self, symbol):
        try:
            data, _exchange = await self._overseas(
                "/uapi/overseas-price/v1/quotations/price",
                "HHDFS00000300", symbol.ticker,
            )
        except Exception as exc:
            log.debug("kis 해외 호가 실패 %s: %s", symbol.ticker, exc)
            return None
        out = data.get("output") or {}
        price = self._num(out, "last")
        if not price or price <= 0:
            return None
        # 미국 주식의 호가단위는 가격과 무관하게 $0.01 입니다.
        tick = 0.01
        return Quote(symbol, datetime.now(UTC), price - tick, price + tick)

    async def resolve(self, ticker: str):
        code = self._domestic_code(ticker)
        if not code:
            return await self._resolve_overseas(ticker)
        try:
            probe = await self.quote(Symbol(code, venue="kis"))
        except Exception:
            probe = None
        if probe is None:
            return None
        return Symbol(
            code, venue="kis", asset_class=AssetClass.EQUITY, quote_currency="KRW",
            lot_size=1, tick_size=korean_tick_size(probe.mid),
            # 한 번 잰 틱을 고정하지 않습니다 — 가격대가 바뀌면 격자도 바뀝니다.
            tick_ladder="krx",
        )

    async def _resolve_overseas(self, ticker: str):
        code = str(ticker or "").strip().upper()
        if not code or not code.replace(".", "").replace("-", "").isalnum():
            return None
        try:
            probe = await self.quote(Symbol(code, venue="kis", quote_currency="USD"))
        except Exception:
            probe = None
        if probe is None:
            return None
        # 미국 주식은 가격과 무관하게 $0.01 이고 상하한가가 없습니다 —
        # 사다리를 켜면 국내 격자가 미국 종목에 걸립니다.
        return Symbol(
            code, venue="kis", asset_class=AssetClass.EQUITY, quote_currency="USD",
            lot_size=1, tick_size=Decimal("0.01"),
        )

    async def describe(self, ticker: str) -> dict | None:
        """종목코드 하나를 사람이 읽을 수 있는 것으로 바꿉니다.

        한글 종목명은 시세 응답(`hts_kor_isnm`)에 이미 실려 옵니다 — 지금까지
        버리고 있었을 뿐입니다. 화면에 "005930" 만 띄우면 그게 무슨 회사인지
        외운 사람만 쓸 수 있고, 잘못 고르면 다른 회사를 삽니다.
        """
        code = self._domestic_code(ticker)
        if not code:
            return await self._describe_overseas(ticker)
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

    async def _describe_overseas(self, ticker: str) -> dict | None:
        code = str(ticker or "").strip().upper()
        if not code:
            return None
        try:
            data, exchange = await self._overseas(
                "/uapi/overseas-price/v1/quotations/price",
                "HHDFS00000300", code,
            )
        except Exception as exc:
            log.debug("kis 해외 종목정보 실패 %s: %s", code, exc)
            return None
        out = data.get("output") or {}
        price = self._num(out, "last")
        if not price or price <= 0:
            return None
        return {
            "ticker": code,
            # 해외 현재가 응답에는 종목명이 없습니다. 티커를 이름 자리에
            # 넣으면 "증권사가 이 종목의 이름을 이렇게 준다" 는 뜻이 되므로
            # 비워 두고, 부르는 쪽이 자기 표로 물러설 수 있게 합니다.
            "name": "",
            "price": price,
            "change_pct": self._num(out, "rate") or 0.0,
            "venue": "kis",
            "currency": "USD",
            "market": exchange,
            # 미국은 상하한가가 없습니다. 0 을 넣으면 화면이 "상한가 0원" 을
            # 그립니다 — 없는 것과 0 은 다릅니다.
            "upper_limit": None,
            "lower_limit": None,
            "tick_size": 0.01,
        }

    async def close(self):
        await self._client.aclose()


def korean_tick_size(price: float) -> Decimal:
    """KRX tick ladder (2023 revision). Orders off the ladder are rejected.

    표는 `quant.core.types` 한 곳에 있습니다 — 주문 격자(`Symbol.round_price`)와
    화면에 뜨는 호가단위가 다른 표를 읽으면, 화면은 맞는데 주문만 거절되는
    상태가 되고 그 둘이 다르다는 사실은 아무 데도 나타나지 않습니다.
    """
    return krx_tick_size(price)
