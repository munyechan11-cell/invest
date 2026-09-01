"""실시간에 가까운 화면이 속도보다 먼저 진실을 말하는지 고정합니다."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "quant" / "api" / "static" / "index.html"
STYLE = ROOT / "quant" / "api" / "static" / "app.css"
HTML = PAGE.read_text(encoding="utf-8")
CSS = STYLE.read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))


def _whole_fn(name: str) -> str:
    match = re.search(rf"((?:async )?function {name}\([^)]*\) \{{.*?\n\}})", SCRIPT, re.S)
    assert match, f"{name} 함수를 찾지 못했습니다"
    return match.group(1)


def _engine() -> str | None:
    return shutil.which("node") or (
        "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
        if Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc").exists()
        else None
    )


def test_mobile_cockpit_flow_is_account_market_order_then_automation():
    rail = re.search(r'<nav class="cockpit-rail".*?</nav>', HTML, re.S)
    assert rail, "거래 준비 상태 navigation이 없습니다"
    targets = re.findall(r'data-cockpit="([^"]+)"', rail.group(0))
    assert targets == ["account", "market", "order", "auto"]
    assert 'aria-label="거래 준비 상태"' in rail.group(0)
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in CSS


def test_account_refresh_is_manual_bounded_and_wakes_on_return():
    assert 'id="brokerRefresh"' in HTML
    assert 'aria-live="polite"' in re.search(
        r'<span class="freshness" id="brokerFreshness".*?</span>', HTML, re.S
    ).group(0)
    scheduler = _whole_fn("scheduleBrokerAccountRefresh")
    assert "Math.max(15000" in scheduler and "Math.min(900000" in scheduler
    assert "next || 20000" in scheduler
    assert "document.hidden" in scheduler
    assert "visibilitychange" in SCRIPT and 'addEventListener("focus"' in SCRIPT
    show_page = _whole_fn("showPage")
    assert "loadBrokerAccount" in show_page
    assert "scheduleBrokerAccountRefresh(20000)" in show_page


def test_duplicate_market_wakeups_do_not_start_the_same_read_twice():
    wake = _whole_fn("wakeMarketPolling")
    loader = _whole_fn("loadChart")
    assert "!marketRequestPending || supersede" in wake
    assert "await loadChart()" in wake
    assert "wakeGeneration !== marketPollWakeGeneration" in wake
    assert "requestKey === marketRequestKey" in loader
    assert 'wakeMarketPolling(true, true)' in SCRIPT


def test_periodic_universe_change_invalidates_the_old_market_selection():
    refresh = _whole_fn("refreshSymbols")
    sync = _whole_fn("syncChartSymbols")
    assert "return previous !== sel.value" in sync
    assert "const chartSelectionChanged = syncChartSymbols(syms)" in refresh
    assert "invalidateMarketSelection()" in refresh
    assert "wakeMarketPolling(true, true)" in refresh


def test_equity_accepts_the_selected_template_canonical_strategy_name():
    matcher = _whole_fn("strategyResponseMatches")
    loader = _whole_fn("loadEquity")
    assert "item.id === selected" in matcher
    assert "reported === strategy.name" in matcher
    assert "!strategyResponseMatches(points.strategy, strategy)" in loader


def test_chart_to_account_mobile_transition_cannot_hide_the_account_column():
    compact = re.sub(r"\s+", "", CSS)
    assert ('body[data-page="account"].deck-l,'
            'body[data-page="me"].deck-l{display:block}') in compact


@pytest.mark.skipif(_engine() is None, reason="JavaScript 엔진이 없습니다")
def test_rest_polling_is_not_announced_as_a_realtime_stream():
    engine = _engine()
    assert engine
    source = _whole_fn("marketFreshnessState") + r"""
var base = {freshness:{status:"live",age_ms:500,poll_after_ms:3000},
  capabilities:{websocket_active:false},_receivedAt:Date.now(),quote:{}};
