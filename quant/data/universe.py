"""동적 유니버스 — what to trade, decided each cycle rather than hand-written.

Ported from Freqtrade's pairlist design, which gets one thing importantly
right: the universe is a **chain**, not a list. The first link *generates*
candidates (every market on the exchange, the top N by turnover, a fixed list),
and every subsequent link only ever *removes* them. That ordering is what makes
the chain safe to extend — a new filter can never smuggle an untradable
instrument into the book.

Why this matters more than it looks: most of the ways a strategy quietly loses
money are universe problems, not signal problems. A pair that listed three days
ago has no history for your indicators. A pair with an 80bp spread eats the
whole edge on entry. A pair that has not moved 2% in a month cannot pay for a
round trip. None of these show up as a bug; they show up as a slow bleed.

And one thing no amount of history can answer: whether the venue will accept an
order on the instrument at all. A 거래정지 name's bars look perfectly healthy
right up until they stop, so that question is asked of the venue, not of the
candles — see `TradingStatus` and `TradingStatusFilter`.

Every filter states *why* it dropped something, and the reasons are kept for
the log — a universe that shrinks silently is impossible to debug at 3am.
"""
from __future__ import annotations

import logging
import math
import random
import statistics
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from quant.core.context import Context
from quant.core.events import EventType
from quant.core.types import Bar, RunMode, Symbol, periods_per_year
from quant.data.provider import DataProvider

log = logging.getLogger("quant.universe")


@dataclass
class SelectionReport:
    """What the chain did, and why. Kept for the operator, not for the engine."""

    candidates: int = 0
    selected: list[str] = field(default_factory=list)
    dropped: dict[str, list[str]] = field(default_factory=dict)   # filter -> tickers
    reasons: dict[str, str] = field(default_factory=dict)         # ticker -> reason
    #: 보유 중인데 거래소 상태가 정상 매매가 아닌 종목 — ticker -> 사유.
    stuck: dict[str, str] = field(default_factory=dict)
    #: 이 선택 결과를 읽을 때 알아야 할 한계. 리포트에 그대로 실린다.
    notes: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "candidates": self.candidates,
            "selected": self.selected,
            "selected_count": len(self.selected),
            "dropped": {k: v for k, v in self.dropped.items() if v},
            "reasons": self.reasons,
            "stuck": self.stuck,
            "notes": self.notes,
            "elapsed_s": round(self.elapsed_s, 2),
        }

    def summary(self) -> str:
        drops = ", ".join(f"{k} -{len(v)}" for k, v in self.dropped.items() if v)
        stuck = ", ".join(self.stuck)
        return (f"universe {self.candidates} → {len(self.selected)}"
                + (f" ({drops})" if drops else "")
                + (f" ⚠ 거래상태 이상: {stuck}" if stuck else ""))


# ─────────────────────────────────────────────────────────────────────────────
# Sources — the first link, which generates candidates
# ─────────────────────────────────────────────────────────────────────────────
class UniverseSource(ABC):
    name = "source"

    @abstractmethod
    async def candidates(self, ctx: Context, provider: DataProvider) -> list[Symbol]:
        ...


class StaticSource(UniverseSource):
    """The hand-written list. Still the right answer for a focused book."""

    name = "static"

    def __init__(self, symbols: list[Symbol]):
        self.symbols = list(symbols)

    async def candidates(self, ctx, provider):
        return list(self.symbols)


class ExchangeSource(UniverseSource):
    """Every market the venue lists, optionally narrowed by quote currency.

    Needs a provider that can enumerate its markets; providers that cannot
    return nothing rather than pretending, and the chain says so.
    """

    name = "exchange"

    def __init__(self, quote_currency: str = "", max_symbols: int = 500,
                 exclude: list[str] | None = None):
        self.quote = quote_currency.upper()
        self.max_symbols = max_symbols
        self.exclude = {e.upper() for e in (exclude or [])}

    async def candidates(self, ctx, provider):
        lister = getattr(provider, "available_symbols", None)
        if lister is None:
            log.warning("%s cannot enumerate markets — exchange source yields nothing",
                        provider.name)
            return []
        symbols = await lister()
        out = [
            s for s in symbols
            if (not self.quote or s.quote_currency.upper() == self.quote)
            and s.ticker.upper() not in self.exclude
        ]
        return out[: self.max_symbols]


