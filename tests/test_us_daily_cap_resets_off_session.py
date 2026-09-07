"""미국 주식 전략의 하루 한도는 **장중에 초기화되면 안 됩니다.**

`TradingBudget.roll` 은 `(now + timezone_offset_hours)` 의 날짜가 바뀌는 순간
새 원장을 열고 중단을 풉니다. 미국 토스 설정이 한국시간(9)을 쓰고 있었는데,
한국 자정은 뉴욕의 10:00~11:00 — 개장 30~90분 뒤입니다. 09:45 ET 에 하루
손실 한도로 멈춘 봇이 몇 분 뒤 새 한도를 통째로 받아 다시 샀습니다.

이 검사는 값을 베끼지 않습니다(`== -5`). 성질을 봅니다 — 2026년 어느 날이든
09:30~16:00 뉴욕 시간 사이에 원장이 바뀌지 않는다. 미국 주식 설정은 캘린더로
찾습니다: 파일 이름이나 통화로 고르면 새 설정이 조용히 빠집니다.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from quant.config.loader import load_config
from quant.core.types import UTC
from quant.live.limits import TradingBudget

NY = ZoneInfo("America/New_York")
OPEN, CLOSE = time(9, 30), time(16, 0)
#: 전략 설정만 — `space.yaml` 같은 탐색 공간 파일에는 `alpha` 가 없습니다.
CONFIGS = [p for p in sorted(Path("configs").glob("*.yaml"))
           if "alpha" in (yaml.safe_load(p.read_text(encoding="utf-8")) or {})]


def _is_us_equity(path: Path) -> bool:
    return load_config(str(path))._calendar().name == "us_equity"


US_CONFIGS = [p for p in CONFIGS if _is_us_equity(p)]


def _days(year: int):
    day = date(year, 1, 1)
    while day.year == year:
        yield day
        day += timedelta(days=1)


def _resets_inside_the_session(tz_offset_hours: float, year: int) -> list[date]:
    """그 해에 원장이 09:30~16:00 ET 사이에 바뀌는 날들.

    `local_day` 는 `now` 에 대해 단조라, 개장과 폐장에서 같은 원장이면 그 사이
    어느 순간에도 바뀌지 않았습니다. 그래서 두 끝만 봐도 됩니다.
    """
    bad = []
    for day in _days(year):
        budget = TradingBudget(max_daily_orders=1, timezone_offset_hours=tz_offset_hours)
        opened = budget.roll(datetime.combine(day, OPEN, NY).astimezone(UTC))
        closed = budget.roll(datetime.combine(day, CLOSE, NY).astimezone(UTC))
        if opened is not closed:
            bad.append(day)
    return bad


def test_the_sweep_actually_finds_the_us_toss_configs():
    """찾는 규칙이 틀리면 아래 검사는 아무것도 안 보면서 통과합니다."""
    assert {"us_toss.yaml", "us_toss_desk.yaml"} <= {p.name for p in US_CONFIGS}


def test_the_detector_would_catch_a_kst_boundary():
    """검사기가 진짜 잡는지 — 한국 자정은 여름·겨울 모두 장중입니다."""
    caught = _resets_inside_the_session(9, 2026)
    assert date(2026, 1, 15) in caught and date(2026, 7, 15) in caught


@pytest.mark.parametrize("path", US_CONFIGS, ids=lambda p: p.name)
def test_the_daily_cap_never_resets_inside_the_us_session(path):
    tz = load_config(str(path)).limits.timezone_offset_hours
    bad = _resets_inside_the_session(tz, 2026)
    assert not bad, (
        f"{path.name}: timezone_offset_hours={tz} 는 {len(bad)}일에 장중 초기화 "
        f"— 첫 예: {bad[0]}. 하루 손실 한도로 멈춘 봇이 개장 직후 새 한도를 받습니다.")
