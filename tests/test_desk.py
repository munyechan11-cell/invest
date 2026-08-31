"""The 16-seat trading desk.

Every test here uses a scripted stand-in for the language model, so what is
being tested is the *machinery*: that all sixteen seats are consulted, that the
stages feed each other, that a risk veto actually closes a position, that a
missing seat degrades safely rather than silently, and that the desk cannot
escape the rules the rest of the engine imposes on it.
"""
import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.alpha.desk import DeskDecision, DeskMemory, TradingDesk
from quant.alpha.llm_client import LLMError, LLMUsage
from quant.alpha.seats import ALL_SEATS, ANALYST_SEATS, roster
from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus, EventType
from quant.core.types import UTC, Bar, Direction, RunMode, Symbol

SYM = Symbol("005930", venue="kis", quote_currency="KRW", tick_size=Decimal("100"))
T0 = datetime(2024, 6, 3, tzinfo=UTC)


class ScriptedLLM:
    """Stands in for `LLMClient`, answering by the shape of the requested schema."""

    def __init__(self, *, stance="bullish", conviction=0.9, action="buy",
                 position_scale=0.9, veto=False, fail_seats=()):
        self.stance, self.conviction = stance, conviction
        self.action, self.position_scale, self.veto = action, position_scale, veto
        self.fail_seats = set(fail_seats)
        self.usage = LLMUsage()
        self.calls: list[str] = []

    async def complete(self, system, user, schema=None):
        props = set((schema or {}).get("properties", {}))
        kind = (
            "preflight" if schema is None else
            "analyst" if "data_sufficient" in props else
            "risk_debate" if "proposed_scale" in props else
            "risk_verdict" if "position_scale" in props else
            "plan" if "strategic_actions" in props else
            "trade" if "entry_style" in props else
            "head" if "invalidation" in props else
            "debate"
        )
        self.calls.append(kind)
        self.usage.add(400, 200)
        if kind in self.fail_seats:
            raise LLMError(f"simulated failure for {kind}")
        if kind == "preflight":
            return "OK"
        return {
            "analyst": {"stance": self.stance, "conviction": self.conviction,
                        "key_points": ["scripted"], "data_sufficient": True},
            "debate": {"argument": "scripted argument", "conviction": 0.6},
            "risk_debate": {"argument": "scripted risk", "proposed_scale": 0.6},
            "risk_verdict": {"position_scale": self.position_scale, "veto": self.veto,
                             "veto_reason": "scripted veto" if self.veto else "",
                             "max_loss_pct": 3.0, "reasoning": "scripted"},
            "plan": {"rating": "buy", "rationale": "scripted plan",
                     "strategic_actions": "scale in", "conviction": 0.7},
            "trade": {"action": "buy", "entry_style": "scale_in", "tranches": 2,
                      "execution_note": "scripted execution", "conviction": 0.7},
            "head": {"action": self.action, "conviction": self.conviction,
                     "target_weight_pct": 20, "expected_move_pct": 4.0,
                     "horizon_bars": 10, "rationale": "scripted rationale",
                     "invalidation": "20일선 종가 이탈", "dissent": ""},
        }[kind]


def make_ctx(mode=RunMode.DRY_RUN, bars=260, price=70_000.0, invested=0):
    pf = Portfolio(10_000_000.0, "KRW")
    ctx = Context(SimClock(T0 + timedelta(days=bars)), pf, EventBus(),
                  timeframe="1d", run_mode=mode)
    ctx.universe = [SYM]
    for i in range(bars):
        p = price * (1 + 0.0004 * i)
        ctx.push_bar(Bar(SYM, T0 + timedelta(days=i), p, p * 1.012, p * 0.988, p, 1e6, "1d"))
    if invested:
        pos = pf.position(SYM)
        pos.quantity = Decimal(str(invested))
        pos.avg_price = price
        pos.opened_at = T0
        pos.mark(price)
    return ctx


def run_desk(desk, ctx):
    bar = ctx.history(SYM, 1)[0]
    asyncio.run(desk.on_start(ctx))
    return asyncio.run(desk.update(ctx, {SYM.key: bar}))


# ── roster ───────────────────────────────────────────────────────────────
def test_the_desk_has_sixteen_seats():
    assert len(ALL_SEATS) == 16
    assert len(ANALYST_SEATS) == 8
    assert len({s.key for s in ALL_SEATS}) == 16      # no duplicate keys