var rest = marketFreshnessState(base);
base.capabilities.websocket_active = true;
var stream = marketFreshnessState(base);
var write = (typeof console !== "undefined" && console.log) ? console.log : print;
write(JSON.stringify({rest:rest.label, stream:stream.label}));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(source)
        path = Path(handle.name)
    try:
        result = subprocess.run([engine, str(path)], capture_output=True, text=True,
                                timeout=30, check=True)
    finally:
        path.unlink(missing_ok=True)
    labels = json.loads(result.stdout.strip().splitlines()[-1])
    assert labels == {"rest": "REST 최신", "stream": "실시간 연결"}


@pytest.mark.skipif(_engine() is None, reason="JavaScript 엔진이 없습니다")
def test_freshness_time_names_the_oldest_displayed_component():
    engine = _engine()
    assert engine
    source = _whole_fn("marketFreshnessState") + r"""
var now = Date.now();
var staleTs = new Date(now - 10800000).toISOString();
var freshTs = new Date(now).toISOString();
var state = marketFreshnessState({
  quote:{ts:freshTs}, _receivedAt:now,
  freshness:{status:"stale",age_ms:10800000,poll_after_ms:10000,components:{
    quote:{status:"fresh",age_ms:0,ts:freshTs},
    depth:{status:"stale",age_ms:10800000,ts:staleTs},
    trades:{status:"stale",age_ms:10700000,ts:staleTs}
  }},
  capabilities:{websocket_active:false}, market:{state:null}
});
var write = (typeof console !== "undefined" && console.log) ? console.log : print;
write(JSON.stringify({label:state.label,basis:state.basisLabel,ts:state.timestamp}));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(source)
        path = Path(handle.name)
    try:
        result = subprocess.run([engine, str(path)], capture_output=True, text=True,
                                timeout=30, check=True)
    finally:
        path.unlink(missing_ok=True)
    got = json.loads(result.stdout.strip().splitlines()[-1])
    assert got["label"] == "오래된 시세"
    assert got["basis"] == "호가창"
    assert got["ts"].endswith("Z")


@pytest.mark.skipif(_engine() is None, reason="JavaScript 엔진이 없습니다")
def test_unknown_component_never_borrows_fresh_quote_time():
    engine = _engine()
    assert engine
    source = _whole_fn("marketFreshnessState") + r"""
var now = Date.now();
var freshTs = new Date(now).toISOString();
var state = marketFreshnessState({
  quote:{ts:freshTs}, _receivedAt:now,
  freshness:{status:"unknown",age_ms:null,poll_after_ms:5000,components:{
    quote:{status:"fresh",age_ms:0,ts:freshTs},
    depth:{status:"unknown",age_ms:null,ts:null},
    trades:{status:"unavailable",age_ms:null,ts:null}
  }},
  capabilities:{websocket_active:false}, market:{state:null}
});
var write = (typeof console !== "undefined" && console.log) ? console.log : print;
write(JSON.stringify({label:state.label,basis:state.basisLabel,
  ts:state.timestamp,age:state.age}));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(source)
        path = Path(handle.name)
    try:
        result = subprocess.run([engine, str(path)], capture_output=True, text=True,
                                timeout=30, check=True)
    finally:
        path.unlink(missing_ok=True)
    got = json.loads(result.stdout.strip().splitlines()[-1])
    assert got == {
        "label": "신선도 미확인", "basis": "호가창", "ts": "", "age": None,
    }


