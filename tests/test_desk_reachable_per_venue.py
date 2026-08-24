"""연동한 증권사 하나로 AI 데스크에 닿을 수 있는가.

토스만 연동한 사용자가 데스크가 붙은 전략을 고르면 화면이 이렇게 답했습니다:

    먼저 연동이 필요합니다: KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO

데스크를 쓰는 전략이 `kr-desk-gemini` 하나뿐이었고, 그건 시세·주문·수급을 전부
한국투자증권으로 받기 때문입니다. 즉 **토스 사용자는 이 서비스의 핵심 기능에
아예 닿을 수 없었습니다.** 키를 잘못 넣은 것도, 연동이 깨진 것도 아니고,
그 조합의 전략이 존재하지 않았던 것입니다.

증권사를 하나 더 뚫으라는 요구는 답이 아닙니다 — 한 곳을 연동한 사람은 그
한 곳으로 이 서비스를 쓸 수 있어야 합니다. 아래 테스트는 그것을 강제합니다.
"""
from __future__ import annotations

from quant.api.server import strategy_catalog
from quant.config.loader import load_config
from quant.webapp.registry import required_secrets

#: 사람이 실제로 연동하는 단위. 한 사람이 이 중 하나만 등록해도 이 서비스가
#: 쓸모 있어야 합니다.
VENUES = {
    "toss": {"TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_NO"},
    "kis": {"KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO", "KIS_ACCOUNT_PRD_CD"},
}


def _configs():
    for name, path in strategy_catalog().items():
        try:
            yield name, load_config(str(path))
        except Exception:
            continue          # 전략이 아닌 YAML(파라미터 공간 등)


def _has_desk(cfg) -> bool:
    return any(m.type in ("desk", "council") for m in cfg.alpha)


def test_each_venue_alone_can_run_a_desk_strategy():
    """한 증권사만 등록해도 데스크가 붙은 전략이 하나는 있어야 합니다."""
    for venue, keys in VENUES.items():
        reachable = [name for name, cfg in _configs()
                     if _has_desk(cfg) and set(required_secrets(cfg)) <= keys]
        assert reachable, (
            f"{venue} 만 연동한 사용자가 쓸 수 있는 데스크 전략이 없습니다. "
            f"그 사람에게 이 서비스의 AI 데스크는 존재하지 않는 기능입니다.")


def test_each_venue_alone_can_run_something_at_all():
    """데스크를 빼고도 — 한 증권사만으로 돌릴 수 있는 전략이 있어야 합니다."""
    for venue, keys in VENUES.items():
        reachable = [name for name, cfg in _configs()
                     if set(required_secrets(cfg)) <= keys]
        assert len(reachable) >= 2, f"{venue} 로 돌릴 수 있는 전략: {reachable}"


def test_a_toss_only_strategy_does_not_ask_for_flow_data():
    """토스 전용 전략이 수급 알파를 쓰면 조용히 KIS 키를 다시 요구합니다.

    수급(`investor_flow`)은 KIS 만 제공합니다. 토스 전용이라고 이름 붙여 놓고
    이 알파를 넣으면 `required_secrets` 에 KIS 키가 되살아나서, 고친 줄 알았던
    "먼저 연동이 필요합니다" 가 그대로 다시 뜹니다.
    """
    for name, cfg in _configs():
        touches_toss = cfg.data.provider == "toss" or cfg.broker.type == "toss"
        if not touches_toss or set(required_secrets(cfg)) - VENUES["toss"]:
            continue          # 애초에 토스 전용이 아닌 전략
        assert cfg.flow.provider != "kis", f"{name}: 토스 전용인데 수급을 KIS 로 받습니다"
        assert not any(m.type == "investor_flow" for m in cfg.alpha), (
            f"{name}: 토스가 주지 않는 수급 자료를 알파가 요구합니다")
