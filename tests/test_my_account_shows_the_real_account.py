""""내 계좌" 는 봇의 장부가 아니라 사람의 계좌다.

토스를 연동해 둔 사람이 계좌 탭을 열었더니 아무것도 없었습니다. 봇이 꺼져
있었기 때문입니다 — 그 탭은 `/api/status` 의 `portfolio` 만 그렸고, 그건 돌고
있는 봇 안에만 있습니다.

연동을 마친 사람에게 빈 화면은 "연동이 안 됐다" 로 읽힙니다. 계좌는 봇의
것이 아닙니다. 봇이 꺼져 있어도, 한 번도 안 돌았어도, 다른 데서 산 종목이어도
거기 있어야 합니다.
"""
from __future__ import annotations

import inspect
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

HTML = Path("quant/api/static/index.html").read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))
CSS = Path("quant/api/static/app.css").read_text(encoding="utf-8")
SERVER = Path("quant/api/server.py").read_text(encoding="utf-8")
TOSS = Path("quant/brokerage/toss_broker.py").read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    m = re.search(rf"(?:async )?function {name}\([^)]*\) \{{(.*?)\n\}}", src, re.S)
    assert m, f"{name} 을 찾지 못했습니다"
    return m.group(1)


def _whole_fn(src: str, name: str) -> str:
    m = re.search(
        rf"((?:async )?function {name}\([^)]*\) \{{.*?\n\}})", src, re.S
    )
    assert m, f"{name} 을 찾지 못했습니다"
    return m.group(1)


_ENGINES = [
    (shutil.which("node"), []),
    ("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc", []),
]


def _engine():
    for path, args in _ENGINES:
        if path and Path(path).exists():
            return path, args
    return None


def _run_account_js(driver: str) -> dict:
    """가짜 DOM/서버에서 실제 loadBrokerAccount를 실행합니다."""
    engine = _engine()
    assert engine, "JavaScript 엔진이 없습니다"
    path, args = engine
    function = "async function loadBrokerAccount() {" + _fn(
        SCRIPT, "loadBrokerAccount") + "\n}"
    prelude = r"""
var BOX = {innerHTML: ""};
var picked = "strategy-a";
var brokerAccountGeneration = 0;
var brokerAccountRetryUntil = 0;
function invalidateBrokerAccount() { brokerAccountGeneration += 1; }
function $(selector) { return BOX; }
function chartStrategy() { return picked; }
function symLabel(ticker, name) { return name || ticker || ""; }
function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/\"/g, "&quot;");
}
var write = (typeof console !== "undefined" && console.log) ? console.log : print;
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(prelude + function + "\n" + driver)
        js = fh.name
    try:
        proc = subprocess.run([path, *args, js], capture_output=True, text=True,
                              timeout=60)
        assert proc.returncode == 0, proc.stderr or proc.stdout
        return json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        Path(js).unlink(missing_ok=True)


def _run_ui_js(functions: list[str], prelude: str, driver: str) -> dict:
    """실제 UI 함수 여러 개를 가짜 DOM/서버 위에서 함께 실행합니다."""
    engine = _engine()
    assert engine, "JavaScript 엔진이 없습니다"
    path, args = engine
    source = "\n".join(_whole_fn(SCRIPT, name) for name in functions)
    common = r"""
var write = (typeof console !== "undefined" && console.log) ? console.log : print;
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(common + prelude + "\n" + source + "\n" + driver)
        js = fh.name
    try:
        proc = subprocess.run([path, *args, js], capture_output=True, text=True,
                              timeout=60)
        assert proc.returncode == 0, proc.stderr or proc.stdout
        return json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        Path(js).unlink(missing_ok=True)


JS_REQUIRED = pytest.mark.skipif(_engine() is None,
                                 reason="JavaScript 엔진이 없습니다")


def test_the_account_tab_does_not_need_a_running_bot():
    assert '"/api/account/broker"' in SERVER, "증권사 계좌를 읽는 경로가 없습니다"
    assert "loadBrokerAccount" in SCRIPT, "화면이 증권사 계좌를 부르지 않습니다"
    show = _fn(SCRIPT, "showPage")
    assert "loadBrokerAccount" in show, (
        "계좌 탭을 열 때 증권사 계좌를 불러오지 않습니다")


