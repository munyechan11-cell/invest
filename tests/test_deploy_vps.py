"""고정 IP VPS 배포 설정.

토스는 허용 IP 목록을 씁니다. 나가는 IP 가 변하는 곳에서는 이 서비스를
운영할 수 없습니다 — 키가 맞아도 403 이고, 배포할 때마다 다시 등록해야
합니다.

여기서 검사하는 것은 문법이 아니라 **이 설정으로 실제 돈을 굴릴 수 있는가**
입니다. HTTPS 가 빠지면 로그인이 안 되고, 종료 유예가 없으면 봉을 처리하다
만 채로 죽고, 상태 경로가 배포 디렉터리 안에 있으면 재배포마다 포지션이
사라집니다.
"""
from __future__ import annotations

from pathlib import Path

import pytest

UNIT = Path("deploy/quant.service").read_text(encoding="utf-8")
CADDY = Path("deploy/Caddyfile").read_text(encoding="utf-8")
GUIDE = Path("deploy/README.md").read_text(encoding="utf-8")


def test_the_service_gets_time_to_finish_its_cycle():
    """SIGTERM 을 받고 바로 죽으면 봉 하나를 처리하다 만 상태가 됩니다."""
    assert "KillSignal=SIGTERM" in UNIT
    stop = next(l for l in UNIT.splitlines() if l.startswith("TimeoutStopSec="))
    assert int(stop.split("=")[1]) >= 60


def test_a_crash_loop_stops_instead_of_hiding():
    """계속 죽는 것을 무한히 되살리면 자동 재시작이 문제를 가립니다."""
    assert "StartLimitBurst=" in UNIT and "StartLimitIntervalSec=" in UNIT


def test_state_lives_outside_the_deploy_directory():
    """재배포가 포지션을 지우면 안 됩니다."""
    assert "ReadWritePaths=/home/quant/data" in UNIT
    assert "DB_PATH=/home/quant/data" in GUIDE
    assert "QUANT_USER_DATA=/home/quant/data" in GUIDE


def test_the_process_cannot_reach_more_than_it_needs():
    """남의 증권사 키를 들고 도는 프로세스라 사고의 반경도 좁아야 합니다."""
    for hardening in ("NoNewPrivileges=true", "ProtectSystem=strict",
                      "ProtectHome=read-only", "PrivateTmp=true"):
        assert hardening in UNIT


def test_https_is_not_optional():
    """`__Host-` 쿠키는 Secure 를 요구하고, Secure 는 HTTPS 를 요구합니다.

    HTTP 로 열면 브라우저가 세션 쿠키를 조용히 버려서 로그인이 되지 않습니다.
    """
    assert "__Host-" in CADDY or "__Host-" in GUIDE
    assert "Strict-Transport-Security" in CADDY


def test_the_proxy_tells_the_app_it_arrived_over_https():
    """앱은 이 헤더로 쿠키 이름을 정합니다 — 없으면 `__Host-` 를 떼고 냅니다."""
    assert "X-Forwarded-Proto" in CADDY


def test_websockets_are_proxied():
    """실시간 심의와 체결이 이 경로로 옵니다."""
    assert "reverse_proxy" in CADDY


@pytest.mark.parametrize("must", [
    "허용 IP",            # 무엇을 등록해야 하는지
    "api.ipify.org",      # 그 값을 어떻게 알아내는지
    "이 서버의 IP",        # 내 컴퓨터 IP 와 헷갈리지 않게
    "QUANT_SECRET_KEY",   # 잃어버리면 자격증명을 못 살립니다
    "출금 권한 없이",       # 키를 어떻게 발급할지
])
def test_the_guide_says_the_things_that_actually_go_wrong(must):
    assert must in GUIDE


def test_the_guide_explains_why_not_a_paas():
    """왜 이 선택을 했는지 적혀 있지 않으면 다음 사람이 되돌립니다."""
    assert "Render" in GUIDE and "고정" in GUIDE


def test_the_secret_key_warning_is_present():
    """이 값을 잃으면 저장된 증권사 키를 되살릴 방법이 없습니다."""
    assert "되살릴 수 없" in GUIDE