# ─────────────────────────────────────────────────────────────────────────────
# 거래 상태 — the one question the bars cannot answer
# ─────────────────────────────────────────────────────────────────────────────
# Every filter below reasons from history, and a 거래정지 name's history looks
# perfectly healthy right up until it stops. Only the venue knows whether an
# instrument can be traded *now*, so the venue has to be asked, and the answer
# has to arrive through a channel that is honest about not existing yet: a
# provider without a status feed fills nothing and the filter says so out loud
# rather than pretending everything is fine.

#: 시장경보·지정 사유. 거래소가 쓰는 표기를 그대로 둔다 — 번역하면 대조가 안 된다.
CAUTION = "투자주의"
WARNING = "투자경고"
DANGER = "투자위험"
SUPERVISED = "관리종목"
OVERHEATED = "단기과열"

#: 프로바이더마다 쓰는 말이 달라 한 어휘로 모은다.
DESIGNATION_ALIASES = {
    "caution": CAUTION, "attention": CAUTION,
    "warning": WARNING, "warn": WARNING,
    "danger": DANGER, "risk": DANGER,
    "supervised": SUPERVISED, "administrative": SUPERVISED, "admin": SUPERVISED,
    "overheated": OVERHEATED, "overheat": OVERHEATED,
}

#: 기본 차단 대상. 투자주의·투자경고가 왜 빠졌는지는 TradingStatusFilter 참고.
DEFAULT_BLOCKED_DESIGNATIONS = (SUPERVISED, DANGER, OVERHEATED)

#: 상태 표는 Context 의 스크래치 상태에 symbol.key 로 얹는다. Symbol 은 frozen 이고
#: 어디서나 동일성 키로 쓰이므로 여기에 상태를 달면 안 된다.
TRADING_STATUS_STATE = "universe.trading_status"
STUCK_POSITION_STATE = "universe.stuck"


def normalize_designation(name: str) -> str:
    tag = (name or "").strip()
    return DESIGNATION_ALIASES.get(tag.lower(), tag)


@dataclass(frozen=True)
class TradingStatus:
    """거래소가 말하는 종목 상태. 프로바이더가 채우고 필터가 읽는다.

    Three mechanism flags, not a venue code, and deliberately so. KIS's
    inquire-price payload carries temp_stop_yn / iscd_stat_cls_code /
    mrkt_warn_cls_code, but the codes that would mean 단기과열 and 정리매매 have
    not been confirmed against a live response, and a filter that silently
    mis-decodes a status field is worse than one that admits it has none.
    Providers do the translation where the venue's documentation lives; this
    module never guesses at a wire format.

    `as_of` 는 이 상태를 확인한 시각이다. 비워도 동작하지만, 비우면 필터가
    오래된 정보를 새 정보와 구분할 수 없다.
    """

    halted: bool = False        # 거래정지·임시정지 — 주문 자체가 접수되지 않는다
    liquidation: bool = False   # 정리매매 — 7거래일, 30분 단일가, 가격제한폭 없음
    single_price: bool = False  # 단일가매매 — 연속 호가창이 존재하지 않는다
    designation: str = ""       # 관리종목·투자주의/경고/위험·단기과열 등 지정 사유
    as_of: datetime | None = None
    source: str = ""
    note: str = ""

    @property
    def continuous(self) -> bool:
        """연속 호가창이 살아 있는 상태인가. 스프레드·회전율·체결 모델의 전제."""
        return not (self.halted or self.liquidation or self.single_price)

    @property
    def exitable(self) -> bool:
        """청산 주문을 낼 수는 있는가. 거래정지만 이것을 완전히 막는다."""
        return not self.halted


def set_trading_status(ctx: Context, symbol: Symbol, status: TradingStatus | None) -> None:
    """프로바이더가 거래소 상태를 알려 오는 통로. `None` 이면 지운다."""
    table = ctx.state(TRADING_STATUS_STATE)
    if status is None:
        table.pop(symbol.key, None)
    else:
        table[symbol.key] = status


def trading_status(ctx: Context, symbol: Symbol) -> TradingStatus | None:
    return ctx.state(TRADING_STATUS_STATE).get(symbol.key)


def stuck_positions(ctx: Context) -> dict[str, str]:
    """정상 매매가 안 되는 상태로 묶여 있는 보유 종목 — symbol.key -> 사유.

    운영자에게 알리는 경로(트레이더·노티파이어)가 읽어 가라고 남겨 둔다. 돈이
    묶였다는 사실은 리포트 안에서만 조용히 살아 있으면 안 된다.
    """
    return dict(ctx.state(STUCK_POSITION_STATE))