def test_the_lookup_is_read_only():
    """조회 경로가 주문을 낼 수 있으면 안 됩니다."""
    from quant.webapp.registry import UserRegistry

    src = inspect.getsource(UserRegistry.broker_account)
    assert "RunMode.DRY_RUN" in src, (
        "계좌 조회가 실거래 어댑터를 세웁니다 — 조회만 하는 경로가 주문을 "
        "낼 수 있는 객체를 들고 다닐 이유가 없습니다.")
    assert "submit" not in src, "조회 경로에서 주문 제출에 닿습니다"


def test_cash_buying_power_is_not_confused_with_legacy_cash():
    """매수가능금액과 예수금은 다른 값이고, 구형 cash는 합계 fallback이 아니다."""
    body = _fn(SCRIPT, "loadBrokerAccount")
    assert "d.cash_buying_power" in body, "현금 매수가능금액을 읽지 않습니다"
    assert "d.investable_assets" in body, "서버가 계산한 운용가능자산을 읽지 않습니다"
    assert "legacyCash = d.cash" in body, "구형 cash 응답의 경계를 드러내지 않습니다"
    assert "money(legacyCash)" in body, "구형 cash를 표시조차 못 합니다"
    assert "investableAssets = d.investable_assets" in body, (
        "운용가능자산을 서버 응답이 아닌 다른 값으로 계산합니다")
    assert "cashBuyingPower +" not in body and "legacyCash +" not in body, (
        "화면이 계좌 총액을 다시 계산합니다 — 통화/출처 계약이 무너집니다")

    # 어댑터도 예수금을 매수가능금액으로 둔갑시키지 않아야 합니다.
    overview = re.search(r"async def account_overview\(self\).*?\n    async def",
                         TOSS, re.S)
    assert overview, "account_overview 를 찾지 못했습니다"
    assert '"cash": None' in overview.group(0), (
        "예수금 자리를 0 으로 채웁니다 — 화면이 '0원' 을 자신 있게 그립니다.")
    assert '"cash_buying_power"' in overview.group(0)
    assert '"investable_assets"' in overview.group(0)


def test_currencies_are_not_added_together():
    """증권사가 합쳐 주지 않는 것을 화면이 합치면 환차손익이 매매 손익에 섞입니다."""
    overview = re.search(r"async def account_overview\(self\).*?\n    async def",
                         TOSS, re.S).group(0)
    assert '"KRW"' in overview and '"USD"' in overview, "통화를 구분하지 않습니다"
    body = _fn(SCRIPT, "loadBrokerAccount")
    assert "Object.entries" in body, (
        "통화별 금액을 하나로 뭉쳐 그립니다 — 원화와 달러가 더해집니다.")


def test_the_bot_book_and_the_account_are_labelled_differently():
    """둘은 다릅니다 — 다른 데서 산 종목, 봇을 켜기 전부터 있던 종목."""
    assert "실제 증권사 잔고" in HTML, "증권사 계좌 패널에 이름이 없습니다"
    source = _fn(SCRIPT, "setBotBookSource")
    assert 'source === "venue"' in source
    assert "봇 운용 장부 · 실계좌 동기화본" in source
    assert 'source === "configured"' in source
    assert "봇 전략 장부 · 설정값 (실계좌 잔고 아님)" in source
    assert HTML.index('id="brokerAcct"') < HTML.index('id="botBookPanel"'), (
        "실제 계좌보다 전략 장부의 큰 숫자가 먼저 보입니다")
    assert 'id="miniBookSource"' in HTML, (
        "상단 숫자의 출처가 보이지 않습니다")
    assert "봇이 들고 있는 것" in HTML, (
        "봇의 장부가 여전히 '보유 종목' 이라고만 적혀 있습니다 — 계좌와 "
        "같은 것으로 읽힙니다.")


