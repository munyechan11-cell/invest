"""토스증권 Open API 어댑터 — 국내 + 미국 주식.

Toss opened a retail Open API in 2026: OAuth 2.0 client-credentials, one REST
surface covering KRX and US equities, quotes through orders.

**두 가지를 먼저 알아야 합니다.**

1. **모의투자 환경이 없습니다.** KIS는 실계좌와 완전히 분리된 모의투자 호스트를
   주지만 토스는 주지 않습니다. 공식 안내조차 "실 환경에서 소액으로 테스트"라고
   말합니다. 즉 토스에서는 이 엔진의 `dry_run` 모드 — 실시간 시세를 읽되 주문은
   내지 않고 체결을 시뮬레이션 — 가 편의 기능이 아니라 **유일한 안전망**입니다.
   그래서 `live=False`인 동안 이 어댑터는 주문 경로를 아예 밟지 않습니다.

2. **필드명은 검증이 필요합니다.** 아래 엔드포인트와 요청 필드는 공개 가이드
   문서를 근거로 구성했고, 공식 레퍼런스로 대조하지 못했습니다. 틀릴 수 있는
   부분을 `_FIELDS` 한 곳에 모아 두었으니, 실거래 전 반드시 공식 문서와 맞춰
   보고 필요하면 그 딕셔너리만 고치면 됩니다. 추측한 값을 사실처럼 숨겨 두는
   것보다 이렇게 드러내는 편이 낫습니다.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from decimal import Decimal

import httpx

from quant.brokerage.base import BrokerageError
from quant.brokerage.live_base import LiveBrokerage
from quant.core.aio import LazyLock
from quant.core.types import (
    UTC,
    AssetClass,
    Bar,
    Fill,
    Order,
    OrderSide,
    OrderType,
    Quote,
    Symbol,
    timeframe_seconds,
    utcnow,
)
from quant.data.provider import DataProvider, register_provider
from quant.execution.costs import PRESETS, FeeModel, SideAwareFeeModel

log = logging.getLogger("quant.toss")

HOST = "https://openapi.tossinvest.com"

#: 토스 공식 OpenAPI 3.0 문서에서 확인한 값들입니다.
#: https://openapi.tossinvest.com/openapi-docs/latest/openapi.json
#:
#: 이 표는 한때 **추정** 이었고 전부 틀렸습니다. 경로는 `/v1/market/...` 이
#: 아니라 `/api/v1/...` 이고, 토큰은 Basic 헤더가 아니라 body 로 받습니다.
#: 그래서 키가 맞아도 토큰 발급이 403 이었고, 그 위의 모든 것이 따라 죽었습니다.
_FIELDS = {
    "token_path": "/oauth2/token",
    "price_path": "/api/v1/prices",
    "stocks_path": "/api/v1/stocks",
    "candles_path": "/api/v1/candles",
    "orderbook_path": "/api/v1/orderbook",
    "accounts_path": "/api/v1/accounts",
    "holdings_path": "/api/v1/holdings",
    "orders_path": "/api/v1/orders",
    "account_header": "X-Tossinvest-Account",
    # 주문 요청 본문
    "order_symbol": "symbol",
    "order_side": "side",
    "order_qty": "quantity",
    "order_type": "orderType",
    "order_price": "price",
    "side_buy": "BUY",
    "side_sell": "SELL",
    "type_market": "MARKET",
    "type_limit": "LIMIT",
    # 응답
    "order_id": "orderId",
    "filled_qty": "filledQuantity",
    "avg_price": "averagePrice",
}

def _explain(response, what: str) -> str:
    """HTTP 실패를 사람이 읽을 수 있는 한 문장으로.

    `raise_for_status()` 의 메시지는 "403 Forbidden for url ..." 이 전부입니다.
    그것으로는 키가 틀린 것인지, IP 가 막힌 것인지, 권한이 없는 것인지 알 수
    없는데 셋은 고치는 방법이 완전히 다릅니다. 토스는 응답 본문에 이유를
    적어 보내므로, 그걸 버리지 않고 그대로 전합니다.
    """
    body = ""
    try:
        data = response.json()
        # OAuth 오류는 `error` 와 `error_description` 이 짝입니다. 앞의 한
        # 단어(`access_denied`)만 읽으면 무엇이 거부됐는지 알 수 없고, 설명은
        # 대개 뒤쪽에 있습니다.
        parts = [str(data.get(k)) for k in
                 ("error", "error_description", "message", "errorMessage", "detail")
                 if data.get(k)]
        body = " — ".join(dict.fromkeys(parts))[:300] or str(data)[:300]
    except Exception:                       # noqa: BLE001 — 본문이 JSON 이 아닐 때
        body = (response.text or "")[:300]

    if response.status_code == 403:
        return (f"{what} 실패 (403) — {body}\n"
                f"확인할 것을 흔한 순서로: ① 허용 IP — 토스증권 앱 설정 → "
                f"Open API → 허용 IP 관리에 이 서버의 공인 IP 가 있어야 합니다"
                f"(집에서 돌리면 집 IP, 서버에 올리면 서버 IP). ② 키를 재발급한 "
                f"적이 있으면 옛 키는 즉시 죽습니다 — 새 값으로 다시 넣으세요. "
                f"③ Open API 이용 동의와 계좌 상태. 세 가지가 다 맞는데도 "
                f"계속 거부되면 토스증권 고객센터에 이 메시지를 그대로 "
                f"보여주세요 — 계정 쪽 설정입니다.")
    if response.status_code == 401:
        return (f"{what} 실패 (401) — {body}\n"
                f"클라이언트 ID·시크릿이 맞는지 확인하세요.")
    if response.status_code == 429:
        return f"{what} 실패 (429) — 호출이 너무 잦습니다. 잠시 후 다시 시도하세요."
    return f"{what} 실패 ({response.status_code}) — {body}"


_TOKENS: dict[str, tuple[str, float]] = {}
_TOKEN_LOCK = LazyLock()


async def toss_token(client_id: str, client_secret: str,
                     timeout: float = 20.0) -> str:
    """OAuth 2.0 액세스 토큰을 받거나 캐시에서 재사용합니다.

    `client_credentials` 그랜트이고, 자격증명은 **본문**으로 보냅니다 —
    공식 문서의 요구사항입니다.
    """
    cache_key = client_id[:10]
    cached = _TOKENS.get(cache_key)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    async with _TOKEN_LOCK:
        cached = _TOKENS.get(cache_key)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        # 토스는 자격증명을 **본문**으로 받습니다. Basic 헤더로 보내고 본문에
        # grant_type 만 넣으면 client_id 가 없다고 403 이 옵니다 — 키가 맞아도.
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{HOST}{_FIELDS['token_path']}",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials",
                      "client_id": client_id, "client_secret": client_secret},
            )
            if r.status_code >= 400:
                # 응답 본문을 버리면 "403 Forbidden" 만 남고, 그것으로는 키가
                # 틀린 것인지 IP 가 막힌 것인지 알 수 없습니다.
                raise BrokerageError(_explain(r, "토스 토큰 발급"))
            data = r.json()
        token = data.get("access_token")
        if not token:
            raise BrokerageError(f"토스 토큰 응답에 access_token 없음: {str(data)[:200]}")
        _TOKENS[cache_key] = (token, time.time() + int(data.get("expires_in", 3600)))
        return token


class _TossClient:
    """Shared HTTP surface for the data provider and the brokerage."""

    def __init__(self, client_id: str, client_secret: str, account_no: str = "",
                 timeout: float = 20.0, requests_per_second: float = 8.0):
        self.client_id = client_id or os.environ.get("TOSS_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("TOSS_CLIENT_SECRET", "")
        self.account_no = account_no or os.environ.get("TOSS_ACCOUNT_NO", "")
        if not (self.client_id and self.client_secret):
            raise BrokerageError("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 가 필요합니다")
        self._http = httpx.AsyncClient(timeout=timeout)
        self._gap = 1.0 / requests_per_second
        self._next_at = 0.0
        self._lock = LazyLock()

    async def _headers(self, with_account: bool = False) -> dict:
        token = await toss_token(self.client_id, self.client_secret)
        headers = {"Authorization": f"Bearer {token}",
                   "Content-Type": "application/json"}
        if with_account:
            if not self.account_no:
                raise BrokerageError("TOSS_ACCOUNT_NO 가 필요합니다")
            headers[_FIELDS["account_header"]] = self.account_no
        return headers

    async def request(self, method: str, path: str, *, params: dict | None = None,
                      json: dict | None = None, account: bool = False) -> dict:
        async with self._lock:
            wait = self._next_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_at = time.monotonic() + self._gap
        r = await self._http.request(
            method, f"{HOST}{path}", params=params, json=json,
            headers=await self._headers(account),
        )
        if r.status_code >= 400:
            raise BrokerageError(_explain(r, f"토스 {method} {path}"))
        if not r.content:
            return {}
        body = r.json()
        # 토스는 모든 응답을 `{"result": ...}` 로 감쌉니다. 호출부마다 벗기면
        # 한 곳을 빠뜨리고, 빠뜨린 그곳은 빈 목록을 조용히 돌려줍니다.
        return body.get("result", body) if isinstance(body, dict) else body

    async def close(self) -> None:
        await self._http.aclose()


@register_provider("toss")
class TossProvider(DataProvider):
    """국내·미국 주식 시세."""

    name = "toss"

    #: 토스가 실제로 받는 값은 둘뿐입니다(`1m`, `1d`). 예전 표에는 5m·1h·1w 가
    #: 있었는데, 그런 주기를 요청하면 서버가 거절합니다 — 있지도 않은 주기를
    #: 지원한다고 적어 두면 그 설정으로 만든 전략이 시작할 때 죽습니다.
    _INTERVAL = {"1m": "1m", "1d": "1d"}

    #: 종목정보(`/api/v1/stocks`)와 현재가(`/api/v1/prices`)는 둘 다 콤마로
    #: 최대 **200건**을 한 번에 받습니다. 공식 문서가 못 박은 값이고, 넘기면
    #: 400 입니다. 종목마다 한 번씩 부르면 4종목짜리 화면이 4번, 유니버스가
    #: 넓으면 200번이 되고 `STOCK` 레이트 리밋에 걸리는 순간 이름이 **전부**
    #: 사라집니다 — 종목이 많을수록 이름이 필요한데, 하필 그때 다 실패합니다.
    _PER_CALL = 200

    def __init__(self, client_id: str = "", client_secret: str = "",
                 account_no: str = "", **kwargs):
        self.client = _TossClient(client_id, client_secret, account_no, **kwargs)

    async def history(self, symbol, timeframe, start, end):
        interval = self._INTERVAL.get(timeframe)
        if interval is None:
            raise ValueError(
                f"토스는 {sorted(self._INTERVAL)} 주기만 제공합니다 (요청: {timeframe})")

        # 토스 캔들은 기간이 아니라 **개수와 커서** 로 셉니다: 최근 것부터
        # `count` 개를 주고, 더 받으려면 그 마지막 시각을 `before` 로 되돌려
        # 보냅니다. `from`/`to` 는 애초에 받지 않습니다.
        step = timeframe_seconds(timeframe)
        want = max(1, int((end - start).total_seconds() // step) + 2)
        now = datetime.now(UTC)
        seen: set[datetime] = set()
        bars: list[Bar] = []
        before: str | None = None

        while len(bars) < want:
            params = {"symbol": symbol.ticker, "interval": interval,
                      "count": min(200, want - len(bars) + 5), "adjusted": True}
            if before:
                params["before"] = before
            data = await self.client.request("GET", _FIELDS["candles_path"],
                                             params=params)
            rows = data.get("candles") or []
            if not rows:
                break
            for row in rows:
                ts = _parse_ts(row.get("timestamp"))
                if ts is None or ts in seen:
                    continue
                seen.add(ts)
                if not (start <= ts < end):
                    continue
                try:
                    bar = Bar(symbol, ts,
                              float(row["openPrice"]), float(row["highPrice"]),
                              float(row["lowPrice"]), float(row["closePrice"]),
                              float(row.get("volume") or 0), timeframe)
                except (KeyError, TypeError, ValueError):
                    continue
                # 아직 닫히지 않은 봉은 돌려주지 않습니다 — 그걸로 계산한
                # 지표는 다음 틱마다 값이 바뀝니다.
                if bar.end_ts > now:
                    continue
                bars.append(bar)
            nxt = data.get("nextBefore")
            oldest = _parse_ts(rows[-1].get("timestamp"))
            if not nxt or (oldest is not None and oldest < start):
                break
            before = nxt
        bars.sort(key=lambda b: b.ts)
        return bars

    async def quote(self, symbol):
        """호가 최우선 한 단. 호가가 없으면 현재가로 물러섭니다.

        장이 닫혀 있으면 호가가 비어 옵니다. 그때 None 을 돌려주면 봇이
        "시세를 못 받았다" 로 읽고 멈추는데, 실제로는 마지막 체결가가 있고
        그것으로 평가는 됩니다.
        """
        try:
            data = await self.client.request(
                "GET", _FIELDS["orderbook_path"], params={"symbol": symbol.ticker})
            bids, asks = data.get("bids") or [], data.get("asks") or []
            if bids and asks:
                return Quote(symbol, utcnow(),
                             float(bids[0]["price"]), float(asks[0]["price"]),
                             float(bids[0].get("volume") or 0),
                             float(asks[0].get("volume") or 0))
        except Exception as exc:            # noqa: BLE001 — 아래 현재가로 갑니다
            log.debug("토스 호가 조회 실패 %s: %s", symbol.ticker, exc)

        try:
            data = await self.client.request(
                "GET", _FIELDS["price_path"], params={"symbols": symbol.ticker})
        except Exception as exc:            # noqa: BLE001
            log.debug("토스 현재가 조회 실패 %s: %s", symbol.ticker, exc)
            return None
        rows = data if isinstance(data, list) else (data.get("prices") or [])
        if not rows:
            return None
        try:
            last = float(rows[0]["lastPrice"])
        except (KeyError, TypeError, ValueError, IndexError):
            return None
        if last <= 0:
            return None
        # 호가가 없으니 한 틱을 스프레드로 가정합니다. 실제 호가보다 넓거나
        # 좁을 수 있지만, 가격이 아예 없는 것보다는 훨씬 낫습니다.
        tick = float(symbol.tick_size) or last * 0.0005
        return Quote(symbol, utcnow(), last - tick, last + tick)

    async def resolve(self, ticker: str):
        code = ticker.strip().upper()
        try:
            data = await self.client.request(
                "GET", _FIELDS["price_path"], params={"symbol": code})
        except Exception:
            return None
        if not data:
            return None
        krx = code.isdigit() and len(code) == 6
        from quant.data.providers.kis import korean_tick_size

        price = float(data.get("price") or data.get("currentPrice") or 0)
        return Symbol(
            code, venue="toss", asset_class=AssetClass.EQUITY,
            quote_currency="KRW" if krx else "USD",
            lot_size=Decimal("1"),
            tick_size=korean_tick_size(price) if krx else Decimal("0.01"),
        )

    async def describe(self, ticker: str) -> dict | None:
        """종목 하나를 사람이 읽을 수 있는 것으로 바꿉니다.

        `kis.describe` 와 같은 자리이지만 출처가 다릅니다 — 한투는 이름을
        **시세** 응답에 얹어 주고(`hts_kor_isnm`), 토스는 종목정보 API 가
        따로 있습니다. 그래서 토스 쪽은 오늘 한 번도 체결되지 않은 종목,
        장이 닫힌 시각, 거래정지 종목도 이름이 나옵니다. 한투 쪽은 현재가가
        0 이면 아무것도 돌려주지 못합니다.
        """
        found = await self.describe_many([ticker])
        return found.get(_api_symbol(ticker))

    async def describe_many(self, tickers: list[str]) -> dict[str, dict]:
        """여러 종목의 이름·시장·통화를 **한 번에**.

        `GET /api/v1/stocks?symbols=005930,AAPL` 하나로 최대 200건입니다.
        국내 코드와 미국 티커를 섞어도 됩니다.

        토스가 받지 않는 심볼(예: `BTC/USDT` 처럼 `/` 가 든 것)은 아예 빼고
        보냅니다. 하나만 섞여도 요청 전체가 400 이라, 같이 물어본 멀쩡한
        종목들까지 이름을 잃습니다.
        """
        wanted: list[str] = []
        for raw in tickers:
            code = _api_symbol(raw)
            if code and code not in wanted:
                wanted.append(code)
        if not wanted:
            return {}

        rows: list[dict] = []
        for start in range(0, len(wanted), self._PER_CALL):
            chunk = wanted[start:start + self._PER_CALL]
            try:
                data = await self.client.request(
                    "GET", _FIELDS["stocks_path"],
                    params={"symbols": ",".join(chunk)})
            except Exception as exc:        # noqa: BLE001 — 한 묶음이 실패해도
                # 나머지 묶음의 이름은 나와야 합니다.
                log.debug("토스 종목정보 조회 실패 (%d건): %s", len(chunk), exc)
                continue
            rows.extend(data if isinstance(data, list) else (data.get("stocks") or []))
        if not rows:
            return {}

        prices = await self._last_prices([str(r.get("symbol") or "") for r in rows])
        out: dict[str, dict] = {}
        for row in rows:
            info = _stock_info(row, prices)
            if info:
                out[info["ticker"]] = info
        return out

    async def _last_prices(self, codes: list[str]) -> dict[str, float]:
        """현재가 다건. 실패하면 빈 표 — 값은 없어도 **이름은 남습니다**.

        검색 결과에 값이 같이 뜨면 종목을 잘못 고른 것을 그 자리에서 알아챕니다.
        하지만 값을 못 받았다고 이름까지 버리면 고치려던 문제로 되돌아갑니다.
        """
        wanted = [c for c in dict.fromkeys(_api_symbol(c) for c in codes) if c]
        out: dict[str, float] = {}
        for start in range(0, len(wanted), self._PER_CALL):
            chunk = wanted[start:start + self._PER_CALL]
            try:
                data = await self.client.request(
                    "GET", _FIELDS["price_path"],
                    params={"symbols": ",".join(chunk)})
            except Exception as exc:        # noqa: BLE001
                log.debug("토스 현재가 다건 조회 실패 (%d건): %s", len(chunk), exc)
                continue
            for row in (data if isinstance(data, list) else (data.get("prices") or [])):
                code = _api_symbol(row.get("symbol"))
                try:
                    price = float(row.get("lastPrice"))
                except (TypeError, ValueError):
                    continue
                if code and price > 0:
                    out[code] = price
        return out

    async def close(self):
        await self.client.close()


class TossBrokerage(LiveBrokerage):
    """토스증권 주문.

    `live=False` 인 동안 주문 경로는 아예 실행되지 않습니다 — 토스에는 모의투자
    호스트가 없어서, dry-run이 곧 "네트워크에 아무것도 보내지 않는다"를 의미해야
    하기 때문입니다.
    """

    name = "toss"

    def __init__(self, portfolio, client_id: str = "", client_secret: str = "",
                 account_no: str = "", fee_model: FeeModel | None = None, **kwargs):
        super().__init__(portfolio, **kwargs)
        self.client = _TossClient(client_id, client_secret, account_no)
        #: 체결에 물릴 비용. `build_brokerage` 가 `build_costs` 로 만든 **바로 그**
        #: 모델을 그대로 넘겨줍니다. 어댑터가 자기 bps knob 을 따로 들면 같은
        #: 설정의 백테스트와 실거래가 조용히 다른 비용을 물고, `costs.sell_tax_bps`
        #: 처럼 사람이 명시한 값이 실거래에서만 무시됩니다.
        self.fees = fee_model
        #: `fee_model` 이 없을 때만 쓰는 프리셋. 통화별로 한 번씩 만들어 둡니다.
        self._preset_fees: dict[str, FeeModel] = {}
        if self.live:
            log.warning(
                "토스증권 실거래 모드 — 모의투자 환경이 없으므로 모든 주문이 실계좌로 나갑니다. "
                "일일 한도(limits)와 주문 한도(max_order_notional)를 반드시 확인하세요."
            )

    # ── 체결 비용 ────────────────────────────────────────────────────────
    def _fee_model_for(self, symbol: Symbol, side: OrderSide) -> FeeModel:
        """이 체결에 적용할 비용 모델.

        `SideAwareFeeModel` 은 반드시 `for_side` 를 거쳐야 합니다. 그 클래스의
        `fee()` 는 base(위탁수수료)만 계산하므로 그냥 부르면 **매도 거래세가
        통째로 사라집니다** — kr_equity 프리셋 기준 2026년 20bp, 이 결함이
        되찾으려는 금액의 대부분입니다.

        `fee_model` 이 안 넘어온 경로(어댑터를 직접 만든 테스트·스크립트)에서도
        0원이어서는 안 되므로 프리셋으로 물러섭니다. 여기서 요율을 새로 정하지
        않는 것이 중요합니다 — 백테스트에 없는 숫자를 실거래에만 발명하는 순간
        두 장부를 비교할 수 없게 됩니다.
        """
        model = self.fees
        if model is None:
            key = "kr_equity" if symbol.quote_currency == "KRW" else "us_equity"
            model = self._preset_fees.get(key)
            if model is None:
                model = self._preset_fees[key] = PRESETS[key]()[0]
        if isinstance(model, SideAwareFeeModel):
            return model.for_side(side)
        return model

    def _fill_fee(self, order: Order, quantity: Decimal, price: float,
                  when: datetime) -> float:
        """체결 하나에 실제로 물릴 위탁수수료 + (매도면) 증권거래세.

        `when` 은 체결 시각입니다. 한국 거래세는 해마다 요율이 바뀌므로
        (`KRX_SELL_TAX_BPS`) 오늘이 아니라 그 체결의 시각으로 찾아야 합니다.

        **언제 이게 안 통하는가.** 이 값은 설정의 비용 모델이 말하는 *예상*
        청구액이지 토스가 실제로 청구한 금액이 아닙니다. 우대 요율 계좌나
        수수료 면제 이벤트가 걸려 있으면 그만큼 어긋납니다. 토스 공식 스펙은
        주문 조회 응답의 `execution.commission` / `execution.tax` 로 실제
        청구액을 주지만, 그 블록은 이 어댑터가 체결 수량조차 최상위에서 찾고
        있어(`_FIELDS["filled_qty"]`) 같이 손대야 읽을 수 있습니다 — 이 결함의
        범위 밖이라 두었습니다.
        """
        model = self._fee_model_for(order.symbol, order.side)
        # 토스에 **실제로 나간** 호가 유형으로 maker/taker 를 가릅니다.
        # `_venue_submit` 은 LIMIT 이 아닌 것을 전부 MARKET 으로 보내므로,
        # `order.type` 을 그대로 믿으면 시장가로 나간 STOP 주문이 maker 로
        # 매겨집니다. (kr/us 프리셋은 이 인자를 보지 않아 지금은 무해합니다.)
        return model.fee(order.symbol, quantity, price,
                         order.type is OrderType.LIMIT, when)

    async def _venue_submit(self, order: Order) -> str:
        if order.type is OrderType.MARKET and order.symbol.quote_currency != "KRW":
            raise BrokerageError("해외 주식은 지정가만 지원합니다 (시장가 불가)")
        body = {
            _FIELDS["order_symbol"]: order.symbol.ticker,
            _FIELDS["order_side"]: (_FIELDS["side_buy"] if order.side is OrderSide.BUY
                                    else _FIELDS["side_sell"]),
            _FIELDS["order_qty"]: int(order.quantity),
            _FIELDS["order_type"]: (_FIELDS["type_limit"] if order.type is OrderType.LIMIT
                                    else _FIELDS["type_market"]),
        }
        if order.type is OrderType.LIMIT:
            body[_FIELDS["order_price"]] = float(order.limit_price or 0)

        data = await self.client.request("POST", _FIELDS["orders_path"],
                                         json=body, account=True)
        broker_id = str(data.get(_FIELDS["order_id"]) or data.get("id") or "")
        if not broker_id:
            raise BrokerageError(f"토스 주문 응답에 주문번호 없음: {str(data)[:200]}")

        filled = float(data.get(_FIELDS["filled_qty"]) or 0)
        if filled > 0:
            price = float(data.get(_FIELDS["avg_price"]) or order.limit_price or 0)
            quantity = Decimal(str(filled))
            if price > 0:
                ts = utcnow()
                self._pending_fills.append(Fill(
                    order_id=order.id, symbol=order.symbol, side=order.side,
                    quantity=quantity, price=price,
                    fee=self._fill_fee(order, quantity, price, ts),
                    ts=ts, tag=order.tag,
                ))
            else:
                # 단가를 모르면 수수료도 0원으로 계산됩니다 — 요율에 0 을 곱한
                # 값이니까요. 그건 "수수료가 없다" 가 아니라 "아직 모른다" 이고,
                # 원가도 낸 돈도 없는 주식을 장부에 얹는 쪽이 더 나쁩니다.
                # 이 주문은 열려 있으므로 다음 `poll_fills` 가 다시 봅니다.
                log.warning("토스 주문 %s: 체결 %s주의 단가를 읽을 수 없어 이번에는 "
                            "체결로 잡지 않습니다", broker_id, filled)
        return broker_id

    async def _venue_cancel(self, order: Order) -> bool:
        await self.client.request(
            "DELETE", f"{_FIELDS['orders_path']}/{order.broker_id}", account=True)
        return True

    async def _venue_open_orders(self):
        data = await self.client.request("GET", _FIELDS["orders_path"],
                                         params={"status": "OPEN"}, account=True)
        return data.get("orders") or data.get("data") or []

    async def _venue_positions(self) -> dict[str, Decimal]:
        data = await self.client.request("GET", _FIELDS["holdings_path"], account=True)
        out: dict[str, Decimal] = {}
        for row in (data.get("holdings") or data.get("data") or []):
            code = row.get("symbol") or row.get("code")
            qty = row.get("quantity") or row.get("balance") or 0
            if code and qty:
                out[f"toss:{code}"] = Decimal(str(qty))
        return out

    async def poll_fills(self):
        if self.live:
            for order in list(self._orders.values()):
                if not order.status.is_open or not order.broker_id:
                    continue
                try:
                    remote = await self.client.request(
                        "GET", f"{_FIELDS['orders_path']}/{order.broker_id}",
                        account=True)
                except Exception as exc:
                    log.debug("토스 주문 조회 실패 %s: %s", order.broker_id, exc)
                    continue
                newly = Decimal(str(remote.get(_FIELDS["filled_qty"]) or 0)) \
                    - order.filled_qty
                if newly > 0:
                    price = float(remote.get(_FIELDS["avg_price"]) or 0)
                    if price <= 0:
                        # 단가 0 에 요율을 곱하면 0원입니다. 그 0 을 장부에 넣으면
                        # 회계층은 "공짜로 샀다" 로 읽습니다 — 다음 폴링까지
                        # 미루는 편이 낫습니다. `order.filled_qty` 를 올리지
                        # 않았으므로 같은 수량이 그대로 다시 잡힙니다.
                        log.warning("토스 주문 %s: 체결단가를 읽을 수 없어 이번 "
                                    "폴링에서는 체결로 잡지 않습니다",
                                    order.broker_id)
                        continue
                    ts = utcnow()
                    fill = Fill(
                        order_id=order.id, symbol=order.symbol, side=order.side,
                        quantity=newly, price=price,
                        fee=self._fill_fee(order, newly, price, ts),
                        ts=ts, tag=order.tag,
                    )
                    order.apply_fill(fill)
                    self._pending_fills.append(fill)
        return await super().poll_fills()

    async def close(self):
        await self.client.close()


#: 토스가 심볼로 받는 글자. 공식 문서의 패턴(`^[A-Za-z0-9.,\-]+$`)에서 구분자
#: 콤마만 뺀 것입니다. 여기 없는 글자가 하나라도 들어가면 요청 전체가 400 이라,
#: 보내기 전에 걸러 냅니다.
_SYMBOL_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.-")

#: 국내 시장 세그먼트. 상하한가·호가단위가 있는 쪽이라 구분이 필요합니다.
_KR_MARKETS = frozenset({"KOSPI", "KOSDAQ", "KR_ETC"})


def _api_symbol(raw) -> str:
    """요청에 실을 심볼. 토스가 받지 않는 것은 빈 문자열."""
    code = str(raw or "").strip().partition(":")[0].strip().upper()
    if not code or any(ch not in _SYMBOL_CHARS for ch in code):
        return ""
    return code


def _stock_info(row: dict, prices: dict[str, float]) -> dict | None:
    """`StockInfo` 한 줄을 화면이 쓰는 모양으로.

    이름이 비어 있으면 티커로 채우지 않고 **빈 채로** 둡니다. 호출부는 그때
    자기 캐시나 정적 표로 물러설 수 있어야 하는데, 티커를 이름 자리에 넣어
    버리면 "증권사가 이 종목의 이름을 이렇게 준다" 는 뜻이 되어 버립니다.
    """
    from quant.data.providers.kis import korean_tick_size

    code = _api_symbol(row.get("symbol"))
    if not code:
        return None
    market = str(row.get("market") or "").strip().upper()
    currency = str(row.get("currency") or "").strip().upper()
    krx = market in _KR_MARKETS or currency == "KRW"
    info = {
        "ticker": code,
        # 한글 종목명이 없으면 영문명이라도. 둘 다 없으면 빈 값입니다.
        "name": (str(row.get("name") or "").strip()
                 or str(row.get("englishName") or "").strip()),
        "english_name": str(row.get("englishName") or "").strip(),
        "venue": "toss",
        "currency": currency,
        "market": market,
        "security_type": str(row.get("securityType") or "").strip().upper(),
        # 상장폐지·상장예정 종목도 이름은 나옵니다. 그 사실을 숨기면 살 수
        # 없는 종목을 살 수 있는 것처럼 보여 줍니다.
        "status": str(row.get("status") or "").strip().upper(),
    }
    price = prices.get(code)
    if price is not None:
        info["price"] = price
        info["tick_size"] = float(korean_tick_size(price)) if krx else 0.01
    elif not krx:
        # 미국 주식의 호가단위는 가격과 무관하게 $0.01 입니다. 국내는 가격에
        # 따라 달라지므로, 현재가를 못 받았으면 **적지 않습니다** — 틀린
        # 호가단위로 낸 지정가는 거절됩니다.
        info["tick_size"] = 0.01
    return info


def _parse_ts(raw) -> datetime | None:
    if raw is None:
        return None
    text = str(raw).strip()
    try:
        if text.isdigit():
            if len(text) == 8:                       # YYYYMMDD
                return datetime.strptime(text, "%Y%m%d").replace(tzinfo=UTC)
            value = float(text)
            if value > 1e11:                         # milliseconds
                value /= 1000.0
            return datetime.fromtimestamp(value, tz=UTC)
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (ValueError, OSError):
        return None
