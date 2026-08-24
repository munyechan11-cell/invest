"""실시간 16인 트레이딩 데스크 — a real-time deliberating agent desk.

Sixteen seats argue to a decision on every cycle. The roster and every seat's
instructions live in `seats.py`; this module is the machinery that runs them.

    ┌ 분석 8석 (동시) ────────────────────────────────────────────────┐
    │ 기술적 · 수급 · 펀더멘털 · 뉴스 · 센티먼트 · 매크로 · 미시구조 · 퀀트 │
    └────────────────────────────────┬───────────────────────────────┘
                                     ▼
    ┌ 토론 2석 (N라운드) ─────────────────────────────────────────────┐
    │ 강세론자  ⚔  약세론자                                            │
    └────────────────────────────────┬───────────────────────────────┘
                                     ▼
    ┌ 리스크 3석 ────────────────────────────────────────────────────┐
    │ 공격형 ⇄ 보수형  →  중립형이 포지션 배율로 수렴 (또는 거부)        │
    └────────────────────────────────┬───────────────────────────────┘
                                     ▼
    ┌ 결정 3석 ──────────────────────────────────────────────────────┐
    │ 리서치 매니저 → 트레이더(체결 계획) → 데스크 헤드(최종 결정)       │
    └────────────────────────────────────────────────────────────────┘

The structure is TradingAgents'; the fact that it produces an `Insight` — and
therefore stays under the rule-based risk models, the protections and the
brokerage guard rails — is LEAN's and Freqtrade's. A confident-sounding
language model has no more authority here than a moving average does.

Three constraints shape the implementation:

* **Latency.** Real time means the deliberation must finish inside one bar.
  There is a hard deadline, and when it is missed the desk degrades to the
  analyst consensus rather than emitting a stale decision late.
* **Cost.** A full sixteen-seat round is ~19 model calls. It runs on a cadence,
  over a shortlist, with per-bar caching and a spend tripwire.
* **Honesty.** Disabled in backtests by default: a model's training data
  postdates every historical bar, so its "predictions" there are hindsight.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import statistics
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from quant.alpha.base import AlphaModel
from quant.alpha.llm_client import (
    CallMeter,
    LLMClient,
    LLMConfig,
    LLMError,
    QuotaExhausted,
    metered,
)
from quant.alpha.seats import (
    BEAR_SEAT,
    BULL_SEAT,
    HEAD_SEAT,
    RESEARCH_MANAGER_SEAT,
    RISK_SEATS,
    SPRITE_NAMES,
    TRADER_SEAT,
    Seat,
    roster,
)
from quant.core.aio import LazySemaphore
from quant.core.context import Context
from quant.core.events import EventType
from quant.core.types import Bar, Direction, Insight, RunMode, Symbol, periods_per_year
from quant.data.flow import FlowFeed
from quant.indicators.streaming import (
    ADX,
    ATR,
    MACD,
    RSI,
    SMA,
    BollingerBands,
    IndicatorSet,
    RollingReturn,
    RollingVolatility,
)

log = logging.getLogger("quant.alpha.desk")


def _as_client(llm):
    """Accept an `LLMConfig`, an `LLMClient`, or anything with `.complete()`.

    Binding the desk to one concrete client class buys nothing and costs the
    ability to plug in a different backend — or a scripted stand-in in tests.
    The only thing the desk actually needs is `await complete(system, user,
    schema)` and a `.usage` counter.
    """
    if llm is None:
        return None
    if hasattr(llm, "complete"):
        return llm
    return LLMClient(llm)


ACTION_TO_DIRECTION = {
    "strong_buy": (Direction.UP, 1.00),
    "buy": (Direction.UP, 0.75),
    "hold": (Direction.FLAT, 0.0),
    "reduce": (Direction.FLAT, 0.0),
    "sell": (Direction.DOWN, 0.75),
    "strong_sell": (Direction.DOWN, 1.00),
}
STANCE_SCORE = {"bullish": 1, "neutral": 0, "bearish": -1}


# ─────────────────────────────────────────────────────────────────────────────
# Decision record
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class DeskDecision:
    symbol_key: str
    ticker: str
    decided_at: datetime
    action: str
    conviction: float
    price_at_decision: float = 0.0
    #: The benchmark this call will be scored against, and its price at the
    #: moment of the call. Stamped on the row rather than read at settle time:
    #: switching the configured benchmark mid-run would otherwise silently
    #: re-base every open call against an index it was never measured on.
    benchmark_key: str = ""
    benchmark_price: float = 0.0
    target_weight_pct: float = 0.0
    expected_move_pct: float = 0.0
    horizon_bars: int = 12
    rationale: str = ""
    invalidation: str = ""
    entry_note: str = ""
    dissent: str = ""
    position_scale: float = 1.0
    max_loss_pct: float = 0.0
    vetoed: bool = False
    veto_reason: str = ""
    analysts: dict = field(default_factory=dict)
    debate: dict = field(default_factory=dict)
    risk_debate: dict = field(default_factory=dict)
    risk: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)
    trade: dict = field(default_factory=dict)
    brief: dict = field(default_factory=dict)
    lessons_used: str = ""
    elapsed_s: float = 0.0
    llm_calls: int = 0
    degraded: str = ""

    @property
    def consensus(self) -> float:
        """Analyst agreement in [-1, 1], conviction-weighted.

        Seats that flagged `data_sufficient=false` are excluded rather than
        counted as neutral — a seat with no data has no vote, and letting it
        dilute the average would quietly bias every decision toward hold.
        """
        score = weight = 0.0
        for report in self.analysts.values():
            if not isinstance(report, dict) or not report.get("data_sufficient", True):
                continue
            c = float(report.get("conviction") or 0)
            score += STANCE_SCORE.get(report.get("stance"), 0) * c
            weight += c
        return score / weight if weight > 0 else 0.0

    @property
    def voting_seats(self) -> int:
        return sum(1 for r in self.analysts.values()
                   if isinstance(r, dict) and r.get("data_sufficient", True))

    @property
    def dissent_ratio(self) -> float:
        """Fraction of voting analysts on the opposite side of the head's call."""
        direction, _ = ACTION_TO_DIRECTION.get(self.action, (Direction.FLAT, 0))
        if direction is Direction.FLAT:
            return 0.0
        want = "bullish" if direction is Direction.UP else "bearish"
        usable = [r for r in self.analysts.values()
                  if isinstance(r, dict) and r.get("data_sufficient", True)]
        if not usable:
            return 1.0
        against = sum(1 for r in usable if r.get("stance") not in (want, "neutral"))
        return against / len(usable)

    def to_dict(self) -> dict:
        return {
            "symbol": self.ticker,
            "symbol_key": self.symbol_key,
            "decided_at": self.decided_at.isoformat(),
            "action": self.action,
            "conviction": round(self.conviction, 3),
            "price_at_decision": self.price_at_decision,
            "benchmark_key": self.benchmark_key,
            "benchmark_price": self.benchmark_price,
            "target_weight_pct": round(self.target_weight_pct, 2),
            "expected_move_pct": round(self.expected_move_pct, 2),
            "horizon_bars": self.horizon_bars,
            "rationale": self.rationale,
            "invalidation": self.invalidation,
            "entry_note": self.entry_note,
            "dissent": self.dissent,
            "position_scale": round(self.position_scale, 3),
            "max_loss_pct": round(self.max_loss_pct, 2),
            "vetoed": self.vetoed,
            "veto_reason": self.veto_reason,
            "consensus": round(self.consensus, 3),
            "voting_seats": self.voting_seats,
            "dissent_ratio": round(self.dissent_ratio, 3),
            "analysts": self.analysts,
            "debate": self.debate,
            "risk_debate": self.risk_debate,
            "risk": self.risk,
            "plan": self.plan,
            "trade": self.trade,
            "brief": self.brief,
            "lessons_used": self.lessons_used,
            "elapsed_s": round(self.elapsed_s, 2),
            "llm_calls": self.llm_calls,
            "degraded": self.degraded,
        }

    def summary_line(self) -> str:
        mark = "⛔" if self.vetoed else {
            "strong_buy": "🟢🟢", "buy": "🟢", "hold": "⚪", "reduce": "🟠",
            "sell": "🔴", "strong_sell": "🔴🔴"}.get(self.action, "⚪")
        return (f"{mark} {self.ticker} {self.action} conv={self.conviction:.2f} "
                f"scale={self.position_scale:.2f} consensus={self.consensus:+.2f} "
                f"({self.voting_seats}석 투표, {self.elapsed_s:.1f}s, {self.llm_calls} calls)")