def test_every_seat_has_a_real_prompt_and_a_schema():
    for seat in ALL_SEATS:
        assert len(seat.system) > 200, f"{seat.key} prompt is too thin to encode method"
        assert seat.schema.get("required"), f"{seat.key} has no required output fields"
        assert seat.title_ko


def test_roster_keeps_the_decision_chain_even_with_one_analyst():
    seats = roster(["flow"])
    stages = {s.stage for s in seats}
    assert stages >= {"analyst", "debate", "risk_debate", "risk_verdict",
                      "plan", "trade", "head"}


def test_roster_rejects_an_empty_analyst_bench():
    with pytest.raises(ValueError):
        roster(["nonexistent_seat"])


# ── the pipeline ─────────────────────────────────────────────────────────
def test_a_full_deliberation_consults_every_stage():
    llm = ScriptedLLM()
    desk = TradingDesk(llm, debate_rounds=2, risk_debate_rounds=1, memory=False)
    insights = run_desk(desk, make_ctx())

    kinds = llm.calls
    # One cheap probe at startup, so an unusable key fails once instead of
    # nineteen times on every bar.
    assert kinds.count("preflight") == 1
    assert kinds.count("analyst") == 8
    assert kinds.count("debate") == 4          # 2 rounds x bull/bear
    assert kinds.count("risk_debate") == 2     # aggressive + conservative
    assert kinds.count("risk_verdict") == 1
    assert kinds.count("plan") == 1
    assert kinds.count("trade") == 1
    assert kinds.count("head") == 1
    assert len(kinds) == 19

    assert len(insights) == 1
    assert insights[0].direction is Direction.UP
    assert insights[0].source == "desk"


def test_conviction_is_scaled_by_the_risk_seat():
    """The risk panel can only shrink. That is the whole contract."""
    strong = TradingDesk(ScriptedLLM(position_scale=1.0), min_conviction=0.1, memory=False)
    weak = TradingDesk(ScriptedLLM(position_scale=0.4), min_conviction=0.1, memory=False)
    a = run_desk(strong, make_ctx())[0]
    b = run_desk(weak, make_ctx())[0]
    assert b.confidence < a.confidence
    assert b.confidence == pytest.approx(a.confidence * 0.4, rel=1e-6)


def test_a_risk_scale_that_drags_conviction_under_the_floor_kills_the_trade():
    """0.9 conviction x 0.75 (a plain 'buy') x 0.4 scale = 0.27 — below the
    0.55 floor, so nothing is emitted. Sizing down far enough is a veto."""
    desk = TradingDesk(ScriptedLLM(position_scale=0.4), min_conviction=0.55, memory=False)
    assert run_desk(desk, make_ctx()) == []


def test_a_veto_closes_the_position_rather_than_merely_blocking_entry():
    desk = TradingDesk(ScriptedLLM(veto=True), memory=False)
    out = run_desk(desk, make_ctx(invested=100))
    assert out and out[0].direction is Direction.FLAT
    assert "거부" in out[0].tag


def test_sell_becomes_a_flat_veto_when_shorting_is_disabled():
    desk = TradingDesk(ScriptedLLM(action="sell", stance="bearish"),
                       allow_short=False, memory=False)
    out = run_desk(desk, make_ctx(invested=100))
    assert out and out[0].direction is Direction.FLAT


def test_hold_emits_nothing_so_other_alphas_keep_control():
    desk = TradingDesk(ScriptedLLM(action="hold"), memory=False)
    assert run_desk(desk, make_ctx(invested=100)) == []


def test_low_conviction_is_dropped():
    desk = TradingDesk(ScriptedLLM(conviction=0.3, position_scale=0.5),
                       min_conviction=0.55, memory=False)
    assert run_desk(desk, make_ctx()) == []


def test_a_failed_risk_seat_shrinks_the_position_instead_of_waving_it_through():
    desk = TradingDesk(ScriptedLLM(fail_seats={"risk_verdict"}), memory=False)
    _out = run_desk(desk, make_ctx())
    decision = desk.history[-1]
    assert decision.position_scale <= 0.4
    assert "실패" in decision.risk.get("reasoning", "") or decision.risk.get("error")


def test_a_failed_analyst_does_not_vote():
    desk = TradingDesk(ScriptedLLM(fail_seats={"analyst"}), memory=False)
    run_desk(desk, make_ctx())
    decision = desk.history[-1]
    assert decision.voting_seats == 0
    assert decision.consensus == 0.0


