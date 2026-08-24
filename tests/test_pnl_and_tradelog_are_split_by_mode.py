"""실현 수익과 매매 기록은 **모의와 실거래를 한 숫자로 합치지 않는다.**

모의로 번 돈은 실제로 번 돈이 아닙니다. 저장소(`pnl_by_period`, `trade_log`)와
`/api/pnl`·`/api/tradelog` 는 진작부터 `mode` 를 받고 있었는데, 화면만 그
인자를 한 번도 붙이지 않았습니다. 그래서 "실현 수익" 칸은 모의 이익과 실거래
손실을 더한 값이었고, 매매 기록은 두 종류의 체결을 한 표에 섞어 놓았습니다.
모의에서 크게 벌고 실거래에서 잃은 사람이 자기가 벌고 있다고 믿게 됩니다.

여기서 검사하는 것은 소스에 어떤 글자가 있는가가 아니라 **화면이 실제로 무엇을
청하고 무엇을 그리는가** 입니다. `index.html` 의 스크립트를 잘라내지 않고
**통째로** 자바스크립트 엔진에 태우고(`chart.js` 도 같이 — 페이지가 함께 읽는
파일입니다), 가짜 서버를 물린 뒤 나간 주소와 그려진 DOM 을 봅니다. 조각만
잘라 태우면 잘라낸 자리에 있는 호출부가 검사 밖으로 빠집니다.

**언제 이 파일이 안 통하는가**: 스텁 DOM 에는 스타일 엔진이 없습니다. 여기서
`hidden` 이나 클래스를 단언하면 CSS 가 그것을 무효로 만들어도 알 수 없습니다
(저자 오리진의 `display` 선언은 UA 의 `[hidden]{display:none}` 을 이깁니다).
그래서 이 파일은 **보이냐 안 보이냐를 한 번도 묻지 않습니다** — 어떤 주소가
나갔고 어떤 글자가 찍혔는지만 묻습니다. 그 둘은 스타일과 무관합니다.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "quant" / "api" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
SCRIPT = re.search(r"<script>\n(.*?)</script>", HTML, re.S).group(1)

#: 엔진. macOS 에는 jsc 가, 리눅스에는 node 가 있는 편입니다. 둘 다 없으면
#: 이 파일의 행동 검사는 건너뛰고, 맨 아래 정적 검사만 남습니다.
_JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"


def _engine() -> str | None:
    if Path(_JSC).exists():
        return _JSC
    return shutil.which("node")


# ── 브라우저 흉내 ────────────────────────────────────────────────────────
# 그리지 않습니다. 스크립트가 만지는 것을 기록만 합니다. 여기에 없는 API 를
# 스크립트가 쓰기 시작하면 이 파일이 먼저 깨집니다 — 그게 맞습니다. 조용히
# 절반만 태우고 통과하는 것보다 훨씬 낫습니다.
DOM = r"""
var __say = (typeof print === "function") ? print : console.log;
var __log = {urls: []};

