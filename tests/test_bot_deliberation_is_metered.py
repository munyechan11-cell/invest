"""봇이 봉마다 돌리는 LLM 심의도 계량과 요금제 상한을 통과하는가.

데스크 비용은 운영자가 냅니다. `usage.py` 가 존재하는 이유가 그것이고, 그
모듈은 스스로를 "과금 정책이 아니라 폭주 방지" 라고 말합니다. 그런데 계량을
부르는 자리가 사용자가 직접 누르는 `/api/evaluate` 와 ▶ 직후 한 번 도는
개장 전 심의뿐이었습니다 — 정작 돈이 나가는 쪽은 사람이 자는 동안 봉마다
16석을 돌리는 봇입니다.

여기서 검사하는 것은 구현식이 아니라 성질 세 가지입니다.

**적힌 것 = 실제로 쓴 것.** 한 사이클은 종목들을 `gather` 로 동시에 심의합니다.
그래서 "이 심의가 얼마를 썼나" 를 클라이언트 전역 카운터의 before/after 로
재면 그 사이에 낀 형제 종목의 호출이 통째로 섞여 들어옵니다 — 단조 증가
카운터라 언제나 **과다**입니다. 종목 하나짜리 테스트는 이걸 못 봅니다(차분이
우연히 정확합니다). 그래서 여기 테스트는 전부 **종목 여럿**이고, 대역 LLM 은
실제 네트워크처럼 루프에 양보합니다.

**상한에 걸리면 아무것도 안 쓴다.** 물어보고 무시하면 안 묻는 것과 같습니다.

**데스크와 카운슬 둘 다.** 계량되지 않는 LLM 알파가 하나라도 남아 있으면
그리로 옮겨 타면 그만입니다. 그래서 아래 성질 테스트는 두 알파를 모두
`update()` 로 **직접 돌립니다** — 바인딩만 확인하고 계량기를 손으로 부르는
테스트는 알파가 조용한 no-op 이어도 통과하는데, 그 조용한 no-op 이 바로 이
결함의 원래 모습이었습니다.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.alpha.council import ResearchCouncilAlpha
from quant.alpha.desk import TradingDesk
from quant.alpha.llm_client import LLMUsage, metered
from quant.config.schema import (
    BrokerConfig,
    CostConfig,
    DataConfig,
    ExecutionConfig,
    ModelSpec,
    PortfolioConfig,
    RiskConfig,
    StrategyConfig,
    SymbolSpec,
    UniverseConfig,
)
from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, Bar, RunMode, Symbol
from quant.live.spend import SpendMeter
from quant.webapp.usage import PLANS, Plan, UsageStore

T0 = datetime(2024, 6, 3, tzinfo=UTC)
TICKERS = ("005930", "000660", "035420", "051910")
SYMBOLS = [Symbol(t, venue="kis", quote_currency="KRW", tick_size=Decimal("100"))
           for t in TICKERS]


class YieldingLLM:
    """좌석 응답 대역. 실제 호출처럼 **루프에 양보합니다**.

    양보가 핵심입니다. 양보하지 않으면 종목별 심의가 사실상 순차로 돌아서,
    전역 카운터 차분으로 재는 틀린 구현도 우연히 맞는 답을 냅니다. 실제
    네트워크 호출은 반드시 양보하므로 여기서도 그렇게 둡니다.
    """

    def __init__(self):
        self.usage = LLMUsage()

    async def complete(self, system, user, schema=None):
        await asyncio.sleep(0)
        props = set((schema or {}).get("properties", {}))
        kind = (
            "preflight" if schema is None else
            "analyst" if "data_sufficient" in props else
            "risk_debate" if "proposed_scale" in props else
            "risk_verdict" if "position_scale" in props else
            "plan" if "strategic_actions" in props else
            "trade" if "entry_style" in props else
            "head" if "invalidation" in props else
            "verdict" if "rating" in props else
            "debate"
        )
        self.usage.add(400, 200)
        await asyncio.sleep(0)
        if kind == "preflight":
            return "OK"
        return {
            "analyst": {"stance": "bullish", "conviction": 0.9,
                        "key_points": ["scripted"], "data_sufficient": True},
            "debate": {"argument": "scripted", "conviction": 0.6},
            "risk_debate": {"argument": "scripted", "proposed_scale": 0.6},
            "risk_verdict": {"position_scale": 0.9, "veto": False, "veto_reason": "",
                             "max_loss_pct": 3.0, "reasoning": "scripted"},
            "plan": {"rating": "buy", "rationale": "scripted",
                     "strategic_actions": "scale in", "conviction": 0.7},
            "trade": {"action": "buy", "entry_style": "scale_in", "tranches": 2,
                      "execution_note": "scripted", "conviction": 0.7},
            "head": {"action": "buy", "conviction": 0.9, "target_weight_pct": 20,
                     "expected_move_pct": 4.0, "horizon_bars": 10,
                     "rationale": "scripted", "invalidation": "20일선 이탈",
                     "dissent": ""},
            "verdict": {"rating": "buy", "conviction": 0.8, "expected_move_pct": 4.0,
                        "horizon_bars": 10, "rationale": "scripted",
                        "position_scale": 0.9, "veto": False, "veto_reason": "",
                        "risks": ["scripted"]},
        }[kind]


def make_ctx(symbols, bars=260, price=70_000.0):
    pf = Portfolio(10_000_000.0, "KRW")
    ctx = Context(SimClock(T0 + timedelta(days=bars)), pf, EventBus(),
                  timeframe="1d", run_mode=RunMode.DRY_RUN)
    ctx.universe = list(symbols)
    for i in range(bars):
        for n, sym in enumerate(symbols):
            p = price * (1 + 0.0004 * i) * (1 + 0.03 * n)
            ctx.push_bar(Bar(sym, T0 + timedelta(days=i), p, p * 1.012, p * 0.988,
                             p, 1e6, "1d"))
    return ctx


def make_desk(llm, **kw):
    kw.setdefault("max_symbols_per_run", 4)
    kw.setdefault("debate_rounds", 1)
    kw.setdefault("risk_debate_rounds", 1)
    kw.setdefault("memory", False)
    return TradingDesk(llm, **kw)


def make_council(llm, **kw):
    kw.setdefault("max_symbols_per_run", 4)
    kw.setdefault("cadence_bars", 1)
    kw.setdefault("debate_rounds", 1)
    return ResearchCouncilAlpha(llm, **kw)


#: 두 LLM 알파를 같은 성질로 검사합니다. 하나만 지키면 나머지 하나가
#: 계량되지 않는 통로로 남습니다.
ALPHAS = {"desk": make_desk, "council": make_council}


def latest_bars(ctx, symbols):
    return {s.key: ctx.history(s, 1)[0] for s in symbols}


def next_bar(ctx, symbols):
    """다음 봉을 만들고 시계를 그만큼 민다.

    `ctx.history` 는 미래 봉을 걸러내고 두 알파 모두 (종목, 봉시각) 으로
    캐시하므로, 시계를 밀지 않으면 두 번째 사이클이 첫 봉을 다시 보고 캐시로
    답합니다 — 그러면 호출이 0 이라 아무 성질도 검사하지 못합니다.
    """
    for sym in symbols:
        last = ctx.history(sym, 1)[0]
        ctx.push_bar(Bar(sym, last.ts + timedelta(days=1), last.close, last.close,
                         last.close, last.close, 1e6, "1d"))
    ctx.clock.set(ctx.now + timedelta(days=1))


class Recorder:
    """`SpendMeter` 자리에 끼우는 대역 — 상한은 항상 열려 있습니다."""

    def __init__(self, allowed=True, why=""):
        self.calls = 0
        self.cost_usd = 0.0
        self.records = 0
        self._allowed, self._why = allowed, why

    def meter(self) -> SpendMeter:
        return SpendMeter(allow=lambda: (self._allowed, self._why),
                          record=self._record)

    def _record(self, llm_calls, cost_usd):
        self.calls += llm_calls
        self.cost_usd += cost_usd
        self.records += 1


def actual(model):
    """이 알파가 지금까지 실제로 쓴 (호출 수, 달러).

    대역 계량기가 아니라 **클라이언트가 센 것**을 읽습니다 — 계량기가 자기
    자신과 비교하면 어떤 값을 적어도 통과합니다. 데스크는 분석석·결정석
    두 클라이언트를 합쳐 주는 `status()`/`estimated_cost_usd` 를 갖고 있고,
    카운슬은 클라이언트가 하나뿐입니다.
    """
    if hasattr(model, "status"):
        return model.status()["llm_calls"], model.estimated_cost_usd
    return model.usage.calls, model.usage.cost_usd


def spent(model, before):
    now = actual(model)
    return now[0] - before[0], now[1] - before[1]


# ── 적힌 것 = 실제로 쓴 것 ────────────────────────────────────────────────
@pytest.mark.parametrize("alpha", sorted(ALPHAS))
@pytest.mark.parametrize("n_symbols", [1, 2, 4])
def test_what_is_billed_is_what_the_bot_actually_spent(alpha, n_symbols):
    """계정에 적힌 지출이 알파가 실제로 쓴 것과 같아야 합니다.

    수정 전에는 봉마감 사이클이 계량기를 아예 부르지 않았습니다(적힌 것 0).
    그리고 계량을 전역 카운터 차분으로 붙이면 이번에는 반대로 부풉니다 —
    그 결과는 두 방향으로 동시에 나쁩니다: 사용자는 산 것의 1/N 만 쓰고
    상한에 걸리고, 운영자가 요금제를 정할 때 보는 `operator_month()` 는
    N배로 부풀어 원가를 잘못 잡게 만듭니다.
    """
    symbols = SYMBOLS[:n_symbols]
    llm, ctx = YieldingLLM(), make_ctx(symbols)
    model = ALPHAS[alpha](llm)
    rec = Recorder()
    model.bind_meter(rec.meter())

    asyncio.run(model.on_start(ctx))         # 사전 점검 호출 — 심의가 아닙니다
    before = actual(model)
    asyncio.run(model.update(ctx, latest_bars(ctx, symbols)))
    used_calls, used_cost = spent(model, before)

    assert used_calls > 0, "대역이 아무 호출도 하지 않았습니다 — 테스트가 무의미합니다"
    assert rec.records == n_symbols, "심의 한 건마다 한 번씩 적혀야 합니다"
    assert rec.calls == used_calls, (
        f"{alpha} {n_symbols}종목: 실제 {used_calls}콜인데 {rec.calls}콜이 "
        f"청구됐습니다 ({rec.calls / used_calls:.2f}배)")
    assert rec.cost_usd == pytest.approx(used_cost, rel=1e-9)


def test_two_models_are_priced_at_their_own_rates():
    """분석석과 결정석은 다른 모델을 씁니다 — `record_spend` 가 있는 이유입니다.

    합계 토큰을 한 단가로 환산하면 싼 쪽을 비싸게(또는 그 반대로) 칩니다.
    계량기도 같은 함정을 밟으면 안 됩니다.
    """
    analyst, decider = YieldingLLM(), YieldingLLM()
    analyst.usage.model = "gemini-3.5-flash-lite"      # 싼 쪽
    decider.usage.model = "claude-opus"                # 비싼 쪽
    ctx = make_ctx(SYMBOLS[:2])
    desk = make_desk(analyst, decision_llm=decider)
    rec = Recorder()
    desk.bind_meter(rec.meter())

    asyncio.run(desk.on_start(ctx))
    before = desk.estimated_cost_usd
    asyncio.run(desk.update(ctx, latest_bars(ctx, SYMBOLS[:2])))

    assert decider.usage.calls > 0, "결정석이 따로 불리지 않았습니다"
    # 두 단가를 뭉개면 이 등식이 깨집니다 — 한쪽으로 치우친 값이 나옵니다.
    assert rec.cost_usd == pytest.approx(desk.estimated_cost_usd - before, rel=1e-9)


@pytest.mark.parametrize("alpha", sorted(ALPHAS))
def test_a_cached_cycle_bills_nothing(alpha):
    """같은 봉을 다시 보면 호출이 없습니다 — 안 쓴 것을 청구하면 안 됩니다."""
    llm, ctx = YieldingLLM(), make_ctx(SYMBOLS[:2])
    model = ALPHAS[alpha](llm)
    rec = Recorder()
    model.bind_meter(rec.meter())
    asyncio.run(model.on_start(ctx))
    asyncio.run(model.update(ctx, latest_bars(ctx, SYMBOLS[:2])))
    billed, calls = rec.calls, actual(model)[0]

    asyncio.run(model.update(ctx, latest_bars(ctx, SYMBOLS[:2])))
    assert actual(model)[0] == calls, "캐시가 안 먹었습니다 — 테스트가 무의미합니다"
    assert rec.calls == billed, "호출 없이 청구가 늘었습니다"


def test_a_manual_deliberation_and_the_bot_cycle_do_not_bill_each_other():
    """사용자가 `/api/evaluate` 를 누른 순간 봇 사이클이 돌고 있어도.

    돌고 있는 봇이 있으면 `/api/evaluate` 는 **그 봇의 데스크 객체**를 그대로
    씁니다(기억과 이력이 이어져야 하니까). 두 경로가 같은 전역 카운터의 차분을
    뜨면 서로의 호출을 세고, 같은 호출이 같은 계정에 두 번 적힙니다.
    """
    llm, ctx = YieldingLLM(), make_ctx(SYMBOLS)
    desk = make_desk(llm, max_symbols_per_run=3)
    bot = Recorder()
    desk.bind_meter(bot.meter())
    asyncio.run(desk.on_start(ctx))
    before = actual(desk)[0]

    async def manual():
        # 서버 요청 핸들러가 하는 것: 자기 계량기를 열고 심의 한 건을 부른다.
        with metered() as spend:
            await desk.deliberate(ctx, SYMBOLS[3])
        return spend

    async def both():
        # 봇 태스크와 요청 태스크는 형제입니다 — 어느 쪽도 상대의 컨텍스트를
        # 물려받지 않습니다. 실제 서버에서도 그렇습니다.
        cycle = asyncio.create_task(desk.update(ctx, latest_bars(ctx, SYMBOLS[:3])))
        request = asyncio.create_task(manual())
        _, spend = await asyncio.gather(cycle, request)
        return spend

    spend = asyncio.run(both())
    total = actual(desk)[0] - before

    assert spend.calls > 0 and bot.calls > 0, "한쪽이 아무것도 세지 못했습니다"
    assert spend.calls + bot.calls == total, (
        f"실제 {total}콜인데 봇 {bot.calls} + 수동 {spend.calls} "
        f"= {spend.calls + bot.calls} 이 청구됐습니다")


@pytest.mark.parametrize("alpha", sorted(ALPHAS))
def test_a_deliberation_that_fails_is_still_billed(alpha):
    """마감을 넘겨 버린 심의도 호출한 만큼은 이미 나갔습니다.

    성공만 계량하면 실패한 호출의 비용이 아무 계정에도 잡히지 않습니다 —
    그리고 실패는 대개 몰려서 옵니다.
    """
    class Broken(YieldingLLM):
        async def complete(self, system, user, schema=None):
            out = await super().complete(system, user, schema)
            props = set((schema or {}).get("properties", {}))
            if "invalidation" in props or "rating" in props:
                raise RuntimeError("마지막 좌석이 죽었다")
            return out

    llm, ctx = Broken(), make_ctx(SYMBOLS[:2])
    model = ALPHAS[alpha](llm)
    rec = Recorder()
    model.bind_meter(rec.meter())
    asyncio.run(model.on_start(ctx))
    before = actual(model)
    asyncio.run(model.update(ctx, latest_bars(ctx, SYMBOLS[:2])))

    used = spent(model, before)[0]
    assert used > 0, "실패 대역이 아무 호출도 하지 않았습니다"
    assert rec.calls == used, f"{alpha}: {used}콜을 쓰고 {rec.calls}콜을 청구했습니다"


# ── 상한 ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("alpha", sorted(ALPHAS))
def test_a_capped_account_stops_the_bot_before_it_spends(alpha):
    """상한에 걸린 계정의 봇은 호출을 **하나도** 하지 않아야 합니다."""
    llm, ctx = YieldingLLM(), make_ctx(SYMBOLS)
    model = ALPHAS[alpha](llm)
    model.bind_meter(
        Recorder(allowed=False, why="이번 달 AI 데스크 사용량 상한에 도달했습니다").meter())
    asyncio.run(model.on_start(ctx))
    before = actual(model)[0]
    insights = asyncio.run(model.update(ctx, latest_bars(ctx, SYMBOLS)))

    assert insights == []
    assert actual(model)[0] == before, "상한에 걸렸는데 돈을 썼습니다"


@pytest.mark.parametrize("alpha", sorted(ALPHAS))
def test_the_free_plan_actually_bites_a_running_bot(alpha, tmp_path):
    """상한이 걸리는 자리는 대역이 아니라 **실제 원장**이어야 합니다.

    무료 요금제는 하루 5회입니다. 4종목짜리 봇은 한 봉에 4회를 쓰므로 첫 봉은
    열리고(0 < 5), 두 번째 봉도 열리고(4 < 5), 세 번째 봉에서 닫힙니다. 상한은
    사이클 시작에 한 번만 보기 때문에 마지막 사이클이 한 사이클치를 넘깁니다 —
    봉 하나를 반쯤 심의해서 어떤 종목은 보고 어떤 종목은 안 본 상태로 두는
    쪽이 더 나쁘다고 봤습니다.
    """
    store = UsageStore(tmp_path / "usage.db")
    uid = 7
    llm, ctx = YieldingLLM(), make_ctx(SYMBOLS)
    model = ALPHAS[alpha](llm)
    model.bind_meter(SpendMeter(
        allow=lambda: store.allow(uid, "free", own_key=False),
        record=lambda c, u: store.record_spend(uid, c, u, own_key=False)))
    asyncio.run(model.on_start(ctx))

    for _ in range(2):
        asyncio.run(model.update(ctx, latest_bars(ctx, SYMBOLS)))
        next_bar(ctx, SYMBOLS)
    assert store.today(uid)["deliberations"] == 8
    used = actual(model)

    asyncio.run(model.update(ctx, latest_bars(ctx, SYMBOLS)))
    assert store.today(uid)["deliberations"] == 8, "상한을 넘겨 계속 심의했습니다"
    assert actual(model)[0] == used[0], "상한에 걸린 뒤에도 호출이 나갔습니다"
    allowed, why = store.allow(uid, "free")
    assert not allowed and "5회" in why


@pytest.mark.parametrize("alpha", sorted(ALPHAS))
def test_the_cost_cap_fires_at_real_spend_not_at_a_fraction_of_it(
        alpha, tmp_path, monkeypatch):
    """월 비용 상한은 **실제로 쓴 금액**에서 걸려야 합니다.

    이 성질이 이 결함의 사용자 쪽 얼굴입니다. 원장이 종목 수만큼 부풀면 돈 낸
    사람이 산 것의 1/N 만 쓴 시점에 데스크가 닫히고, 그러면서 화면은 "다
    썼습니다" 라고 말합니다. 상한이 아니라 벌칙이 됩니다.
    """
    monkeypatch.setitem(PLANS, "qa", Plan("qa", "QA", daily_deliberations=0,
                                          monthly_cost_usd=1.0))
    store = UsageStore(tmp_path / "usage.db")
    uid = 11
    llm, ctx = YieldingLLM(), make_ctx(SYMBOLS)
    model = ALPHAS[alpha](llm)
    model.bind_meter(SpendMeter(
        allow=lambda: store.allow(uid, "qa", own_key=False),
        record=lambda c, u: store.record_spend(uid, c, u, own_key=False)))
    asyncio.run(model.on_start(ctx))
    base = actual(model)[1]

    for _ in range(40):
        if not store.allow(uid, "qa")[0]:
            break
        asyncio.run(model.update(ctx, latest_bars(ctx, SYMBOLS)))
        next_bar(ctx, SYMBOLS)
    else:
        pytest.fail("$1.00 상한이 40사이클 동안 걸리지 않았습니다")

    real = actual(model)[1] - base
    assert real >= 1.0, f"{alpha}: 실제 ${real:.4f} 만 쓰고 $1.00 상한에 걸렸습니다"
    assert store.month(uid)["cost_usd"] == pytest.approx(real, abs=1e-3)


@pytest.mark.parametrize("alpha", sorted(ALPHAS))
def test_an_unbound_alpha_runs_exactly_as_before(alpha):
    """CLI 로 자기 컴퓨터에서 돌리는 봇에는 계정이 없습니다.

    키도 비용도 본인 것이라 잴 이유가 없고, 계량기를 안 묶었다고 심의가
    멈추면 안 됩니다.
    """
    llm, ctx = YieldingLLM(), make_ctx(SYMBOLS[:2])
    model = ALPHAS[alpha](llm)
    asyncio.run(model.on_start(ctx))
    insights = asyncio.run(model.update(ctx, latest_bars(ctx, SYMBOLS[:2])))
    assert actual(model)[0] > 0, "계량기가 없다고 심의가 멈췄습니다"
    assert isinstance(insights, list)


# ── 봇에 실제로 물려 있는가 ────────────────────────────────────────────────
def live_config(alpha: list[ModelSpec]) -> StrategyConfig:
    """오프라인으로 뜨는 최소 봇 설정 — 합성 시세, 페이퍼 브로커."""
    return StrategyConfig(
        name="metering-test",
        mode=RunMode.DRY_RUN,
        data=DataConfig(provider="synthetic", params={"seed": 3}, timeframe="1d",
                        warmup_bars=60, cache=False),
        universe=UniverseConfig(
            symbols=[SymbolSpec(ticker=t, venue="SIM") for t in ("AAA", "BBB")]),
        alpha=alpha,
        portfolio=PortfolioConfig(starting_cash=100_000, max_gross_leverage=1.0,
                                  cash_reserve_pct=0.0, min_trade_weight=0.0),
        risk=RiskConfig(),
        execution=ExecutionConfig(min_order_notional=1.0),
        costs=CostConfig(preset="zero_cost"),
        broker=BrokerConfig(type="paper"),
    )


def test_a_bot_binds_its_meter_to_every_llm_alpha(tmp_path):
    """봇이 세워질 때 LLM 알파 **전부**가 그 사람 계량기에 묶여야 합니다.

    이름으로 `desk` 만 찾으면 `council` 이 계량되지 않는 통로로 남습니다 —
    한쪽만 막힌 상한은 상한이 아닙니다. 규칙 기반 알파는 비용이 0원이라
    묶을 것도 없습니다.
    """
    from quant.live.trader import LiveTrader

    # 키는 클라이언트를 세우는 데만 쓰입니다 — 이 테스트는 심의를 부르지
    # 않으므로 네트워크로 나가는 것이 없습니다.
    llm = {"llm": {"provider": "anthropic", "api_key": "TEST-KEY-NEVER-CALLED"}}
    cfg = live_config([ModelSpec(type="desk", params=llm),
                       ModelSpec(type="council", params=llm),
                       ModelSpec(type="ema_cross")])
    rec = Recorder()
    meter = rec.meter()
    trader = LiveTrader(cfg, state_path=str(tmp_path / "s.db"), meter=meter)
    try:
        models = trader.engine.alpha.models
        llm_alphas = [m for m in models if hasattr(m, "bind_meter")]
        assert len(llm_alphas) == 2, "데스크와 카운슬 둘 다 있어야 합니다"
        for m in llm_alphas:
            assert m.meter is meter, f"{m.name} 이 계량기에 묶이지 않았습니다"
    finally:
        trader.state.close()


class StubDesk:
    """`_deliberate_now` 가 실제로 보는 것만 갖춘 데스크.

    진짜 좌석을 부르지 않는 이유: 여기서 검사하는 것은 심의 내용이 아니라
    **누가 몇 번 청구하는가** 이고, 진짜 데스크를 쓰면 그 답이 대역 LLM 의
    호출 수에 가려집니다.
    """

    name = "desk"

    def __init__(self, meter=None, deliberations=2, calls_each=10, cost_each=0.05):
        self.meter = meter
        self._n, self._calls, self._cost = deliberations, calls_each, cost_each
        self.llm_calls = 0
        self.estimated_cost_usd = 0.0

    def status(self):
        return {"llm_calls": self.llm_calls}

    async def update(self, ctx, bars):
        for _ in range(self._n):
            self.llm_calls += self._calls
            self.estimated_cost_usd += self._cost
            # 묶여 있으면 심의 한 건마다 자기가 적습니다 — 실제 데스크가
            # `_deliberate_cached` 에서 하는 것과 같습니다.
            if self.meter is not None:
                self.meter.record(self._calls, self._cost)
        return []


def _run_opening(tmp_path, desk, meter):
    """개장 전 심의 한 번을 돌린다 — `LiveTrader._deliberate_now` 그대로."""
    from quant.core.types import Symbol as Sym
    from quant.live.trader import LiveTrader

    cfg = live_config([ModelSpec(type="ema_cross")])
    trader = LiveTrader(cfg, state_path=str(tmp_path / "open.db"), meter=meter)
    try:
        ctx = trader.engine.ctx
        sym = Sym("AAA", venue="SIM")
        ctx.universe = [sym]
        ctx.push_bar(Bar(sym, T0, 100.0, 101.0, 99.0, 100.0, 1e6, "1d"))
        trader.desk = lambda: desk
        asyncio.run(trader._deliberate_now("테스트"))
    finally:
        trader.state.close()


def test_the_opening_deliberation_is_billed_once_not_twice(tmp_path):
    """▶ 직후의 심의가 두 번 청구되면 안 됩니다.

    이 경로는 그 데스크의 `update()` 를 그대로 부릅니다. 데스크가 계정에
    묶이면서 심의 한 건마다 스스로 적기 시작했으므로, 트레이더가 전역 카운터
    차분으로 한 번 더 적으면 같은 호출이 두 번 청구됩니다 — 그리고 그 두 번째
    청구는 같은 데스크를 쓰는 `/api/evaluate` 의 호출까지 함께 셉니다.
    """
    rec = Recorder()
    meter = rec.meter()
    _run_opening(tmp_path, StubDesk(meter=meter), meter)

    assert rec.calls == 20, f"20콜을 쓰고 {rec.calls}콜이 청구됐습니다"
    assert rec.cost_usd == pytest.approx(0.10)
    assert rec.records == 2, f"심의 2건인데 원장에 {rec.records}줄이 적혔습니다"


def test_the_manual_endpoint_does_not_bill_from_the_shared_desks_counters():
    """`/api/evaluate` 가 데스크 전역 카운터의 차분으로 청구하면 안 됩니다.

    봇이 돌고 있으면 그 핸들러는 **봇의 데스크 객체**를 그대로 씁니다. 차분을
    뜨는 순간 그 사이 봇이 다른 종목을 심의한 호출까지 이 사람에게 청구되고,
    같은 호출이 봇 쪽 원장에도 적혀 이중청구가 됩니다.

    성질이 아니라 코드 모양을 보는 테스트인 것을 압니다 — 진짜 동시성을
    보려면 앱 전체를 띄워야 하고, 그 성질 자체는 바로 위
    `test_a_manual_deliberation_and_the_bot_cycle_do_not_bill_each_other` 가
    같은 계량기로 검사합니다. 여기서 막는 것은 핸들러가 옛 방식으로
    되돌아가는 것뿐입니다.
    """
    from pathlib import Path

    server = Path("quant/api/server.py").read_text(encoding="utf-8")
    body = re.search(r'@app\.post\("/api/evaluate"\).*?@app\.get', server, re.S).group(0)
    assert "with metered()" in body, "이 요청만 세는 계량기를 열지 않습니다"
    assert 'model.status()["llm_calls"]' not in body, (
        "데스크 전역 호출 수로 청구합니다 — 돌고 있는 봇의 호출이 섞입니다")
    assert "model.estimated_cost_usd" not in body, (
        "데스크 전역 비용으로 청구합니다 — 돌고 있는 봇의 지출이 섞입니다")


def test_an_unbound_desk_is_still_billed_by_the_trader(tmp_path):
    """묶이지 않은 데스크에서는 트레이더가 계속 적어야 합니다.

    계량기를 받았는데 아무도 안 적는 조합이 생기면, ▶ 를 껐다 켜는 것만으로
    운영자 카드가 무제한으로 열립니다 — 이 경로에 계량이 붙은 이유입니다.
    """
    rec = Recorder()
    _run_opening(tmp_path, StubDesk(meter=None), rec.meter())

    assert rec.calls == 20, f"20콜을 쓰고 {rec.calls}콜이 청구됐습니다"
    assert rec.records == 1, "묶이지 않은 데스크는 트레이더가 사이클 단위로 적습니다"
