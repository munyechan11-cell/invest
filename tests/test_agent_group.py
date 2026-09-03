"""에이전트 그룹 — 한 계좌를 나눠 쓰는 봇들의 구성 규칙.

여기서 확인하는 것은 "만들어진다" 가 아니라 **없는 돈이 생기지 않는다** 입니다.
계좌는 하나인데 에이전트는 넷이므로, 자본을 나누는 산수가 한 번만 틀려도 넷이
합쳐 계좌보다 큰 금액으로 사이징합니다. 그 사실은 우리 원장이 아니라 증권사의
주문 거절로 처음 드러나고, 그때는 이미 원장과 계좌가 갈라진 뒤입니다.

그래서 분배는 언제나 **내림** 이고, 합이 계좌를 넘지 않는 것을 여러 방향에서
확인합니다.
"""
import pytest

from quant.core.types import RunMode
from quant.live.agents import (
    MAX_AGENTS,
    AgentConfigError,
    AgentGroup,
    AgentSpec,
)


def spec(agent_id="a1", weight=0.5, mode=RunMode.DRY_RUN, **kw):
    return AgentSpec(
        agent_id=agent_id,
        label=kw.get("label", f"에이전트 {agent_id}"),
        config_path=kw.get("config_path", "configs/kr_toss_desk.yaml"),
        capital_weight=weight,
        mode=mode,
    )


# ── 스펙 하나의 규칙 ──────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["", "A1", "1a", "../etc", "a" * 33, "a b", "a/b"])
def test_agent_id_rejects_anything_that_could_become_a_path(bad):
    """id 는 디렉터리 이름이 됩니다 — `../` 하나가 남의 상태 파일에 닿습니다."""
    with pytest.raises(AgentConfigError):
        spec(agent_id=bad)


def test_zero_weight_is_refused_rather_than_silently_idle():
    """자본 0 인 에이전트는 주문을 못 내면서 데스크 비용은 그대로 씁니다.

    화면에서는 "도는 중" 으로 보이므로 사용자는 이것을 고장으로 읽습니다.
    """
    with pytest.raises(AgentConfigError, match="0 입니다"):
        spec(weight=0.0)


def test_weight_over_one_hundred_percent_is_refused():
    with pytest.raises(AgentConfigError, match="100%"):
        spec(weight=1.5)


def test_label_is_required_so_the_screen_can_tell_them_apart():
    with pytest.raises(AgentConfigError, match="이름이 없습니다"):
        spec(label="   ")


# ── 그룹의 규칙 ──────────────────────────────────────────────────────────
def test_empty_group_is_refused():
    with pytest.raises(AgentConfigError, match="하나도 없습니다"):
        AgentGroup(agents=())


def test_more_than_four_agents_is_refused():
    five = tuple(spec(agent_id=f"a{i}", weight=0.1) for i in range(MAX_AGENTS + 1))
    with pytest.raises(AgentConfigError, match=f"최대 {MAX_AGENTS}"):
        AgentGroup(agents=five)


def test_exactly_four_agents_is_allowed():
    four = tuple(spec(agent_id=f"a{i}", weight=0.25) for i in range(MAX_AGENTS))
    group = AgentGroup(agents=four)
    assert len(group.agents) == MAX_AGENTS
    assert group.total_weight == pytest.approx(1.0)


def test_duplicate_agent_id_is_refused():
    with pytest.raises(AgentConfigError, match="겹칩니다"):
        AgentGroup(agents=(spec("a1", 0.3), spec("a1", 0.3)))


def test_weights_summing_over_one_are_refused():
    """넷이 같은 돈을 각자 자기 것으로 보면 120% 가 나갑니다."""
    with pytest.raises(AgentConfigError, match="100% 를 넘습니다"):
        AgentGroup(agents=(spec("a1", 0.6), spec("a2", 0.6)))


def test_four_quarters_is_not_rejected_by_floating_point_dust():
    """0.25 를 네 번 더하면 1.0000000000000002 가 되는 경우가 있습니다.

    그것으로 사용자를 막으면 화면에서 25% 를 네 번 고른 사람이 시작하지
    못합니다.
    """
    group = AgentGroup(agents=tuple(
        spec(agent_id=f"a{i}", weight=0.25) for i in range(4)
    ))
    assert group.total_weight <= 1.0 + 1e-9


def test_weights_under_one_leave_the_rest_uninvested():
    """합이 100% 미만인 것은 허용됩니다 — 일부를 현금으로 두는 선택입니다."""
    group = AgentGroup(agents=(spec("a1", 0.3), spec("a2", 0.2)))
    assert group.total_weight == pytest.approx(0.5)


