""""정지했습니다" 라고 써 놓고 봇은 계속 돌고 있었습니다.

    03:11:36  POST /api/trader/stop   200 OK
    03:11:46  POST /api/trader/start  409 Conflict   ← 10초 뒤, 아직 돌고 있음

정지는 **이번 사이클을 끝내고** 멈춥니다. 서버는 그 사실을 정직하게
돌려줍니다 — `{"stopping": true, "stopped": false}`. 그런데 화면이 그 값을
읽지도 않고 "정지했습니다" 라고 썼습니다.

그래서 사람은 멈춘 줄 알고 ▶ 시작을 누르고, 서버는 "이미 돌고 있습니다" 로
답합니다. 화면에서 그건 **"정지가 안 된다"** 로 읽힙니다. AI 데스크 한
사이클은 몇 분이 걸릴 수 있어서, 그 몇 분이 통째로 고장처럼 보입니다.
"""
from __future__ import annotations

import re
from pathlib import Path

HTML = Path("quant/api/static/index.html").read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))
SERVER = Path("quant/api/server.py").read_text(encoding="utf-8")
REGISTRY = Path("quant/webapp/registry.py").read_text(encoding="utf-8")


def stop_handler() -> str:
    start = SCRIPT.index('document.getElementById("runStop").onclick')
    return SCRIPT[start:start + 2400]


# ── 서버는 원래 정직했습니다 ────────────────────────────────────────────
def test_the_server_reports_whether_it_actually_stopped():
    assert '"stopping": True, "stopped": stopped' in REGISTRY


def test_the_server_waits_only_briefly_and_says_so():
    """HTTP 요청을 몇 분씩 붙잡는 것이 정답은 아닙니다 — 대신 아직
    멈추지 않았다는 사실을 돌려줍니다."""
    assert "STOP_GRACE_SECONDS = 20.0" in REGISTRY
    assert "멈추지 않았습니다" in REGISTRY


# ── 화면이 그것을 읽는가 ────────────────────────────────────────────────
def test_the_screen_reads_the_stopped_flag():
    assert "res.stopped === false" in stop_handler(), (
        "응답을 읽지 않고 정지했다고 씁니다")


def test_a_pending_stop_is_not_announced_as_done():
    body = stop_handler()
    done = body.index('"정지했습니다 — 포지션은 그대로입니다"')
    pending = body.index('"정지 중…')
    assert pending < done, "아직 도는 중인데 먼저 '정지했습니다' 라고 씁니다"


def test_it_says_the_wait_can_be_minutes():
    """몇 초를 기다리다 포기하는 사람이 ▶ 시작을 눌러 409 를 봅니다."""
    assert "몇 분 걸릴 수 있습니다" in stop_handler()


def test_positions_are_promised_in_both_messages():
    """멈춘다는 말이 '청산한다' 로 읽히면 안 됩니다."""
    body = stop_handler()
    assert body.count("포지션은 그대로") >= 2


# ── 멈출 때까지 지켜봅니다 ──────────────────────────────────────────────
def test_the_screen_watches_until_it_really_stops():
    assert "function waitForStop" in SCRIPT
    assert "waitForStop()" in stop_handler()


def test_the_watch_gives_up_out_loud_rather_than_hanging():
    """영영 '정지 중…' 이면 끝난 것인지 걸린 것인지 알 수 없습니다."""
    watch = SCRIPT[SCRIPT.index("async function waitForStop"):][:1400]
    assert "아직 멈추지 않았습니다" in watch
    assert "tries <= 0" in watch


def test_a_failed_poll_does_not_count_as_stopped():
    """조회 한 번 실패를 '멈췄다' 로 바꾸면, 돌고 있는 봇을 멈춘 것으로
    그립니다 — 이 화면이 반복해서 고쳐 온 실수입니다."""
    watch = SCRIPT[SCRIPT.index("async function waitForStop"):][:1400]
    catch = watch[watch.index("catch (e)"):]
    assert "setRunning(false)" not in catch[:300]


# ── 무엇을 보고 판정하는가 ──────────────────────────────────────────────
def test_the_running_flag_belongs_to_the_signed_in_user():
    """`trader_running` 은 **로그인한 사람의 봇** 입니다. 로그인하지 않은
    호출에는 언제나 false 라서, 그 값을 '아무도 안 돌고 있다' 로 읽으면
    남의 봇을 돌고 있는 채로 끄게 됩니다."""
    assert "running = bool(seat is not None and seat.running())" in SERVER
    assert "자기 봇만 봅니다" in SERVER
