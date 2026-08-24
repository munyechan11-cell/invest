"""배포 설정이 사용자가 입력한 것을 잃어버리지 않는가.

증권사 키와 하루 한도는 배포 설정이 아니라 **사이트의 온보딩·마이페이지**에서
각자 입력합니다. 그래서 그 값이 어디에 저장되는지가 배포 설정의 책임이 됩니다.

Render 의 작업 디렉터리는 배포할 때마다 새로 만들어집니다. `.env` 를 기본
경로에 두면 마이페이지에서 입력한 한투 키가 재배포마다 조용히 사라지고,
봇은 설정되지 않은 채로 다시 떠 있습니다.
"""
import pathlib

import pytest

yaml = pytest.importorskip("yaml")

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def web():
    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    services = blueprint["services"]
    assert len(services) == 1, (
        "서비스가 둘이면 디스크를 나눠 가질 수 없습니다 — 대시보드와 트레이더가 "
        "서로 다른 상태를 보게 됩니다")
    return services[0]


@pytest.fixture(scope="module")
def env(web):
    return {e["key"]: e for e in web["envVars"]}


def test_everything_written_at_runtime_lives_on_the_disk(web, env):
    mount = web["disk"]["mountPath"]
    for key in ("DB_PATH", "QUANT_ENV_FILE"):
        value = env[key]["value"]
        assert value.startswith(mount + "/"), (
            f"{key}={value} 가 디스크({mount}) 밖입니다 — 재배포하면 사라집니다")


def test_the_encryption_key_is_generated_not_left_empty(env):
    """이 값으로 모든 사용자의 증권사 키를 암호화합니다. 없으면 서버가 뜨지 않습니다."""
    assert env["QUANT_SECRET_KEY"].get("generateValue") is True


def test_the_shared_operator_token_is_gone(env):
    """공용 토큰 하나가 관리자 자리를 열면 그건 로그인이 아니라 로그인의 우회입니다.

    여러 사람이 쓰는 서비스에서 그 값은 URL 과 프록시 로그를 타고 흐릅니다.
    사람을 정하는 것은 세션 쿠키뿐이어야 합니다.
    """
    assert "QUANT_API_TOKEN" not in env


def test_broker_keys_are_not_in_the_blueprint(env):
    """키는 사이트에서 입력합니다. 양쪽에 있으면 어느 쪽이 진짜인지 알 수 없습니다."""
    from quant.live.credentials import VENUES

    venue_keys = {name for v in VENUES for name, _, _ in v.fields}
    leaked = sorted(venue_keys & set(env))
    assert not leaked, f"증권사 키가 배포 설정에 있습니다: {leaked}"


def test_daily_limits_are_not_in_the_blueprint(env):
    limits = {"QUANT_LIMIT_DAILY_NOTIONAL", "QUANT_LIMIT_DAILY_ORDERS",
              "QUANT_LIMIT_DAILY_LOSS", "QUANT_LIMIT_DAILY_LOSS_PCT"}
    assert not (limits & set(env)), "하루 한도는 각자 사이트에서 입력합니다"


def test_every_key_in_the_blueprint_is_one_the_code_reads(env):
    """오타나 옛 이름이 남으면 채워도 아무 일이 일어나지 않습니다."""
    from quant.live.credentials import WRITABLE_KEYS

    # 화면에서 설정할 수 없고 프로세스가 뜨기 전에 있어야 하는 것들. 특히
    # QUANT_SECRET_KEY 는 **일부러** WRITABLE_KEYS 에 없습니다 — 자기 키를
    # 복호화하는 열쇠를 사용자가 설정 화면에서 바꿀 수 있으면 안 됩니다.
    infra = {"PYTHON_VERSION", "DB_PATH", "LOG_FORMAT", "QUANT_ENV_FILE",
             "QUANT_SECRET_KEY", "QUANT_USERS_DB", "QUANT_USER_DATA"}
    unknown = sorted(k for k in env if k not in WRITABLE_KEYS and k not in infra)
    assert not unknown, f"코드가 읽지 않는 키: {unknown}"


def test_the_service_does_not_sleep(web):
    """유휴 시 슬립되는 플랜은 자동매매에 쓸 수 없습니다."""
    assert web["plan"] != "free"


def test_the_start_command_binds_the_platform_port_and_state(web):
    cmd = web["startCommand"]
    assert "--host 0.0.0.0" in cmd and "$PORT" in cmd and "$DB_PATH" in cmd
