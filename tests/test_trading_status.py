"""거래상태 필터 — the only link in the chain that asks the venue anything.

Every other filter reasons from bars, and a 거래정지 name's bars look perfectly
healthy right up until they stop: AgeFilter, VolumeFilter and SpreadFilter all
pass it, and the engine goes off to open a position it cannot open or — far
worse — cannot close.

Two things are pinned here. An instrument the venue will not trade normally
never enters the universe. And a name you already hold is never simply dropped
when it halts: dropping it only means HeldPositionFilter puts it back and the
engine keeps trading it as if nothing happened, so it stays, flagged '청산
불가', and it is loud. Money being stuck is not a quiet fact.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from quant.core.account import Portfolio
from quant.core.clock import SimClock
from quant.core.context import Context
from quant.core.events import EventBus, EventType
from quant.core.types import UTC, Bar, RunMode, Symbol
from quant.data.provider import DataProvider
from quant.data.universe import (
    BUILTIN_UNIVERSE_FILTERS,
    CAUTION,
    DANGER,
    OVERHEATED,
    SUPERVISED,
    WARNING,
    AgeFilter,
    SelectionReport,
    SpreadFilter,
    StaticSource,
    TradingStatus,
    TradingStatusFilter,
    UniverseSelector,
    set_trading_status,
    stuck_positions,
)

T0 = datetime(2024, 6, 3, tzinfo=UTC)


class NullProvider(DataProvider):
    name = "null"

    async def history(self, symbol, timeframe, start, end):
        return []


def sym(ticker: str, venue: str = "kis") -> Symbol:
    return Symbol(ticker, venue=venue, quote_currency="KRW",
                  tick_size=Decimal("100"), lot_size=Decimal("1"))


def make_ctx() -> Context:
    return Context(SimClock(T0), Portfolio(10_000_000.0), EventBus(), timeframe="1d")


def seed(ctx: Context, symbol: Symbol, price: float = 60_000.0, bars: int = 5) -> None:
    for i in range(bars):
        ts = T0 - timedelta(days=bars - i)
        ctx.push_bar(Bar(symbol, ts, price, price * 1.01, price * 0.99, price, 1e6, "1d"))


def hold(ctx: Context, symbol: Symbol, qty: int = 10, price: float = 60_000.0) -> None:
    seed(ctx, symbol, price)
    pos = ctx.portfolio.position(symbol)
    pos.quantity = Decimal(str(qty))
    pos.avg_price = price
    pos.opened_at = T0 - timedelta(days=5)
    pos.mark(price)


def collect(ctx: Context) -> list:
    seen: list = []
    ctx.bus.on(EventType.PROTECTION, seen.append)
    return seen


def run(f, ctx, symbols):
    report = SelectionReport()
    out = asyncio.run(f.apply(ctx, symbols, report))
    return out, report


def status(**kw) -> TradingStatus:
    kw.setdefault("as_of", T0)
    return TradingStatus(source="test", **kw)


# ── 거래정지: the state the engine has no model for ───────────────────────
def test_a_halted_name_never_enters_the_universe():
    ok, halted = sym("005930"), sym("000660")
    ctx = make_ctx()
    set_trading_status(ctx, ok, status())
    set_trading_status(ctx, halted, status(halted=True))
    out, report = run(TradingStatusFilter(), ctx, [ok, halted])
    assert out == [ok]
    assert "거래정지" in report.reasons["000660"]
    assert report.dropped["trading_status"] == ["000660"]


def test_a_held_halted_name_is_kept_and_flagged_cannot_exit():
    """The subtlety that makes this filter different from freqtrade's DelistFilter.

    Dropping it would be undone by HeldPositionFilter one link later, and the
    engine would go on targeting a position it cannot close. It stays, and the
    operator is told their money is stuck."""
    held = sym("000660")
    ctx = make_ctx()
    hold(ctx, held)
    set_trading_status(ctx, held, status(halted=True))
    out, report = run(TradingStatusFilter(), ctx, [held])
    assert out == [held]
    assert not report.dropped
    assert "청산할 수 없습니다" in report.stuck["000660"]
    assert stuck_positions(ctx) == {"kis:000660": report.stuck["000660"]}


def test_a_stuck_position_is_announced_once_not_every_refresh():
    held = sym("000660")
    ctx = make_ctx()
    hold(ctx, held)
    set_trading_status(ctx, held, status(halted=True))
    seen = collect(ctx)
    f = TradingStatusFilter()
    run(f, ctx, [held])
    run(f, ctx, [held])
    assert len(seen) == 1
    payload = seen[0].payload
    assert payload["symbol"] == "000660" and "거래정지" in payload["reason"]


def test_the_flag_clears_and_is_announced_when_trading_resumes():
    held = sym("000660")
    ctx = make_ctx()
    hold(ctx, held)
    set_trading_status(ctx, held, status(halted=True))
    seen = collect(ctx)
    f = TradingStatusFilter()
    run(f, ctx, [held])
    set_trading_status(ctx, held, status())
    out, report = run(f, ctx, [held])
    assert out == [held]
    assert not report.stuck and stuck_positions(ctx) == {}
    assert [e.payload["reason"] for e in seen][-1] == "정상 매매 재개"


def test_a_resting_order_counts_as_held():
    """A buy that is still resting when the name halts leaves the same exposure
    problem as a filled one."""
    pending = sym("005930")
    ctx = make_ctx()
    seed(ctx, pending)
    ctx.set_pending({pending.key: Decimal("5")})
    set_trading_status(ctx, pending, status(halted=True))
    out, report = run(TradingStatusFilter(), ctx, [pending])
    assert out == [pending]
    assert "005930" in report.stuck


# ── 정리매매 / 단일가: trading that is not the trading we model ────────────
def test_liquidation_is_blocked_and_the_holder_is_told_about_the_deadline():
    """정리매매 is 7 sessions of 30-minute 단일가 with no 가격제한폭 at all —
    the ±30% the rest of the engine assumes simply does not exist."""
    doomed = sym("900110")
    ctx = make_ctx()
    hold(ctx, doomed)
    set_trading_status(ctx, doomed, status(liquidation=True))
    out, report = run(TradingStatusFilter(), ctx, [doomed])
    assert out == [doomed]
    assert "정리매매" in report.stuck["900110"]
    assert "7거래일" in report.stuck["900110"]

    fresh = sym("900120")
    ctx2 = make_ctx()
    set_trading_status(ctx2, fresh, status(liquidation=True))
    out2, report2 = run(TradingStatusFilter(), ctx2, [fresh])
    assert out2 == []
    assert "정리매매" in report2.reasons["900120"]


def test_single_price_auction_is_blocked_because_the_order_book_is_not_there():
    """단기과열 puts the name into 30-minute 단일가 for 3 sessions, extendable
    once to 6. Spread, turnover and the mid-based limit offset are then all
    computed from a continuous book that does not exist."""
    overheated = sym("035420")
    ctx = make_ctx()
    set_trading_status(ctx, overheated, status(single_price=True, designation=OVERHEATED))
    out, report = run(TradingStatusFilter(), ctx, [overheated])
    assert out == []
    assert "단일가매매" in report.reasons["035420"]


def test_a_held_name_in_a_single_price_auction_is_flagged_but_not_called_untradable(caplog):
    """단기과열 can still be sold — what it cannot do is give the execution model
    the continuous book it prices against. Calling that '청산 불가' would spend
    the operator's attention on the wrong alarm."""
    held = sym("035420")
    ctx = make_ctx()
    hold(ctx, held)
    set_trading_status(ctx, held, status(single_price=True, designation=OVERHEATED))
    with caplog.at_level(logging.WARNING, logger="quant.universe"):
        out, report = run(TradingStatusFilter(), ctx, [held])
    assert out == [held]
    assert "청산할 수 없습니다" not in report.stuck["035420"]
    assert "연속 호가창" in report.stuck["035420"]
    levels = {r.levelname for r in caplog.records if "035420" in r.getMessage()}
    assert levels == {"WARNING"}