@pytest.mark.skipif(_engine() is None, reason="JavaScript 엔진이 없습니다")
def test_old_trade_event_does_not_relabel_a_fresh_book_as_stale():
    engine = _engine()
    assert engine
    source = _whole_fn("marketFreshnessState") + r"""
var now = Date.now();
var freshTs = new Date(now).toISOString();
var oldTs = new Date(now - 10800000).toISOString();
var state = marketFreshnessState({
  quote:{ts:freshTs}, _receivedAt:now,
  freshness:{status:"fresh",age_ms:0,poll_after_ms:2500,components:{
    quote:{status:"fresh",age_ms:0,ts:freshTs,affects_overall:true},
    depth:{status:"fresh",age_ms:0,ts:freshTs,affects_overall:true},
    trades:{status:"stale",age_ms:10800000,ts:oldTs,affects_overall:false}
  }},
  capabilities:{websocket_active:false}, market:{state:null}
});
var write = (typeof console !== "undefined" && console.log) ? console.log : print;
write(JSON.stringify({label:state.label,basis:state.basisLabel,ts:state.timestamp}));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(source)
        path = Path(handle.name)
    try:
        result = subprocess.run([engine, str(path)], capture_output=True, text=True,
                                timeout=30, check=True)
    finally:
        path.unlink(missing_ok=True)
    got = json.loads(result.stdout.strip().splitlines()[-1])
    assert got["label"] == "REST 최신"
    assert got["basis"] == "현재가"
    assert got["ts"].endswith("Z")


def test_primary_new_controls_keep_touch_and_keyboard_contracts():
    assert re.search(r"\.cockpit-step\{[^}]*min-height:58px", CSS, re.S)
    assert re.search(r"\.account-refreshbar \.btn\{min-height:44px", CSS)
    assert ':focus-visible{outline' in CSS
    reduced = re.findall(r"@media\(prefers-reduced-motion:reduce\).*?\n\}", CSS, re.S)
    assert any("transition:none" in block for block in reduced)


def test_market_numbers_remain_readable_and_missing_change_is_not_invented():
    """10px 장식 글꼴과 실제 투자 숫자를 분리합니다."""
    compact = re.sub(r"\s+", "", CSS)
    assert "table.micro{display:table;width:100%" in compact
    assert "font-family:var(--mono);font-size:12px" in compact
    assert ".market-freshbartime,.market-source{font-family:var(--mono);font-size:12px" in compact
    chart_loader = _whole_fn("loadChart")
    assert 'rawChange == null ? "—"' in chart_loader
    assert "last -" not in chart_loader


def test_snapshot_fallback_keeps_truthful_provider_and_tick_contracts():
    snapshot = _whole_fn("marketSnapshot")
    freshness = _whole_fn("renderMarketFreshness")
    assert "(snapshot || {}).tick_size || (candles || {}).tick_size" in snapshot
    assert "snapshotHasPrice ? snapshotQuote : (candleQuote || snapshotQuote)" in snapshot
    assert "snapshotHasPrice ? ((snapshot || {}).freshness || fallbackFreshness)" in snapshot
    assert 'provider.startsWith("toss_")' in freshness
    assert '"토스증권"' in freshness


def test_missing_all_prices_is_unavailable_and_never_enables_order_controls():
    snapshot = _whole_fn("marketSnapshot")
    loader = _whole_fn("loadChart")
    assert "const candleHasPrice" in snapshot
    assert '!candleHasPrice ? "unavailable"' in snapshot
    assert 'message: candleHasPrice' in snapshot
    assert "setChartOrderAvailability(hasUsablePrice)" in loader
    assert "Number.isFinite(rawLast) && rawLast > 0" in loader
    freshness = _whole_fn("marketFreshnessState")
    renderer = _whole_fn("renderMarketFreshness")
    assert '"unavailable"].includes(state)' in freshness
    assert 'state === "unavailable" ? "시세 조회 불가"' in freshness
    assert 'status.state === "unavailable"' in renderer


def test_reconnect_aligns_hidden_selector_before_strategy_scoped_reads():
    refresh = _whole_fn("refresh")
    align = _whole_fn("alignRunningStrategySelection")
    strategies_loader = _whole_fn("loadStrategies")
    assert "strategyAligned = alignRunningStrategySelection()" in refresh
    assert refresh.index("strategyAligned = alignRunningStrategySelection()") < \
        refresh.index("await loadEquity(generation)")
    assert "x.name === runningStrategy" in align
    assert "sel.value = active.id" in align
    assert "invalidateMarketSelection()" in align
    assert "alignRunningStrategySelection()" in strategies_loader


def test_partial_account_response_never_claims_the_whole_account_is_latest():
    account = _whole_fn("loadBrokerAccount")
    assert "d.summary_complete === false || d.items_complete === false" in account
    assert 'partial ? "계좌 일부 미조회" : "계좌 최신"' in account
    assert '"일부 통화 합계 미조회"' in account


def test_strategy_change_invalidates_old_account_amount_and_cooldown():
    invalidate = _whole_fn("invalidateBrokerAccount")
    assert "brokerAccountRetryUntil = 0" in invalidate
    assert 'rail.textContent = "계좌 확인 필요"' in invalidate
    assert "delete updated.dataset.updatedAt" in invalidate


def test_market_panel_prioritizes_identity_position_orders_and_ticket():
    markers = [
        'class="market-freshbar"', 'id="cPos"', 'id="cOrders"',
        'class="ticket"', 'id="cFacts"', 'id="chartBox"',
    ]
    offsets = [HTML.index(marker) for marker in markers]
    assert offsets == sorted(offsets)
    compact = re.sub(r"\s+", "", CSS)
    assert ".quotebar#cSym{min-width:44px;width:100%}" in compact
    assert '@media(min-width:1241px)' in compact
    assert 'grid-template-areas:"symbolsymbolsymbol""timeframestalestale"' \
        '"pricepricechange"' in compact
    assert "@media(max-width:1240px)" in compact
    assert ".deck-r>.panel{scroll-margin-top:" in compact
    assert ".ticket{scroll-margin-top:" in compact


def test_tablet_breakpoint_is_shared_by_visibility_and_scroll_logic():
    desk = _whole_fn("scrollToDesk")
    visible = _whole_fn("marketPanelVisible")
    view = _whole_fn("showView")
    assert "viewportWidth <= 1240" in desk
    assert ") > 1240 ||" in visible
    assert "viewportWidth <= 1240" in view


def test_every_money_action_uses_code_first_review_before_posting():
    review = _whole_fn("confirmOrderReview")
    manual = _whole_fn("manualAction")
    chart = _whole_fn("chartOrder")
    symbol = _whole_fn("orderSymbolLabel")
    assert "`${code} · ${nm}`" in symbol
    assert "const status = await api(\"/api/status\")" in review
    assert "실거래 주문 요청이 대기열에 들어갑니다" in review
    assert manual.index("await confirmOrderReview") < manual.index("await post(path")
    assert chart.index("await confirmOrderReview") < chart.index('await post("/api/manual/"')
    for label in ("매수 주문 검토", "매도 주문 검토", "이 종목 청산 검토"):
        assert label in HTML


def test_unpriced_or_stale_market_buy_needs_a_fresh_trade_quote_or_limit():
    reviewable = _whole_fn("marketBuyReviewable")
    manual = _whole_fn("manualAction")
    chart = _whole_fn("chartOrder")
    assert 'priceKind === "last" || priceKind === "midpoint"' in reviewable
    assert 'quoteStatus === "fresh" || quoteStatus === "live"' in reviewable
    assert "payload.limit_price == null" in manual
    assert "!marketBuyReviewable(chartData, payload.ticker)" in manual
    assert 'side === "buy" && !(limit > 0)' in chart


def test_status_refresh_relabels_an_already_running_bot_with_authoritative_mode():
    refresh = _whole_fn("refresh")
    setter = _whole_fn("setRunning")
    assert "setRunning(!!s.running, true)" in refresh
    assert "if (refreshDetails) renderRunSummary(!!on)" in setter


def test_bot_ledger_orders_and_positions_are_not_labelled_as_whole_account_truth():
    orders = _whole_fn("renderOrderStrip")
    chart = _whole_fn("loadChart")
    assert "봇 미체결" in orders
    assert "orders_source" in orders
    assert "내 미체결" not in orders
    assert "최근 봇 체결 없음" in orders
    assert "최근 내 체결 없음" not in orders
    assert "봇 장부 보유" in chart
    assert "실제 보유는 내 계좌에서 확인하세요" in chart
    assert 'orders_source === "running_engine"' in chart
    assert 'ordersAvailable ? orders.length : "미조회"' in chart


@pytest.mark.skipif(_engine() is None, reason="JavaScript 엔진이 없습니다")
def test_an_unsupported_rich_snapshot_cannot_erase_a_valid_candle_price():
    """지원 안 되는 depth 응답도 성공 응답이므로 객체 존재만 보면 안 됩니다."""
    source = _whole_fn("marketSnapshot") + r"""