# ── 자본 분배 ────────────────────────────────────────────────────────────
def test_ten_man_won_splits_into_two_five_man_won():
    """사용자가 말한 그대로의 경우: 10만원 → 5만/5만."""
    group = AgentGroup(agents=(spec("attack", 0.5), spec("defend", 0.5)))
    assert group.allocate(100_000, quantum="1") == {
        "attack": 50_000.0, "defend": 50_000.0,
    }


def test_allocation_never_exceeds_the_account():
    """이 함수의 유일한 계약. 어떤 가중치 조합에서도 넘지 않습니다."""
    group = AgentGroup(agents=tuple(
        spec(agent_id=f"a{i}", weight=0.25) for i in range(4)
    ))
    for equity in (1.0, 7.0, 99.99, 100_000.0, 1_234_567.89):
        allocated = sum(group.allocate(equity, quantum="0.01").values())
        assert allocated <= equity + 1e-9, f"{equity} 에서 {allocated} 를 나눴습니다"


def test_indivisible_remainder_stays_in_the_account():
    """1원을 셋으로 나누면 0원씩이고 1원은 계좌에 남습니다.

    누구 하나에게 몰아주면 그 에이전트만 가중치보다 큰 자본을 갖게 되고, 그
    차이가 성향 비교를 조용히 오염시킵니다.
    """
    group = AgentGroup(agents=tuple(
        spec(agent_id=f"a{i}", weight=1 / 3) for i in range(3)
    ))
    out = group.allocate(100, quantum="1")
    assert sum(out.values()) == 99.0        # 33 × 3, 1원은 남는다
    assert set(out.values()) == {33.0}      # 몰아준 곳이 없다


def test_allocation_before_the_account_is_known_is_zero_not_a_guess():
    """워밍업 전이나 잔고 조회 실패 상태입니다.

    추정치를 넣으면 그 추정으로 진짜 주문이 나갑니다.
    """
    group = AgentGroup(agents=(spec("a1", 0.5), spec("a2", 0.5)))
    for unknown in (0.0, -1.0, float("nan"), float("inf")):
        assert group.allocate(unknown) == {"a1": 0.0, "a2": 0.0}


def test_partial_allocation_leaves_the_rest_alone():
    group = AgentGroup(agents=(spec("a1", 0.3),))
    assert group.allocate(100_000, quantum="1") == {"a1": 30_000.0}


# ── 실거래 등급 ──────────────────────────────────────────────────────────
def test_group_is_live_when_any_single_agent_is_live():
    """그룹의 위험 등급은 가장 위험한 에이전트가 정합니다.

    넷 중 하나만 실거래여도 계좌에서는 진짜 주문이 나갑니다.
    """
    group = AgentGroup(agents=(
        spec("watch", 0.5, mode=RunMode.DRY_RUN),
        spec("real", 0.5, mode=RunMode.LIVE),
    ))
    assert group.has_live is True


def test_all_dry_run_group_is_not_live():
    group = AgentGroup(agents=(spec("a1", 0.5), spec("a2", 0.5)))
    assert group.has_live is False


# ── 화면에서 온 목록 ─────────────────────────────────────────────────────
def test_from_dicts_builds_the_group_the_screen_described():
    group = AgentGroup.from_dicts([
        {"agent_id": "attack", "label": "공격 · 단기",
         "config_path": "configs/kr_toss_desk.yaml",
         "capital_weight": 0.5, "mode": "dry_run"},
        {"agent_id": "defend", "label": "보수 · 장기",
         "config_path": "configs/kr_toss.yaml",
         "capital_weight": 0.5, "mode": "live"},
    ])
    assert group.ids == ("attack", "defend")
    assert group.get("defend").mode is RunMode.LIVE
    assert group.has_live is True


def test_unknown_mode_is_refused_rather_than_downgraded():
    """실거래를 요청했는데 조용히 관찰만 돌면 사용자는 주문이 나가고 있다고
    믿은 채로 하루를 보냅니다. 반대 방향의 침묵은 더 나쁩니다."""
    with pytest.raises(AgentConfigError, match="실행 모드"):
        AgentGroup.from_dicts([
            {"agent_id": "a1", "label": "x", "config_path": "c.yaml",
             "capital_weight": 1.0, "mode": "paper"},
        ])


def test_backtest_mode_is_refused_with_a_pointer_to_the_right_command():
    with pytest.raises(AgentConfigError, match="백테스트"):
        AgentGroup.from_dicts([
            {"agent_id": "a1", "label": "x", "config_path": "c.yaml",
             "capital_weight": 1.0, "mode": "backtest"},
        ])


def test_to_dict_round_trips_through_from_dicts():
    group = AgentGroup(agents=(
        spec("a1", 0.4, mode=RunMode.LIVE), spec("a2", 0.6),
    ))
    again = AgentGroup.from_dicts(group.to_dict()["agents"])
    assert again == group
