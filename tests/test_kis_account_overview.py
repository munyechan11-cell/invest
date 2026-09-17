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


#: 해외 잔고(`TTTS3012R`) output1 한 줄. 이름은 저장소가 이미 쓰고 있는
#: `ovrs_pdno`/`ovrs_cblc_qty`/`pchs_avg_pric` 를 따르고, 나머지는 못 읽으면
#: 빈 칸이 되도록 만들어 뒀습니다 — 틀린 숫자보다 빈 칸이 낫습니다.
AAPL = {"ovrs_pdno": "AAPL", "ovrs_item_name": "APPLE INC",
        "ovrs_cblc_qty": "3", "pchs_avg_pric": "228.40",
        "now_pric2": "245.67", "ovrs_stck_evlu_amt": "737.01",
        "frcr_evlu_pfls_amt": "51.81", "evlu_pfls_rt": "7.56"}


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

    async def _overseas_rows(self):
        if isinstance(self._overseas, Exception):
            raise self._overseas
        return list(self._overseas or [])


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
    말하지 않으면, 사람은 이 화면이 계좌 전부라고 믿습니다.

    경고는 **집계 쪽** 에 답니다. 보유 표에는 해외분이 들어가 있으므로,
    표가 불완전하다고 말하면 그게 틀린 말이 됩니다."""
    out = overview(overseas=[AAPL])
    assert out["items_complete"] is True, "표에는 들어 있습니다"
    assert out["summary_complete"] is False
    assert "해외 보유 1종목" in out["summary_message"]
    assert "집계" in out["summary_message"]


def test_a_domestic_only_account_reports_complete():
    out = overview(overseas=[])
    assert out["items_complete"] is True and out["items_message"] == ""
    assert out["summary_complete"] is True and out["summary_message"] == ""


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


# ── 환경과 주문은 다른 축입니다 ──────────────────────────────────────────
def test_reading_the_real_account_does_not_mean_sending_orders():
    """예전에는 `self.paper = not self.live` 였습니다. 그래서 조회 전용으로
    모드를 낮춘 화면이 읽는 **계좌까지** 바뀌었습니다 — 실계좌를 보려고 연
    화면이 모의투자 잔고를 그렸고, 같은 앱 키라도 그 둘은 다른 계좌입니다."""
    from quant.core.account import Portfolio

    real_read = KisBrokerage(Portfolio(0.0, "KRW"), app_key="k", app_secret="s",
                             account_no="12345678", environment="live",
                             live=False, allow_env_credentials=False)
    assert real_read.paper is False, "실계좌를 읽으라고 했는데 모의투자를 봅니다"
    assert real_read.sends_orders is False, "읽기만 해야 합니다"

    paper_read = KisBrokerage(Portfolio(0.0, "KRW"), app_key="k", app_secret="s",
                              account_no="12345678", environment="paper",
                              live=False, allow_env_credentials=False)
    assert paper_read.paper is True and paper_read.sends_orders is False


def test_an_unset_environment_keeps_the_old_inference():
    """비워 두면 예전 그대로입니다 — 이 변경이 남의 설정을 조용히 바꾸면
    안 됩니다."""
    from quant.core.account import Portfolio

    broker = KisBrokerage(Portfolio(0.0, "KRW"), app_key="k", app_secret="s",
                          account_no="12345678", live=False,
                          allow_env_credentials=False)
    assert broker.paper is True


def test_paper_host_with_real_money_is_refused():
    from quant.brokerage.base import BrokerageError
    from quant.core.account import Portfolio

    with pytest.raises(BrokerageError, match="함께 쓸 수 없습니다"):
        KisBrokerage(Portfolio(0.0, "KRW"), app_key="k", app_secret="s",
                     account_no="12345678", environment="paper", live=True,
                     allow_env_credentials=False)


def test_a_typo_in_the_environment_is_refused_not_guessed():
    from quant.brokerage.base import BrokerageError
    from quant.core.account import Portfolio

    with pytest.raises(BrokerageError, match="environment"):
        KisBrokerage(Portfolio(0.0, "KRW"), app_key="k", app_secret="s",
                     account_no="12345678", environment="mock",
                     allow_env_credentials=False)


# ── 환경마다 다른 키를 배선합니다 ────────────────────────────────────────
def kis_cfg(**broker):
    from quant.config.schema import StrategyConfig

    return StrategyConfig.model_validate({
        "name": "t", "mode": broker.pop("mode", "dry_run"),
        "universe": {"symbols": [{"ticker": "005930", "venue": "kis",
                                  "quote_currency": "KRW"}]},
        "alpha": [{"type": "ema_cross"}],
        "broker": {"type": "kis", **broker},
        # 실거래 설정은 하루 한도가 하나라도 있어야 만들어집니다.
        "limits": {"max_daily_orders": 5},
    })


def test_a_dry_run_strategy_asks_for_the_paper_keys():
    """모의투자 호스트로는 모의투자 키만 들어갑니다. 실계좌 키를 요구하면
    사람은 맞는 키를 넣고도 계속 거절당하고, 그때 나오는 말은 "키가 틀렸다"
    입니다 — 키는 맞고 문이 다른 것인데."""
    from quant.webapp.registry import required_secrets

    assert set(required_secrets(kis_cfg())) == {
        "KIS_PAPER_APP_KEY", "KIS_PAPER_APP_SECRET", "KIS_PAPER_ACCOUNT_NO"}


def test_an_explicit_live_environment_asks_for_the_real_keys():
    from quant.webapp.registry import required_secrets

    needed = set(required_secrets(kis_cfg(params={"environment": "live"})))
    assert needed == {"KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO"}


def test_a_live_strategy_asks_for_the_real_keys():
    from quant.webapp.registry import required_secrets

    cfg = kis_cfg(mode="live", live_trading_confirmed=True)
    assert set(required_secrets(cfg)) == {
        "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO"}


def test_the_setup_screen_offers_both_kis_environments():
    """한 칸에 받으면 둘 중 하나만 쓸 수 있고, 어느 쪽을 넣었는지도
    알 수 없습니다."""
    from quant.live.credentials import VENUES_BY_ID

    assert "kis" in VENUES_BY_ID and "kis_paper" in VENUES_BY_ID
    assert "모의투자" in VENUES_BY_ID["kis_paper"].label_ko
    assert "실계좌" in VENUES_BY_ID["kis"].label_ko
    paper_fields = {env for env, _, _ in VENUES_BY_ID["kis_paper"].fields}
    live_fields = {env for env, _, _ in VENUES_BY_ID["kis"].fields}
    assert not (paper_fields & live_fields), "두 환경이 같은 칸을 쓰면 안 됩니다"


# ── 미국을 돌리는 사람의 계좌 ────────────────────────────────────────────
#
# `configs/us_kis_paper.yaml` 을 내면서 생긴 구멍입니다. 어댑터는 해외 보유를
# **세기만 하고** 표에는 넣지 않았습니다. 국내만 하는 사람에게는 경고 한 줄로
# 충분했지만, 미국만 하는 사람은 자기 보유가 한 줄도 없는 표를 봅니다 —
# 그리고 그 화면은 연동이 깨진 것과 구별되지 않습니다. "모의여도 잔액 볼 수
# 있어야지" 가 이 탭이 존재하는 이유였습니다.

def test_overseas_holdings_actually_appear_in_the_table():
    tickers = [i["ticker"] for i in overview(overseas=[AAPL])["items"]]
    assert "AAPL" in tickers, "해외 보유가 표에 없습니다"


def test_a_us_only_account_is_not_an_empty_screen():
    """국내 보유가 없는 계좌 — 미국만 돌리면 이게 보통입니다."""
    empty = {"output1": [], "output2": [{"dnca_tot_amt": "0"}]}
    out = overview(balance=empty, overseas=[AAPL])
    assert len(out["items"]) == 1 and out["items"][0]["name"] == "APPLE INC"


def test_the_foreign_row_carries_its_currency():
    """원화 종목과 한 표에 섞입니다. 통화가 없으면 화면이 $245.67 을
    245원 옆에 245.67 로 앉힙니다."""
    row = overview(overseas=[AAPL])["items"][-1]
    assert row["currency"] == "USD"
    assert row["market_value"] == {"USD": 737.01}
    assert row["pnl"] == {"USD": 51.81}
    assert row["pnl_pct"] == pytest.approx(0.0756)


def test_a_zero_quantity_overseas_row_is_not_a_holding():
    """KIS 는 판 종목을 수량 0 으로 함께 줍니다."""
    sold = dict(AAPL, ovrs_cblc_qty="0")
    assert [i["ticker"] for i in overview(overseas=[sold])["items"]] == \
        ["005930", "000660"]


def test_an_unreadable_overseas_price_is_blank_not_zero():
    """0 을 넣으면 화면이 "$0" 이라고 자신 있게 씁니다."""
    broken = dict(AAPL, now_pric2="", ovrs_stck_evlu_amt="N/A")
    row = overview(overseas=[broken])["items"][-1]
    assert row["last_price"] is None and row["market_value"] == {}


def test_a_bad_holding_value_does_not_accuse_the_summary():
    """보유 한 줄이 이상한 것과 **집계가 이상한 것** 은 다른 말입니다.
    그 칸이 "조회 불가" 로 비는 것 자체가 이미 화면에 보이는 신호입니다."""
    out = overview(overseas=[dict(AAPL, ovrs_stck_evlu_amt="N/A")])
    assert "숫자로 읽을 수 없습니다" not in out["summary_message"]


def test_the_orders_path_is_untouched():
    """`positions()` 가 쓰는 창구는 그대로입니다 — 돈이 지나가는 길을
    화면 때문에 흔들면 안 됩니다."""
    import inspect

    src = inspect.getsource(KisBrokerage._venue_positions)
    assert "_overseas_balance" in src
    assert "_overseas_rows" not in src