var chartData = null;
var marketBarsFetchedAt = 0;
function URLSearchParams(init) {
  this.values = init || {};
  this.get = key => this.values[key] || "";
  this.toString = () => "";
}
async function api(path) {
  if (path.indexOf("/api/market/snapshot") === 0) return {
    ticker:"AAA", currency:"USD", quote:{price:null,bid:null,ask:null,ts:null},
    freshness:{status:"unavailable",age_ms:null,poll_after_ms:10000},
    capabilities:{depth:false,recent_trades:false}, depth:null, recent_trades:[]
  };
  return {
    ticker:"AAA", currency:"USD", timeframe:"1d",
    quote:{price:122.24,ts:"2026-09-01T00:00:00+00:00"},
    bars:[{c:122.24}], stale:false, position:null, orders:[], fills:[]
  };
}
(async () => {
  const out = await marketSnapshot(new URLSearchParams({
    ticker:"AAA",timeframe:"1d",strategy:"demo",count:"160"
  }));
  var write = (typeof console !== "undefined" && console.log) ? console.log : print;
  write(JSON.stringify({
    price:out.quote.price,
    freshness:out.freshness.status,
    fallback:out._snapshotFailed
  }));
})().catch(error => {
  var write = (typeof console !== "undefined" && console.log) ? console.log : print;
  write("ERROR: " + error);
});
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(source)
        path = Path(handle.name)
    try:
        result = subprocess.run([_engine(), str(path)], capture_output=True,
                                text=True, timeout=30, check=True)
    finally:
        path.unlink(missing_ok=True)
    got = json.loads(result.stdout.strip().splitlines()[-1])
    assert got == {"price": 122.24, "freshness": "delayed", "fallback": True}