# ─────────────────────────────────────────────────────────────────────────────
# Filters — every subsequent link, which may only remove
# ─────────────────────────────────────────────────────────────────────────────
class UniverseFilter(ABC):
    name = "filter"
    #: how many bars of history this filter needs to judge a symbol
    lookback = 0
    #: freqtrade's SupportsBacktesting flag. False means the filter judges from
    #: something only the live venue knows, so a backtest silently skips it and
    #: comes out optimistic. The report has to say so rather than pretend.
    supports_backtesting = True

    @abstractmethod
    async def apply(self, ctx: Context, symbols: list[Symbol],
                    report: SelectionReport) -> list[Symbol]:
        ...

    def _drop(self, report: SelectionReport, symbol: Symbol, reason: str) -> None:
        report.dropped.setdefault(self.name, []).append(symbol.ticker)
        report.reasons[symbol.ticker] = f"{self.name}: {reason}"


class _HistoryFilter(UniverseFilter):
    """Base for filters that judge from bar history already in the context."""

    def bars(self, ctx: Context, symbol: Symbol) -> list[Bar]:
        return ctx.history(symbol, self.lookback) if self.lookback else ctx.history(symbol)


class AgeFilter(_HistoryFilter):
    """Drop instruments without enough history to warm an indicator.

    The most common cause of a strategy's first live trades being nonsense: a
    freshly listed pair whose 200-period average is computed from 12 bars.
    """

    name = "age"

    def __init__(self, min_bars: int = 200):
        self.min_bars = min_bars
        self.lookback = min_bars + 5

    async def apply(self, ctx, symbols, report):
        out = []
        for s in symbols:
            n = len(ctx.history(s))
            if n < self.min_bars:
                self._drop(report, s, f"{n} bars < {self.min_bars} required")
            else:
                out.append(s)
        return out


