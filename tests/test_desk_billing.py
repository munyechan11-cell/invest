"""누구 키로 심의가 나가고, 그 비용을 누가 내는가.

AI 데스크 비용은 서비스가 냅니다. 그래서 두 가지가 동시에 참이어야 합니다:
요금제 상한이 실제로 걸릴 것, 그리고 상한을 면제받는 사람은 **정말로 자기
키로** 돌 것.

이 둘이 어긋나면 최악의 조합이 생깁니다 — 상한은 면제되는데 비용은 운영자가
내고, 그 지출이 운영자 집계에도 안 잡혀서 탐지되지 않습니다. 실제로 한 번
그랬습니다: `own_key` 를 "GOOGLE_API_KEY 라는 이름이 등록됐는가" 로 판정했고,
심의를 세우는 경로만 사용자 자격증명을 거치지 않았습니다.

출하 설정은 이제 Jev(`JEV_API_KEY`)로 돕니다. 같은 규칙을 **출하 설정의 실제
제공자** 로 확인하고, 계정 화면이 받는 자기 키(Gemini) 경로는 이 파일 안에서
google 로 바꾼 사본으로 따로 확인합니다. 그리고 둘이 섞였을 때 — Jev 데스크에
Gemini 키를 넣은 사람 — 가 상한을 면제받지 않는지도 봅니다.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from quant.alpha.llm_client import LLMError
from quant.config.loader import load_config
from quant.webapp.accounts import Accounts
from quant.webapp.registry import UserRegistry

GOOD = "correct-horse-9"
OPERATOR = "SERVICE-OPERATOR-KEY-SENTINEL"
MINE = "USER-OWN-KEY-SENTINEL"


#: (제공자, 그 제공자의 키 이름). 첫째는 출하 설정이 실제로 쓰는 것, 둘째는
#: 계정 화면의 자기 키(`_BYO_LLM_KEY`) 경로 — google 데스크를 쓰는 배포라면
#: 같은 규칙이 그대로 지켜져야 합니다.
PROVIDERS = [("jev", "JEV_API_KEY"), ("google", "GOOGLE_API_KEY")]

#: 결정석(`decision_llm`)이 따로 있는 모양. 출하 설정에는 이제 없지만(판정석도
#: 같은 클라이언트), README 예시와 보관 설정은 씁니다. 이게 빠지면 "자기 키가
#: 결정석까지 들어가는가" 를 아무도 안 봅니다 — 클라이언트가 하나뿐이라
#: `_keys(desk)` 가 저절로 한 원소가 되기 때문입니다.
DECISION_MODELS = {"jev": {"provider": "jev"},
                   "google": {"provider": "google", "model": "gemini-3.1-pro-preview"}}


def _desk_spec(cfg):
    return next(m for m in cfg.alpha if m.type in ("desk", "council"))


@pytest.fixture
def shipped_cfg():
    return load_config("configs/kr_desk_gemini.yaml")


@pytest.fixture(params=[(p, env, split) for p, env in PROVIDERS for split in (False, True)],
                ids=[p + ("-decision" if split else "")
                     for p, _ in PROVIDERS for split in (False, True)])
def desk_cfg(request, shipped_cfg):
    """(데스크 설정, 그 데스크가 읽는 키 이름, 결정석이 따로 있는가)."""
    provider, env, split = request.param
    cfg = shipped_cfg
    if _desk_spec(shipped_cfg).params["llm"]["provider"] != provider:
        cfg = shipped_cfg.model_copy(deep=True)
        _desk_spec(cfg).params["llm"] = {"provider": provider, "model": "gemini-3.7-flash"}
    if split:
        cfg = cfg.model_copy(deep=True)
        _desk_spec(cfg).params["decision_llm"] = dict(DECISION_MODELS[provider])
    return cfg, env, split


@pytest.fixture
def reg(tmp_path, monkeypatch):
    # 배포에서는 운영자 키가 실제로 프로세스 환경에 있습니다(render.yaml).
    for env in {env for _, env in PROVIDERS}:
        monkeypatch.setenv(env, OPERATOR)
    accounts = Accounts(tmp_path / "acc.db", secret="x" * 40)
    return UserRegistry(accounts, root=tmp_path / "users")


def _user(reg, email, key=None, env="JEV_API_KEY"):
    u = reg.accounts.register(email, GOOD)
    if key:
        reg.accounts.put_secret(u.id, env, key)
    return u


def _keys(desk):
    return {desk.client.config.resolved_key(),
            desk.decision_client.config.resolved_key()}


def test_the_shipped_desk_runs_on_jev_with_one_client(shipped_cfg):
    """아래 검사들의 첫 제공자가 출하 설정의 **실제** 제공자인지 고정합니다."""
    params = _desk_spec(shipped_cfg).params
    assert params["llm"]["provider"] == PROVIDERS[0][0] == "jev"
    assert "decision_llm" not in params          # 판정석도 같은 클라이언트·같은 키


def test_a_user_with_their_own_key_actually_uses_it(reg, desk_cfg):
    """자기 키를 넣었으면 그 키로 나가야 합니다.

    안 그러면 상한만 면제받고 비용은 운영자가 냅니다.
    """
    cfg, env, split = desk_cfg
    u = _user(reg, "mine@example.com", MINE, env)
    desk, own = reg.desk_for(u.id, cfg)
    # 결정석이 따로 있으면 클라이언트가 둘이어야 검사가 결정석까지 닿습니다.
    assert (desk.decision_client is not desk.client) is split
    assert _keys(desk) == {MINE}, "사용자 키가 데스크에 도달하지 않았습니다"
    assert own is True


def test_a_user_without_a_key_runs_on_the_service_key(reg, desk_cfg):
    cfg, _env, _split = desk_cfg
    u = _user(reg, "none@example.com")
    desk, own = reg.desk_for(u.id, cfg)
    assert _keys(desk) == {OPERATOR}
    assert own is False, "서비스 비용으로 도는데 상한을 면제받습니다"


def test_a_junk_key_does_not_buy_an_exemption_at_the_operators_expense(reg, desk_cfg):
    """아무 문자열이나 넣어 상한을 없애는 길이 있으면 안 됩니다.

    막는 방법은 그 값을 검증하는 것이 아니라 — 유효성은 불러 봐야 압니다 —
    **넣은 값을 실제로 쓰는** 것입니다. 가짜 키면 심의가 실패하고, 그 실패는
    그 사람 몫입니다. 운영자 카드로 넘어가지 않습니다.
    """
    cfg, env, split = desk_cfg
    u = _user(reg, "junk@example.com", "FAKEQA-anything-goes-here-000000", env)
    desk, own = reg.desk_for(u.id, cfg)
    assert (desk.decision_client is not desk.client) is split
    assert OPERATOR not in _keys(desk), \
        "가짜 키로 상한을 면제받으면서 운영자 키로 심의가 나갑니다"
    assert own is True


def test_one_seat_on_the_service_key_is_not_own_key(reg, desk_cfg, monkeypatch):
    """분석석만 자기 키고 결정석은 운영자 키면, "자기 키니까 무제한" 은 거짓입니다."""
    cfg, env, _split = desk_cfg
    u = _user(reg, "half@example.com", MINE, env)
    cfg = cfg.model_copy(deep=True)
    spec = _desk_spec(cfg)
    # 결정석만 다른 제공자로 바꿉니다 — 그쪽 키는 이 사용자에게 없습니다.
    # (출하 Jev 설정에는 결정석이 따로 없어 새로 답니다.)
    spec.params["decision_llm"] = {**spec.params.get("decision_llm", {}),
                                   "provider": "anthropic"}
    assert reg.desk_owns_key(u.id, cfg) is False


def test_a_gemini_key_does_not_exempt_a_jev_desk(reg, shipped_cfg):
    """계정 화면이 받는 자기 키는 Gemini 키인데, 출하 데스크는 Jev 로 돕니다.

    그 키를 넣은 사람을 "자기 키" 로 치면 상한은 사라지고 심의는 운영자의
    Jev 키로 나갑니다 — 이 파일이 막으려는 바로 그 조합입니다.
    """
    u = _user(reg, "gemini@example.com", MINE, "GOOGLE_API_KEY")
    desk, own = reg.desk_for(u.id, shipped_cfg)
    assert _keys(desk) == {OPERATOR}, "Jev 데스크에 Gemini 키가 들어갔습니다"
    assert own is False, "운영자 Jev 키로 도는데 상한을 면제받습니다"
    assert reg.desk_owns_key(u.id, shipped_cfg) is False


def test_a_strategy_without_a_desk_is_not_own_key(reg):
    u = _user(reg, "nodesk@example.com", MINE)
    reg.accounts.put_secret(u.id, "GOOGLE_API_KEY", MINE)
    assert reg.desk_owns_key(u.id, load_config("configs/demo.yaml")) is False
    desk, own = reg.desk_for(u.id, load_config("configs/demo.yaml"))
    assert desk is None and own is False


def test_the_meter_still_bites_for_service_funded_users(reg):
    """면제가 아닌 사람에게는 상한이 실제로 걸려야 합니다."""
    u = _user(reg, "capped@example.com")
    for _ in range(5):                       # 무료 요금제는 하루 5회
        reg.usage.record_spend(u.id, llm_calls=19, cost_usd=0.06, own_key=False)
    allowed, why = reg.usage.allow(u.id, "free", own_key=False)
    assert not allowed and "5회" in why


def test_service_funded_spend_is_visible_to_the_operator(reg):
    """운영자가 자기 지출을 못 보면 폭주를 알아챌 방법이 없습니다."""
    u = _user(reg, "seen@example.com")
    reg.usage.record_spend(u.id, llm_calls=19, cost_usd=0.06, own_key=False)
    month = reg.usage.operator_month()
    assert month["deliberations"] == 1
    assert month["cost_usd"] == pytest.approx(0.06)
    assert any(r["user_id"] == u.id for r in reg.usage.leaderboard())


def test_the_no_key_message_names_the_desks_own_provider(shipped_cfg):
    """출하 데스크는 Jev 입니다. 키가 없을 때 "본인 Gemini 키를 넣으세요" 라고
    하면 넣어도 아무것도 바뀌지 않습니다 — 그 키는 google 데스크에만 들어갑니다."""
    from quant.api.server import _desk_llm

    assert _desk_llm(shipped_cfg) == ("Jev", False)
    google = shipped_cfg.model_copy(deep=True)
    _desk_spec(google).params["llm"] = {"provider": "google"}
    assert _desk_llm(google) == ("Gemini", True)
    assert _desk_llm(load_config("configs/demo.yaml")) == ("LLM", False)


# ── 키가 없다는 안내가 실제 응답에서도 데스크의 제공자를 말하는가 ─────────────
#: 봇을 세우다 LLM 클라이언트에서 죽는 것과 같은 실패.
_NO_KEY = LLMError("no API key for provider 'jev' — set the matching env var")


class _NoKeyRegistry:
    async def start(self, *args, **kwargs):
        raise _NO_KEY

    async def start_group(self, *args, **kwargs):
        raise _NO_KEY


def _user_desk():
    from quant.api.server import UserDesk

    hub = SimpleNamespace(publish=lambda *a, **k: None)
    state = SimpleNamespace(hub_for=lambda uid: hub)
    return UserDesk(SimpleNamespace(id=1), state, None, _NoKeyRegistry())


def test_starting_a_jev_desk_without_a_key_names_jev_not_gemini():
    """헬퍼만이 아니라 **실제 시작 응답** 이 제공자를 따라가는가.

    예전 문구는 "본인 Gemini 키를 넣으세요" 였습니다. Jev 데스크에서는 그 키를
    넣어도 아무것도 바뀌지 않습니다 — 넣을 곳이 아니라 운영자에게 물을 일입니다.
    """
    from fastapi import HTTPException

    from quant.api.server import GroupStartRequest, StartRequest

    desk = _user_desk()
    with pytest.raises(HTTPException) as single:
        asyncio.run(desk.start(StartRequest(config_path="kr_desk_gemini")))
    with pytest.raises(HTTPException) as group:
        asyncio.run(desk.start_group(GroupStartRequest(agents=[{
            "agent_id": "a", "label": "A", "config_path": "kr_desk_gemini",
            "capital_weight": 1.0}])))
    for err in (single.value, group.value):
        assert err.status_code == 503
        assert "Jev" in err.detail and "운영자" in err.detail, err.detail
        assert "Gemini" not in err.detail, err.detail


def test_every_no_key_503_asks_the_desk_which_provider_it_uses():
    """`/api/evaluate` 는 앱 안에 묶인 함수라 본문을 봅니다 — 세 곳 모두 `_desk_llm`."""
    server = Path("quant/api/server.py").read_text(encoding="utf-8")
    bodies = {
        "start": re.findall(r"async def start\(self, req: StartRequest\).*?async def ",
                            server, re.S),
        "start_group": re.findall(r"async def start_group\(self.*?async def stop",
                                  server, re.S),
        "evaluate": re.findall(r'@app\.post\("/api/evaluate"\).*?@app\.get',
                               server, re.S),
    }
    for name, found in bodies.items():
        body = max(found, key=len)            # 추상 선언이 아니라 구현
        assert "_desk_llm(" in body, name
        assert "Gemini" not in body, name     # 제공자를 박아 둔 문구가 없다