# ── honesty guards ───────────────────────────────────────────────────────
def test_an_unusable_key_disables_the_desk_instead_of_failing_every_bar():
    """Without the probe an empty credit balance fails all nineteen calls on
    every single bar — it looks like graceful degradation when in fact the desk
    can never work, and each bar pays the full deadline before saying so."""
    llm = ScriptedLLM(fail_seats={"preflight"})
    desk = TradingDesk(llm, memory=False)
    ctx = make_ctx()
    asyncio.run(desk.on_start(ctx))

    status = desk.status()
    assert not status["enabled"]
    assert "사전 점검" in status["disabled_reason"] or "실패" in status["disabled_reason"]
    before = len(llm.calls)
    assert asyncio.run(desk.update(ctx, {SYM.key: ctx.history(SYM, 1)[0]})) == []
    assert len(llm.calls) == before, "비활성화된 데스크가 호출을 계속했습니다"


def test_a_credit_balance_error_says_what_to_do():
    class Broke:
        usage = LLMUsage()

        async def complete(self, system, user, schema=None):
            raise LLMError("anthropic 400: Your credit balance is too low")

    desk = TradingDesk(Broke(), memory=False)
    asyncio.run(desk.on_start(make_ctx()))
    reason = desk.status()["disabled_reason"]
    assert "크레딧" in reason and "Plans & Billing" in reason


def test_the_desk_refuses_to_run_in_a_backtest_by_default():
    desk = TradingDesk(ScriptedLLM(), memory=False)
    ctx = make_ctx(mode=RunMode.BACKTEST)
    asyncio.run(desk.on_start(ctx))
    assert asyncio.run(desk.update(ctx, {SYM.key: ctx.history(SYM, 1)[0]})) == []
    assert "백테스트" in desk.status()["disabled_reason"]


def test_it_can_be_enabled_in_a_backtest_explicitly():
    desk = TradingDesk(ScriptedLLM(), allow_in_backtest=True, memory=False)
    assert run_desk(desk, make_ctx(mode=RunMode.BACKTEST))


def test_the_brief_is_sliced_per_seat():
    """A seat that cannot see the fundamentals cannot opine on them."""
    desk = TradingDesk(ScriptedLLM(), memory=False)
    ctx = make_ctx()
    brief = desk.build_brief(ctx, SYM)
    flow_seat = next(s for s in ANALYST_SEATS if s.key == "flow")
    sliced = TradingDesk._slice(brief, flow_seat)
    assert "가격" in sliced and "유동성" in sliced
    assert "기술지표" not in sliced       # not this seat's job


def test_the_brief_names_strategy_equity_as_a_book_not_the_real_account():
    desk = TradingDesk(ScriptedLLM(), memory=False)
    brief = desk.build_brief(make_ctx(), SYM)
    portfolio = brief["포트폴리오"]
    assert portfolio["전략 장부 평가액"] > 0
    assert "총자산" not in portfolio, (
        "설정상 starting_cash를 증권사 실계좌 총자산처럼 모델에 보냅니다")


def test_deliberation_is_published_as_an_event():
    seen = []
    desk = TradingDesk(ScriptedLLM(), memory=False)
    ctx = make_ctx()
    ctx.bus.on(EventType.DELIBERATION, lambda e: seen.append(e))
    run_desk(desk, ctx)
    assert seen and seen[0].payload["action"] == "buy"
    assert seen[0].payload["voting_seats"] == 8


def test_cost_tripwire_stops_further_deliberation():
    desk = TradingDesk(ScriptedLLM(), cost_limit_usd=0.0001, cadence_bars=1, memory=False)
    ctx = make_ctx()
    run_desk(desk, ctx)                     # first run spends
    before = len(desk.history)
    ctx.clock.set(ctx.now + timedelta(days=1))
    asyncio.run(desk.update(ctx, {SYM.key: ctx.history(SYM, 1)[0]}))
    assert len(desk.history) == before      # tripwire held


