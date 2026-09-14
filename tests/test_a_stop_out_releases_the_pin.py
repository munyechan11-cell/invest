"""보유가 사라지면 고정(pin)도 사라집니다.

핀의 뜻은 하나입니다 — "전략이 내가 산 것을 되팔지 못하게 하라"
(`ManualControl._build_one`). 되팔 물량이 없으면 그 뜻이 남을 자리가 없습니다.

그런데 지금까지 핀은 **사람이 직접 청산한 경우에만** 풀렸습니다
(`manual._exit_order`). 손절·트레일링·킬스위치가 대신 팔고 나가면 핀은 그대로
남고, 포트폴리오 모델은 `if ctx.is_pinned(symbol): continue` 라 그 종목을
**영원히 건너뜁니다.** 유니버스가 서너 종목인 출하 설정에서는 전략의 3분의
1이 조용히 사라지는 것이고, 핀은 SQLite 에 저장되므로 재시작해도 돌아오지
않습니다. 화면에는 보유도 없는 종목에 핀 하나가 붙어 있을 뿐입니다.
"""
import asyncio
from datetime import datetime
from decimal import Decimal

from quant.alpha.base import AlphaModel
from quant.brokerage.paper import PaperBrokerage
from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.engine import Engine
from quant.core.events import EventBus
from quant.core.types import UTC, Fill, OrderSide, Symbol
from quant.execution.costs import NoSlippage, PercentFeeModel
from quant.execution.models import ImmediateExecution
from quant.portfolio.models import EqualWeighting

T0 = datetime(2024, 6, 3, tzinfo=UTC)
SYM = Symbol("005930", venue="toss", quote_currency="KRW",
             tick_size=Decimal("100"), lot_size=Decimal("1"), tick_ladder="krx")
OTHER = Symbol("000660", venue="toss", quote_currency="KRW",
               tick_size=Decimal("500"), lot_size=Decimal("1"))


def engine_with(cash: float = 10_000_000.0) -> Engine:
    """진짜 엔진입니다 — `_book_fills` 가 실제로 지나는 길을 그대로 씁니다."""
    pf = Portfolio(cash)
    ctx = Context(SimClock(T0), pf, EventBus(), timeframe="1d")
    ctx.universe = [SYM, OTHER]
    broker = PaperBrokerage(pf, fee_model=PercentFeeModel(taker_bps=0),
                            slippage_model=NoSlippage())
    return Engine(ctx, _Quiet(),
                  EqualWeighting(cash_reserve_pct=0.0, max_position_weight=1.0),
                  ImmediateExecution(min_order_notional=1), broker)


class _Quiet(AlphaModel):
    name = "quiet"

    async def update(self, ctx, bars):
        return []


def fill(symbol: Symbol, side: OrderSide, qty: str, price: float) -> Fill:
    return Fill(order_id="o", symbol=symbol, side=side,
                quantity=Decimal(qty), price=price, fee=0.0, ts=T0)


def book(engine: Engine, *fills: Fill) -> None:
    asyncio.run(engine._book_fills(list(fills)))


# ── 핵심 ─────────────────────────────────────────────────────────────────
def test_a_stop_out_hands_the_symbol_back_to_the_strategy():
    engine = engine_with()
    engine.ctx.pin(SYM, "수동 매수")
    book(engine, fill(SYM, OrderSide.BUY, "10", 70_000))
    assert engine.ctx.is_pinned(SYM), "체결 전후로 핀은 유지돼야 합니다"

    book(engine, fill(SYM, OrderSide.SELL, "10", 63_000))       # 손절
    assert engine.ctx.portfolio.quantity(SYM) == 0
    assert not engine.ctx.is_pinned(SYM), (
        "보유가 0인데 핀이 남으면 포트폴리오 모델이 이 종목을 영원히 건너뜁니다")


def test_a_partial_exit_keeps_the_pin():
    """절반만 팔린 것은 여전히 '내가 산 물량' 입니다."""
    engine = engine_with()
    engine.ctx.pin(SYM, "수동 매수")
    book(engine, fill(SYM, OrderSide.BUY, "10", 70_000),
         fill(SYM, OrderSide.SELL, "4", 71_000))
    assert engine.ctx.portfolio.quantity(SYM) == 6
    assert engine.ctx.is_pinned(SYM)


def test_an_unfilled_manual_buy_keeps_its_pin():
    """핀은 **발주** 때 찍히고 체결은 나중입니다. 그 사이 보유는 0 이지만,
    그때 풀어 버리면 다음 봉에 전략이 같은 종목을 건드립니다."""
    engine = engine_with()
    engine.ctx.pin(SYM, "수동 매수")
    book(engine, fill(OTHER, OrderSide.BUY, "1", 200_000))   # 남의 종목 체결
    assert engine.ctx.is_pinned(SYM)


def test_a_flip_through_zero_keeps_the_pin():
    """0 을 지나 반대로 넘어간 것은 '보유가 사라진' 것이 아닙니다."""
    engine = engine_with()
    engine.ctx.pin(SYM, "수동 매수")
    book(engine, fill(SYM, OrderSide.BUY, "10", 70_000),
         fill(SYM, OrderSide.SELL, "15", 69_000))
    assert engine.ctx.portfolio.quantity(SYM) == -5
    assert engine.ctx.is_pinned(SYM)


def test_an_unpinned_symbol_is_untouched():
    engine = engine_with()
    book(engine, fill(SYM, OrderSide.BUY, "10", 70_000),
         fill(SYM, OrderSide.SELL, "10", 71_000))
    assert engine.ctx.pinned == {}


def test_only_the_symbol_that_closed_is_released():
    engine = engine_with()
    engine.ctx.pin(SYM, "수동 매수")
    engine.ctx.pin(OTHER, "수동 매수")
    book(engine, fill(SYM, OrderSide.BUY, "10", 70_000),
         fill(OTHER, OrderSide.BUY, "1", 200_000),
         fill(SYM, OrderSide.SELL, "10", 63_000))
    assert not engine.ctx.is_pinned(SYM)
    assert engine.ctx.is_pinned(OTHER), "남의 핀까지 쓸어버리면 안 됩니다"