def test_a_held_halted_name_is_logged_at_error(caplog):
    held = sym("000660")
    ctx = make_ctx()
    hold(ctx, held)
    set_trading_status(ctx, held, status(halted=True))
    with caplog.at_level(logging.WARNING, logger="quant.universe"):
        run(TradingStatusFilter(), ctx, [held])
    assert [r.levelname for r in caplog.records if "000660" in r.getMessage()] == ["ERROR"]


# ── 시장경보: which designations are worth a round trip ────────────────────
def test_caution_is_not_filtered_because_nothing_about_the_mechanism_changes():
    """투자주의 is a one-day tag on an otherwise normal continuous auction.
    Filtering it buys no execution safety and pays ~35-45bp per forced round
    trip for the privilege."""
    tagged = sym("005380")
    ctx = make_ctx()
    set_trading_status(ctx, tagged, status(designation=CAUTION))
    out, _ = run(TradingStatusFilter(), ctx, [tagged])
    assert out == [tagged]


def test_warning_is_not_filtered_by_default_but_can_be():
    """투자경고 does not itself halt: the one-day 매매거래정지 is conditional on
    a further two-day 40% gain. Available to the operator, off by default."""
    tagged = sym("005380")
    ctx = make_ctx()
    set_trading_status(ctx, tagged, status(designation=WARNING))
    assert run(TradingStatusFilter(), ctx, [tagged])[0] == [tagged]
    out, report = run(TradingStatusFilter(blocked_designations=[WARNING]), ctx, [tagged])
    assert out == []
    assert "투자경고" in report.reasons["005380"]


