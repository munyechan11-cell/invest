"""성적표는 전략을 재야 합니다 — 매도를 몇 번에 나눴는지가 아니라.

장부는 포지션을 줄이는 체결마다 한 줄을 씁니다. 손익과 거래세가 거기서
확정되니 그게 맞습니다. 문제는 성적표가 그 줄을 그대로 셌다는 것입니다:
`configs/demo.yaml` 한 판에서 조각 580개(그중 490개가 분할매도)를 "580거래
승률 74.7% 기대값 +6.99%" 로 인쇄하는데, 같은 판의 계좌는 -7.97% 입니다.
익절은 조금씩 덜어내고 손절은 한 번에 하는 장부에서는 조각 대부분이 이기니까요.

여기서 지키는 성질:

1. 성적표의 단위는 자리(진입~청산)다 — 같은 자리를 몇 번에 나눠 팔든 1건.
2. **경제적 무동작인 워시 트림(같은 값에 팔았다 즉시 되사기)은 성적표를
   한 톨도 움직이지 않는다.** 재매수 가격을 진입가 근처부터 청산가 위까지
   넓게 잡고 완전 일치를 요구합니다 — 앞선 시도들은 진입가 근처에서만
   재어서 두 자릿수 %p 오차를 놓쳤습니다.
3. 진짜로 자본을 더 넣으면 분모는 커져야 한다 (분모를 첫 진입에 얼려서
   2번을 통과시키는 길을 막습니다).
4. 회전율은 여전히 조각을 센다 — 돈이 오간 횟수를 묻는 지표니까.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.backtest.metrics import PerformanceReport, analyze
from quant.backtest.runner import BacktestResult
from quant.core.account import Portfolio
from quant.core.types import UTC, Fill, OrderSide, Symbol
from quant.optimize.losses import _thin_penalty

SYM = Symbol("T", venue="SIM", lot_size=Decimal("1"))
OTHER = Symbol("U", venue="SIM", lot_size=Decimal("1"))
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def fill(side, qty, price, day, fee=0.0, symbol=SYM):
    return Fill("o", symbol, side, Decimal(str(qty)), price, fee,
                T0 + timedelta(days=day))


def _mark_curve(pf, symbol, prices, first_day):
    """보유 중 몇 번 마크해 자산곡선을 평평하지 않게 만듭니다.

    `analyze` 는 수익률이 전부 0 인 장부에서 `tail_ratio` 의 0 나눗셈으로
    죽습니다. 이 티켓 밖의 기존 결함이라 고치지 않고 피해 갑니다.
    """
    for k, px in enumerate(prices, start=first_day):
        pf.mark(symbol, px)
        pf.record_equity(T0 + timedelta(days=k))


def wash_book(entry, exit_px, rebuy_price, washes, qty=100, half=50, fee=0.0):
    """진입 → (같은 값에 절반 팔았다 즉시 되사기) × washes → 전량 청산.

    washes 를 늘려도 계좌·자산곡선·포지션이 비트 단위로 같아야 합니다 —
    그걸 이 파일의 테스트가 직접 확인합니다.
    """
    pf = Portfolio(1_000_000.0)
    pf.record_equity(T0)
    pf.apply_fill(fill(OrderSide.BUY, qty, entry, day=0, fee=fee))
    for day in range(1, washes + 1):
        pf.apply_fill(fill(OrderSide.SELL, half, rebuy_price, day=day, fee=fee))
        pf.apply_fill(fill(OrderSide.BUY, half, rebuy_price, day=day, fee=fee))
    _mark_curve(pf, SYM, (entry * 1.02, entry * 0.99, entry * 1.03), 200)
    pf.apply_fill(fill(OrderSide.SELL, qty, exit_px, day=210, fee=fee))
    pf.record_equity(T0 + timedelta(days=210))
    return pf


SCORECARD = ("trades", "win_rate", "profit_factor", "expectancy",
             "avg_win", "avg_loss", "best_trade", "worst_trade")


# ── 1. 단위 ──────────────────────────────────────────────────────────────
def test_one_position_sold_in_slices_is_one_trade():
    """자리 하나를 여섯 번에 나눠 팔아도 성적표는 1건입니다.

    장부는 여전히 조각 여섯 줄을 갖고 있어야 합니다 — 되접기는 성적표에서만
    하는 일이고, 실현손익·거래세·보호장치는 조각을 그대로 봐야 합니다.
    """
    pf = Portfolio(1_000_000.0)
    pf.record_equity(T0)
    pf.apply_fill(fill(OrderSide.BUY, 60, 100.0, day=0))
    _mark_curve(pf, SYM, (102.0, 99.0, 103.0), 1)
    for i in range(6):
        pf.apply_fill(fill(OrderSide.SELL, 10, 110.0, day=10 + i))
    pf.record_equity(T0 + timedelta(days=20))

    rep = analyze(pf)
    assert len(pf.closed_trades) == 6, "장부는 조각을 그대로 갖고 있어야 합니다"
    assert rep.trades == 1
    assert rep.expectancy == pytest.approx(0.10)


def test_a_flip_ends_one_trade_and_starts_another():
    """롱을 닫고 숏을 여는 체결은 롱을 실제로 끝냈습니다 — 자리 경계입니다."""
    pf = Portfolio(1_000_000.0)
    pf.record_equity(T0)
    pf.apply_fill(fill(OrderSide.BUY, 10, 100.0, day=0))
    _mark_curve(pf, SYM, (102.0, 99.0, 103.0), 1)
    pf.apply_fill(fill(OrderSide.SELL, 15, 110.0, day=10))     # 롱 청산 + 숏 진입
    pf.apply_fill(fill(OrderSide.BUY, 5, 90.0, day=11))        # 숏 커버
    pf.record_equity(T0 + timedelta(days=12))

    rep = analyze(pf)
    assert rep.trades == 2
    # 롱: 10주가 100→110 (+10%), 숏: 5주가 110→90 (+18.18%)
    assert rep.best_trade == pytest.approx(100.0 / 550.0)
    assert rep.worst_trade == pytest.approx(0.10)


def test_trades_from_different_symbols_do_not_merge():
    pf = Portfolio(1_000_000.0)
    pf.record_equity(T0)
    pf.apply_fill(fill(OrderSide.BUY, 10, 100.0, day=0))
    pf.apply_fill(fill(OrderSide.BUY, 10, 100.0, day=0, symbol=OTHER))
    _mark_curve(pf, SYM, (102.0, 99.0, 103.0), 1)
    pf.apply_fill(fill(OrderSide.SELL, 5, 110.0, day=10))
    pf.apply_fill(fill(OrderSide.SELL, 10, 90.0, day=11, symbol=OTHER))
    pf.apply_fill(fill(OrderSide.SELL, 5, 110.0, day=12))
    pf.record_equity(T0 + timedelta(days=13))

    rep = analyze(pf)
    assert rep.trades == 2
    assert rep.win_rate == pytest.approx(0.5)


# ── 2. 매도 스케줄은 성적표를 움직일 수 없다 ─────────────────────────────
@pytest.mark.parametrize("rebuy_price", [100.0, 105.0, 130.0, 199.0, 400.0])
@pytest.mark.parametrize("washes", [1, 3, 6])
def test_a_wash_trim_is_a_no_op_for_the_account(rebuy_price, washes):
    """이 파일의 전제: 같은 값에 팔았다 되사기는 계좌에 아무 일도 아니다.

    전제가 깨지면 아래 불변성 테스트는 아무것도 증명하지 못하므로 따로 잽니다.
    """
    plain = wash_book(100.0, 200.0, rebuy_price, 0)
    washed = wash_book(100.0, 200.0, rebuy_price, washes)
    assert washed.cash == plain.cash
    assert washed.equity == plain.equity
    assert [(p.ts, p.equity) for p in washed.equity_curve] == \
           [(p.ts, p.equity) for p in plain.equity_curve]


@pytest.mark.parametrize("rebuy_price", [100.0, 105.0, 130.0, 199.0, 400.0])
@pytest.mark.parametrize("washes", [1, 3, 6])
def test_wash_trims_do_not_move_the_scorecard(rebuy_price, washes):
    """무동작 거래를 몇 개 끼워 넣어도 성적표는 **완전히** 같아야 합니다.

    밴드가 아니라 완전 일치입니다. 앞선 시도들은 재매수가를 진입가 근처에서만
    재고 "0.5%p 미만의 이동은 회계 사실" 이라고 적었는데, 재매수가를 청산가
    근처로 올리면 같은 자리의 수익률이 +100% 에서 +62% 로 내려앉았습니다.
    """
    plain = analyze(wash_book(100.0, 200.0, rebuy_price, 0))
    washed = analyze(wash_book(100.0, 200.0, rebuy_price, washes))
    assert [getattr(washed, f) for f in SCORECARD] == \
           [getattr(plain, f) for f in SCORECARD]
    assert plain.expectancy == pytest.approx(1.0), "10,000 을 묶어 10,000 을 벌었다"


@pytest.mark.parametrize("rebuy_price", [150.0, 300.0])
def test_wash_trims_do_not_shrink_a_loss(rebuy_price):
    """고점에서 씻고 물린 자리의 손실을 축소해 인쇄하면 안 됩니다.

    자금배분이 직접 읽는 숫자입니다. -60% 를 -21.8% 로 보여주면 자리당
    꼬리손실을 3배 작게 믿게 됩니다.
    """
    plain = analyze(wash_book(100.0, 40.0, rebuy_price, 0))
    washed = analyze(wash_book(100.0, 40.0, rebuy_price, 3))
    assert plain.worst_trade == pytest.approx(-0.60)
    assert washed.worst_trade == plain.worst_trade


def test_a_losing_position_cannot_print_a_winning_expectancy():
    """익절만 조금씩 덜어내고 크게 물린 자리 — 이 티켓이 지목한 실패 형태.

    조각을 세면 여섯 번 이기고 한 번 지므로 승률 86%·기대값 +22.9% 가 나오는데,
    같은 자리는 실제로 500 을 잃었습니다.
    """
    pf = Portfolio(1_000_000.0)
    pf.record_equity(T0)
    pf.apply_fill(fill(OrderSide.BUY, 100, 100.0, day=0))
    _mark_curve(pf, SYM, (102.0, 99.0, 103.0), 1)
    for i in range(6):
        pf.apply_fill(fill(OrderSide.SELL, 5, 130.0, day=10 + i))
    pf.apply_fill(fill(OrderSide.SELL, 70, 80.0, day=20))
    pf.record_equity(T0 + timedelta(days=21))

    realized = pf.equity - pf.starting_cash
    assert realized == pytest.approx(-500.0)
    rep = analyze(pf)
    assert rep.trades == 1
    assert rep.win_rate == 0.0
    assert rep.expectancy < 0, "계좌가 잃었는데 성적표가 이겼다고 하면 안 됩니다"
    assert rep.expectancy == pytest.approx(-0.05)


# ── 3. 분모를 얼려서 통과하는 길은 막는다 ────────────────────────────────
def test_adding_real_capital_does_enlarge_the_denominator():
    """트림 후 재매수와 달리, 진짜로 더 넣은 돈은 분모에 들어가야 합니다.

    분모를 첫 진입 금액에 얼려 버리면 워시 트림 불변성은 공짜로 통과하지만
    이 자리가 +100% 로 인쇄됩니다 — 실제로는 30,000 을 묶어 10,000 을 벌었습니다.
    """
    pf = Portfolio(1_000_000.0)
    pf.record_equity(T0)
    pf.apply_fill(fill(OrderSide.BUY, 100, 100.0, day=0))
    pf.apply_fill(fill(OrderSide.BUY, 100, 200.0, day=1))
    _mark_curve(pf, SYM, (204.0, 198.0, 206.0), 2)
    pf.apply_fill(fill(OrderSide.SELL, 200, 200.0, day=10))
    pf.record_equity(T0 + timedelta(days=11))

    rep = analyze(pf)
    assert pf.equity - pf.starting_cash == pytest.approx(10_000.0)
    assert rep.expectancy == pytest.approx(10_000.0 / 30_000.0)


def test_a_short_is_measured_against_the_notional_it_raised():
    """숏은 진입이 현금 유입이라, 부호를 정규화하지 않으면 분모가 0 이 됩니다."""
    pf = Portfolio(1_000_000.0)
    pf.record_equity(T0)
    pf.apply_fill(fill(OrderSide.SELL, 100, 100.0, day=0))
    _mark_curve(pf, SYM, (98.0, 101.0, 97.0), 1)
    for _ in range(3):                       # 60 에서 씻기 — 역시 무동작
        pf.apply_fill(fill(OrderSide.BUY, 50, 60.0, day=10))
        pf.apply_fill(fill(OrderSide.SELL, 50, 60.0, day=10))
    pf.apply_fill(fill(OrderSide.BUY, 100, 80.0, day=11))
    pf.record_equity(T0 + timedelta(days=12))

    rep = analyze(pf)
    assert pf.equity - pf.starting_cash == pytest.approx(2_000.0)
    assert rep.trades == 1
    assert rep.expectancy == pytest.approx(0.20)


def test_selling_far_above_cost_does_not_inflate_the_denominator():
    """번 돈이 묶은 돈으로 둔갑하면 안 됩니다.

    100 에 사서 1000 에 팔면 청산 대금 90,000 이 현금으로 들어옵니다. 오간
    현금의 절댓값을 분모로 쓰면 90,000 이 잡혀 +900% 가 +100% 로 찌그러집니다.
    """
    pf = Portfolio(1_000_000.0)
    pf.record_equity(T0)
    pf.apply_fill(fill(OrderSide.BUY, 100, 100.0, day=0))
    _mark_curve(pf, SYM, (102.0, 99.0, 103.0), 1)
    pf.apply_fill(fill(OrderSide.SELL, 100, 1_000.0, day=10))
    pf.record_equity(T0 + timedelta(days=11))

    rep = analyze(pf)
    assert rep.expectancy == pytest.approx(9.0)


# ── 4. 되접지 *않아야* 하는 것 ───────────────────────────────────────────
def test_turnover_still_counts_every_slice():
    """회전율은 "돈이 얼마나 오갔나" 입니다 — 나눠 판 체결도 실제로 오갔습니다."""
    plain = analyze(wash_book(100.0, 200.0, 150.0, 0))
    washed = analyze(wash_book(100.0, 200.0, 150.0, 6))
    assert washed.turnover > plain.turnover


def test_the_ledger_still_records_every_partial_exit():
    """성적표를 고친다고 장부에서 조각을 지우면 거래세·보호장치가 무너집니다."""
    pf = wash_book(100.0, 200.0, 150.0, 4)
    assert len(pf.closed_trades) == 5
    assert sum(1 for t in pf.closed_trades if not t.closes_position) == 4


# ── 5. 목적함수의 표본 문턱은 이 단위 변경에 움직이면 안 된다 ────────────
def _result(round_trips: int, ledger_records: int) -> BacktestResult:
    return BacktestResult(
        config_name="t", report=PerformanceReport(trades=round_trips),
        equity_curve=[], monthly={}, trades=[{}] * ledger_records,
        engine_summary={})


def test_the_sample_floor_did_not_move_when_the_scorecard_unit_changed():
    """`_thin_penalty` 는 예전부터 장부 레코드를 세었고, 계속 그래야 합니다.

    성적표의 `trades` 를 그대로 넣으면 문턱 20 이 walk-forward 학습창에서
    전부 켜집니다 — 지금까지 모든 후보에서 항등적으로 0 이라 없는 것과 같던
    항이 후보 순위를 정하는 항으로 바뀝니다. 눈금과 문턱을 함께 다시 정하는
    것은 기존 walk-forward 판정을 전부 무효로 만드는 별개 결정입니다.
    """
    # 자리 하나를 25조각으로 판 장부: 문턱은 예전과 같이 발동하지 않습니다.
    assert _thin_penalty(_result(round_trips=1, ledger_records=25), 2.0) == 0.0
    # 조각 자체가 모자라면 예전과 똑같이 발동합니다.
    assert _thin_penalty(_result(round_trips=1, ledger_records=6), 2.0) > 0.0
    assert _thin_penalty(_result(round_trips=0, ledger_records=0), 2.0) >= 1e6