function __El() {
  return {
    innerHTML: "", textContent: "", value: "", hidden: false, className: "",
    title: "", disabled: false, checked: false, inert: false, type: "",
    onclick: null, onchange: null, oninput: null, onsubmit: null, onkeydown: null,
    children: [], options: [], dataset: {}, files: [],
    clientWidth: 600, clientHeight: 300, offsetWidth: 600, offsetHeight: 300,
    width: 600, height: 300,
    style: {setProperty: function () {}, removeProperty: function () {}},
    classList: (function () {
      var s = {};
      return {
        add: function () { for (var i = 0; i < arguments.length; i++) s[arguments[i]] = 1; },
        remove: function () { for (var i = 0; i < arguments.length; i++) delete s[arguments[i]]; },
        toggle: function (c, on) {
          if (on === undefined) { if (s[c]) delete s[c]; else s[c] = 1; }
          else if (on) s[c] = 1; else delete s[c];
        },
        contains: function (c) { return !!s[c]; }
      };
    })(),
    setAttribute: function () {}, removeAttribute: function () {},
    getAttribute: function () { return ""; }, hasAttribute: function () { return false; },
    appendChild: function (c) { this.children.push(c); return c; },
    removeChild: function () {}, remove: function () {}, replaceChildren: function () {},
    insertAdjacentHTML: function (where, html) {
      if (where === "beforeend") this.innerHTML += html;
      else this.innerHTML = html + this.innerHTML;
    },
    addEventListener: function () {}, removeEventListener: function () {},
    dispatchEvent: function (ev) {
      var h = this["on" + (ev && ev.type)];
      if (typeof h === "function") h.call(this, ev);
      return true;
    },
    focus: function () {}, blur: function () {}, click: function () {},
    scrollIntoView: function () {}, closest: function () { return null; },
    querySelector: function () { return null; }, querySelectorAll: function () { return []; },
    getBoundingClientRect: function () { return {width: 600, height: 44, top: 0, left: 0}; },
    getContext: function () {
      return {setTransform: function () {}, clearRect: function () {},
        beginPath: function () {}, moveTo: function () {}, lineTo: function () {},
        stroke: function () {}, fill: function () {}, closePath: function () {},
        arc: function () {}, fillRect: function () {}, fillText: function () {},
        measureText: function () { return {width: 10}; },
        save: function () {}, restore: function () {},
        createLinearGradient: function () { return {addColorStop: function () {}}; }};
    },
    resize: function () {}
  };
}

var __els = {};
function __get(sel) {
  if (!__els[sel]) __els[sel] = __El();
  return __els[sel];
}

var document = {
  readyState: "complete", title: "", cookie: "", activeElement: null,
  documentElement: __El(), body: __El(), head: __El(),
  getElementById: function (id) { return __get("#" + id); },
  querySelector: function (s) { return __get(s); },
  querySelectorAll: function () { return []; },
  createElement: function (t) { var e = __El(); e.tagName = t; return e; },
  createTextNode: function () { return __El(); },
  addEventListener: function () {}, removeEventListener: function () {},
  dispatchEvent: function () { return true; }
};
document.body.children = [];

function Event(type) { this.type = type; }
Event.prototype.preventDefault = function () {};
Event.prototype.stopPropagation = function () {};
function CustomEvent(type, init) { this.type = type; this.detail = (init || {}).detail; }
CustomEvent.prototype = Event.prototype;

var location = {protocol: "https:", host: "x.test", href: "https://x.test/",
  search: "", pathname: "/", origin: "https://x.test", reload: function () {}};
var navigator = {userAgent: "stub",
  serviceWorker: {register: function () { return {catch: function () {}}; }},
  clipboard: {writeText: function () { return Promise.resolve(); }}};
var devicePixelRatio = 1;
var localStorage = {_d: {},
  getItem: function (k) { return this._d[k] === undefined ? null : this._d[k]; },
  setItem: function (k, v) { this._d[k] = String(v); },
  removeItem: function (k) { delete this._d[k]; }};
var sessionStorage = localStorage;
function WebSocket() {
  this.close = function () {}; this.send = function () {};
  this.onmessage = null; this.onopen = null; this.onclose = null; this.onerror = null;
}
function setInterval() { return 0; }
function clearInterval() {}
function setTimeout() { return 0; }
function clearTimeout() {}
function requestAnimationFrame() { return 0; }
function cancelAnimationFrame() {}
function addEventListener() {}
function removeEventListener() {}
function matchMedia() { return {matches: false, addListener: function () {},
  addEventListener: function () {}}; }
function getComputedStyle() { return {getPropertyValue: function () { return ""; }}; }
function alert() {}
function confirm() { return true; }
function URLSearchParams() { this.get = function () { return null; }; }
function MutationObserver() { this.observe = function () {}; this.disconnect = function () {}; }
function IntersectionObserver() { this.observe = function () {}; this.disconnect = function () {}; }
function ResizeObserver() { this.observe = function () {}; this.disconnect = function () {}; }

/* 가짜 서버. `__routes` 가 undefined 를 주면 그 요청은 **영원히 답이 없습니다**
   — 부팅 코드가 조용히 그 자리에 멈춰 서고, 시나리오가 부르는 것만 실제로
   돕니다. `{__hold: body}` 를 주면 답을 붙잡아 뒀다가 나중에 풀어 줍니다. */
