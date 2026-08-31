"""토스증권 Open API 어댑터 — 국내 + 미국 주식.

Toss opened a retail Open API in 2026: OAuth 2.0 client-credentials, one REST
surface covering KRX and US equities, quotes through orders.

**두 가지를 먼저 알아야 합니다.**

1. **모의투자 환경이 없습니다.** KIS는 실계좌와 완전히 분리된 모의투자 호스트를
   주지만 토스는 주지 않습니다. 공식 안내조차 "실 환경에서 소액으로 테스트"라고
   말합니다. 즉 토스에서는 이 엔진의 `dry_run` 모드 — 실시간 시세를 읽되 주문은
   내지 않고 체결을 시뮬레이션 — 가 편의 기능이 아니라 **유일한 안전망**입니다.
   그래서 `live=False`인 동안 이 어댑터는 주문 경로를 아예 밟지 않습니다.

2. **계좌번호와 API 식별자는 다릅니다.** 공식 OpenAPI v1.2.14 기준으로 계좌
   범위 요청의 헤더에는 사람이 보는 `accountNo`가 아니라 `/api/v1/accounts`
   가 돌려준 `accountSeq`가 들어갑니다. 이 어댑터는 둘을 먼저 대조한 뒤에만
   holdings·buying-power·주문 경로를 엽니다. 공식 필드와 경로는 `_FIELDS` 한
   곳에서 관리합니다.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation

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
    OrderStatus,
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
    "buying_power_path": "/api/v1/buying-power",
    "orders_path": "/api/v1/orders",
    "account_header": "X-Tossinvest-Account",
    # 주문 요청 본문
    "order_symbol": "symbol",
    "order_side": "side",
    "order_qty": "quantity",
    "order_type": "orderType",
    "order_price": "price",
    "client_order_id": "clientOrderId",
    "time_in_force": "timeInForce",
    "side_buy": "BUY",
    "side_sell": "SELL",
    "type_market": "MARKET",
    "type_limit": "LIMIT",
    # 응답
    "order_id": "orderId",
    "execution": "execution",
    "filled_qty": "filledQuantity",
    "avg_price": "averageFilledPrice",
    "filled_amount": "filledAmount",
    "commission": "commission",
    "tax": "tax",
    "filled_at": "filledAt",
}

_TOSS_CUM_AMOUNT = "_toss_cumulative_filled_amount"
_TOSS_CUM_COMMISSION = "_toss_cumulative_commission"
_TOSS_CUM_TAX = "_toss_cumulative_tax"
_TOSS_CANCEL_CONFIRM_ATTEMPTS = 5
_TOSS_CANCEL_CONFIRM_SECONDS = 6.0


class _TossHTTPError(BrokerageError):
    """One non-2xx Toss response with its status preserved for side-effect safety."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class _TossResponseDecodeError(BrokerageError):
    """A successful HTTP response whose JSON body cannot be trusted."""


class _TossSubmitResponseError(BrokerageError):
    """A 2xx order response that cannot prove which order was accepted."""


def _decimal_string(raw: object, what: str) -> str:
    """Return one positive decimal in the OpenAPI wire format (never a float)."""
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise BrokerageError(f"토스 {what}을 숫자로 읽을 수 없습니다") from exc
    if not value.is_finite() or value <= 0:
        raise BrokerageError(f"토스 {what}은 0보다 큰 유한한 숫자여야 합니다")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _decimal_scale(value: Decimal) -> int:
    """Significant decimal places after removing harmless trailing zeroes."""
    normalized = value.normalize()
    return max(0, -normalized.as_tuple().exponent)


def _nonnegative_decimal(raw: object, what: str, *, nullable: bool = False) -> Decimal:
    """Parse an official cumulative decimal without accepting NaN or negatives."""
    if raw is None and nullable:
        return Decimal("0")
    if raw in (None, ""):
        raise BrokerageError(f"토스 주문 응답에 {what} 값이 없습니다")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise BrokerageError(f"토스 주문 응답의 {what} 값을 읽을 수 없습니다") from exc
    if not value.is_finite() or value < 0:
        raise BrokerageError(f"토스 주문 응답의 {what} 값이 유효한 0 이상 숫자가 아닙니다")
    return value


def _client_order_id(order: Order) -> str:
    """Stable Toss idempotency key for one local order, always schema-safe."""
    raw = str(order.id or "")
    if not raw:
        raise BrokerageError(
            "토스 주문의 로컬 order id가 없어 clientOrderId를 만들 수 없습니다"
        )
    if (len(raw) <= 36
            and all(ch.isascii() and (ch.isalnum() or ch in "-_") for ch in raw)):
        return raw
    return "q_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

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


_TOKENS: dict[bytes, tuple[str, float]] = {}
_TOKEN_LOCK = LazyLock()


def _token_cache_key(client_id: str, client_secret: str) -> bytes:
    """Opaque, credential-complete cache partition for one Toss application."""
    return hashlib.sha256(
        client_id.encode("utf-8") + b"\0" + client_secret.encode("utf-8")
    ).digest()


