"""망가진 보호장치는 "괜찮다" 가 아니다.

`ProtectionManager` 는 보호장치가 던진 예외를 잡아 로그만 남기고 넘어갔습니다.
호출자가 받는 결과는 **"이 보호장치가 보고 나서 잠글 이유가 없다고 했다"** 와
글자 하나 다르지 않았습니다.

이 저장소는 같은 모양으로 이미 한 번 데였습니다 (`accf346`): LLM 이 사흘 내내
죽어 있었고, 사이클마다 로그가 남았고, 폴백도 돌았고, **사흘 동안 아무도 몰랐습
니다.** 로그가 없어서가 아니라 로그만 있어서였습니다.

그래서 실패는 발동과 같은 채널로 나갑니다 — 이벤트 버스를 타고 운영자 알림까지.
그리고 연속으로 계속 실패하면 그건 일시적 결함이 아니라 **안전장치가 없는
상태** 이므로 신규 진입을 멈춥니다. 첫 실패에 멈추지는 않습니다. 한 번은 데이터
결함일 가능성이 더 큽니다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, Symbol
from quant.risk.protections import Protection, ProtectionManager

SYM = Symbol("AAA", venue="SIM", tick_size=Decimal("0.01"), lot_size=Decimal("1"))
T0 = datetime(2024, 1, 1, tzinfo=UTC)


class Broken(Protection):
    """정해진 봉에서만 터지는 보호장치."""

    name = "broken"
    per_symbol = False

    def __init__(self, fails: int = 99, stop_bars: int = 10):
        super().__init__(lookback_bars=60, stop_bars=stop_bars)
        self.calls = 0
        self.fails = fails

    def check(self, ctx, symbol):
        self.calls += 1
        if self.calls <= self.fails:
            raise RuntimeError("quote feed returned nothing")
        return False, ""


class Working(Protection):
    """옆에서 멀쩡히 도는 보호장치 — 같이 죽으면 안 된다."""

    name = "working"
    per_symbol = False

    def __init__(self):
        super().__init__(lookback_bars=60, stop_bars=10)
        self.calls = 0

    def check(self, ctx, symbol):
        self.calls += 1
        return False, ""


def _ctx() -> Context:
    ctx = Context(SimClock(T0), Portfolio(100_000.0), EventBus(), timeframe="1d")
    ctx.universe = [SYM]
    return ctx


def _bar(mgr: ProtectionManager, ctx: Context, n: int) -> list[dict]:
    ctx.clock.set(T0 + timedelta(days=n))
    return mgr.apply(ctx)


def test_a_failure_reaches_the_caller():
    """로그가 아니라 반환값에 담겨야 운영자 알림까지 간다."""
    ctx = _ctx()
    events = _bar(ProtectionManager(Broken()), ctx, 1)

    assert len(events) == 1
    assert events[0]["failed"] is True
    assert events[0]["protection"] == "broken"
    # 알림이 그리는 세 칸 — 빠지면 화면에 None 이 뜬다.
    assert events[0]["symbol"] and events[0]["reason"]
    assert "quote feed returned nothing" in events[0]["reason"]


def test_one_failure_does_not_halt_the_book():
    ctx = _ctx()
    _bar(ProtectionManager(Broken(fails=1)), ctx, 1)

    assert not ctx.is_locked(SYM)[0]


def test_a_protection_that_keeps_failing_stops_new_entries():
    ctx = _ctx()
    mgr = ProtectionManager(Broken(), broken_after_bars=3)

    for n in (1, 2):
        _bar(mgr, ctx, n)
        assert not ctx.is_locked(SYM)[0], f"{n}봉째에 이미 멈췄다"

    events = _bar(mgr, ctx, 3)
    locked, why = ctx.is_locked(SYM)
    assert locked
    assert "broken" in why
    assert events[0]["bars"] == 3


def test_a_crash_does_not_take_the_working_ones_down():
    ctx = _ctx()
    good = Working()
    ProtectionManager(Broken(), good).apply(ctx)

    assert good.calls == 1


def test_recovery_clears_the_streak():
    """회복하면 다시 3봉을 채워야 멈춘다 — 누적이 남으면 안 된다."""
    ctx = _ctx()
    broken = Broken(fails=2)
    mgr = ProtectionManager(broken, broken_after_bars=3)

    for n in (1, 2, 3):
        _bar(mgr, ctx, n)
    assert not ctx.is_locked(SYM)[0]

    broken.fails, broken.calls = 99, 0  # 다시 고장
    for n in (4, 5):
        _bar(mgr, ctx, n)
    assert not ctx.is_locked(SYM)[0]

    _bar(mgr, ctx, 6)
    assert ctx.is_locked(SYM)[0]
