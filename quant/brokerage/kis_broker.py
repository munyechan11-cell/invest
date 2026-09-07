"""Korea Investment & Securities order adapter (domestic + overseas equities).

KIS separates *paper* (모의투자) and *live* into different hosts and different
transaction ids, so `live=False` never reaches the real account even with
production credentials.

Which environment is a separate question from whether orders are sent at all:

  · `mode: dry_run`                              — reads 모의투자, sends nothing
  · `mode: dry_run` + `broker.params.paper_trading: true`
                                                 — sends real orders to 모의투자
  · `mode: live`                                 — real money, real host

Only the last one reports `RunMode.LIVE`. 모의투자 is where a strategy is meant
to be validated first, so it has to be an environment the engine can actually
submit into, not a host it merely reads balances from.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from decimal import Decimal

import httpx

from quant.brokerage.base import BrokerageError
from quant.brokerage.live_base import LiveBrokerage
from quant.core.types import UTC, Fill, Order, OrderSide, OrderType, utcnow
from quant.data.calendar import KST
from quant.data.providers.kis import kis_host, kis_token
from quant.execution.costs import krx_sell_tax_bps

log = logging.getLogger("quant.brokerage.kis")

# (buy, sell) transaction ids per environment
TR_DOMESTIC = {True: ("TTTC0802U", "TTTC0801U"), False: ("VTTC0802U", "VTTC0801U")}
TR_OVERSEAS = {True: ("TTTT1002U", "TTTT1006U"), False: ("VTTT1002U", "VTTT1001U")}
TR_BALANCE = {True: "TTTC8434R", False: "VTTC8434R"}
TR_OVERSEAS_BALANCE = {True: "TTTS3012R", False: "VTTS3012R"}
EXCHANGE_CURRENCY = {"NASD": "USD", "NAS": "USD", "NYSE": "USD", "AMEX": "USD",
                     "SEHK": "HKD", "SHAA": "CNY", "SZAA": "CNY",
                     "TKSE": "JPY", "HASE": "VND", "VNSE": "VND"}
# 주식일별주문체결조회 / 해외주식 주문체결내역 — the only channel that reports a fill
TR_DAILY_CCLD = {True: "TTTC8001R", False: "VTTC8001R"}
TR_OVERSEAS_CCLD = {True: "TTTS3035R", False: "VTTS3035R"}

DOMESTIC_CCLD_PATH = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
OVERSEAS_CCLD_PATH = "/uapi/overseas-stock/v1/trading/inquire-ccnl"

#: The two execution endpoints name the same column differently, so every read
#: goes through `_field` with both spellings.
_CCLD_KEYS = {
    "order_id": ("odno", "ODNO"),
    "ticker": ("pdno", "PDNO"),
    "order_qty": ("ord_qty", "ft_ord_qty"),
    "filled_qty": ("tot_ccld_qty", "ft_ccld_qty", "ccld_qty"),
    "avg_price": ("avg_prvs", "ft_ccld_unpr3", "ccld_unpr"),
    "filled_amount": ("tot_ccld_amt", "ft_ccld_amt3"),
    "remaining": ("rmn_qty", "nccs_qty"),
    "date": ("ord_dt", "ord_gno_dt"),
    "time": ("ord_tmd", "ord_tm"),
}


def _ambiguous_submit(exc: BaseException) -> bool:
    """이 실패에서 "주문이 안 나갔다" 를 단정할 수 있는가.

    단정할 수 있는 것은 서버가 **읽고 거절한** 경우뿐입니다(4xx). 전송 오류·
    타임아웃·5xx·깨진 응답은 요청이 이미 처리됐을 수도 있습니다.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    if isinstance(exc, httpx.TransportError):
        return True
    # 2xx 인데 JSON 이 아닌 경우 등 — 응답을 해석하지 못한 것도 애매합니다.
    return isinstance(exc, ValueError)