# ─────────────────────────────────────────────────────────────────────────────
# Memory / reflection (TradingAgents' loop, scored against realised prices)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Lesson:
    ticker: str
    decided_at: datetime
    action: str
    conviction: float
    entry_price: float
    realised_pct: float
    correct: bool
    rationale: str
    invalidation: str
    #: What the benchmark did over the same window, and the excess left after
    #: subtracting it. `benchmark_key` is empty when the call was scored raw —
    #: no benchmark configured, or a different one than the call was stamped
    #: with — so a reader can tell "beat the index" from "the tape went up".
    benchmark_pct: float = 0.0
    excess_pct: float = 0.0
    benchmark_key: str = ""
    settled_at: datetime | None = None
    symbol_key: str = ""


class DeskMemory:
    """Remembers past calls and scores them once their horizon has elapsed.

    Without this the desk makes the same mistake indefinitely: nothing in a
    stateless prompt chain can notice that "외국인 순매수 연속" has been wrong
    six times running on this particular name. Scoring is done against realised
    price, not against the desk's own later opinion, so the feedback is real —
    and against the benchmark where the call recorded one, because in a rising
    KOSPI every long call looks right and a hit rate that measures the index
    teaches the desk nothing about itself.

    The journal is durable once `bind_store` has been called: the calibration
    line needs four settled calls at conviction ≥ 0.7, which on daily bars with
    a four-name shortlist is weeks of trading — longer than this process is
    likely to live.
    """

    def __init__(self, capacity: int = 400, lookback_lessons: int = 6,
                 settle_grace_bars: int = 5):
        self.capacity = capacity
        self.lookback = lookback_lessons
        #: How long a due call is held when it cannot be priced yet. A late or
        #: stalled feed is the common case and resolves itself; see `settle`.
        self.grace_bars = max(settle_grace_bars, 0)
        #: Calls that could never be scored. Kept and reported rather than
        #: forgotten: it is the only number that shows the sample is
        #: conditioned on the names that stayed priceable.
        self.unscored = 0
        self._pending: list[DeskDecision] = []
        self._lessons: list[Lesson] = []
        #: durable store, once one is bound — see `bind_store`.
        self._store = None

    def record(self, decision: DeskDecision) -> None:
        if decision.action == "hold" or decision.price_at_decision <= 0:
            return
        self._pending.append(decision)
        if len(self._pending) > self.capacity:
            dropped = len(self._pending) // 2
            # These are calls the desk will now never grade. Counting them
            # keeps the hit rate honest about what it is a hit rate *of*.
            self.unscored += dropped
            del self._pending[:dropped]
        self._persist()

    def settle(self, ctx: Context) -> list[Lesson]:
        """Score every pending call whose horizon has now passed."""
        settled: list[Lesson] = []
        still_open: list[DeskDecision] = []
        dropped = 0
        for d in self._pending:
            due = d.decided_at + ctx.bar_delta * max(d.horizon_bars, 1)
            if ctx.now < due:
                still_open.append(d)
                continue
            price = self._mark(ctx, d.symbol_key)
            if price <= 0 or d.price_at_decision <= 0:
                # A call on a name that had left the universe used to be
                # discarded here in silence, which conditions the sample on
                # survival — and the names that stop being priced are not a
                # random half. Hold it while the feed might still catch up,
                # then drop it loudly and count it.
                if d.price_at_decision > 0 and ctx.now < due + ctx.bar_delta * self.grace_bars:
                    still_open.append(d)
                    continue
                dropped += 1
                log.warning("데스크 회고 불가: %s %s (%s 판단) — 가격을 구할 수 없어 "
                            "채점에서 제외합니다", d.ticker, d.action, d.decided_at.date())
                continue
            realised = price / d.price_at_decision - 1.0
            bench_move, bench_key = self._benchmark_move(ctx, d)
            excess = realised - bench_move
            direction, _ = ACTION_TO_DIRECTION.get(d.action, (Direction.FLAT, 0))
            correct = (excess > 0) if direction is Direction.UP else (excess < 0)
            lesson = Lesson(
                ticker=d.ticker, decided_at=d.decided_at, action=d.action,
                conviction=d.conviction, entry_price=d.price_at_decision,
                realised_pct=realised * 100, correct=correct,
                rationale=d.rationale[:220], invalidation=d.invalidation[:160],
                benchmark_pct=bench_move * 100, excess_pct=excess * 100,
                benchmark_key=bench_key, settled_at=ctx.now, symbol_key=d.symbol_key,
            )
            self._lessons.append(lesson)
            settled.append(lesson)
        self._pending = still_open
        if len(self._lessons) > self.capacity:
            del self._lessons[: len(self._lessons) // 2]
        self.unscored += dropped
        if settled or dropped:
            self._persist()
        return settled

    @staticmethod
    def _mark(ctx: Context, symbol_key: str) -> float:
        """Last price for a symbol key, in or out of the current universe."""
        symbol = next((s for s in ctx.universe if s.key == symbol_key), None)
        if symbol is None:
            venue, _, ticker = symbol_key.partition(":")
            if not ticker:
                return 0.0
            # Quotes and bar history are keyed by `key` alone, so a stand-in
            # carrying the same key prices a name the universe has since
            # dropped off the history the context still holds.
            symbol = Symbol(ticker=ticker, venue=venue)
        return ctx.price(symbol)

    @staticmethod
    def _benchmark_move(ctx: Context, d: DeskDecision) -> tuple[float, str]:
        """The benchmark's move over the call's window, and which benchmark.

        Returns `(0.0, "")` when the call must be scored raw. Refuses to mix
        instruments: if the configured benchmark has changed since the call was
        made, the stored entry price belongs to a different index, and
        subtracting a number that means nothing is worse than subtracting none.
        """
        bench = ctx.benchmark
        if bench is None or not d.benchmark_key or d.benchmark_price <= 0:
            return 0.0, ""
        if bench.key != d.benchmark_key:
            log.warning("벤치마크가 %s → %s 로 바뀌어 %s 판단은 원수익률로 채점합니다",
                        d.benchmark_key, bench.key, d.ticker)
            return 0.0, ""
        now = ctx.price(bench)
        if now <= 0:
            return 0.0, ""
        return now / d.benchmark_price - 1.0, d.benchmark_key

    def lessons_for(self, symbol: Symbol) -> str:
        """Prose the head of desk reads before deciding.

        Same-ticker history first (most relevant), then cross-ticker calibration
        — because "you were 2 for 9 at conviction ≥ 0.7" is a fact about the
        desk, not about the name.
        """
        same = [line for line in self._lessons if line.ticker == symbol.ticker][-self.lookback:]
        lines: list[str] = []
        if same:
            lines.append(f"[{symbol.ticker} 과거 판단 결과]")
            for line in same:
                mark = "적중" if line.correct else "실패"
                versus = (f" (벤치 {line.benchmark_pct:+.2f}%, 초과 {line.excess_pct:+.2f}%)"
                          if line.benchmark_key else "")
                lines.append(
                    f"- {line.decided_at:%Y-%m-%d} {line.action} (확신 {line.conviction:.2f}) "
                    f"→ {line.realised_pct:+.2f}%{versus} {mark} · 근거: {line.rationale[:120]}"
                )
        confident = [line for line in self._lessons if line.conviction >= 0.7]
        if len(confident) >= 4:
            hit = sum(1 for line in confident if line.correct) / len(confident)
            lines.append(
                f"[캘리브레이션] 확신 0.7 이상 판단 {len(confident)}건 중 적중률 {hit:.0%}. "
                + ("확신도가 과대평가되어 있으니 낮춰 잡아라." if hit < 0.6 else
                   "확신도 보정은 대체로 맞았다.")
            )
        return "\n".join(lines)

    @property
    def stats(self) -> dict:
        if not self._lessons:
            return {"scored": 0, "pending": len(self._pending),
                    "unscored": self.unscored}
        hits = sum(1 for x in self._lessons if x.correct)
        return {
            "scored": len(self._lessons),
            "pending": len(self._pending),
            "unscored": self.unscored,
            "hit_rate": round(hits / len(self._lessons), 3),
            "avg_realised_pct": round(
                statistics.fmean([x.realised_pct for x in self._lessons]), 3),
            "avg_excess_pct": round(
                statistics.fmean([x.excess_pct for x in self._lessons]), 3),
        }

    # ── durability ───────────────────────────────────────────────────────
    def bind_store(self, store) -> None:
        """Write the journal through to `store` on every change.

        Binding rather than snapshotting on a timer, for the same reason the
        daily budget binds: the desk decides once per cadence and the process
        can die at any point in between, and a journal that is one cycle behind
        loses exactly the call that was in flight when it died.
        """
        self._store = store

    def _persist(self) -> None:
        if self._store is None:
            return
        try:
            self._store.save_desk_memory(self)
        except Exception:
            # A bookkeeping failure must never stop the trading path — but say
            # so loudly: from here the journal only lasts until the restart.
            self._store = None
            log.exception("데스크 회고 저장 실패 — 재시작하면 판단 이력이 초기화됩니다")

    def to_state(self) -> dict:
        """The journal as plain rows, for the state DB."""
        return {
            "pending": [{
                "symbol_key": d.symbol_key, "ticker": d.ticker,
                "decided_at": d.decided_at.isoformat(), "action": d.action,
                "conviction": d.conviction,
                "price_at_decision": d.price_at_decision,
                "horizon_bars": d.horizon_bars,
                "benchmark_key": d.benchmark_key,
                "benchmark_price": d.benchmark_price,
                "rationale": d.rationale, "invalidation": d.invalidation,
            } for d in self._pending],
            "lessons": [{
                "symbol_key": x.symbol_key, "ticker": x.ticker,
                "decided_at": x.decided_at.isoformat(),
                "settled_at": x.settled_at.isoformat() if x.settled_at else "",
                "action": x.action, "conviction": x.conviction,
                "entry_price": x.entry_price, "realised_pct": x.realised_pct,
                "benchmark_pct": x.benchmark_pct, "excess_pct": x.excess_pct,
                "benchmark_key": x.benchmark_key, "correct": int(x.correct),
                "rationale": x.rationale, "invalidation": x.invalidation,
            } for x in self._lessons],
            "unscored": self.unscored,
        }

    def load_state(self, state: dict) -> int:
        """Rebuild the journal from stored rows. Returns how many came back.

        A row this build cannot parse is skipped, not raised: the journal is a
        learning aid, and refusing to start the bot over one malformed line
        would be a far worse failure than losing the line.
        """
        pending: list[DeskDecision] = []
        for row in state.get("pending") or []:
            try:
                pending.append(DeskDecision(
                    symbol_key=row["symbol_key"], ticker=row["ticker"],
                    decided_at=datetime.fromisoformat(row["decided_at"]),
                    action=row["action"], conviction=float(row["conviction"] or 0.0),
                    price_at_decision=float(row["price_at_decision"] or 0.0),
                    horizon_bars=int(row["horizon_bars"] or 1),
                    benchmark_key=row["benchmark_key"] or "",
                    benchmark_price=float(row["benchmark_price"] or 0.0),
                    rationale=row["rationale"] or "",
                    invalidation=row["invalidation"] or "",
                ))
            except (KeyError, TypeError, ValueError):
                log.warning("데스크 대기 판단 한 건을 읽지 못해 건너뜁니다", exc_info=True)
        lessons: list[Lesson] = []
        for row in state.get("lessons") or []:
            try:
                settled_at = row.get("settled_at") or ""
                lessons.append(Lesson(
                    ticker=row["ticker"],
                    decided_at=datetime.fromisoformat(row["decided_at"]),
                    action=row["action"], conviction=float(row["conviction"] or 0.0),
                    entry_price=float(row["entry_price"] or 0.0),
                    realised_pct=float(row["realised_pct"] or 0.0),
                    correct=bool(row["correct"]),
                    rationale=row["rationale"] or "",
                    invalidation=row["invalidation"] or "",
                    benchmark_pct=float(row["benchmark_pct"] or 0.0),
                    excess_pct=float(row["excess_pct"] or 0.0),
                    benchmark_key=row["benchmark_key"] or "",
                    settled_at=datetime.fromisoformat(settled_at) if settled_at else None,
                    symbol_key=row.get("symbol_key") or "",
                ))
            except (KeyError, TypeError, ValueError):
                log.warning("데스크 회고 한 건을 읽지 못해 건너뜁니다", exc_info=True)
        self._pending = pending[-self.capacity:]
        self._lessons = lessons[-self.capacity:]
        self.unscored = int(state.get("unscored") or 0)
        return len(self._pending) + len(self._lessons)


ContextSource = Callable[[Symbol, datetime], Awaitable[str]]


# ─────────────────────────────────────────────────────────────────────────────
# The desk
# ─────────────────────────────────────────────────────────────────────────────
class TradingDesk(AlphaModel):
    name = "desk"

    def __init__(
        self,
        llm: LLMConfig | LLMClient,
        *,
        flow_feed: FlowFeed | None = None,
        decision_llm: LLMConfig | LLMClient | None = None,
        cadence_bars: int = 1,
        max_symbols_per_run: int = 4,
        debate_rounds: int = 2,
        risk_debate_rounds: int = 1,
        min_conviction: float = 0.55,
        max_dissent_ratio: float = 0.6,
        default_horizon_bars: int = 12,
        deadline_s: float = 120.0,
        allow_in_backtest: bool = False,
        allow_short: bool = False,
        context_source: ContextSource | None = None,
        shortlist: Callable[[Context, dict[str, Bar]], list[Symbol]] | None = None,
        seats: Sequence[str] | None = None,
        concurrency: int = 8,
        cost_limit_usd: float = 0.0,
        memory: bool = True,
    ):
        self.client = _as_client(llm)
        # The judging seats decide real money; they are worth the stronger model.
        self.decision_client = _as_client(decision_llm) or self.client
        self.flow_feed = flow_feed
        self.cadence = max(cadence_bars, 1)
        self.max_symbols = max_symbols_per_run
        self.debate_rounds = max(debate_rounds, 1)
        self.risk_debate_rounds = max(risk_debate_rounds, 1)
        self.min_conviction = min_conviction
        self.max_dissent_ratio = max_dissent_ratio
        self.default_horizon = default_horizon_bars
        self.deadline_s = deadline_s
        self.allow_in_backtest = allow_in_backtest
        self.allow_short = allow_short
        self.context_source = context_source
        self.shortlist_fn = shortlist
        self.cost_limit_usd = cost_limit_usd

        self.seats: list[Seat] = roster(seats)
        self.analyst_seats = [s for s in self.seats if s.is_analyst]
        self.memory = DeskMemory() if memory else None

        self._sem = LazySemaphore(concurrency)
        self._bar_count = 0
        self._cache: dict[str, DeskDecision] = {}
        self.decisions: dict[str, DeskDecision] = {}
        self.history: list[DeskDecision] = []
        self.warmup_bars = 210
        self._sets: dict[str, IndicatorSet] = {}
        self._disabled = ""
        #: `quant.live.spend.SpendMeter` — `allow()` 와 `record()` 만 씁니다.
        #: 타입으로 못 박지 않는 이유: 알파 계층이 라이브 계층을 import 하기
        #: 시작하면, 백테스트와 CLI 가 쓰는 이 파일이 그쪽 의존성에 묶입니다.
        self.meter = None
        self._capped = ""

    # ── 누가 내는가 ───────────────────────────────────────────────────────
    def bind_meter(self, meter) -> None:
        """이 데스크의 지출을 어느 계정에 달 것인가.

        데스크는 계정 없이도 돕니다 — CLI 로 자기 컴퓨터에서 돌리면 키도 비용도
        본인 것이라 잴 이유가 없습니다. 그래서 기본값은 묶이지 않음이고, 그때
        `update()` 는 지금까지와 똑같이 동작합니다.

        웹에서 뜬 봇만 `LiveTrader` 가 여기에 그 사람 계량기를 묶습니다. 이 한
        줄이 없으면 봉마다 도는 심의가 요금제 상한도 원장도 통과하지 않고 —
        사용량을 재는 자리가 사용자가 직접 누르는 `/api/evaluate` 와 개장 전
        심의 한 번뿐이었습니다. 정작 돈이 나가는 쪽은 사람이 자는 동안 봉마다
        16석을 돌리는 봇입니다.
        """
        self.meter = meter

    # ── lifecycle ────────────────────────────────────────────────────────
    async def on_start(self, ctx: Context) -> None:
        if ctx.run_mode is RunMode.BACKTEST and not self.allow_in_backtest:
            self._disabled = (
                "데스크는 백테스트에서 기본 비활성화됩니다: LLM의 학습 데이터가 과거 봉 이후를 "
                "이미 알고 있어 '예측'이 사후확신이 됩니다. 필요하면 allow_in_backtest=True 와 "
                "시점 기준 context_source를 함께 지정하세요."
            )
            log.warning(self._disabled)
            return
        if ctx.run_mode is RunMode.BACKTEST:
            log.warning("데스크가 백테스트에서 활성화됨 — 결과는 낙관 편향이며 엣지의 증거가 아닙니다.")
        # Preflight. Without it an unusable key or an empty credit balance fails
        # all nineteen calls on every single bar — the desk looks like it is
        # "degrading" when in fact it can never work, and each bar pays the full
        # deadline in latency before saying so.
        probe = await self._preflight()
        if probe:
            self._disabled = probe
            log.error("데스크 비활성화: %s", probe)
            return

        log.info(
            "트레이딩 데스크 가동 · 총 %d석 (분석 %d + 토론 2 + 리스크 3 + 결정 3) · "
            "토론 %d라운드 · 마감 %.0fs",
            len(self.seats), len(self.analyst_seats), self.debate_rounds, self.deadline_s,
        )
        log.info("분석 좌석: %s", ", ".join(s.title_ko for s in self.analyst_seats))

    async def _list_models(self) -> list[str]:
        """Ask the provider what it actually serves.

        A 404 that only says "not found" leaves the operator guessing at a
        model name; one that lists the real options is a one-line fix.
        """
        lister = getattr(self.client, "list_models", None)
        if lister is None:
            return []
        try:
            return await lister()
        except Exception:
            return []

    async def _preflight(self) -> str:
        """One cheap call. Returns a reason to stay disabled, or "" when healthy."""
        try:
            await asyncio.wait_for(
                self.client.complete("Reply with the single word OK.", "ping", None),
                timeout=min(self.deadline_s, 45.0),
            )
        except asyncio.TimeoutError:
            return "LLM 응답이 없습니다 (사전 점검 시간 초과) — 네트워크나 모델 설정을 확인하세요"
        except LLMError as exc:
            message = str(exc)
            if "credit balance" in message or "quota" in message.lower():
                return (f"Anthropic 계정 크레딧이 부족합니다. "
                        f"console.anthropic.com 의 Plans & Billing 에서 충전한 뒤 "
                        f"다시 시작하세요. (원문: {message[:160]})")
            if " 401:" in message or " 403:" in message:
                return f"API 키가 거부되었습니다 — 키를 확인하세요. ({message[:160]})"
            if " 404:" in message:
                available = await self._list_models()
                hint = f" 사용 가능: {', '.join(available[:8])}" if available else ""
                return (f"모델을 찾을 수 없습니다 — 모델 이름을 확인하세요."
                        f"{hint} ({message[:120]})")
            return f"LLM 사전 점검 실패: {message[:200]}"
        except Exception as exc:                      # pragma: no cover - defensive
            return f"LLM 사전 점검 실패: {type(exc).__name__}: {exc}"
        return ""

    # ── the shared brief ─────────────────────────────────────────────────
    def _indicators(self, ctx: Context, symbol: Symbol) -> IndicatorSet:
        iset = self._sets.get(symbol.key)
        if iset is None:
            iset = IndicatorSet(
                rsi=RSI(14), sma20=SMA(20), sma50=SMA(50), sma200=SMA(200),
                atr=ATR(14), macd=MACD(), adx=ADX(14), bb=BollingerBands(20, 2.0),
                ret5=RollingReturn(5), ret20=RollingReturn(20), ret60=RollingReturn(60),
                vol=RollingVolatility(20, periods_per_year(ctx.timeframe)),
            )
            history = ctx.history(symbol)
            if history:
                iset.prime(history[:-1])   # the newest bar arrives via update()
            self._sets[symbol.key] = iset
        return iset

    def build_brief(self, ctx: Context, symbol: Symbol) -> dict:
        """Deterministic facts, in sections. Every seat argues from the same numbers."""
        bars = ctx.history(symbol, 260)
        if not bars:
            return {}
        ind = self._indicators(ctx, symbol)
        last = bars[-1]
        closes = [b.close for b in bars]
        volumes = [b.volume for b in bars]
        avg_vol = statistics.fmean(volumes[-20:]) if len(volumes) >= 20 else 0.0
        quote = ctx.quote(symbol)
        pos = ctx.portfolio.positions.get(symbol.key)
        locked, lock_reason = ctx.is_locked(symbol)
        atr_pct = ind.atr.percent_of(last.close)

        brief: dict = {
            "종목": symbol.ticker,
            "거래소": symbol.venue,
            "통화": symbol.quote_currency,
            "기준시각": ctx.now.isoformat(),
            "봉주기": ctx.timeframe,
            "가격": {
                "종가": round(last.close, 4),
                "고가": round(last.high, 4),
                "저가": round(last.low, 4),
                "5봉수익률%": round((ind.ret5.value or 0) * 100, 2),
                "20봉수익률%": round((ind.ret20.value or 0) * 100, 2),
                "60봉수익률%": round((ind.ret60.value or 0) * 100, 2),
                "52주고점대비%": round((last.close / max(closes[-252:]) - 1) * 100, 2),
                "52주저점대비%": round((last.close / min(closes[-252:]) - 1) * 100, 2),
            },
            "기술지표": {
                "RSI14": round(ind.rsi.value, 1) if ind.rsi.value else None,
                "SMA20": round(ind.sma20.value, 4) if ind.sma20.value else None,
                "SMA50": round(ind.sma50.value, 4) if ind.sma50.value else None,
                "SMA200": round(ind.sma200.value, 4) if ind.sma200.value else None,
                "200일선위": bool(ind.sma200.value and last.close > ind.sma200.value),
                "MACD히스토그램": round(ind.macd.histogram, 6) if ind.macd.histogram else None,
                "ADX14": round(ind.adx.value, 1) if ind.adx.value else None,
                "국면": ("추세" if (ind.adx.value or 0) >= 25
                        else "박스권" if (ind.adx.value or 0) <= 20 else "전환"),
                "볼린저%B": round(ind.bb.percent_b, 3) if ind.bb.percent_b is not None else None,
                "ATR비율%": round(atr_pct * 100, 2),
                "연환산변동성%": round((ind.vol.value or 0) * 100, 1),
            },
            "유동성": {
                "당일거래량": round(last.volume),
                "20일평균거래량": round(avg_vol),
                "거래량배수": round(last.volume / avg_vol, 2) if avg_vol > 0 else None,
                "호가스프레드%": round(quote.spread_pct * 100, 4) if quote else None,
            },
            "체결비용": {
                "최소주문단위": float(symbol.lot_size),
                "호가단위": float(symbol.tick_size),
                "최소주문금액": float(symbol.min_notional),
                "왕복비용추정%": round(
                    ((quote.spread_pct if quote else 0.001) * 2 + 0.0005) * 100, 3),
                "1%포지션의거래량비중%": round(
                    (ctx.equity * 0.01 / last.close) / avg_vol * 100, 3
                ) if avg_vol > 0 and last.close > 0 else None,
            },
            "포트폴리오": {
                "총자산": round(ctx.equity, 2),
                "보유종목수": len(ctx.portfolio.open_positions),
                "현금비중%": round(ctx.portfolio.cash / max(ctx.equity, 1e-9) * 100, 1),
                "현재낙폭%": round(ctx.portfolio.drawdown * 100, 2),
                "이종목보유": ({
                    "수량": float(pos.quantity),
                    "평단": round(pos.avg_price, 4),
                    "평가손익%": round(pos.unrealized_pct * 100, 2),
                } if pos and not pos.is_flat else None),
                "거래잠금": lock_reason if locked else None,
            },
            "통계": {
                "관측봉수": len(bars),
                "일별변동성%": round(atr_pct * 100, 2),
                "최근20봉승률%": round(
                    sum(1 for a, b in zip(closes[-21:], closes[-20:]) if b > a) / 20 * 100, 1
                ) if len(closes) >= 21 else None,
                "포트폴리오누적수익%": round(ctx.portfolio.total_return * 100, 2),
                "누적매매수": len(ctx.portfolio.closed_trades),
            },
        }

        if self.flow_feed is not None:
            summary = self.flow_feed.summary(symbol, ctx.now, 20)
            recent = self.flow_feed.get(symbol, ctx.now)[-5:]
            brief["수급"] = ({
                "요약20일": summary.to_dict() if summary else None,
                "최근5일": [f.to_dict() for f in recent],
            } if (summary or recent) else {
                "데이터없음": self.flow_feed.failures.get(
                    symbol.key, "수급 데이터 소스가 연결되지 않음")
            })
        return brief

    @staticmethod
    def _slice(brief: dict, seat: Seat) -> dict:
        """Show a seat only its own sections.

        Not just token thrift: a seat that cannot see the fundamentals cannot
        pretend to have an opinion about them, which is exactly the failure mode
        the prompts warn against.
        """
        base = {k: v for k, v in brief.items() if not isinstance(v, dict)}
        for section in seat.brief_sections:
            if section in brief:
                base[section] = brief[section]
        return base

    # ── seat execution ───────────────────────────────────────────────────
    async def _ask(self, seat: Seat, user: str) -> dict:
        client = self.decision_client if seat.tier == "decision" else self.client
        async with self._sem:
            return await client.complete(seat.system, user, seat.schema)

    async def _safe_ask(self, seat: Seat, user: str, fallback: dict) -> dict:
        try:
            return await self._ask(seat, user)
        except QuotaExhausted as exc:
            # Stop the whole desk. Letting the remaining fifteen seats each
            # retry an exhausted allowance turns a clear failure into ten
            # minutes of deadline burn and the same answer.
            self._disabled = (
                f"LLM 사용량이 소진되었습니다 — 데스크를 중단합니다. "
                f"16석 한 바퀴는 호출 약 {len(self.seats) + 3}회를 씁니다. "
                f"무료 티어라면 유료 전환이나 좌석 축소(seats 옵션)를 고려하세요. "
                f"({str(exc)[:150]})"
            )
            log.error(self._disabled)
            raise
        except LLMError as exc:
            log.warning("%s 좌석 실패: %s", seat.key, exc)
            return {**fallback, "error": str(exc)}

    async def _run_analyst(self, seat: Seat, brief: dict, external: str) -> dict:
        payload = json.dumps(self._slice(brief, seat), ensure_ascii=False, indent=2)
        user = f"결정론적으로 계산된 시장 브리프다. 이 숫자들을 사실로 간주하라.\n{payload}\n\n"
        if external:
            user += f"[{brief.get('기준시각')}] 시점 기준 외부 컨텍스트:\n{external}\n\n"
        user += (
            "당신 좌석의 관점에서만 판단하라. 다른 분석가의 영역은 침범하지 마라. "
            "브리프의 숫자를 인용해 근거를 대라. 데이터가 없으면 지어내지 말고 "
            "data_sufficient=false 로 답하라."
        )
        return await self._safe_ask(seat, user, {
            "stance": "neutral", "conviction": 0.0, "data_sufficient": False,
            "key_points": ["좌석 응답 실패"],
        })

    async def _run_stage(self, coro, remaining: float):
        """Run one stage, letting a quota exhaustion abort the deliberation."""
        return await asyncio.wait_for(coro, timeout=remaining)

    async def _run_debate(self, brief: dict, analysts: dict) -> dict:
        """Bull and bear argue for N rounds, each seeing the other's last turn."""
        transcript: list[str] = []
        rounds: list[dict] = []
        base = (
            f"브리프:\n{json.dumps(self._slice(brief, BULL_SEAT), ensure_ascii=False)}\n\n"
            f"분석가 리포트:\n{json.dumps(analysts, ensure_ascii=False)}\n\n"
        )
        for r in range(self.debate_rounds):
            entry: dict = {"round": r + 1}
            for seat in (BULL_SEAT, BEAR_SEAT):
                user = base
                if transcript:
                    user += "지금까지의 토론:\n" + "\n\n".join(transcript[-4:]) + "\n\n"
                user += (f"{r + 1}/{self.debate_rounds}라운드. {seat.title_ko}로서 논거를 펴고 "
                         "상대를 반박하되, 상대의 가장 강한 주장은 concession에 인정하라.")
                res = await self._safe_ask(seat, user, {"argument": "응답 실패", "conviction": 0.0})
                entry[seat.key] = res
                transcript.append(f"[{seat.title_ko} R{r + 1}] {res.get('argument', '')}")
            rounds.append(entry)
        return {"rounds": rounds, "transcript": transcript}

    async def _run_risk_panel(self, brief: dict, analysts: dict, debate: dict
                              ) -> tuple[dict, dict]:
        """Aggressive vs conservative, reconciled by the neutral seat."""
        aggressive, conservative, neutral = RISK_SEATS
        base = (
            f"브리프:\n{json.dumps(self._slice(brief, neutral), ensure_ascii=False)}\n\n"
            f"분석가 리포트:\n{json.dumps(analysts, ensure_ascii=False)}\n\n"
            f"강세/약세 토론:\n{json.dumps(debate.get('rounds', []), ensure_ascii=False)}\n\n"
        )
        history: list[str] = []
        rounds: list[dict] = []
        for r in range(self.risk_debate_rounds):
            prompts = base + (("리스크 토론:\n" + "\n\n".join(history[-2:]) + "\n\n")
                              if history else "")
            pair = await asyncio.gather(
                self._safe_ask(aggressive, prompts + f"{r + 1}라운드. 사이즈를 주장하라.",
                               {"argument": "응답 실패", "proposed_scale": 0.5}),
                self._safe_ask(conservative, prompts + f"{r + 1}라운드. 자본 보존을 주장하라.",
                               {"argument": "응답 실패", "proposed_scale": 0.3}),
            )
            rounds.append({"round": r + 1, "aggressive": pair[0], "conservative": pair[1]})
            history.append(f"[공격형] {pair[0].get('argument', '')}")
            history.append(f"[보수형] {pair[1].get('argument', '')}")

        verdict = await self._safe_ask(
            neutral,
            base + f"리스크 토론:\n{json.dumps(rounds, ensure_ascii=False)}\n\n"
                   "두 주장을 하나의 포지션 배율로 수렴시켜라. 거부는 이름 붙일 수 있는 위험이 "
                   "있을 때만.",
            # A missing risk seat must reduce size, never wave anything through.
            {"position_scale": 0.4, "veto": False,
             "reasoning": "리스크 좌석 응답 실패 — 안전을 위해 사이즈 축소"},
        )
        return {"rounds": rounds}, verdict

    async def _run_plan(self, brief: dict, analysts: dict, debate: dict) -> dict:
        user = (
            f"브리프:\n{json.dumps(self._slice(brief, RESEARCH_MANAGER_SEAT), ensure_ascii=False)}\n\n"
            f"분석가 리포트:\n{json.dumps(analysts, ensure_ascii=False)}\n\n"
            f"강세/약세 토론:\n{json.dumps(debate.get('rounds', []), ensure_ascii=False)}\n\n"
            "방향성 계획을 확정하라."
        )
        return await self._safe_ask(RESEARCH_MANAGER_SEAT, user, {
            "rating": "hold", "rationale": "리서치 매니저 응답 실패",
            "strategic_actions": "다음 사이클 재평가", "conviction": 0.0,
        })

    async def _run_trade(self, brief: dict, plan: dict, risk: dict) -> dict:
        user = (
            f"브리프:\n{json.dumps(self._slice(brief, TRADER_SEAT), ensure_ascii=False)}\n\n"
            f"리서치 계획:\n{json.dumps(plan, ensure_ascii=False)}\n\n"
            f"리스크 판정:\n{json.dumps(risk, ensure_ascii=False)}\n\n"
            "이 계획을 실행 가능한 주문으로 번역하라. 방향은 재논쟁하지 마라."
        )
        return await self._safe_ask(TRADER_SEAT, user, {
            "action": "hold", "entry_style": "market_now",
            "execution_note": "트레이더 좌석 응답 실패", "conviction": 0.0,
        })

    async def _run_head(self, brief: dict, analysts: dict, debate: dict,
                        risk_debate: dict, risk: dict, plan: dict, trade: dict,
                        lessons: str) -> dict:
        user = (
            f"브리프:\n{json.dumps(self._slice(brief, HEAD_SEAT), ensure_ascii=False)}\n\n"
            f"분석가 {len(analysts)}석:\n{json.dumps(analysts, ensure_ascii=False)}\n\n"
            f"강세/약세 토론:\n{json.dumps(debate.get('rounds', []), ensure_ascii=False)}\n\n"
            f"리스크 토론:\n{json.dumps(risk_debate.get('rounds', []), ensure_ascii=False)}\n\n"
            f"리스크 판정:\n{json.dumps(risk, ensure_ascii=False)}\n\n"
            f"리서치 계획:\n{json.dumps(plan, ensure_ascii=False)}\n\n"
            f"트레이더 실행안:\n{json.dumps(trade, ensure_ascii=False)}\n\n"
        )
        if lessons:
            user += f"과거 판단 결과:\n{lessons}\n\n"
        user += (
            f"기본 보유기간은 {self.default_horizon}봉이다(셋업이 다르게 말하면 바꿔라). "
            f"공매도는 {'허용' if self.allow_short else '불가'}다. "
            "결정하라. 무효화 조건은 반드시 관측 가능한 사건으로 적어라."
        )
        return await self._ask(HEAD_SEAT, user)

    # ── one full deliberation ────────────────────────────────────────────
    def _calls(self) -> int:
        n = self.client.usage.calls
        if self.decision_client is not self.client:
            n += self.decision_client.usage.calls
        return n

    async def deliberate(self, ctx: Context, symbol: Symbol) -> DeskDecision | None:
        # 이 심의가 쓴 호출만 셉니다. `_calls()` 의 before/after 로 재던 자리인데,
        # 한 사이클의 종목들이 `gather` 로 동시에 도는 이상 그 차분에는 형제
        # 종목의 호출이 통째로 섞입니다 — 화면의 "N calls" 가 종목 수만큼
        # 부풀어 보이던 이유입니다.
        with metered() as spend:
            return await self._deliberate(ctx, symbol, spend)

    async def _deliberate(self, ctx: Context, symbol: Symbol,
                          spend: CallMeter) -> DeskDecision | None:
        started = time.monotonic()
        brief = self.build_brief(ctx, symbol)
        if not brief:
            return None

        external = ""
        if self.context_source is not None:
            try:
                external = await asyncio.wait_for(
                    self.context_source(symbol, ctx.now), timeout=self.deadline_s * 0.15
                )
            except Exception as exc:
                log.warning("외부 컨텍스트 조회 실패 %s: %s", symbol.ticker, exc)

        remaining = lambda: max(self.deadline_s - (time.monotonic() - started), 0.1)  # noqa: E731
        degraded = ""

        try:
            reports = await asyncio.wait_for(
                asyncio.gather(*(self._run_analyst(s, brief, external)
                                 for s in self.analyst_seats)),
                timeout=remaining(),
            )
        except asyncio.TimeoutError:
            log.warning("%s: 분석 단계 마감 초과 — 이번 사이클 건너뜀", symbol.ticker)
            return None
        except QuotaExhausted:
            return None
        analysts = {s.key: r for s, r in zip(self.analyst_seats, reports)}

        debate: dict = {"rounds": [], "transcript": []}
        risk_debate: dict = {"rounds": []}
        risk: dict = {}
        plan: dict = {}
        trade: dict = {}
        lessons = self.memory.lessons_for(symbol) if self.memory else ""

        try:
            debate = await asyncio.wait_for(self._run_debate(brief, analysts),
                                            timeout=remaining())
            risk_debate, risk = await asyncio.wait_for(
                self._run_risk_panel(brief, analysts, debate), timeout=remaining())
            plan = await asyncio.wait_for(self._run_plan(brief, analysts, debate),
                                          timeout=remaining())
            trade = await asyncio.wait_for(self._run_trade(brief, plan, risk),
                                           timeout=remaining())
            head = await asyncio.wait_for(
                self._run_head(brief, analysts, debate, risk_debate, risk, plan,
                               trade, lessons),
                timeout=remaining())
        except QuotaExhausted:
            log.error("%s: 사용량 소진으로 심의 중단", symbol.ticker)
            return None
        except (asyncio.TimeoutError, LLMError) as exc:
            # Degrade to the analyst consensus rather than emit a late or
            # half-formed decision. In real time the deadline is part of the
            # contract, not a suggestion.
            degraded = f"마감 초과/오류로 분석가 합의 대체 ({type(exc).__name__})"
            log.warning("%s: %s", symbol.ticker, degraded)
            head, risk = self._consensus_fallback(analysts, risk)

        # Stamp the benchmark the call will be graded against, not just its
        # price: a benchmark that is configured but unpriced (no warm-up data)
        # would otherwise grade every call against a flat line pretending to be
        # an index. Unstamped means "score this one raw", and says so.
        bench = ctx.benchmark
        bench_price = ctx.price(bench) if bench is not None else 0.0

        return DeskDecision(
            symbol_key=symbol.key,
            ticker=symbol.ticker,
            decided_at=ctx.now,
            price_at_decision=ctx.price(symbol),
            benchmark_key=bench.key if bench is not None and bench_price > 0 else "",
            benchmark_price=bench_price,
            action=str(head.get("action", "hold")).lower(),
            conviction=float(head.get("conviction") or 0.0),
            target_weight_pct=float(head.get("target_weight_pct") or 0.0),
            expected_move_pct=float(head.get("expected_move_pct") or 0.0),
            horizon_bars=int(head.get("horizon_bars") or self.default_horizon),
            rationale=str(head.get("rationale", "")),
            invalidation=str(head.get("invalidation", "")),
            entry_note=str(head.get("entry_note", "")) or str(trade.get("execution_note", "")),
            dissent=str(head.get("dissent", "")),
            position_scale=max(0.0, min(1.0, float(risk.get("position_scale", 1.0) or 0.0))),
            max_loss_pct=float(risk.get("max_loss_pct") or 0.0),
            vetoed=bool(risk.get("veto")),
            veto_reason=str(risk.get("veto_reason", "")),
            analysts=analysts, debate=debate, risk_debate=risk_debate, risk=risk,
            plan=plan, trade=trade, brief=brief, lessons_used=lessons,
            elapsed_s=time.monotonic() - started,
            llm_calls=spend.calls,
            degraded=degraded,
        )

    def _consensus_fallback(self, analysts: dict, risk: dict) -> tuple[dict, dict]:
        """Deterministic decision from the analyst seats alone."""
        score = weight = 0.0
        for r in analysts.values():
            if not isinstance(r, dict) or not r.get("data_sufficient", True):
                continue
            c = float(r.get("conviction") or 0)
            score += STANCE_SCORE.get(r.get("stance"), 0) * c
            weight += c
        consensus = score / weight if weight > 0 else 0.0
        head = {
            "action": "buy" if consensus > 0.35 else "sell" if consensus < -0.35 else "hold",
            # capped, because no debate was held to test this view
            "conviction": min(abs(consensus), 0.7),
            "rationale": "토론·결정 단계가 마감 내 완료되지 않아 분석가 합의로 대체",
            "invalidation": "다음 사이클에서 완전한 심의가 완료되면 재평가",
            "horizon_bars": self.default_horizon,
        }
        risk = dict(risk) if risk else {
            "position_scale": 0.5, "veto": False, "reasoning": "축약 심의 — 사이즈 절반 제한"}
        risk["position_scale"] = min(float(risk.get("position_scale", 0.5) or 0.5), 0.5)
        return head, risk

    # ── AlphaModel interface ─────────────────────────────────────────────
    def _shortlist(self, ctx: Context, bars: dict[str, Bar]) -> list[Symbol]:
        if self.shortlist_fn is not None:
            return self.shortlist_fn(ctx, bars)[: self.max_symbols]
        scored: list[tuple[float, Symbol]] = []
        for bar in bars.values():
            ind = self._indicators(ctx, bar.symbol)
            move = abs(ind.ret5.value or 0.0) * 2 + abs(ind.ret20.value or 0.0)
            held = 1.0 if ctx.is_invested(bar.symbol) else 0.0   # always re-review holdings
            flow = 0.0
            if self.flow_feed is not None:
                s = self.flow_feed.summary(bar.symbol, ctx.now, 20)
                if s is not None:
                    # unusual 수급 is exactly where fresh information lives
                    flow = min(abs(s.participation_zscore) / 3.0, 1.0) * 0.8
            scored.append((move + held + flow, bar.symbol))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in scored[: self.max_symbols]]

    async def update(self, ctx: Context, bars: dict[str, Bar]) -> list[Insight]:
        if self._disabled:
            return []
        for bar in bars.values():
            self._indicators(ctx, bar.symbol).update(bar)

        if self.memory is not None:
            for lesson in self.memory.settle(ctx):
                # The verdict is on the excess, so print it — "+2.1% 실패" reads
                # like a bug until you can see the index did +3.4%.
                log.info("데스크 회고: %s %s → %+.2f%%%s (%s)",
                         lesson.ticker, lesson.action, lesson.realised_pct,
                         f" · 벤치 {lesson.benchmark_pct:+.2f}% · 초과 "
                         f"{lesson.excess_pct:+.2f}%" if lesson.benchmark_key else "",
                         "적중" if lesson.correct else "실패")

        self._bar_count += 1
        if (self._bar_count - 1) % self.cadence != 0:
            return []
        if self.cost_limit_usd and self.estimated_cost_usd >= self.cost_limit_usd:
            log.warning("데스크 비용 한도 $%.2f 도달 — 심의 중단", self.cost_limit_usd)
            return []
        if not self._within_plan():
            return []

        if self.flow_feed is not None:
            await self.flow_feed.refresh([b.symbol for b in bars.values()])

        targets = self._shortlist(ctx, bars)
        if not targets:
            return []

        results = await asyncio.gather(
            *(self._deliberate_cached(ctx, s) for s in targets), return_exceptions=True
        )

        insights: list[Insight] = []
        for symbol, decision in zip(targets, results):
            if isinstance(decision, BaseException):
                log.warning("데스크 심의 실패 %s: %s", symbol.ticker, decision)
                continue
            if decision is None:
                continue
            self.decisions[symbol.key] = decision
            self.history.append(decision)
            if len(self.history) > 500:
                del self.history[:250]
            if self.memory is not None:
                self.memory.record(decision)
            log.info("데스크 결정: %s", decision.summary_line())
            await ctx.bus.publish(EventType.DELIBERATION, decision.to_dict(), source=self.name)

            insight = self._to_insight(ctx, symbol, decision)
            if insight is not None:
                insights.append(insight)
        return insights

    def _within_plan(self) -> bool:
        """요금제 상한 안인가 — 계정에 묶여 있을 때만 물어봅니다.

        사이클 시작에 한 번만 봅니다. 심의마다 다시 물으면 같은 봉 안에서 상한이
        반쯤 걸려 어떤 종목은 심의되고 어떤 종목은 안 되는데, 그건 사용자에게
        설명할 수 없는 상태입니다. 대신 마지막 사이클이 상한을 한 사이클치까지
        넘길 수 있습니다 — 넘기는 쪽이 봉 하나를 반쯤 심의한 채로 두는 쪽보다
        낫다고 봤습니다.

        같은 사유는 한 번만 로그에 남깁니다. 상한은 하루 또는 한 달 단위라
        봉마다 같은 줄을 찍으면 로그가 그 문장으로만 채워집니다.
        """
        if self.meter is None:
            return True
        allowed, why = self.meter.allow()
        if allowed:
            self._capped = ""
            return True
        if self._capped != why:
            self._capped = why
            log.warning("요금제 상한 — 데스크 심의를 멈춥니다: %s", why)
        return False

    def _to_insight(self, ctx: Context, symbol: Symbol, d: DeskDecision) -> Insight | None:
        direction, strength = ACTION_TO_DIRECTION.get(d.action, (Direction.FLAT, 0.0))

        if d.vetoed:
            # A veto closes the position; it never means merely "do not add".
            return self._insight(
                ctx, symbol, Direction.FLAT, period=ctx.bar_delta * 3, confidence=0.9,
                tag=f"리스크 거부: {d.veto_reason or d.risk.get('reasoning', '')}"[:180],
                decision=d.to_dict(),
            )

        if direction is Direction.DOWN and not self.allow_short:
            direction = Direction.FLAT

        if direction is Direction.FLAT:
            if d.action in ("reduce", "sell", "strong_sell"):
                return self._insight(
                    ctx, symbol, Direction.FLAT,
                    period=ctx.bar_delta * max(d.horizon_bars // 2, 2), confidence=0.85,
                    tag=f"데스크 {d.action}: {d.rationale[:150]}", decision=d.to_dict(),
                )
            # 'hold' says nothing, so an existing position is left to the other
            # alpha models rather than being closed by silence.
            return None

        conviction = d.conviction * strength * d.position_scale
        if d.dissent_ratio > self.max_dissent_ratio:
            # The head overrode most of the desk. Allowed, but it should cost
            # conviction rather than pass through unnoticed.
            conviction *= 1.0 - d.dissent_ratio
        if conviction < self.min_conviction:
            return None

        return self._insight(
            ctx, symbol, direction,
            period=ctx.bar_delta * max(d.horizon_bars, 1),
            confidence=min(conviction, 0.95),
            magnitude=(abs(d.expected_move_pct) / 100.0) or None,
            tag=f"데스크 {d.action}: {d.rationale[:150]}",
            decision=d.to_dict(),
        )

    async def _deliberate_cached(self, ctx: Context, symbol: Symbol) -> DeskDecision | None:
        last = ctx.latest(symbol)
        if last is None:
            return None
        key = hashlib.sha1(
            f"{symbol.key}|{last.ts.isoformat()}|{ctx.timeframe}|"
            f"{self.debate_rounds}|{len(self.analyst_seats)}".encode()
        ).hexdigest()
        cached = self._cache.get(key)
        if cached is not None:
            # 캐시로 답한 심의는 아무 호출도 하지 않았습니다 — 안 쓴 것을
            # 청구하지 않으려면 계량기를 여는 것도 여기 뒤여야 합니다.
            return cached
        with metered() as spend:
            try:
                decision = await self.deliberate(ctx, symbol)
            finally:
                # 실패했어도 부른 만큼은 이미 나갔습니다. 성공만 적으면 마감을
                # 넘겨 버린 심의의 비용이 아무 계정에도 안 잡히고, 실패는 대개
                # 몰려서 옵니다.
                if self.meter is not None and spend.calls:
                    self.meter.record(spend.calls, spend.cost_usd)
        if decision is not None:
            self._cache[key] = decision
            if len(self._cache) > 1000:
                for k in list(self._cache)[:500]:
                    self._cache.pop(k, None)
        return decision

    # ── operations ───────────────────────────────────────────────────────
    @property
    def estimated_cost_usd(self) -> float:
        """Spend estimate, priced per model.

        This drives `cost_limit_usd`, so a single blended rate was wrong in
        both directions: it over-charged a cheap analyst model by ~6x (halting
        a run that had barely spent anything) and would under-charge a mixed
        setup whose decision seats run on a larger model. Unknown models fall
        back to the most expensive rate we know — see `price_for`.
        """
        counters = {id(self.client.usage): self.client.usage,
                    id(self.decision_client.usage): self.decision_client.usage}
        return sum(u.cost_usd for u in counters.values())

    def status(self) -> dict:
        return {
            "enabled": not self._disabled,
            "disabled_reason": self._disabled,
            "seat_count": len(self.seats),
            "seats": [
                {"key": s.key, "name": SPRITE_NAMES.get(s.key, s.key.upper()),
                 "title": s.title_ko, "stage": s.stage, "tier": s.tier}
                for s in self.seats
            ],
            "debate_rounds": self.debate_rounds,
            "risk_debate_rounds": self.risk_debate_rounds,
            "cadence_bars": self.cadence,
            "deadline_s": self.deadline_s,
            "deliberations": len(self.history),
            "llm_calls": self._calls(),
            "estimated_cost_usd": round(self.estimated_cost_usd, 3),
            "cost_limit_usd": self.cost_limit_usd or None,
            "memory": self.memory.stats if self.memory else None,
        }
