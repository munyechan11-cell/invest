"""화면 어디에도 종목코드만 뜨지 않게.

`005930` 이라고만 적힌 줄은 그 코드를 외운 사람만 읽을 수 있고, 잘못 고르면
**다른 회사를 삽니다**. 그래서 서버가 티커를 내보내는 자리마다 이름을 같이
싣는지 여기서 고정합니다.

검사하는 것은 "이름 필드가 있다" 가 아니라 성질 셋입니다.

  1. 이름을 **지어내지 않는가.** 모르는 종목은 티커 그대로여야 합니다. 자신
     있게 뜬 틀린 이름은 빈칸보다 훨씬 나쁩니다.
  2. 찾는 **순서**. 증권사가 직접 준 이름(조회 기록)이 손으로 적은 표를 이깁니다.
     사명이 바뀌면 표가 아니라 증권사 쪽이 먼저 맞아지기 때문입니다.
  3. 모르는 것만, **한 번에** 물어보는가. 종목마다 한 번씩 부르면 유니버스가
     넓을수록 느려지고, 레이트 리밋에 걸리는 순간 이름이 전부 사라집니다 —
     하필 종목이 많아서 이름이 가장 필요한 화면에서.

실제 토스·한투 엔드포인트는 어디서도 부르지 않습니다. HTTP 를 내는 자리는
전부 가짜로 바꿔 두고, 무엇을 어떻게 물었는지만 봅니다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from quant.api.server import create_app
from quant.data.names import STATIC_NAMES, NameBook
from quant.live.state import StateStore

#: 이 저장소가 기본으로 싣는 설정에 들어 있는 종목들. 사용자가 아무것도 고르지
#: 않아도 화면에 뜨는 것들이라, 여기에 이름이 없으면 첫 화면부터 숫자만 뜹니다.
SHIPPED = [
    "005930", "000660", "035420", "005380",
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "LLY", "SPY",
]


# ── 정적 표 ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("ticker", SHIPPED)
def test_every_shipped_symbol_has_a_name(ticker):
    name = NameBook().name(ticker)
    assert name and name != ticker, f"{ticker} 가 코드 그대로 뜹니다"


def test_no_korean_code_in_the_shipped_configs_is_left_unnamed():
    """설정에 종목을 추가할 때 표를 같이 늘리게 만드는 검사입니다.

    6자리 숫자는 사람이 읽을 수 있는 구석이 하나도 없습니다 — 미국 티커는
    최소한 회사 이름의 약자라도 되지만, `207940` 은 아무것도 아닙니다.
    """
    missing = set()
    for path in sorted(Path("configs").glob("*.y*ml")):
        body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        universe = (body.get("universe") or {})
        for spec in (universe.get("symbols") or []):
            ticker = str((spec or {}).get("ticker") or "")
            if re.fullmatch(r"\d{6}", ticker) and ticker not in STATIC_NAMES:
                missing.add(f"{ticker} ({path.name})")
    assert not missing, f"이름 없는 국내 종목코드: {sorted(missing)}"


def test_a_name_is_never_invented():
    """모르는 종목은 티커 그대로. 그럴듯한 이름을 만들어 내면 안 됩니다."""
    book = NameBook()
    assert book.name("999999") == "999999"
    assert book.known("999999") is None
    assert book.label("ZZZZ") == {"ticker": "ZZZZ", "name": "ZZZZ"}


def test_a_symbol_key_is_understood_as_a_ticker():
    """포지션과 상태 DB 는 `TICKER:VENUE` 를 씁니다 — 그대로 찾으면 늘 실패합니다."""
    assert NameBook().name("005930:toss") == "삼성전자"


# ── 찾는 순서 ───────────────────────────────────────────────────────────
def _state(tmp_path) -> str:
    return str(tmp_path / "state.db")


def test_a_looked_up_symbol_is_actually_written_down(tmp_path):
    """조회 기록은 **한 줄도 쌓인 적이 없었습니다.**

    `remember_ticker` 가 없는 속성(`self._lock`)을 잡고 있어서 부를 때마다
    AttributeError 로 죽었고, 그 예외를 검색 경로가 그대로 흘려보냈습니다.
    그래서 "한 번 찾은 종목은 다음부터 이름으로도 찾힌다" 는 설명은 코드에만
    있고 동작한 적은 없었습니다 — 화면은 매번 다시 코드로 물어봐야 했습니다.
    """
    store = StateStore(_state(tmp_path))
    store.remember_ticker({"ticker": "068270", "venue": "toss",
                           "name": "셀트리온", "currency": "KRW"})
    store.close()

    reopened = StateStore(_state(tmp_path))
    try:
        assert [r["name"] for r in reopened.known_tickers()] == ["셀트리온"]
    finally:
        reopened.close()


def test_an_empty_name_does_not_erase_a_known_one(tmp_path):
    """이름 없이 다시 저장하면 알던 이름을 잃습니다."""
    store = StateStore(_state(tmp_path))
    store.remember_ticker({"ticker": "068270", "venue": "toss",
                           "name": "셀트리온", "currency": "KRW"})
    store.remember_ticker({"ticker": "068270", "venue": "toss",
                           "name": "", "currency": "KRW"})
    try:
        assert [r["name"] for r in store.known_tickers()] == ["셀트리온"]
    finally:
        store.close()


def test_what_the_broker_told_us_beats_the_hand_written_table(tmp_path):
    """사명이 바뀌면 손으로 적은 표가 아니라 증권사 쪽이 먼저 맞아집니다."""
    store = StateStore(_state(tmp_path))
    store.remember_ticker({"ticker": "035420", "venue": "toss",
                           "name": "네이버", "currency": "KRW"})
    store.close()
    assert NameBook().name("035420") == STATIC_NAMES["035420"]
    assert NameBook(_state(tmp_path)).name("035420") == "네이버"


# ── 프로바이더 조회 ─────────────────────────────────────────────────────
class FakeFeed:
    """다건 조회를 가진 프로바이더. 몇 번, 무엇을 물었는지 남깁니다."""

    def __init__(self, table: dict[str, str]):
        self.table = table
        self.calls: list[list[str]] = []

    async def describe_many(self, tickers):
        self.calls.append(list(tickers))
        return {t: {"ticker": t, "name": self.table[t], "venue": "toss",
                    "currency": "KRW"}
                for t in tickers if t in self.table}


class BrokenFeed:
    def __init__(self):
        self.calls = 0

    async def describe_many(self, tickers):
        self.calls += 1
        raise RuntimeError("증권사 응답 없음")


@pytest.mark.asyncio
async def test_only_the_unknown_symbols_are_asked_and_only_once(tmp_path):
    """아는 것을 다시 묻는 호출은 그 자체로 레이트 리밋을 깎아먹습니다."""
    feed = FakeFeed({"068270": "셀트리온", "323410": "카카오뱅크"})
    book = NameBook(_state(tmp_path))
    got = await book.resolve_many(["005930", "AAPL", "068270", "323410"], feed)

    assert len(feed.calls) == 1, f"한 번에 묻지 않았습니다: {feed.calls}"
    assert feed.calls[0] == ["068270", "323410"], "아는 종목까지 물었습니다"
    assert got["005930"] == "삼성전자"
    assert got["068270"] == "셀트리온"


@pytest.mark.asyncio
async def test_nothing_missing_means_no_call_at_all(tmp_path):
    """화면은 이걸 몇 초마다 다시 그립니다."""
    feed = FakeFeed({})
    await NameBook(_state(tmp_path)).resolve_many(["005930", "AAPL"], feed)
    assert feed.calls == []


@pytest.mark.asyncio
async def test_a_resolved_name_is_remembered_for_next_time(tmp_path):
    feed = FakeFeed({"068270": "셀트리온"})
    await NameBook(_state(tmp_path)).resolve_many(["068270"], feed)
    # 새 요청 — 프로바이더 없이도 알아야 합니다.
    assert NameBook(_state(tmp_path)).name("068270") == "셀트리온"


@pytest.mark.asyncio
async def test_a_symbol_nobody_knows_is_not_asked_over_and_over(tmp_path):
    """시세 화면은 몇 초마다 다시 그립니다.

    끝내 이름이 없는 종목 하나 때문에 그때마다 증권사 호출이 새로 나가면,
    그 호출이 레이트 리밋을 깎아서 정작 필요한 시세 조회가 거절됩니다.
    """
    feed = FakeFeed({})
    for _ in range(3):                      # 화면이 세 번 다시 그렸다고 치고
        await NameBook(_state(tmp_path)).resolve_many(["999999"], feed)
    assert len(feed.calls) == 1


@pytest.mark.asyncio
async def test_one_persons_miss_does_not_silence_anothers_broker(tmp_path):
    """사람마다 연동한 증권사가 다릅니다.

    A 의 증권사가 모르는 종목을 "아무도 못 찾는 것" 으로 치면, B 는 자기
    증권사에 물어볼 기회를 잃습니다.
    """
    blind = FakeFeed({})
    await NameBook(str(tmp_path / "a.db")).resolve_many(["999999"], blind)

    knows = FakeFeed({"999999": "어떤회사"})
    got = await NameBook(str(tmp_path / "b.db")).resolve_many(["999999"], knows)
    assert knows.calls == [["999999"]]
    assert got["999999"] == "어떤회사"


@pytest.mark.asyncio
async def test_a_broken_feed_still_gives_back_the_names_we_have(tmp_path):
    """이름을 못 받았다고 목록 자체가 죽으면 고친 것보다 부순 것이 큽니다."""
    feed = BrokenFeed()
    got = await NameBook(_state(tmp_path)).resolve_many(["005930", "068270"], feed)
    assert feed.calls == 1
    assert got["005930"] == "삼성전자"
    assert got["068270"] == "068270"


# ── 토스 다건 조회 ──────────────────────────────────────────────────────
#: 토스 공식 OpenAPI 문서의 `GET /api/v1/stocks` 응답 예시 그대로입니다.
TOSS_ROWS = {
    "005930": {"symbol": "005930", "name": "삼성전자", "englishName": "SamsungElec",
               "market": "KOSPI", "securityType": "STOCK", "status": "ACTIVE",
               "currency": "KRW"},
    "AAPL": {"symbol": "AAPL", "name": "애플", "englishName": "APPLE INC",
             "market": "NASDAQ", "securityType": "STOCK", "status": "ACTIVE",
             "currency": "USD"},
}


def _toss(monkeypatch):
    """네트워크를 내지 않는 토스 프로바이더 + 무엇을 물었는지 기록."""
    from quant.brokerage import toss_broker as T

    provider = T.TossProvider(client_id="test-id", client_secret="test-secret")
    asked: list[tuple[str, dict]] = []

    async def fake_request(method, path, *, params=None, json=None, account=False):
        asked.append((path, dict(params or {})))
        symbols = [s for s in (params or {}).get("symbols", "").split(",") if s]
        if path == T._FIELDS["stocks_path"]:
            return [TOSS_ROWS[s] for s in symbols if s in TOSS_ROWS]
        if path == T._FIELDS["price_path"]:
            return [{"symbol": s, "lastPrice": "72000", "currency": "KRW"}
                    for s in symbols if s in TOSS_ROWS]
        raise AssertionError(f"예상 밖 호출: {path}")

    monkeypatch.setattr(provider.client, "request", fake_request)
    return provider, asked


@pytest.mark.asyncio
async def test_toss_reads_the_documented_name_fields(monkeypatch):
    provider, _asked = _toss(monkeypatch)
    info = await provider.describe("005930")
    assert info["name"] == "삼성전자"
    assert info["ticker"] == "005930"
    assert info["currency"] == "KRW"
    assert info["market"] == "KOSPI"


@pytest.mark.asyncio
async def test_toss_asks_for_two_hundred_symbols_per_call(monkeypatch):
    """250 종목을 250 번 부르면 레이트 리밋에 걸려 전부 실패합니다."""
    provider, asked = _toss(monkeypatch)
    await provider.describe_many([f"{i:06d}" for i in range(250)])

    stocks = [params for path, params in asked
              if path.endswith("/stocks")]
    assert len(stocks) == 2, f"묶어서 묻지 않았습니다 ({len(stocks)}회)"
    assert len(stocks[0]["symbols"].split(",")) == 200
    assert len(stocks[1]["symbols"].split(",")) == 50


@pytest.mark.asyncio
async def test_toss_drops_symbols_the_api_would_reject(monkeypatch):
    """`/` 가 든 심볼 하나가 섞이면 요청 전체가 400 입니다.

    그러면 같이 물어본 멀쩡한 종목들까지 이름을 잃습니다.
    """
    provider, asked = _toss(monkeypatch)
    found = await provider.describe_many(["005930", "BTC/USDT", "AAPL"])

    sent = [params["symbols"] for path, params in asked if path.endswith("/stocks")]
    assert sent == ["005930,AAPL"]
    assert set(found) == {"005930", "AAPL"}


@pytest.mark.asyncio
async def test_toss_keeps_the_name_when_the_price_call_fails(monkeypatch):
    """장이 닫혀 있거나 시세가 안 와도 이름은 나와야 합니다."""
    from quant.brokerage import toss_broker as T

    provider, _asked = _toss(monkeypatch)
    inner = provider.client.request

    async def flaky(method, path, *, params=None, json=None, account=False):
        if path == T._FIELDS["price_path"]:
            raise RuntimeError("현재가 없음")
        return await inner(method, path, params=params, json=json, account=account)

    monkeypatch.setattr(provider.client, "request", flaky)
    info = await provider.describe("005930")
    assert info["name"] == "삼성전자"
    assert "price" not in info          # 없는 값을 0 으로 채우지 않습니다


# ── API 응답 ────────────────────────────────────────────────────────────
STRATEGY = {
    "name": "이름확인전략",
    "mode": "dry_run",
    "data": {"provider": "synthetic", "timeframe": "1d", "calendar": "always_open",
             "warmup_bars": 60},
    "universe": {"symbols": [
        {"ticker": "005930", "venue": "toss", "quote_currency": "KRW",
         "tick_size": 100, "lot_size": 1},
        {"ticker": "AAPL", "venue": "toss", "quote_currency": "USD",
         "tick_size": 0.01, "lot_size": 1},
    ]},
    "alpha": [{"type": "ema_cross"}],
    "broker": {"type": "paper"},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "templates"
    root.mkdir()
    (root / "named.yaml").write_text(
        yaml.safe_dump(STRATEGY, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("QUANT_SECRET_KEY", "n" * 48)
    monkeypatch.setenv("QUANT_USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setenv("QUANT_USER_DATA", str(tmp_path / "userdata"))
    monkeypatch.setenv("QUANT_ENV_FILE", str(tmp_path / "env.test"))
    monkeypatch.setenv("QUANT_CONFIG_DIR", str(root))
    c = TestClient(create_app(None, state_path=str(tmp_path / "state.db")))
    assert c.post("/api/auth/register", json={
        "email": "a@example.com", "password": "correct-horse-9",
        "display_name": "t"}).status_code == 201
    return c


def test_the_strategy_list_says_what_it_buys(client):
    """`005930, 000660` 만 늘어놓으면 무엇에 돈을 넣는 전략인지 알 수 없습니다."""
    listed = client.get("/api/strategies").json()["strategies"]
    named = next(s for s in listed if s["id"] == "named")
    assert named["tickers"] == [{"ticker": "005930", "name": "삼성전자"},
                                {"ticker": "AAPL", "name": "애플"}]


def test_the_ticker_tape_carries_names(client):
    rows = client.get("/api/universe", params={"strategy": "named"}).json()["symbols"]
    assert {r["ticker"]: r["name"] for r in rows} == {"005930": "삼성전자",
                                                     "AAPL": "애플"}
    assert all(r["change_pct"] is None for r in rows)


def test_the_chart_says_which_company_it_is_showing(client):
    body = client.get("/api/candles", params={"ticker": "005930",
                                              "strategy": "named"}).json()
    assert body["ticker"] == "005930"          # 기존 필드는 그대로
    assert body["ticker_name"] == "삼성전자"


def test_a_strategy_symbol_is_searchable_by_name_from_the_first_screen(client):
    """조회 기록이 비어 있어도 전략에 든 종목은 이름으로 찾혀야 합니다.

    한 번도 검색해 본 적 없는 사람에게 "먼저 6자리 코드를 넣으세요" 라고
    말하면, 코드를 모르는 사람은 시작조차 못 합니다.
    """
    hits = client.get("/api/lookup", params={"q": "삼성",
                                             "strategy": "named"}).json()["results"]
    assert [(r["ticker"], r["name"]) for r in hits] == [("005930", "삼성전자")]


def test_stopped_universe_closes_its_temporary_name_provider(
    client, monkeypatch,
):
    import quant.webapp.registry as registry_module

    closed = 0

    class Provider:
        async def describe_many(self, _tickers):
            return {}

        async def close(self):
            nonlocal closed
            closed += 1

    monkeypatch.setattr(
        registry_module.UserRegistry,
        "data_provider",
        lambda _self, _user_id, _config: Provider(),
    )

    response = client.get("/api/universe", params={"strategy": "named"})

    assert response.status_code == 200
    assert closed == 1


def test_six_digit_lookup_closes_provider_after_describe_failure(
    client, monkeypatch,
):
    import quant.webapp.registry as registry_module

    closed = 0

    class Provider:
        async def describe(self, _ticker):
            raise RuntimeError("lookup failed")

        async def close(self):
            nonlocal closed
            closed += 1

    monkeypatch.setattr(
        registry_module.UserRegistry,
        "data_provider",
        lambda _self, _user_id, _config: Provider(),
    )

    response = client.get("/api/lookup", params={
        "q": "123456", "strategy": "named",
    })

    assert response.status_code == 200
    assert response.json()["results"] == []
    assert closed == 1


def test_the_trade_log_says_what_was_traded(client, tmp_path):
    """반년 뒤에 다시 읽는 기록입니다. 코드만 남으면 알아볼 수 없습니다."""
    store = StateStore(str(tmp_path / "userdata" / "u1" / "state.db"))
    store.start_run("이름확인전략", "dry_run", starting_cash=1_000_000.0)
    store.record_closed_trade({
        "symbol": "005930", "side": "buy", "quantity": 1,
        "entry_price": 70000.0, "exit_price": 72000.0,
        "entry_ts": "2026-01-02T00:00:00+00:00",
        "exit_ts": "2026-01-05T00:00:00+00:00",
        "pnl": 2000.0, "pnl_pct": 2.86, "fees": 0.0, "exit_tag": "take_profit",
    })
    store.close()

    trades = client.get("/api/tradelog").json()["trades"]
    assert [t["symbol"] for t in trades] == ["005930"]      # 기존 필드는 그대로
    assert [t["symbol_name"] for t in trades] == ["삼성전자"]