def test_danger_and_supervised_are_blocked_by_default():
    """투자위험 halts for a day at designation; 관리종목 is the road to 정리매매."""
    risky, watched = sym("011000"), sym("012000")
    ctx = make_ctx()
    set_trading_status(ctx, risky, status(designation=DANGER))
    set_trading_status(ctx, watched, status(designation=SUPERVISED))
    out, report = run(TradingStatusFilter(), ctx, [risky, watched])
    assert out == []
    assert "투자위험" in report.reasons["011000"]
    assert "관리종목" in report.reasons["012000"]


def test_a_designation_on_a_held_name_is_not_a_cannot_exit():
    """관리종목 blocks new money, but the order book is normal and the position
    can be sold like any other — calling that 'stuck' would cry wolf."""
    watched = sym("012000")
    ctx = make_ctx()
    hold(ctx, watched)
    set_trading_status(ctx, watched, status(designation=SUPERVISED))
    out, report = run(TradingStatusFilter(), ctx, [watched])
    assert out == [watched]
    assert not report.stuck
    assert "보유 중이라 유지" in report.reasons["012000"]


def test_english_designation_aliases_normalise_to_the_krx_wording():
    a, b = sym("011000"), sym("012000")
    ctx = make_ctx()
    set_trading_status(ctx, a, status(designation="danger"))
    set_trading_status(ctx, b, status(designation="Supervised"))
    out, _ = run(TradingStatusFilter(blocked_designations=["risk", SUPERVISED]), ctx, [a, b])
    assert out == []


# ── 상태가 없을 때 — the honest half ──────────────────────────────────────
def test_no_status_passes_by_default_and_the_filter_says_it_is_inert(caplog):
    """Not every venue exposes a status field. A filter that quietly does
    nothing is the most dangerous kind of safety net."""
    unknown = sym("BTC/USDT", venue="binance")
    ctx = make_ctx()
    with caplog.at_level(logging.WARNING, logger="quant.universe"):
        out, report = run(TradingStatusFilter(), ctx, [unknown])
    assert out == [unknown]
    assert not report.dropped
    assert any("아무것도 걸러내지 못합니다" in r.getMessage() for r in caplog.records)


def test_require_status_drops_what_the_venue_will_not_vouch_for():
    known, unknown = sym("005930"), sym("000660")
    ctx = make_ctx()
    set_trading_status(ctx, known, status())
    out, report = run(TradingStatusFilter(require_status=True), ctx, [known, unknown])
    assert out == [known]
    assert "확인 불가" in report.reasons["000660"]


def test_a_stale_halt_still_halts_but_a_stale_normal_status_is_only_a_guess():
    """Halts last days. Forgetting one because the reading is old is the far
    more expensive mistake, so staleness only ever downgrades 'normal'."""
    old_halt, old_ok = sym("000660"), sym("005930")
    ctx = make_ctx()
    stale_ts = T0 - timedelta(days=5)
    set_trading_status(ctx, old_halt, status(halted=True, as_of=stale_ts))
    set_trading_status(ctx, old_ok, status(as_of=stale_ts))

    out, report = run(TradingStatusFilter(), ctx, [old_halt, old_ok])
    assert out == [old_ok]
    assert "오래된 정보" in report.reasons["000660"]

    out2, report2 = run(TradingStatusFilter(require_status=True), ctx, [old_halt, old_ok])
    assert out2 == []
    assert "오래되었습니다" in report2.reasons["005930"]


def test_staleness_can_be_switched_off():
    old_ok = sym("005930")
    ctx = make_ctx()
    set_trading_status(ctx, old_ok, status(as_of=T0 - timedelta(days=90)))
    out, _ = run(TradingStatusFilter(require_status=True, stale_after_bars=0), ctx, [old_ok])
    assert out == [old_ok]


def test_the_status_table_is_keyed_by_venue_and_ticker():
    """Two venues can list the same ticker; Symbol is frozen and used as an
    identity key, which is exactly why the status lives beside it."""
    kis, other = sym("005930"), sym("005930", venue="sim")
    ctx = make_ctx()
    set_trading_status(ctx, kis, status(halted=True))
    out, _ = run(TradingStatusFilter(), ctx, [kis, other])
    assert out == [other]


# ── 체인 안에서 ───────────────────────────────────────────────────────────
def test_the_chain_keeps_a_halted_holding_so_an_exit_can_still_be_emitted():
    held, ok = sym("000660"), sym("005930")
    ctx = make_ctx()
    hold(ctx, held)
    seed(ctx, ok)
    set_trading_status(ctx, held, status(halted=True))
    set_trading_status(ctx, ok, status())
    selector = UniverseSelector(StaticSource([held, ok]), [TradingStatusFilter()])
    out = asyncio.run(selector.select(ctx, NullProvider()))
    assert {s.key for s in out} == {held.key, ok.key}
    report = selector.last_report
    assert "000660" in report.stuck
    assert "000660" in report.summary()
    assert report.to_dict()["stuck"] == report.stuck


