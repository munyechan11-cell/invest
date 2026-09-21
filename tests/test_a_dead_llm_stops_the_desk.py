"""LLM 이 죽었는데 데스크가 **사흘 내내** 때리고 있었습니다.

서버 로그가 그대로 말합니다:

    Sep 18 06:11:32 → Sep 21 00:23:53   좌석 실패가 사흘 내내
    google 429: Your project has exceeded its monthly spending cap
    데스크 비활성화: 0건                 ← 한 번도 스스로 안 껐습니다

멈추는 장치는 **있었습니다** — `QuotaExhausted` 를 만나면 데스크가 스스로
꺼집니다. 그런데 그 판정이 "per day / daily / quota_exceeded" 같은 말과
`retryDelay` 만 봤습니다. 구글이 돌려준 "monthly spending cap" 에는 그 중
아무것도 없어서 **평범한 혼잡** 으로 분류됐고, 사이클마다 열아홉 번씩
실패한 뒤 "분석가 합의 대체" 로 물러섰습니다.

화면에는 판정이 정상처럼 떴습니다. 그게 축약본이라는 사실은 작은 표 하나뿐이고,
그 상태가 사흘째라는 것은 어디에도 없었습니다.
"""
from __future__ import annotations

import asyncio

import pytest

from quant.alpha.llm_client import (
    LLMConfig,
    LLMError,
    LLMUsage,
    QuotaExhausted,
    _is_long_exhaustion,
)

# 실제로 이 사용자의 서버에서 돌아온 문장들입니다.
SPEND_CAP = ("google 429: Your project has exceeded its monthly spending cap. "
             "Please go to AI Studio at https://ai.studio/spend to manage your project")
DEPLETED = ("google 429: Your prepayment credits are depleted. Please go to "
            "AI Studio at https://ai.studio/projects to manage your project and billing")


# ── 무엇이 "기다려도 안 풀리는" 것인가 ──────────────────────────────────
@pytest.mark.parametrize("message", [SPEND_CAP, DEPLETED])
def test_money_problems_are_terminal_not_congestion(message):
    assert _is_long_exhaustion(message), (
        "결제 문제를 혼잡으로 읽으면 영원히 재시도합니다")


def test_the_old_signals_still_work():
    assert _is_long_exhaustion("Quota exceeded for quota metric per day")
    assert _is_long_exhaustion("rate limited, retryDelay: 30s")


def test_a_short_wait_is_still_just_congestion():
    """진짜 혼잡까지 치명으로 읽으면, 잠깐 쉬면 될 것에 데스크를 꺼 버립니다."""
    assert not _is_long_exhaustion("Resource exhausted, retry in 2.5s")
    assert not _is_long_exhaustion("internal error")


# ── 데스크가 실제로 멈추는가 ────────────────────────────────────────────
def disabled_reason(message: str, provider: str = "google") -> str:
    from quant.alpha.desk import TradingDesk
    from tests.test_desk import make_ctx

    class Broke:
        usage = LLMUsage()
        config = LLMConfig(provider=provider, api_key="x")

        async def complete(self, *a, **kw):
            raise LLMError(message)

    d = TradingDesk(Broke(), memory=False)
    asyncio.run(d.on_start(make_ctx()))
    return d.status()["disabled_reason"]


@pytest.mark.parametrize("message", [SPEND_CAP, DEPLETED])
def test_the_desk_turns_itself_off(message):
    assert disabled_reason(message), "꺼지지 않았습니다 — 사이클마다 다시 때립니다"


def test_a_spending_cap_does_not_tell_you_to_wait():
    """하루 할당량은 기다리면 풀리고, 지출 한도는 **기다려도 안 풀립니다.**
    한 문장으로 뭉개면 안 풀릴 것을 기다리게 됩니다."""
    said = disabled_reason(SPEND_CAP)
    assert "기다려도 풀리지 않습니다" in said
    assert "ai" in said.lower() and "Google AI Studio" in said


def test_a_daily_quota_says_it_will_clear():
    said = disabled_reason("google 429: Quota exceeded for quota metric per day")
    assert "하루 단위로 풀립니다" in said


def test_the_reason_does_not_paste_a_url_wall():
    said = disabled_reason(SPEND_CAP)
    assert "?" not in said.split("원문")[-1] or len(said) < 400


def test_it_names_the_provider_it_actually_used():
    """제미나이로 돌리는 사람에게 Anthropic 을 말하면 안 됩니다."""
    assert "Anthropic" not in disabled_reason(SPEND_CAP)


# ── 실제로 일어난 일: 시작은 멀쩡, 도중에 소진 ──────────────────────────
#
# 사전 점검은 05:43 에 통과했습니다. 잔액은 06:11 에 떨어졌습니다. 즉 이
# 고장은 **시작할 때가 아니라 도중에** 옵니다 — 사전 점검만으로는 절대
# 못 잡는 자리이고, 실제로 사흘을 그렇게 갔습니다.

class _DiesLater:
    """사전 점검은 통과하고, 그 뒤 호출부터 한도에 걸리는 대역."""

    usage = LLMUsage()

    def __init__(self, message: str):
        self.message = message
        self.config = LLMConfig(provider="google", api_key="x")
        self.calls = 0

    async def complete(self, system, user, schema=None):
        self.calls += 1
        if self.calls == 1:
            return "OK"                      # 사전 점검
        # **실제 클라이언트와 같은 규칙으로** 올립니다. `LLMClient.complete`
        # 은 429 중 기다려도 안 풀리는 것을 `QuotaExhausted` 로 바꿔 주는데,
        # 그 변환을 건너뛴 대역으로 시험하면 여기서만 통과하는 테스트가
        # 됩니다. 변환 규칙 자체는 위쪽 `_is_long_exhaustion` 테스트가 봅니다.
        if " 429:" in self.message and _is_long_exhaustion(self.message):
            raise QuotaExhausted(self.message)
        raise LLMError(self.message)


def run_until_it_gives_up(message: str):
    from quant.alpha.desk import TradingDesk
    from tests.test_desk import SYM, make_ctx

    llm = _DiesLater(message)
    d = TradingDesk(llm, memory=False)
    ctx = make_ctx()
    asyncio.run(d.on_start(ctx))
    assert not d.status()["disabled_reason"], "사전 점검은 통과해야 하는 시나리오입니다"
    asyncio.run(d.update(ctx, {SYM.key: ctx.history(SYM, 1)[0]}))
    return d, llm


@pytest.mark.parametrize("message", [SPEND_CAP, DEPLETED])
def test_exhaustion_that_arrives_mid_run_still_stops_the_desk(message):
    d, _ = run_until_it_gives_up(message)
    assert d.status()["disabled_reason"], (
        "도중에 소진된 것을 못 잡으면 사이클마다 열아홉 번씩 계속 때립니다")


def test_it_stops_calling_once_it_gives_up():
    """꺼진 뒤에도 부르면, 꺼진 의미가 없습니다."""
    from tests.test_desk import SYM, make_ctx

    d, llm = run_until_it_gives_up(SPEND_CAP)
    ctx = make_ctx()
    spent = llm.calls
    asyncio.run(d.update(ctx, {SYM.key: ctx.history(SYM, 1)[0]}))
    assert llm.calls == spent


def test_an_ordinary_seat_failure_does_not_kill_the_desk():
    """한 좌석이 한 번 실패한 것과 한도 소진은 다릅니다 — 전자까지 데스크를
    끄면 잠깐 흔들릴 때마다 판단이 멎습니다."""
    d, _ = run_until_it_gives_up("google 500: internal error")
    assert not d.status()["disabled_reason"]
