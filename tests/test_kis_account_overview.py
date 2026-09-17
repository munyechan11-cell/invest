"""한국투자증권을 연동한 사람도 "내 계좌" 를 볼 수 있어야 합니다.

`account_overview` 는 토스 어댑터에만 있었습니다. 그래서 한투를 연동한
사람이 본 것은 잔고가 아니라 **"계좌 조회 미지원"** 한 줄이었고, 그건 연동이
실패했다는 뜻으로 읽힙니다 — 실제로 그렇게 읽혔습니다. 화면은 진작
`source === "kis"` 를 그릴 준비가 돼 있었고, 어댑터만 비어 있었습니다.

이 창구가 지켜야 하는 규칙은 하나입니다: **모르는 것을 아는 척하지 않기.**
못 읽은 금액은 0 이 아니라 빈 칸이고, 이름이 다른 숫자를 같은 칸에 넣지
않으며, 없는 값(오늘 손익)은 비슷하게 생긴 것으로 채우지 않습니다.
"""
from __future__ import annotations

import asyncio

import pytest

from quant.brokerage.kis_broker import KisBrokerage
from quant.core.account import Portfolio

BALANCE = {
    "output1": [
        {"pdno": "005930", "prdt_name": "삼성전자", "hldg_qty": "13",
         "pchs_avg_pric": "71250", "prpr": "73100", "evlu_amt": "950300",
         "evlu_pfls_amt": "24050", "evlu_pfls_rt": "2.60"},
        {"pdno": "000660", "prdt_name": "SK하이닉스", "hldg_qty": "2",
         "pchs_avg_pric": "214000", "prpr": "205500", "evlu_amt": "411000",
         "evlu_pfls_amt": "-17000", "evlu_pfls_rt": "-3.97"},
        # 수량 0 인 행은 KIS 가 흔히 함께 줍니다 — 보유가 아닙니다.
        {"pdno": "035420", "prdt_name": "NAVER", "hldg_qty": "0"},
    ],
    "output2": [{"dnca_tot_amt": "1284300", "scts_evlu_amt": "1361300",
                 "tot_evlu_amt": "2645600", "pchs_amt_smtl_amt": "1354250",
                 "evlu_pfls_smtl_amt": "7050"}],
}


class _Kis(KisBrokerage):
    """네트워크 없이 `account_overview` 만 돌리는 대역."""

    def __init__(self, balance=None, overseas=None):
        self.portfolio = Portfolio(0.0, "KRW")
        self.paper = False
        self.overseas_exchange = "NASD"
        self._balance = BALANCE if balance is None else balance
        self._overseas = overseas

    async def _paged(self, *args, **kwargs):
        yield self._balance

    async def _overseas_balance(self):
        if isinstance(self._overseas, Exception):
            raise self._overseas
        return (self._overseas or {}), {}


def overview(**kw) -> dict:
    return asyncio.run(_Kis(**kw).account_overview())


# ── 있는 것은 제대로 ─────────────────────────────────────────────────────
def test_the_tab_is_supported_at_all():
    """이것 하나가 없어서 연동한 사람이 "미지원" 을 봤습니다."""
    assert callable(getattr(KisBrokerage, "account_overview", None))


def test_the_summary_comes_from_the_venues_own_totals():
    out = overview()
    assert out["source"] == "kis"
    assert out["cash"] == {"KRW": 1_284_300.0}
    assert out["market_value"] == {"KRW": 1_361_300.0}
    assert out["investable_assets"] == {"KRW": 2_645_600.0}
    assert out["invested"] == {"KRW": 1_354_250.0}
    assert out["pnl"] == {"KRW": 7_050.0}
    assert out["pnl_pct"] == pytest.approx(7_050 / 1_354_250)
    assert out["summary_complete"] is True


def test_holdings_carry_the_numbers_a_person_looks_for_first():
    items = overview()["items"]
    assert [i["ticker"] for i in items] == ["005930", "000660"], "수량 0 은 보유가 아닙니다"
    samsung = items[0]
    assert samsung["name"] == "삼성전자"
    assert samsung["quantity"] == 13
    assert samsung["avg_price"] == 71_250 and samsung["last_price"] == 73_100
    assert samsung["market_value"] == {"KRW": 950_300.0}
    assert samsung["pnl"] == {"KRW": 24_050.0}
    assert samsung["pnl_pct"] == pytest.approx(0.026)


# ── 이름이 다른 숫자를 같은 칸에 넣지 않습니다 ───────────────────────────
def test_deposit_is_not_reported_as_buying_power():
    """예수금은 "미수 없이 살 수 있는 금액" 이 아닙니다. 그 칸을 믿고 주문
    크기를 정하는 사람이 미수를 냅니다. 매수가능금액은 종목·호가를 넣어야
    답이 나오는 별개 창구라 계좌 단위 숫자가 아닙니다."""
    out = overview()
    assert out["cash_buying_power"] == {}
    assert out["cash"] == {"KRW": 1_284_300.0}


