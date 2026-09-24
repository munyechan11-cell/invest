"""`scripts/desk_live_check.py --provider jev` 가 실측을 **정직하게** 적는가.

이 스크립트는 실키로 한 번 돌려 "연결과 실제 소요 시간" 을 확인하라고 커밋이
권하는 도구입니다. 그런데:

* 비용을 `.3f` 로 적어 Jev(종목당 $0.0005 안팎)는 늘 **$0.000** 이었고, 시작
  점검까지 합친 값을 "LLM 16회" 옆에 적었습니다.
* 좌석이 전부 실패해도 소요 시간과 "실시간 적용 판정 … 1m 이상 권장" 을
  찍고 **0 으로 끝났습니다.** 실패한 좌석은 기다리지 않고 대체값을 내므로 그
  시간은 실제 심의보다 짧고, 그걸로 봉 주기를 권하면 틀린 권고입니다.

네트워크는 나가지 않습니다(가짜 Jev), `.env` 도 읽지 않습니다(`load_env_file`
을 먼저 막고 스크립트를 불러옵니다).
"""
from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
from pathlib import Path

import httpx
import pytest

import quant.live.credentials as credentials
from tests.test_jev import FakeJev, desk_answers, no_backoff, seat_calls  # noqa: F401

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "desk_live_check.py"


def run_script(monkeypatch, answer) -> tuple[int, str, FakeJev]:
    monkeypatch.setattr(credentials, "load_env_file", lambda *a, **k: None)
    for var in ("ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JEV_API_KEY", "offline-test")
    fake = FakeJev(answer)
    original = httpx.AsyncClient.__init__

    def offline(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake)
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", offline)
    spec = importlib.util.spec_from_file_location("desk_live_check_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", ["desk_live_check.py", "--provider", "jev"])
    printed: list[str] = []
    monkeypatch.setattr("builtins.print",
                        lambda *a, **k: printed.append(" ".join(str(x) for x in a)))
    code = asyncio.run(module.run(module.parse_args()))
    return code, "\n".join(printed), fake


def test_a_healthy_run_prints_a_real_cost_and_exits_zero(monkeypatch):
    code, out, fake = run_script(monkeypatch, desk_answers("bullish"))
    assert code == 0, out
    assert len(seat_calls(fake)) == 16
    # 가짜 Jev 는 호출마다 입력 677 토큰: 16 × 677 / 1e6 × $0.042 = $0.00045
    assert "LLM 16회 · 추정 $0.00045 (종목 1개)" in out, out
    assert "시작 점검: LLM 1회" in out
    assert "실시간 적용 판정" in out


def test_failed_seats_are_counted_and_the_run_exits_nonzero(monkeypatch):
    bullish = desk_answers("bullish")

    def answer(name, args):
        if name == "jev_evaluate" and "stance" in args["questions"]:
            return {"answers": {"stance_v2": {}}}          # 서버가 키 이름을 바꿨다
        return bullish(name, args)

    code, out, _ = run_script(monkeypatch, answer)
    assert code != 0, out
    assert re.search(r"좌석 \d+곳 실패", out), out
    assert "질문 'stance' 의 답이 없습니다" in out             # 첫 오류를 보여 준다
    assert "실시간 적용 판정" not in out                       # 틀린 권고를 하지 않는다


# ── 한 좌석만 실패해도 속도 판정을 하지 않는다 ───────────────────────────────
@pytest.mark.parametrize("seat", ["Head of Desk", "Trader", "Research Manager",
                                  "Neutral Risk", "Aggressive Risk", "Bear Researcher"])
def test_a_single_failed_seat_after_the_analysts_still_exits_nonzero(monkeypatch, seat):
    """분석가 말고 뒷좌석 하나만 실패한 경우. 예전 검사는 분석가 실패만 봐서,
    이 자리의 판정을 지워도 모든 테스트가 통과했습니다."""
    bullish = desk_answers("bullish")

    def answer(name, args):
        if name == "jev_evaluate" and args["state"].get("seat") == seat:
            return {"jsonrpc": "2.0", "result": {"isError": True, "content": [
                {"type": "text", "text": "MCP error -32602: Invalid arguments"}]}}
        return bullish(name, args)

    code, out, _ = run_script(monkeypatch, answer)
    assert code == 1, out
    assert re.search(r"좌석 \d+곳 실패", out), out
    assert "-32602" in out
    assert "실시간 적용 판정" not in out


# ── 재시도 대기가 속도 판정에 섞이지 않는다 (4차 점검) ──────────────────────
def test_retries_are_printed_and_qualify_the_speed_verdict(monkeypatch, no_backoff):  # noqa: F811
    """헤드가 503 을 두 번 받고 답했습니다. 좌석 실패는 0 이라 예전에는 백오프
    4.5초가 든 소요 시간으로 "실시간 적용 판정" 을 그대로 찍었습니다."""
    bullish = desk_answers("bullish")
    head: list[int] = []

    def answer(name, args):
        if name == "jev_evaluate" and args["state"].get("seat") == "Head of Desk":
            head.append(1)
            if len(head) <= 2:
                return httpx.Response(503, text="Service Unavailable")
        return bullish(name, args)

    code, out, _ = run_script(monkeypatch, answer)
    assert code == 0, out
    assert "재시도 2회" in out and "Jev 처리 시간 합 8.0초" in out, out
    assert "속도 판정은 참고용" in out
    healthy_code, healthy, _ = run_script(monkeypatch, desk_answers("bullish"))
    assert healthy_code == 0 and "재시도 0회" in healthy
    assert "참고용" not in healthy
