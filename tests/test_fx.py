"""통화 환산 계층 — 조용히 틀린 숫자가 나오지 않는가.

이 저장소에서 통화는 가장 위험한 자리였습니다. 원화 종목과 달러 종목을 한
유니버스에 넣어도 **에러가 나지 않았고**, 7만(원)과 250(달러)이 같은 자릿수로
더해져 70,250 이라는 숫자가 화면에 아무 표시 없이 떴습니다. 실제로 그랬습니다:

    >>> pf.holdings_value      # 70,000원짜리 1주 + $250짜리 1주
    70250.0

그래서 이 파일이 검사하는 것은 "환산이 되는가" 가 아니라 **틀린 숫자가 조용히
나올 수 있는 길이 전부 막혔는가** 입니다. 네 갈래가 있습니다.

1. **기준통화 경로는 조회도 곱셈도 하지 않는다.** 단일 통화 운용은 이 계층이
   생기기 전과 완전히 같은 숫자를 내야 합니다. 그래서 소스를 부르면 터지는
   가짜 소스를 물려 두고 봅니다 — "환율 1.0 을 받아서 곱한다" 는 구현은 여기서
   죽습니다.
2. **환율은 그 시각의 것.** 지금 환율로 어제 체결을 환산하면 손익에 환차익이
   섞이고, 전략이 번 것과 원달러가 움직인 것을 구분할 수 없게 됩니다.
3. **못 받으면 멈춘다.** 마지막으로 받은 값으로 이어 쓰지 않습니다. 하루 묵은
   환율은 그날의 모든 외화 주문을 틀린 크기로 내보냅니다.
4. **섞인 설정은 시작을 막는다.** 아직 장부가 환산을 하지 않으므로, 유일하게
   안전한 답은 시작하지 않는 것입니다.

토스 호출은 `httpx.MockTransport` 로 대신합니다 — 실제 API 는 부르지 않습니다.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from quant.brokerage import toss_broker as T
from quant.config.schema import (
    ModelSpec,
    PortfolioConfig,
    StrategyConfig,
    SymbolSpec,
    UniverseConfig,
)
from quant.core.fx import (
    Fx,
    FxRate,
    FxRateSource,
    FxUnavailable,
    create_fx_source,
)
from quant.core.types import UTC
from quant.data.providers.toss_fx import TossFxSource

CLIENT_ID = "test-client-id"
T1 = datetime(2026, 3, 25, 0, 30, tzinfo=UTC)      # 09:30 KST
T2 = T1 + timedelta(days=1)


# ── 가짜 소스 ────────────────────────────────────────────────────────────
class Recording(FxRateSource):
    """물어본 것을 기록하고, 시각별로 미리 정해 둔 환율을 돌려줍니다."""

    name = "recording"

    def __init__(self, by_instant: dict[datetime, float] | None = None,
                 default: float | None = 1380.0):
        self.by_instant = by_instant or {}
        self.default = default
        self.asked: list[tuple[str, str, datetime]] = []
        self.fail: str | None = None

    async def rate(self, source: str, target: str, when: datetime) -> FxRate:
        self.asked.append((source, target, when))
        if self.fail:
            raise RuntimeError(self.fail)
        value = self.by_instant.get(when, self.default)
        if value is None:
            raise RuntimeError("환율 없음")
        return FxRate(source, target, value, when, origin=self.name)


class Exploding(FxRateSource):
    """부르는 것 자체가 실패입니다 — 기준통화 경로를 재는 데 씁니다."""

    name = "exploding"

    async def rate(self, source, target, when):
        raise AssertionError(
            f"기준통화 금액에 환율을 물었습니다 ({source}→{target})")


# ─────────────────────────────────────────────────────────────────────────
# 1. 기준통화 경로 — 지금과 완전히 같은 숫자
# ─────────────────────────────────────────────────────────────────────────
async def test_a_base_currency_amount_is_passed_through_without_any_lookup():
    """단일 통화 운용은 이 계층이 있든 없든 같은 숫자여야 합니다.

    소스를 아예 부르지 않는 것이 그 보증입니다. "환율 1.0 을 받아 곱한다" 는
    구현도 대개 같은 값을 내지만, 그 구현은 환율 소스가 죽는 날 단일 통화
    운용까지 함께 멈춥니다.
    """
    fx = Fx("KRW", source=Exploding())

    assert await fx.to_base(70_000.0, "KRW", T1) == 70_000.0
    # 나누어떨어지지 않는 값도 비트까지 그대로.
    assert await fx.to_base(1 / 3, "KRW", T1) == 1 / 3
    # 대소문자와 공백은 표기 차이일 뿐 다른 통화가 아닙니다.
    assert await fx.to_base(250.0, " krw ", T1) == 250.0
    assert await fx.rate_to_base("KRW", T1) == 1.0


async def test_a_foreign_amount_with_no_rate_source_is_refused_not_assumed_to_be_one():
    """소스가 없을 때 1.0 으로 물러서면 그게 바로 70,250 원짜리 고장입니다."""
    fx = Fx("KRW")            # 소스 없음 = 기준통화 전용

    with pytest.raises(FxUnavailable) as exc:
        await fx.to_base(250.0, "USD", T1)
    assert "USD" in str(exc.value) and "KRW" in str(exc.value)


# ─────────────────────────────────────────────────────────────────────────
# 2. 환율은 그 시각의 것
# ─────────────────────────────────────────────────────────────────────────
async def test_each_instant_is_converted_at_its_own_rate():
    """어제 체결을 오늘 환율로 환산하면 손익에 환차익이 섞여 들어갑니다."""
    source = Recording({T1: 1300.0, T2: 1400.0}, default=None)
    fx = Fx("KRW", source=source)

    assert await fx.to_base(100.0, "USD", T1) == pytest.approx(130_000.0)
    assert await fx.to_base(100.0, "USD", T2) == pytest.approx(140_000.0)
    assert [asked[2] for asked in source.asked] == [T1, T2]
    # 방향도 함께: 물어본 것은 "1 USD 가 KRW 로 얼마인가" 입니다.
    assert {(a[0], a[1]) for a in source.asked} == {("USD", "KRW")}


async def test_the_instant_asked_for_is_the_caller_s_instant_not_now():
    """봉 시각을 그대로 넘기지 않으면 위 검사는 통과해도 실전에서 틀립니다."""
    source = Recording()
    fx = Fx("KRW", source=source)
    await fx.to_base(1.0, "USD", T1)

    asked = source.asked[0][2]
    assert asked.year == T1.year and asked.date() == T1.date()
    assert abs((asked - T1).total_seconds()) < 60


# ─────────────────────────────────────────────────────────────────────────
# 3. 캐시 — 같은 시각은 한 번만
# ─────────────────────────────────────────────────────────────────────────
async def test_the_same_instant_is_fetched_once_however_many_symbols_ask():
    """봉 하나에 외화 종목이 여덟이면 조회도 여덟 번 나가던 자리입니다."""
    source = Recording()
    fx = Fx("KRW", source=source)

    results = await asyncio.gather(*[fx.to_base(1.0, "USD", T1) for _ in range(8)])

    assert results == [1380.0] * 8
    assert len(source.asked) == 1, "같은 시각을 여러 번 물었습니다"


async def test_different_instants_are_not_served_from_each_other_s_cache():
    source = Recording({T1: 1300.0, T2: 1400.0}, default=None)
    fx = Fx("KRW", source=source)

    await fx.to_base(1.0, "USD", T1)
    await fx.to_base(1.0, "USD", T2)
    await fx.to_base(1.0, "USD", T1)          # 캐시 적중

    assert len(source.asked) == 2


async def test_seconds_inside_one_minute_share_a_lookup_and_the_minute_is_what_is_asked():
    """토스 환율은 1분 갱신이라 같은 분의 두 요청은 같은 값입니다.

    캐시 키와 실제 요청이 **같은 알갱이**여야 합니다. 한쪽만 자르면 09:30:59 로
    받아 온 값이 09:30:00 의 캐시에 앉습니다.
    """
    source = Recording()
    fx = Fx("KRW", source=source)

    await fx.to_base(1.0, "USD", T1.replace(second=12))
    await fx.to_base(1.0, "USD", T1.replace(second=59, microsecond=999_000))

    assert len(source.asked) == 1
    assert source.asked[0][2] == T1.replace(second=0, microsecond=0)


# ─────────────────────────────────────────────────────────────────────────
# 4. 못 받으면 멈춘다
# ─────────────────────────────────────────────────────────────────────────
async def test_a_failed_lookup_never_falls_back_to_the_last_good_rate():
    """조용히 틀린 크기로 사는 것보다 그 종목을 안 사는 편이 낫습니다."""
    source = Recording({T1: 1300.0}, default=None)
    fx = Fx("KRW", source=source)
    assert await fx.to_base(100.0, "USD", T1) == pytest.approx(130_000.0)

    source.fail = "환율 서버 응답 없음"
    with pytest.raises(FxUnavailable) as exc:
        await fx.to_base(100.0, "USD", T2)
    assert "1300" not in str(exc.value), "직전 환율을 답으로 들고 있습니다"

    # 실패는 캐시에 남지 않습니다 — 다음 봉에 소스가 살아나면 그대로 됩니다.
    source.fail = None
    source.by_instant[T2] = 1400.0
    assert await fx.to_base(100.0, "USD", T2) == pytest.approx(140_000.0)


@pytest.mark.parametrize("bad", [0.0, -1380.0, float("nan"), float("inf")])
async def test_an_unusable_rate_is_refused_rather_than_multiplied(bad):
    """0 이면 외화 평가금액이 사라지고, 음수면 손익의 부호가 뒤집힙니다."""
    fx = Fx("KRW", source=Recording(default=bad))
    with pytest.raises(FxUnavailable):
        await fx.to_base(100.0, "USD", T1)


# ─────────────────────────────────────────────────────────────────────────
# 5. 토스 환율 소스
# ─────────────────────────────────────────────────────────────────────────
def rate_body(**overrides) -> dict:
    """공식 스펙의 `ExchangeRateResponse` — 모든 수가 문자열입니다."""
    body = {
        "baseCurrency": "USD", "quoteCurrency": "KRW",
        "rate": "1380.5", "midRate": "1375", "basisPoint": "40",
        "rateChangeType": "UP",
        "validFrom": "2026-03-25T09:30:00+09:00",
        "validUntil": "2026-03-25T09:31:00+09:00",
    }
    body.update(overrides)
    return body


class FakeToss:
    """토스 HTTP 표면. 요청을 기록하고 미리 짜 둔 응답을 돌려줍니다."""

    def __init__(self, body: dict | None, status: int = 200):
        self.body = body
        self.status = status
        self.calls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/exchange-rate", request.url
        assert request.headers["Authorization"] == "Bearer test-token"
        self.calls.append(dict(request.url.params))
        if self.status >= 400:
            return httpx.Response(self.status, json={"message": "안 됩니다"})
        # 토스는 모든 응답을 `{"result": ...}` 로 한 번 감쌉니다.
        return httpx.Response(200, json={"result": self.body})


def toss_source(fake: FakeToss) -> TossFxSource:
    T._TOKENS[CLIENT_ID[:10]] = ("test-token", time.time() + 3600)
    src = TossFxSource(client_id=CLIENT_ID, client_secret="test-secret")
    src._client._http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return src


async def ask(fake: FakeToss, source: str = "USD", target: str = "KRW",
              when: datetime = T1) -> FxRate:
    src = toss_source(fake)
    try:
        return await src.rate(source, target, when)
    finally:
        await src.close()


def test_the_endpoint_matches_the_official_spec():
    assert T._FIELDS["exchange_rate_path"] == "/api/v1/exchange-rate"


def test_the_source_is_registered_so_a_config_can_name_it():
    T._TOKENS[CLIENT_ID[:10]] = ("test-token", time.time() + 3600)
    made = create_fx_source("toss", client_id=CLIENT_ID, client_secret="s")
    assert isinstance(made, TossFxSource)


async def test_the_request_carries_the_instant_and_both_currencies():
    """`dateTime` 을 빼면 "지금" 이 오고, 그러면 이 계층의 이유가 사라집니다."""
    fake = FakeToss(rate_body())
    await ask(fake)

    params = fake.calls[0]
    assert params["baseCurrency"] == "USD"    # 환산의 출발 — 1 단위를 세는 쪽
    assert params["quoteCurrency"] == "KRW"   # 환산의 도착
    assert params["dateTime"] == T1.isoformat()


async def test_the_pair_is_not_flipped_on_the_way_out():
    """토스의 base/quote 는 이 저장소의 quote_currency 와 뜻이 반대입니다.

    한 번 더 뒤집으면 1,380 대신 1/1,380 을 곱하게 되고, 그 결과도 화면에는
    평범한 숫자로 뜹니다.
    """
    fake = FakeToss(rate_body(baseCurrency="KRW", quoteCurrency="USD",
                              midRate="0.000727"))
    fx = Fx("KRW", source=toss_source(fake))
    with pytest.raises(FxUnavailable, match="통화 쌍"):
        await fx.to_base(100.0, "USD", T1)


async def test_the_mid_rate_is_used_not_the_buy_rate():
    """`rate` 는 매수 환율입니다. 그걸로 평가하면 사자마자 스프레드만큼
    부풀고 팔 때 사라져, 전략이 하지 않은 매매의 손익이 곡선에 섞입니다."""
    got = await ask(FakeToss(rate_body(rate="1380.5", midRate="1375")))
    assert got.rate == pytest.approx(1375.0)


async def test_the_validity_window_comes_back_with_the_rate():
    """이 구간이 요청한 시각을 덮는지가, 소스가 "지금" 을 주기 시작했는지를
    알 수 있는 유일한 표식입니다."""
    got = await ask(FakeToss(rate_body()))
    assert got.valid_from == datetime(2026, 3, 25, 0, 30, tzinfo=UTC)
    assert got.valid_until == datetime(2026, 3, 25, 0, 31, tzinfo=UTC)
    assert got.covers(T1)


async def test_a_window_that_misses_the_asked_instant_is_logged_not_hidden(caplog):
    fake = FakeToss(rate_body(validFrom="2026-03-24T09:30:00+09:00",
                              validUntil="2026-03-24T09:31:00+09:00"))
    fx = Fx("KRW", source=toss_source(fake))
    with caplog.at_level("WARNING", logger="quant.fx"):
        assert await fx.to_base(100.0, "USD", T1) == pytest.approx(137_500.0)
    assert "유효 구간" in caplog.text


@pytest.mark.parametrize("body", [
    rate_body(midRate=None, rate=None),
    rate_body(midRate="", rate=""),
    rate_body(midRate="없음", rate="없음"),
])
async def test_a_response_without_a_usable_rate_raises_instead_of_returning_zero(body):
    """환율 0 은 외화 평가금액을 통째로 지우면서도 숫자로 보입니다."""
    fx = Fx("KRW", source=toss_source(FakeToss(body)))
    with pytest.raises(FxUnavailable):
        await fx.to_base(100.0, "USD", T1)


async def test_an_http_failure_stops_the_conversion():
    fx = Fx("KRW", source=toss_source(FakeToss(None, status=429)))
    with pytest.raises(FxUnavailable):
        await fx.to_base(100.0, "USD", T1)


def test_the_exchange_rate_endpoint_is_reached_from_exactly_one_place():
    """환산이 두 곳에 생기면 언젠가 한 곳만 고칩니다.

    경로 문자열은 토스 표(`_FIELDS`)에 한 번만 적히고, 그 표를 들여다보는
    모듈도 하나뿐이어야 합니다. 설명 글에 경로가 나오는 것은 세지 않습니다 —
    그래서 문자열 상수만 봅니다.
    """
    import ast

    root = Path(__file__).resolve().parent.parent / "quant"
    literal, reads = [], []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "exchange-rate" not in text and "exchange_rate_path" not in text:
            continue
        if any(isinstance(n, ast.Constant) and n.value == "/api/v1/exchange-rate"
               for n in ast.walk(ast.parse(text))):
            literal.append(path.name)
        if '_FIELDS["exchange_rate_path"]' in text:
            reads.append(path.name)

    assert sorted(literal) == ["toss_broker.py"], literal
    assert sorted(reads) == ["toss_fx.py"], reads


# ─────────────────────────────────────────────────────────────────────────
# 6. 섞인 설정은 시작을 막는다
# ─────────────────────────────────────────────────────────────────────────
def config(base: str, symbols: list[tuple[str, str]], **universe) -> StrategyConfig:
    return StrategyConfig(
        name="t", alpha=[ModelSpec(type="ema_cross")],
        universe=UniverseConfig(
            symbols=[SymbolSpec(ticker=t, venue="toss", quote_currency=c)
                     for t, c in symbols],
            **universe),
        portfolio=PortfolioConfig(starting_cash=10_000_000, base_currency=base),
    )


def test_a_universe_with_two_currencies_is_refused():
    """이걸 허용하면 7만(원)과 250(달러)이 같은 자릿수로 더해집니다."""
    with pytest.raises(ValidationError) as exc:
        config("KRW", [("005930", "KRW"), ("AAPL", "USD")])
    message = str(exc.value)
    assert "AAPL" in message and "005930" in message, "어느 종목들인지 안 알려줍니다"
    assert "KRW" in message and "USD" in message


def test_the_refusal_says_why_and_what_to_do():
    with pytest.raises(ValidationError) as exc:
        config("KRW", [("005930", "KRW"), ("AAPL", "USD")])
    message = str(exc.value)
    assert "70,250" in message, "왜 위험한지가 없습니다"
    assert "나누" in message, "무엇을 하라는 말이 없습니다"


def test_a_single_currency_book_still_starts_whatever_its_label_says():
    """이 작업은 **섞인 것**만 막습니다. 라벨이 어긋난 것은 막지 않습니다.

    더하는 값이 전부 한 통화면 환산이 끼어들 자리가 없어 산수가 지금과
    똑같습니다. 여기서 같이 막으면 멀쩡히 돌던 설정들이 시작조차 못 하게 되고,
    그건 "기존 동작을 바꾸지 않는다" 를 어기는 쪽입니다.
    """
    cfg = config("USD", [("005930", "KRW"), ("000660", "KRW")])
    assert [s.quote_currency for s in cfg.universe.symbols] == ["KRW", "KRW"]


def test_a_mismatched_label_is_said_out_loud_because_it_bites_later(caplog):
    """장부가 환산을 시작하는 순간 이 라벨은 곧바로 금액에 들어옵니다."""
    with caplog.at_level("WARNING", logger="quant.config"):
        config("USD", [("005930", "KRW")])
    assert "base_currency" in caplog.text
    assert "KRW" in caplog.text and "USD" in caplog.text


def test_case_and_spacing_are_not_treated_as_a_different_currency(caplog):
    """`krw` 와 `KRW` 를 다른 통화로 읽으면 멀쩡한 설정이 막히거나 경고가 뜹니다."""
    with caplog.at_level("WARNING", logger="quant.config"):
        assert config("krw", [("005930", " KRW "), ("000660", "krw")]).universe.symbols
    assert caplog.text == ""


def test_a_dynamic_universe_must_name_the_currency_it_narrows_to():
    """설정이 종목을 적지 않으면 이 검증은 아무것도 볼 수 없습니다.

    거래소 하나에 USDT·BTC·KRW 마켓이 함께 있으므로, 좁히지 않은
    `source: exchange` 는 통화가 섞인 책을 **실행 중에** 만들어 냅니다.
    """
    with pytest.raises(ValidationError, match="quote_currency"):
        config("USDT", [], source=ModelSpec(type="exchange"))

    ok = config("USDT", [], source=ModelSpec(type="exchange",
                                             params={"quote_currency": "USDT"}))
    assert ok.universe.source.params["quote_currency"] == "USDT"


@pytest.mark.parametrize("path", [
    "configs/demo.yaml", "configs/demo_flow.yaml", "configs/kr_equity.yaml",
    "configs/kr_toss.yaml", "configs/kr_desk_gemini.yaml", "configs/live_crypto.yaml",
    "configs/us_equity.yaml", "configs/us_toss.yaml",
    "configs/kr_toss_desk.yaml", "configs/us_toss_desk.yaml",
])
def test_every_shipped_config_still_loads(path):
    """검증이 너무 세면 멀쩡한 설정이 막힙니다 — 그쪽이 더 흔한 사고입니다."""
    from quant.config.loader import load_config

    load_config(path)


def test_a_single_currency_backtest_still_produces_the_same_numbers():
    """단일 통화 운용의 산수는 이 작업 전과 **비트까지** 같아야 합니다.

    아래 값들은 이 변경 **이전** 코드에서 실제로 관측한 것입니다. 환산 계수
    1.0 이 어딘가에서 곱셈으로 끼어들거나, 환율 조회가 단일 통화 경로에
    들어오면 여기서 어긋납니다.
    """
    from quant.backtest.runner import run_backtest
    from quant.config.loader import load_config

    cfg = load_config("configs/kr_equity.yaml")
    cfg.data.provider, cfg.data.params = "synthetic", {"seed": 7}
    cfg.portfolio = PortfolioConfig(starting_cash=10_000_000, base_currency="KRW",
                                    model=cfg.portfolio.model,
                                    max_position_weight=0.35, cash_reserve_pct=0.05)
    report = asyncio.run(run_backtest(cfg)).report

    assert report.trades == 98
    assert report.ending_equity == pytest.approx(11_805_828.929566598, rel=1e-12)
    assert report.total_fees == pytest.approx(239_264.14790739294, rel=1e-12)
