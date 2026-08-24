"""Execution — diffing targets against reality and emitting orders.

The execution model's only job is *how* to get from the current book to the
target book. It never questions the target. Splitting it out means switching
from "cross the spread immediately" to "work the order over an hour" is a
one-line config change with no strategy edits.

It owns one thing more than that, and it is easy to miss: the orders it already
sent. Every target here is diffed against `projected_quantity` — the filled
position plus everything resting — which is only honest for as long as the
resting orders in that projection are real. An order that will never fill, or
one the venue already killed without telling us, keeps suppressing every future
target on its symbol; on KRX, where 지정가 die at 15:30 and no adapter reports
it, that suppression outlives the session and then the whole process.

So a model ages the orders it sent and asks for the stale ones back.
`OrderAgePolicy` is the per-side patience that decides when — entries can wait,
exits cannot, and an exit that still will not fill escalates instead of quietly
expiring. The one rule the whole mechanism rests on: a cancel and a fill can
race, so nothing new goes out for a symbol while our own cancel is in flight.
Sizing the replacement before the race is settled is exactly how one intended
position becomes two.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from enum import Enum

from quant.core.context import Context
from quant.core.types import (
    UTC,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PortfolioTarget,
    Symbol,
    TimeInForce,
)
from quant.data.calendar import KRX_REGULAR, KST

log = logging.getLogger("quant.execution")

#: 종가 단일가매매 호가접수시간. 이 구간에는 취소만 들어가고 정정은 받지 않으므로,
#: 취소 후 재주문(= 정정과 같은 것)은 장 마감까지 멈춰야 합니다.
KRX_CANCEL_ONLY_FROM = time(15, 20)

#: 취소를 몇 봉마다 다시 시끄럽게 알릴지. 어댑터가 취소를 못 하는 경우(KIS 국내
#: `_venue_cancel`)에는 매 봉 경고가 로그를 덮어버리므로 주기적으로만 올립니다.
_RENOTIFY_EVERY = 20


class AgeAction(str, Enum):
    """What to do about an order that has rested too long.

    freqtrade's `adjust_order_price` returns one of three things — keep, replace
    at a new price, or give up — and its emergency exit is the fourth. Naming
    them separately matters because each has a different downstream effect
    here: REPRICE lets the next diff repost, CANCEL stands the symbol down for
    a while, ESCALATE makes the next order cross the spread.
    """

    HOLD = "hold"
    REPRICE = "reprice"
    CANCEL = "cancel"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class OrderAgePolicy:
    """미체결 주문을 언제까지 기다릴지 — freqtrade 의 `unfilledtimeout`.

    진입과 청산에 같은 인내심을 주면 둘 중 하나는 반드시 틀립니다. 진입은
    안 되면 그만이지만, 청산은 반드시 나가야 하는 주문이라 더 빨리 포기하고
    더 세게 밀어붙여야 합니다.

    `max_reprices` 는 비용 상한입니다. 취소 후 재주문은 매번 스프레드를 한 번
    더 건너는 일이고, 얇은 코스닥 종목에서는 한 번에 17bp — 거래세 20bp 와
    맞먹습니다. 한도를 넘으면 쫓아가기를 멈추고 쫓아다닌 만큼 쉽니다.
    """

    #: 진입 주문을 몇 봉까지 기다릴지
    entry_bars: int = 4
    #: 청산 주문을 몇 봉까지 기다릴지 — 진입보다 짧아야 합니다
    exit_bars: int = 2
    #: 청산이 몇 번 연속 미체결되면 시장가로 넘길지
    exit_timeout_count: int = 2
    #: 진입 재주문 허용 횟수 (0 이면 한 번 미체결에 바로 포기)
    max_reprices: int = 2
    #: 0 이면 긴급 청산을 시장가로 냅니다. 시장가를 받지 않는 창구(KIS 해외주식)
    #: 에서는 bp 를 주면 그만큼 호가를 뚫는 지정가로 대신 냅니다.
    escalate_cross_bps: float = 0.0
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.entry_bars < 1 or self.exit_bars < 1:
            raise ValueError("order age timeouts are counted in bars and must be >= 1")
        if self.exit_timeout_count < 1:
            raise ValueError("exit_timeout_count must be >= 1")
        if self.max_reprices < 0 or self.escalate_cross_bps < 0:
            raise ValueError("max_reprices and escalate_cross_bps cannot be negative")

    def timeout(self, reducing: bool) -> int:
        return self.exit_bars if reducing else self.entry_bars

    @property
    def chase_cooldown(self) -> int:
        """쫓아다닌 만큼 쉽니다.

        Deriving it beats another knob: the only number that makes the cap mean
        something is one on the scale of the chase it is capping, and that is
        exactly `entry_bars` times the number of tries it allowed.
        """
        return self.entry_bars * (self.max_reprices + 1)

    @classmethod
    def coerce(cls, value) -> OrderAgePolicy:
        """Accept a policy, a YAML mapping, or a bare on/off switch."""
        if value is None:
            return cls()
        if isinstance(value, OrderAgePolicy):
            return value
        if isinstance(value, bool):
            return cls(enabled=value)
        if isinstance(value, dict):
            return cls(**value)
        raise TypeError(f"order_age must be a policy, a mapping or a bool, got {value!r}")


@dataclass(frozen=True)
class OrderReview:
    """One resting order's verdict for this bar, for an operator or a live loop."""

    order: Order
    action: AgeAction
    age_bars: int
    reason: str