@JS_REQUIRED
def test_bot_book_labels_follow_the_reported_capital_source():
    got = _run_ui_js(["setBotBookSource"], r"""
var elements = {};
function $(selector) {
  if (!elements[selector]) elements[selector] = {textContent: ""};
  return elements[selector];
}
""", r"""
var venueTitle = setBotBookSource("venue");
var venueMini = elements["#miniBookSource"].textContent;
var configuredTitle = setBotBookSource("configured");
var configuredMini = elements["#miniBookSource"].textContent;
write(JSON.stringify({venueTitle: venueTitle, venueMini: venueMini,
  configuredTitle: configuredTitle, configuredMini: configuredMini,
  desk: elements["#deskBookKind"].textContent}));
""")
    assert got["venueTitle"] == "봇 운용 장부 · 실계좌 동기화본"
    assert got["venueMini"] == "봇 장부 · 실계좌 동기화"
    assert got["configuredTitle"] == "봇 전략 장부 · 설정값 (실계좌 잔고 아님)"
    assert got["configuredMini"] == "봇 장부 · 설정값"
    assert got["desk"] == got["configuredTitle"]


def test_status_loss_clears_the_old_bot_book():
    clear = _fn(SCRIPT, "clearBotBook")
    assert "renderMini(null)" in clear, "헤더에 옛 전략 금액이 남습니다"
    assert 'hudEquity").textContent = "—"' in clear
    assert 'positions").querySelector("tbody")' in clear
    refresh = _fn(SCRIPT, "refresh")
    assert 'clearBotBook(s.message || "미가동")' in refresh, (
        "봇이 꺼졌는데 마지막 전략 장부가 남습니다")
    assert 'clearBotBook("상태 조회 실패")' in refresh, (
        "상태 조회가 실패했는데 마지막 전략 장부가 현재 값처럼 남습니다")
    mini = _fn(SCRIPT, "renderMini")
    assert 'miniEquity").textContent = "—"' in mini
    assert 'miniChange").textContent = "—"' in mini


def test_stream_payloads_only_request_current_http_truth():
    """WebSocket ring의 옛 payload를 현재 장부/심의로 직접 그리지 않습니다."""
    connect = _fn(SCRIPT, "connect")
    handler = _fn(SCRIPT, "handleStreamEvent")
    assert "handleStreamEvent(e)" in connect
    assert "renderHud(e.payload)" not in connect
    assert "playDeliberation(e.payload)" not in connect
    assert "renderHud" not in handler and "playDeliberation" not in handler
    assert 'e.type === "equity"' in handler
    assert 'e.type === "deliberation"' in handler
    assert "scheduleStreamRefresh()" in handler


@JS_REQUIRED
def test_old_stream_equity_cannot_restore_money_after_status_failure():
    got = _run_ui_js(
        ["refresh", "cancelStreamRefresh", "scheduleStreamRefresh",
         "handleStreamEvent"],
        r"""
var refreshGeneration = 0;
var deskRequestGeneration = 0;
var streamRefreshTimer = null;
var STREAM_REFRESH_DEBOUNCE_MS = 160;
var lastBotError = "";
var runningStrategy = "";
var botState = null;
var deskState = null;
var lastShown = null;
var equity = [];
var me = {email: "now@example.com"};
var MODE_BADGE = {};
var hud = [];
var cleared = [];
var pushed = [];
var timerSeq = 0;
var fakeTimers = {};
function setTimeout(fn, ms) {
  var id = ++timerSeq;
  fakeTimers[id] = {fn: fn, ms: ms, active: true};
  return id;
}
function clearTimeout(id) { if (fakeTimers[id]) fakeTimers[id].active = false; }
function runActiveTimer() {
  var ids = Object.keys(fakeTimers).filter(function (id) { return fakeTimers[id].active; });
  if (ids.length !== 1) throw new Error("active timer count " + ids.length);
  var timer = fakeTimers[ids[0]];
  timer.active = false;
  timer.fn();
}
function $(selector) {
  return {textContent: "", title: "", className: "", classList: {add: function () {}}};
}
function setConn() {}
function setRunning() {}
function note() {}
function adoptRunMode() {}
function runningLabel(value) { return value || "—"; }
function modeKo(value) { return value || ""; }
function renderHud(value) { hud.push(value.equity); }
function clearBotBook(value) { cleared.push(value); }
function drawEquity() {}
async function loadEquity() {}
function renderTape() {}
function renderFlow() {}
function renderDeskAvailability() {}
function playDeliberation() {}
function pushEvent(value) { pushed.push(value.type); }
""",
        r"""
function api(path) {
  if (path === "/api/health") return Promise.resolve({trader_running: false});
  if (path === "/api/status") return Promise.reject(new Error("status down"));
  if (path.indexOf("/api/equity") === 0) return Promise.resolve({points: []});
  if (path.indexOf("/api/universe") === 0) return Promise.resolve({symbols: []});
  if (path === "/api/flow") return Promise.resolve({available: false});
  if (path.indexOf("/api/desk") === 0) return Promise.resolve({deliberations: []});
  throw new Error("unexpected " + path);
}
var actualRefresh = refresh;
var refreshCalls = 0;
var lastRefresh = null;
refresh = function () {
  refreshCalls += 1;
  lastRefresh = actualRefresh();
  return lastRefresh;
};
(async function () {
  handleStreamEvent({type: "equity", payload: {
    equity: 800000, capital_source: "configured"}});
  runActiveTimer();
  await lastRefresh;
  write(JSON.stringify({refreshCalls: refreshCalls, hud: hud, cleared: cleared,
    pushed: pushed, timer: streamRefreshTimer}));
})().catch(function (e) { write(JSON.stringify({error: String(e)})); });
""",
    )
    assert "error" not in got, got
    assert got == {
        "refreshCalls": 1,
        "hud": [],
        "cleared": ["상태 조회 실패"],
        "pushed": ["equity"],
        "timer": None,
    }


