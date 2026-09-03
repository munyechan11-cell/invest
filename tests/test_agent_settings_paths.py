"""에이전트별 성향·한도 — 공격형의 손절이 보수형에 적용되지 않는가.

성향을 사람이 아니라 에이전트의 속성으로 옮기는 것이 이 기능의 전부입니다.
파일이 갈리지 않으면 나머지는 전부 무의미합니다 — 화면에서 공격형의 손절을
넓히면 보수형의 손절도 함께 넓어지고, 두 성향은 하나로 뭉개집니다.

동시에 **그룹을 쓰지 않는 사람의 경로는 정확히 그대로** 여야 합니다. 기존
사용자의 성향과 한도가 파일 이동 때문에 초기화되면, 그 사람은 자기가 고른 손절
폭을 잃고도 그 사실을 모릅니다.
"""
import json
import os
import stat

import pytest

from quant.live.profile import InvestorProfile
from quant.webapp.accounts import Accounts
from quant.webapp.registry import UserRegistry

SECRET = "agent-settings-test-secret-0123456789abcdef"


@pytest.fixture
def registry(tmp_path):
    accounts = Accounts(tmp_path / "users.db", secret=SECRET)
    reg = UserRegistry(accounts, root=tmp_path / "users")
    user = accounts.register("a@b.com", "pw-12345678")
    yield reg, user.id
    accounts.close()


def profile(risk):
    """R 축만 움직인 성향 하나. 손절 폭이 여기서 나옵니다."""
    return InvestorProfile(scores={"R": risk, "H": 0.0, "E": 0.0, "C": 0.0})


# ── 파일이 갈리는가 ──────────────────────────────────────────────────────
def test_two_agents_keep_separate_profiles(registry):
    reg, uid = registry
    reg.save_profile(uid, profile(1.0), "attack")
    reg.save_profile(uid, profile(-1.0), "defend")

    attack = reg.profile(uid, "attack")
    defend = reg.profile(uid, "defend")

    assert attack.settings()["stop_atr_multiple"] != defend.settings()["stop_atr_multiple"]
    assert attack.scores["R"] == 1.0
    assert defend.scores["R"] == -1.0


def test_two_agents_keep_separate_limits(registry):
    reg, uid = registry
    reg.save_limits(uid, {"max_daily_orders": 60}, "attack")
    reg.save_limits(uid, {"max_daily_orders": 5}, "defend")

    assert reg.limits(uid, "attack")["max_daily_orders"] == 60
    assert reg.limits(uid, "defend")["max_daily_orders"] == 5


def test_saving_one_agents_limit_does_not_touch_the_other(registry):
    reg, uid = registry
    reg.save_limits(uid, {"max_daily_orders": 60, "max_daily_loss": 300_000},
                    "attack")
    reg.save_limits(uid, {"max_daily_orders": 5}, "defend")

    attack = reg.limits(uid, "attack")
    assert attack["max_daily_orders"] == 60
    assert attack["max_daily_loss"] == 300_000


def test_each_agent_gets_its_own_directory(registry):
    reg, uid = registry
    reg.save_profile(uid, profile(0.5), "attack")

    paths = reg.agent_paths(uid, "attack")
    assert paths.home.name == "attack"
    assert paths.home.parent.name == "agents"
    assert paths.profile.exists()


# ── 그룹을 쓰지 않는 사람은 그대로 ───────────────────────────────────────
def test_a_user_without_agents_uses_the_original_files(registry):
    """1인 1봇 시절의 자리 그대로여야 합니다."""
    reg, uid = registry
    reg.save_profile(uid, profile(0.7))

    user_paths = reg.paths(uid)
    assert user_paths.profile.exists()
    assert not user_paths.agents_root.exists(), "쓰지도 않는 디렉터리를 만들었습니다"


def test_the_account_level_files_are_not_disturbed_by_agents(registry):
    """기존 사용자가 그룹을 만들어도 원래 설정은 그 자리에 남습니다."""
    reg, uid = registry
    reg.save_limits(uid, {"max_daily_orders": 12})
    reg.save_limits(uid, {"max_daily_orders": 99}, "attack")

    assert reg.limits(uid)["max_daily_orders"] == 12
    assert reg.limits(uid, "attack")["max_daily_orders"] == 99


def test_an_agent_with_nothing_saved_reads_as_no_caps(registry):
    reg, uid = registry
    assert reg.limits(uid, "attack") == {
        "max_daily_notional": 0.0, "max_daily_orders": 0.0,
        "max_daily_loss": 0.0, "max_daily_loss_pct": 0.0,
    }


# ── 경로가 되기 전에 검증한다 ────────────────────────────────────────────
@pytest.mark.parametrize("sneaky", ["../../etc", "..", "a/b", "A1", "", "ok\n"])
def test_an_agent_id_that_could_escape_the_directory_is_refused(registry, sneaky):
    """`../` 하나가 남의 에이전트 상태에 닿습니다 — `_uid()` 와 같은 이유입니다."""
    reg, uid = registry
    with pytest.raises(ValueError):
        reg.agent_paths(uid, sneaky)


def test_the_agent_directory_is_private(registry):
    """포지션과 체결 기록이 들어 있습니다. 한 단계만 빠뜨려도 그 아래 전부가
    열립니다."""
    reg, uid = registry
    paths = reg.agent_paths(uid, "attack")

    for directory in (paths.home, paths.home.parent, paths.home.parent.parent):
        mode = stat.S_IMODE(os.stat(directory).st_mode)
        assert mode == 0o700, f"{directory} 가 {oct(mode)} 입니다"


def test_the_saved_limits_file_is_readable_json(registry):
    reg, uid = registry
    reg.save_limits(uid, {"max_daily_loss": 50_000}, "attack")

    raw = json.loads(reg.agent_paths(uid, "attack").limits.read_text("utf-8"))
    assert raw["max_daily_loss"] == 50_000


# ── 아직 돌지 않는 에이전트에 성향을 저장할 때 ───────────────────────────
def test_saving_an_agent_profile_does_not_reach_the_single_bot(registry):
    """조용히 단일 봇에 적용하면, 공격형의 성향 변경이 보수형 봇에 적용되고
    화면은 성공했다고 말합니다."""
    reg, uid = registry
    assert reg.save_profile(uid, profile(1.0), "attack") is None
    assert reg.trader(uid, "attack") is None
