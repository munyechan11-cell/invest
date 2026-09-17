"""설치 스크립트.

사용자가 서버에서 한 번 실행하는 것이라, 여기서 잘못되면 되돌리기가
어렵습니다. 특히 비밀키 — 이 값을 다시 만들면 저장된 증권사 키를 전부 못
읽게 되고, 그건 "다시 실행했더니 모두 로그아웃되고 연동이 풀렸다" 로
나타납니다.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

SH = Path("deploy/install.sh")
TEXT = SH.read_text(encoding="utf-8")


def test_it_is_valid_bash():
    subprocess.run(["bash", "-n", str(SH)], check=True)


def test_it_stops_on_the_first_error():
    """설치가 반쯤 된 상태로 "완료" 를 찍으면 그게 제일 나쁩니다."""
    assert re.search(r"^set -euo pipefail$", TEXT, re.M)


def test_the_secret_key_is_never_regenerated():
    """이 값을 갈아엎으면 저장된 증권사 키를 전부 못 읽습니다.

    여러 번 실행하는 것은 정상적인 일입니다 — 코드를 갱신할 때마다 부릅니다.
    그때마다 키가 새로 만들어지면 사용자는 매번 연동을 다시 해야 합니다.
    """
    block = TEXT[TEXT.index("ENV_FILE="):TEXT.index("6/7")]
    assert "grep -q '^QUANT_SECRET_KEY=..'" in block
    assert "건드리지 않습니다" in block
    # 생성은 else 가지에만 있어야 합니다.
    assert block.index("token_urlsafe") > block.index("else")


def test_the_app_port_is_not_open_to_the_world():
    """인증서 없이 8000 을 열면 로그인 비밀번호가 평문으로 오갑니다."""
    opened = re.findall(r"^ufw allow (\S+)", TEXT, re.M)
    assert set(opened) == {"22/tcp", "80/tcp", "443/tcp"}, \
        f"방화벽에서 연 포트: {opened}"


def test_state_lives_outside_the_code_directory():
    """`git pull` 이 포지션을 지우면 안 됩니다."""
    assert "DATA_DIR=/home/$APP_USER/data" in TEXT
    assert "APP_DIR=/home/$APP_USER/app" in TEXT
    assert "DB_PATH=$DATA_DIR" in TEXT


def test_the_service_account_cannot_log_in_as_a_person():
    """서비스 계정과 사람 계정을 나눠 두는 것이 사고 반경을 좁힙니다."""
    assert "useradd -m -s /bin/bash \"$APP_USER\"" in TEXT
    assert 'APP_USER=quant' in TEXT


def test_it_refuses_to_run_without_root():
    assert '[ "$(id -u)" -eq 0 ]' in TEXT


def test_it_fails_loudly_if_the_service_does_not_come_up():
    """"설치 완료" 를 찍어 놓고 서비스가 죽어 있으면 아무도 모릅니다."""
    assert "systemctl is-active --quiet quant" in TEXT
    assert "journalctl -u quant" in TEXT


@pytest.mark.parametrize("must", [
    "허용 IP 관리",        # 무엇을 등록해야 하는지
    "이 서버의 IP",         # 내 컴퓨터 IP 와 헷갈리지 않게
    "ssh -L 8000",         # 도메인 없이 안전하게 붙는 법
    "되살릴 수 없",         # 비밀키를 잃으면
])
def test_the_closing_message_says_what_to_do_next(must):
    tail = TEXT[TEXT.index("설치 완료"):]
    assert must in tail


def test_the_data_directory_is_not_world_readable():
    """남의 증권사 키가 사는 곳입니다."""
    assert 'install -d -o "$APP_USER" -g "$APP_USER" -m 700 "$DATA_DIR"' in TEXT
    assert 'chmod 600 "$ENV_FILE"' in TEXT


# ── 도메인 붙이기 ────────────────────────────────────────────────────────
DOMAIN_SH = Path("deploy/domain.sh")
DOMAIN_TEXT = DOMAIN_SH.read_text(encoding="utf-8")


def test_the_domain_script_is_valid_bash():
    subprocess.run(["bash", "-n", str(DOMAIN_SH)], check=True)


def test_it_checks_dns_before_asking_for_a_certificate():
    """DNS 가 아직 다른 곳을 가리키면 발급이 실패하고 한동안 재시도가 막힙니다.

    Let's Encrypt 는 실패를 세고, 몇 번 넘으면 그 도메인을 한 시간쯤 잠급니다.
    확인 없이 시도하는 것은 그 시간을 버리는 일입니다.
    """
    dns = DOMAIN_TEXT[DOMAIN_TEXT.index("1/5"):DOMAIN_TEXT.index("2/5")]
    assert "getent hosts" in dns
    assert 'RESOLVED" != "$SERVER_IP' in dns
    assert "재시도가 막힙니다" in dns


def test_it_waits_for_the_certificate_before_declaring_success():
    """Let's Encrypt 왕복에 몇 초 걸립니다 — 바로 확인하면 성공을 실패로 봅니다."""
    tail = DOMAIN_TEXT[DOMAIN_TEXT.index("5/5"):]
    assert "for _ in $(seq" in tail
    assert "api/health" in tail


def test_it_tells_the_app_its_own_origin():
    assert "CORS_ORIGINS=https://$DOMAIN" in DOMAIN_TEXT


def test_a_failure_says_where_to_look():
    assert "journalctl -u caddy" in DOMAIN_TEXT
    assert "ufw status" in DOMAIN_TEXT


# ── 배포가 사용자 입력을 덮어쓰지 않는가 ────────────────────────────────
#
# 예전에는 이 성질을 `render.yaml` 을 읽는 테스트가 지켰습니다. Render 를
# 없애면서 그 파일이 사라졌으므로, 같은 계약을 지금 배포하는 곳으로 옮깁니다 —
# 지켜야 하는 것은 블루프린트가 아니라 **"배포 설정은 사람이 입력한 값을
# 건드리지 않는다"** 는 사실이었습니다.

def test_the_installer_writes_no_broker_credentials():
    """증권사 키는 배포 설정이 아니라 **마이페이지** 에서 각자 입력합니다.

    설치 스크립트가 같은 이름을 환경에 올려 두면, 사용자가 화면에서 넣은
    값과 둘 중 어느 쪽이 진짜인지 알 수 없게 됩니다. 그리고 그 혼동은
    "키를 분명히 넣었는데 남의 계좌로 주문이 나간다" 로 끝납니다.
    """
    text = TEXT
    for name in ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO",
                 "KIS_PAPER_APP_KEY", "KIS_PAPER_APP_SECRET",
                 "TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_NO",
                 "ALPACA_API_KEY", "BINANCE_KEY"):
        assert f"{name}=" not in text, (
            f"설치 스크립트가 {name} 을 환경에 씁니다 — 증권사 키는 "
            "사용자별로 암호화돼 계정 DB 에 들어가야 합니다")


def test_the_installer_writes_no_daily_limits():
    """하루 한도는 설정 파일과 사용자 설정에서 옵니다. 배포가 환경변수로
    얹으면 그 값이 조용히 둘을 이깁니다 — 누가 정한 한도인지 알 수 없는
    상태로 실거래가 돕니다."""
    text = TEXT
    assert "QUANT_LIMIT_DAILY" not in text