@JS_REQUIRED
def test_old_strategy_stream_deliberation_fetches_and_plays_only_current_b():
    got = _run_ui_js(
        ["refresh", "cancelDeliberationReplay", "cancelStreamRefresh",
         "scheduleStreamRefresh", "handleStreamEvent"],
        r"""
var refreshGeneration = 0;
var deskRequestGeneration = 0;
var playGeneration = 0;
var playing = false;
var lastPlayed = null;
var streamRefreshTimer = null;
var STREAM_REFRESH_DEBOUNCE_MS = 160;
var lastBotError = "";
var runningStrategy = "";
var botState = null;
var deskState = null;
var lastShown = null;
var equity = [];
var me = {email: "now@example.com"};
var MODE_BADGE = {live: "실거래"};
var played = [];
var hud = [];
var timerFn = null;
function setTimeout(fn) { timerFn = fn; return 1; }
function clearTimeout() { timerFn = null; }
function $(selector) {
  return {textContent: "", title: "", className: "",
    classList: {add: function () {}, remove: function () {}}};
}
function setConn() {}
function setRunning() {}
function note() {}
function adoptRunMode() {}
function alignRunningStrategySelection() { return false; }
function runningLabel(value) { return value || "—"; }
function modeKo(value) { return value || ""; }
function renderHud(value) { hud.push(value.equity); }
function clearBotBook() {}
function drawEquity() {}
async function loadEquity() {}
function renderTape() {}
function renderFlow() {}
function renderDeskAvailability() {}
function playDeliberation(value) { played.push(value.decided_at); }
function resetFloor() {}
function syncPlayBar() {}
function pushEvent() {}
""",
        r"""
function api(path) {
  if (path === "/api/health") return Promise.resolve({trader_running: true});
  if (path === "/api/status") return Promise.resolve({running: true,
    strategy: "strategy-b", mode: "live",
    portfolio: {equity: 426319, capital_source: "venue"}});
  if (path.indexOf("/api/equity") === 0) return Promise.resolve({points: []});
  if (path.indexOf("/api/universe") === 0) return Promise.resolve({symbols: []});
  if (path === "/api/flow") return Promise.resolve({available: false});
  if (path.indexOf("/api/desk") === 0) return Promise.resolve({
    marker: "strategy-b", deliberations: [{decided_at: "B-current"}]});
  throw new Error("unexpected " + path);
}
var actualRefresh = refresh;
var refreshCalls = 0;
var lastRefresh = null;
refresh = function () {
  refreshCalls += 1;
  lastRefresh = actualRefresh();
  return lastRefresh;
};
(async function () {
  cancelDeliberationReplay();
  handleStreamEvent({type: "deliberation", payload: {
    decided_at: "A-old", strategy: "strategy-a"}});
  var run = timerFn;
  timerFn = null;
  run();
  await lastRefresh;
  write(JSON.stringify({refreshCalls: refreshCalls, played: played, hud: hud,
    marker: deskState.marker}));
})().catch(function (e) { write(JSON.stringify({error: String(e)})); });
""",
    )
    assert "error" not in got, got
    assert got == {
        "refreshCalls": 1,
        "played": ["B-current"],
        "hud": [426319],
        "marker": "strategy-b",
    }