class TradingStatusFilter(UniverseFilter):
    """Ask the venue whether the instrument is tradable at all.

    freqtrade's DelistFilter does the crypto version of this: ask the exchange
    for a delisting date, remove the pair, and refuse to backtest. KRX needs
    more, because several of its states do not stop trading — they *replace*
    it. 정리매매 runs 7 sessions of 30-minute 단일가 with no 가격제한폭 at all,
    and 단기과열 switches a name to 30-minute 단일가 for 3 sessions, extendable
    once to 6. In those states there is no continuous order book, so
    SpreadFilter's high-low proxy, VolumeFilter's turnover, the mid-based limit
    offset and the impact term in quant/execution/costs.py are every one of
    them computed from a book that is not there. That is why this filter
    belongs *before* the cost-sensitive links, not after them.

    보유 종목은 절대 그냥 빼지 않는다. 빼 봐야 HeldPositionFilter 가 도로 넣고,
    엔진은 아무 일도 없었다는 듯 청산을 시도한다. 대신 유니버스에 남긴 채
    '청산 불가'로 표시하고 로그·이벤트로 시끄럽게 알린다 — 돈이 묶였다는 사실은
    조용히 지나가면 안 되는 종류의 사실이다.

    지정 사유는 기본적으로 관리종목·투자위험·단기과열 셋만 막는다. 투자주의는
    매매 방식이 전혀 바뀌지 않는 1일짜리 꼬리표라, 걸러 봐야 강제 왕복 한 번에
    35~45bp(거래세 20bp + 수수료 + 갓 지정된 종목의 넓은 스프레드)가 그냥
    나간다. 투자경고도 지정 자체로는 매매거래정지가 붙지 않는다 — 2일간 40%
    추가 상승이라는 별도 조건이 있어야 하루 정지된다. 반면 투자위험은 지정과
    동시에 하루 정지되고, 관리종목은 정리매매로 가는 길목이다.

    상태를 아무도 안 채워 주면 이 필터는 아무것도 못 거른다. 그럴 때 조용히
    통과시키는 대신 로그로 알린다 — 켜 놓고 잊은 안전장치가 제일 위험하다.

    지정·정지는 시점 데이터라 과거를 되돌려 볼 수 없다. 백테스트에서는 이
    필터가 사실상 꺼져 있고 그만큼 결과가 실거래보다 낙관적으로 나온다는 뜻이라,
    아닌 척하는 대신 리포트에 적는다(`supports_backtesting = False`).
    """

    name = "trading_status"
    supports_backtesting = False

    def __init__(self, blocked_designations: list[str] | None = None,
                 require_status: bool = False, stale_after_bars: int = 2):
        raw = (DEFAULT_BLOCKED_DESIGNATIONS if blocked_designations is None
               else blocked_designations)
        self.blocked = frozenset(normalize_designation(d) for d in raw if str(d).strip())
        self.require_status = require_status
        self.stale_after_bars = max(int(stale_after_bars), 0)
        self._announced: dict[str, str] = {}
        self._sourced: bool | None = None
        if CAUTION in self.blocked:
            log.warning("투자주의를 유니버스에서 제외하도록 설정되어 있습니다 — 매매 방식이 "
                        "바뀌지 않는 1일 꼬리표라, 강제 왕복 한 번마다 35~45bp가 이유 없이 "
                        "나갑니다")

    async def apply(self, ctx, symbols, report):
        held = {p.symbol.key for p in ctx.portfolio.open_positions}
        out: list[Symbol] = []
        with_status = 0
        for s in symbols:
            if trading_status(ctx, s) is not None:
                with_status += 1
            entry, exit_ = self._assess(ctx, s)
            if not (entry or exit_):
                await self._resolved(ctx, s)
                out.append(s)
            elif s.key in held or ctx.pending_quantity(s) != 0:
                out.append(s)
                if exit_:
                    await self._flag(ctx, s, exit_, report)
                else:
                    report.reasons[s.ticker] = f"{self.name}: {entry} — 보유 중이라 유지"
            else:
                await self._resolved(ctx, s)
                self._drop(report, s, entry or exit_)

        # 앞선 필터가 이미 빼 버린 보유 종목도 상태는 봐야 한다. 여기서 다시 넣지는
        # 않는다 — 그건 HeldPositionFilter 의 일이고, 필터는 더하지 않는다.
        seen = {s.key for s in symbols}
        for pos in ctx.portfolio.open_positions:
            if pos.symbol.key in seen:
                continue
            _, exit_ = self._assess(ctx, pos.symbol)
            if exit_:
                await self._flag(ctx, pos.symbol, exit_, report)

        self._note_source(with_status, len(symbols))
        return out

    def _assess(self, ctx: Context, symbol: Symbol) -> tuple[str, str]:
        """(진입을 막는 사유, 청산이 정상이 아닌 사유). 둘 다 빈 문자열이면 정상."""
        status = trading_status(ctx, symbol)
        if status is None:
            return ("거래상태 확인 불가" if self.require_status else "", "")
        entry, exit_ = "", ""
        if status.continuous:
            tag = normalize_designation(status.designation)
            if tag and tag in self.blocked:
                entry = f"{tag} 지정"
        elif status.halted:
            entry = "거래정지 — 주문이 접수되지 않습니다"
            exit_ = "거래정지 — 지금은 청산할 수 없습니다"
        elif status.liquidation:
            entry = "정리매매 — 30분 단일가, 가격제한폭 없음"
            exit_ = "정리매매 — 30분 단일가로만 나갈 수 있고 7거래일 뒤 상장폐지됩니다"
        else:
            entry = "단일가매매 — 연속 호가창이 없습니다"
            exit_ = "단일가매매 — 연속 호가창이 없어 청산 가격이 모델과 다릅니다"
        if self._is_stale(ctx, status):
            # 오래된 '정지'는 그대로 정지로 둔다. 정지는 며칠씩 가고, 모르겠다고
            # 되돌리는 쪽이 훨씬 비싼 실수다. 오래된 '정상'만 모르는 것으로 본다.
            entry = f"{entry} (오래된 정보)" if entry else ""
            exit_ = f"{exit_} (오래된 정보)" if exit_ else ""
            if not entry and self.require_status:
                entry = "거래상태 정보가 오래되었습니다"
        return entry, exit_

    def _is_stale(self, ctx: Context, status: TradingStatus) -> bool:
        if not self.stale_after_bars or status.as_of is None:
            return False
        if status.as_of.tzinfo is None:
            return False        # 시각대를 안 주는 프로바이더까지 버릴 수는 없다
        try:
            return (ctx.now - status.as_of) > ctx.bar_delta * self.stale_after_bars
        except TypeError:       # naive/aware 혼용 — 신선도 판단만 포기한다
            return False

    async def _flag(self, ctx: Context, symbol: Symbol, reason: str,
                    report: SelectionReport) -> None:
        report.stuck[symbol.ticker] = reason
        report.reasons[symbol.ticker] = f"{self.name}: {reason}"
        ctx.state(STUCK_POSITION_STATE)[symbol.key] = reason
        if self._announced.get(symbol.key) == reason:
            return              # 같은 사실을 매 갱신마다 다시 소리치지는 않는다
        self._announced[symbol.key] = reason
        status = trading_status(ctx, symbol)
        # 나갈 수조차 없는 상태와, 나갈 수는 있는데 모델과 다른 상태를 로그에서
        # 구분한다. 어느 쪽이든 이벤트는 똑같이 나간다.
        emit = log.warning if status is not None and status.exitable else log.error
        emit("보유 종목 %s — %s", symbol.ticker, reason)
        await ctx.bus.publish(
            EventType.PROTECTION,
            {"protection": "거래상태", "symbol": symbol.ticker, "venue": symbol.venue,
             "reason": reason},
            source=self.name,
        )

    async def _resolved(self, ctx: Context, symbol: Symbol) -> None:
        ctx.state(STUCK_POSITION_STATE).pop(symbol.key, None)
        if self._announced.pop(symbol.key, None) is None:
            return
        log.info("%s 거래상태 정상화 — 정상 매매로 돌아왔습니다", symbol.ticker)
        await ctx.bus.publish(
            EventType.PROTECTION,
            {"protection": "거래상태", "symbol": symbol.ticker, "venue": symbol.venue,
             "reason": "정상 매매 재개"},
            source=self.name,
        )

    def _note_source(self, with_status: int, total: int) -> None:
        if not total:
            return
        if with_status:
            if self._sourced is not True:
                self._sourced = True
                log.info("거래상태 확인 — %d/%d 종목", with_status, total)
        elif self._sourced is not False:
            self._sourced = False
            if self.require_status:
                log.error("거래상태를 알려 주는 종목이 하나도 없습니다 — require_status "
                          "설정 때문에 유니버스가 통째로 비워집니다")
            else:
                log.warning("거래상태를 알려 주는 종목이 하나도 없습니다 — 이 필터는 지금 "
                            "아무것도 걸러내지 못합니다. 프로바이더가 set_trading_status() "
                            "로 상태를 채워야 동작합니다")


