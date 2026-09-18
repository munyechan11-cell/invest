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


# ── 시작과 첫 심의 사이 ──────────────────────────────────────────────────
#
# ▶ 시작을 누르면 배너는 "시작됨" 이라고 뜨는데, 바로 아래 데스크 칸은
# **"아직 시작하지 않았습니다"** 라고 말하고 있었습니다. 한 화면이 서로 다른
# 두 말을 하면 사람은 켜진 건지 아닌지 알 수 없습니다.
#
# 그 사이는 짧지 않습니다 — 20종목 × 260봉을 내려받고 나서야 첫 심의가
# 시작되고, 심의 자체가 몇 분입니다.

def test_the_desk_says_it_is_starting_not_that_it_never_started():
    assert '"시작 중…"' in SCRIPT
    body = SCRIPT[SCRIPT.index("function deskWaitReason"):][:2000]
    starting = body.index("시작 중…")
    idle = body.index("아직 시작하지 않았습니다")
    assert starting < idle, "아직 준비 중인데 '시작하지 않았다' 가 먼저 걸립니다"


def test_pressing_start_marks_the_moment():
    assert "startingSince = Date.now()" in SCRIPT


def test_it_stops_saying_starting_once_the_desk_speaks():
    watch = SCRIPT[SCRIPT.index("function stillStarting"):][:900]
    assert "last_decision_at" in watch, "데스크가 말을 해도 계속 '시작 중' 입니다"
    assert "startingSince = 0" in watch


def test_starting_does_not_cover_a_real_failure_forever():
    """영원히 '시작 중…' 으로 덮으면, 그 밑에 있는 진짜 이유를 못 읽습니다."""
    watch = SCRIPT[SCRIPT.index("function stillStarting"):][:900]
    assert "STARTING_GRACE_MS" in watch
    assert "STARTING_GRACE_MS = 6 * 60 * 1000" in SCRIPT


def test_a_broken_desk_still_wins_over_starting():
    """키가 없거나 크레딧이 떨어진 것은 기다려도 안 됩니다 — 그 문장이
    '시작 중…' 뒤로 숨으면 안 됩니다."""
    body = SCRIPT[SCRIPT.index("function deskWaitReason"):][:2000]
    assert body.index("데스크가 꺼졌습니다") < body.index("시작 중…")


def test_stopping_clears_the_starting_flag():
    assert "startingSince = 0;" in SCRIPT[SCRIPT.index('runStop").onclick'):][:2500]


# ── 저장된 값을 지울 수 있는가 ──────────────────────────────────────────
#
# 빈 칸은 "그대로 두라" 는 뜻입니다 — 그래야 비밀 칸을 비운 채 폼을 낼 수
# 있으니 맞는 설계인데, 그 결과 **한 번 저장한 값을 지울 방법이 없었습니다.**
# 본인 Gemini 키가 남아 있으면 그게 서비스 키를 이기므로, 잔액이 떨어진 옛
# 키가 계속 쓰이고 새 키는 한 번도 안 불립니다.

def test_an_empty_field_still_means_leave_it_alone():
    assert '빈 칸은 "이미 저장된 것을 그대로 두라"는 뜻입니다' in SERVER


def test_there_is_a_way_to_delete_one_stored_value():
    assert '@app.post("/api/setup/forget/{name}")' in SERVER
    assert "def forget_secret" in SERVER
    assert "drop_secret" in SERVER


def test_the_screen_offers_it_where_the_value_is():
    assert 'data-forget=' in SCRIPT and "지우기" in SCRIPT


def test_deleting_asks_first():
    """되돌릴 수 없습니다."""
    handler = SCRIPT[SCRIPT.index('data-forget'):][:1200]
    assert "confirm(" in handler and "되돌릴 수 없습니다" in handler


def test_deleting_says_when_there_was_nothing_to_delete():
    """'지웠습니다' 만 뜨면, 애초에 없던 것과 지운 것이 구별되지 않습니다."""
    handler = SCRIPT[SCRIPT.index('data-forget'):][:1400]
    assert "저장돼 있지 않았습니다" in handler
