"""한도가 실제로 무는지.

두 결함이 겹쳐서 하루 한도 전체가 장식이었습니다. 하나는 신규 진입의 가격이
0원으로 잡혀 금액 한도가 늘 통과했고, 다른 하나는 원장이 시뮬레이션 시각 대신
벽시계로 날짜를 세어 매 봉 초기화됐습니다. 두 경로 모두 여기서 막습니다.
"""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

from quant.brokerage.paper import PaperBrokerage
from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.types import UTC, Order, OrderSide, OrderType, RunMode, Symbol
from quant.live.limits import TradingBudget

SYM = Symbol("005930", venue="kis", quote_currency="KRW")


def _order(qty: float) -> Order:
    return Order(symbol=SYM, side=OrderSide.BUY, quantity=Decimal(str(qty)),
                 type=OrderType.MARKET)


def test_disabled_caps_still_record_order_usage():
    """Cap enforcement can be off; the account-day ledger cannot be off."""
    portfolio = Portfolio(10_000_000.0, "KRW")
    portfolio.mark(SYM, 70_000.0)
    budget = TradingBudget()
    broker = PaperBrokerage(portfolio, run_mode=RunMode.LIVE)
    broker.budget = budget

    submitted = asyncio.run(broker.submit(_order(1)))

    assert submitted.status.value == "submitted"
    assert not budget.configured
    assert budget.today is not None
    assert budget.today.orders == 1
    assert budget.today.notional == 70_000.0


# ── 가격 0원 문제 ────────────────────────────────────────────────────────
def test_marking_a_symbol_we_do_not_hold_records_its_price():
    """평가에는 안 잡히지만 한도 계산에는 잡혀야 합니다."""
    pf = Portfolio(10_000_000.0, "KRW")
    pf.mark(SYM, 70_000.0)
    assert pf.position(SYM).last_price == 70_000.0
    # 보유가 아니므로 자산 평가는 그대로여야 합니다.
    assert pf.open_positions == []
    assert pf.holdings_value == 0.0
    assert pf.equity == 10_000_000.0


def test_a_new_entry_is_measured_against_the_notional_cap():
    """예전에는 미보유 종목 시장가 주문이 0원으로 계산돼 전부 통과했습니다."""
    pf = Portfolio(10_000_000.0, "KRW")
    pf.mark(SYM, 70_000.0)
    budget = TradingBudget(max_daily_notional=1_000_000)

    price = pf.position(SYM).last_price
    ok, why = budget.check(_order(1_000), price, is_reducing=False)
    assert not ok
    assert "한도" in why


def test_holding_the_symbol_is_not_what_decides_whether_the_cap_applies():
    """보유 여부로 한도가 갈리면, 신규 진입만 무제한이 됩니다."""
    budget = TradingBudget(max_daily_notional=1_000_000)
    held, flat = Portfolio(10_000_000.0, "KRW"), Portfolio(10_000_000.0, "KRW")
    for pf in (held, flat):
        pf.mark(SYM, 70_000.0)
    held.position(SYM).quantity = 1.0

    a, _ = budget.check(_order(1_000), held.position(SYM).last_price, False)
    b, _ = budget.check(_order(1_000), flat.position(SYM).last_price, False)
    assert a == b is False


# ── 시계 문제 ────────────────────────────────────────────────────────────
def test_the_ledger_follows_the_engine_clock_not_the_machine():
    """봉을 넘겨도 같은 날이면 같은 원장이어야 합니다."""
    day = datetime(2025, 10, 1, 1, 0, tzinfo=UTC)
    clock = SimClock(day)
    budget = TradingBudget(max_daily_orders=1, timezone_offset_hours=0,
                           clock=clock)

    # 첫 주문은 통과하고 기록됩니다.
    ok, _ = budget.check(_order(1), 70_000.0, False)
    assert ok
    budget.record_order(_order(1), 70_000.0)

    # 같은 날 다음 봉 — 시각만 바뀌었을 뿐 한도는 유지돼야 합니다.
    for hour in range(2, 9):
        clock.set(day.replace(hour=hour))
        ok, why = budget.check(_order(1), 70_000.0, False)
        assert not ok, f"{hour}시에 한도가 풀렸습니다"

    # 날이 바뀌면 그때 초기화됩니다.
    clock.set(day + timedelta(days=1))
    ok, _ = budget.check(_order(1), 70_000.0, False)
    assert ok


def test_without_a_clock_the_budget_still_works_off_the_wall_clock():
    """시계를 안 붙인 호출부가 깨지지는 않아야 합니다."""
    budget = TradingBudget(max_daily_orders=1)
    assert budget.check(_order(1), 70_000.0, False)[0]


def test_an_exit_is_never_blocked_by_a_spent_budget():
    """손실 포지션에 가두는 안전장치는 안전장치가 아닙니다."""
    clock = SimClock(datetime(2025, 10, 1, tzinfo=UTC))
    budget = TradingBudget(max_daily_notional=1, max_daily_orders=1, clock=clock)
    budget.record_order(_order(1_000), 70_000.0)
    ok, why = budget.check(_order(1_000), 70_000.0, is_reducing=True)
    assert ok, why
