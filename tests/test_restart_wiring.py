"""재시작 배선 — 저장·복원 기계가 실제로 호출되는가.

state.py 에 저장·복원 메서드가 있어도 trader.py 가 부르지 않으면 아무 일도
일어나지 않습니다. 실제로 그랬습니다: 기계는 완성돼 있었고 테스트도 통과했지만
제품에서는 원래 실패가 그대로 재현됐습니다. 그래서 여기서는 메서드가 아니라
**LiveTrader 를 통해** 검증합니다.
"""
import inspect
from datetime import datetime

from quant.core.types import UTC
from quant.live import trader as trader_mod
from quant.live.state import StateStore


def _source(name: str) -> str:
    return inspect.getsource(getattr(trader_mod.LiveTrader, name))


def test_start_restores_the_budget_whether_or_not_it_resumed():
    """새 실행도 자기 첫 크래시를 넘겨야 하므로 resumed 안쪽이면 안 됩니다."""
    def indent(line: str) -> int:
        return len(line) - len(line.lstrip())

    lines = _source("start").split("\n")
    guard = next(x for x in lines if x.strip().startswith("if resumed:"))
    call = next(x for x in lines if "restore_budget" in x)
    assert indent(call) <= indent(guard), (
        "restore_budget 이 resumed 분기 안에 있습니다 — 새 실행은 자기 첫 "
        "크래시에서 한도를 잃습니다")


def test_start_restores_operator_pins():
    assert "restore_pins" in _source("start")


def test_pins_are_saved_on_the_equity_tick_and_on_shutdown():
    """둘 중 하나만 있으면, 크래시 또는 정상종료 중 한쪽에서 핀이 사라집니다."""
    assert "save_pins" in _source("_attach_observers")
    assert "save_pins" in _source("shutdown")


def test_the_halt_survives_a_restart(tmp_path):
    """한도에 걸려 멈춘 봇이 재시작으로 풀리면 한도가 아닙니다."""
    from quant.live.limits import TradingBudget

    db = str(tmp_path / "state.db")
    now = datetime(2026, 8, 23, 6, 0, tzinfo=UTC)

    first = StateStore(db)
    first.start_run("demo", "live", 10_000_000.0)
    budget = TradingBudget(max_daily_orders=1, timezone_offset_hours=9)
    first.restore_budget(budget, now)
    budget.roll(now)
    budget.today.orders = 1
    budget._halt("일일 주문 건수 한도 1건 도달")
    assert budget.halted
    first.close()

    # 같은 날 재시작
    second = StateStore(db)
    assert second.resume_run("demo", "live")
    fresh = TradingBudget(max_daily_orders=1, timezone_offset_hours=9)
    assert second.restore_budget(fresh, now), "오늘 원장을 찾지 못했습니다"
    assert fresh.halted, "재시작이 중단 상태를 지웠습니다"
    assert fresh.today.orders == 1
    second.close()


def test_a_genuinely_new_day_starts_clean(tmp_path):
    from datetime import timedelta

    from quant.live.limits import TradingBudget

    db = str(tmp_path / "state.db")
    now = datetime(2026, 8, 23, 6, 0, tzinfo=UTC)

    first = StateStore(db)
    first.start_run("demo", "live", 10_000_000.0)
    budget = TradingBudget(max_daily_orders=1, timezone_offset_hours=9)
    first.restore_budget(budget, now)
    budget.roll(now)
    budget.today.orders = 1
    budget._halt("한도 도달")
    first.close()

    second = StateStore(db)
    second.resume_run("demo", "live")
    fresh = TradingBudget(max_daily_orders=1, timezone_offset_hours=9)
    second.restore_budget(fresh, now + timedelta(days=1))
    assert not fresh.halted, "다음 거래일인데 중단이 유지됐습니다"
    second.close()
