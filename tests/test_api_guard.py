"""공개 바인딩 안전장치.

경고 한 줄로 두면 하필 실제 배포 구성에서만 무시됩니다 — 호스팅 플랫폼은
예외 없이 0.0.0.0 바인딩을 요구하기 때문입니다. 그 순간 매수·매도·전량청산·
자격증명 저장 엔드포인트가 인증 없이 인터넷에 열립니다.

조건은 "공유 토큰이 있는가" 가 아니라 "**사람이 가입할 수 있는가**" 입니다.
공유 토큰은 없어졌습니다 — 값 하나가 자리를 열면 그것은 로그인이 아니라
로그인의 우회이고, 그 값은 주소창과 프록시 로그를 타고 흐릅니다. 암호화 키가
없으면 계정을 만들 수 없고, 계정이 없으면 이 API 에는 앉을 자리가 없습니다.
"""

import pytest

from quant.api.server import UnsafeBind, assert_safe_to_bind


@pytest.fixture(autouse=True)
def _no_secret(monkeypatch):
    monkeypatch.delenv("QUANT_SECRET_KEY", raising=False)
    monkeypatch.delenv("QUANT_API_TOKEN", raising=False)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", ""])
def test_loopback_needs_no_token(host):
    """내 컴퓨터에서만 보이는 주소는 토큰 없이도 열 수 있어야 합니다."""
    assert_safe_to_bind(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.0.12", "10.0.0.4",
                                  "203.0.113.7", "0.0.0.0 ", "LOCALHOST.example.com"])
def test_public_bind_without_accounts_refuses(host):
    with pytest.raises(UnsafeBind) as exc:
        assert_safe_to_bind(host)
    # 무엇을 해야 하는지가 메시지에 있어야 합니다.
    assert "QUANT_SECRET_KEY" in str(exc.value)


@pytest.mark.parametrize("host", ["0.0.0.0", "203.0.113.7"])
def test_an_encryption_key_permits_public_bind(host, monkeypatch):
    monkeypatch.setenv("QUANT_SECRET_KEY", "s" * 48)
    assert_safe_to_bind(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "203.0.113.7"])
def test_a_shared_token_no_longer_permits_anything(host, monkeypatch):
    """예전의 우회로가 살아 있으면 안 됩니다."""
    monkeypatch.setenv("QUANT_API_TOKEN", "t" * 40)
    with pytest.raises(UnsafeBind):
        assert_safe_to_bind(host)


def test_whitespace_only_key_does_not_count(monkeypatch):
    """공백만 든 값은 키가 아닙니다 — 빈 환경변수로 우회되면 안 됩니다."""
    monkeypatch.setenv("QUANT_SECRET_KEY", "   ")
    with pytest.raises(UnsafeBind):
        assert_safe_to_bind("0.0.0.0")


def test_a_short_key_does_not_count(monkeypatch):
    """이 값 하나가 모든 가입자의 증권사 키를 지킵니다."""
    monkeypatch.setenv("QUANT_SECRET_KEY", "short")
    with pytest.raises(UnsafeBind):
        assert_safe_to_bind("0.0.0.0")


def test_hostname_containing_localhost_is_not_loopback():
    """'localhost' 가 부분 문자열이라고 루프백은 아닙니다."""
    with pytest.raises(UnsafeBind):
        assert_safe_to_bind("localhost.attacker.example")


# ── 교차출처 ────────────────────────────────────────────────────────────
def _app(monkeypatch, cors=None):
    from quant.api.server import create_app
    if cors is None:
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("CORS_ORIGINS", cors)
    return create_app(None, state_path=":memory:")


def _preflight(app, origin):
    from fastapi.testclient import TestClient
    return TestClient(app).options("/api/setup", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    })


def test_no_origin_is_allowed_by_default(monkeypatch, tmp_path):
    """기본값 '*' 는 사용자가 방문한 아무 페이지나 로컬 대시보드에 주문을
    넣을 수 있게 합니다. 대시보드는 같은 출처에서 서빙되므로 필요 없습니다."""
    monkeypatch.chdir(tmp_path)
    app = _app(monkeypatch)
    r = _preflight(app, "https://evil.example")
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_an_explicitly_named_origin_is_allowed(monkeypatch, tmp_path):
    """UI 를 다른 곳에서 서빙하는 사람은 직접 적으면 됩니다."""
    monkeypatch.chdir(tmp_path)
    app = _app(monkeypatch, cors="https://desk.example")
    r = _preflight(app, "https://desk.example")
    assert r.headers.get("access-control-allow-origin") == "https://desk.example"


def test_naming_one_origin_does_not_admit_another(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    app = _app(monkeypatch, cors="https://desk.example")
    r = _preflight(app, "https://evil.example")
    assert r.headers.get("access-control-allow-origin") != "https://evil.example"
