"""`quant validate` 가 돌려보기 전에 말해 주는 산수.

실거래 사이징은 `starting_cash` 가 아니라 증권사가 말하는 **실제 평가액** 을
읽습니다. 그래서 설정에 적힌 세 숫자 — 종목당 비중, 최소 주문금액, 주문당
상한 — 가 진짜 잔고와 어긋나면 신규 진입이 전부 건너뛰어지거나 전부
거절됩니다. 예외도 로그도 없이, 화면에는 "대기 중" 만 남습니다.

이 검사는 아무것도 막지 않습니다. 하루를 잃기 전에 말해 줄 뿐입니다.
"""
import pytest

from quant.cli import _load
from quant.config.preflight import entry_window, preflight_warnings
from quant.config.schema import StrategyConfig
from tests.conftest import BACKTEST_CONFIGS as BACKTEST  # noqa: E402
from tests.conftest import LIVE_CONFIGS as LIVE  # noqa: E402


@pytest.fixture
def env(monkeypatch):
    for var in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_NO",
                "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO",
                "BINANCE_KEY", "BINANCE_SECRET", "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_CHAT_ID", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(var, "x")


def cfg(**kw) -> StrategyConfig:
    base = {
        "name": "t", "mode": "live",
        "portfolio": {"max_position_weight": 0.30, "cash_reserve_pct": 0.05,
                      "starting_cash": 1_000},
        "execution": {"min_order_notional": 200},
        "broker": {"type": "toss", "max_order_notional": 300,
                   "live_trading_confirmed": True},
        "limits": {"max_daily_notional": 500, "max_daily_orders": 10},
    }
    for section, values in kw.items():
        base.setdefault(section, {})
        base[section] = {**base[section], **values} if isinstance(base[section], dict) else values
    return StrategyConfig(**base)


# ── 구간 자체 ────────────────────────────────────────────────────────────
def test_the_window_is_the_two_caps_divided_by_the_weight():
    low, high = entry_window(cfg())
    assert round(low) == 702        # 200 / (0.30 * 0.95)
    assert round(high) == 1_053     # 300 / (0.30 * 0.95)


def test_an_impossible_window_is_named_as_one():
    notes = preflight_warnings(cfg(execution={"min_order_notional": 400}))
    assert any("구간이 없습니다" in n for n in notes)


def test_a_narrow_window_says_it_is_narrow():
    notes = preflight_warnings(cfg())
    assert any("두 배도 안 돼서" in n for n in notes)


def test_a_roomy_window_does_not_say_that():
    notes = preflight_warnings(cfg(broker={"max_order_notional": 10_000}))
    assert not any("두 배도 안 돼서" in n for n in notes)


# ── 하루 한도끼리의 모순 ─────────────────────────────────────────────────
def test_an_unreachable_order_count_cap_is_reported_with_the_real_number():
    notes = preflight_warnings(cfg())
    hit = [n for n in notes if "주문 건수 한도" in n]
    assert hit and "최대 2건" in hit[0]


def test_a_consistent_pair_of_caps_is_silent():
    notes = preflight_warnings(cfg(limits={"max_daily_notional": 5_000,
                                           "max_daily_orders": 10}))
    assert not any("주문 건수 한도" in n for n in notes)


def test_exits_consuming_turnover_is_said_out_loud_on_live_only():
    assert any("사용량에는 기록" in n for n in preflight_warnings(cfg()))
    assert not any("사용량에는 기록" in n
                   for n in preflight_warnings(cfg(mode="dry_run",
                                          broker={"live_trading_confirmed": False})))


# ── 출하되는 설정 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", LIVE)
def test_no_shipped_live_config_has_an_impossible_entry_window(path, env):
    low, high = entry_window(_load(path))
    assert low < high, f"{path} 는 어떤 잔고에서도 신규 진입을 낼 수 없습니다"


@pytest.mark.parametrize("path", LIVE)
def test_every_live_config_tells_the_operator_its_window(path, env):
    """실거래는 구간이 넓든 좁든 말해야 합니다 — 그 숫자를 아는 것이
    시작 전에 할 일입니다."""
    assert any("계좌 평가액" in n for n in preflight_warnings(_load(path)))


