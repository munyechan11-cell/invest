"""국내 호가단위는 가격대마다 달라지고, 주문 격자는 그것을 따라야 합니다.

설정에 고정 `tick_size` 하나만 적어 두면 종목이 가격대 경계를 넘는 순간
격자 밖 지정가가 되고 거래소가 거절합니다. 진입만 막히는 것이 아닙니다 —
**손절도 지정가로 나갑니다.** 그래서 그 종목은 매 봉 거절만 반복하고
포지션에서 빠져나갈 길이 없어집니다. 조용한 결함의 전형입니다: 예외도
로그도 없고, 화면은 정상이고, 거래소만 계속 거절합니다.
"""
from decimal import Decimal

import pytest

from quant.cli import _load
from quant.core.types import (
    KRX_TICK_LADDER,
    KRX_TICK_TOP,
    OrderSide,
    Symbol,
    krx_tick_size,
)
from quant.data.providers.kis import korean_tick_size
from tests.conftest import shipped_configs

#: 국내 종목을 다루는 출하 설정 전부. 이름을 박아 두면 설정 하나를 옮길 때마다
#: 가드가 없는 파일을 찾다가 깨집니다 — 지켜야 하는 것은 **그때그때 목록에
#: 있는 것들** 입니다.
KR_LIVE = [path for path in shipped_configs()
           if any(sym.quote_currency.upper() == "KRW"
                  for sym in _load(path).universe.symbols)]


def kr(tick: str = "100", ladder: str = "krx") -> Symbol:
    return Symbol("005930", venue="toss", quote_currency="KRW",
                  tick_size=Decimal(tick), tick_ladder=ladder)


# ── 사다리 자체 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("price,tick", [
    (1_999, "1"), (2_000, "5"), (4_999, "5"), (5_000, "10"),
    (19_999, "10"), (20_000, "50"), (49_999, "50"), (50_000, "100"),
    (199_999, "100"), (200_000, "500"), (499_999, "500"), (500_000, "1000"),
    (1_200_000, "1000"),
])
def test_the_ladder_matches_the_2023_krx_table(price, tick):
    assert krx_tick_size(price) == Decimal(tick)


def test_the_display_path_reads_the_same_table_as_the_order_path():
    """화면과 주문이 다른 표를 읽으면, 화면은 맞는데 주문만 거절됩니다 —
    그리고 둘이 다르다는 사실은 어디에도 나타나지 않습니다."""
    for price in (900, 3_300, 12_000, 33_000, 120_000, 330_000, 900_000):
        assert korean_tick_size(price) == krx_tick_size(price)


def test_every_coarser_tick_is_a_multiple_of_every_finer_one():
    """사다리가 중첩이라는 사실이 올림 한 칸을 안전하게 만듭니다."""
    ticks = [Decimal(t) for _, t in KRX_TICK_LADDER] + [Decimal(KRX_TICK_TOP)]
    for fine, coarse in zip(ticks, ticks[1:]):
        assert coarse % fine == 0, f"{coarse} 는 {fine} 의 배수가 아닙니다"


def test_rounding_up_across_a_band_boundary_still_lands_on_the_grid():
    """경계 바로 아래 매도를 올리면 다음 칸으로 넘어갑니다. 경계값이 다음
    칸 틱의 배수가 아니면 그 올림이 곧 거절입니다."""
    sym = kr()
    for threshold, _ in KRX_TICK_LADDER:
        price = Decimal(threshold) - Decimal("1")
        snapped = sym.round_price(price, OrderSide.SELL)
        assert snapped % krx_tick_size(snapped) == 0, (
            f"{price} 매도 올림 → {snapped}, "
            f"그 가격대의 틱 {krx_tick_size(snapped)} 격자 밖"
        )


# ── Symbol 의 동작 ───────────────────────────────────────────────────────
def test_the_ladder_prices_by_band_not_by_the_configured_tick():
    sym = kr(tick="100")                       # 설정은 100 이지만
    assert sym.round_price(213_400, OrderSide.BUY) == Decimal("213_000")
    assert sym.round_price(213_400, OrderSide.SELL) == Decimal("213_500")
    assert sym.tick_at(213_400) == Decimal("500")
    assert sym.tick_at(120_000) == Decimal("100")


def test_without_the_ladder_nothing_changes():
    """사다리를 켜지 않은 설정은 예전 그대로 고정 틱입니다 — 이 기능이
    남의 설정을 조용히 바꿔치기하면 안 됩니다."""
    sym = kr(tick="100", ladder="")
    assert sym.round_price(213_400, OrderSide.BUY) == Decimal("213_400")
    assert sym.tick_at(213_400) == Decimal("100")


def test_an_unknown_ladder_name_falls_back_to_the_configured_tick():
    sym = kr(tick="100", ladder="krxx")
    assert sym.round_price(213_450, OrderSide.BUY) == Decimal("213_400")


def test_a_zero_tick_still_means_no_grid():
    sym = Symbol("SYN", venue="SIM", tick_size=Decimal("0"))
    assert sym.round_price(1.23456, OrderSide.BUY) == Decimal("1.23456")


# ── 설정이 실제로 켜져 있는가 ────────────────────────────────────────────


@pytest.mark.parametrize("path", KR_LIVE)
def test_every_shipped_korean_symbol_uses_the_ladder(path, monkeypatch):
    for var in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_NO",
                "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GOOGLE_API_KEY"):
        monkeypatch.setenv(var, "x")
    config = _load(path)
    krw = [s for s in config.universe.symbols if s.quote_currency.upper() == "KRW"]
    assert krw, f"{path} 에 국내 종목이 없습니다"
    for spec in krw:
        assert spec.tick_ladder == "krx", (
            f"{path} 의 {spec.ticker} 이 고정 틱 {spec.tick_size} 을 씁니다 — "
            "가격대가 바뀌면 지정가가 거절되고 손절도 나가지 못합니다"
        )


def test_a_typo_in_the_ladder_name_is_rejected_not_ignored():
    from pydantic import ValidationError

    from quant.config.schema import SymbolSpec

    SymbolSpec(ticker="005930", tick_ladder="KRX")      # 대소문자는 봐 줍니다
    with pytest.raises(ValidationError, match="tick_ladder"):
        SymbolSpec(ticker="005930", tick_ladder="kospi")