@JS_REQUIRED
def test_reconnect_ring_fifty_events_debounce_to_one_current_refresh():
    got = _run_ui_js(
        ["cancelStreamRefresh", "scheduleStreamRefresh", "handleStreamEvent"],
        r"""
var streamRefreshTimer = null;
var STREAM_REFRESH_DEBOUNCE_MS = 160;
var me = {email: "now@example.com"};
var timerSeq = 0;
var fakeTimers = {};
var refreshCalls = 0;
var hudCalls = 0;
var playCalls = 0;
function setTimeout(fn, ms) {
  var id = ++timerSeq;
  fakeTimers[id] = {fn: fn, ms: ms, active: true};
  return id;
}
function clearTimeout(id) { if (fakeTimers[id]) fakeTimers[id].active = false; }
function refresh() { refreshCalls += 1; }
function renderHud() { hudCalls += 1; }
function playDeliberation() { playCalls += 1; }
function pushEvent() {}
""",
        r"""
for (var i = 0; i < 50; i += 1) {
  handleStreamEvent({type: i % 2 ? "deliberation" : "equity",
    payload: {equity: 800000, decided_at: "old-" + i}});
}
var active = Object.keys(fakeTimers).filter(function (id) {
  return fakeTimers[id].active;
});
var before = refreshCalls;
fakeTimers[active[0]].active = false;
fakeTimers[active[0]].fn();
write(JSON.stringify({active: active.length, before: before, after: refreshCalls,
  hudCalls: hudCalls, playCalls: playCalls, timer: streamRefreshTimer}));
""",
    )
    assert got == {
        "active": 1,
        "before": 0,
        "after": 1,
        "hudCalls": 0,
        "playCalls": 0,
        "timer": None,
    }


@JS_REQUIRED
def test_a_slow_old_status_cannot_restore_stale_strategy_money():
    got = _run_ui_js(["refresh"], r"""
var refreshGeneration = 0;
var deskRequestGeneration = 0;
var lastBotError = "";
var runningStrategy = "";
var botState = null;
var deskState = null;
var lastShown = null;
var equity = [];
var MODE_BADGE = {live: "실거래"};
var elements = {};
var hud = [];
var cleared = [];
function $(selector) {
  if (!elements[selector]) elements[selector] = {
    textContent: "", title: "", className: "", classList: {add: function () {}}
  };
  return elements[selector];
}
function setConn() {}
function setRunning() {}
function note() {}
function adoptRunMode() {}
function alignRunningStrategySelection() { return false; }
function runningLabel(value) { return value || "—"; }
function modeKo(value) { return value || ""; }
function renderHud(value) { hud.push(value.equity); }
function clearBotBook(value) { cleared.push(value); }
function drawEquity() {}
async function loadEquity() {}
function renderTape() {}
function renderFlow() {}
function renderDeskAvailability() {}
function playDeliberation() {}
""", r"""
var pendingStatus = [];
function api(path) {
  if (path === "/api/health") return Promise.resolve({trader_running: true});
  if (path === "/api/status") return new Promise(function (resolve) {
    pendingStatus.push(resolve);
  });
  if (path.indexOf("/api/equity") === 0) return Promise.resolve({points: []});
  if (path.indexOf("/api/universe") === 0) return Promise.resolve({symbols: []});
  if (path === "/api/flow") return Promise.resolve({available: false});
  if (path.indexOf("/api/desk") === 0) return Promise.resolve({deliberations: []});
  throw new Error("unexpected " + path);
}
function status(equity, source) {
  return {running: true, strategy: "kr-toss", mode: "live",
    portfolio: {equity: equity, capital_source: source}};
}
async function waitFor(count) {
  for (var i = 0; i < 100 && pendingStatus.length < count; i += 1) {
    await Promise.resolve();
  }
  if (pendingStatus.length < count) throw new Error("status request missing");
}
(async function () {
  var oldRequest = refresh();
  await waitFor(1);
  var newRequest = refresh();
  await waitFor(2);
  pendingStatus[1](status(426319, "venue"));
  await newRequest;
  pendingStatus[0](status(800000, "configured"));
  await oldRequest;
  write(JSON.stringify({hud: hud, cleared: cleared}));
})().catch(function (e) { write(JSON.stringify({error: String(e)})); });
""")
    assert "error" not in got, got
    assert got["hud"] == [426319]
    assert got["cleared"] == []