class VolumeFilter(_HistoryFilter):
    """Rank by traded value and keep the top N above a floor.

    Value, not share count: 10m shares of a ₩1,200 stock is a different market
    from 10m shares of a ₩800,000 one.
    """

    name = "volume"

    def __init__(self, lookback_bars: int = 20, top_n: int = 0,
                 min_value: float = 0.0):
        self.lookback = lookback_bars
        self.top_n = top_n
        self.min_value = min_value

    async def apply(self, ctx, symbols, report):
        scored: list[tuple[float, Symbol]] = []
        for s in symbols:
            bars = self.bars(ctx, s)
            if not bars:
                self._drop(report, s, "no history")
                continue
            value = statistics.fmean([b.close * b.volume for b in bars])
            if value < self.min_value:
                self._drop(report, s, f"turnover {value:,.0f} < {self.min_value:,.0f}")
                continue
            scored.append((value, s))
        scored.sort(key=lambda x: x[0], reverse=True)
        if self.top_n:
            for _, s in scored[self.top_n:]:
                self._drop(report, s, f"outside top {self.top_n} by turnover")
            scored = scored[: self.top_n]
        return [s for _, s in scored]


class PriceFilter(_HistoryFilter):
    """Drop instruments whose tick size is coarse relative to their price.

    A ₩1,000 stock on a ₩1 tick has 10bp of granularity — a limit order can
    only ever be placed on a grid that costs more than many strategies make.
    """

    name = "price"

    def __init__(self, min_price: float = 0.0, max_price: float = 0.0,
                 max_tick_pct: float = 0.0):
        self.min_price = min_price
        self.max_price = max_price
        self.max_tick_pct = max_tick_pct
        self.lookback = 1

    async def apply(self, ctx, symbols, report):
        out = []
        for s in symbols:
            price = ctx.price(s)
            if price <= 0:
                self._drop(report, s, "no price")
                continue
            if self.min_price and price < self.min_price:
                self._drop(report, s, f"price {price:,.4f} < {self.min_price:,.4f}")
                continue
            if self.max_price and price > self.max_price:
                self._drop(report, s, f"price {price:,.4f} > {self.max_price:,.4f}")
                continue
            if self.max_tick_pct:
                tick_pct = float(s.tick_size) / price
                if tick_pct > self.max_tick_pct:
                    self._drop(report, s,
                               f"tick {tick_pct:.3%} coarser than {self.max_tick_pct:.3%}")
                    continue
            out.append(s)
        return out


