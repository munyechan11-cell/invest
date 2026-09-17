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


# ── 구간의 폭이 아니라 남은 여유 ─────────────────────────────────────────
#
# 출하 설정 전부가 천장에서 5~20% 아래에서 시작합니다. `max_order_notional`
# 을 `starting_cash × 비중 × (1-예비)` 바로 위로 잡아 둔 결과인데, 그 의도는
# 맞아도 **계좌가 자라면 그 의도가 벽이 됩니다.** 그리고 그 벽에 닿는 날은
# 계좌가 잘 되고 있는 날입니다.
#
# 캡을 올려서 예행연습만 편하게 만들지는 않습니다 — 예행연습이 실거래와 같은
# 벽을 만나야 예행연습입니다(하루 한도를 모의투자에도 거는 것과 같은 이유).
# 대신 시작하기 전에 그 거리를 말합니다.

def test_starting_just_under_the_ceiling_is_said_out_loud():
    # 1,000 × 0.30 × 0.95 = 285, 캡 300 → 천장 1,053. 시작 1,000 은 95% 지점.
    notes = preflight_warnings(cfg(mode="dry_run"))
    assert any("천장의 95%" in n for n in notes), notes
    assert any("5% 만 늘어도" in n for n in notes)


def test_it_says_which_two_knobs_move_the_wall():
    note = [n for n in preflight_warnings(cfg(mode="dry_run")) if "천장의" in n][0]
    assert "max_order_notional" in note and "max_position_weight" in note


def test_the_warning_says_exits_still_go_out():
    """이 문장이 "지금 포지션에 갇힌다" 로 읽히면 안 됩니다."""
    note = [n for n in preflight_warnings(cfg(mode="dry_run")) if "천장의" in n][0]
    assert "청산과 손절은 그대로 나갑니다" in note


def test_starting_just_above_the_floor_is_said_too():
    notes = preflight_warnings(cfg(mode="dry_run",
                                   portfolio={"starting_cash": 750}))
    assert any("바닥에서" in n for n in notes), notes


def test_a_comfortable_start_says_nothing_about_walls():
    """늘 켜져 있는 경고는 아무도 안 읽습니다."""
    notes = preflight_warnings(cfg(mode="dry_run",
                                   broker={"max_order_notional": 3_000},
                                   portfolio={"starting_cash": 2_000}))
    assert not any("천장의" in n or "바닥에서" in n for n in notes), notes


def test_every_shipped_live_config_says_where_its_wall_is(env):
    """출하 설정이 이 사실을 말하지 않고 나가면, 계좌가 5% 늘어난 날
    처음으로 알게 됩니다."""
    for path in LIVE:
        config = _load(path)
        if entry_window(config) is None:
            continue
        assumed = config.portfolio.starting_cash
        low, high = entry_window(config)
        if not (high < assumed * 1.25 or assumed < low * 1.25):
            continue
        notes = preflight_warnings(config)
        assert any("천장의" in n or "바닥에서" in n for n in notes), (
            f"{path} 은 벽 바로 앞에서 시작하는데 아무 말도 하지 않습니다")


# ── 주문 상한은 하루 예산에서 옵니다 ─────────────────────────────────────
#
# 예전에는 `starting_cash × 비중 × (1-예비)` 바로 위로 잡혀 있었습니다.
# 그러면 계좌가 5% 만 자라도 신규 진입이 전부 거절됩니다 — 그리고 그 벽에
# 닿는 날은 계좌가 잘 되고 있는 날입니다.
#
# 이제 **하루 거래대금 한도와 같은 값** 입니다. 하루 예산보다 큰 주문은
# 어차피 하루 한도에서 거절되므로 그보다 크게 잡는 것은 의미가 없고, 작게
# 잡으면 계좌가 자랄 때 먼저 닿는 두 번째 벽이 됩니다.
#
# **하루 총 노출은 이 규칙으로 바뀌지 않습니다.** 오늘 나갈 수 있는 주문
# 크기는 여전히 포트폴리오의 비중이 정하고, 하루 총량은 하루 한도가 정합니다.

@pytest.mark.parametrize("path", LIVE)
def test_the_per_order_cap_is_not_a_second_tighter_wall(path, env):
    config = _load(path)
    cap = config.broker.max_order_notional
    daily = config.limits.max_daily_notional
    if not cap or not daily:
        return
    assert cap >= daily, (
        f"{path}: 주문 상한 {cap:,.0f} 이 하루 한도 {daily:,.0f} 보다 낮습니다 — "
        "계좌가 자라면 하루 예산을 다 쓰기 전에 이 칸이 먼저 막습니다")


@pytest.mark.parametrize("path", LIVE)
def test_a_shipped_config_can_grow_by_half_before_it_hits_the_wall(path, env):
    """출하 설정이 천장 바로 아래에서 시작하면, 계좌가 조금만 잘 돼도
    그날로 신규 진입이 멈춥니다."""
    config = _load(path)
    window = entry_window(config)
    if window is None:
        return
    _low, high = window
    assumed = config.portfolio.starting_cash
    assert high >= assumed * 1.5, (
        f"{path}: 시작 자본 {assumed:,.0f} 에서 천장 {high:,.0f} 까지 "
        f"{high / assumed:.2f}배뿐입니다")


def test_raising_the_cap_did_not_touch_the_floor():
    """바닥(최소 주문금액)은 비용 통제입니다 — 천장을 올린다고 같이
    올리면 작은 계좌가 아무것도 못 삽니다."""
    config = _load("configs/kr_kis_paper.yaml")
    assert config.execution.min_order_notional == 5_000_000
    assert entry_window(config)[0] == pytest.approx(5_000_000 / (0.35 * 0.95))