@JS_REQUIRED
def test_strategy_switch_discards_a_desk_response_that_is_still_in_flight():
    got = _run_ui_js(["refresh", "cancelDeliberationReplay"], r"""
var refreshGeneration = 0;
var deskRequestGeneration = 0;
var playGeneration = 0;
var playing = false;
var lastPlayed = null;
var lastBotError = "";
var runningStrategy = "";
var botState = null;
var deskState = {marker: "before-switch"};
var lastShown = null;
var equity = [];
var MODE_BADGE = {};
var played = [];
var elements = {};
function $(selector) {
  if (!elements[selector]) elements[selector] = {
    textContent: "", title: "", className: "",
    classList: {add: function () {}, remove: function () {}}
  };
  return elements[selector];
}
function setConn() {}
function setRunning() {}
function note() {}
function adoptRunMode() {}
function runningLabel(value) { return value || "—"; }
function modeKo(value) { return value || ""; }
function renderHud() {}
function clearBotBook() {}
function drawEquity() {}
async function loadEquity() {}
function renderTape() {}
function renderFlow() {}
function renderDeskAvailability() {}
function playDeliberation(value) { played.push(value.decided_at); }
function resetFloor() {}
function syncPlayBar() {}
""", r"""
var pendingDesk = [];
function api(path) {
  if (path === "/api/health") return Promise.resolve({trader_running: false});
  if (path === "/api/status") return Promise.resolve({running: false});
  if (path.indexOf("/api/equity") === 0) return Promise.resolve({points: []});
  if (path.indexOf("/api/universe") === 0) return Promise.resolve({symbols: []});
  if (path === "/api/flow") return Promise.resolve({available: false});
  if (path.indexOf("/api/desk") === 0) return new Promise(function (resolve) {
    pendingDesk.push(resolve);
  });
  throw new Error("unexpected " + path);
}
async function waitForDesk() {
  for (var i = 0; i < 100 && !pendingDesk.length; i += 1) {
    await Promise.resolve();
  }
  if (!pendingDesk.length) throw new Error("desk request missing");
}
(async function () {
  var request = refresh();
  await waitForDesk();
  cancelDeliberationReplay();
  pendingDesk[0]({marker: "old-strategy", deliberations: [{decided_at: "old"}]});
  await request;
  write(JSON.stringify({played: played, marker: deskState.marker}));
})().catch(function (e) { write(JSON.stringify({error: String(e)})); });
""")
    assert "error" not in got, got
    assert got == {"played": [], "marker": "before-switch"}


def test_a_broken_lookup_says_what_went_wrong():
    body = _fn(SCRIPT, "loadBrokerAccount")
    assert "d.error" in body, "증권사가 답을 안 줬을 때를 구분하지 않습니다"
    assert "supported === false" in body, "연동이 안 된 경우를 구분하지 않습니다"


@JS_REQUIRED
def test_real_values_replace_old_strategy_money_without_local_fallback():
    got = _run_account_js(r"""
async function api() {
  return {
    supported: true, source: "toss",
    cash_buying_power: {KRW: 420000},
    market_value: {KRW: 0}, investable_assets: {KRW: 420000},
    cash: null, invested: {KRW: 0}, pnl: {KRW: "not-a-number"},
    daily_pnl: {}, items: []
  };
}
(async function () {
  BOX.innerHTML = "옛 봇 장부 800,000원";
  await loadBrokerAccount();
  write(JSON.stringify({html: BOX.innerHTML}));
})().catch(function (e) { write(JSON.stringify({error: String(e)})); });
""")
    assert "error" not in got, got
    html = got["html"]
    assert "현금 매수가능금액" in html and "420,000원" in html
    assert "보유주식 평가금액" in html and "운용 가능 자산" in html
    assert "800,000" not in html, "옛 전략 장부를 실제 계좌 fallback으로 남깁니다"
    assert "NaN" not in html and "조회 불가" in html


