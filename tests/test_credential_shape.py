"""넣은 값이 실제로 저장됐는가.

"키를 넣었는데 403" 일 때 가장 먼저 확인해야 하는 것은 키가 맞는지가 아니라
**내가 넣은 값이 저장돼 있는가** 입니다. 브라우저 자동완성이 로그인 비밀번호를
채우고 그것이 진짜 키를 덮어쓰는 일이 실제로 일어나고, 그때 거래소는 그냥
"거부" 라고만 답합니다.

유효성은 불러 봐야 압니다. 하지만 모양은 부르기 전에 알 수 있고, 잘못
저장된 값은 대부분 모양에서 드러납니다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from quant.api.server import _shape_problem

HTML = Path("quant/api/static/index.html").read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))


@pytest.mark.parametrize("env,value", [
    ("TOSS_CLIENT_ID", "tsck_live_" + "a" * 22),
    ("TOSS_CLIENT_SECRET", "tssk_live_" + "b" * 30),
    ("KIS_APP_KEY", "P" * 36),
    ("KIS_APP_SECRET", "S" * 180),
])
def test_a_well_formed_key_passes(env, value):
    assert _shape_problem(env, value) == ""


def test_an_autofilled_password_is_caught():
    """이게 이 검사가 존재하는 이유입니다."""
    problem = _shape_problem("TOSS_CLIENT_ID", "MyLoginPassword123!")
    assert problem and "tsck_" in problem
    assert "자동완성" in problem, "무엇을 의심해야 하는지 말해야 합니다"


def test_a_pasted_value_with_whitespace_is_caught():
    assert "공백" in _shape_problem("TOSS_CLIENT_ID", " tsck_live_" + "a" * 22)
    assert "공백" in _shape_problem("TOSS_CLIENT_SECRET", "tssk_live_aaa bbb" + "c" * 20)


def test_a_truncated_value_is_caught():
    assert "짧" in _shape_problem("KIS_APP_SECRET", "S" * 20)


def test_an_unknown_field_is_left_alone():
    """모양을 모르는 값에 대해 추측하지 않습니다."""
    assert _shape_problem("SOMETHING_ELSE", "whatever") == ""
    assert _shape_problem("TOSS_CLIENT_ID", "") == ""


def test_the_browser_blocks_autofill_on_credential_fields():
    """크롬은 type=password 에 autocomplete="off" 를 자주 무시합니다."""
    row = re.search(r"function fieldRow\(env, label, hint\) \{(.*?)\n\}", SCRIPT, re.S).group(1)
    assert 'autocomplete="new-password"' in row
    # 비밀번호 관리자들도 각자 막아야 합니다.
    assert "data-lpignore" in row and "data-1p-ignore" in row


def test_the_screen_checks_shape_before_saving():
    """검증 버튼은 화면 값을 먼저 저장합니다 — 그때 진짜 키가 덮어써집니다."""
    assert "function shapeProblem" in SCRIPT
    assert "KEY_PREFIX" in SCRIPT
    # 저장 버튼과 검증 버튼 양쪽에서 걸러야 합니다.
    assert SCRIPT.count("shapeProblem(") >= 2


def test_the_inspector_never_returns_the_value():
    """점검은 모양만 봅니다. 값이 나가면 그건 점검이 아니라 유출입니다."""
    server = Path("quant/api/server.py").read_text(encoding="utf-8")
    body = re.search(r'@app\.get\("/api/setup/inspect"\).*?@app\.post', server, re.S).group(0)
    assert '"length"' in body and '"starts_with"' in body
    # 앞 5자·뒤 4자 말고 전체를 내보내는 줄이 없어야 합니다.
    assert '"value"' not in body
    assert "value[:5]" in body