# ── memory ───────────────────────────────────────────────────────────────
def test_memory_scores_a_call_against_realised_price():
    memory = DeskMemory()
    ctx = make_ctx()
    d = DeskDecision(symbol_key=SYM.key, ticker=SYM.ticker, decided_at=T0,
                     action="buy", conviction=0.8, price_at_decision=70_000.0,
                     horizon_bars=5, rationale="r", invalidation="i")
    memory.record(d)
    lessons = memory.settle(ctx)
    assert len(lessons) == 1
    assert lessons[0].correct is (ctx.price(SYM) > 70_000.0)
    assert memory.stats["scored"] == 1


def test_memory_ignores_holds():
    memory = DeskMemory()
    memory.record(DeskDecision(symbol_key=SYM.key, ticker=SYM.ticker, decided_at=T0,
                               action="hold", conviction=0.5, price_at_decision=70_000.0))
    assert memory.stats["pending"] == 0


def test_lessons_surface_a_miscalibrated_desk():
    memory = DeskMemory()
    ctx = make_ctx()
    for i in range(6):
        memory.record(DeskDecision(
            symbol_key=SYM.key, ticker=SYM.ticker, decided_at=T0 + timedelta(days=i),
            action="sell", conviction=0.85, price_at_decision=70_000.0, horizon_bars=1,
            rationale="scripted"))
    memory.settle(ctx)          # prices rose, so every 'sell' was wrong
    text = memory.lessons_for(SYM)
    assert "캘리브레이션" in text
    assert "낮춰" in text


# ── 출력 잘림 ────────────────────────────────────────────────────────────
def test_a_truncated_response_retries_with_a_bigger_budget():
    """잘린 응답을 같은 예산으로 재시도하면 똑같이 잘립니다.

    실제로 겪은 실패입니다. microstructure 좌석이 max_tokens 에서 잘려 JSON 이
    깨졌고, 클라이언트는 "model did not return JSON" 으로만 보고한 뒤 동일한
    요청을 세 번 반복했습니다 — 비용은 3배, 좌석은 그대로 실패.
    """
    import httpx

    from quant.alpha.llm_client import LLMClient, LLMConfig

    budgets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        budget = json.loads(request.content)["generationConfig"]["maxOutputTokens"]
        budgets.append(budget)
        if budget < 400:                      # 예산이 작으면 잘린 채로 돌려준다
            return httpx.Response(200, json={
                "candidates": [{"finishReason": "MAX_TOKENS",
                                "content": {"parts": [{"text": '{"stance": "bul'}]}}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}})
        return httpx.Response(200, json={
            "candidates": [{"finishReason": "STOP",
                            "content": {"parts": [{"text": '{"stance": "bullish"}'}]}}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 20}})

    client = LLMClient(LLMConfig(provider="google", model="gemini-3.7-flash",
                                 api_key="test-key", max_tokens=200))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    assert asyncio.run(client.complete("sys", "user", {"type": "object"})) == {
        "stance": "bullish"}
    # 같은 예산으로 반복하지 않고 키워서 재시도했는가
    assert budgets == [200, 400], budgets


def test_truncation_gives_up_rather_than_growing_without_bound():
    """4배까지 키워도 안 끝나면 포기합니다. 8배도 안 끝날 테니까요."""
    import httpx

    from quant.alpha.llm_client import LLMClient, LLMConfig, Truncated

    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["generationConfig"]["maxOutputTokens"])
        return httpx.Response(200, json={
            "candidates": [{"finishReason": "MAX_TOKENS",
                            "content": {"parts": [{"text": "{"}]}}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}})

    client = LLMClient(LLMConfig(provider="google", model="gemini-3.7-flash",
                                 api_key="test-key", max_tokens=100, max_retries=9))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(Truncated):
        asyncio.run(client.complete("sys", "user", {"type": "object"}))
    assert seen == [100, 200, 400], seen


def test_cost_is_priced_per_model_not_at_a_blended_rate():
    """싼 모델을 비싼 요율로 매기면 cost_limit_usd 가 6배 일찍 멈춥니다."""
    from quant.alpha.llm_client import LLMUsage

    flash = LLMUsage(53_289, 6_571, 19, "gemini-3.7-flash")
    opus = LLMUsage(53_289, 6_571, 19, "claude-opus-5")
    assert round(flash.cost_usd, 4) == 0.0646
    assert round(opus.cost_usd, 4) == 0.4307
    # 모르는 모델은 가장 비싼 요율로 — 한도가 과소계상되는 쪽이 더 나쁩니다.
    assert LLMUsage(53_289, 6_571, 19, "some-new-model").cost_usd == opus.cost_usd
