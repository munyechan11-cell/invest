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
import re
from pathlib import Path

HTML = Path("quant/api/static/index.html").read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))
SERVER = Path("quant/api/server.py").read_text(encoding="utf-8")
TOSS = Path("quant/brokerage/toss_broker.py").read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    m = re.search(rf"(?:async )?function {name}\([^)]*\) \{{(.*?)\n\}}", src, re.S)
    assert m, f"{name} 을 찾지 못했습니다"
    return m.group(1)


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


def test_missing_cash_is_not_drawn_as_zero():
    """토스는 예수금을 주지 않습니다. 0 은 "공짜" 도 "없음" 도 아닌 "모름" 입니다."""
    body = _fn(SCRIPT, "loadBrokerAccount")
    assert "d.cash == null" in body, "예수금이 없을 때를 구분하지 않습니다"
    assert "제공하지 않아" in body, (
        "예수금을 못 받았다는 사실을 말하지 않습니다 — 빈칸은 '돈이 없다' 로 "
        "읽힙니다.")
    # 어댑터도 0 으로 채우지 않아야 합니다.
    overview = re.search(r"async def account_overview\(self\).*?\n    async def",
                         TOSS, re.S)
    assert overview, "account_overview 를 찾지 못했습니다"
    assert '"cash": None' in overview.group(0), (
        "예수금 자리를 0 으로 채웁니다 — 화면이 '0원' 을 자신 있게 그립니다.")


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
    assert "증권사 계좌" in HTML, "증권사 계좌 패널에 이름이 없습니다"
    assert "봇이 들고 있는 것" in HTML, (
        "봇의 장부가 여전히 '보유 종목' 이라고만 적혀 있습니다 — 계좌와 "
        "같은 것으로 읽힙니다.")


def test_a_broken_lookup_says_what_went_wrong():
    body = _fn(SCRIPT, "loadBrokerAccount")
    assert "d.error" in body, "증권사가 답을 안 줬을 때를 구분하지 않습니다"
    assert "supported === false" in body, "연동이 안 된 경우를 구분하지 않습니다"


def test_each_holding_shows_what_it_is_worth():
    """계좌를 볼 때 가장 먼저 찾는 숫자입니다.

    수량과 현재가만 놓고 사람이 곱하게 두면, 그건 계좌 화면이 아니라
    계산 문제입니다.
    """
    body = _fn(SCRIPT, "loadBrokerAccount")
    assert "평가금액" in body, "종목별 평가금액 열이 없습니다"
    assert "money(x.market_value)" in body, "종목별 평가금액을 그리지 않습니다"


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
