"""판정을 못 한 리스크 모델은 "줄일 이유 없음" 이 아니다.

`CompositeRiskModel` 은 모델이 던진 예외를 잡아 로그만 남기고 **타깃을 그대로
들고** 다음 모델로 넘어갔습니다. 여기가 브로커 직전 마지막 층이므로, 그 모델이
잘라 내려던 만큼이 통째로 빠진 채 알파가 요청한 원래 크기가 주문으로 나갑니다.

여기서 도는 것들입니다 — 종목별 손절, 트레일링 스톱, 포트폴리오 킬스위치,
포지션 수 한도, 잠금 게이트. 반환값은 평범한 타깃 리스트라 아래쪽 어디에서도
"판정을 못 했다" 와 "판정했고 줄일 게 없었다" 를 구분할 수 없습니다.

`ProtectionManager` 와 같은 모양인데 이쪽이 주문 경로 위입니다.

**알 수 없는 것을 아는 척하지 않는 쪽으로** 갑니다: 줄이는 주문은 전부 통과,
늘리는 것은 하나도 안 보냅니다. 한 봉 늦은 진입은 싸고, 검증 안 된 증액은
그 모델이 거절했을 포지션만큼 비쌉니다.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, PortfolioTarget, Symbol
from quant.risk.base import CompositeRiskModel, RiskManagementModel

SYM = Symbol("AAA", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))
T0 = datetime(2024, 1, 1, tzinfo=UTC)


class Broken(RiskManagementModel):
    name = "broken"

    def manage(self, ctx, targets):
        raise RuntimeError("history feed returned nothing")


class Quiet(RiskManagementModel):
    """멀쩡히 돌지만 줄일 게 없는 모델 — 옆에서 같이 죽으면 안 된다."""

    name = "quiet"

    def __init__(self):
        self.calls = 0

    def manage(self, ctx, targets):
        self.calls += 1
        return targets


def _ctx(held: int = 0) -> Context:
    ctx = Context(SimClock(T0), Portfolio(100_000.0), EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    if held:
        pos = ctx.portfolio.position(SYM)
        pos.quantity, pos.avg_price = Decimal(held), 100.0
    return ctx


def _manage(ctx, models, target_qty):
    stack = CompositeRiskModel(*models)
    out = stack.manage(ctx, [PortfolioTarget(SYM, Decimal(target_qty), tag="alpha")])
    return stack, out[0]


def test_a_new_entry_is_held_back():
    ctx = _ctx(held=0)
    stack, t = _manage(ctx, [Broken()], 100)

    assert t.quantity == 0
    assert "진입 보류" in t.tag
    assert "history feed returned nothing" in t.tag
    assert stack.failed and "broken" in stack.failed[0]


def test_an_increase_is_capped_at_what_is_already_held():
    ctx = _ctx(held=40)
    _, t = _manage(ctx, [Broken()], 100)

    assert t.quantity == Decimal("40")
    assert "증액 보류" in t.tag


def test_a_reduction_still_goes_out():
    """줄이는 주문을 막으면 그건 안전장치가 아니라 덫이다."""
    ctx = _ctx(held=100)
    _, t = _manage(ctx, [Broken()], 40)

    assert t.quantity == Decimal("40")
    assert "보류" not in t.tag


def test_a_full_exit_still_goes_out():
    ctx = _ctx(held=100)
    _, t = _manage(ctx, [Broken()], 0)

    assert t.quantity == 0
    assert "보류" not in t.tag


def test_a_flip_is_cut_back_to_the_close():
    """전환은 청산 + 신규다. 검증이 안 됐으면 청산 쪽만 남는다."""
    ctx = _ctx(held=100)
    _, t = _manage(ctx, [Broken()], -100)

    assert t.quantity == 0
    assert "전환 보류" in t.tag


def test_the_working_models_still_run():
    ctx = _ctx(held=0)
    quiet = Quiet()
    CompositeRiskModel(Broken(), quiet).manage(
        ctx, [PortfolioTarget(SYM, Decimal("100"), tag="alpha")])

    assert quiet.calls == 1


def test_nothing_changes_when_every_model_is_healthy():
    ctx = _ctx(held=0)
    stack, t = _manage(ctx, [Quiet()], 100)

    assert t.quantity == Decimal("100")
    assert stack.failed == []