async def toss_token(client_id: str, client_secret: str,
                     timeout: float = 20.0) -> str:
    """OAuth 2.0 액세스 토큰을 받거나 캐시에서 재사용합니다.

    `client_credentials` 그랜트이고, 자격증명은 **본문**으로 보냅니다 —
    공식 문서의 요구사항입니다.
    """
    # 여러 사용자의 client id는 같은 공급자 접두사를 공유할 수 있습니다.
    # 앞 10글자만 키로 쓰면 먼저 로그인한 사람의 bearer token이 다음 사람의
    # 계좌 요청·주문에 붙습니다. secret 회전도 같은 id의 옛 토큰을 재사용하면
    # 안 되므로 두 값 전체의 digest로 격리합니다. 원문 자격증명은 캐시 키나
    # 로그에 남기지 않습니다.
    cache_key = _token_cache_key(client_id, client_secret)
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
        # The settings screen asks for the human-readable account number, while
        # every account-scoped Toss endpoint requires the opaque ``accountSeq``
        # returned by GET /api/v1/accounts.  Cache only that derived identifier
        # for this short-lived client; never guess it from the account number.
        self._account_seq: str | None = None
        self._account_lock = LazyLock()

    @staticmethod
    def _normalized_account_no(raw: object) -> str:
        """Normalize the separators people copy from an account screen.

        Toss documents ``accountNo`` as a numeric string.  Accept spaces and
        hyphens only; stripping arbitrary characters could turn a typo into a
        different, valid account number.
        """
        value = "".join(str(raw or "").split()).replace("-", "")
        return value if value.isascii() and value.isdigit() else ""

    async def _resolved_account_seq(self) -> str:
        """Resolve configured ``accountNo`` to the required ``accountSeq``.

        Falling back to the first account is forbidden: on a multi-account
        user that would make a valid credential silently trade the wrong book.
        """
        if self._account_seq is not None:
            return self._account_seq
        configured = str(self.account_no or "").strip()
        wanted = self._normalized_account_no(configured)
        if not wanted:
            raise BrokerageError(
                "TOSS_ACCOUNT_NO 형식이 올바르지 않습니다 — 숫자 계좌번호를 확인하세요"
            )
        async with self._account_lock:
            if self._account_seq is not None:
                return self._account_seq
            accounts = await self.request("GET", _FIELDS["accounts_path"])
            if not isinstance(accounts, list):
                raise BrokerageError(
                    "토스 계좌 목록 응답 형식이 올바르지 않습니다 — result 배열이 없습니다"
                )
            matches = [
                row for row in accounts
                if isinstance(row, dict)
                and self._normalized_account_no(row.get("accountNo")) == wanted
            ]
            # Newer setup screens also allow the documented accountSeq itself.
            # Prefer accountNo when both happen to look the same, then accept an
            # exact sequence match.  Never coerce leading zeroes or pick row 0.
            if not matches:
                matches = [
                    row for row in accounts
                    if isinstance(row, dict)
                    and not isinstance(row.get("accountSeq"), bool)
                    and str(row.get("accountSeq")) == configured
                ]
            if not matches:
                raise BrokerageError(
                    "등록한 토스 계좌번호/식별번호와 일치하는 활성 계좌가 없습니다 — "
                    "설정 값을 확인하세요"
                )
            if len(matches) != 1:
                raise BrokerageError(
                    "등록한 토스 계좌번호에 여러 accountSeq가 연결되어 있어 "
                    "안전하게 계좌를 선택할 수 없습니다"
                )
            seq = matches[0].get("accountSeq")
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
                raise BrokerageError(
                    "토스 계좌 목록 응답의 accountSeq가 올바른 정수가 아닙니다"
                )
            # httpx requires header values to be strings even though the OpenAPI
            # schema describes accountSeq itself as int64.
            self._account_seq = str(seq)
            return self._account_seq

    async def _headers(self, with_account: bool = False) -> dict:
        token = await toss_token(self.client_id, self.client_secret)
        headers = {"Authorization": f"Bearer {token}",
                   "Content-Type": "application/json"}
        if with_account:
            headers[_FIELDS["account_header"]] = await self._resolved_account_seq()
        return headers

    async def request(self, method: str, path: str, *, params: dict | None = None,
                      json: dict | None = None,
                      account: bool = False) -> dict | list:
        # Resolve accountSeq before taking this request's rate-limit slot.  The
        # resolver itself calls the unscoped /accounts endpoint; doing that from
        # inside the slot would let the outer request run immediately afterward
        # and violate the configured gap.
        headers = await self._headers(account)
        async with self._lock:
            wait = self._next_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_at = time.monotonic() + self._gap
        r = await self._http.request(
            method, f"{HOST}{path}", params=params, json=json,
            headers=headers,
        )
        if r.status_code >= 400:
            raise _TossHTTPError(
                r.status_code, _explain(r, f"토스 {method} {path}")
            )
        if not r.content:
            return {}
        try:
            body = r.json()
        except ValueError as exc:
            raise _TossResponseDecodeError(
                f"토스 {method} {path} 성공 응답을 JSON으로 읽을 수 없습니다"
            ) from exc
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


def _num(raw: object) -> float | None:
    """문자열 십진수 → float. 못 읽으면 None — 0 으로 채우지 않습니다.

    토스는 금액을 문자열로 줍니다. 못 읽은 값을 0 으로 두면 화면이 "0원" 을
    자신 있게 그리는데, 그건 "공짜" 도 "없음" 도 아니고 "모름" 입니다.
    """
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _named(block: object, currency: object) -> dict:
    """종목 하나의 금액 블록 → {통화: 숫자}.

    합산 블록(`Price`)은 통화별 필드를 갖지만 종목 블록은 그 종목의 통화
    하나뿐입니다. 통화 코드를 붙여 두지 않으면 화면이 원화인지 달러인지 모른
    채로 숫자를 찍고, 미국 종목의 250 이 250원으로 보입니다.
    """
    cur = str(currency or "").upper() or "KRW"
    if isinstance(block, dict):
        # {"amount": ...} 로 한 겹 싸여 오는 경우.
        block = block.get("amount", block)
    if isinstance(block, dict):
        for key, code in (("krw", "KRW"), ("usd", "USD")):
            v = _num(block.get(key))
            if v is not None:
                return {code: v}
        return {}
    v = _num(block)
    return {} if v is None else {cur: v}