class SpreadFilter(UniverseFilter):
    """Drop instruments whose quoted spread eats the expected edge.

    Uses the live quote when there is one and falls back to the bar's own
    high-low range as a proxy, which is crude but directionally right.
    """

    name = "spread"

    def __init__(self, max_spread_pct: float = 0.005):
        self.max_spread = max_spread_pct
        self.lookback = 1

    async def apply(self, ctx, symbols, report):
        out = []
        for s in symbols:
            quote = ctx.quote(s)
            if quote is not None and math.isfinite(quote.spread_pct):
                spread = quote.spread_pct
            else:
                bar = ctx.latest(s)
                if bar is None or bar.close <= 0:
                    self._drop(report, s, "no quote or bar")
                    continue
                spread = (bar.high - bar.low) / bar.close * 0.25   # rough proxy
            if spread > self.max_spread:
                self._drop(report, s, f"spread {spread:.3%} > {self.max_spread:.3%}")
                continue
            out.append(s)
        return out


class VolatilityFilter(_HistoryFilter):
    """Keep instruments inside a volatility band.

    Both ends matter. Below the floor there is no move to capture and costs
    dominate; above the ceiling position sizing becomes guesswork and stops get
    hit by noise.
    """

    name = "volatility"

    def __init__(self, lookback_bars: int = 60, min_annual_vol: float = 0.10,
                 max_annual_vol: float = 2.0):
        self.lookback = lookback_bars
        self.min_vol = min_annual_vol
        self.max_vol = max_annual_vol

    async def apply(self, ctx, symbols, report):
        out = []
        ppy = periods_per_year(ctx.timeframe)
        for s in symbols:
            closes = [b.close for b in self.bars(ctx, s)]
            rets = [math.log(b / a) for a, b in zip(closes, closes[1:])
                    if a > 0 and b > 0]
            if len(rets) < 10:
                self._drop(report, s, "not enough returns to measure volatility")
                continue
            vol = statistics.pstdev(rets) * math.sqrt(ppy)
            if vol < self.min_vol:
                self._drop(report, s, f"vol {vol:.1%} below {self.min_vol:.1%} floor")
            elif vol > self.max_vol:
                self._drop(report, s, f"vol {vol:.1%} above {self.max_vol:.1%} ceiling")
            else:
                out.append(s)
        return out


class RangeStabilityFilter(_HistoryFilter):
    """Drop instruments that simply have not moved.

    Freqtrade's insight: a pair whose whole recent range is smaller than a
    round trip's cost cannot be profitable regardless of how good the signal
    is. This is a different question from volatility — a name can have high
    variance and still go nowhere.
    """

    name = "range_stability"

    def __init__(self, lookback_bars: int = 30, min_range_pct: float = 0.03):
        self.lookback = lookback_bars
        self.min_range = min_range_pct

    async def apply(self, ctx, symbols, report):
        out = []
        for s in symbols:
            bars = self.bars(ctx, s)
            if len(bars) < 5:
                self._drop(report, s, "not enough bars")
                continue
            low = min(b.low for b in bars)
            high = max(b.high for b in bars)
            rng = (high - low) / low if low > 0 else 0.0
            if rng < self.min_range:
                self._drop(report, s, f"{self.lookback}-bar range {rng:.2%} "
                                      f"< {self.min_range:.2%}")
            else:
                out.append(s)
        return out


