"""시작 버튼이 계량되지 않는 LLM 지출 통로가 되지 않는가.

봇을 켜면 봉을 기다리지 않고 그 자리에서 심의를 한 번 합니다 — 일봉 전략에서
첫 발언까지 하루를 기다리게 하지 않으려고 넣은 것입니다. 그런데 그 심의가
어디에도 안 잡히면, 껐다 켜기를 반복하는 것만으로 LLM 이 무한히 나갑니다.

데스크 자신의 `cost_limit_usd` 는 여기서 아무것도 막지 못합니다. 봇을 새로
세울 때마다 데스크도 새로 만들어져서 그 값이 0 부터 다시 세기 때문입니다.

`/api/evaluate` 는 처음부터 두 가지를 했습니다 — 부르기 전에 요금제를 묻고,
끝나면 실제 비용을 적습니다. 봇 경로도 같아야 합니다.
"""
from __future__ import annotations

import re
from pathlib import Path

TRADER = Path("quant/live/trader.py").read_text(encoding="utf-8")
REGISTRY = Path("quant/webapp/registry.py").read_text(encoding="utf-8")


def _opening() -> str:
    """지금 한 번 심의하는 코드. docstring 은 뺍니다.

    이 함수의 docstring 이 `desk.update()` 를 언급하는데, 그걸 코드로 세면
    "심의를 먼저 부르고 나중에 묻는다" 는 없는 결함이 잡힙니다. 실제로
    잡혔습니다.
    """
    m = re.search(r"async def _deliberate_now\(self[^)]*\).*?\n\n    (?:async )?def",
                  TRADER, re.S)
    assert m, "심의 본문을 찾지 못했습니다"
    return re.sub(r'"""..*?"""', "", m.group(0), count=1, flags=re.S)


# ── 계량은 데스크 한 곳에서 — 함수를 **실행** 해 확인합니다 ──────────────
#
# 예전에는 이 파일이 `_deliberate_now` 의 **본문 문자열** 에 `meter.allow` 가
# 있는지 봤습니다. 그러면 계량이 어디 있는지는 지켜지지만 **어떤 경로가
# 계량되는지** 는 지켜지지 않습니다. 실제로 그 틈으로 빠져나간 것이 있습니다:
# 봉마다 도는 심의(`on_bars` → `TradingDesk.update`)는 요금제를 묻지도 사용량을
# 적지도 않았습니다 — 시작 버튼보다 훨씬 많이 도는, 돈이 되는 경로입니다.
#
# 이제 계량은 데스크 한 곳에 있고, 아래 검사는 두 경로 모두를 실제로 돌립니다.

from datetime import datetime, timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402

from quant.alpha.desk import TradingDesk  # noqa: E402
from quant.alpha.llm_client import LLMUsage  # noqa: E402
from quant.core.account import Portfolio  # noqa: E402
from quant.core.clock import SimClock  # noqa: E402
from quant.core.context import Context  # noqa: E402
from quant.core.events import EventBus  # noqa: E402
from quant.core.types import UTC, Bar, RunMode, Symbol  # noqa: E402
from quant.live.spend import SpendMeter  # noqa: E402

SYM = Symbol("005930", venue="toss", quote_currency="KRW",
             tick_size=Decimal("100"))
T0 = datetime(2026, 1, 5, tzinfo=UTC)


class CountingLLM:
    """부른 횟수를 세는 대역. `fail=True` 면 부른 **뒤에** 실패합니다."""

    def __init__(self, fail: bool = False):
        self.usage = LLMUsage()
        self.calls = 0
        self.fail = fail

    async def complete(self, system, user, schema=None):
        self.calls += 1
        self.usage.add(1_000, 500)
        if self.fail:
            raise RuntimeError("모델 오류")
        props = set((schema or {}).get("properties", {}))
        if "data_sufficient" in props:
            return {"stance": "neutral", "conviction": 0.1,
                    "key_points": ["x"], "data_sufficient": True}
        if "proposed_scale" in props:
            return {"argument": "x", "proposed_scale": 0.5}
        if "position_scale" in props:
            return {"position_scale": 0.5, "veto": False, "reasoning": "x"}
        if "strategic_actions" in props:
            return {"rating": "hold", "rationale": "x",
                    "strategic_actions": "x", "conviction": 0.1}
        if "entry_style" in props:
            return {"action": "hold", "entry_style": "market_now",
                    "execution_note": "x", "conviction": 0.1}
        if "invalidation" in props:
            return {"action": "hold", "conviction": 0.1, "rationale": "x",
                    "invalidation": "x"}
        return {"argument": "x", "conviction": 0.1}


class Recorder:
    """`SpendMeter` 가 받는 두 콜백을 흉내 냅니다."""

    def __init__(self, allowed=True, why=""):
        self.allowed, self.why = allowed, why
        self.records: list[tuple[int, float]] = []

    def meter(self) -> SpendMeter:
        return SpendMeter(allow=lambda: (self.allowed, self.why),
                          record=lambda c, s: self.records.append((c, s)))