def _required_nonnegative_amount(raw: object, what: str) -> float:
    """Parse a required broker amount without inventing a local fallback."""
    if raw in (None, ""):
        raise BrokerageError(f"토스 {what} 응답에 금액이 없습니다")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise BrokerageError(f"토스 {what} 응답 금액을 읽을 수 없습니다") from exc
    if not value.is_finite() or value < 0:
        raise BrokerageError(f"토스 {what} 응답 금액이 유효한 0 이상 숫자가 아닙니다")
    return float(value)


def _market_values(data: dict) -> dict[str, float]:
    """Official HoldingsOverview.marketValue.amount, kept per currency."""
    value = data.get("marketValue")
    amount = value.get("amount") if isinstance(value, dict) else None
    if not isinstance(amount, dict):
        raise BrokerageError(
            "토스 보유 주식 응답에 marketValue.amount가 없습니다"
        )
    # Price.krw is required by the official schema.  Price.usd is nullable when
    # there are no US holdings, so absence there means zero holdings, not an
    # unknown exchange-rate conversion.
    out = {
        "KRW": _required_nonnegative_amount(
            amount.get("krw"), "보유 주식 원화 평가금액"
        )
    }
    if amount.get("usd") is not None:
        out["USD"] = _required_nonnegative_amount(
            amount.get("usd"), "보유 주식 달러 평가금액"
        )
    return out