var __routes = null;
var __held = [];
function __reply(body) {
  return {ok: true, status: 200,
    json: function () { return Promise.resolve(body); },
    text: function () { return Promise.resolve(JSON.stringify(body)); },
    headers: {get: function () { return "application/json"; }}};
}
function fetch(url) {
  __log.urls.push(url);
  var body = __routes ? __routes(url) : undefined;
  if (body === undefined) return new Promise(function () {});
  if (body && body.__hold) {
    return new Promise(function (res) {
      __held.push(function () { res(__reply(body.__hold)); });
    });
  }
  return Promise.resolve(__reply(body));
}

/* `chart.js` 는 `window.PriceChart` 로 자기를 내놓습니다. node 에서는 파일이
   모듈로 감싸여 `this` 가 전역이 아니므로, 진짜 전역을 집어 줘야 두 엔진에서
   같은 일이 벌어집니다. */
var window = (typeof globalThis === "object") ? globalThis : this;
window.document = document;
window.location = location;
window.navigator = navigator;
window.fetch = fetch;
"""

# ── 시나리오 ─────────────────────────────────────────────────────────────
DRIVER = r"""
function __urls() { return __log.urls.slice(); }
function __clear() { __log.urls.length = 0; }
function __el(id) { return document.getElementById(id); }
async function __settle() { for (var i = 0; i < 12; i++) await Promise.resolve(); }

function __pnl(n) {
  var one = {pnl: n, trades: 1, wins: 1, win_rate: 0.5, fees: 0, since: "2026-01-01"};
  return {periods: {today: one, week: one, month: one, year: one},
          modes: ["dry_run", "live"]};
}
function __log1(tag, n, total) {
  return {total: total === undefined ? 1 : total, offset: 0,
          trades: [{symbol: tag, side: "buy", quantity: 1, entry_price: 1,
                    exit_price: 2, pnl: n, pnl_pct: 0.5,
                    exit_ts: "2026-01-02T03:04:05+00:00", exit_tag: tag}]};
}
function __serve(pnlBody, logBody) {
  __routes = function (u) {
    if (u.indexOf("/api/pnl") === 0) return pnlBody;
    if (u.indexOf("/api/tradelog") === 0) return logBody;
    return undefined;
  };
}
function __snap(o, key) {
  o[key + "_grid"] = __el("pnlGrid").innerHTML;
  o[key + "_rows"] = __el("tradeLog").innerHTML;
  o[key + "_pnl_label"] = __el("pnlModeShown").textContent;
  o[key + "_log_label"] = __el("tradeModeShown").textContent;
}

/* 시나리오 도중에 무엇이 터져도 관측값은 내보냅니다. 그래야 실패한 검사가
   "여기까지는 이랬다" 를 말해 줍니다 — 하네스가 통째로 죽어 버리면 화면이
   무엇을 잘못했는지가 아니라 하네스가 죽었다는 것만 남습니다. */
