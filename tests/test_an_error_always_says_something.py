"""오류 문장이 **뒤가 잘린 채** 화면에 올라오고 있었습니다.

서버 로그에 이렇게 찍혔습니다:

    ERROR  kis fill channel is down: 주식일별주문체결조회 실패:

`실패: ` 뒤가 공백입니다. 읽는 사람은 원인이 없는 것인지, 우리가 못 적은
것인지 알 수 없습니다.

원인은 단순합니다 — **어떤 예외는 `str()` 이 빈 문자열입니다.**
`httpx.ReadTimeout()` 과 `asyncio.TimeoutError()` 가 그렇고, 하필 그 둘이
네트워크가 흔들릴 때 나오는 예외입니다. 즉 **원인을 제일 알아야 하는 날에만**
문장이 비었습니다.

이번엔 29초 만에 복구돼서 넘어갔습니다. 다음에 진짜로 끊기면 그렇지 않습니다.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from quant.core.types import one_line_error

EMPTY = [httpx.ReadTimeout(""), httpx.ConnectError(""), asyncio.TimeoutError(),
         RuntimeError(""), ValueError()]


@pytest.mark.parametrize("exc", EMPTY)
def test_an_empty_exception_still_names_itself(exc):
    said = one_line_error(exc)
    assert said.strip(), f"{type(exc).__name__} 이 빈 문장을 냅니다"
    assert said == type(exc).__name__


def test_a_real_message_is_untouched():
    assert one_line_error(ValueError("진짜 사유")) == "진짜 사유"


def test_a_url_still_loses_its_query_string():
    """계좌번호가 질의문자열로 들어옵니다 — 그 규칙이 이번 변경으로
    사라지면 안 됩니다."""
    said = one_line_error(RuntimeError(
        "500 for url 'https://openapi.koreainvestment.com/x?CANO=10094558&A=1'"))
    assert "10094558" not in said and "?" not in said


def test_it_is_still_one_line_and_bounded():
    said = one_line_error(RuntimeError("가\n나\n다" + "x" * 500))
    assert "\n" not in said and len(said) <= 141


# ── 실제로 그 자리들이 쓰고 있는가 ──────────────────────────────────────
#
# 같은 모양이 여러 곳에 흩어져 있었습니다. 주문을 막거나 화면에 뜨는
# 자리들만 골라 고쳤습니다 — 거기서 문장이 비면 사람이 할 수 있는 일이
# 없어집니다.

SITES = {
    "quant/brokerage/kis_broker.py": ["주식일별주문체결조회 실패",
                                      "체결을 확인할 수 없는 상태로는"],
    "quant/brokerage/live_base.py": ["주문 직전 안전 상태를 확인하지 못했습니다",
                                     "venue rejected"],
    "quant/brokerage/sleeve.py": ["주문 직전 안전 상태를 확인하지 못했습니다"],
    "quant/brokerage/toss_broker.py": ["토스 취소 후 원주문 상태를"],
    "quant/core/engine.py": ["로컬 미결 주문 조회 실패"],
}


@pytest.mark.parametrize("path,needles", SITES.items())
def test_the_blocking_messages_never_go_blank(path, needles):
    text = Path(path).read_text(encoding="utf-8")
    for needle in needles:
        idx = text.index(needle)
        window = text[idx:idx + 220]
        assert "one_line_error(" in window, (
            f"{path}: '{needle}' 뒤가 빌 수 있습니다 — 예외를 그대로 끼웠습니다")
        assert "{exc}" not in window.split("one_line_error(")[0], (
            f"{path}: '{needle}' 가 아직 맨 예외를 씁니다")
