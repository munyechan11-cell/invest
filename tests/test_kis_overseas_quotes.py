"""한투 키 하나로 미국 시세까지 받습니다.

주문 어댑터는 진작 해외 모의투자를 지원했습니다(`VTTT1002U` 등). 그런데 시세
제공자가 `domestic-stock` 만 불러서, 미국을 돌리려면 토스나 야후를 따로 붙여야
했습니다. 야후는 15분 지연이라 **"연습에선 됐는데"** 를 만드는 자리입니다 —
화면에서 본 가격과 주문이 닿는 가격이 다르면 그 연습의 결과는 믿을 수 없습니다.

이 파일이 고정하는 것:

* 6자리 숫자면 국내, 아니면 해외 — 그리고 **국내 경로는 하나도 안 바뀝니다.**
* 거래소 코드가 두 벌(`NASD` 주문 / `NAS` 시세)이고, 섞이지 않는다.
* 못 읽은 값은 0 이 아니라 **버립니다.** 0 으로 채운 봉은 폭락으로 보입니다.
* 세 거래소가 전부 실패하면 **조용히 비우지 않고 이유를 말합니다** — 모의투자
  환경이 해외 시세를 안 주는 경우가 그리로 옵니다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.core.types import UTC, Symbol
from quant.data.providers.kis import (
    OVERSEAS_QUOTE_EXCHANGE,
    KisProvider,
)

T0 = datetime(2026, 9, 17, tzinfo=UTC)
US = Symbol("LLY", venue="kis", quote_currency="USD",
            tick_size=Decimal("0.01"), lot_size=Decimal("1"))
KR = Symbol("005930", venue="kis", quote_currency="KRW",
            tick_size=Decimal("100"), lot_size=Decimal("1"), tick_ladder="krx")

PRICE = {"rt_cd": "0", "output": {"last": "245.67", "base": "243.10",
                                  "rate": "1.06", "tvol": "51234567"}}
DAILY = {"rt_cd": "0", "output2": [
    {"xymd": "20260916", "open": "243.00", "high": "246.10", "low": "242.55",
     "clos": "245.67", "tvol": "51234567"},
    {"xymd": "20260915", "open": "240.10", "high": "243.80", "low": "239.90",
     "clos": "243.10", "tvol": "48111222"},
]}
DOMESTIC = {"rt_cd": "0", "output": {"stck_prpr": "71300", "hts_kor_isnm": "삼성전자",
                                     "prdy_ctrt": "1.2", "stck_mxpr": "92600",
                                     "stck_llam": "49900"}}


class _Kis(KisProvider):
    """네트워크 없이 도는 대역. 어느 거래소가 이 종목을 아는지 정합니다."""

    def __init__(self, knows="NYS", price=None, daily=None, **kw):
        super().__init__(app_key="k", app_secret="s",
                         allow_env_credentials=False, **kw)
        self.knows = knows
        self.price = PRICE if price is None else price
        self.daily = DAILY if daily is None else daily
        self.calls: list[tuple] = []

    async def _get(self, path, tr_id, params):
        leaf = path.rsplit("/", 1)[-1]
        self.calls.append((params.get("EXCD"), leaf))
        if "domestic-stock" in path:
            return DOMESTIC
        if self.knows is None or params.get("EXCD") != self.knows:
            raise RuntimeError("해당 종목 없음")
        return self.daily if leaf == "dailyprice" else self.price


def history(provider, symbol=US, days=20):
    return asyncio.run(provider.history(symbol, "1d", T0 - timedelta(days=days), T0))


# ── 국내인가 해외인가 ────────────────────────────────────────────────────
@pytest.mark.parametrize("ticker,domestic", [
    ("005930", True), ("000660", True),
    ("AAPL", False), ("LLY", False), ("BRK.B", False), ("A", False),
])
def test_six_digits_is_domestic_everything_else_is_not(ticker, domestic):
    assert bool(KisProvider._domestic_code(ticker)) is domestic


def test_a_us_ticker_is_no_longer_mangled_into_a_korean_code():
    """예전에는 숫자만 뽑아 `zfill(6)` 했습니다 — `AAPL` 이 `000000` 이 되어
    없는 국내 종목을 조회했고, 답은 맞지만 이유가 틀렸습니다."""
    assert KisProvider._domestic_code("AAPL") == ""


def test_the_domestic_path_is_untouched():
    """이 변경이 국내 조회를 건드리면 안 됩니다."""
    provider = _Kis()
    quote = asyncio.run(provider.quote(KR))
    assert quote.bid == 71_200 and quote.ask == 71_400      # 71,300 ± 한 틱
    assert all("domestic" in leaf or leaf == "inquire-price"
               for _excd, leaf in provider.calls)
    assert provider.calls[0][0] is None, "국내 호출에 EXCD 가 붙으면 안 됩니다"


# ── 해외 시세 ────────────────────────────────────────────────────────────
def test_a_us_quote_comes_back_with_a_cent_spread():
    quote = asyncio.run(_Kis().quote(US))
    assert quote.mid == pytest.approx(245.67)
    assert quote.ask - quote.bid == pytest.approx(0.02)     # 미국 틱은 $0.01


def test_us_daily_bars_parse():
    bars = history(_Kis())
    assert [b.ts.strftime("%Y-%m-%d") for b in bars] == ["2026-09-15", "2026-09-16"]
    assert bars[-1].open == 243.00 and bars[-1].close == 245.67
    assert bars[-1].volume == 51_234_567


# ── 거래소 코드 두 벌 ────────────────────────────────────────────────────
def test_the_order_code_is_translated_to_the_quote_code():
    """`NASD`(주문) 와 `NAS`(시세) 는 한 글자 차이라, 섞어 쓰면 "없는 종목" 이
    돌아오고 그 답은 티커 오타와 구별되지 않습니다."""
    assert OVERSEAS_QUOTE_EXCHANGE["NASD"] == "NAS"
    assert OVERSEAS_QUOTE_EXCHANGE["NYSE"] == "NYS"
    assert OVERSEAS_QUOTE_EXCHANGE["AMEX"] == "AMS"
    assert _Kis(overseas_exchange="NYSE").overseas_exchange == "NYS"


def test_it_searches_the_other_exchanges_and_remembers():
    provider = _Kis(knows="AMS")
    assert asyncio.run(provider.quote(US)) is not None
    assert [e for e, _ in provider.calls] == ["NAS", "NYS", "AMS"]

    provider.calls.clear()
    asyncio.run(provider.quote(US))
    assert [e for e, _ in provider.calls] == ["AMS"], "맞은 곳을 기억해야 합니다"


def test_the_configured_exchange_is_tried_first():
    provider = _Kis(knows="NYS", overseas_exchange="NYSE")
    asyncio.run(provider.quote(US))
    assert provider.calls[0][0] == "NYS"


# ── 모르는 것을 지어내지 않습니다 ────────────────────────────────────────
def test_a_row_with_an_unreadable_price_is_dropped_not_zeroed():
    """0 으로 채운 봉은 지표에 들어가 폭락으로 읽힙니다."""
    broken = {"rt_cd": "0", "output2": [
        dict(DAILY["output2"][0]),
        {"xymd": "20260915", "open": "", "high": "1", "low": "1", "clos": "x"},
    ]}
    bars = history(_Kis(daily=broken))
    assert [b.ts.strftime("%Y-%m-%d") for b in bars] == ["2026-09-16"]


def test_a_quote_of_zero_is_no_quote():
    assert asyncio.run(_Kis(price={"rt_cd": "0", "output": {"last": "0"}}).quote(US)) is None


def test_no_exchange_knowing_it_says_why_instead_of_returning_nothing():
    """모의투자 환경이 해외 시세를 안 주면 여기로 옵니다. 빈 목록을 돌려주면
    "오늘 거래가 없었다" 와 구별되지 않습니다."""
    with pytest.raises(RuntimeError, match="해외 시세를 읽지 못했습니다"):
        history(_Kis(knows=None))


def test_the_error_names_the_environment():
    with pytest.raises(RuntimeError, match="paper=True"):
        history(_Kis(knows=None))


def test_a_quote_failure_is_soft_because_callers_expect_none():
    """호가는 없을 수 있는 값입니다 — `quote()` 의 계약이 그렇습니다."""
    assert asyncio.run(_Kis(knows=None).quote(US)) is None


# ── resolve / describe ───────────────────────────────────────────────────
def test_resolving_a_us_ticker_gives_a_dollar_symbol_without_the_krx_ladder():
    """미국은 상하한가가 없고 호가단위가 가격과 무관하게 $0.01 입니다 —
    국내 사다리를 켜면 미국 종목에 원화 격자가 걸립니다."""
    symbol = asyncio.run(_Kis().resolve("lly"))
    assert symbol.ticker == "LLY" and symbol.quote_currency == "USD"
    assert symbol.tick_size == Decimal("0.01") and symbol.tick_ladder == ""


def test_resolving_a_domestic_code_still_turns_the_ladder_on():
    symbol = asyncio.run(_Kis().resolve("005930"))
    assert symbol.quote_currency == "KRW" and symbol.tick_ladder == "krx"


def test_describe_reports_no_price_limits_for_a_us_name():
    """미국은 상하한가가 없습니다. 0 을 넣으면 화면이 "상한가 0" 을 그립니다."""
    info = asyncio.run(_Kis().describe("LLY"))
    assert info["currency"] == "USD" and info["tick_size"] == 0.01
    assert info["upper_limit"] is None and info["lower_limit"] is None
    assert info["market"] == "NYS"


def test_describe_leaves_the_name_blank_rather_than_echoing_the_ticker():
    """티커를 이름 자리에 넣으면 "증권사가 이 종목의 이름을 이렇게 준다" 는
    뜻이 됩니다. 부르는 쪽이 자기 표로 물러설 수 있어야 합니다."""
    assert asyncio.run(_Kis().describe("LLY"))["name"] == ""


def test_a_weekly_request_maps_to_the_venues_weekly_code():
    provider = _Kis()
    # 주봉은 **마감된 것만** 돌려줍니다. 9/16 봉의 주는 9/23 에 끝나므로
    # 창이 거기까지 열려 있어야 나옵니다 — 아직 그려지는 중인 봉을 내주면
    # 그게 곧 미래 참조입니다.
    assert asyncio.run(provider.history(US, "1w", T0 - timedelta(days=60),
                                        T0 + timedelta(days=10)))
    assert provider.calls, "호출이 없었습니다"


def test_an_unclosed_weekly_bar_is_withheld():
    assert asyncio.run(_Kis().history(US, "1w", T0 - timedelta(days=60), T0)) == []


def test_an_unknown_timeframe_is_refused_not_guessed():
    with pytest.raises(ValueError, match="1d"):
        asyncio.run(_Kis().history(US, "5m", T0 - timedelta(days=2), T0))


def test_paging_stops_when_the_venue_keeps_returning_the_same_rows():
    """같은 장이 계속 돌아오면 커서가 안 움직여 무한히 돕니다."""
    provider = _Kis()
    history(provider, days=900)
    assert len(provider.calls) < 20, f"{len(provider.calls)}번 불렀습니다"


# ── 기억이 틀렸을 때 ─────────────────────────────────────────────────────
class _Flaky(_Kis):
    """맞았던 거래소가 나중에 오류를 내는 대역."""

    def __init__(self, fail_after=1, **kw):
        super().__init__(**kw)
        self.fail_after = fail_after
        self.rounds = 0

    async def _get(self, path, tr_id, params):
        self.rounds += 1
        if self.rounds > self.fail_after:
            raise RuntimeError("일시 오류")
        return await super()._get(path, tr_id, params)


def test_a_remembered_exchange_that_starts_failing_does_not_go_quiet():
    """기억한 곳이 실패하면 예전에는 **빈 응답** 을 돌려줬습니다 — 일봉은
    빈 목록이 되고 호가는 `None` 이 되어, "오늘 거래가 없었다" 와 구별되지
    않았습니다. 처음 조회가 실패할 때는 말하면서 두 번째부터는 입을 다무는
    셈이었습니다."""
    provider = _Flaky(knows="NAS", fail_after=1)
    asyncio.run(provider.quote(US))                  # 여기서 NAS 를 기억합니다
    with pytest.raises(RuntimeError, match="해외 시세를 읽지 못했습니다"):
        history(provider)


def test_a_failed_lookup_forgets_the_exchange_it_had_remembered():
    """맞지 않는 기억을 남겨 두면 다음 호출도 같은 곳부터 갑니다."""
    provider = _Flaky(knows="NAS", fail_after=1)
    asyncio.run(provider.quote(US))
    assert provider._exchange_of.get("LLY") == "NAS"
    with pytest.raises(RuntimeError):
        history(provider)
    assert "LLY" not in provider._exchange_of


def test_it_still_searches_the_other_exchanges_after_a_remembered_one_fails():
    """기억한 곳만 물어보고 포기하면, 종목이 다른 거래소로 옮겨간 경우를
    영영 못 찾습니다."""
    provider = _Kis(knows="NAS")
    asyncio.run(provider.quote(US))
    provider.knows = "AMS"                           # 이제 AMS 만 압니다
    provider.calls.clear()
    assert asyncio.run(provider.quote(US)) is not None
    assert [e for e, _ in provider.calls] == ["NAS", "NYS", "AMS"]


def test_reaching_the_start_of_a_symbols_history_keeps_what_it_read():
    """상장일 이전을 물으면 창구가 오류로 답할 수 있습니다. 그건 그 종목의
    역사가 끝난 지점이지 고장이 아니므로, 읽은 것까지는 돌려줍니다."""
    provider = _Flaky(knows="NAS", fail_after=1)
    bars = history(provider, days=900)
    assert [b.ts.strftime("%Y-%m-%d") for b in bars] == ["2026-09-15", "2026-09-16"]