class CorrelationFilter(_HistoryFilter):
    """Keep the universe from becoming one bet wearing several tickers.

    Greedy: walks the candidates in their incoming order (so an upstream rank
    is respected) and drops any name too correlated with one already kept.
    """

    name = "correlation"

    def __init__(self, lookback_bars: int = 90, max_correlation: float = 0.9):
        self.lookback = lookback_bars
        self.max_corr = max_correlation

    @staticmethod
    def _returns(bars: list[Bar]) -> list[float]:
        closes = [b.close for b in bars]
        return [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]

    @staticmethod
    def _corr(xs: list[float], ys: list[float]) -> float | None:
        n = min(len(xs), len(ys))
        if n < 20:
            return None
        xs, ys = xs[-n:], ys[-n:]
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        sy = math.sqrt(sum((y - my) ** 2 for y in ys))
        if sx <= 0 or sy <= 0:
            return None
        return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)

    async def apply(self, ctx, symbols, report):
        kept: list[tuple[Symbol, list[float]]] = []
        for s in symbols:
            rets = self._returns(self.bars(ctx, s))
            clash = None
            for other, other_rets in kept:
                c = self._corr(rets, other_rets)
                if c is not None and abs(c) > self.max_corr:
                    clash = (other, c)
                    break
            if clash is not None:
                self._drop(report, s,
                           f"corr {clash[1]:+.2f} with {clash[0].ticker}")
            else:
                kept.append((s, rets))
        return [s for s, _ in kept]


class PerformanceFilter(UniverseFilter):
    """Rank by the strategy's own realised P&L on each instrument.

    Genuinely double-edged, and worth saying so: it compounds a real edge, and
    it also chases luck on a short sample. `min_trades` is the guard — below
    it, a symbol is left alone rather than promoted or demoted on noise.
    """

    name = "performance"

    def __init__(self, lookback_bars: int = 500, min_trades: int = 4,
                 drop_worst: int = 0, min_profit_pct: float | None = None):
        self.lookback_bars = lookback_bars
        self.min_trades = min_trades
        self.drop_worst = drop_worst
        self.min_profit = min_profit_pct

    async def apply(self, ctx, symbols, report):
        window = ctx.bar_delta * self.lookback_bars
        scored: list[tuple[float, int, Symbol]] = []
        for s in symbols:
            trades = ctx.recent_trades(s, within=window)
            total = sum(t.pnl_pct for t in trades)
            scored.append((total, len(trades), s))

        out = []
        for total, n, s in scored:
            if n < self.min_trades:
                out.append(s)                        # too few trades to judge
                continue
            if self.min_profit is not None and total < self.min_profit:
                self._drop(report, s, f"{n} trades netting {total:+.2%}")
                continue
            out.append(s)

        if self.drop_worst:
            judged = [(t, n, s) for t, n, s in scored
                      if n >= self.min_trades and s in out]
            judged.sort(key=lambda x: x[0])
            for total, n, s in judged[: self.drop_worst]:
                self._drop(report, s, f"worst performer: {total:+.2%} over {n} trades")
                out.remove(s)
        return out


class ShuffleFilter(UniverseFilter):
    """Randomise the order.

    Not decoration: when a downstream cap keeps the first N, a stable ordering
    means the same names are always favoured, and a backtest of that is a
    backtest of the ordering as much as the strategy.
    """

    name = "shuffle"

    def __init__(self, seed: int = 0):
        self.seed = seed
        self._round = 0

    async def apply(self, ctx, symbols, report):
        out = list(symbols)
        random.Random(f"{self.seed}:{self._round}").shuffle(out)
        self._round += 1
        return out


class LimitFilter(UniverseFilter):
    """Hard cap on universe size — the last link, usually."""

    name = "limit"

    def __init__(self, max_symbols: int = 20, offset: int = 0):
        self.max_symbols = max_symbols
        self.offset = offset

    async def apply(self, ctx, symbols, report):
        kept = symbols[self.offset: self.offset + self.max_symbols]
        for s in symbols:
            if s not in kept:
                self._drop(report, s, f"outside positions {self.offset}"
                                      f"..{self.offset + self.max_symbols}")
        return kept


class HeldPositionFilter(UniverseFilter):
    """Force anything currently held back into the universe.

    Placed last, always. Dropping a symbol you hold means no model ever emits
    an exit for it and the position becomes permanent — the single most
    dangerous failure mode a dynamic universe has.
    """

    name = "held"

    async def apply(self, ctx, symbols, report):
        keys = {s.key for s in symbols}
        out = list(symbols)
        for pos in ctx.portfolio.open_positions:
            if pos.symbol.key not in keys:
                out.append(pos.symbol)
                report.reasons[pos.symbol.ticker] = "held: re-added so it can be exited"
        return out


