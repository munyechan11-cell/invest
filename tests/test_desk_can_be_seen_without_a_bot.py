"""봇을 켜지 않고도 데스크가 도는 것을 볼 수 있는가.

"일단 어떻게 돌아가는지 보고 싶다" 는 자동매매를 켜기 **전에** 생기는
마음입니다. 그런데 지금까지 그걸 보려면 봇부터 켜야 했고, 일봉 전략이라
그다음엔 다음 장 마감까지 기다려야 했습니다. 자기 돈을 넣을지 정하기 전에
확인할 방법이 없었던 셈입니다.

`/api/evaluate` 는 처음부터 이걸 할 수 있었습니다 — 봉을 기다리지도, 장이
열려 있지도 않아도 종목 하나를 그 자리에서 16명에게 물어봅니다. 두 가지가
막고 있었습니다.

**첫째, 응답을 버렸습니다.** 좌석별 발언이 전부 응답에 실려 오는데
(`analysts`·`debate`·`risk_debate`) 화면은 요약 카드만 그렸습니다. 데스크는
돌았는데 사람은 도는 걸 못 봤습니다.

**둘째, 버튼이 숨어 있었습니다.** 수동매매 패널 안쪽에 있어서, 데스크가 비어
있는 것을 보고 있는 사람은 그게 있는 줄 몰랐습니다. 비어 있는 그 자리에 두는
것이 맞습니다.
"""
from __future__ import annotations

import re
from pathlib import Path

HTML = Path("quant/api/static/index.html").read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))


def _body(name: str) -> str:
    m = re.search(rf"function {name}\([^)]*\) \{{(.*?)\n\}}", SCRIPT, re.S)
    assert m, f"{name} 함수가 없습니다"
    return m.group(1)


def test_an_on_demand_deliberation_is_replayed_in_the_rooms():
    """요약 카드만 그리면 "데스크가 도는 것" 을 본 게 아닙니다."""
    body = re.search(r"async function askDeskFor\([^)]*\) \{(.*?)\n\}\n", SCRIPT, re.S)
    assert body, "askDeskFor 가 없습니다"
    src = body.group(1)
    assert "playDeliberation" in src, (
        "즉석 심의 결과를 데스크에서 재생하지 않습니다 — 16명이 말한 것이 "
        "응답에 그대로 들어 있는데 요약만 그리고 버립니다.")
    assert "scrollToDesk" in src, "재생은 하는데 화면이 그리로 가지 않습니다"


def test_the_button_sits_where_the_desk_is_empty():
    assert "function renderTryNow" in SCRIPT
    body = _body("renderTryNow")
    assert "askDeskFor" in body, "버튼이 심의를 부르지 않습니다"


def test_the_button_only_appears_when_it_would_actually_work():
    """눌러도 실패할 버튼은 버튼이 아닙니다.

    데스크가 없는 전략, 연동이 안 된 전략, 볼 종목이 없는 전략에서 이 버튼을
    띄우면 사용자는 눌러서 오류를 읽게 됩니다.
    """
    body = _body("renderTryNow")
    assert "hasDesk(st)" in body, "데스크 없는 전략에서도 버튼이 뜹니다"
    assert "missingFor(st)" in body, "연동 안 된 전략에서도 버튼이 뜹니다"
    assert "볼 종목이 없습니다" in body, "종목이 없을 때를 구분하지 않습니다"


def test_a_ticker_is_never_rendered_as_an_object():
    """종목 목록이 두 모양으로 옵니다 — "005930" 과 {ticker, name}.

    한쪽만 다루면 다른 쪽에서 "[object Object]" 가 화면에 뜹니다. 실제로
    떴습니다. 코드를 꺼내는 자리를 한 군데로 모아야 합니다.
    """
    assert "function tickerCode" in SCRIPT, "티커 두 모양을 한 군데서 다루지 않습니다"
    label = _body("tickerLabel")
    assert "tickerCode" in label, (
        "tickerLabel 이 티커를 직접 문자열로 다룹니다 — 객체가 오면 "
        "'[object Object]' 가 화면에 뜹니다.")
    # 찍는 규칙은 `symLabel` 한 곳에만 둡니다. 두 군데면 언젠가 한 곳만
    # 고치고, 화면 절반에서만 이름이 뜨는 상태가 됩니다.
    assert "symLabel(" in label, "tickerLabel 이 자기만의 표기 규칙을 갖고 있습니다"
    rule = _body("symLabel")
    assert "nm !== code" in rule or "nm != code" in rule, (
        "이름이 티커와 같을 때 '005930 (005930)' 을 만듭니다")


