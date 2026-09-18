"""국내 일봉 창구가 500 을 냅니다 — 모의투자에서도, **실계좌에서도.**

    봇이 멈췄습니다 — 시세를 받지 못해 시작할 수 없습니다 (005930, 000660,
    035420). 증권사가 말한 이유: Server error '500 Internal Server Error' for
    url 'https://openapi.koreainvestment.com:9443/…/inquire-daily-itemchartprice…

어제는 이것이 모의투자 호스트만의 문제라고 봤습니다. 틀렸습니다. 현재가는
양쪽에서 오는데 **기간별시세만** 양쪽에서 500 입니다. 요청 형태는 이 저장소가
처음부터 쓰던 것 그대로이고, 여기서 원인을 특정할 방법은 없습니다.

특정할 수 없는 것을 특정한 척하는 대신, **두 번째 문** 을 붙입니다. 한투는
같은 일봉을 `inquire-daily-price`(`FHKST01010400`) 로도 줍니다. 기간을 못
받아 워밍업을 다 채우지는 못하지만, 봇이 시작조차 못 하는 것보다 낫습니다 —
부족한 봉은 "신호를 낼 수 없다" 로 따로 말해 줍니다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from quant.core.types import UTC, Symbol
from quant.data.providers.kis import KisProvider

T0 = datetime(2026, 9, 18, tzinfo=UTC)
KR = Symbol("005930", venue="kis", quote_currency="KRW", tick_ladder="krx")

CHART = {"rt_cd": "0", "output2": [
    {"stck_bsop_date": "20260916", "stck_oprc": "71000", "stck_hgpr": "71500",
     "stck_lwpr": "70800", "stck_clpr": "71300", "acml_vol": "9000000"},
    {"stck_bsop_date": "20260915", "stck_oprc": "70500", "stck_hgpr": "71200",
     "stck_lwpr": "70100", "stck_clpr": "71000", "acml_vol": "8000000"},
]}
DAILY = {"rt_cd": "0", "output": [
    {"stck_bsop_date": "20260916", "stck_oprc": "71000", "stck_hgpr": "71500",
     "stck_lwpr": "70800", "stck_clpr": "71300", "acml_vol": "9000000"},
    {"stck_bsop_date": "20260915", "stck_oprc": "70500", "stck_hgpr": "71200",
     "stck_lwpr": "70100", "stck_clpr": "71000", "acml_vol": "8000000"},
    {"stck_bsop_date": "20260914", "stck_oprc": "", "stck_clpr": "x"},   # 못 읽는 줄
]}


class _Kis(KisProvider):
    def __init__(self, chart=True, daily=True, **kw):
        super().__init__(app_key="k", app_secret="s",
                         allow_env_credentials=False, **kw)
        self.chart_ok, self.daily_ok = chart, daily
        self.calls: list[str] = []

    async def _get(self, path, tr_id, params):
        leaf = path.rsplit("/", 1)[-1]
        self.calls.append(leaf)
        if leaf == "inquire-daily-itemchartprice":
            if not self.chart_ok:
                raise RuntimeError(
                    "Server error '500 Internal Server Error' for url "
                    "'https://openapi.koreainvestment.com:9443/uapi/domestic-stock"
                    "/v1/quotations/inquire-daily-itemchartprice?FID_COND_MRKT_"
                    "DIV_CODE=J&FID_INPUT_ISCD=005930&FID_INPUT_DATE_1=20260501'")
            return CHART
        if leaf == "inquire-daily-price":
            if not self.daily_ok:
                raise RuntimeError("500 again")
            return DAILY
        raise AssertionError(f"뜻밖의 호출: {leaf}")


def history(provider, days=200):
    return asyncio.run(provider.history(KR, "1d", T0 - timedelta(days=days), T0))


# ── 첫 번째 문이 열려 있으면 두 번째는 두드리지 않습니다 ────────────────
def test_the_normal_path_is_unchanged():
    provider = _Kis()
    bars = history(provider)
    assert [b.ts.strftime("%Y-%m-%d") for b in bars] == ["2026-09-15", "2026-09-16"]
    assert "inquire-daily-price" not in provider.calls


# ── 닫혔을 때 ────────────────────────────────────────────────────────────
def test_a_500_falls_back_instead_of_stopping_the_bot():
    provider = _Kis(chart=False)
    bars = history(provider)
    assert bars, "두 번째 문이 있는데 빈손으로 돌아왔습니다"
    assert "inquire-daily-price" in provider.calls
    assert bars[-1].close == 71_300


def test_the_fallback_drops_rows_it_cannot_read():
    """0 으로 채운 봉은 지표에 들어가 폭락으로 읽힙니다."""
    days = {b.ts.strftime("%Y-%m-%d") for b in history(_Kis(chart=False))}
    assert "2026-09-14" not in days


def test_both_doors_closed_says_so_with_both_reasons():
    """한쪽 이유만 말하면 "그 엔드포인트만 고치면 되겠네" 로 읽힙니다."""
    with pytest.raises(RuntimeError) as err:
        history(_Kis(chart=False, daily=False))
    text = str(err.value)
    assert "기간별시세" in text and "일자별시세" in text
    assert "시세 조회" in text, "무엇을 확인해야 하는지가 없습니다"


def test_the_error_does_not_paste_a_truncated_url_across_the_screen():
    """화면이 잘린 URL 로 가득 차면 아무도 안 읽습니다."""
    with pytest.raises(RuntimeError) as err:
        history(_Kis(chart=False, daily=False))
    assert len(str(err.value)) < 420
    assert "\\n" not in str(err.value)


def test_a_working_first_door_after_a_failed_page_keeps_what_it_read():
    """첫 장은 왔는데 다음 장이 500 이면, 읽은 것까지는 돌려줍니다 —
    그리고 두 번째 문을 굳이 두드리지 않습니다."""
    provider = _Kis()
    calls = {"n": 0}
    original = provider._get

    async def flaky(path, tr_id, params):
        if path.endswith("itemchartprice"):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("500")
        return await original(path, tr_id, params)

    provider._get = flaky
    bars = history(provider, days=400)
    assert bars and "inquire-daily-price" not in provider.calls
