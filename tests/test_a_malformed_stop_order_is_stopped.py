"""프로세스를 떠나기 전 마지막 문이 스톱 주문을 그냥 통과시켰습니다.

`Brokerage.validate` 는 "raise before anything leaves the process if an order is
malformed" 라고 적혀 있고, 실주문 경로의 `_guard` 가 증권사 호출 직전에
부릅니다. 그런데 가격 검사를 **문자열 접두사** 로 했습니다:

    if order.type.value.startswith("limit") and order.limit_price is None:

`OrderType.STOP_LIMIT.value` 는 `"stop_limit"` 이라 "limit" 으로 시작하지
않습니다. **지정가 없는 스톱리밋이 그대로 나갔습니다.** 스톱 주문의 발동가는
아예 검사 대상이 아니었습니다.

시뮬레이터는 빠진 값을 메워 줍니다 — 발동가가 없으면 봉 시가, 지정가가 없으면
발동가. 그래서 백테스트에서는 아무 일도 안 일어납니다. 거래소는 메워 주지
않습니다.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from quant.brokerage.base import Brokerage, BrokerageError
from quant.core.types import Order, OrderSide, OrderType, Symbol

SYM = Symbol("AAA", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))


class _Validator(Brokerage):
    """추상 훅만 채운 껍데기 — 검사만 부른다."""

    async def submit(self, order): ...
    async def cancel(self, order): ...
    async def open_orders(self): ...


def _order(order_type, *, limit=None, stop=None, qty="10") -> Order:
    return Order(SYM, OrderSide.BUY, Decimal(qty), order_type,
                 limit_price=limit, stop_price=stop)


def test_a_stop_limit_without_a_limit_price_is_refused():
    with pytest.raises(BrokerageError, match="limit price"):
        _Validator().validate(_order(OrderType.STOP_LIMIT, stop=99.0))


def test_a_stop_without_a_trigger_price_is_refused():
    with pytest.raises(BrokerageError, match="stop price"):
        _Validator().validate(_order(OrderType.STOP))


def test_a_stop_limit_without_either_price_is_refused():
    with pytest.raises(BrokerageError):
        _Validator().validate(_order(OrderType.STOP_LIMIT))


def test_a_complete_stop_limit_passes():
    _Validator().validate(_order(OrderType.STOP_LIMIT, limit=99.0, stop=100.0))


def test_the_existing_checks_still_hold():
    v = _Validator()
    with pytest.raises(BrokerageError, match="limit price"):
        v.validate(_order(OrderType.LIMIT))
    with pytest.raises(BrokerageError, match="non-positive"):
        v.validate(_order(OrderType.MARKET, qty="0"))
    v.validate(_order(OrderType.MARKET))       # 시장가는 가격이 필요 없다