def bars_ctx(n: int = 260):
    pf = Portfolio(10_000_000.0, "KRW")
    ctx = Context(SimClock(T0 + timedelta(days=n)), pf, EventBus(),
                  timeframe="1d", run_mode=RunMode.DRY_RUN)
    ctx.universe = [SYM]
    for i in range(n):
        p = 70_000.0 * (1 + 0.0004 * i)
        ctx.push_bar(Bar(SYM, T0 + timedelta(days=i), p * 0.995, p * 1.015,
                         p * 0.985, p, 1e6, "1d"))
    return ctx


async def run_one_bar(desk, ctx):
    """봉 하나를 데스크에 넣는다 — `on_bars` 가 하는 것과 같은 호출."""
    return await desk.update(ctx, {SYM.key: ctx.history(SYM)[-1]})


@pytest.mark.asyncio
async def test_the_bar_path_asks_before_spending():
    """봉마다 도는 심의가 요금제를 묻는가 — 시작 버튼보다 훨씬 자주 돕니다."""
    llm = CountingLLM()
    recorder = Recorder(allowed=False, why="이번 달 상한에 도달했습니다")
    desk = TradingDesk(llm, memory=False)
    desk.meter = recorder.meter()

    out = await run_one_bar(desk, bars_ctx())

    assert llm.calls == 0, "거절당했는데 LLM 을 불렀습니다"
    assert out == []
    assert "상한" in desk.status()["metered_note"], "쉬는 이유를 말하지 않습니다"


@pytest.mark.asyncio
async def test_the_bar_path_records_what_it_spent():
    llm = CountingLLM()
    recorder = Recorder()
    desk = TradingDesk(llm, memory=False)
    desk.meter = recorder.meter()

    await run_one_bar(desk, bars_ctx())

    assert llm.calls > 0, "픽스처가 심의까지 가지 못했습니다"
    assert recorder.records, "봉마다 도는 심의가 계량되지 않습니다"
    calls, spent = recorder.records[0]
    assert calls == llm.calls and spent > 0


@pytest.mark.asyncio
async def test_it_records_even_when_the_deliberation_fails():
    """실패해도 부른 만큼은 청구됩니다.

    성공만 계량하면 실패한 호출의 비용이 아무 계정에도 안 잡히고, 나중에
    소급해서 만들 수도 없습니다.
    """
    llm = CountingLLM(fail=True)
    recorder = Recorder()
    desk = TradingDesk(llm, memory=False)
    desk.meter = recorder.meter()

    await run_one_bar(desk, bars_ctx())

    assert llm.calls > 0
    assert recorder.records, "실패한 호출이 아무 계정에도 잡히지 않습니다"


@pytest.mark.asyncio
async def test_a_desk_without_a_meter_still_deliberates():
    """계정이 없는 배포에는 셀 사람이 없습니다. 그때도 데스크는 돌아야 합니다."""
    llm = CountingLLM()
    desk = TradingDesk(llm, memory=False)

    await run_one_bar(desk, bars_ctx())

    assert llm.calls > 0


def test_the_meter_is_asked_once_per_deliberation_not_twice():
    """계량이 두 곳에 있으면 같은 호출이 두 번 청구됩니다."""
    body = _opening()
    assert "meter.record" not in body, (
        "시작 시 심의가 데스크와 별개로 또 적습니다 — 같은 호출이 두 번 "
        "청구됩니다")
    assert "meter.allow" not in body, (
        "두 곳에서 요금제를 물으면 규칙이 갈라집니다")


def test_the_trader_hands_its_meter_to_the_desk():
    """붙이지 않으면 위 검사가 전부 통과해도 실제 봇은 계량되지 않습니다."""
    assert "desk.meter = meter" in TRADER, (
        "트레이더가 데스크에 계량기를 물리지 않습니다")


def test_the_trader_does_not_need_to_know_about_accounts():
    """`LiveTrader` 가 요금제·계정을 알면 CLI 단독 실행이 그것에 묶입니다."""
    assert "usage" not in TRADER.lower() or "UsageStore" not in TRADER
    assert "from quant.webapp" not in TRADER, (
        "라이브 트레이더가 웹 계층을 import 합니다 — 단일 사용자 CLI 가 "
        "계정 DB 없이는 못 돌게 됩니다.")


def test_the_registry_wires_a_meter_for_every_bot():
    assert "meter=self._meter(" in REGISTRY, "봇에 계량기를 물리지 않습니다"
    m = re.search(r"    def _meter\(self.*?\n    (?:async )?def ", REGISTRY, re.S)
    assert m, "_meter 를 찾지 못했습니다"
    body = m.group(0)
    assert "usage.allow" in body and "usage.record_spend" in body
    # 자기 키면 상한 면제 — 다만 "이름이 등록됐는가" 가 아니라 "그 값이 실제로
    # 데스크에 들어갔는가" 로 판정해야 합니다.
    assert "desk_owns_key" in body, (
        "자기 키 판정을 이름 등록 여부로 합니다 — 아무 문자열이나 저장하면 "
        "상한이 사라지면서 정작 심의는 운영자 키로 나갑니다.")


def test_a_single_user_deployment_still_runs_without_a_meter():
    """계정이 없는 배포에는 셀 사람이 없습니다. 그때도 봇은 떠야 합니다."""
    assert "meter: SpendMeter | None = None" in TRADER, (
        "계량기가 필수 인자입니다 — CLI 단독 실행이 깨집니다")
