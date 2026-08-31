"""Shared machinery for real-money brokerage adapters.

Everything here exists because live trading fails in ways backtests cannot:
duplicate submissions after a timeout, positions that drift from local state,
a sizing bug that would have been harmless in simulation. The guard rails are
deliberately blunt and deliberately on by default.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from decimal import Decimal

from quant.brokerage.base import Brokerage, BrokerageError
from quant.core.account import Portfolio
from quant.core.aio import LazyLock
from quant.core.types import (
    Fill,
    Order,
    OrderStatus,
    RunMode,
    Symbol,
    utcnow,
)

log = logging.getLogger("quant.brokerage.live")


@dataclass(frozen=True)
class _CapitalOrderCheckpoint:
    """Venue-authoritative position state before one accepted live order."""

    order: Order
    quantity_before: Decimal


class LiveBrokerage(Brokerage):
    """Base for venue adapters. Subclasses implement the four `_venue_*` hooks."""

    #: Most brokers are intentionally *not* account-authoritative: a strategy may
    #: own a 1,000,000 KRW slice of a 50,000,000 KRW account.  Adopting the whole
    #: account there silently multiplies risk.  A venue adapter must opt in only
    #: when its live contract is explicitly "trade this account's actual capital".
    venue_capital_truth = False

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
        self._capital_ready = not (self.live and self.venue_capital_truth)
        self._capital_error = ""
        self._venue_buying_power: float | None = None
        # Accepted exposure-increasing orders reserve buying power locally until
        # the adapter observes a terminal state. Broker balance endpoints can
        # lag order acceptance; a fresh-looking but stale response must not
        # erase that reservation and permit a second order against the same cash.
        # order id -> (the mutable Order, buying power immediately before it)
        # Terminal status alone cannot release this reservation: an executions
        # endpoint often reports FILLED before buying-power reflects the debit.
        self._capital_reservations: dict[str, tuple[Order, float]] = {}
        # Every accepted truth-mode order stays here beyond FILLED/CANCELED until
        # per-symbol holdings and the aggregate account cash prove that the venue
        # endpoints include the same batch of fills the engine has booked.
        self._capital_order_checkpoints: dict[str, _CapitalOrderCheckpoint] = {}
        # A zero-fill CANCELED/REJECTED result is meaningful only when an adapter
        # successfully fetched and validated that terminal order detail.  Status
        # alone is not evidence: a failed lookup followed by local cancellation
        # must retain its reservation. Adapters explicitly mark that observation
        # through ``_mark_terminal_observed``.
        self._capital_terminal_observed: set[str] = set()
        self._capital_synced_at: float | None = None
        # Armed by LiveTrader only when StateStore restored a previously
        # venue-authoritative run. The first post-restart sync must match that
        # durable cash/quantity baseline before this process may adopt reality.
        self._restored_venue_truth_guard = False
        # A process that did not reach verified clean shutdown may have accepted
        # an order whose terminal fill has not reached the account endpoints yet.
        # No finite number of matching startup polls disproves that possibility.
        self._restored_reconciliation_required = False
        self._orders: dict[str, Order] = {}
        self._recent_submits: list[float] = []
        #: (symbol, side, rounded qty) → timestamp, to stop a retry after a
        #: network timeout from becoming a second real position
        self._dedupe: dict[tuple, float] = {}
        self._pending_fills: list[Fill] = []
        self._lock = LazyLock()
        # A capital hook may cache one broker response for the immediately
        # following positions/cost hooks.  Serialise the whole reconciliation,
        # otherwise two callers can combine capital from snapshot A with
        # positions from snapshot B.
        self._sync_lock = LazyLock()

    # ── hooks ────────────────────────────────────────────────────────────
    async def _venue_submit(self, order: Order) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    async def _pre_venue_submit(self, order: Order) -> None:
        """Last side-effect-free check immediately before a live order.

        Most adapters need no extra request here.  A venue that cannot attach
        ownership metadata to its open-order list can override this hook to
        reject operator/app orders that appeared after startup, without
        coupling the generic brokerage layer to that venue.
        """
        return None

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

    async def _venue_capital(self) -> dict | None:
        """Account-authoritative capital snapshot. Explicit opt-in only.

        ``venue_capital_truth`` adapters return ``cash`` (cash buying power) and
        ``holdings_value`` in the portfolio base currency.  Optional
        ``gross_exposure``/``net_exposure`` override the long-only defaults and
        ``currency`` is validated when present.  The base class deliberately has
        no cache: every sync, including a pre-buy check, asks the venue again.
        """
        return None

    @property
    def uses_venue_capital(self) -> bool:
        """Whether this *real-money* instance adopts venue capital."""
        return self.live and self.venue_capital_truth

    @property
    def account_ready(self) -> bool:
        return self._capital_ready

    def _capital_failed(self, error: str) -> None:
        self._capital_ready = False
        self._capital_error = error

    def expect_restored_venue_truth(
        self,
        restored: bool,
        *,
        reconciliation_required: bool = False,
    ) -> None:
        """Require the first sync to explain any drift from durable live state.

        A new process has no in-memory broker order ids. If an accepted order
        filled between a crash and restart, silently letting venue quantities
        overwrite the restored book loses the closed trade and its daily loss.
        StateStore tells us explicitly whether this is a restored venue ledger;
        first-ever account adoption deliberately leaves this guard off.
        """
        self._restored_venue_truth_guard = bool(restored and self.uses_venue_capital)
        self._restored_reconciliation_required = bool(
            reconciliation_required and self.uses_venue_capital
        )

    async def shutdown_remote_open_order_count(self) -> int:
        """Return a verified remote open-order count for safe shutdown.

        A local cancel acknowledgement is not the venue's final state. Every
        live adapter already implements ``_venue_open_orders`` for reconciliation;
        shutdown uses one last bounded API read and treats lookup failure as
        uncertainty rather than an empty account.
        """
        if not self.sends_orders:
            return 0
        remote = await self._venue_open_orders()
        if not isinstance(remote, list):
            raise BrokerageError("증권사 미결 주문 응답이 목록이 아닙니다")
        return len(remote)

    def _mark_terminal_observed(self, order: Order) -> None:
        """Record one successfully validated terminal order-detail response.

        Venue adapters call this only after the complete detail payload has been
        parsed and its terminal status applied to ``order``.  Keeping this as a
        separate signal prevents a network/parse failure from masquerading as a
        genuine zero-fill cancellation.
        """
        if order.status.is_open:
            raise BrokerageError(
                f"{order.symbol.ticker} 주문은 아직 종료 상태가 아닙니다"
            )
        if order.id in self._capital_order_checkpoints:
            self._capital_terminal_observed.add(order.id)

    def _same_symbol_capital_pending(self, order: Order) -> bool:
        """Whether this symbol already has an un-reconciled truth-mode order."""
        key = order.symbol.key
        return any(
            checkpoint.order.symbol.key == key
            for checkpoint in self._capital_order_checkpoints.values()
        ) or any(
            tracked.symbol.key == key and tracked.status.is_open
            for tracked in self._orders.values()
        )

    def _validated_capital(self, raw: dict | None) -> dict[str, float]:
        if not isinstance(raw, dict):
            raise BrokerageError("증권사 자산 조회가 빈 응답을 돌려줬습니다")
        currency = raw.get("currency")
        if currency is not None and str(currency).upper() != self.portfolio.base_currency.upper():
            raise BrokerageError(
                f"증권사 자산 통화 {currency} 와 전략 기준 통화 "
                f"{self.portfolio.base_currency} 가 다릅니다"
            )
        out: dict[str, float] = {}
        for key in ("cash", "holdings_value"):
            try:
                value = float(raw[key])
            except (KeyError, TypeError, ValueError) as exc:
                raise BrokerageError(f"증권사 자산 응답의 {key} 값을 읽을 수 없습니다") from exc
            if not math.isfinite(value) or value < 0:
                raise BrokerageError(f"증권사 자산 응답의 {key} 값이 올바르지 않습니다")
            out[key] = value
        for key in ("gross_exposure", "net_exposure"):
            if raw.get(key) is None:
                continue
            try:
                value = float(raw[key])
            except (TypeError, ValueError) as exc:
                raise BrokerageError(f"증권사 자산 응답의 {key} 값을 읽을 수 없습니다") from exc
            if not math.isfinite(value) or (key == "gross_exposure" and value < 0):
                raise BrokerageError(f"증권사 자산 응답의 {key} 값이 올바르지 않습니다")
            out[key] = value
        return out

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
        if self.uses_venue_capital and self._restored_reconciliation_required:
            # The stored position itself may be stale: what looks like an exit
            # locally can be a brand-new short after the unseen terminal SELL.
            # Recovery happens in the Toss app, never through this uncertain
            # process.
            raise BrokerageError(
                "비정상 종료된 실계좌는 수동 재조정 전까지 어떤 주문도 보내지 않습니다"
            )
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
        increasing = not self._reduces_position(order)
        if self.uses_venue_capital and increasing:
            if not self._capital_ready:
                detail = self._capital_error or "아직 실제 계좌 자산을 확인하지 못했습니다"
                raise BrokerageError(
                    f"실계좌 조회가 확인되지 않아 신규 주문을 보내지 않습니다 ({detail})"
                )
            if self._venue_buying_power is None:
                raise BrokerageError("증권사 매수 가능 금액을 알 수 없어 신규 주문을 보내지 않습니다")
            if notional > self._venue_buying_power + 1e-9:
                raise BrokerageError(
                    f"주문 금액 {notional:,.2f} 이 증권사 매수 가능 금액 "
                    f"{self._venue_buying_power:,.2f} 을 넘습니다"
                )
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
            synced_for_pending = False
            if self.uses_venue_capital and self._same_symbol_capital_pending(order):
                # A terminal checkpoint may already be reflected at the venue;
                # give one serialized snapshot the chance to retire it. Open
                # orders return from sync before network I/O, while an outage
                # leaves the checkpoint intact and therefore remains fail-closed.
                await self.sync()
                synced_for_pending = True
            if self.uses_venue_capital and self._same_symbol_capital_pending(order):
                # Two reductions can both be valid against the same pre-fill
                # quantity, then leave mutually impossible settlement targets.
                # Serialize only this symbol; an unrelated position must remain
                # closable during the first order's account-settlement lag.
                order.status = OrderStatus.REJECTED
                order.reject_reason = (
                    "같은 종목의 이전 미결 주문 또는 종료 주문이 실계좌 장부에 "
                    "반영될 때까지 추가 주문을 보내지 않습니다"
                )
                log.warning(
                    "blocked overlapping truth order for %s", order.symbol.ticker
                )
                return order
            # The official buying-power contract recommends checking immediately
            # before an order.  Do that for new exposure only: a lookup outage
            # must fail closed on buys, but it must not trap an existing position
            # by blocking its exit.  ``sync`` also refreshes venue holdings before
            # deciding whether this order really reduces exposure.
            if (self.uses_venue_capital and not synced_for_pending
                    and not self._reduces_position(order)):
                await self.sync()
            increasing = not self._reduces_position(order)
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
                await self._pre_venue_submit(order)
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
            if self.uses_venue_capital:
                self._capital_order_checkpoints[order.id] = _CapitalOrderCheckpoint(
                    order=order,
                    quantity_before=self.portfolio.quantity(order.symbol),
                )
            if (self.uses_venue_capital and increasing
                    and self._venue_buying_power is not None):
                price = order.limit_price or self.portfolio.position(order.symbol).last_price
                spent = abs(float(order.quantity)) * float(price) * float(
                    order.symbol.multiplier)
                self._capital_reservations[order.id] = (
                    order, self._venue_buying_power,
                )
                self._venue_buying_power = max(0.0, self._venue_buying_power - spent)
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
            _, fills_observed = await self._reap(order)
            # 취소가 닿기 전에 전부 체결됐다면 그건 취소된 주문이 아닙니다.
            # CANCELED 로 덮으면 방금 booked 한 체결과 상태가 서로 어긋납니다.
            if order.remaining > 0:
                order.status = OrderStatus.CANCELED
            self._orders.pop(order.id, None)
            # A confirmed cancel with no fill spent no cash, so its reservation
            # can be released immediately. A partial/full fill stays reserved
            # until a later buying-power snapshot proves that debit is visible.
            if fills_observed and order.filled_qty <= 0:
                self._capital_reservations.pop(order.id, None)
                self._capital_order_checkpoints.pop(order.id, None)
                self._capital_terminal_observed.discard(order.id)
        return ok

    async def _reap(self, order: Order) -> tuple[Decimal, bool]:
        """장부에서 빼기 직전에, 취소보다 먼저 체결된 몫을 한 번 더 걷습니다.

        취소를 냈다고 취소된 것은 아닙니다. 주문이 호가에 걸려 있는 동안
        상대가 체결시켜 버렸을 수 있고, 그 체결은 취소 요청과 경주해서
        이깁니다. 거래소에는 체결로 남았는데 여기서는 취소로 남으면, 그 주식은
        계좌에 있으면서 엔진의 장부에는 없습니다 — 손절도 사이징도 하루 한도도
        걸리지 않는 포지션이 생깁니다.

        `poll_fills` 가 `self._orders` 를 훑기 때문에, 빼기 **전에** 불러야
        합니다. 순서를 바꾸면 이 함수는 아무것도 못 찾습니다.

        돌려주는 것은 이 주문이 실제로 체결된 총 수량과 그 수량을 이번에
        확인했는지 여부입니다. 조회 실패를 미체결 0으로 취급하면 아직 증권사
        응답에 반영되지 않은 현금을 다음 주문이 다시 쓸 수 있습니다. 상위
        (`quant/execution/base.py`)가 "취소가 체결에 졌다" 를 판정할 때 읽는
        `order.filled_qty` 도 여기서 갱신됩니다.
        """
        try:
            drained = await self.poll_fills()
        except Exception as exc:            # noqa: BLE001 — 취소는 계속돼야 합니다
            log.warning("취소 직전 체결 확인 실패 %s: %s", order.symbol.ticker, exc)
            return order.filled_qty, False
        if drained:
            # poll_fills 는 큐를 비웁니다. 엔진이 아직 가져가지 않은 체결이라
            # 그대로 두면 우리가 삼킨 것이 됩니다. 시간 순서대로 되돌려 놓습니다.
            self._pending_fills[:0] = drained
        return order.filled_qty, True

    async def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status.is_open]

    def drain_pending_fills(self) -> list[Fill]:
        """Drain only fills an adapter has already validated locally.

        This method performs no venue I/O.  It exists so the engine can preserve
        a verified fill from the beginning of a multi-order poll even when a
        later order-detail request fails.  Draining the list is exact-once: a
        retry cannot book the same cached fill again.
        """
        out, self._pending_fills = self._pending_fills, []
        return out

    async def poll_fills(self) -> list[Fill]:
        """Drain fills discovered since the last call. The engine calls this."""
        return self.drain_pending_fills()

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
        """Serialize one account snapshot from capital through positions/costs."""
        async with self._sync_lock:
            return await self._sync_once()

    async def _sync_once(self) -> dict:
        """Reconcile local positions against the venue. The venue wins."""
        capital: dict[str, float] | None = None
        if self.uses_venue_capital:
            open_orders = [order for order in self._orders.values()
                           if order.status.is_open]
            if open_orders:
                # Order detail and holdings/buying-power are separate venue
                # requests with no shared snapshot version. A fill can land
                # after the first poll but before holdings: adopting that
                # position now and booking the cumulative fill next poll would
                # apply one trade twice. Stay on the locally booked state until
                # every tracked order is terminal; temporary under-allocation
                # is cheaper than fabricated cash/positions.
                reason = (
                    f"미결 주문 {len(open_orders)}건의 체결 정산이 끝나지 않았습니다"
                )
                self._capital_failed(reason)
                return {
                    "ok": False,
                    "error": reason,
                    "capital_ready": False,
                    "transient": "open_orders",
                    "open_orders": [order.id for order in open_orders],
                }
            # Mark unavailable *before* the network call.  Retaining yesterday's
            # True through a failed refresh is exactly how a stale 800k balance
            # becomes permission for today's real order.
            self._capital_ready = False
            try:
                capital = self._validated_capital(await self._venue_capital())
            except Exception as exc:  # noqa: BLE001 — adapter failures become a report
                self._capital_failed(str(exc))
                log.error("account capital sync failed: %s", exc)
                return {"ok": False, "error": str(exc), "capital_ready": False}
        try:
            venue = await self._venue_positions()
        except Exception as exc:
            if self.uses_venue_capital:
                self._capital_failed(str(exc))
            log.error("position sync failed: %s", exc)
            return {"ok": False, "error": str(exc),
                    "capital_ready": self._capital_ready}

        report = {"ok": True, "venue_positions": {k: float(v) for k, v in venue.items()}}
        if not self.sends_orders:
            # A dry run's positions were never sent anywhere, so the venue has
            # never heard of them. Letting it "win" here flattened the whole
            # simulation on every cycle.
            report.update({"drift": {}, "corrected": {}, "uncorrected": {},
                           "observed_only": "no orders are being sent to this venue"})
            return report

        if self.uses_venue_capital and self._restored_reconciliation_required:
            recovery = (
                "이전 실행이 증권사 미결 주문·마지막 체결 확인을 마치지 못해, "
                "현재 응답이 저장 장부와 같아도 늦게 반영될 체결이 없다고 증명할 "
                "수 없습니다. 자동 주문 이력 재구성은 지원하지 않습니다. 토스 앱에서 "
                "당일 체결·미체결 주문과 실제 보유수량·현금을 확인하고 일일 손실 "
                "한도를 수동 대조한 뒤, 기존 상태를 보관하고 새 실행으로 초기화하세요"
            )
            error = f"비정상 종료된 실계좌 실행은 자동 재개하지 않습니다. {recovery}"
            self._capital_failed(error)
            report.update({
                "ok": False,
                "error": error,
                "capital_ready": False,
                "transient": "restored_run_requires_reconciliation",
                "recovery_required": True,
                "recovery": recovery,
            })
            return report

        if (self.uses_venue_capital and self._restored_venue_truth_guard
                and not self._orders and not self._capital_order_checkpoints):
            assert capital is not None
            local_positions = {
                key: position.quantity
                for key, position in self.portfolio.positions.items()
                if not position.is_flat
            }
            restored_drift: dict[str, dict] = {}
            for key in sorted(set(local_positions) | set(venue)):
                local_qty = local_positions.get(key, Decimal("0"))
                venue_qty = venue.get(key, Decimal("0"))
                if local_qty != venue_qty:
                    restored_drift[key] = {
                        "stored": float(local_qty), "venue": float(venue_qty),
                    }
            stored_cash = self.portfolio.cash
            cash_tolerance = max(0.01, abs(stored_cash) * 1e-9)
            cash_matches = (
                math.isfinite(stored_cash)
                and stored_cash >= 0
                and abs(capital["cash"] - stored_cash) <= cash_tolerance
            )
            if restored_drift or not cash_matches:
                recovery = (
                    "자동 주문 이력이 없어 이 차이를 체결로 재구성할 수 없습니다. "
                    "토스 앱에서 당일 체결·미체결 주문과 실제 보유수량을 확인하고, "
                    "일일 손실 한도를 수동 대조한 뒤 기존 상태를 보관하고 새 실행으로 "
                    "초기화하세요. 확인 전에는 자동으로 장부를 덮거나 주문을 재개하지 않습니다"
                )
                reason = "재시작 전 저장 장부와 현재 실계좌의 수량 또는 현금이 다릅니다"
                error = f"{reason}. {recovery}"
                self._capital_failed(error)
                report.update({
                    "ok": False,
                    "error": error,
                    "capital_ready": False,
                    "transient": "unexplained_restored_account_drift",
                    "recovery_required": True,
                    "recovery": recovery,
                    "restored_drift": {
                        "cash": ({
                            "stored": stored_cash, "venue": capital["cash"],
                        } if not cash_matches else {}),
                        "positions": restored_drift,
                    },
                })
                return report

        try:
            costs = await self._venue_costs()
        except Exception as exc:
            log.debug("venue average costs unavailable: %s", exc)
            costs = {}

        if self.uses_venue_capital and self._capital_order_checkpoints:
            assert capital is not None
            proofs: dict[str, dict] = {}
            for order_id, checkpoint in self._capital_order_checkpoints.items():
                tracked = checkpoint.order
                expected_qty = (
                    checkpoint.quantity_before
                    + tracked.filled_qty * tracked.side.sign
                )
                local_qty = self.portfolio.quantity(tracked.symbol)
                venue_qty = venue.get(tracked.symbol.key, Decimal("0"))
                reason = ""
                if tracked.status.is_open:
                    reason = "order checkpoint is not terminal"
                elif (not tracked.filled_qty.is_finite()
                      or tracked.filled_qty < 0
                      or tracked.filled_qty > tracked.quantity):
                    reason = "terminal cumulative fill quantity is invalid"
                elif tracked.filled_qty == 0:
                    if order_id not in self._capital_terminal_observed:
                        reason = "terminal zero-fill order has not been observed"
                    elif local_qty != checkpoint.quantity_before:
                        reason = "local quantity changed despite a zero-fill terminal"
                    elif venue_qty != checkpoint.quantity_before:
                        reason = "venue quantity changed despite a zero-fill terminal"
                else:
                    if local_qty != expected_qty:
                        reason = "the cumulative fill has not been booked locally"
                    elif venue_qty != expected_qty:
                        reason = "venue holdings have not reflected the cumulative fill"
                proofs[order_id] = {
                    "symbol": tracked.symbol.key,
                    "side": tracked.side.value,
                    "local": float(local_qty),
                    "venue": float(venue_qty),
                    "expected": float(expected_qty),
                    "reason": reason,
                }

            # Cash is one account-wide number, so it cannot prove each order in
            # isolation.  A BUY and a risk-reducing SELL may settle together:
            # checking the BUY against its old cash ceiling and the SELL against
            # its old cash floor makes both look stale even when the final account
            # is exact.  ``portfolio.cash`` already contains every verified fill
            # exactly once, including its official fees. Compare that aggregate
            # ledger with the account snapshot only after every per-symbol
            # quantity proof passes.
            quantity_failed = any(proof["reason"] for proof in proofs.values())
            expected_cash = self.portfolio.cash
            cash_tolerance = max(0.01, abs(expected_cash) * 1e-9)
            cash_matches = (
                not quantity_failed
                and abs(capital["cash"] - expected_cash) <= cash_tolerance
            )
            if not quantity_failed and not cash_matches:
                for proof in proofs.values():
                    proof["reason"] = (
                        "venue buying power does not match the exact-once local fill ledger"
                    )
            elif quantity_failed:
                # Settlement is a batch: removing the orders that happen to pass
                # while another one is still stale changes the cash proof's basis
                # underneath the next sync. Keep every checkpoint until the same
                # snapshot proves the whole batch.
                for proof in proofs.values():
                    if not proof["reason"]:
                        proof["reason"] = (
                            "another terminal order in this settlement batch is unresolved"
                        )

            unresolved = {
                order_id: proof
                for order_id, proof in proofs.items()
                if proof["reason"]
            }
            if unresolved:
                reason = (
                    f"종료 주문 {len(unresolved)}건이 계좌 자산에 아직 반영되지 않았습니다"
                )
                self._capital_failed(reason)
                report.update({
                    "ok": False,
                    "error": reason,
                    "capital_ready": False,
                    "transient": "terminal_order_settlement",
                    "unsettled_orders": unresolved,
                })
                return report
            for order_id in tuple(self._capital_order_checkpoints):
                self._capital_order_checkpoints.pop(order_id, None)
                self._capital_reservations.pop(order_id, None)
                self._capital_terminal_observed.discard(order_id)
        symbols = self._known_symbols()

        corrected: dict[str, dict] = {}
        uncorrected: dict[str, dict] = {}
        # Account-authoritative capital has to be an all-or-nothing snapshot.
        # Discover unmappable positions before touching local cash or quantities;
        # otherwise a failed sync leaves a half-adopted book behind.
        if self.uses_venue_capital:
            for key, qty in venue.items():
                local = self.portfolio.positions.get(key)
                local_qty = local.quantity if local else Decimal("0")
                symbol = local.symbol if local is not None else symbols.get(key)
                entry = {"local": float(local_qty), "venue": float(qty)}
                if qty != 0 and symbol is None:
                    entry["reason"] = "symbol is not mapped in this strategy"
                    uncorrected[key] = entry
                    continue
                avg = float(costs.get(key) or (local.avg_price if local else 0.0))
                if qty != 0 and avg <= 0 and (local is None or local.avg_price <= 0):
                    entry["reason"] = "venue average cost is unavailable"
                    uncorrected[key] = entry
            if uncorrected:
                reason = ("증권사 보유 종목을 전략 장부에 안전하게 연결할 수 없습니다: "
                          + ", ".join(sorted(uncorrected)))
                self._capital_failed(reason)
                log.error("UNCORRECTED position drift — refusing new exposure: %s",
                          sorted(uncorrected))
                return {
                    "ok": False,
                    "error": reason,
                    "venue_positions": {k: float(v) for k, v in venue.items()},
                    "drift": dict(uncorrected),
                    "corrected": {},
                    "uncorrected": uncorrected,
                    "capital_ready": False,
                }
        for key, qty in venue.items():
            local = self.portfolio.positions.get(key)
            local_qty = local.quantity if local else Decimal("0")
            if local_qty == qty:
                # A legacy state row can have the right quantity but no cost.
                # Reconciliation should heal that too; quantity equality alone
                # does not make the position safe for stop/PnL calculations.
                if local is not None and local.avg_price <= 0:
                    avg = float(costs.get(key) or 0.0)
                    if avg > 0:
                        local.avg_price = avg
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
                if not self.uses_venue_capital:
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
                if not self.uses_venue_capital:
                    self.portfolio.cash += (float(pos.quantity) * pos.avg_price
                                            * float(pos.symbol.multiplier))
                pos.quantity = Decimal("0")
                pos.avg_price = 0.0

        if self.uses_venue_capital:
            assert capital is not None
            first = self.portfolio.adopt_venue_capital(
                cash=capital["cash"],
                holdings_value=capital["holdings_value"],
                gross_exposure=capital.get("gross_exposure"),
                net_exposure=capital.get("net_exposure"),
            )
            fresh_buying_power = capital["cash"]
            if (self._capital_reservations
                    and self._venue_buying_power is not None):
                # The venue may report its pre-order cash until an accepted
                # order settles. Keep the stricter local reservation until each
                # accepted order is either canceled unfilled or its actual fill
                # debit is visible in buying power.
                self._venue_buying_power = min(
                    fresh_buying_power, self._venue_buying_power)
            else:
                self._venue_buying_power = fresh_buying_power
            self._capital_ready = True
            self._capital_error = ""
            self._capital_synced_at = time.monotonic()
            self._restored_venue_truth_guard = False
            report["capital"] = {
                "source": "venue", "cash": capital["cash"],
                "holdings_value": capital["holdings_value"],
                "equity": self.portfolio.equity,
                "baseline_initialized": first,
                "available_for_new_orders": self._venue_buying_power,
            }
            report["capital_ready"] = True
        else:
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
        report = None
        if self.reconcile_on_start:
            report = await self.sync()
        if self.uses_venue_capital and (
            report is None or not report.get("ok") or not self._capital_ready
        ):
            detail = self._capital_error or (report or {}).get("error") or "계좌 조회 실패"
            raise BrokerageError(
                f"실계좌 자산을 확인하지 못해 실거래를 시작하지 않습니다 ({detail})"
            )
        if self.live:
            mode = "LIVE — real money"
        elif self.paper_venue:
            mode = "PAPER VENUE — orders go to the broker's simulated account"
        else:
            mode = "DRY RUN — no orders will be sent"
        log.warning("%s connected in %s mode", self.name, mode)
