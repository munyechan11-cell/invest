"""데스크 사용량 계량과 상한.

비용을 운영자가 내는 구조라, 한 사람의 설정 실수가 운영자 카드로 청구되면
안 됩니다. 그리고 요금제는 나중 일이지만 계량은 첫날부터 돌아야 합니다 —
"누가 얼마나 썼는가"는 소급해서 만들 수 없습니다.
"""
from datetime import datetime, timedelta

import pytest

from quant.core.types import UTC
from quant.webapp.usage import PLANS, UsageStore, plan_for

MODEL = "gemini-3.7-flash"
# 실측 심의 1회: 입력 53,289 / 출력 6,571 → 약 $0.065
ONE = {"model": MODEL, "llm_calls": 16,
       "input_tokens": 53_289, "output_tokens": 6_571}


@pytest.fixture
def store(tmp_path):
    return UsageStore(tmp_path / "usage.db")


def test_a_deliberation_is_priced_by_the_model_that_ran_it(store):
    cost = store.record(1, **ONE)
    assert round(cost, 4) == 0.0646
    assert store.today(1)["deliberations"] == 1
    assert store.today(1)["llm_calls"] == 16


def test_usage_accumulates_within_a_day(store):
    for _ in range(3):
        store.record(1, **ONE)
    today = store.today(1)
    assert today["deliberations"] == 3
    assert round(today["cost_usd"], 3) == 0.194


def test_one_users_spending_is_not_anothers(store):
    store.record(1, **ONE)
    store.record(1, **ONE)
    store.record(2, **ONE)
    assert store.today(1)["deliberations"] == 2
    assert store.today(2)["deliberations"] == 1


def test_the_day_rolls_on_korean_time_not_utc(store):
    """UTC 자정에 초기화하면 한국 장 한복판에서 리셋됩니다."""
    # 2026-08-24 00:30 KST = 2026-08-23 15:30 UTC
    late = datetime(2026, 8, 23, 15, 30, tzinfo=UTC)
    early = datetime(2026, 8, 23, 14, 30, tzinfo=UTC)   # 같은 날 23:30 KST
    store.record(1, now=early, **ONE)
    store.record(1, now=late, **ONE)
    assert store.today(1, now=early)["deliberations"] == 1
    assert store.today(1, now=late)["deliberations"] == 1


# ── 상한 ────────────────────────────────────────────────────────────────
def test_the_free_plan_stops_at_its_daily_count(store):
    plan = PLANS["free"]
    for _ in range(plan.daily_deliberations):
        assert store.allow(1, "free")[0]
        store.record(1, **ONE)
    allowed, reason = store.allow(1, "free")
    assert not allowed
    assert "규칙 기반" in reason, "무엇이 계속 도는지 알려주지 않으면 겁만 줍니다"


def test_tomorrow_opens_again(store):
    for _ in range(PLANS["free"].daily_deliberations):
        store.record(1, **ONE)
    now = datetime.now(UTC)
    assert not store.allow(1, "free", now=now)[0]
    assert store.allow(1, "free", now=now + timedelta(days=1))[0]


def test_a_users_own_key_is_not_charged_to_the_operator(store):
    """자기 키로 돌리면 상한을 받지 않고, 운영자 비용에도 안 잡힙니다."""
    for _ in range(20):
        store.record(1, own_key=True, **ONE)
    assert store.allow(1, "free", own_key=True)[0]
    assert store.today(1)["deliberations"] == 0          # 운영자 부담분은 0
    assert store.operator_month()["cost_usd"] == 0.0


def test_the_monthly_ceiling_catches_what_the_daily_one_lets_through(store):
    """하루 5회씩 한 달이면 하루 상한만으로는 안 막힙니다."""
    day = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)
    for d in range(20):
        for _ in range(PLANS["free"].daily_deliberations):
            store.record(1, now=day + timedelta(days=d), **ONE)
    at = day + timedelta(days=20)
    assert store.month(1, now=at)["cost_usd"] > PLANS["free"].monthly_cost_usd
    assert not store.allow(1, "free", now=at)[0]


def test_the_unlimited_plan_has_no_ceiling(store):
    for _ in range(500):
        store.record(1, **ONE)
    assert store.allow(1, "unlimited")[0]


def test_an_unknown_plan_falls_back_to_free_not_to_unlimited(store):
    """오타 하나가 무제한이 되면 안 됩니다."""
    assert plan_for("gold").id == "free"
    assert plan_for(None).id == "free"
    assert plan_for("").id == "free"


# ── 운영자가 볼 것 ──────────────────────────────────────────────────────
def test_the_operator_can_see_who_is_expensive(store):
    """요금제를 정하려면 누가 얼마나 쓰는지부터 봐야 합니다."""
    for _ in range(5):
        store.record(1, **ONE)
    store.record(2, **ONE)
    board = store.leaderboard()
    assert [r["user_id"] for r in board] == [1, 2]
    assert board[0]["cost_usd"] > board[1]["cost_usd"]


def test_status_tells_the_user_where_they_stand(store):
    store.record(1, **ONE)
    s = store.status(1, "free")
    assert s["plan"]["label"] == "무료"
    assert s["today"] == {"deliberations": 1, "limit": 5}
    assert s["allowed"]