def test_todays_pnl_is_left_empty_rather_than_guessed():
    """잔고 응답에 오늘 손익이 없습니다. 자산증감액은 입출금이 섞여 있어
    손익이 아닙니다 — 비슷하게 생긴 숫자를 대신 넣는 것이 이 코드베이스가
    반복해서 고쳐 온 실수입니다."""
    out = overview()
    assert out["daily_pnl"] == {} and out["daily_pnl_pct"] is None


# ── 모르는 것은 0 이 아닙니다 ────────────────────────────────────────────
def test_an_unreadable_amount_becomes_a_blank_not_a_zero():
    broken = {"output1": [], "output2": [{"dnca_tot_amt": "모름",
                                          "scts_evlu_amt": "1000"}]}
    out = overview(balance=broken)
    assert out["cash"] == {}, "0원이라고 자신 있게 쓰면 안 됩니다"
    assert out["market_value"] == {"KRW": 1000.0}
    assert out["summary_complete"] is False and "예수금" in out["summary_message"]


def test_a_missing_summary_block_says_so():
    out = overview(balance={"output1": [], "output2": []})
    assert out["summary_complete"] is False
    assert "output2" in out["summary_message"]
    assert out["investable_assets"] == {}


def test_a_summary_sent_as_an_object_is_read_too():
    """KIS 는 output2 를 배열로도 객체로도 줍니다."""
    single = {"output1": [], "output2": {"dnca_tot_amt": "500"}}
    assert overview(balance=single)["cash"] == {"KRW": 500.0}


# ── 해외 잔고가 국내 잔고를 죽이지 않습니다 ──────────────────────────────
def test_a_missing_overseas_permission_does_not_empty_the_whole_tab():
    """해외 계좌 권한이 없는 사람에게 국내 잔고까지 안 보이면 안 됩니다."""
    out = overview(overseas=RuntimeError("해외계좌 권한 없음"))
    assert len(out["items"]) == 2, "국내분은 그대로 보여야 합니다"
    assert out["items_complete"] is False
    assert "해외" in out["items_message"] and "권한" in out["items_message"]


def test_overseas_holdings_are_flagged_as_missing_from_the_totals():
    """국내 잔고 창구는 해외분을 주지 않습니다. 합계에 없다는 사실을
    말하지 않으면, 사람은 이 화면이 계좌 전부라고 믿습니다."""
    out = overview(overseas={"kis:AAPL": 3})
    assert out["items_complete"] is False
    assert "해외 보유 1종목" in out["items_message"]


def test_a_domestic_only_account_reports_complete():
    out = overview(overseas={})
    assert out["items_complete"] is True and out["items_message"] == ""


# ── 지원하지 않는 어댑터 안내 ────────────────────────────────────────────
def test_an_unsupported_adapter_says_the_bot_is_still_fine():
    """"alpaca 은 계좌 조회를 지원하지 않습니다" 만 읽으면 연동이 깨진 줄
    압니다. 이 탭만 비어 있고 매매에는 문제가 없다는 사실을 함께 씁니다."""
    import inspect

    from quant.webapp.registry import UserRegistry

    source = inspect.getsource(UserRegistry.broker_account)
    assert "봇을 돌리는 데는 문제가 없고" in source


# ── 어느 계좌를 본 것인가 ────────────────────────────────────────────────
def test_the_overview_says_which_kis_account_it_read():
    """모의투자와 실계좌는 호스트부터 다른 **별개 계좌** 입니다. 이 값이
    없으면 화면이 모의투자 잔고를 실계좌로 그립니다 — 이 화면이 만들 수
    있는 가장 비싼 오해입니다."""
    paper = _Kis()
    paper.paper = True
    assert asyncio.run(paper.account_overview())["environment"] == "paper"

    real = _Kis()
    real.paper = False
    assert asyncio.run(real.account_overview())["environment"] == "live"


# ── 모의 전략을 보고 있어도 연동한 계좌가 나옵니다 ───────────────────────
def test_a_paper_strategy_falls_back_to_the_connected_broker():
    """계좌는 전략의 것이 아니라 사람의 것입니다. 전략을 바꿔야만 자기
    잔고가 보이는 것은 이 탭의 설명과 정면으로 어긋납니다."""
    from quant.webapp.registry import _connected_account_venue

    kis = {"KIS_APP_KEY": "k", "KIS_APP_SECRET": "s", "KIS_ACCOUNT_NO": "12345678"}
    assert _connected_account_venue(kis) == "kis"


def test_an_incomplete_connection_is_not_chosen():
    """키가 하나라도 비면 어댑터가 생성자에서 터지고, 그 예외는 "연동이
    깨졌다" 로 보입니다 — 실제로는 우리가 고르지 말았어야 할 곳입니다."""
    from quant.webapp.registry import _connected_account_venue

    assert _connected_account_venue({"KIS_APP_KEY": "k"}) == ""
    assert _connected_account_venue({}) == ""


def test_nothing_connected_says_so_instead_of_naming_paper():
    import inspect

    from quant.webapp.registry import UserRegistry

    source = inspect.getsource(UserRegistry.broker_account)
    assert "아직 연동한 증권사가 없습니다" in source
    assert "모의투자 계좌도 마찬가지입니다" in source
