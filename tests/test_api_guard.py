"""공개 바인딩 안전장치.

경고 한 줄로 두면 하필 실제 배포 구성에서만 무시됩니다 — 호스팅 플랫폼은
예외 없이 0.0.0.0 바인딩을 요구하기 때문입니다. 그 순간 매수·매도·전량청산·
자격증명 저장 엔드포인트가 인증 없이 인터넷에 열립니다.
"""
import os

import pytest

from quant.api.server import UnsafeBind, assert_safe_to_bind


@pytest.fixture(autouse=True)
def _no_token(monkeypatch):
    monkeypatch.delenv("QUANT_API_TOKEN", raising=False)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", ""])
def test_loopback_needs_no_token(host):
    """내 컴퓨터에서만 보이는 주소는 토큰 없이도 열 수 있어야 합니다."""
    assert_safe_to_bind(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.0.12", "10.0.0.4",
                                  "203.0.113.7", "0.0.0.0 ", "LOCALHOST.example.com"])
def test_public_bind_without_a_token_refuses(host):
    with pytest.raises(UnsafeBind) as exc:
        assert_safe_to_bind(host)
    # 무엇을 해야 하는지가 메시지에 있어야 합니다.
    assert "QUANT_API_TOKEN" in str(exc.value)


@pytest.mark.parametrize("host", ["0.0.0.0", "203.0.113.7"])
def test_a_token_permits_public_bind(host, monkeypatch):
    monkeypatch.setenv("QUANT_API_TOKEN", "s" * 32)
    assert_safe_to_bind(host)


def test_whitespace_only_token_does_not_count(monkeypatch):
    """공백만 든 값은 토큰이 아닙니다 — 빈 환경변수로 우회되면 안 됩니다."""
    monkeypatch.setenv("QUANT_API_TOKEN", "   ")
    with pytest.raises(UnsafeBind):
        assert_safe_to_bind("0.0.0.0")


def test_hostname_containing_localhost_is_not_loopback():
    """'localhost' 가 부분 문자열이라고 루프백은 아닙니다."""
    with pytest.raises(UnsafeBind):
        assert_safe_to_bind("localhost.attacker.example")
