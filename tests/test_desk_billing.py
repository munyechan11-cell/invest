"""누구 키로 심의가 나가고, 그 비용을 누가 내는가.

AI 데스크 비용은 서비스가 냅니다. 그래서 두 가지가 동시에 참이어야 합니다:
요금제 상한이 실제로 걸릴 것, 그리고 상한을 면제받는 사람은 **정말로 자기
키로** 돌 것.

이 둘이 어긋나면 최악의 조합이 생깁니다 — 상한은 면제되는데 비용은 운영자가
내고, 그 지출이 운영자 집계에도 안 잡혀서 탐지되지 않습니다. 실제로 한 번
그랬습니다: `own_key` 를 "GOOGLE_API_KEY 라는 이름이 등록됐는가" 로 판정했고,
심의를 세우는 경로만 사용자 자격증명을 거치지 않았습니다.
"""
from __future__ import annotations

import pytest

from quant.config.loader import load_config
from quant.webapp.accounts import Accounts
from quant.webapp.registry import UserRegistry

GOOD = "correct-horse-9"
OPERATOR = "SERVICE-OPERATOR-KEY-SENTINEL"
MINE = "USER-OWN-KEY-SENTINEL"


@pytest.fixture
def desk_cfg():
    return load_config("configs/kr_desk_gemini.yaml")


@pytest.fixture
def reg(tmp_path, monkeypatch):
    # 배포에서는 운영자 키가 실제로 프로세스 환경에 있습니다(render.yaml).
    monkeypatch.setenv("GOOGLE_API_KEY", OPERATOR)
    accounts = Accounts(tmp_path / "acc.db", secret="x" * 40)
    return UserRegistry(accounts, root=tmp_path / "users")


def _user(reg, email, key=None):
    u = reg.accounts.register(email, GOOD)
    if key:
        reg.accounts.put_secret(u.id, "GOOGLE_API_KEY", key)
    return u


def _keys(desk):
    return {desk.client.config.resolved_key(),
            desk.decision_client.config.resolved_key()}


def test_a_user_with_their_own_key_actually_uses_it(reg, desk_cfg):
    """자기 키를 넣었으면 그 키로 나가야 합니다.

    안 그러면 상한만 면제받고 비용은 운영자가 냅니다.
    """
    u = _user(reg, "mine@example.com", MINE)
    desk, own = reg.desk_for(u.id, desk_cfg)
    assert _keys(desk) == {MINE}, "사용자 키가 데스크에 도달하지 않았습니다"
    assert own is True


def test_a_user_without_a_key_runs_on_the_service_key(reg, desk_cfg):
    u = _user(reg, "none@example.com")
    desk, own = reg.desk_for(u.id, desk_cfg)
    assert _keys(desk) == {OPERATOR}
    assert own is False, "서비스 비용으로 도는데 상한을 면제받습니다"


def test_a_junk_key_does_not_buy_an_exemption_at_the_operators_expense(reg, desk_cfg):
    """아무 문자열이나 넣어 상한을 없애는 길이 있으면 안 됩니다.

    막는 방법은 그 값을 검증하는 것이 아니라 — 유효성은 불러 봐야 압니다 —
    **넣은 값을 실제로 쓰는** 것입니다. 가짜 키면 심의가 실패하고, 그 실패는
    그 사람 몫입니다. 운영자 카드로 넘어가지 않습니다.
    """
    u = _user(reg, "junk@example.com", "FAKEQA-anything-goes-here-000000")
    desk, own = reg.desk_for(u.id, desk_cfg)
    assert OPERATOR not in _keys(desk), \
        "가짜 키로 상한을 면제받으면서 운영자 키로 심의가 나갑니다"
    assert own is True


def test_one_seat_on_the_service_key_is_not_own_key(reg, desk_cfg, monkeypatch):
    """분석석만 자기 키고 결정석은 운영자 키면, "자기 키니까 무제한" 은 거짓입니다."""
    u = _user(reg, "half@example.com", MINE)
    cfg = desk_cfg.model_copy(deep=True)
    spec = next(m for m in cfg.alpha if m.type in ("desk", "council"))
    # 결정석만 다른 제공자로 바꿉니다 — 그쪽 키는 이 사용자에게 없습니다.
    spec.params["decision_llm"] = {**spec.params["decision_llm"], "provider": "anthropic"}
    assert reg.desk_owns_key(u.id, cfg) is False


def test_a_strategy_without_a_desk_is_not_own_key(reg):
    u = _user(reg, "nodesk@example.com", MINE)
    assert reg.desk_owns_key(u.id, load_config("configs/demo.yaml")) is False
    desk, own = reg.desk_for(u.id, load_config("configs/demo.yaml"))
    assert desk is None and own is False


def test_the_meter_still_bites_for_service_funded_users(reg, desk_cfg):
    """면제가 아닌 사람에게는 상한이 실제로 걸려야 합니다."""
    u = _user(reg, "capped@example.com")
    for _ in range(5):                       # 무료 요금제는 하루 5회
        reg.usage.record_spend(u.id, llm_calls=19, cost_usd=0.06, own_key=False)
    allowed, why = reg.usage.allow(u.id, "free", own_key=False)
    assert not allowed and "5회" in why


def test_service_funded_spend_is_visible_to_the_operator(reg, desk_cfg):
    """운영자가 자기 지출을 못 보면 폭주를 알아챌 방법이 없습니다."""
    u = _user(reg, "seen@example.com")
    reg.usage.record_spend(u.id, llm_calls=19, cost_usd=0.06, own_key=False)
    month = reg.usage.operator_month()
    assert month["deliberations"] == 1
    assert month["cost_usd"] == pytest.approx(0.06)
    assert any(r["user_id"] == u.id for r in reg.usage.leaderboard())