@pytest.mark.skipif(_engine() is None, reason="JavaScript 엔진이 없습니다")
def test_candle_fallback_hides_unlabelled_rich_data_and_keeps_retry_after():
    source = _whole_fn("marketSnapshot") + r"""
var chartData = null;
var marketBarsFetchedAt = 0;
function URLSearchParams(init) {
  this.values = init || {};
  this.get = key => this.values[key] || "";
  this.toString = () => "";
}
async function api(path) {
  if (path.indexOf("/api/market/snapshot") === 0) return {
    ticker:"AAA", currency:"USD", quote:{price:null,bid:99,ask:null,ts:null},
    freshness:{status:"unknown",age_ms:null,poll_after_ms:120000,
      components:{depth:{status:"stale",age_ms:90000,affects_overall:true}}},
    capabilities:{depth:true,depth_available:true,recent_trades_available:true},
    depth:{bids:[{price:99,quantity:1}],asks:[]},
    recent_trades:[{price:99,quantity:1}]
  };
  return {ticker:"AAA",currency:"USD",timeframe:"1d",
    quote:{price:100,price_kind:"bar_close",ts:"2026-09-01T00:00:00+00:00"},
    bars:[{c:100}],stale:false,position:null,orders:[],fills:[]};
}
(async () => {
  const out = await marketSnapshot(new URLSearchParams({
    ticker:"AAA",timeframe:"1d",strategy:"demo",count:"160"
  }));
  var write = (typeof console !== "undefined" && console.log) ? console.log : print;
  write(JSON.stringify({poll:out.freshness.poll_after_ms,depth:out.depth,
    trades:out.recent_trades.length,depthAvailable:out.capabilities.depth_available}));
})().catch(error => {
  var write = (typeof console !== "undefined" && console.log) ? console.log : print;
  write("ERROR: " + error);
});
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(source)
        path = Path(handle.name)
    try:
        result = subprocess.run([_engine(), str(path)], capture_output=True,
                                text=True, timeout=30, check=True)
    finally:
        path.unlink(missing_ok=True)
    got = json.loads(result.stdout.strip().splitlines()[-1])
    assert got == {
        "poll": 120000, "depth": None, "trades": 0, "depthAvailable": False,
    }