def test_a_holding_an_earlier_filter_already_dropped_is_still_flagged():
    """Placement can go wrong, and the alert must not go missing when it does:
    the position is still stuck whether or not this filter saw the symbol."""
    held = sym("000660")
    ctx = make_ctx()
    hold(ctx, held)
    ctx.portfolio.position(held).opened_at = T0 - timedelta(days=1)
    set_trading_status(ctx, held, status(halted=True))
    selector = UniverseSelector(
        StaticSource([held]), [AgeFilter(min_bars=200), TradingStatusFilter()]
    )
    out = asyncio.run(selector.select(ctx, NullProvider()))
    assert [s.key for s in out] == [held.key]          # HeldPositionFilter 가 되돌린 것
    assert "000660" in selector.last_report.stuck


def test_a_backtest_report_admits_the_filter_did_not_run():
    """지정·정지는 시점 데이터라 과거가 없다. freqtrade marks DelistFilter
    supports_backtesting = NO for the same reason; the result is optimistic by
    an amount nobody can measure, so it gets printed next to the number."""
    s = sym("005930")
    ctx = make_ctx()
    seed(ctx, s)
    selector = UniverseSelector(StaticSource([s]), [TradingStatusFilter()])
    asyncio.run(selector.select(ctx, NullProvider()))
    notes = selector.last_report.to_dict()["notes"]
    assert any("백테스트에서는 동작하지 않습니다" in n for n in notes)

    live = Context(SimClock(T0), Portfolio(1.0), EventBus(), timeframe="1d",
                   run_mode=RunMode.LIVE)
    seed(live, s)
    asyncio.run(selector.select(live, NullProvider()))
    assert selector.last_report.notes == []


def test_placing_it_after_a_cost_sensitive_filter_is_called_out(caplog):
    with caplog.at_level(logging.WARNING, logger="quant.universe"):
        UniverseSelector(StaticSource([]), [SpreadFilter(), TradingStatusFilter()])
    assert any("체인 앞쪽으로 옮기세요" in r.getMessage() for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="quant.universe"):
        UniverseSelector(StaticSource([]), [TradingStatusFilter(), SpreadFilter()])
    assert not [r for r in caplog.records if "체인 앞쪽으로" in r.getMessage()]


def test_blocking_caution_warns_that_it_only_costs_money(caplog):
    with caplog.at_level(logging.WARNING, logger="quant.universe"):
        TradingStatusFilter(blocked_designations=[CAUTION])
    assert any("35~45bp" in r.getMessage() for r in caplog.records)


def test_the_filter_is_registered_for_configs():
    assert BUILTIN_UNIVERSE_FILTERS["trading_status"] is TradingStatusFilter


# ── 갇힘 표시는 풀려야 합니다 ────────────────────────────────────────────
def test_a_holding_that_resumes_trading_stops_being_flagged_as_stuck():
    """거래가 재개됐는데 "청산할 수 없습니다" 가 남으면 그건 거짓말입니다.

    갇힘 표시는 트레이더와 노티파이어가 읽으라고 만든 것입니다. 한 번 켜진 뒤
    안 꺼지면, 사용자는 팔 수 있는 종목을 못 판다고 믿게 됩니다.
    """
    ctx, s0 = make_ctx(), sym("000660")
    seed(ctx, s0)
    hold(ctx, s0)
    filt = TradingStatusFilter()

    set_trading_status(ctx, s0, status(halted=True))
    run(filt, ctx, [s0])
    assert stuck_positions(ctx)

    # 거래 재개 — 다만 관리종목 지정은 남아 진입은 계속 막힙니다.
    set_trading_status(ctx, s0, status(designation="관리종목"))
    run(filt, ctx, [s0])
    assert stuck_positions(ctx) == {}, "재개됐는데 갇힘 표시가 남았습니다"


def test_a_holding_outside_the_candidate_list_is_also_released():
    """앞선 필터가 이미 빼 버린 보유 종목도 회복되면 풀려야 합니다."""
    ctx, s0 = make_ctx(), sym("000660")
    seed(ctx, s0)
    hold(ctx, s0)
    filt = TradingStatusFilter()

    set_trading_status(ctx, s0, status(halted=True))
    run(filt, ctx, [])                      # 후보 목록에 없음
    assert stuck_positions(ctx)

    set_trading_status(ctx, s0, status())
    run(filt, ctx, [])
    assert stuck_positions(ctx) == {}
