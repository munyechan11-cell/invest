"""심의한 종목과 차트가 다르면, 그 판단이 지금 화면의 종목 것으로 읽힙니다.

데스크가 035420 을 심의하는데 차트는 005930 을 보고 있었습니다. 두 화면이
나란히 있고 한쪽에만 종목코드가 작게 적혀 있으면, 사람은 큰 쪽(차트)을
기준으로 읽습니다 — 그리고 "관망" 을 삼성전자에 대한 판단으로 이해합니다.

그래서 차트가 심의를 따라갑니다. **단 사람이 직접 고른 순간부터는 안
따라갑니다** — 보고 있던 차트가 심의 때마다 튀면 그것대로 못 쓰고, 내가
고른 것을 덮는 자동 동작은 화면이 말을 안 듣는 것으로 느껴집니다.
"""
from __future__ import annotations

from tests.test_my_account_shows_the_real_account import (
    JS_REQUIRED,
    _run_ui_js,
)

PRELUDE = r"""
var options = [{value: "005930"}, {value: "000660"}];
var changes = 0;
var SEL = {
  value: "005930",
  options: options,
  add: function (opt) { options.push(opt); },
  dispatchEvent: function (e) { changes += 1; if (this.onchange) this.onchange(e); },
  onchange: null,
};
function Option(text, value) { return {value: value, text: text}; }
var document = {
  getElementById: function (id) { return id === "cSym" ? SEL : null; },
  addEventListener: function () {},
};
function esc(v) { return String(v == null ? "" : v); }
function invalidateMarketSelection() {}
function wakeMarketPolling() {}
var marketBarsFetchedAt = 0;
var deskFollow = true;
var deskFollowedSymbol = "";
"""

DRIVER_TEMPLATE = r"""
%s
write(JSON.stringify({symbol: SEL.value, changes: changes,
                      follow: deskFollow, options: options.length}));
"""


def run(script: str) -> dict:
    return _run_ui_js(["gotoSymbol", "followDesk"], PRELUDE,
                      DRIVER_TEMPLATE % script)


@JS_REQUIRED
def test_the_chart_moves_to_the_symbol_being_deliberated():
    got = run('followDesk("035420");')
    assert got["symbol"] == "035420"
    assert got["changes"] == 1, "차트를 다시 불러오지 않았습니다"


@JS_REQUIRED
def test_an_unknown_symbol_is_added_rather_than_ignored():
    """데스크는 후보 목록 밖 종목도 심의할 수 있습니다."""
    got = run('followDesk("035420");')
    assert got["options"] == 3


@JS_REQUIRED
def test_the_same_symbol_twice_does_not_reload_the_chart():
    """폴링마다 차트를 흔들 이유가 없습니다."""
    got = run('followDesk("035420"); followDesk("035420"); followDesk("035420");')
    assert got["changes"] == 1


@JS_REQUIRED
def test_a_symbol_the_user_is_already_on_is_left_alone():
    got = run('followDesk("005930");')
    assert got["changes"] == 0


@JS_REQUIRED
def test_once_the_user_picks_a_symbol_the_desk_stops_dragging_the_chart():
    """내가 고른 것을 덮는 자동 동작은 화면이 말을 안 듣는 것으로 느껴집니다."""
    got = run('deskFollow = false; followDesk("035420");')
    assert got["symbol"] == "005930" and got["changes"] == 0


@JS_REQUIRED
def test_clicking_the_deliberated_name_turns_following_back_on():
    """다시 따라가게 하는 방법이 있어야 합니다 — 없으면 한 번 끄면 끝입니다."""
    got = run('deskFollow = false; gotoSymbol("035420", true);')
    assert got["symbol"] == "035420" and got["follow"] is True


@JS_REQUIRED
def test_following_does_not_turn_itself_off():
    """추종이 보내는 합성 이벤트를 사람이 고른 것으로 세면, 첫 추종 뒤에
    바로 꺼집니다. 브라우저 이벤트만 `isTrusted` 인 이유가 그것입니다."""
    got = run('followDesk("035420"); followDesk("000660");')
    assert got["symbol"] == "000660" and got["follow"] is True


def test_the_user_change_handler_only_trusts_real_events():
    from pathlib import Path

    html = Path("quant/api/static/index.html").read_text(encoding="utf-8")
    assert "event.isTrusted) deskFollow = false" in html
