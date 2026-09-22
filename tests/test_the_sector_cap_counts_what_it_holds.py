"""섹터 한도는 이번 봉에 빠진 보유도 세야 한다.

`SectorExposureCap` 은 **이번 봉 타깃만** 더해서 그룹 노출을 계산했습니다.
리밸런스 데드밴드는 비중이 거의 안 움직인 종목을 타깃에서 빼 버립니다. 그
종목들의 노출은 실재하는데 한도가 못 봅니다.

바로 위 `MaxPositionCount` 는 같은 함정을 주석으로 적어 놓고 `silent =
held - keyed` 로 막아 뒀습니다 — "오래된 이름들이 조용히 있는 한 장부는 한도
없이 커진다." 섹터 한도에는 그 코드가 없었습니다.

빠진 보유는 여기서 줄일 수 없으므로(이번 봉 목표가 없습니다) 한도는 배치 쪽을
더 조여서 지킵니다. 안 보고 통과시키는 것보다 낫고, 들고 있던 종목을 강제로
팔지도 않습니다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, Bar, PortfolioTarget, Symbol
from quant.risk.models import SectorExposureCap

TECH_A = Symbol("AAA", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))
TECH_B = Symbol("BBB", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))
GROUPS = {"AAA": "tech", "BBB": "tech"}
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def _ctx(*held: tuple[Symbol, int]) -> Context:
    """현금 100,000. 가격 100 이므로 100주 = 평가 10,000."""
    ctx = Context(SimClock(T0), Portfolio(100_000.0), EventBus(), timeframe="1d")
    ctx.universe = [TECH_A, TECH_B]
    for sym in (TECH_A, TECH_B):
        ctx.push_bar(Bar(sym, T0, 100.0, 100.0, 100.0, 100.0, 1_000))
    # 봉은 닫혀야 보입니다 — `history` 가 `end_ts <= now` 로 거릅니다. 시계를
    # 안 넘기면 `ctx.price` 가 0 을 돌려주고, 그러면 노출이 전부 0 이라 이
    # 파일의 모든 테스트가 **아무것도 증명하지 않은 채 통과합니다.**
    ctx.clock.set(T0 + timedelta(days=1))
    for sym, qty in held:
        pos = ctx.portfolio.position(sym)
        pos.quantity, pos.avg_price = Decimal(qty), 100.0
        pos.mark(100.0)
    assert ctx.price(TECH_A) == 100.0 and ctx.price(TECH_B) == 100.0
    return ctx


def test_a_quiet_holding_counts_toward_the_group():
    # BBB 300주(평가 30,000)를 들고 있고 이번 봉 타깃에는 없다.
    # 자산 = 현금 100,000 + 보유 30,000 = 130,000, 한도 40% = 52,000.
    # AAA 로 300주(30,000)를 더 사려 하면 그룹 합계 60,000 → 46% > 40%.
    ctx = _ctx((TECH_B, 300))
    assert ctx.equity == 130_000.0
    cap = SectorExposureCap(GROUPS, max_group_weight=0.40)

    out = cap.manage(ctx, [PortfolioTarget(TECH_A, Decimal("300"), tag="alpha")])

    assert out[0].quantity < Decimal("300"), "조용한 보유를 못 봤다"
    assert "sector cap" in out[0].tag


def test_the_same_book_passes_when_the_quiet_holding_is_gone():
    """대조군: 조용한 보유만 없으면 같은 타깃이 그대로 나가야 한다."""
    ctx = _ctx()
    cap = SectorExposureCap(GROUPS, max_group_weight=0.40)

    out = cap.manage(ctx, [PortfolioTarget(TECH_A, Decimal("300"), tag="alpha")])

    assert out[0].quantity == Decimal("300")


def test_the_cap_is_silent_when_the_group_fits():
    ctx = _ctx((TECH_B, 100))                      # 10%
    cap = SectorExposureCap(GROUPS, max_group_weight=0.40)

    out = cap.manage(ctx, [PortfolioTarget(TECH_A, Decimal("100"), tag="alpha")])

    assert out[0].quantity == Decimal("100")


def test_a_holding_that_is_in_the_batch_is_not_counted_twice():
    """타깃에 있는 종목은 타깃 값으로만 센다 — 보유분을 또 더하면 안 된다."""
    ctx = _ctx((TECH_A, 350))
    cap = SectorExposureCap(GROUPS, max_group_weight=0.40)

    # 350주를 들고 있고 목표는 350주(=35%). 한도 40% 안이므로 손대면 안 된다.
    out = cap.manage(ctx, [PortfolioTarget(TECH_A, Decimal("350"), tag="alpha")])

    assert out[0].quantity == Decimal("350")


def test_an_ungrouped_holding_is_ignored():
    other = Symbol("ZZZ", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))
    ctx = _ctx()
    ctx.universe.append(other)
    ctx.push_bar(Bar(other, T0, 100.0, 100.0, 100.0, 100.0, 1_000))
    pos = ctx.portfolio.position(other)
    pos.quantity, pos.avg_price = Decimal("900"), 100.0
    pos.mark(100.0)

    cap = SectorExposureCap(GROUPS, max_group_weight=0.40)
    out = cap.manage(ctx, [PortfolioTarget(TECH_A, Decimal("100"), tag="alpha")])

    assert out[0].quantity == Decimal("100")