class TossBrokerage(LiveBrokerage):
    """토스증권 주문.

    `live=False` 인 동안 주문 경로는 아예 실행되지 않습니다 — 토스에는 모의투자
    호스트가 없어서, dry-run이 곧 "네트워크에 아무것도 보내지 않는다"를 의미해야
    하기 때문입니다.
    """

    name = "toss"
    # Toss provides an account-scoped cash buying power and holdings market
    # value snapshot.  LiveBrokerage may therefore adopt this venue truth
    # atomically instead of keeping the YAML starting_cash as the live account.
    venue_capital_truth = True

    def __init__(self, portfolio, client_id: str = "", client_secret: str = "",
                 account_no: str = "", fee_model: FeeModel | None = None, **kwargs):
        super().__init__(portfolio, **kwargs)
        self.client = _TossClient(client_id, client_secret, account_no)
        self._capital_holdings: dict | None = None
        self._capital_cash: float | None = None
        self._venue_avg_cost: dict[str, float] = {}
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

    @staticmethod
    def _supports_fractional_market_sell(order: Order, quantity: Decimal) -> bool:
        return (
            order.symbol.quote_currency.upper() == "USD"
            and order.side is OrderSide.SELL
            and order.type is OrderType.MARKET
            and quantity != quantity.to_integral_value()
        )

    @classmethod
    def _official_quantity_text(cls, order: Order) -> str:
        """Validate Toss's asymmetric quantity contract and return its wire value."""
        text = _decimal_string(order.quantity, "주문 수량")
        quantity = Decimal(text)
        if quantity == quantity.to_integral_value():
            return text
        if not cls._supports_fractional_market_sell(order, quantity):
            raise BrokerageError(
                "토스 소수점 수량 주문은 미국 주식 MARKET SELL만 지원합니다"
            )
        if _decimal_scale(quantity) > 6:
            raise BrokerageError("토스 미국 주식 소수점 매도는 소수점 6자리까지만 지원합니다")
        return text

    def _assert_sell_within_holdings(self, order: Order) -> None:
        if order.side is not OrderSide.SELL:
            return
        held = self.portfolio.quantity(order.symbol)
        if held <= 0 or order.quantity > held:
            raise BrokerageError(
                f"토스는 공매도를 지원하지 않습니다 — 매도 수량 {order.quantity}이 "
                f"현재 보유 수량 {max(held, Decimal('0'))}을 넘습니다"
            )

    def exact_flatten_order_type(
        self,
        symbol: Symbol,
        current_quantity: Decimal,
        target_quantity: Decimal,
    ) -> OrderType | None:
        """Expose Toss's fractional US liquidation route to execution only."""
        quantity = abs(Decimal(current_quantity))
        if (
            target_quantity == 0
            and current_quantity > 0
            and symbol.quote_currency.upper() == "USD"
            and quantity != quantity.to_integral_value()
            and _decimal_scale(quantity) <= 6
        ):
            return OrderType.MARKET
        return None

    def validate(self, order: Order) -> None:
        quantity_text = self._official_quantity_text(order)
        quantity = Decimal(quantity_text)
        if self._supports_fractional_market_sell(order, quantity):
            self._assert_sell_within_holdings(order)
            # Run every generic guard on an immutable copy whose grid expresses
            # only this documented exit route. The actual Symbol stays at lot 1,
            # so portfolio construction can never create fractional BUY orders.
            fractional_symbol = replace(order.symbol, lot_size=Decimal("0.000001"))
            super().validate(replace(order, symbol=fractional_symbol))
            return
        super().validate(order)

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
        # `_venue_submit` 이 지원하지 않는 STOP 계열을 거절하므로 여기까지 온
        # 주문의 `order.type` 은 실제 전송된 MARKET/LIMIT 과 일치합니다.
        return model.fee(order.symbol, quantity, price,
                         order.type is OrderType.LIMIT, when)

    async def _venue_submit(self, order: Order) -> str:
        if order.type not in {OrderType.MARKET, OrderType.LIMIT}:
            raise BrokerageError("토스 주문은 MARKET 또는 LIMIT 유형만 지원합니다")
        quantity_text = self._official_quantity_text(order)
        self._assert_sell_within_holdings(order)
        client_order_id = _client_order_id(order)
        order.meta["toss_client_order_id"] = client_order_id
        body = {
            _FIELDS["client_order_id"]: client_order_id,
            _FIELDS["order_symbol"]: order.symbol.ticker,
            _FIELDS["order_side"]: (_FIELDS["side_buy"] if order.side is OrderSide.BUY
                                    else _FIELDS["side_sell"]),
            # OpenAPI decimal values are strings.  int() used to silently turn
            # a valid US fractional sell into a different order quantity.
            _FIELDS["order_qty"]: quantity_text,
            _FIELDS["order_type"]: (_FIELDS["type_limit"] if order.type is OrderType.LIMIT
                                    else _FIELDS["type_market"]),
            # Toss does not support GTC/IOC/FOK.  Existing engine orders are
            # intentionally placed as the documented DAY default.
            _FIELDS["time_in_force"]: "DAY",
        }
        if order.type is OrderType.LIMIT:
            body[_FIELDS["order_price"]] = _decimal_string(
                order.limit_price, "지정가"
            )

        # A transport failure, an HTTP 5xx, or a malformed 2xx can all happen
        # after Toss accepted the order. Retry once with the *same*
        # clientOrderId so the official 10-minute idempotency window returns
        # the original order instead of duplicating it. A documented 4xx is a
        # rejected request and is therefore not retried blindly.
        data: object
        broker_id = ""
        for attempt in range(2):
            try:
                data = await self.client.request(
                    "POST", _FIELDS["orders_path"], json=body, account=True,
                )
                if not isinstance(data, dict):
                    raise _TossSubmitResponseError(
                        "토스 주문 생성 응답이 객체가 아닙니다"
                    )
                broker_id = str(data.get(_FIELDS["order_id"]) or "").strip()
                if not broker_id:
                    raise _TossSubmitResponseError(
                        "토스 주문 생성 응답에 orderId가 없습니다"
                    )
            except Exception as exc:  # noqa: BLE001 — classify side-effect ambiguity
                ambiguous = (
                    isinstance(
                        exc,
                        (
                            httpx.TransportError,
                            _TossResponseDecodeError,
                            _TossSubmitResponseError,
                        ),
                    )
                    or isinstance(exc, _TossHTTPError) and exc.status_code >= 500
                )
                if attempt == 0 and not ambiguous:
                    raise
                if attempt == 0:
                    continue
                reason = (
                    f"토스 주문 응답을 두 번 받지 못했습니다 "
                    f"(clientOrderId={client_order_id}) — 중복 주문 방지를 위해 "
                    "체결 조회 채널을 잠급니다"
                )
                self.fill_channel_down(reason)
                raise BrokerageError(reason) from exc
            else:
                break
        assert isinstance(data, dict) and broker_id
        returned_client_id = data.get(_FIELDS["client_order_id"])
        if returned_client_id is not None and str(returned_client_id) != client_order_id:
            # orderId is enough to track this accepted order. Raising after the
            # side effect would orphan it and invite a duplicate retry.
            log.error(
                "토스 주문 %s의 clientOrderId가 요청과 다릅니다 (요청 %s, 응답 %s)",
                broker_id, client_order_id, returned_client_id,
            )
        # OrderResponse has no execution block.  The first detail poll is the
        # only place where cumulative fills can be booked exactly once.
        return broker_id

    async def _venue_cancel(self, order: Order) -> bool:
        cancel_key = "toss_cancel_operation_id"
        if cancel_key not in order.meta:
            try:
                operation = await self.client.request(
                    "POST", f"{_FIELDS['orders_path']}/{order.broker_id}/cancel",
                    json={}, account=True,
                )
                operation_id = (
                    str(operation.get(_FIELDS["order_id"]) or "").strip()
                    if isinstance(operation, dict) else ""
                )
                # This is the cancel *operation* id, never the identity of the
                # original order.  Store it only as an audit marker.
                order.meta[cancel_key] = operation_id or "unknown"
            except Exception as exc:  # noqa: BLE001 — acceptance may be unknown
                # Even a timeout can happen after the cancel was accepted. Keep
                # the original order tracked and inspect its detail instead of
                # resubmitting or declaring it canceled.
                order.meta[cancel_key] = "unknown"
                log.warning("토스 취소 요청 응답을 확인하지 못했습니다 %s: %s",
                            order.broker_id, exc)

        # The account client may enforce a one-second global request gap. Five
        # detail reads therefore need more than one second even when Toss itself
        # responds instantly. Keep the whole confirmation phase bounded while
        # leaving enough room for every documented poll attempt.
        deadline = time.monotonic() + _TOSS_CANCEL_CONFIRM_SECONDS
        last_status = ""
        for _attempt in range(_TOSS_CANCEL_CONFIRM_ATTEMPTS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                remote = await asyncio.wait_for(
                    self.client.request(
                        "GET", f"{_FIELDS['orders_path']}/{order.broker_id}",
                        account=True,
                    ),
                    timeout=remaining,
                )
                snapshot = self._order_snapshot(order, remote)
                self._apply_order_snapshot(order, snapshot)
            except Exception as exc:  # noqa: BLE001 — do not orphan on uncertainty
                reason = f"토스 취소 후 원주문 상태를 확인하지 못했습니다: {exc}"
                self.fill_channel_down(reason)
                log.warning("%s", reason)
                return False
            last_status = snapshot["status"]
            if last_status in {"FILLED", "CANCELED", "REJECTED"}:
                order.meta.pop(cancel_key, None)
                return True

        # PENDING means the rejected/expired cancel operation returned the
        # original order to its previous state. Permit a later explicit retry.
        # PENDING_CANCEL/PENDING_REPLACE remain in-flight and retain the marker,
        # so another caller polls rather than stacking operation records.
        if last_status in {"PENDING", "PARTIAL_FILLED"}:
            order.meta.pop(cancel_key, None)
        return False

    async def _venue_open_orders(self):
        data = await self.client.request("GET", _FIELDS["orders_path"],
                                         params={"status": "OPEN"}, account=True)
        if not isinstance(data, dict) or not isinstance(data.get("orders"), list):
            raise BrokerageError(
                "토스 미체결 주문 응답 형식이 올바르지 않습니다 — orders 배열이 없습니다"
            )
        if data.get("hasNext") is not False or data.get("nextCursor") is not None:
            raise BrokerageError(
                "토스 OPEN 주문 응답의 pagination 값이 올바르지 않습니다 — "
                "전체 주문을 확인할 수 없습니다"
            )
        return data["orders"]

    async def _assert_owned_remote_open_orders(self) -> None:
        try:
            remote = await self._venue_open_orders()
            known = {
                str(order.broker_id): order for order in self._orders.values()
                if order.broker_id
            }
            unknown = []
            for row in remote:
                broker_id = (str(row.get(_FIELDS["order_id"]) or "").strip()
                             if isinstance(row, dict) else "")
                if not broker_id or broker_id not in known:
                    unknown.append(row)
                else:
                    # A reconnect inside the same process may prove ownership
                    # from its in-memory broker id, but it still cannot trust a
                    # malformed list row as an execution watermark.
                    self._order_snapshot(known[broker_id], row)
            if unknown:
                raise BrokerageError(
                    f"토스 계좌에 소유권을 확인할 수 없는 미체결 주문이 {len(unknown)}건 "
                    "있습니다. 공식 주문 목록에는 clientOrderId가 없어 이 봇 주문과 "
                    "사용자 앱 주문을 안전하게 구분할 수 없습니다. 자동 복원·취소하지 "
                    "않습니다 — 토스 앱에서 미체결 주문을 확인·취소한 뒤 다시 시작하세요"
                )
        except Exception as exc:
            self._capital_failed(str(exc))
            raise

    async def _pre_venue_submit(self, order: Order) -> None:
        """Refuse app/WTS orders that appeared after the startup checks.

        Toss's open-order response does not expose ``clientOrderId``.  The
        adapter can therefore prove ownership only for broker ids already held
        in this process.  Rechecking here closes the long runtime window between
        ``connect()`` and the next real order; the remaining GET-to-POST race is
        bounded to this one request pair and fails closed on any read error.
        """
        await self._assert_owned_remote_open_orders()

    async def connect(self) -> None:
        if self.live:
            # Check on both sides of the multi-request capital sync.  A user can
            # place an app order while startup is reading the account; checking
            # only before sync leaves that entire window unguarded.
            await self._assert_owned_remote_open_orders()
        await super().connect()
        if self.live:
            await self._assert_owned_remote_open_orders()

    async def _holdings_overview(self) -> dict:
        data = await self.client.request("GET", _FIELDS["holdings_path"], account=True)
        if not isinstance(data, dict):
            raise BrokerageError(
                "토스 보유 주식 응답 형식이 올바르지 않습니다 — result 객체가 없습니다"
            )
        if not isinstance(data.get("items"), list):
            raise BrokerageError(
                "토스 보유 주식 응답 형식이 올바르지 않습니다 — items 배열이 없습니다"
            )
        return data

    async def _cash_buying_power(self, currency: str) -> float:
        currency = str(currency or "").upper()
        if currency not in {"KRW", "USD"}:
            raise BrokerageError(
                f"토스 매수 가능 금액은 KRW/USD만 조회할 수 있습니다 ({currency or '통화 없음'})"
            )
        data = await self.client.request(
            "GET", _FIELDS["buying_power_path"],
            params={"currency": currency}, account=True,
        )
        if not isinstance(data, dict):
            raise BrokerageError(
                f"토스 {currency} 매수 가능 금액 응답에 result 객체가 없습니다"
            )
        returned = str(data.get("currency") or "").upper()
        if returned != currency:
            raise BrokerageError(
                f"토스 매수 가능 금액 응답 통화가 요청과 다릅니다 "
                f"(요청 {currency}, 응답 {returned or '없음'})"
            )
        return _required_nonnegative_amount(
            data.get("cashBuyingPower"), f"{currency} 현금 매수 가능 금액"
        )

    async def _account_snapshot(
        self, currencies: tuple[str, ...]
    ) -> tuple[dict, dict[str, float]]:
        """Conservative cash/holdings snapshot across execution races.

        The API has no atomic account endpoint.  A sell between ``holdings``
        and ``buying-power`` otherwise combines the old shares with the new
        sale proceeds and briefly invents equity.  Bracket holdings with two
        cash reads and keep the smaller cash value per currency.  Buys, sells,
        deposits and withdrawals can then understate this snapshot, never
        overstate the amount that a new order may use.
        """
        normalized = tuple(dict.fromkeys(str(c or "").upper() for c in currencies))
        if not normalized:
            raise BrokerageError("토스 계좌 snapshot에 조회할 통화가 없습니다")
        before = {
            currency: await self._cash_buying_power(currency)
            for currency in normalized
        }
        holdings = await self._holdings_overview()
        after = {
            currency: await self._cash_buying_power(currency)
            for currency in normalized
        }
        return holdings, {
            currency: min(before[currency], after[currency])
            for currency in normalized
        }

    async def _venue_capital(self) -> dict[str, float | str]:
        """Fresh base-currency capital truth for one atomic live reconciliation."""
        # Fetch into locals first.  A half-valid response must not leave a cache
        # that the following position hook could mistake for a complete snapshot.
        self._capital_holdings = None
        self._capital_cash = None
        currency = str(self.portfolio.base_currency or "").upper()
        data, cash_by_currency = await self._account_snapshot((currency,))
        values = _market_values(data)
        cash = cash_by_currency[currency]
        holdings_value = values.get(currency, 0.0)
        self._capital_holdings = data
        self._capital_cash = cash
        return {
            "currency": currency,
            "cash": cash,
            "holdings_value": holdings_value,
        }

    async def _venue_cash(self) -> float | None:
        # The truth reconciliation calls _venue_capital first, so reuse that
        # exact value rather than taking a second, potentially different snapshot.
        if self._capital_cash is not None:
            return self._capital_cash
        return await self._cash_buying_power(self.portfolio.base_currency)

    async def account_overview(self) -> dict:
        """증권사가 말하는 계좌 상태 — 봇과 무관하게.

        "내 계좌" 탭은 지금까지 **돌고 있는 봇의 장부**를 그렸습니다. 봇이
        꺼져 있으면 그릴 것이 없어서 탭이 통째로 비었고, 토스를 연동해 둔
        사람에게는 그게 "연동이 안 됐다" 로 읽힙니다. 실제로 그렇게 읽혔습니다.

        계좌는 봇의 것이 아니라 사람의 것입니다. 봇이 꺼져 있어도, 봇이 한 번도
        안 돌았어도, 다른 데서 산 종목이어도 여기 나와야 합니다.

        **예수금과 현금 매수 가능 금액은 다릅니다.** 토스는 예수금 잔액을 주지
        않지만 `/api/v1/buying-power` 로 미수 없는 현금 매수 가능 금액을 줍니다.
        따라서 `cash` 는 계속 비워 두고, `cash_buying_power` 로 이름을 분리합니다.

        금액은 **통화별로 따로** 옵니다(원화 합, 달러 합). 토스가 환산해서
        합쳐 주지 않으므로 여기서도 합치지 않습니다. 환율을 여기서 끌어와
        더하면 그 순간 환차손익이 매매 손익에 섞입니다.
        """
        data, cash_buying_power = await self._account_snapshot(("KRW", "USD"))

        def money(block: object) -> dict:
            """통화별 금액 블록 → {"KRW": 숫자, "USD": 숫자}. 없는 통화는 뺍니다."""
            if not isinstance(block, dict):
                return {}
            out = {}
            for key, code in (("krw", "KRW"), ("usd", "USD")):
                raw = block.get(key)
                if raw in (None, ""):
                    continue
                try:
                    out[code] = float(raw)
                except (TypeError, ValueError):
                    continue
            return out

        def rate(block: object, key: str = "rate") -> float | None:
            if not isinstance(block, dict):
                return None
            try:
                return float(block[key])
            except (KeyError, TypeError, ValueError):
                return None

        market_value = _market_values(data)
        pnl = data.get("profitLoss") or {}
        daily = data.get("dailyProfitLoss") or {}
        items = []
        for row in (data.get("items") or []):
            try:
                qty = float(row.get("quantity") or 0)
            except (TypeError, ValueError):
                continue
            if not qty:
                continue
            items.append({
                "ticker": str(row.get("symbol") or ""),
                # 토스가 종목명을 실어 줍니다 — 우리가 따로 찾을 필요가 없습니다.
                "name": str(row.get("name") or "") or str(row.get("symbol") or ""),
                "currency": str(row.get("currency") or ""),
                "quantity": qty,
                "last_price": _num(row.get("lastPrice")),
                "avg_price": _num(row.get("averagePurchasePrice")),
                # 종목 금액은 그 종목의 통화 하나뿐입니다. 통화 코드를 함께
                # 넣어 두지 않으면 화면이 원화인지 달러인지 모른 채 찍습니다.
                "market_value": _named(row.get("marketValue"), row.get("currency")),
                "pnl": _named(row.get("profitLoss"), row.get("currency")),
                "pnl_pct": rate(row.get("profitLoss")),
            })
        # Never add KRW to USD.  Each value is useful only inside its own
        # currency; FX conversion belongs to a separate, explicitly priced step.
        investable_assets = {
            currency: buying_power + market_value.get(currency, 0.0)
            for currency, buying_power in cash_buying_power.items()
        }
        return {
            "source": "toss",
            "invested": money(data.get("totalPurchaseAmount")),
            "market_value": market_value,
            "pnl": money(pnl.get("amount")),
            "pnl_pct": rate(pnl),
            "daily_pnl": money(daily.get("amount")),
            "daily_pnl_pct": rate(daily),
            # 예수금 자체는 제공되지 않습니다. 매수 가능 금액을 그 이름으로
            # 둔갑시키지 않고 별도 필드로 내보냅니다.
            "cash": None,
            "cash_buying_power": cash_buying_power,
            "investable_assets": investable_assets,
            "value_kind": "cash_buying_power_plus_holdings",
            "items": items,
        }

    async def _venue_positions(self) -> dict[str, Decimal]:
        # _venue_capital runs first during live reconciliation.  Consume that
        # exact holdings response so quantity and account value cannot come from
        # different moments.  Direct callers still receive a fresh snapshot.
        data = self._capital_holdings
        self._capital_holdings = None
        if data is None:
            data = await self._holdings_overview()
        out: dict[str, Decimal] = {}
        costs: dict[str, float] = {}
        for index, row in enumerate(data["items"]):
            if not isinstance(row, dict):
                raise BrokerageError(
                    f"토스 보유 주식 items[{index}]가 객체가 아닙니다"
                )
            code = str(row.get("symbol") or "").strip().upper()
            if not code:
                raise BrokerageError(
                    f"토스 보유 주식 items[{index}]에 symbol이 없습니다"
                )
            try:
                qty = Decimal(str(row.get("quantity")))
            except (InvalidOperation, ValueError) as exc:
                raise BrokerageError(
                    f"토스 보유 주식 {code}의 quantity를 읽을 수 없습니다"
                ) from exc
            if not qty.is_finite() or qty < 0:
                raise BrokerageError(
                    f"토스 보유 주식 {code}의 quantity가 유효한 0 이상 숫자가 아닙니다"
                )
            if not qty:
                continue
            key = f"toss:{code}"
            if key in out:
                raise BrokerageError(f"토스 보유 주식 응답에 {code}가 중복되었습니다")
            out[key] = qty
            avg = _required_nonnegative_amount(
                row.get("averagePurchasePrice"), f"{code} 평균 매수가"
            )
            if avg <= 0:
                raise BrokerageError(f"토스 보유 주식 {code}의 평균 매수가가 0입니다")
            costs[key] = avg
        self._venue_avg_cost = costs
        return out

    async def _venue_costs(self) -> dict[str, float]:
        return dict(self._venue_avg_cost)

    def _order_snapshot(self, order: Order, remote: object) -> dict:
        """Validate one official Order row before mutating the local order."""
        if not isinstance(remote, dict):
            raise BrokerageError("토스 주문 상세 응답이 객체가 아닙니다")
        broker_id = str(remote.get(_FIELDS["order_id"]) or "").strip()
        if broker_id != str(order.broker_id):
            raise BrokerageError("토스 주문 상세의 orderId가 조회한 주문과 다릅니다")
        if str(remote.get("symbol") or "").strip().upper() != order.symbol.ticker.upper():
            raise BrokerageError("토스 주문 상세의 symbol이 로컬 주문과 다릅니다")
        expected_side = "BUY" if order.side is OrderSide.BUY else "SELL"
        if str(remote.get("side") or "").upper() != expected_side:
            raise BrokerageError("토스 주문 상세의 side가 로컬 주문과 다릅니다")
        expected_type = "LIMIT" if order.type is OrderType.LIMIT else "MARKET"
        if str(remote.get("orderType") or "").upper() != expected_type:
            raise BrokerageError("토스 주문 상세의 orderType이 로컬 주문과 다릅니다")
        if str(remote.get("timeInForce") or "").upper() != "DAY":
            raise BrokerageError("토스 주문 상세의 timeInForce를 DAY 주문으로 연결할 수 없습니다")
        if str(remote.get("currency") or "").upper() != order.symbol.quote_currency.upper():
            raise BrokerageError("토스 주문 상세의 currency가 종목 통화와 다릅니다")
        if _parse_ts(remote.get("orderedAt")) is None:
            raise BrokerageError("토스 주문 상세의 orderedAt을 읽을 수 없습니다")

        quantity = _nonnegative_decimal(remote.get("quantity"), "quantity")
        if quantity != order.quantity:
            raise BrokerageError("토스 주문 상세의 quantity가 로컬 주문과 다릅니다")
        if order.type is OrderType.LIMIT:
            price = _nonnegative_decimal(remote.get("price"), "price")
            if price <= 0 or price != Decimal(str(order.limit_price)):
                raise BrokerageError("토스 주문 상세의 price가 로컬 지정가와 다릅니다")
        elif remote.get("price") is not None:
            raise BrokerageError("토스 시장가 주문 상세에 예기치 않은 price가 있습니다")

        status = str(remote.get("status") or "").upper()
        open_statuses = {"PENDING", "PENDING_CANCEL", "PENDING_REPLACE", "PARTIAL_FILLED"}
        terminal_statuses = {"FILLED", "CANCELED", "REJECTED"}
        # CANCEL_REJECTED/REPLACE_REJECTED are separate operation records; the
        # original order is documented to return to its previous state, so an
        # original-order detail request must not be closed from either code.
        # REPLACED is terminal for the original but its successor order id is
        # absent here, leaving an untracked live order.  All three therefore
        # fail closed instead of guessing an exposure state.
        if status in {"CANCEL_REJECTED", "REPLACE_REJECTED"}:
            raise BrokerageError(
                f"토스 {status} 작업 레코드를 원주문 상태로 연결할 수 없습니다"
            )
        if status == "REPLACED":
            raise BrokerageError("정정된 토스 주문의 후속 orderId를 추적할 수 없습니다")
        if status not in open_statuses | terminal_statuses:
            raise BrokerageError(
                f"토스 주문 상태 {status or '없음'}를 안전하게 처리할 수 없습니다"
            )
        execution = remote.get(_FIELDS["execution"])
        if not isinstance(execution, dict):
            raise BrokerageError("토스 주문 상세에 execution 객체가 없습니다")
        filled_qty = _nonnegative_decimal(
            execution.get(_FIELDS["filled_qty"]), "execution.filledQuantity"
        )
        if filled_qty > quantity:
            raise BrokerageError("토스 누적 체결 수량이 주문 수량을 넘습니다")
        filled_amount = _nonnegative_decimal(
            execution.get(_FIELDS["filled_amount"]),
            "execution.filledAmount", nullable=filled_qty == 0,
        )
        average_price_raw = execution.get(_FIELDS["avg_price"])
        average_price = _nonnegative_decimal(
            average_price_raw, "execution.averageFilledPrice",
            nullable=filled_qty == 0,
        )
        filled_at = _parse_ts(execution.get(_FIELDS["filled_at"]))
        if filled_qty > 0 and (
            filled_amount <= 0 or average_price <= 0 or filled_at is None
        ):
            raise BrokerageError(
                "토스 체결 수량은 있지만 금액·평균가·최종 체결시각이 완전하지 않습니다"
            )
        commission = _nonnegative_decimal(
            execution.get(_FIELDS["commission"]),
            "execution.commission", nullable=True,
        )
        tax = _nonnegative_decimal(
            execution.get(_FIELDS["tax"]), "execution.tax", nullable=True,
        )

        if status == "PENDING" and filled_qty != 0:
            raise BrokerageError("PENDING 주문에 누적 체결 수량이 있습니다")
        if status == "PARTIAL_FILLED" and not (0 < filled_qty < quantity):
            raise BrokerageError("PARTIAL_FILLED 상태와 누적 체결 수량이 모순됩니다")
        if status == "FILLED" and filled_qty != quantity:
            raise BrokerageError("FILLED 상태인데 누적 체결 수량이 주문 수량과 다릅니다")
        return {
            "status": status,
            "filled_qty": filled_qty,
            "filled_amount": filled_amount,
            "commission": commission,
            "tax": tax,
            "filled_at": filled_at,
        }

    @staticmethod
    def _previous_cumulative(order: Order, key: str, what: str) -> Decimal:
        if key not in order.meta:
            if order.filled_qty == 0:
                return Decimal("0")
            raise BrokerageError(
                f"기존 토스 주문의 {what} 누적 기준값이 없어 새 체결분을 분리할 수 없습니다"
            )
        return _nonnegative_decimal(order.meta[key], what)

    def _apply_order_snapshot(self, order: Order, snapshot: dict) -> None:
        total_qty = snapshot["filled_qty"]
        previous_qty = order.filled_qty
        previous_amount = self._previous_cumulative(
            order, _TOSS_CUM_AMOUNT, "기존 filledAmount"
        )
        previous_commission = self._previous_cumulative(
            order, _TOSS_CUM_COMMISSION, "기존 commission"
        )
        previous_tax = self._previous_cumulative(order, _TOSS_CUM_TAX, "기존 tax")
        total_amount = snapshot["filled_amount"]
        total_commission = snapshot["commission"]
        total_tax = snapshot["tax"]
        if total_qty < previous_qty:
            raise BrokerageError("토스 누적 체결 수량이 이전 조회보다 줄었습니다")
        if total_amount < previous_amount:
            raise BrokerageError("토스 누적 체결 금액이 이전 조회보다 줄었습니다")
        if total_commission < previous_commission or total_tax < previous_tax:
            raise BrokerageError("토스 누적 수수료/세금이 이전 조회보다 줄었습니다")

        newly = total_qty - previous_qty
        amount_delta = total_amount - previous_amount
        commission_delta = total_commission - previous_commission
        tax_delta = total_tax - previous_tax
        if newly == 0 and any(v != 0 for v in (amount_delta, commission_delta, tax_delta)):
            raise BrokerageError("새 체결 없이 누적 체결금액·비용만 바뀌어 정확히 장부화할 수 없습니다")
        if newly > 0:
            if amount_delta <= 0 or snapshot["filled_at"] is None:
                raise BrokerageError("새 체결 수량에 대응하는 체결금액·시각이 없습니다")
            fill = Fill(
                order_id=order.id,
                symbol=order.symbol,
                side=order.side,
                quantity=newly,
                price=float(amount_delta / newly),
                fee=float(commission_delta + tax_delta),
                ts=snapshot["filled_at"],
                tag=order.tag,
            )
            order.apply_fill(fill)
            self._pending_fills.append(fill)

        order.meta[_TOSS_CUM_AMOUNT] = str(total_amount)
        order.meta[_TOSS_CUM_COMMISSION] = str(total_commission)
        order.meta[_TOSS_CUM_TAX] = str(total_tax)
        status = snapshot["status"]
        # Cumulative quantity is the exposure truth even if the broker's status
        # transition lags by one read (for example PENDING_CANCEL at the instant
        # the last share fills). Never turn a fully-filled order back into a
        # locally-open PARTIAL zombie.
        if total_qty >= order.quantity:
            order.status = OrderStatus.FILLED
        elif status in {"PENDING", "PENDING_CANCEL", "PENDING_REPLACE", "PARTIAL_FILLED"}:
            order.status = OrderStatus.PARTIAL if total_qty > 0 else OrderStatus.SUBMITTED
        elif status == "FILLED":
            order.status = OrderStatus.FILLED
        elif status == "CANCELED":
            order.status = OrderStatus.CANCELED
        elif status == "REJECTED":
            order.status = OrderStatus.REJECTED
            order.reject_reason = "토스가 접수된 주문을 거부했습니다"
        if status in {"FILLED", "CANCELED", "REJECTED"}:
            self._mark_terminal_observed(order)
        order.updated_at = snapshot["filled_at"] or utcnow()

    async def poll_fills(self):
        failures: list[str] = []
        observed = 0
        if self.live:
            for order in list(self._orders.values()):
                if not order.status.is_open or not order.broker_id:
                    continue
                observed += 1
                try:
                    remote = await self.client.request(
                        "GET", f"{_FIELDS['orders_path']}/{order.broker_id}",
                        account=True,
                    )
                    self._apply_order_snapshot(order, self._order_snapshot(order, remote))
                except Exception as exc:  # noqa: BLE001 — fail the whole observation
                    failures.append(f"{order.broker_id}: {exc}")
        if failures:
            reason = "토스 주문 상세 조회/해석 실패 — " + "; ".join(failures[:3])
            self.fill_channel_down(reason)
            # cancel()->_reap() must see this as an unobserved result. Returning
            # an empty list would release a zero-fill cash reservation even
            # though the cancel/fill race was never checked.
            raise BrokerageError(reason)
        if observed:
            self.fill_channel_up()
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
