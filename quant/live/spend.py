"""봇이 쓴 LLM 을 누구 앞으로 다는가.

`/api/evaluate` 는 심의를 부르기 전에 요금제를 물어보고 끝나면 실제 비용을
적습니다. 봇이 부르는 심의에는 그게 없었습니다 — 계량되지 않는 LLM 호출
경로는 결국 운영자 카드로 청구되고, 나중에 소급해서 만들 수도 없습니다.

`LiveTrader` 는 계정도 요금제도 모릅니다(알아야 할 이유도 없습니다). 그래서
"물어보고 적는" 두 가지만 이 좁은 인터페이스로 받습니다. 단일 사용자 배포에는
셀 사람이 없으므로 이 값이 통째로 `None` 입니다.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class SpendMeter:
    """한 사용자에게 묶인 계량기."""

    #: () -> (허용 여부, 사용자에게 보여줄 사유)
    allow: Callable[[], tuple[bool, str]]
    #: (호출 수, 달러) -> None
    record: Callable[[int, float], None]
