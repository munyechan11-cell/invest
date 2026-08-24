"""Shared machinery for real-money brokerage adapters.

Everything here exists because live trading fails in ways backtests cannot:
duplicate submissions after a timeout, positions that drift from local state,
a sizing bug that would have been harmless in simulation. The guard rails are
deliberately blunt and deliberately on by default.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal

from quant.brokerage.base import Brokerage, BrokerageError
from quant.core.account import Portfolio
from quant.core.aio import LazyLock
from quant.core.types import Fill, Order, OrderStatus, RunMode, Symbol, utcnow

log = logging.getLogger("quant.brokerage.live")


class LiveBrokerage(Brokerage):
    """Base for venue adapters. Subclasses implement the four `_venue_*` hooks."""

    def __init__(
        self,
        portfolio: Portfolio,
        live: bool = False,
        paper_venue: bool = False,
        max_order_notional: float = 10_000.0,
        max_orders_per_minute: int = 20,
        reconcile_on_start: bool = True,
    ):
        self.portfolio = portfolio
        #: real money. Nothing else may set `run_mode` to LIVE.
        self.live = bool(live)
        #: the broker's own simulated account (KIS 모의투자, Alpaca paper).
        #: Real orders leave the process, but no real money is at stake — so it
        #: is a *destination*, not a permission level, and `live` stays False.
        self.paper_venue = bool(paper_venue) and not self.live
        #: `live` used to mean both "which venue" and "may we send at all",
        #: which made 모의투자 unreachable: the only mode that actually
        #: submitted was real money. Submission keys off this instead.
        self.sends_orders = self.live or self.paper_venue
        self.run_mode = RunMode.LIVE if self.live else RunMode.DRY_RUN
        self.max_order_notional = max_order_notional
        self.max_orders_per_minute = max_orders_per_minute
        self.reconcile_on_start = reconcile_on_start
        #: an adapter that cannot see its own fills is trading blind, so it
        #: stops submitting until the channel comes back. Adapters flip this
        #: through `fill_channel_down()` / `fill_channel_up()`.
        self.fill_channel_ok = True
        self.fill_channel_error = ""
        self._cash_warned = False
        self._orders: dict[str, Order] = {}
        self._recent_submits: list[float] = []
        #: (symbol, side, rounded qty) → timestamp, to stop a retry after a
        #: network timeout from becoming a second real position
        self._dedupe: dict[tuple, float] = {}
        self._pending_fills: list[Fill] = []
        self._lock = LazyLock()

    # ── hooks ────────────────────────────────────────────────────────────
    async def _venue_submit(self, order: Order) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    async def _venue_cancel(self, order: Order) -> bool:  # pragma: no cover
        raise NotImplementedError

    async def _venue_open_orders(self) -> list[dict]:  # pragma: no cover
        raise NotImplementedError

    async def _venue_positions(self) -> dict[str, Decimal]:  # pragma: no cover
        raise NotImplementedError

    async def _venue_costs(self) -> dict[str, float]:
        """Venue-reported average cost per position key. Optional."""
        return {}

    async def _venue_cash(self) -> float | None:
        """Venue-reported free cash in the portfolio's base currency. Optional."""
        return None

    # ── fill visibility ──────────────────────────────────────────────────
    def fill_channel_down(self, reason: str) -> None:
        if self.fill_channel_ok:
            log.error("%s fill channel is down: %s", self.name, reason)
        self.fill_channel_ok = False
        self.fill_channel_error = reason

    def fill_channel_up(self) -> None:
        if not self.fill_channel_ok:
            log.warning("%s fill channel recovered", self.name)
        self.fill_channel_ok = True
        self.fill_channel_error = ""

    # ── guard rails ──────────────────────────────────────────────────────
    def _guard(self, order: Order) -> None:
        allowed, reason = self._budget_check(order)
        if not allowed:
            raise BrokerageError(reason)
        if self.sends_orders and not self.fill_channel_ok:
            raise BrokerageError(
                f"체결 조회 채널이 끊겼습니다 ({self.fill_channel_error}) — "
                "체결을 볼 수 없는 상태에서는 주문하지 않습니다"
            )
        self.validate(order)
        price = order.limit_price or self.portfolio.position(order.symbol).last_price
        if not price or price <= 0:
            # An order nobody can price is an order nobody can bound. The
            # ceiling used to be skipped in exactly this case, which is every
            # market order opening a new position.
            raise BrokerageError(
                f"{order.symbol.ticker} 의 가격을 알 수 없어 주문 금액을 계산할 수 "
                "없습니다 — 한도를 확인할 수 없는 주문은 보내지 않습니다"
            )
        notional = abs(float(order.quantity)) * price * float(order.symbol.multiplier)
        if notional > self.max_order_notional:
            raise BrokerageError(
                f"order notional {notional:,.2f} exceeds the "
                f"{self.max_order_notional:,.2f} per-order limit — refusing to send"
            )
        now = time.monotonic()
        self._recent_submits = [t for t in self._recent_submits if now - t < 60]
        if len(self._recent_submits) >= self.max_orders_per_minute:
            raise BrokerageError(
                f"rate limit: {self.max_orders_per_minute} orders/minute already sent. "
                "A strategy that wants more than this is almost certainly looping."
            )
        key = (order.symbol.key, order.side.value, str(order.quantity))
        last = self._dedupe.get(key)
        if last and now - last < 10:
            raise BrokerageError(
                "identical order sent less than 10s ago — suppressed as a probable duplicate"
            )

    async def submit(self, order: Order) -> Order:
        async with self._lock:
            try:
                self._guard(order)
            except BrokerageError as exc:
                order.status = OrderStatus.REJECTED
                order.reject_reason = str(exc)
                log.warning("blocked order for %s: %s", order.symbol.ticker, exc)
                return order

            if not self.sends_orders:
                # Dry run: everything except the network call, so the same code
                # path, guard rails and logging are exercised.
                order.status = OrderStatus.SUBMITTED
                order.broker_id = f"dry-{order.id}"
                order.meta["dry_run"] = True
                log.info("[DRY RUN] would send %s %s %s @ %s", order.side.value,
                         order.quantity, order.symbol.ticker, order.limit_price or "market")
                self._orders[order.id] = order
                self._record_submission(order)
                return order

            try:
                broker_id = await self._venue_submit(order)
            except Exception as exc:
                order.status = OrderStatus.REJECTED
                order.reject_reason = f"venue rejected: {exc}"
                log.error("venue rejected %s: %s", order.symbol.ticker, exc)
                return order

            order.broker_id = broker_id
            order.status = OrderStatus.SUBMITTED
            order.updated_at = utcnow()
            self._orders[order.id] = order
            self._record_submission(order)
            log.info("sent %s %s %s (broker id %s)", order.side.value, order.quantity,
                     order.symbol.ticker, broker_id)
            return order

    def _record_submission(self, order: Order) -> None:
        """Book an accepted order against the rate limit, the daily budget and
        the duplicate window.

        A dry run has to run this too. Skipping it left the 하루 거래대금/주문건수
        caps and the duplicate suppressor at zero usage in the only mode the
        operator gets to rehearse them in.
        """
        now = time.monotonic()
        self._recent_submits.append(now)
        self._budget_record(order)
        self._dedupe[(order.symbol.key, order.side.value, str(order.quantity))] = now

    async def cancel(self, order: Order) -> bool:
        if not self.sends_orders or order.broker_id is None:
            self._orders.pop(order.id, None)
            order.status = OrderStatus.CANCELED
            return True
        try:
            ok = await self._venue_cancel(order)
        except Exception as exc:
            log.warning("cancel failed for %s: %s", order.symbol.ticker, exc)
            return False
        if ok:
            await self._reap(order)
            # 취소가 닿기 전에 전부 체결됐다면 그건 취소된 주문이 아닙니다.
            # CANCELED 로 덮으면 방금 booked 한 체결과 상태가 서로 어긋납니다.
            if order.remaining > 0:
                order.status = OrderStatus.CANCELED
            self._orders.pop(order.id, None)
        return ok

    async def _reap(self, order: Order) -> Decimal:
        """장부에서 빼기 직전에, 취소보다 먼저 체결된 몫을 한 번 더 걷습니다.

        취소를 냈다고 취소된 것은 아닙니다. 주문이 호가에 걸려 있는 동안
        상대가 체결시켜 버렸을 수 있고, 그 체결은 취소 요청과 경주해서
        이깁니다. 거래소에는 체결로 남았는데 여기서는 취소로 남으면, 그 주식은
        계좌에 있으면서 엔진의 장부에는 없습니다 — 손절도 사이징도 하루 한도도
        걸리지 않는 포지션이 생깁니다.

        `poll_fills` 가 `self._orders` 를 훑기 때문에, 빼기 **전에** 불러야
        합니다. 순서를 바꾸면 이 함수는 아무것도 못 찾습니다.

        돌려주는 것은 이 주문이 실제로 체결된 총 수량입니다. 상위
        (`quant/execution/base.py`)가 "취소가 체결에 졌다" 를 판정할 때 읽는
        `order.filled_qty` 도 여기서 갱신됩니다.
        """
        try:
            drained = await self.poll_fills()
        except Exception as exc:            # noqa: BLE001 — 취소는 계속돼야 합니다
            log.warning("취소 직전 체결 확인 실패 %s: %s", order.symbol.ticker, exc)
            return order.filled_qty
        if drained:
            # poll_fills 는 큐를 비웁니다. 엔진이 아직 가져가지 않은 체결이라
            # 그대로 두면 우리가 삼킨 것이 됩니다. 시간 순서대로 되돌려 놓습니다.
            self._pending_fills[:0] = drained
        return order.filled_qty

    async def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status.is_open]

    async def poll_fills(self) -> list[Fill]:
        """Drain fills discovered since the last call. The engine calls this."""
        out, self._pending_fills = self._pending_fills, []
        return out

    def _known_symbols(self) -> dict[str, Symbol]:
        """Venue position key → Symbol, for every instrument this session knows.

        A venue key the engine cannot map to a Symbol cannot become a position:
        quantity alone is not tradable — lot size, tick size and multiplier all
        come from the Symbol.
        """
        symbols = {key: pos.symbol for key, pos in self.portfolio.positions.items()}
        for order in self._orders.values():
            symbols.setdefault(order.symbol.key, order.symbol)
        return symbols

    async def _report_cash(self, report: dict) -> None:
        """Surface the venue's cash without adopting it.

        Quantities are a claim about the world and the venue is always right
        about them. Cash is not the same kind of fact: `portfolio.cash` is the
        budget the operator gave *this strategy*, which is routinely a slice of
        a larger account. Overwriting it would silently resize every position
        to the whole account. Adopting a position already debits what it cost,
        so the book stays honest; a divergence beyond that is worth saying out
        loud, not acting on.
        """
        try:
            cash = await self._venue_cash()
        except Exception as exc:
            log.debug("venue cash unavailable: %s", exc)
            return
        if cash is None:
            return
        local = self.portfolio.cash
        report["cash"] = {"local": local, "venue": float(cash)}
        material = abs(float(cash) - local) > max(abs(local), 1.0) * 0.01
        if material and not self._cash_warned:
            log.warning("현금 불일치: 엔진 장부 %.0f, 계좌 %.0f — 이 전략의 예산은 "
                        "장부 쪽입니다. 계좌 전체를 굴릴 생각이면 "
                        "portfolio.starting_cash 를 맞추세요.", local, float(cash))
        self._cash_warned = material

    async def sync(self) -> dict:
        """Reconcile local positions against the venue. The venue wins."""
        try:
            venue = await self._venue_positions()
        except Exception as exc:
            log.error("position sync failed: %s", exc)
            return {"ok": False, "error": str(exc)}

        report = {"ok": True, "venue_positions": {k: float(v) for k, v in venue.items()}}
        if not self.sends_orders:
            # A dry run's positions were never sent anywhere, so the venue has
            # never heard of them. Letting it "win" here flattened the whole
            # simulation on every cycle.
            report.update({"drift": {}, "corrected": {}, "uncorrected": {},
                           "observed_only": "no orders are being sent to this venue"})
            return report

        try:
            costs = await self._venue_costs()
        except Exception as exc:
            log.debug("venue average costs unavailable: %s", exc)
            costs = {}
        symbols = self._known_symbols()

        corrected: dict[str, dict] = {}
        uncorrected: dict[str, dict] = {}
        for key, qty in venue.items():
            local = self.portfolio.positions.get(key)
            local_qty = local.quantity if local else Decimal("0")
            if local_qty == qty:
                continue
            entry = {"local": float(local_qty), "venue": float(qty)}
            if local is None:
                symbol = symbols.get(key)
                if symbol is None:
                    uncorrected[key] = entry
                    continue
                local = self.portfolio.position(symbol)
            avg = float(costs.get(key) or 0.0)
            if avg <= 0:
                avg = local.avg_price
            adopted = qty - local_qty
            local.quantity = qty
            if avg > 0:
                if local.avg_price <= 0:
                    local.avg_price = avg
                # Shares that appeared out of nowhere were paid for with real
                # cash. Booking the quantity and not the money invents equity.
                self.portfolio.cash -= (
                    float(adopted) * avg * float(local.symbol.multiplier)
                )
            corrected[key] = entry
        for key, pos in self.portfolio.positions.items():
            if not pos.is_flat and key not in venue:
                corrected[key] = {"local": float(pos.quantity), "venue": 0.0}
                # Something closed this outside the engine. Return the cost
                # basis rather than deleting the capital: the exit price is
                # unknowable from here, and guessing at it fabricates a
                # realized gain or loss. Dropping the quantity alone left the
                # book permanently poorer by the whole position.
                self.portfolio.cash += (float(pos.quantity) * pos.avg_price
                                        * float(pos.symbol.multiplier))
                pos.quantity = Decimal("0")
                pos.avg_price = 0.0

        await self._report_cash(report)

        if corrected:
            log.warning("position drift corrected against the venue: %s", corrected)
        if uncorrected:
            # Deliberately louder than the corrected case: these holdings exist
            # at the broker and the engine cannot even name them, so no stop,
            # no sizing rule and no daily cap applies to them.
            log.error(
                "UNCORRECTED position drift — the engine is trading blind on %s. "
                "These are held at %s but map to no symbol in this run.",
                sorted(uncorrected), self.name,
            )
        report.update({"drift": {**corrected, **uncorrected},
                       "corrected": corrected, "uncorrected": uncorrected})
        return report

    async def connect(self) -> None:
        if self.reconcile_on_start:
            await self.sync()
        if self.live:
            mode = "LIVE — real money"
        elif self.paper_venue:
            mode = "PAPER VENUE — orders go to the broker's simulated account"
        else:
            mode = "DRY RUN — no orders will be sent"
        log.warning("%s connected in %s mode", self.name, mode)