# ─────────────────────────────────────────────────────────────────────────────
# The chain
# ─────────────────────────────────────────────────────────────────────────────
#: 호가와 회전율로 판단하는 필터들. 단일가·정리매매 종목에서는 그 입력들이
#: 존재하지 않는 호가창에서 계산된 값이라 판단 자체가 성립하지 않는다.
COST_SENSITIVE_FILTERS = ("volume", "price", "spread", "volatility", "range_stability")


def _status_filter_placed_late(filters: list[UniverseFilter]) -> str:
    """거래상태 필터 앞에 놓인 비용 기반 필터 이름. 없으면 빈 문자열."""
    seen: list[str] = []
    for f in filters:
        if isinstance(f, TradingStatusFilter):
            return next((n for n in seen if n in COST_SENSITIVE_FILTERS), "")
        seen.append(f.name)
    return ""


class UniverseSelector:
    """One source, then filters, then a mandatory held-position re-add."""

    def __init__(self, source: UniverseSource, filters: list[UniverseFilter] | None = None,
                 refresh_every_bars: int = 24, warmup_bars: int = 250):
        self.source = source
        self.filters = list(filters or [])
        if not any(isinstance(f, HeldPositionFilter) for f in self.filters):
            self.filters.append(HeldPositionFilter())
        late = _status_filter_placed_late(self.filters)
        if late:
            log.warning("거래상태 필터가 %s 필터 뒤에 있습니다 — 단일가·정리매매 종목의 "
                        "스프레드와 회전율은 없는 호가창에서 나온 숫자라, 앞선 필터가 "
                        "그 숫자로 판단하게 됩니다. 체인 앞쪽으로 옮기세요", late)
        self.refresh_every = max(refresh_every_bars, 1)
        self.warmup_bars = warmup_bars
        self._bar_count = 0
        self._warned_backtest = False
        self.last_report: SelectionReport | None = None

    @property
    def required_history(self) -> int:
        return max([self.warmup_bars, *(f.lookback for f in self.filters)] or [0])

    def due(self) -> bool:
        due = (self._bar_count % self.refresh_every) == 0
        self._bar_count += 1
        return due

    async def select(self, ctx: Context, provider: DataProvider) -> list[Symbol]:
        import time as _time

        started = _time.monotonic()
        report = SelectionReport()
        symbols = await self.source.candidates(ctx, provider)
        report.candidates = len(symbols)

        for f in self.filters:
            before = len(symbols)
            try:
                symbols = await f.apply(ctx, symbols, report)
            except Exception:
                # A broken filter must not empty the book.
                log.exception("universe filter %s raised — skipped", f.name)
                continue
            log.debug("universe filter %s: %d → %d", f.name, before, len(symbols))

        self._note_backtest_limits(ctx, report)
        report.selected = [s.ticker for s in symbols]
        report.elapsed_s = _time.monotonic() - started
        self.last_report = report
        log.info("%s", report.summary())
        return symbols

    def _note_backtest_limits(self, ctx: Context, report: SelectionReport) -> None:
        """백테스트에서 재현되지 않는 필터를 리포트에 적어 둔다.

        A filter that asks the live venue a question has no historical answer,
        so in a backtest it simply does not fire. That makes the backtest
        optimistic by an amount nobody can measure — the only honest move is to
        print it next to the result instead of letting the number stand alone.
        """
        if ctx.run_mode is not RunMode.BACKTEST:
            return
        for f in self.filters:
            if f.supports_backtesting:
                continue
            report.notes.append(
                f"{f.name} 필터는 거래소의 현재 상태를 묻는 필터라 백테스트에서는 동작하지 "
                "않습니다 — 지정·정지 이력은 시점 데이터라 과거를 재현할 수 없고, 그만큼 이 "
                "결과는 실거래보다 낙관적입니다"
            )
        if report.notes and not self._warned_backtest:
            self._warned_backtest = True
            for note in report.notes:
                log.warning("%s", note)


BUILTIN_UNIVERSE_SOURCES = {
    "static": StaticSource,
    "exchange": ExchangeSource,
}

BUILTIN_UNIVERSE_FILTERS = {
    "age": AgeFilter,
    "trading_status": TradingStatusFilter,
    "volume": VolumeFilter,
    "price": PriceFilter,
    "spread": SpreadFilter,
    "volatility": VolatilityFilter,
    "range_stability": RangeStabilityFilter,
    "correlation": CorrelationFilter,
    "performance": PerformanceFilter,
    "shuffle": ShuffleFilter,
    "limit": LimitFilter,
    "held": HeldPositionFilter,
}