@JS_REQUIRED
def test_non_number_money_fields_are_unavailable_not_zero_won():
    got = _run_account_js(r"""
async function api() {
  return {
    supported: true, source: "toss",
    cash_buying_power: {KRW: false},
    market_value: {KRW: []}, investable_assets: {KRW: " "},
    cash: null, invested: {}, pnl: {}, daily_pnl: {}, items: []
  };
}
(async function () {
  await loadBrokerAccount();
  write(JSON.stringify({html: BOX.innerHTML}));
})().catch(function (e) { write(JSON.stringify({error: String(e)})); });
""")
    assert "error" not in got, got
    html = got["html"]
    assert html.count("조회 불가") >= 6
    assert "0원" not in html


@JS_REQUIRED
@pytest.mark.parametrize("reply", ["throw", "unsupported", "error", "invalid"])
def test_lookup_failure_clears_numbers_instead_of_showing_a_fallback(reply):
    if reply == "throw":
        api = 'async function api() { throw new Error("timeout"); }'
    elif reply == "unsupported":
        api = ('async function api() { return {supported:false, '
               'message:"계좌를 찾지 못했습니다"}; }')
    elif reply == "error":
        api = ('async function api() { return {supported:true, '
               'error:"증권사 조회 실패"}; }')
    else:
        api = 'async function api() { return null; }'
    got = _run_account_js(api + r"""
(async function () {
  BOX.innerHTML = "800,000원";
  await loadBrokerAccount();
  write(JSON.stringify({html: BOX.innerHTML}));
})().catch(function (e) { write(JSON.stringify({error: String(e)})); });
""")
    assert "error" not in got, got
    assert "800,000" not in got["html"]
    assert ("실패" in got["html"] or "찾지 못했습니다" in got["html"]
            or "불러오지 못했습니다" in got["html"])


@JS_REQUIRED
def test_a_late_response_from_the_old_strategy_cannot_replace_the_new_account():
    got = _run_account_js(r"""
var pending = [];
function api(url) {
  return new Promise(function (resolve) { pending.push({url: url, resolve: resolve}); });
}
function account(value) {
  return {supported:true, source:"toss", cash_buying_power:{KRW:value},
    market_value:{KRW:0}, investable_assets:{KRW:value}, cash:null,
    invested:{KRW:0}, pnl:{KRW:0}, daily_pnl:{KRW:0}, items:[]};
}
(async function () {
  picked = "strategy-a";
  var oldRequest = loadBrokerAccount();
  await Promise.resolve();
  picked = "strategy-b";
  var newRequest = loadBrokerAccount();
  await Promise.resolve();
  pending[1].resolve(account(420000));
  await newRequest;
  pending[0].resolve(account(800000));
  await oldRequest;
  write(JSON.stringify({html: BOX.innerHTML, urls: pending.map(function (x) { return x.url; })}));
})().catch(function (e) { write(JSON.stringify({error: String(e)})); });
""")
    assert "error" not in got, got
    assert "420,000원" in got["html"]
    assert "800,000원" not in got["html"]
    assert "strategy-a" in got["urls"][0] and "strategy-b" in got["urls"][1]


def test_each_holding_shows_what_it_is_worth():
    """계좌를 볼 때 가장 먼저 찾는 숫자입니다.

    수량과 현재가만 놓고 사람이 곱하게 두면, 그건 계좌 화면이 아니라
    계산 문제입니다.
    """
    body = _fn(SCRIPT, "loadBrokerAccount")
    assert "평가금액" in body, "종목별 평가금액 열이 없습니다"
    assert "money(x.market_value)" in body, "종목별 평가금액을 그리지 않습니다"


def test_holdings_keep_native_table_layout_without_changing_profile_cards():
    """공유 class 이름 때문에 holdings의 thead와 tbody가 다른 grid가 되면 안 됩니다."""
    body = _fn(SCRIPT, "loadBrokerAccount")
    assert re.search(
        r'<table class="acct".*?<thead><tr>.*?</thead><tbody>\$\{rows\}</tbody></table>',
        body,
        re.S,
    ), "holdings가 thead/tbody를 가진 실제 table DOM이 아닙니다"
    table_rule = re.search(r"table\.acct\{([^}]*)\}", CSS, re.S)
    assert table_rule and "display:table" in table_rule.group(1), (
        "profile의 .acct grid가 holdings table까지 덮어 열 정렬을 깨뜨립니다")
    profile_rule = re.search(r"(?<!table)\.acct\{([^}]*)\}", CSS, re.S)
    assert profile_rule and "display:grid" in profile_rule.group(1), (
        "holdings를 고치며 profile 카드의 grid 배치를 없앴습니다")


