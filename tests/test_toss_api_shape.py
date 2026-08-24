"""토스 어댑터가 실제 API 와 같은 모양으로 말하는가.

이 표는 한때 **추정** 이었고 전부 틀렸습니다. 경로는 `/v1/market/...` 이
아니라 `/api/v1/...` 이고, 토큰은 Basic 헤더가 아니라 본문으로 받습니다.
그래서 키가 맞아도 토큰 발급이 403 이었고, 그 위의 모든 것이 따라 죽었습니다.

여기서 검사하는 것은 공식 OpenAPI 문서에서 확인한 값들입니다:
https://openapi.tossinvest.com/openapi-docs/latest/openapi.json
"""
from __future__ import annotations

import inspect

import pytest

from quant.brokerage import toss_broker as T


def test_the_host_is_the_documented_one():
    assert T.HOST == "https://openapi.tossinvest.com"


@pytest.mark.parametrize("key,path", [
    ("token_path", "/oauth2/token"),
    ("price_path", "/api/v1/prices"),
    ("stocks_path", "/api/v1/stocks"),
    ("candles_path", "/api/v1/candles"),
    ("orderbook_path", "/api/v1/orderbook"),
    ("accounts_path", "/api/v1/accounts"),
    ("holdings_path", "/api/v1/holdings"),
    ("orders_path", "/api/v1/orders"),
])
def test_every_path_matches_the_spec(key, path):
    assert T._FIELDS[key] == path


def test_the_token_request_sends_credentials_in_the_body():
    """Basic 헤더로 보내고 본문에 grant_type 만 넣으면 403 입니다 — 키가 맞아도."""
    src = inspect.getsource(T.toss_token)
    assert '"client_id": client_id' in src
    assert '"client_secret": client_secret' in src
    # 헤더로 되돌아가지 않았는지. 주석 속 설명은 세지 않습니다.
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert "Authorization" not in code, "토큰 발급에 인증 헤더를 다시 붙였습니다"


def test_a_failed_call_explains_itself():
    """"403 Forbidden for url ..." 로는 무엇을 고쳐야 하는지 알 수 없습니다."""
    src = inspect.getsource(T._explain)
    # 403 의 압도적으로 흔한 원인은 허용 IP 입니다.
    assert "허용 IP" in src
    assert "401" in src and "429" in src


def test_only_the_two_supported_intervals_are_offered():
    """있지도 않은 주기를 지원한다고 적어 두면 그 설정이 시작할 때 죽습니다."""
    assert set(T.TossProvider._INTERVAL) == {"1m", "1d"}
    assert set(T.TossProvider._INTERVAL.values()) == {"1m", "1d"}


def test_candles_are_paged_by_cursor_not_by_date_range():
    """토스 캔들은 기간이 아니라 개수와 커서로 셉니다."""
    src = inspect.getsource(T.TossProvider.history)
    assert "count" in src and "before" in src
    assert "nextBefore" in src
    assert '"from"' not in src and '"to"' not in src


def test_candle_fields_use_the_documented_names():
    src = inspect.getsource(T.TossProvider.history)
    for field in ("openPrice", "highPrice", "lowPrice", "closePrice"):
        assert field in src, f"{field} 를 읽지 않습니다"


def test_the_result_envelope_is_unwrapped_once_and_centrally():
    """호출부마다 벗기면 한 곳을 빠뜨리고, 그곳은 빈 목록을 조용히 돌려줍니다."""
    src = inspect.getsource(T._TossClient.request)
    assert 'body.get("result", body)' in src


def test_a_closed_market_still_gives_a_price():
    """장이 닫히면 호가가 비어 옵니다 — 그때 None 이면 봇이 시작하지 못합니다."""
    src = inspect.getsource(T.TossProvider.quote)
    assert "lastPrice" in src
    assert src.index("orderbook_path") < src.index("price_path"), \
        "호가를 먼저 보고, 없을 때 현재가로 물러서야 합니다"