#: 취소 요청이 이만큼 지나면 성공을 더 기다리지 않습니다. 4봉은 어느 거래소든
#: 정상 취소가 돌아오기에 넉넉하고, 그 뒤로도 안 돌아온다면 돌아오지 않습니다.
CANCEL_PATIENCE_BARS = 4


@dataclass
class _Resting:
    """Our view of one order we sent, kept alive until it leaves the book."""

    order: Order
    #: 보낼 때 포지션을 줄이는 주문이었는지. 주문의 의도는 도중에 바뀌지 않으므로
    #: 발주 시점에 굳혀 둡니다 — 부분체결로 포지션이 변해도 청산은 청산입니다.
    reducing: bool
    #: 엔진 시계로 본 발주 시각. `Order.created_at` 은 벽시계라서 백테스트의
    #: 시뮬레이션 시각과 섞이면 안 됩니다.
    placed_at: datetime
    age: int = 0
    filled: Decimal = Decimal("0")
    cancel_requested: bool = False
    #: 취소를 부탁한 뒤 몇 봉이 지났는가. 취소가 **성공한다는 보장이 없어서**
    #: 셉니다 — KIS 어댑터의 취소는 지금 무조건 실패하고, 그러면 이 기록이
    #: 영원히 남아 같은 종목의 주문을 계속 막습니다.
    cancel_age: int = 0
    asks: int = 0
    action: AgeAction = AgeAction.HOLD

    @property
    def cancel_stale(self) -> bool:
        """취소를 부탁한 지 너무 오래됐다 — 이제 없는 셈 칩니다.

        취소는 요청이지 명령이 아닙니다. 거래소가 거절할 수도, 어댑터가
        지원하지 않을 수도 있습니다(지금 KIS 가 그렇습니다). 성공을 무한정
        기다리면 그 종목의 신규 진입이 영구히 잠기므로, 몇 봉 뒤에는 포기하고
        평소처럼 계산합니다 — `projected_quantity` 가 브로커의 미체결을 그대로
        빼주므로 이중 계상은 그쪽에서 막힙니다.
        """
        return self.cancel_requested and self.cancel_age >= CANCEL_PATIENCE_BARS
    reason: str = ""


def _kst(moment: datetime) -> datetime:
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(KST)