@JS_REQUIRED
def test_malformed_holdings_are_skipped_without_hiding_valid_account_truth():
    got = _run_account_js(r"""
async function api() {
  return {
    supported: true, source: "toss",
    cash_buying_power: {KRW: 426319}, market_value: {KRW: 0},
    investable_assets: {KRW: 426319}, cash: null,
    invested: {KRW: 0}, pnl: {KRW: 0}, daily_pnl: {KRW: 0},
    items: [null, false, "", [], {}, {ticker: {bad: true}},
      {ticker: "005930", name: "삼성전자", quantity: false,
       avg_price: [], last_price: " ", market_value: {KRW: 0},
       pnl: {KRW: 0}, pnl_pct: false}]
  };
}
(async function () {
  await loadBrokerAccount();
  write(JSON.stringify({html: BOX.innerHTML}));
})().catch(function (e) { write(JSON.stringify({error: String(e), html: BOX.innerHTML})); });
""")
    assert "error" not in got, got
    html = got["html"]
    assert "426,319원" in html and "보유주식 평가금액</b><span>0원" in html
    assert "삼성전자" in html
    assert "[object Object]" not in html
    assert html.count("<tbody><tr>") == 1
    assert html.count("<td>—</td>") >= 3, "비수치 holding 값을 0으로 둔갑시킵니다"


def test_holdings_scroller_and_summary_rows_are_accessible_and_mobile_safe():
    body = _fn(SCRIPT, "loadBrokerAccount")
    assert 'class="acct-scroll"' in body
    assert 'role="region"' in body and 'tabindex="0"' in body
    assert 'aria-label="실제 증권사 보유주식 표.' in body
    assert '<caption class="sr-only">실제 증권사 보유주식</caption>' in body
    assert body.count('scope="col"') == 6
    assert '#brokerAcct .r{display:grid' in CSS
    assert '.acct-scroll{' in CSS and "overflow-x:auto" in CSS
    assert ".acct-scroll:focus-visible" in CSS
    assert ".acct-scroll table.acct{min-width:640px}" in CSS
    assert 'id="brokerAcct" role="status"' not in HTML, (
        "표 전체를 atomic status로 읽어 screen reader에 매 행을 강제 공지합니다")


@pytest.mark.parametrize("viewport_width", [320, 360, 520])
def test_mobile_widths_keep_mini_source_visible_and_holdings_scrollable(viewport_width):
    assert viewport_width <= 900
    start = CSS.rfind("@media(max-width:900px){")
    end = CSS.find("\n}", start)
    assert start >= 0 and end > start, "900px 이하 mobile layout 규칙이 없습니다"
    rules = CSS[start:end]
    assert "grid-template-rows:auto auto auto" in rules
    assert ".top-in>.mini{grid-column:1/-1;grid-row:2" in rules
    assert "overflow:visible;justify-content:flex-start;flex-wrap:wrap" in rules
    assert "text-overflow:clip;white-space:normal" in rules
    assert ".top-in>.pagetabs{grid-column:1/-1;grid-row:3" in rules
    assert "overflow-x:auto" in CSS and "min-width:640px" in CSS


def test_a_us_holding_is_not_drawn_in_won():
    """종목 금액은 그 종목의 통화 하나뿐이라 코드가 안 붙어 옵니다.

    붙여 두지 않으면 화면이 원화인지 달러인지 모른 채 찍고, 애플 $250 이
    250원으로 보입니다.
    """
    assert "def _named(" in TOSS, "종목 금액에 통화를 붙이는 곳이 없습니다"
    from quant.brokerage.toss_broker import _named

    assert _named({"amount": "1310.50"}, "USD") == {"USD": 1310.5}
    assert _named({"amount": "7560000"}, "KRW") == {"KRW": 7560000.0}
    # 통화가 비어 오면 국내로 봅니다 — 토스의 기본 시장입니다.
    assert _named({"amount": "1000"}, None) == {"KRW": 1000.0}
    # 못 읽는 값은 0 이 아니라 없음입니다.
    assert _named({"amount": "??"}, "KRW") == {}
    assert _named(None, "KRW") == {}