var __out = {};
(async function () {
  var out = __out;
  try {
  var sel = __el("pnlMode");
  out.picker_wired = typeof sel.onchange === "function";

  // ① 부팅 — 아무것도 안 고른 상태에서 나가는 두 요청
  __serve(__pnl(987), __log1("DRYSYM", 987));
  __clear();
  await loadPnl();
  await loadTradeLog();
  out.boot_urls = __urls();
  __snap(out, "boot");

  // ② 실거래로 바꾼다 — 서버는 완전히 다른 숫자를 준다
  __serve(__pnl(-777), __log1("LIVESYM", -777));
  __clear();
  sel.value = "live";
  sel.onchange();
  await __settle();
  out.live_urls = __urls();
  __snap(out, "live");

  // ③ "더 보기" 도 같은 모드여야 한다
  __serve(__pnl(-777), __log1("LIVESYM2", -777, 2));
  __clear();
  __el("tradeMore").onclick();
  await __settle();
  out.more_urls = __urls();

  // ④ 늦게 온 남의 모드 응답. 모의를 청해 놓고 실거래로 돌아온 뒤,
  //    붙잡아 둔 모의 응답을 풀어 준다.
  __routes = function (u) {
    if (u.indexOf("/api/pnl") === 0) return {__hold: __pnl(999999)};
    if (u.indexOf("/api/tradelog") === 0) return {__hold: __log1("LATEDRY", 999999)};
    return undefined;
  };
  sel.value = "dry_run"; sel.onchange();
  await __settle();
  __serve(__pnl(-777), __log1("LIVESYM", -777));
  sel.value = "live"; sel.onchange();
  await __settle();
  __held.splice(0).forEach(function (f) { f(); });
  await __settle();
  __snap(out, "late");

  // ⑤ 봇이 돌기 시작한 모드를 화면이 따라가는가. 지금 나는 체결이 표에
  //    없는 것이 제일 나쁩니다 — 그래서 서버가 말하는 실행 모드를 봅니다.
  sel.value = "dry_run"; sel.onchange();
  await __settle();
  __clear();
  __routes = function (u) {
    if (u.indexOf("/api/health") === 0) return {trader_running: true};
    if (u.indexOf("/api/status") === 0) return {running: true, mode: "live",
                                                strategy: "kr-toss-desk"};
    if (u.indexOf("/api/pnl") === 0) return __pnl(-777);
    if (u.indexOf("/api/tradelog") === 0) return __log1("LIVESYM", -777);
    return undefined;
  };
  refresh();
  await __settle();
  out.running_urls = __urls();
  out.running_pick = __el("pnlMode").value;
  } catch (e) { out.crash = String(e && e.stack || e); }
  __say("<<<" + JSON.stringify(out) + ">>>");
})();
"""


@pytest.fixture(scope="module")
def screen() -> dict:
    """스크립트를 통째로 태우고, 시나리오가 남긴 관측값을 돌려준다."""
    engine = _engine()
    if engine is None:
        pytest.skip("자바스크립트 엔진이 없습니다")
    chart = (STATIC / "chart.js").read_text(encoding="utf-8")
    prog = "\n".join([DOM, chart, SCRIPT, DRIVER])
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(prog)
        path = fh.name
    try:
        proc = subprocess.run([engine, path], capture_output=True, text=True,
                              timeout=120)
    finally:
        Path(path).unlink(missing_ok=True)
    body = re.search(r"<<<(.*?)>>>", proc.stdout, re.S)
    assert body, ("화면 스크립트가 시나리오를 끝내지 못했습니다 "
                  f"(rc={proc.returncode}):\n{(proc.stdout + proc.stderr)[:3000]}")
    return json.loads(body.group(1))


def _mode_of(url: str) -> str | None:
    m = re.search(r"[?&]mode=([^&]*)", url)
    return m.group(1) if m else None


def _asked(urls: list[str], prefix: str) -> list[str]:
    return [u for u in urls if u.startswith(prefix)]


def _obs(screen: dict, key: str):
    """관측값 하나. 시나리오가 거기까지 못 갔으면 못 간 이유를 그대로 보여준다."""
    assert key in screen, (f"시나리오가 {key} 까지 가지 못했습니다 — "
                           + screen.get("crash", "이유가 기록되지 않았습니다"))
    return screen[key]


# ── 요청 ─────────────────────────────────────────────────────────────────

def _without_comments(src: str) -> str:
    """`//` 와 `/* */` 를 같은 길이의 공백으로. 줄 번호와 자리를 유지합니다.

    문자열 안의 `//` 는 주석이 아니지만, 이 검사가 찾는 것은 `/api/...` 라
    그 구분까지 할 필요는 없습니다 — 문자열 안의 주소는 진짜 호출부입니다.
    """
    out, i, n = [], 0, len(src)
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
        else:
            out.append(src[i])
            i += 1
            continue
        out.append("".join(c if c == "\n" else " " for c in src[i:j]))
        i = j
    return "".join(out)

def test_the_picker_is_wired(screen):
    """선택기가 없거나 아무 데도 연결돼 있지 않으면 아래 검사가 전부 무의미합니다."""
    assert _obs(screen, "picker_wired"), "#pnlMode 의 onchange 가 없습니다"


@pytest.mark.parametrize("prefix", ["/api/pnl", "/api/tradelog"])
def test_no_request_goes_out_without_a_mode(screen, prefix):
    """모드 없는 요청 하나면 화면은 다시 모의와 실거래를 더한 값을 받습니다."""
    asked = _asked(_obs(screen, "boot_urls"), prefix)
    assert asked, f"{prefix} 를 아예 부르지 않았습니다"
    for url in asked:
        assert _mode_of(url), f"모드 없이 나간 요청: {url}"


def test_the_numbers_and_the_table_ask_for_the_same_mode(screen):
    """실현 수익은 모의를, 매매 기록은 실거래를 세면 둘을 맞춰 볼 수 없습니다."""
    for key in ("boot_urls", "live_urls"):
        urls = _obs(screen, key)
        pnl = {_mode_of(u) for u in _asked(urls, "/api/pnl")}
        log = {_mode_of(u) for u in _asked(urls, "/api/tradelog")}
        assert pnl == log, f"{key}: 실현 수익 {pnl} vs 매매 기록 {log}"


def test_more_pages_stay_in_the_same_mode(screen):
    """"더 보기" 가 모드를 잃으면 그 40줄이 한 표 안에서 앞줄과 섞입니다."""
    asked = _asked(_obs(screen, "more_urls"), "/api/tradelog")
    assert asked, '"더 보기" 가 아무것도 부르지 않았습니다'
    for url in asked:
        assert _mode_of(url) == "live", url


# ── 화면 ─────────────────────────────────────────────────────────────────
def test_switching_replaces_the_numbers_it_does_not_add_to_them(screen):
    """모의 +987 을 보다가 실거래로 옮기면 화면에 987 이 남아 있으면 안 됩니다."""
    grid, rows = _obs(screen, "live_grid"), _obs(screen, "live_rows")
    assert "-777" in grid, grid
    assert "987" not in grid, grid
    assert "LIVESYM" in rows, rows
    assert "DRYSYM" not in rows, rows


@pytest.mark.parametrize("key,word", [("boot", "모의매매"), ("live", "실거래")])
def test_the_word_and_the_numbers_come_from_the_same_answer(screen, key, word):
    """숫자 옆에 어느 쪽 숫자인지가 적혀 있어야 합니다.

    라벨만 따로 움직이면 최악입니다 — 모의 수익 위에 "실거래" 가 붙습니다.
    """
    for where in ("pnl", "log"):
        seen = _obs(screen, f"{key}_{where}_label")
        assert word in seen, f"{key}/{where}: {seen!r} 에 {word} 가 없습니다"


def test_a_late_answer_from_the_other_mode_never_lands(screen):
    """모드를 바꾸고 돌아오는 사이 도착한 옛 응답은 버려야 합니다.

    그리지 않으면 화면은 "실거래" 라고 적힌 칸에 모의 숫자를 세워 둡니다 —
    이 결함이 처음 하던 짓과 정확히 같은 것을, 한 모드가 아니라 한 순간에.
    """
    assert "999999" not in _obs(screen, "late_grid"), screen["late_grid"]
    assert "LATEDRY" not in _obs(screen, "late_rows"), screen["late_rows"]
    assert "실거래" in _obs(screen, "late_pnl_label")
    assert "실거래" in _obs(screen, "late_log_label")


def test_the_screen_follows_the_mode_the_bot_is_running_in(screen):
    """지금 돌고 있는 봇의 체결이 표에 없는 것이 제일 나쁩니다.

    기본값을 한쪽에 고정하면 반대쪽 사람이 오늘 자기 체결을 못 봅니다. 무엇이
    도는지는 서버가 이미 말해 줍니다(`/api/status` 의 `mode` — 머리말 배지가
    그 값입니다).
    """
    assert _obs(screen, "running_pick") == "live"
    for prefix in ("/api/pnl", "/api/tradelog"):
        asked = _asked(_obs(screen, "running_urls"), prefix)
        assert asked, f"{prefix} 를 다시 부르지 않았습니다"
        assert all(_mode_of(u) == "live" for u in asked), asked


# ── 엔진이 없는 곳에서도 남는 최소한의 그물 ──────────────────────────────
def test_every_call_site_in_the_source_carries_a_mode():
    """엔진이 없는 기계(배포 서버)에서 도는 것은 이것뿐입니다.

    위 행동 검사만큼 말해 주지는 않습니다 — 소스에 글자가 있는지만 봅니다.
    그래도 "한 호출부에서 모드를 빠뜨렸다" 는 이 결함의 원래 모습이라, 그
    모양만큼은 여기서 막힙니다.
    """
    # 주석에서 이 주소를 **언급**하는 것은 호출이 아닙니다. 코드만 봅니다 —
    # 안 그러면 "이 결함이 왜 생겼는지" 를 설명하는 주석을 다는 순간 검사가
    # 실패하고, 다음 사람은 설명을 지우게 됩니다.
    code = _without_comments(SCRIPT)
    sites = list(re.finditer(r"/api/(?:pnl|tradelog)", code))
    assert sites, "두 주소를 부르는 자리가 아예 없습니다"
    for m in sites:
        # 한 줄이 아니라 그 자리 언저리를 봅니다 — 주소를 이어 붙이느라 줄이
        # 바뀌는 것은 이 결함과 아무 상관이 없습니다.
        near = code[m.start():m.start() + 160]
        assert "mode=" in near, f"모드 없이 부르는 자리: {near.splitlines()[0]}"


def test_a_failed_switch_does_not_leave_a_table_you_can_append_to():
    """모드를 바꾸는 첫 요청이 실패하면 표에는 앞 모드의 줄이 남습니다.

    그 상태에서 `tradeShown` 은 이미 0 이라, '더 보기' 를 누르면 새 모드의
    줄이 옛 줄 **아래에 이어 붙습니다** — 실거래 체결이 "모의매매" 이름표
    밑에 서고, 카운트는 화면과 다른 숫자를 자신 있게 뜁니다.

    트리거는 흔합니다: 전환 중 502 한 번(서버 재시작·모바일 끊김) + 클릭 한 번.
    """
    body = re.search(r"async function loadTradeLog\(more\) \{(.*?)\n\}",
                     SCRIPT, re.S).group(1)
    catch = body[body.index("catch"):body.index("const rows")]
    assert "tradeMore" in catch, (
        "요청이 실패했는데 '더 보기' 를 살려 둡니다 — 누르면 두 모드가 한 표에 "
        "섞입니다.")
    assert "tradeLog" in catch, (
        "요청이 실패했는데 앞 모드의 줄을 그대로 둡니다")


def test_a_stopped_bot_does_not_show_zero_to_a_live_only_account():
    """실거래 사용자의 표준 동선은 장 마감 후에 결과를 보러 오는 것입니다.

    그때가 정확히 봇이 꺼져 있는 때이고, 모드를 정할 근거가 없어 기본값
    `dry_run` 의 숫자 — 즉 0 — 이 뜹니다. 실거래만 해 온 계정이 "오늘 0원,
    0건" 을 봅니다.

    서버는 답을 이미 실어 보냅니다: `/api/pnl` 의 `modes` 는 기록이 실제로
    있는 모드 목록입니다.
    """
    assert "function adoptRecordedMode" in SCRIPT, (
        "봇이 꺼져 있을 때 어느 모드를 볼지 정하는 곳이 없습니다")
    pnl = re.search(r"async function loadPnl\(\) \{(.*?)\n\}", SCRIPT, re.S).group(1)
    assert "adoptRecordedMode(d.modes)" in pnl, (
        "서버가 보낸 `modes` 를 버립니다 — 저장소 주석이 경고하는 것의 정확한 "
        "대칭형입니다.")
    body = re.search(r"function adoptRecordedMode\(modes\) \{(.*?)\n\}",
                     SCRIPT, re.S).group(1)
    # 돌고 있는 봇이 있으면 그쪽이 우선입니다.
    assert "seenRunMode" in body, "돌고 있는 봇의 모드를 덮어씁니다"
    # 양쪽 다 기록이 있으면 사람이 고른 것을 그대로 둬야 합니다.
    assert "length !== 1" in body, (
        "양쪽 다 기록이 있을 때도 한쪽을 골라 버립니다 — 사람이 고른 것을 "
        "덮으면 안 됩니다.")