@pytest.mark.parametrize("path", LIVE)
def test_no_shipped_live_config_has_caps_that_contradict_each_other(path, env):
    """두 한도가 서로 모르면 적어 둔 건수는 장식입니다.

    최소 주문금액이 `거래대금 ÷ 건수` 보다 크면 건수 한도에는 영원히 닿지
    못합니다 — 언제나 거래대금이 먼저 막습니다. 화면과 확인창은 "하루 10건"
    이라고 말하는데 실제로는 2건이고, 그 차이는 어디에도 안 나옵니다.

    고치는 방향은 **최소 주문금액을 내리는 쪽** 입니다. 그건 비용 통제지
    안전 천장이 아닙니다. 거래대금·건수·주문당 상한을 올려서 맞추면 한도를
    푸는 것이고, 그건 운영자의 결정이지 정합성 수정이 아닙니다.
    """
    config = _load(path)
    lim, floor = config.limits, config.execution.min_order_notional
    if not (lim.max_daily_notional and lim.max_daily_orders and floor):
        return
    assert floor <= lim.max_daily_notional / lim.max_daily_orders + 1e-9, (
        f"{path}: 최소 주문 {floor:,.0f} 이면 거래대금 한도 "
        f"{lim.max_daily_notional:,.0f} 안에서 최대 "
        f"{int(lim.max_daily_notional // floor)}건인데 건수 한도는 "
        f"{lim.max_daily_orders}건으로 적혀 있습니다"
    )


@pytest.mark.parametrize("path", LIVE)
def test_no_shipped_live_config_has_a_window_too_narrow_to_survive_drift(path, env):
    """구간이 두 배도 안 되면 계좌가 30% 만 움직여도 벗어납니다 — 그리고
    벗어난 날 봇은 조용히 아무것도 안 삽니다."""
    assert not any("두 배도 안 돼서" in note
                   for note in preflight_warnings(_load(path))), (
        f"{path} 의 진입 구간이 너무 좁습니다")


@pytest.mark.parametrize("path", BACKTEST)
def test_a_backtest_with_a_roomy_window_stays_quiet(path, env):
    """경고가 늘 켜져 있으면 아무도 안 읽습니다."""
    assert preflight_warnings(_load(path)) == []


def test_the_paper_broker_has_no_per_order_ceiling_to_warn_about():
    """`max_order_notional` 은 `LiveBrokerage._guard` 에만 있습니다. 페이퍼는
    그 값을 받지도 않으므로, 설정에 남은 기본값을 천장으로 읽으면 있지도 않은
    벽을 경고하게 됩니다 — kr_equity.yaml 이 그 경우였습니다."""
    assert entry_window(cfg(mode="backtest",
                            broker={"type": "paper",
                                    "live_trading_confirmed": False})) is None


# ── 증권사 잔고를 못 읽는 조합 ───────────────────────────────────────────
#
# `us_kis_paper` 를 내면서 드러났습니다. 한투 잔고 창구가 주는 예수금은
# **원화** 이고, 달러 장부에 그 숫자를 넣으면 1원 = 1달러 환산이 됩니다.
# 어댑터는 그래서 아예 넘기지 않습니다 — 막는 것까지는 맞습니다. 그런데 그
# 결과 사이징 기준이 조용히 `starting_cash` 로 남고, "어차피 계좌를 읽으니까"
# 하고 그 숫자를 대충 적어 둔 사람은 그 사실을 알 길이 없습니다.

def kis(currency: str) -> StrategyConfig:
    return cfg(broker={"type": "kis"}, portfolio={"base_currency": currency})


def test_a_dollar_book_on_kis_is_told_that_starting_cash_is_the_real_basis():
    notes = preflight_warnings(kis("USD"))
    assert any("starting_cash" in n and "USD" in n for n in notes), notes


def test_a_won_book_on_kis_says_nothing_because_the_balance_is_read():
    assert not any("starting_cash" in n for n in preflight_warnings(kis("KRW")))


def test_toss_says_nothing_because_it_reports_cash_in_the_books_currency():
    """토스는 `_venue_capital` 로 통화를 붙여 말합니다 — 안 맞으면 게이트웨이가
    멈춥니다. 여기서 경고할 일이 아닙니다."""
    notes = preflight_warnings(cfg(portfolio={"base_currency": "USD"}))
    assert not any("starting_cash" in n for n in notes)


def test_the_warning_matches_what_the_adapter_actually_does():
    """경고와 어댑터가 따로 놀면, 둘 중 하나가 틀렸다는 사실이 아무 데도
    나타나지 않습니다. 실제로 `_venue_cash()` 를 불러서 맞춰 둡니다."""
    import asyncio

    from quant.brokerage.kis_broker import KisBrokerage
    from quant.core.account import Portfolio

    class _Kis(KisBrokerage):
        def __init__(self, currency):
            self.portfolio = Portfolio(0.0, currency)
            self._venue_deposit = 1_284_300.0

    for currency in ("USD", "KRW"):
        reads_balance = asyncio.run(_Kis(currency)._venue_cash()) is not None
        warned = any("starting_cash" in n
                     for n in preflight_warnings(kis(currency)))
        assert reads_balance is not warned, (
            f"{currency}: 어댑터는 잔고를 "
            f"{'읽는데' if reads_balance else '못 읽는데'} 경고는 "
            f"{'있습니다' if warned else '없습니다'}")