def test_join_never_stringifies_a_ticker_object():
    """`join` 은 원소마다 ToString 을 부릅니다.

    목록이 {ticker, name} 으로 바뀌면서 전략 설명의 "대상 종목" 줄이
    "[object Object], [object Object]" 를 그렸습니다 — 하필 사람이 자기 돈을
    넣기 직전에 읽는 자리에서.
    """
    for m in re.finditer(r"(\w+)\.join\(", SCRIPT):
        name = m.group(1)
        assert name not in ("tickers", "symbols"), (
            f"{name}.join(...) — 원소가 객체면 [object Object] 가 찍힙니다. "
            "tickerLabel 로 문자열을 만든 뒤 이으세요.")


def test_the_desk_speaks_before_the_first_bar_closes():
    """봇을 켠 사람도 다음 봉까지 기다리면 안 됩니다."""
    trader = Path("quant/live/trader.py").read_text(encoding="utf-8")
    assert "_opening_deliberation" in trader
    body = re.search(r"async def _deliberate_now\(self[^)]*\).*?\n\n    (?:async )?def",
                     trader, re.S)
    assert body, "심의 본문을 찾지 못했습니다"
    src = body.group(0)
    assert "desk.update" in src, "데스크를 부르지 않습니다"
    # 인사이트를 장부에 넣는 것은 "예약" 이고, 주문이 아닙니다. 주문은 다음
    # on_bars 가 포트폴리오 구성과 리스크를 거쳐 만듭니다.
    assert "engine.on_bars" not in src, (
        "봉 없이 도는 심의가 엔진을 돌립니다 — 시작 버튼이 곧 주문이 됩니다.")
    assert "_submit" not in src and "execution_model" not in src, (
        "봉 없이 도는 심의에서 주문 경로에 닿습니다")


def test_a_failed_deliberation_says_so_where_the_button_was():
    """실패는 누른 자리에 떠야 합니다.

    데스크 옆 버튼을 눌렀는데 오류가 저 아래 수동매매 패널에만 뜨면, 사람은
    아무 일도 안 일어난 줄 압니다 — 실제로 그렇게 보였습니다. 서버는 이유를
    한국어로 만들어 보내는데(키 거절·시세 실패·봉 부족) 그 문장이 화면 어디에도
    안 뜨면, 무엇을 고쳐야 할지 알 방법이 없습니다.
    """
    assert "function deskFailed" in SCRIPT, "실패를 데스크에 그리는 자리가 없습니다"
    ask = re.search(r"async function askDeskFor\([^)]*\) \{(.*?)\n\}\n", SCRIPT, re.S)
    assert ask, "askDeskFor 를 찾지 못했습니다"
    catch = ask.group(1)[ask.group(1).index("catch"):]
    assert "deskFailed" in catch, (
        "심의가 실패했는데 데스크 자리에는 아무 말도 안 남습니다")
    body = _body("deskFailed")
    assert "bad" in body, "실패가 기다림과 같은 회색으로 보입니다"
    assert "message" in body, (
        "서버가 만들어 준 이유를 버리고 고정 문구만 띄웁니다 — 무엇을 고쳐야 "
        "할지 알 수 없게 됩니다.")


def test_it_asks_the_strategy_that_is_actually_running():
    """봇이 돌면 선택기는 감춰지고 값도 리셋됩니다.

    그 값으로 물으면 지금 돌고 있는 것과 다른 전략의 데스크에 묻게 되고,
    돌아온 대답은 사용자가 보고 있는 전략의 판단이 아닙니다.
    """
    ask = re.search(r"async function askDeskFor\([^)]*\) \{(.*?)\n\}\n", SCRIPT, re.S).group(1)
    call = ask[:ask.index("catch")]
    assert "shownStrategy()" in call, (
        "즉석 심의가 선택기 값으로 묻습니다 — 봇이 도는 동안 그 값은 "
        "돌고 있는 전략이 아닙니다.")
