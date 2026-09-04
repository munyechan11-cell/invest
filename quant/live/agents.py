"""에이전트 — 한 계좌 안에서 성향이 다른 봇 여럿을 굴리는 단위.

1인 1봇일 때 "성향" 은 사람의 속성이었습니다. 프로필 하나, 한도 하나, 전략
하나. 공격적 단기와 보수적 장기를 같이 보고 싶으면 하나를 멈춰야 했고, 멈춘
쪽은 비교 대상이 아니라 그냥 없는 것이 됩니다.

성향을 **사람이 아니라 에이전트의 속성**으로 옮깁니다. 한 사람이 최대 네 개의
에이전트를 두고, 각 에이전트는 자기 전략 설정·자기 성향·자기 손절·자기 하루
한도를 갖습니다. 계좌는 그대로 하나입니다.

계좌가 하나라는 사실이 이 모듈이 존재하는 이유 전부입니다:

**자본은 나눠야 합니다.** 나누지 않으면 네 에이전트가 같은 10만원을 각자 자기
것으로 보고 각자 사이징합니다. 넷이 동시에 "현금의 30%" 를 쓰면 120% 가 나가고,
그 사실은 주문이 거절되고 나서야 드러납니다. `allocate()` 가 계좌 자산을
가중치대로 쪼개고, **합이 계좌를 넘지 않는 것** 은 반올림 단계에서도 유지됩니다
— 올림 한 번이 없는 돈을 만듭니다.

**개수에는 천장이 있습니다.** 네 개입니다. 에이전트 하나는 완전한 엔진 하나라
워밍업·시세 구독·LLM 심의를 각자 돌리고, 무엇보다 **사람이 동시에 이해할 수
있어야** 합니다. 계좌 하나에서 서로 반대로 움직이는 봇이 다섯 개면 화면이
설명할 수 있는 것은 이미 없습니다.

**가중치 0 은 받지 않습니다.** 자본이 0인 에이전트는 주문을 낼 수 없으면서
데스크 좌석과 LLM 비용은 그대로 씁니다. "잠시 꺼둠" 을 표현하고 싶으면
에이전트를 빼십시오 — 돌지 않는 것과 돌지만 아무것도 못 하는 것은 화면에서
구별되지 않고, 후자는 사용자가 고장으로 읽습니다.

`AgentSpec` 은 무엇을 돌릴지만 말합니다. 실제 엔진·슬리브·게이트웨이 배선은
`quant.brokerage.sleeve` 와 `quant.live.gateway` 가 맡습니다.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal

from quant.core.types import RunMode

#: 한 계좌에 둘 수 있는 에이전트 수. 위 docstring 의 이유로 네 개입니다.
MAX_AGENTS = 4

#: 디렉터리 이름이 되는 값이라 경로가 될 수 있는 문자는 전부 막습니다.
#: `UserRegistry._uid` 가 사용자 id 에 하는 것과 같은 이유입니다 — `../` 하나가
#: 남의 에이전트 상태에 닿습니다.
#:
#: 끝을 `$` 가 아니라 `\Z` 로 닫습니다. 파이썬의 `$` 는 문자열 맨 끝의 개행
#: **앞** 에서도 일치하므로 `"attack\n"` 이 통과합니다. 경로 탈출은 아니지만
#: `"attack"` 과 `"attack\n"` 이 서로 다른 디렉터리가 되어, 화면에는 같은 이름의
#: 에이전트가 둘 보이고 각자 다른 성향 파일을 읽습니다.
_AGENT_ID = re.compile(r"\A[a-z][a-z0-9_-]{0,31}\Z")

#: 가중치 합이 이만큼까지는 부동소수 오차로 봅니다. 1.0000000000000002 을
#: "100% 초과" 로 거절하면 화면에서 25% 를 네 번 고른 사용자가 막힙니다.
_WEIGHT_EPSILON = 1e-9


class AgentConfigError(ValueError):
    """에이전트 구성이 스스로 모순이다. 문장은 그대로 화면에 나갑니다."""


@dataclass(frozen=True)
class AgentSpec:
    """에이전트 한 대가 무엇을 돌릴지.

    성향(`profile`)과 하루 한도(`limits`)는 여기 담지 않습니다. 둘 다 사용자가
    화면에서 자주 바꾸는 값이라 파일로 삽니다(`agents/{agent_id}/profile.json`,
    `limits.json`) — 실행 중인 에이전트에 즉시 반영하는 경로가 이미 있고
    (`UserRegistry.apply_profile_live`), 그 경로를 스펙 사본이 가로채면 화면이
    바꾼 값과 봇이 쓰는 값이 갈립니다.
    """

    #: 디렉터리 이름이자 화면·이벤트·상태 DB 의 귀속 키.
    agent_id: str
    #: 사람이 읽는 이름. "공격 · 단기" 처럼 사용자가 붙입니다.
    label: str
    #: 이 에이전트가 돌릴 전략 설정 파일.
    config_path: str
    #: 계좌 자산 중 이 에이전트 몫. 0 < w <= 1.
    capital_weight: float
    #: 에이전트마다 따로 정합니다 — 하나는 실거래, 하나는 관찰만 이 가능해야
    #: 새 성향을 돈을 걸지 않고 같은 시세로 검증할 수 있습니다.
    mode: RunMode = RunMode.DRY_RUN

    def __post_init__(self) -> None:
        if not _AGENT_ID.match(self.agent_id or ""):
            raise AgentConfigError(
                f"에이전트 id 는 영소문자로 시작하는 32자 이내의 "
                f"영숫자·_·- 여야 합니다: {self.agent_id!r}"
            )
        if not (self.label or "").strip():
            raise AgentConfigError(
                f"에이전트 {self.agent_id} 에 이름이 없습니다 — 화면에서 "
                f"어느 봇이 무엇을 하는지 구별할 수 없습니다"
            )
        if not (self.config_path or "").strip():
            raise AgentConfigError(f"에이전트 {self.agent_id} 에 전략 설정이 없습니다")
        weight = float(self.capital_weight)
        if not math.isfinite(weight):
            raise AgentConfigError(
                f"에이전트 {self.agent_id} 의 자본 비중이 숫자가 아닙니다"
            )
        if weight <= 0:
            raise AgentConfigError(
                f"에이전트 {self.agent_id} 의 자본 비중이 0 입니다 — 주문을 낼 수 "
                f"없으면서 데스크 비용은 그대로 듭니다. 쓰지 않을 에이전트는 "
                f"비중을 0 으로 두지 말고 목록에서 빼세요."
            )
        if weight > 1.0 + _WEIGHT_EPSILON:
            raise AgentConfigError(
                f"에이전트 {self.agent_id} 의 자본 비중이 100% 를 넘습니다: {weight:.1%}"
            )

    @property
    def is_live(self) -> bool:
        return self.mode is RunMode.LIVE

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "label": self.label,
            "config_path": self.config_path,
            "capital_weight": round(float(self.capital_weight), 6),
            "mode": self.mode.value,
        }


@dataclass(frozen=True)
class AgentGroup:
    """한 사용자의 에이전트 전부. 계좌 하나를 나눠 쓰는 집합.

    그룹은 **실행 단위이자 정지 단위**입니다. 슬리브 원장이 증권사 잔고와
    어긋나면 어느 에이전트가 틀렸는지 알 방법이 없으므로 그때는 그룹 전체가
    멈춥니다 — 자세한 것은 `quant.live.gateway` 에 있습니다.
    """

    agents: tuple[AgentSpec, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.agents:
            raise AgentConfigError("에이전트가 하나도 없습니다")
        if len(self.agents) > MAX_AGENTS:
            raise AgentConfigError(
                f"에이전트는 최대 {MAX_AGENTS} 개까지입니다 (요청 {len(self.agents)} 개)"
            )
        seen: set[str] = set()
        for spec in self.agents:
            if spec.agent_id in seen:
                raise AgentConfigError(
                    f"에이전트 id 가 겹칩니다: {spec.agent_id} — 상태와 체결이 "
                    f"같은 자리에 쌓여 둘 중 누구의 포지션인지 사라집니다"
                )
            seen.add(spec.agent_id)
        total = self.total_weight
        if total > 1.0 + _WEIGHT_EPSILON:
            raise AgentConfigError(
                f"자본 비중 합이 100% 를 넘습니다: {total:.1%} — 넷이 같은 돈을 "
                f"각자 자기 것으로 보고 사이징하면 주문이 거절되고 나서야 "
                f"드러납니다"
            )

    # ── 조회 ─────────────────────────────────────────────────────────────
    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(spec.agent_id for spec in self.agents)

    @property
    def total_weight(self) -> float:
        return math.fsum(float(spec.capital_weight) for spec in self.agents)

    @property
    def has_live(self) -> bool:
        """실거래 에이전트가 하나라도 있는가.

        그룹의 위험 등급은 가장 위험한 에이전트가 정합니다. 넷 중 하나만
        실거래여도 계좌에서는 진짜 주문이 나가므로, 상태 DB 점유·복구 게이트
        같은 계좌 단위 방어는 전부 이 값을 봅니다.
        """
        return any(spec.is_live for spec in self.agents)

    def get(self, agent_id: str) -> AgentSpec:
        for spec in self.agents:
            if spec.agent_id == agent_id:
                return spec
        raise KeyError(f"그런 에이전트가 없습니다: {agent_id}")

    # ── 자본 분배 ────────────────────────────────────────────────────────
    def allocate(self, account_equity: float,
                 quantum: str | Decimal = "0.01") -> dict[str, float]:
        """계좌 자산을 에이전트별 몫으로 쪼갠다.

        **합이 계좌를 넘지 않는 것이 이 함수의 유일한 계약입니다.** 넘는 순간
        없는 돈이 생기고, 그 돈으로 낸 주문은 증권사가 거절합니다 — 우리 쪽
        원장에는 남고 계좌에는 없는 포지션이 그렇게 만들어집니다.

        그래서 각 몫은 `quantum` 격자로 **내림** 합니다. 올림은 한 번도 하지
        않습니다. 내림에서 남는 잔돈(최대 에이전트 수 × quantum)은 어느
        에이전트에도 주지 않고 계좌에 남깁니다 — 누구 하나에게 몰아주면 그
        에이전트만 가중치보다 큰 자본을 갖게 되고, 그 차이는 성향 비교를
        조용히 오염시킵니다.

        `quantum` 은 통화의 최소 단위입니다. 원화 계좌면 `"1"` 을 주세요.
        """
        equity = float(account_equity)
        if not math.isfinite(equity) or equity <= 0:
            # 워밍업 전이거나 잔고 조회가 아직 실패한 상태입니다. 0 을 나눠
            # 주는 것이 맞습니다 — 추정치를 넣으면 그 추정으로 주문이 나갑니다.
            return {spec.agent_id: 0.0 for spec in self.agents}

        step = Decimal(str(quantum))
        if step <= 0:
            raise AgentConfigError(f"자본 분배 단위는 양수여야 합니다: {quantum!r}")

        total = Decimal(str(equity))
        out: dict[str, float] = {}
        for spec in self.agents:
            raw = total * Decimal(str(float(spec.capital_weight)))
            steps = (raw / step).to_integral_value(rounding=ROUND_FLOOR)
            out[spec.agent_id] = float(steps * step)

        # 내림만 했으므로 이 단언은 구조적으로 참입니다. 그래도 남겨 둡니다 —
        # 이 함수가 틀리면 틀렸다는 사실이 계좌에서 처음 드러나기 때문입니다.
        allocated = math.fsum(out.values())
        if allocated > equity + _WEIGHT_EPSILON:
            raise AgentConfigError(
                f"자본 분배가 계좌를 넘었습니다: {allocated:.2f} > {equity:.2f}"
            )
        return out

    def to_dict(self) -> dict:
        return {
            "agents": [spec.to_dict() for spec in self.agents],
            "total_weight": round(self.total_weight, 6),
            "has_live": self.has_live,
            "max_agents": MAX_AGENTS,
        }

    # ── 만들기 ───────────────────────────────────────────────────────────
    @classmethod
    def from_dicts(cls, rows: list[dict]) -> AgentGroup:
        """화면·API 가 보낸 목록에서 그룹을 만든다.

        `mode` 는 문자열로 옵니다. 모르는 값을 `DRY_RUN` 으로 조용히 떨어뜨리지
        않습니다 — 실거래를 요청했는데 관찰만 돌면 사용자는 자기 봇이 주문을
        내고 있다고 믿은 채로 하루를 보냅니다. 반대 방향의 침묵은 더 나쁩니다.
        """
        specs: list[AgentSpec] = []
        for index, row in enumerate(rows or []):
            raw_mode = str(row.get("mode", RunMode.DRY_RUN.value))
            try:
                mode = RunMode(raw_mode)
            except ValueError:
                raise AgentConfigError(
                    f"{index + 1} 번째 에이전트의 실행 모드를 알 수 없습니다: "
                    f"{raw_mode!r}"
                ) from None
            if mode is RunMode.BACKTEST:
                raise AgentConfigError(
                    f"{index + 1} 번째 에이전트: 백테스트는 에이전트로 돌리지 "
                    f"않습니다 — `quant backtest` 를 쓰세요"
                )
            specs.append(AgentSpec(
                agent_id=str(row.get("agent_id", "")),
                label=str(row.get("label", "")),
                config_path=str(row.get("config_path", "")),
                capital_weight=row.get("capital_weight", 0.0),
                mode=mode,
            ))
        return cls(agents=tuple(specs))
