"""봇이 죽으면 화면이 그렇게 말하는가.

가장 조용한 종류의 고장이었습니다. 시작 버튼을 누르면 "시작됨" 이 뜨고,
봇은 몇 초 뒤 워밍업에서 시세를 못 받아 죽고, 화면은 아무 말 없이 "오프라인"
으로 돌아갑니다. 사용자는 봇이 돌고 있다고 믿은 채로 기다립니다 —
자동매매에서 그건 "아무 일도 안 하는데 하고 있다고 믿는" 상태입니다.
"""
from __future__ import annotations

import re
from pathlib import Path

SERVER = Path("quant/api/server.py").read_text(encoding="utf-8")
REGISTRY = Path("quant/webapp/registry.py").read_text(encoding="utf-8")
TRADER = Path("quant/live/trader.py").read_text(encoding="utf-8")
HTML = Path("quant/api/static/index.html").read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))


def test_health_carries_the_reason_a_bot_died():
    body = re.search(r'@app\.get\("/api/health"\).*?@app\.get', SERVER, re.S).group(0)
    assert "last_error" in body
    # 돌고 있을 때는 실패 사유를 보낼 이유가 없습니다.
    assert "not running" in body or "not running" in body or "if seat is not None and not running" in body


def test_the_screen_says_it_out_loud():
    assert "봇이 멈췄습니다" in SCRIPT
    # 폴링마다 같은 문구를 다시 띄우면 읽히지 않습니다.
    assert "lastBotError" in SCRIPT


def test_the_warmup_failure_speaks_korean():
    """"no symbol produced usable warm-up data" 로는 무엇을 해야 하는지 알 수 없습니다."""
    assert "no symbol produced usable warm-up data" not in TRADER
    assert "시세를 받지 못해 시작할 수 없습니다" in TRADER
    # 무엇을 확인해야 하는지도 말해야 합니다.
    assert "증권사 키" in TRADER and "장 시간" in TRADER


def test_a_korean_message_is_not_prefixed_with_the_exception_name():
    """"RuntimeError: 시세를…" 는 앞 여덟 글자가 읽는 사람을 멈추게 합니다."""
    assert "speaks_korean" in REGISTRY
    body = re.search(r"def _finished.*?self\._note\(uid, \"bot_failed\"", REGISTRY, re.S).group(0)
    assert "text if speaks_korean else" in body


def test_starting_a_desk_strategy_without_a_key_is_not_a_server_error():
    """500 "서버 오류 — 로그를 확인하세요" 는 사용자가 할 수 있는 게 없습니다."""
    # 추상 선언(raise NotImplementedError)이 아니라 **구현**을 봐야 합니다.
    bodies = re.findall(r"async def start\(self, req: StartRequest\).*?async def stop",
                        SERVER, re.S)
    body = max(bodies, key=len)
    assert "LLMError" in body
    assert "AI 데스크를 쓰는 전략인데" in body
    assert "503" in body


def test_nan_never_reaches_the_wire():
    """JSON 에 NaN 은 없습니다. 인코더가 만나면 응답 전체가 500 이 됩니다."""
    assert "class SafeJSONResponse" in SERVER
    assert "default_response_class=SafeJSONResponse" in SERVER
    # 웹소켓도 같은 길을 지나야 합니다 — 브라우저 JSON.parse 는 NaN 에서 죽습니다.
    assert SERVER.count("finite(") >= 3


def test_a_failed_get_shows_what_the_server_said():
    """"404 /api/candles" 는 사용자가 고칠 수 있는 것이 아닙니다."""
    body = re.search(r"async function api\(path\) \{(.*?)\n\}", SCRIPT, re.S).group(1)
    assert "body.detail || body.error" in body