class ExecutionModel(ABC):
    name = "execution"

    def __init__(self, min_order_notional: float = 1.0, order_age=None):
        self.min_order_notional = min_order_notional
        self.order_age = OrderAgePolicy.coerce(order_age)
        # The very `Order` objects the brokerage holds: `submit` mutates and
        # returns the object it was handed, and both brokerages keep that same
        # object, so reading `status` here reads the broker's own view rather
        # than a copy of it that can drift.
        self._resting: dict[str, _Resting] = {}
        self._entry_timeouts: dict[str, int] = {}
        self._exit_timeouts: dict[str, int] = {}
        self._entry_cooldown: dict[str, int] = {}
        self._cross_next: dict[str, Symbol] = {}
        self._stood_down: set[str] = set()
        self._reviewed_at: datetime | None = None
        self._last_review: list[OrderReview] = []

    @abstractmethod
    def execute(self, ctx: Context, targets: list[PortfolioTarget]) -> list[Order]: ...

    # -- order ageing ----------------------------------------------------
    @property
    def pending_cancellations(self) -> list[Order]:
        """Orders the policy wants off the book.

        A view, not a queue. An order stays in it until it actually leaves the
        book, so a caller that drops one — or a cancel the venue refused — is
        simply asked again next bar, and one that already went is gone from the
        list the moment it does. Draining it is not required for correctness:
        nothing new goes out for these symbols meanwhile.
        """
        return [r.order for r in self._resting.values()
                if r.cancel_requested and r.order.status.is_open]

    def review_orders(self, ctx: Context) -> list[OrderReview]:
        """Age every order we sent and rule on the stale ones.

        `_deltas` calls this, so every model gets it for free. A live loop that
        wants the cancellations at the top of its cycle — before it refreshes
        the projection — can call it there as well: the age is measured in bars,
        so two calls inside one bar age nothing twice, and a cycle that arrives
        early only ever buys the order one more bar of patience.
        """
        if self._reviewed_at is not None and ctx.now - self._reviewed_at < ctx.bar_delta:
            return self._last_review
        self._reviewed_at = ctx.now
        self._expire_cooldowns()
        self._forget_stale_escalations(ctx)
        if not self._amend_window_closed(ctx):
            self._stood_down.clear()

        reviews: list[OrderReview] = []
        for oid, rec in list(self._resting.items()):
            order = rec.order
            if order.status is OrderStatus.NEW:
                # Never accepted anywhere, so it cannot be resting at a venue
                # and cannot be double counted. Only orders that reached a book
                # can become zombies.
                self._resting.pop(oid)
                continue
            if not order.status.is_open:
                self._resolved(rec)
                self._resting.pop(oid)
                continue
            if rec.cancel_requested:
                rec.cancel_age += 1
            if order.filled_qty > rec.filled:
                # Progress at the venue is the opposite of a stalled order: the
                # patience counter starts again from this fill.
                rec.filled, rec.age = order.filled_qty, 0
                self._forget_timeout(order.symbol.key, rec.reducing)
            rec.age += 1

            if not rec.cancel_requested:
                action, reason = self._verdict(ctx, rec)
                if action is AgeAction.HOLD:
                    continue
                rec.cancel_requested = True
                rec.action, rec.reason = action, reason
                if action is AgeAction.ESCALATE:
                    self._cross_next[order.symbol.key] = order.symbol
                if self._amend_window_closed(ctx, order.symbol):
                    self._stood_down.add(order.symbol.key)
            rec.asks += 1
            self._announce(rec)
            reviews.append(OrderReview(order, rec.action, rec.age, rec.reason))

        self._last_review = reviews
        return reviews

    def _verdict(self, ctx: Context, rec: _Resting) -> tuple[AgeAction, str]:
        action, reason = self._timeout_verdict(ctx, rec)
        if action is AgeAction.REPRICE and self._amend_window_closed(ctx, rec.order.symbol):
            # 정정이 막힌 시간대에서 재주문은 취소로만 끝냅니다. 청산 긴급 전환은
            # 그대로 두되, 실제 주문은 단일가 시간이 끝난 뒤에 나갑니다.
            return AgeAction.CANCEL, f"{reason} (종가 단일가 — 재주문은 미룹니다)"
        return action, reason

    def _timeout_verdict(self, ctx: Context, rec: _Resting) -> tuple[AgeAction, str]:
        order = rec.order
        key = order.symbol.key
        if self._session_over(ctx, rec):
            # Not a policy choice — a fact. Nothing is resting at KRX after the
            # close, so the only thing left to fix is our own book.
            return AgeAction.CANCEL, "장 종료 — 거래소에 남아 있지 않은 주문입니다"
        if not self.order_age.enabled or rec.age < self.order_age.timeout(rec.reducing):
            return AgeAction.HOLD, ""

        if rec.reducing:
            count = self._exit_timeouts.get(key, 0) + 1
            self._exit_timeouts[key] = count
            if count >= self.order_age.exit_timeout_count:
                return (AgeAction.ESCALATE,
                        f"청산 주문 {count}회 연속 미체결 — 다음 봉에 시장가로 넘깁니다")
            return AgeAction.REPRICE, f"청산 주문 {rec.age}봉 미체결 — 다시 냅니다"

        count = self._entry_timeouts.get(key, 0) + 1
        self._entry_timeouts[key] = count
        if count > self.order_age.max_reprices:
            self._entry_cooldown[key] = self.order_age.chase_cooldown
            return (AgeAction.CANCEL,
                    f"진입 재주문 {self.order_age.max_reprices}회 한도 도달 — "
                    f"{self.order_age.chase_cooldown}봉 동안 신규 진입을 멈춥니다")
        return AgeAction.REPRICE, f"진입 주문 {rec.age}봉 미체결 — 다시 냅니다"

    def _resolved(self, rec: _Resting) -> None:
        """One of our orders left the book, by fill, cancel, expiry or rejection."""
        key = rec.order.symbol.key
        if rec.order.filled_qty > 0:
            self._forget_timeout(key, rec.reducing)
        if rec.cancel_requested and rec.order.filled_qty > rec.filled:
            # The cancel lost the race to a fill. Whatever we were about to do
            # instead is moot: the position moved, and the next diff sizes off
            # the fill against a projection the engine rebuilds from the
            # broker's open orders. Neither half can double count.
            self._cross_next.pop(key, None)
            log.info("%s 취소보다 체결이 먼저 들어왔습니다 — 체결 수량 %s 기준으로 "
                     "다시 계산합니다", rec.order.symbol.ticker, rec.order.filled_qty)

    def _announce(self, rec: _Resting) -> None:
        """Say it once, then only occasionally — a venue that cannot cancel
        would otherwise bury the log under one warning per bar."""
        if rec.asks != 1 and rec.asks % _RENOTIFY_EVERY != 0:
            return
        log.warning("%s %s %s 미체결 %d봉 — 취소를 요청합니다: %s",
                    rec.order.symbol.ticker, rec.order.side.value, rec.order.remaining,
                    rec.age, rec.reason)
        if rec.asks >= _RENOTIFY_EVERY:
            log.error("%s 취소 요청이 %d봉째 받아들여지지 않았습니다 — 증권사 앱/HTS 에서 "
                      "직접 취소하세요. 그때까지 이 종목은 신규도 청산도 나가지 않습니다",
                      rec.order.symbol.ticker, rec.asks)

    def _expire_cooldowns(self) -> None:
        for key in list(self._entry_cooldown):
            self._entry_cooldown[key] -= 1
            if self._entry_cooldown[key] <= 0:
                self._entry_cooldown.pop(key)
                # Served the cost cap; the symbol gets a fresh set of retries.
                self._entry_timeouts.pop(key, None)

    def _forget_stale_escalations(self, ctx: Context) -> None:
        for key, symbol in list(self._cross_next.items()):
            if ctx.portfolio.quantity(symbol) == 0:
                self._cross_next.pop(key)

    def _forget_timeout(self, key: str, reducing: bool) -> None:
        (self._exit_timeouts if reducing else self._entry_timeouts).pop(key, None)

    # -- KRX specifics ---------------------------------------------------
    @staticmethod
    def _is_krx(symbol: Symbol) -> bool:
        # 국내/해외 구분 기준은 KIS 어댑터와 같게 통화로 잡습니다.
        return symbol.quote_currency == "KRW"

    def _session_over(self, ctx: Context, rec: _Resting) -> bool:
        """KRX 주문은 하루짜리입니다 — 15:30 이후에는 거래소에 남아 있지 않습니다.

        KRX has no GTC: every order dies at the close whatever tif we asked
        for. No adapter reports that, so a local book left alone keeps the dead
        order in `projected_quantity` and silently disarms the symbol — for a
        resting sell on a market where the owner cannot short to hedge, that is
        an exit that quietly stopped existing. The date arithmetic needs no
        holiday table: an order cannot have been placed on a closed day.
        """
        if not self._is_krx(rec.order.symbol):
            return False
        placed = _kst(rec.placed_at)
        close = datetime.combine(placed.date(), KRX_REGULAR.close, tzinfo=KST)
        return _kst(ctx.now) >= close

    def _amend_window_closed(self, ctx: Context, symbol: Symbol | None = None) -> bool:
        """종가 단일가매매 시간대에는 정정이 안 됩니다 — 취소만 됩니다.

        취소 후 재주문은 정정과 같은 것이므로 이 구간에서는 함께 멈춥니다. 취소
        자체는 계속 나갑니다: 그쪽이 위험을 줄이는 방향입니다.
        """
        if symbol is not None and not self._is_krx(symbol):
            return False
        return KRX_CANCEL_ONLY_FROM <= _kst(ctx.now).time() < KRX_REGULAR.close

    # -- shared helpers --------------------------------------------------
    def _deltas(self, ctx: Context, targets: list[PortfolioTarget]
                ) -> list[tuple[PortfolioTarget, Decimal, float]]:
        """(target, signed delta quantity, price) for every target worth acting on."""
        self.review_orders(ctx)
        out = []
        for t in targets:
            price = ctx.price(t.symbol)
            if price <= 0:
                continue
            # Diff against *projected* holdings — filled position plus anything
            # already resting — or an unfilled order is re-sent every bar.
            current = ctx.projected_quantity(t.symbol)
            delta = t.symbol.round_qty(t.quantity - current)
            if delta == 0:
                continue

            # A minimum-notional floor is a cost heuristic for *entries*. Applying
            # it to an exit is how a stop-loss silently fails: the target says
            # flat, the order is never sent, and the position rides to zero. Any
            # target that reduces the position is therefore exempt.
            reducing = abs(t.quantity) < abs(current) or (
                current != 0 and (t.quantity > 0) != (current > 0)
            )
            if not reducing:
                notional = abs(float(delta)) * price * float(t.symbol.multiplier)
                floor = max(self.min_order_notional, float(t.symbol.min_notional))
                if notional < floor:
                    # Below the venue minimum the order would be rejected anyway;
                    # below our own floor it is not worth the fee.
                    continue
            if self._withheld(ctx, t.symbol, reducing):
                continue
            out.append((t, delta, price))
        return out

    def _withheld(self, ctx: Context, symbol: Symbol, reducing: bool) -> bool:
        """Whether anything new may go out for `symbol` this bar.

        The first clause is the whole race guard. Sizing a replacement while our
        cancel is in flight assumes the cancel won; if the fill won instead the
        position already moved and the replacement is a second bet on the same
        idea. One bar of patience costs nothing — the engine rebuilds
        `projected_quantity` from the broker's open orders in between, so the
        next diff is right whichever way the race went, and a partially filled
        order needs no resizing because the target was an absolute position all
        along.
        """
        key = symbol.key
        # 청산은 어떤 이유로도 막지 않습니다. 취소 요청이 걸려 있다는 이유로
        # 청산을 미루면, 취소가 실패하는 거래소에서는 그 종목이 영영 잠깁니다 —
        # 손절도, 리스크 청산도, 사용자의 수동 매도도 나가지 못합니다.
        if not reducing and any(
                r.cancel_requested and not r.cancel_stale
                and r.order.symbol.key == key
                for r in self._resting.values()):
            return True
        if key in self._stood_down and self._amend_window_closed(ctx, symbol):
            return True
        # An exit is never withheld by the chase cap. A cost control that traps
        # a position is a worse tool than no cost control — same reasoning as
        # the min-notional exemption above.
        return not reducing and self._entry_cooldown.get(key, 0) > 0

    def _order(self, ctx: Context, target: PortfolioTarget, delta: Decimal,
               order_type=OrderType.MARKET, limit_price: float | None = None,
               tif=TimeInForce.GTC, tag_suffix: str = "") -> Order:
        side = OrderSide.BUY if delta > 0 else OrderSide.SELL
        reducing = self._reduces(ctx, target.symbol, side)
        if reducing and target.symbol.key in self._cross_next:
            # An escalation changes *how* we trade, never how much: the size is
            # still whatever the model asked for. Only the patience is revoked.
            self._cross_next.pop(target.symbol.key)
            order_type, limit_price, tif = self._crossing(ctx, target.symbol, side)
            tag_suffix = f"{tag_suffix} | 미체결 청산 긴급 전환"
        order = Order(
            symbol=target.symbol,
            side=side,
            quantity=abs(delta),
            type=order_type,
            limit_price=(
                float(target.symbol.round_price(limit_price, side))
                if limit_price is not None else None
            ),
            tif=tif,
            tag=f"{target.tag}{tag_suffix}",
            source=self.name,
            meta={"target_qty": float(target.quantity), "model": target.source},
        )
        self._resting[order.id] = _Resting(order, reducing, ctx.now)
        return order

    def _crossing(self, ctx: Context, symbol: Symbol, side: OrderSide):
        """How to get out now. A market order unless the venue refuses them.

        KIS overseas orders are the case that forces this: the API has no market
        order type for foreign equities, so an escalation sent as MARKET is
        rejected and the position stays on. A limit priced through the touch is
        the same intent in an instrument every venue accepts. Give it enough bp
        to clear at least one tick — the price is snapped away from crossing, so
        a cross worth less than a tick rounds itself back inside the book.
        """
        bps = self.order_age.escalate_cross_bps
        if bps <= 0:
            return OrderType.MARKET, None, TimeInForce.GTC
        quote = ctx.quote(symbol)
        if side is OrderSide.BUY:
            ref = quote.ask if quote else ctx.price(symbol)
            return OrderType.LIMIT, ref * (1 + bps / 10_000.0), TimeInForce.DAY
        ref = quote.bid if quote else ctx.price(symbol)
        return OrderType.LIMIT, ref * (1 - bps / 10_000.0), TimeInForce.DAY

    @staticmethod
    def _reduces(ctx: Context, symbol: Symbol, side: OrderSide) -> bool:
        """Same test the brokerage uses: does this order move the book toward flat."""
        held = ctx.portfolio.quantity(symbol)
        if held == 0:
            return False
        return (held > 0) == (side is OrderSide.SELL)

    def _resting_on(self, key: str) -> bool:
        """Whether one of our own orders is still on the book for `Symbol.key`."""
        return any(r.order.symbol.key == key for r in self._resting.values())
