"""드로다운 킬스위치는 재배포로 풀리면 안 된다.

`save_locks` 의 주석은 이렇게 말합니다 — "엔진이 가진 모든 보호장치(손절 가드,
쿨다운, 저수익, **드로다운 정지**)가 메모리에만 살아 있어서, 재시작하면 방금
잠근 그 종목을 하필 최악의 시점에 다시 산다."

절반만 맞았습니다. 정지의 **잠금** 은 저장됩니다. 그런데 그 정지가 **몇 번째**
인지는 저장되지 않았습니다. 그리고 그 횟수가 킬스위치 본체입니다 — 세 번이면
운이 나쁜 게 아니라 전략이 고장난 것이고, 운영자가 볼 때까지 멈춥니다.

재배포 한 번에 목숨 세 개가 새로 생겼습니다. "영구 정지" 도 같이 지워졌습니다.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, PortfolioTarget, Symbol
from quant.live.state import StateStore
from quant.risk.base import CompositeRiskModel
from quant.risk.models import MaximumDrawdownPortfolio, TradingLockGate

SYM = Symbol("AAA", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))
T0 = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture
def db_path():
    return os.path.join(tempfile.mkdtemp(), "state.db")


def opened(db) -> StateStore:
    st = StateStore(db)
    if st.resume_run("s", "live") is None:
        st.start_run("s", "live", 10_000_000.0)
    return st


def _ctx() -> Context:
    ctx = Context(SimClock(T0), Portfolio(100_000.0), EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    return ctx


def _trip(model: MaximumDrawdownPortfolio, ctx: Context, times: int) -> None:
    """한도를 `times` 번 건드린다. 사이사이 정지를 풀어 다시 걸리게 한다."""
    for n in range(times):
        ctx.clock.set(T0 + timedelta(days=n * 10))
        ctx.portfolio.high_water_mark = 200_000.0
        model.manage(ctx, [PortfolioTarget(SYM, Decimal("10"))])
        ctx.clock.set(T0 + timedelta(days=n * 10 + 5))
        ctx.portfolio.high_water_mark = ctx.portfolio.equity
        model.manage(ctx, [])


def test_the_trip_count_survives(db_path):
    """두 번 쓴 목숨이 재시작으로 돌아오면 안 된다."""
    ctx = _ctx()
    model = MaximumDrawdownPortfolio(max_drawdown_pct=0.10, halt_bars=2, max_trips=3)
    _trip(model, ctx, 2)
    assert model.trips == 2 and not model.halted_permanently

    st = opened(db_path)
    st.save_risk_state(CompositeRiskModel(model).durable_state())
    st.close()                                       # crash / redeploy / OOM

    fresh = MaximumDrawdownPortfolio(max_drawdown_pct=0.10, halt_bars=2, max_trips=3)
    st2 = opened(db_path)
    assert CompositeRiskModel(fresh).load_durable_state(st2.restore_risk_state()) == 1

    assert fresh.trips == 2
    # 남은 목숨은 하나. 한 번 더면 영구 정지여야 한다.
    _trip(fresh, _ctx(), 1)
    assert fresh.halted_permanently


def test_a_permanent_halt_is_still_permanent(db_path):
    ctx = _ctx()
    model = MaximumDrawdownPortfolio(max_drawdown_pct=0.10, halt_bars=2, max_trips=2)
    _trip(model, ctx, 2)
    assert model.halted_permanently

    st = opened(db_path)
    st.save_risk_state(CompositeRiskModel(model).durable_state())
    st.close()

    fresh = MaximumDrawdownPortfolio(max_drawdown_pct=0.10, halt_bars=2, max_trips=2)
    st2 = opened(db_path)
    CompositeRiskModel(fresh).load_durable_state(st2.restore_risk_state())

    assert fresh.halted_permanently
    # 그리고 실제로 아무것도 못 연다.
    out = fresh.manage(_ctx(), [PortfolioTarget(SYM, Decimal("10"))])
    assert out[0].quantity == 0
    assert "halted" in out[0].tag


def test_a_clean_run_stores_nothing(db_path):
    """한 번도 안 걸린 모델까지 저장할 이유는 없다."""
    model = MaximumDrawdownPortfolio()
    assert CompositeRiskModel(model, TradingLockGate()).durable_state() == {}

    st = opened(db_path)
    st.save_risk_state({})
    assert st.restore_risk_state() == {}


def test_unreadable_rows_are_skipped_not_fatal(db_path):
    st = opened(db_path)
    st.save_risk_state({"max_dd_portfolio": {"trips": 2}})
    st.conn.execute("UPDATE risk_state SET payload='{not json'")
    st.conn.commit()

    assert st.restore_risk_state() == {}
