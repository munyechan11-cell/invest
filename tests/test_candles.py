"""시세 조회 — 화면 오른쪽이 답해야 하는 세 가지.

지금 얼마인가, 나는 얼마에 들어갔는가, 봇이 방금 무엇을 했는가.
세 번 나눠 부르면 서로 다른 순간의 답이 한 화면에 섞이므로 한 응답으로 묶습니다.
"""

import pytest
from fastapi.testclient import TestClient

from quant.api.server import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_SECRET_KEY", "t" * 48)
    monkeypatch.setenv("QUANT_USERS_DB", str(tmp_path / "users.db"))
    c = TestClient(create_app(None, state_path=str(tmp_path / "state.db")))
    assert c.post("/api/auth/register", json={
        "email": "a@example.com", "password": "correct-horse-9",
        "display_name": "t"}).status_code == 201
    return c


def test_prices_are_visible_before_any_bot_is_running(client):
    """봇을 켜야만 호가를 볼 수 있으면 순서가 거꾸로입니다.

    데스크가 수동 매수를 추천했을 때, 사용자는 봇을 켜지 않은 채로 가격을 보고
    직접 삽니다. 그게 이 화면의 절반입니다.
    """
    r = client.get("/api/candles", params={"ticker": "AAA", "strategy": "demo",
                                           "count": 60})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["bars"]) == 60
    assert body["quote"]["price"] > 0
    assert body["tick_size"] > 0        # 지정가를 호가단위에 맞추려면 필요합니다
    assert body["currency"]


def test_bars_are_chronological_and_complete(client):
    bars = client.get("/api/candles", params={
        "ticker": "AAA", "strategy": "demo", "count": 40}).json()["bars"]
    assert [b["t"] for b in bars] == sorted(b["t"] for b in bars)
    for b in bars:
        assert b["l"] <= b["o"] <= b["h"]
        assert b["l"] <= b["c"] <= b["h"]


def test_a_ticker_outside_the_strategy_is_refused(client):
    """임의의 티커를 물어볼 수 있으면 시세 조회가 아니라 데이터 계약을 넓히는 일입니다."""
    r = client.get("/api/candles", params={"ticker": "NOPE", "strategy": "demo"})
    assert r.status_code == 404


def test_a_strategy_path_cannot_be_named(client):
    """전략은 이름으로만 고릅니다 — 경로를 받으면 파일 열람이 됩니다."""
    for attempt in ("../../etc/passwd", "/etc/passwd", "../.env"):
        r = client.get("/api/candles", params={"ticker": "AAA", "strategy": attempt})
        assert r.status_code == 400, attempt


def test_without_a_strategy_it_says_so_rather_than_guessing(client):
    r = client.get("/api/candles", params={"ticker": "AAA"})
    assert r.status_code == 400
    assert "전략" in r.json()["detail"]


def test_it_needs_a_session(client):
    anon = TestClient(client.app)
    assert anon.get("/api/candles",
                    params={"ticker": "AAA", "strategy": "demo"}).status_code == 401


def test_one_users_fills_are_not_anothers(client, tmp_path):
    """체결 점은 내 것만 찍혀야 합니다."""
    other = TestClient(client.app)
    other.post("/api/auth/register", json={
        "email": "b@example.com", "password": "correct-horse-9", "display_name": "b"})
    mine = client.get("/api/candles", params={"ticker": "AAA", "strategy": "demo"}).json()
    theirs = other.get("/api/candles", params={"ticker": "AAA", "strategy": "demo"}).json()
    assert mine["fills"] == [] and theirs["fills"] == []
    assert mine["position"] is None and theirs["position"] is None


@pytest.mark.parametrize("count", [19, 501])
def test_the_bar_count_is_bounded(client, count):
    """한도 없는 조회는 로그인한 아무나 누를 수 있는 크래시 경로입니다."""
    r = client.get("/api/candles", params={"ticker": "AAA", "strategy": "demo",
                                           "count": count})
    assert r.status_code == 422


def test_a_dead_feed_does_not_take_the_screen_down(client, monkeypatch):
    """시세가 없다고 내 포지션까지 안 보이면 안 됩니다."""
    import quant.webapp.registry as reg

    def boom(self, user_id, config):
        raise RuntimeError("feed down")

    monkeypatch.setattr(reg.UserRegistry, "data_provider", boom)
    r = client.get("/api/candles", params={"ticker": "AAA", "strategy": "demo"})
    assert r.status_code == 200
    body = r.json()
    assert body["stale"] is True
    assert body["bars"] == []