class KisBrokerage(LiveBrokerage):
    name = "kis"

    #: 목록 조회에서 따라갈 페이지 수의 상한. 한 장에 수십 행이 오므로 이
    #: 정도면 현실적인 계좌를 전부 덮습니다. 넘으면 **부분 결과를 돌려주지 않고**
    #: 예외로 끝냅니다 — 절반만 아는 잔고는 없는 종목을 청산으로 읽습니다.
    MAX_QUERY_PAGES = 20

    def __init__(self, portfolio, app_key: str = "", app_secret: str = "",
                 account_no: str = "", product_code: str = "01",
                 overseas_exchange: str = "NASD", paper_trading: bool = False,
                 commission_bps: float = 1.5, sell_tax_bps: float | None = None,
                 overseas_commission_bps: float = 25.0,
                 allow_env_credentials: bool = True, **kwargs):
        if paper_trading and kwargs.get("live"):
            raise BrokerageError(
                "broker.params.paper_trading: true 와 mode: live 는 함께 쓸 수 "
                "없습니다 — 모의투자와 실계좌 중 하나만 고르세요"
            )
        super().__init__(portfolio, paper_venue=paper_trading, **kwargs)
        self.app_key = (app_key or os.environ.get("KIS_APP_KEY", "")
                        if allow_env_credentials else app_key)
        self.app_secret = (app_secret or os.environ.get("KIS_APP_SECRET", "")
                           if allow_env_credentials else app_secret)
        self.account_no = (account_no or os.environ.get("KIS_ACCOUNT_NO", "")
                           if allow_env_credentials else account_no)
        self.product_code = product_code
        self.overseas_exchange = overseas_exchange
        #: which KIS environment this session talks to — hosts and tr_ids.
        #: Independent of `sends_orders`: a dry run still reads 모의투자.
        self.paper = not self.live
        # 체결 조회 returns quantities and prices but no commission, and a fee
        # of 0.0 is a number the accounting layer believes. Charge the KRX
        # retail schedule the backtest already assumes instead.
        self.commission_bps = commission_bps
        #: None 이면 체결 시점의 법정 세율(`krx_sell_tax_bps`)을 씁니다. 여기에
        #: 상수를 박아두면 세율이 바뀐 해부터 조용히 틀리고, 백테스트가 돌던
        #: 과거 구간에도 오늘 값이 소급됩니다. 명시하면 그 값으로 고정 —
        #: 우대 요율처럼 법정 세율과 다른 계좌를 위한 탈출구입니다.
        self.sell_tax_bps = sell_tax_bps
        self.overseas_commission_bps = overseas_commission_bps
        self._client = httpx.AsyncClient(timeout=20)
        self._venue_avg_cost: dict[str, float] = {}
        self._venue_deposit: float | None = None
        missing = [n for n, v in (("KIS_APP_KEY", self.app_key),
                                  ("KIS_APP_SECRET", self.app_secret),
                                  ("KIS_ACCOUNT_NO", self.account_no)) if not v]
        if missing:
            raise BrokerageError(f"KIS brokerage needs {', '.join(missing)}")

    async def _headers(self, tr_id: str, body: dict | None = None) -> dict:
        token = await kis_token(self.app_key, self.app_secret, self.paper)
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        if body is not None and self.live:
            headers["hashkey"] = await self._hashkey(body)
        return headers

    async def _hashkey(self, body: dict) -> str:
        """KIS requires a tamper-proof hash on live orders."""
        r = await self._client.post(
            f"{kis_host(self.paper)}/uapi/hashkey", json=body,
            headers={"content-type": "application/json; charset=utf-8",
                     "appkey": self.app_key, "appsecret": self.app_secret},
        )
        r.raise_for_status()
        return r.json()["HASH"]

    async def _venue_submit(self, order: Order) -> str:
        domestic = order.symbol.quote_currency == "KRW"
        # keyed by environment, not by permission — a 모의투자 order is a real
        # submission that happens to carry the VTTC* ids
        buy, sell = (TR_DOMESTIC if domestic else TR_OVERSEAS)[not self.paper]
        tr_id = buy if order.side is OrderSide.BUY else sell

        if domestic:
            path = "/uapi/domestic-stock/v1/trading/order-cash"
            body = {
                "CANO": self.account_no[:8],
                "ACNT_PRDT_CD": self.product_code,
                "PDNO": order.symbol.ticker,
                # 01 = market, 00 = limit
                "ORD_DVSN": "01" if order.type is OrderType.MARKET else "00",
                "ORD_QTY": str(int(order.quantity)),
                "ORD_UNPR": "0" if order.type is OrderType.MARKET
                            else str(int(order.limit_price or 0)),
            }
        else:
            if order.type is OrderType.MARKET:
                raise BrokerageError(
                    "KIS overseas orders must be limit orders — the API has no "
                    "market order type for foreign equities"
                )
            path = "/uapi/overseas-stock/v1/trading/order"
            body = {
                "CANO": self.account_no[:8],
                "ACNT_PRDT_CD": self.product_code,
                "OVRS_EXCG_CD": self.overseas_exchange,
                "PDNO": order.symbol.ticker,
                "ORD_QTY": str(int(order.quantity)),
                "OVRS_ORD_UNPR": f"{order.limit_price:.2f}",
                "ORD_SVR_DVSN_CD": "0",
                "ORD_DVSN": "00",
            }

        headers = await self._headers(tr_id, body)
        self._enforce_submission_guard(order)
        try:
            r = await self._client.post(
                f"{kis_host(self.paper)}{path}", headers=headers, json=body,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            # **응답을 못 받은 것은 "주문이 안 나갔다" 가 아닙니다.** 전송 오류나
            # 5xx 뒤에도 KIS 는 주문을 접수했을 수 있는데, 여기서 그냥 예외로
            # 끝내면 상위 층은 REJECTED 로 적고 로컬 주문 표에는 아무것도 남지
            # 않습니다. 그러면 다음 봉이 같은 목표를 다시 diff 해서 **같은 주문을
            # 또 보냅니다.** 시장가는 그 사이 `sync()` 가 잔고에서 체결을 채택해
            # 막아 주지만, 지정가는 잔고에 안 잡히므로 둘 다 살아 체결되면 노출이
            # 두 배가 됩니다. KIS 는 취소가 구현돼 있지 않아 되돌릴 수도 없습니다.
            #
            # 토스에는 `clientOrderId` 재시도와 채널 잠금이 있는데 KIS 에는
            # 대응물이 없습니다. 최소한 **눈을 감았다는 사실** 은 남깁니다 —
            # 채널이 내려가면 `_guard` 가 다음 주문을 막고, 사람이 체결 내역을
            # 확인할 때까지 자동으로 재시도하지 않습니다.
            if _ambiguous_submit(exc):
                reason = (
                    f"KIS 주문 응답을 받지 못했습니다 ({type(exc).__name__}: {exc}) — "
                    "접수됐는지 알 수 없어 중복 주문을 막기 위해 체결 조회 채널을 "
                    "잠급니다. 증권사 앱에서 미체결·당일 체결을 확인하세요"
                )
                self.fill_channel_down(reason)
                raise BrokerageError(reason) from exc
            raise
        if str(data.get("rt_cd", "1")) != "0":
            raise BrokerageError(f"KIS order rejected: {data.get('msg1') or data}")
        return str((data.get("output") or {}).get("ODNO") or "")

    async def _venue_cancel(self, order: Order) -> bool:
        # KIS cancellation needs the original order's branch number, which the
        # submit response does not always carry; surfacing this honestly beats
        # silently pretending the cancel worked.
        raise BrokerageError(
            "KIS order cancellation is not implemented — cancel from the broker's "
            "own app or HTS. Prefer market/IOC orders so nothing rests."
        )

    # ── 체결 조회 ─────────────────────────────────────────────────────────
    def _ccld_window(self) -> tuple[str, str]:
        """The KST date range the execution query has to cover.

        A session that runs past midnight KST still has yesterday's resting
        orders open, and a query for today alone would never see them fill.
        """
        today = datetime.now(KST).strftime("%Y%m%d")
        days = [order.created_at.astimezone(KST).strftime("%Y%m%d")
                for order in self._orders.values() if order.status.is_open]
        return min([*days, today]), today

    def _ccld_scopes(self) -> set[str]:
        scopes = {"domestic" if order.symbol.quote_currency == "KRW" else "overseas"
                  for order in self._orders.values() if order.status.is_open}
        # With nothing resting, still query the domestic book: this doubles as
        # the liveness probe for the fill channel itself.
        return scopes or {"domestic"}

    async def _venue_executions(self) -> list[dict]:
        """Today's order/execution rows — 주식일별주문체결조회.

        One call per cycle for the whole account rather than one per open
        order: KIS rate-limits per app key, and the same rows also answer
        `_venue_open_orders()`.
        """
        start, end = self._ccld_window()
        scopes = self._ccld_scopes()
        rows: list[dict] = []
        if "domestic" in scopes:
            rows.extend(await self._query_executions(
                DOMESTIC_CCLD_PATH, TR_DAILY_CCLD[not self.paper], "output1",
                {"INQR_STRT_DT": start, "INQR_END_DT": end,
                 "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", "PDNO": "",
                 "CCLD_DVSN": "00", "ORD_GNO_BRNO": "", "ODNO": "",
                 "INQR_DVSN_3": "00", "INQR_DVSN_1": "",
                 "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""},
            ))
        if "overseas" in scopes:
            rows.extend(await self._query_executions(
                OVERSEAS_CCLD_PATH, TR_OVERSEAS_CCLD[not self.paper], "output",
                {"PDNO": "%", "ORD_STRT_DT": start, "ORD_END_DT": end,
                 "SLL_BUY_DVSN": "00", "CCLD_NCCS_DVSN": "00",
                 "OVRS_EXCG_CD": self.overseas_exchange, "SORT_SQN": "DS",
                 "ORD_DT": "", "ORD_GNO_BRNO": "", "ODNO": "",
                 "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""},
            ))
        return rows

    async def _paged(self, path: str, tr_id: str, params: dict,
                     *, ctx_suffix: str, what: str):
        """KIS 목록 조회를 **끝까지** 읽는다. 한 페이지만 읽으면 안 된다.

        KIS 는 목록을 나눠 주고 이어받기 키를 `ctx_area_fk###`/`nk###` 로,
        "다음 장이 있음" 을 응답 헤더 `tr_cont`(F/M)로 알립니다. 예전에는 그
        둘을 무시하고 빈 키로 첫 장만 읽었습니다.

        그것이 왜 위험한가: 둘째 장의 보유 종목은 `_venue_positions()` 에
        없으므로 `_sync_once` 가 "외부에서 청산됐다" 로 읽고 로컬 수량을 0 으로
        만든 뒤 **원가만큼 현금을 지어냅니다.** 엔진은 그 종목이 비었고 현금이
        늘었다고 믿어 다시 사고, 사라진 수량에는 손절이 걸리지 않습니다.
        체결 조회도 같아서, 뒤 장의 체결은 아무 표시 없이 장부에 오르지
        않습니다.

        그래서 이 함수는 **부분 결과를 돌려주지 않습니다.** 페이지 상한에
        닿거나 이어받기 키가 이상하면 예외로 끝냅니다 — 모르는 채로 절반만
        아는 것보다 조회 실패가 낫습니다(`sync` 가 그것을 실패로 다룹니다).
        """
        fk = nk = ""
        tr_cont = ""
        for _page in range(self.MAX_QUERY_PAGES):
            headers = await self._headers(tr_id)
            if tr_cont:
                headers["tr_cont"] = tr_cont
            r = await self._client.get(
                f"{kis_host(self.paper)}{path}",
                headers=headers,
                params={"CANO": self.account_no[:8],
                        "ACNT_PRDT_CD": self.product_code, **params,
                        f"CTX_AREA_FK{ctx_suffix}": fk,
                        f"CTX_AREA_NK{ctx_suffix}": nk},
            )
            r.raise_for_status()
            data = r.json()
            if str(data.get("rt_cd", "0")) != "0":
                raise BrokerageError(f"KIS {what} 거부: {data.get('msg1') or data}")
            yield data
            more = (r.headers.get("tr_cont") or "").strip().upper()
            if more not in ("F", "M"):
                return
            fk = str(data.get(f"ctx_area_fk{ctx_suffix}") or "").strip()
            nk = str(data.get(f"ctx_area_nk{ctx_suffix}") or "").strip()
            if not nk:
                raise BrokerageError(
                    f"KIS {what}: 다음 장이 있다고 하는데 이어받기 키가 없습니다 — "
                    "일부만 읽고 전체인 척하지 않습니다"
                )
            tr_cont = "N"
        raise BrokerageError(
            f"KIS {what}: {self.MAX_QUERY_PAGES}장을 넘겨도 끝이 나오지 "
            "않았습니다 — 일부만 읽고 전체인 척하지 않습니다"
        )

    async def _query_executions(self, path: str, tr_id: str, output: str,
                                params: dict) -> list[dict]:
        suffix = "200" if "CTX_AREA_FK200" in params else "100"
        params = {k: v for k, v in params.items()
                  if not k.startswith("CTX_AREA_")}
        rows: list[dict] = []
        async for data in self._paged(path, tr_id, params,
                                      ctx_suffix=suffix, what="체결 조회"):
            rows.extend(list(data.get(output) or []))
        return rows

    def _sell_tax_bps(self, when: datetime | None = None) -> float:
        """그 체결에 실제로 물린 증권거래세율.

        세율은 해마다 바뀌므로(`KRX_SELL_TAX_BPS`) 오늘이 아니라 **체결 시각**
        으로 찾습니다 — 과거 구간을 되짚는 세션이 오늘 요율을 소급하면, 세율이
        달랐던 구간의 손익이 통째로 틀립니다.

        과세 기준은 한국의 역년이라 KST 로 옮겨서 연도를 봅니다. UTC 그대로면
        1월 1일 오전(KST)의 체결이 전년도 세율로 매겨집니다.
        """
        if self.sell_tax_bps is not None:
            return self.sell_tax_bps
        return krx_sell_tax_bps(None if when is None else when.astimezone(KST))

    def _fill_fee(self, order: Order, quantity: Decimal, price: float,
                  when: datetime | None = None) -> float:
        notional = abs(float(quantity)) * price * float(order.symbol.multiplier)
        if order.symbol.quote_currency == "KRW":
            bps = self.commission_bps
            if order.side is OrderSide.SELL:
                bps += self._sell_tax_bps(when)   # 증권거래세 — 매도에만 붙는다
        else:
            bps = self.overseas_commission_bps
        return notional * bps / 10_000.0

    async def poll_fills(self) -> list[Fill]:
        if self.sends_orders:
            try:
                rows = await self._venue_executions()
            except Exception as exc:
                self.fill_channel_down(f"주식일별주문체결조회 실패: {exc}")
                return await super().poll_fills()

            by_id = {_field(row, "order_id"): row for row in rows
                     if _field(row, "order_id")}
            unpriced: list[str] = []
            unreadable: list[str] = []
            for order in list(self._orders.values()):
                if not order.status.is_open or not order.broker_id:
                    continue
                row = by_id.get(order.broker_id)
                if row is None:
                    continue
                try:
                    # 체결 수량은 필수입니다. 없거나 못 읽는 값을 0 으로 치면
                    # `newly <= 0` 으로 조용히 건너뛰어, 계좌에는 있는 체결이
                    # 장부에는 영영 없습니다 — 그 포지션엔 손절도 사이징도
                    # 안 걸립니다. 단가 없는 체결(`unpriced`)과 같은 결로
                    # 채널을 내립니다.
                    total = _number(row, "filled_qty", required=True)
                    newly = total - order.filled_qty
                    if newly <= 0:
                        continue
                    price = _delta_price(row, order, total, newly)
                except BrokerageError as exc:
                    unreadable.append(f"{order.broker_id}: {exc}")
                    continue
                if price <= 0:
                    # Booking a fill at 0 is worse than not booking it: it puts
                    # the shares in the book with no basis and no cash paid.
                    unpriced.append(order.broker_id)
                    continue
                # 수수료는 체결 시각으로 매깁니다 — 거래세율이 그 시점 기준
                # 이라, 같은 row 에서 읽은 시각을 그대로 넘겨야 합니다.
                ts = _ccld_ts(row)
                fill = Fill(
                    order_id=order.id, symbol=order.symbol, side=order.side,
                    quantity=newly, price=price,
                    fee=self._fill_fee(order, newly, price, ts),
                    ts=ts, tag=order.tag,
                    liquidity="taker" if order.type is OrderType.MARKET else "maker",
                )
                order.apply_fill(fill)
                self._pending_fills.append(fill)
            problems: list[str] = []
            if unreadable:
                problems.append(
                    "체결 row 를 읽을 수 없습니다 — " + "; ".join(unreadable[:3]))
            if unpriced:
                problems.append(
                    f"주문 {', '.join(unpriced)} 의 체결단가를 읽을 수 없습니다")
            if problems:
                self.fill_channel_down(" / ".join(problems))
            else:
                self.fill_channel_up()
        return await super().poll_fills()

    async def _venue_open_orders(self):
        # `_remaining` 이 못 읽는 row 에서 올리는 오류를 그대로 전파합니다.
        # 종료 직전의 미결 주문 수는 "확인 못 한 것은 남은 것" 이어야 하는데,
        # 못 읽는 row 를 0 으로 쳐서 빼면 계좌에 걸린 주문이 셈에서 빠집니다.
        return [row for row in await self._venue_executions() if _remaining(row) > 0]

    async def _venue_costs(self) -> dict[str, float]:
        return dict(self._venue_avg_cost)

    async def _venue_cash(self) -> float | None:
        # 예수금 is KRW; adopting it into a USD-denominated book would be a
        # 1 KRW = 1 USD conversion.
        if self.portfolio.base_currency != "KRW":
            return None
        return self._venue_deposit

    async def connect(self) -> None:
        if self.sends_orders:
            # An adapter that cannot read 체결 내역 buys shares and never learns
            # it owns them — no stop arms, no sizing sees them. Refuse the
            # session rather than start one that trades unseen.
            try:
                await self._venue_executions()
            except Exception as exc:
                raise BrokerageError(
                    "KIS 체결 조회(주식일별주문체결조회)를 사용할 수 없습니다 — "
                    f"체결을 확인할 수 없는 상태로는 주문하지 않습니다: {exc}"
                ) from exc
        await super().connect()

    def _holds_overseas(self) -> bool:
        symbols = [pos.symbol for pos in self.portfolio.positions.values()]
        symbols += [order.symbol for order in self._orders.values()]
        return any(sym.quote_currency != "KRW" for sym in symbols)

    async def _venue_positions(self) -> dict[str, Decimal]:
        out, costs = await self._domestic_balance()
        if self._holds_overseas():
            # The domestic balance endpoint does not list foreign holdings, and
            # a holding the venue never reports is one `sync()` would flatten.
            overseas_out, overseas_costs = await self._overseas_balance()
            out.update(overseas_out)
            costs.update(overseas_costs)
        self._venue_avg_cost = costs
        return out

    async def _domestic_balance(self) -> tuple[dict[str, Decimal], dict[str, float]]:
        out: dict[str, Decimal] = {}
        costs: dict[str, float] = {}
        deposit = None
        async for data in self._paged(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            TR_BALANCE[not self.paper],
            {"AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
             "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
             "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00"},
            ctx_suffix="100", what="잔고 조회",
        ):
            for row in data.get("output1") or []:
                qty = Decimal(str(row.get("hldg_qty") or 0))
                if qty:
                    key = f"kis:{row.get('pdno')}"
                    out[key] = qty
                    # 매입평균가. Without it an adopted position is born at
                    # basis 0 and every P&L number downstream is the market value.
                    costs[key] = float(row.get("pchs_avg_pric") or 0)
            summary = data.get("output2") or []
            if isinstance(summary, dict):
                summary = [summary]
            if summary and summary[0].get("dnca_tot_amt") not in (None, ""):
                # 예수금은 계좌 합계라 어느 장에 실려 와도 같은 값입니다.
                deposit = summary[0].get("dnca_tot_amt")
        self._venue_deposit = float(deposit) if deposit is not None else None
        return out, costs

    async def _overseas_balance(self) -> tuple[dict[str, Decimal], dict[str, float]]:
        out: dict[str, Decimal] = {}
        costs: dict[str, float] = {}
        async for data in self._paged(
            "/uapi/overseas-stock/v1/trading/inquire-balance",
            TR_OVERSEAS_BALANCE[not self.paper],
            {"OVRS_EXCG_CD": self.overseas_exchange,
             "TR_CRCY_CD": EXCHANGE_CURRENCY.get(self.overseas_exchange, "USD")},
            ctx_suffix="200", what="해외 잔고 조회",
        ):
            for row in data.get("output1") or []:
                qty = Decimal(str(row.get("ovrs_cblc_qty") or 0))
                if qty:
                    key = f"kis:{row.get('ovrs_pdno')}"
                    out[key] = qty
                    costs[key] = float(row.get("pchs_avg_pric") or 0)
        return out, costs

    async def close(self):
        await self._client.aclose()


# ── 체결 row parsing ─────────────────────────────────────────────────────
def _field(row: dict, name: str) -> str:
    for key in _CCLD_KEYS[name]:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _number(row: dict, name: str, *, required: bool = False) -> Decimal:
    """체결 row 의 숫자 하나. 못 읽는 값은 0 이 아니라 오류입니다.

    0 은 "없음" 이지 "모름" 이 아닙니다. 못 읽는 `tot_ccld_qty` 를 0 으로
    돌려주면 `poll_fills` 는 새 체결이 없다고 믿고 넘어가고, 못 읽는
    `rmn_qty` 를 0 으로 돌려주면 종료 직전 미결 주문 수에서 그 주문이
    빠집니다 — 둘 다 아무 표시 없이 조용히 틀립니다.

    비어 있는 값은 `required` 일 때만 오류입니다. 평균가·체결금액처럼 없을 수
    있는 자리(미체결 row)는 0 으로 두고, 호출자가 "단가 없음" 으로 다룹니다.
    """
    raw = _field(row, name).replace(",", "")
    if not raw:
        if required:
            raise BrokerageError(
                f"KIS 체결 row 에 {name} 값이 없습니다 ({'/'.join(_CCLD_KEYS[name])})"
            )
        return Decimal("0")
    try:
        value = Decimal(raw)
    except (ArithmeticError, ValueError) as exc:
        raise BrokerageError(
            f"KIS 체결 row 의 {name} 값을 숫자로 읽을 수 없습니다: {raw!r}"
        ) from exc
    if not value.is_finite():
        raise BrokerageError(f"KIS 체결 row 의 {name} 값이 유한한 숫자가 아닙니다: {raw!r}")
    return value


def _remaining(row: dict) -> Decimal:
    if _field(row, "remaining"):
        return _number(row, "remaining")
    # 잔량 필드가 없으면 주문수량과 체결수량 둘 다 있어야 셈이 됩니다. 하나라도
    # 없는 row 를 0 으로 계산하면 걸려 있는 주문이 "다 체결됨" 으로 빠집니다.
    return (_number(row, "order_qty", required=True)
            - _number(row, "filled_qty", required=True))


def _row_avg_price(row: dict, filled: Decimal) -> float:
    price = float(_number(row, "avg_price"))
    if price > 0:
        return price
    amount = float(_number(row, "filled_amount"))
    return amount / float(filled) if amount > 0 and filled > 0 else 0.0


def _delta_price(row: dict, order: Order, filled: Decimal, newly: Decimal) -> float:
    """Price of the *new* shares, not of everything filled so far.

    KIS reports a running average over the whole order. Booking each slice of a
    partially filled order at that average smears the later slices' price back
    over the earlier ones and leaves the position basis wrong.
    """
    avg = _row_avg_price(row, filled)
    if avg <= 0 or order.filled_qty <= 0:
        return avg
    prior = float(order.filled_qty) * order.avg_fill_price
    price = (avg * float(filled) - prior) / float(newly)
    return price if price > 0 else avg


def _ccld_ts(row: dict) -> datetime:
    """체결 시각 as UTC. KIS reports YYYYMMDD + HHMMSS in KST."""
    day, clock = _field(row, "date"), _field(row, "time")
    if len(day) == 8 and len(clock) == 6:
        try:
            stamp = datetime.strptime(day + clock, "%Y%m%d%H%M%S")
            return stamp.replace(tzinfo=KST).astimezone(UTC)
        except ValueError:
            pass
    return utcnow()
