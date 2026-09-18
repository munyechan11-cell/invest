""""매도" 가 떴는데 아무 일도 안 일어납니다.

    매도 · 확신 80%
    …공매도가 불가한 제약 조건과 **현재 보유 비중이 0%인 점** 을 고려하여,
    신규 매수를 엄격히 차단하고 현금 비중을 유지하기 위해
    'sell(청산/매수불가)' 을 결정한다.

데스크는 정확히 말했습니다. 화면만 큼직하게 **"매도"** 라고 썼습니다.
보유가 0이고 공매도를 안 쓰는 전략에서 `sell` 은 "판다" 가 아니라 **"사지
않는다"** 입니다. 팔 것이 없으니 주문도 없고, 사람은 체결을 찾으러 갔다가
못 찾고 "매도가 떠도 안 넘어간다" 고 읽습니다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from tests.test_my_account_shows_the_real_account import JS_REQUIRED, _run_ui_js

HTML = Path("quant/api/static/index.html").read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))

PRELUDE = """
var botState = null;
function setBot(positions) {
  botState = positions === null ? null : {portfolio: {positions: positions}};
}
"""


def run(action: str, positions) -> bool:
    driver = (f"setBot({json.dumps(positions)});\n"
              f"var d = {{action: {json.dumps(action)}, symbol: '032830'}};\n"
              "write(JSON.stringify({flat: nothingToSell(d)}));")
    return _run_ui_js(["nothingToSell"], PRELUDE, driver)["flat"]


@JS_REQUIRED
def test_a_sell_with_no_position_is_flagged():
    assert run("sell", []) is True


@JS_REQUIRED
def test_a_sell_on_something_held_is_a_real_sell():
    assert run("sell", [{"symbol": "032830", "quantity": 10}]) is False


@JS_REQUIRED
def test_a_zero_quantity_row_is_not_a_holding():
    assert run("sell", [{"symbol": "032830", "quantity": 0}]) is True


@JS_REQUIRED
def test_holding_a_different_symbol_does_not_count():
    assert run("sell", [{"symbol": "005930", "quantity": 10}]) is True


@JS_REQUIRED
def test_a_buy_is_never_flagged():
    assert run("buy", []) is False
    assert run("hold", []) is False


@JS_REQUIRED
def test_reduce_and_strong_sell_are_the_same_case():
    assert run("reduce", []) is True
    assert run("strong_sell", []) is True


@JS_REQUIRED
def test_it_says_nothing_when_it_does_not_know():
    """보유를 모르는 상태에서 "낼 주문이 없습니다" 라고 하면, 실제로 팔린
    주문을 안 팔렸다고 말하게 됩니다."""
    assert run("sell", None) is False


# ── 화면이 실제로 말하는가 ──────────────────────────────────────────────
def test_the_card_explains_it_rather_than_just_shouting_sell():
    card = SCRIPT[SCRIPT.index("function renderVerdict"):][:1800]
    assert "낼 주문이" in card and "사지 않는다" in card


def test_the_colour_stops_screaming_when_nothing_moves():
    """빨간 '매도' 는 무슨 일이 일어났다는 신호입니다 — 아무 일도 안 일어난
    판정에 그 색을 쓰면 색이 거짓말을 합니다."""
    card = SCRIPT[SCRIPT.index("function renderVerdict"):][:1800]
    assert 'flat ? "var(--muted)" : color' in card


def test_it_names_why_there_is_no_short_leg():
    card = SCRIPT[SCRIPT.index("function renderVerdict"):][:1800]
    assert "공매도" in card
