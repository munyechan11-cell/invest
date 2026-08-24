"""첫 화면.

곧바로 이메일 칸부터 내밀면, 무엇에 가입하는지 모른 채로 비밀번호를 만들게
됩니다. 자기 돈을 맡길지 정하는 화면이라 먼저 말해야 할 것이 있습니다 —
무엇을 하는지, 내 증권사 키가 어떻게 다뤄지는지, 그리고 잃을 수 있다는 것.
"""
from __future__ import annotations

import re

from tests.screen import screen

HTML = screen()
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))
CSS = HTML[HTML.find("<style>"):HTML.find("</style>")]


def test_the_login_card_is_not_the_first_thing_on_screen():
    """카드는 접혀 있고, 위를 읽고 나서 엽니다."""
    assert re.search(r"#authCard\{display:none", CSS)
    assert re.search(r"body\.showauth #authCard\{display:block\}", CSS)
    # 그리고 여는 길이 실제로 있어야 합니다.
    for btn in ("ctaJoin", "ctaLogin"):
        assert f'id="{btn}"' in HTML
        assert f'$("#{btn}").onclick' in SCRIPT


def test_the_risk_of_losing_money_is_on_the_first_screen():
    """자동매매는 원금을 전부 잃을 수 있습니다. 그걸 가입 뒤에 말하면 늦습니다."""
    land = HTML[HTML.index('<div class="land">'):HTML.index('id="authCard"')]
    assert "원금" in land and "위험" in land
    assert "보장" in land, "수익 보장이 아니라는 말이 없습니다"
    # 투자 자문이 아니라는 것도 분명히.
    assert "자문" in land


def test_the_first_screen_says_how_credentials_are_handled():
    land = HTML[HTML.index('<div class="land">'):HTML.index('id="authCard"')]
    assert "암호화" in land
    assert "출금" in land, "출금 권한 없이 발급하라는 안내가 없습니다"


def test_the_demo_needs_no_account():
    """"가입해 보면 안다" 는 가입할 이유가 아닙니다."""
    assert 'id="ctaDemo"' in HTML
    assert "function openDemoStage" in SCRIPT
    body = re.search(r"function openDemoStage\(\) \{(.*?)\n\}", SCRIPT, re.S).group(1)
    # 하드코딩된 기록을 재생합니다 — 로그인 전에 서버를 부르면 401 입니다.
    assert "playDeliberation(DEMO" in body
    assert "api(" not in body and "fetch(" not in body


def test_the_demo_moves_the_rooms_instead_of_copying_them():
    """복제하면 좌석 id 가 둘이 되어 getElementById 가 어느 쪽을 집을지 모릅니다."""
    body = re.search(r"function openDemoStage\(\) \{(.*?)\n\}", SCRIPT, re.S).group(1)
    assert "appendChild(rooms)" in body
    assert "cloneNode" not in body
    # 그리고 되돌릴 자리가 있어야 합니다 — 안 그러면 로그인 후 데스크가 빕니다.
    assert 'id="roomsHome"' in HTML
    back = re.search(r"function closeDemoStage\(\) \{(.*?)\n\}", SCRIPT, re.S).group(1)
    assert "roomsHome" in back


def test_logging_in_puts_the_rooms_back():
    """데모를 켠 채로 로그인하면 데스크가 랜딩에 남아 있게 됩니다."""
    body = re.search(r"function signedIn\(user\) \{(.*?)\n\}", SCRIPT, re.S).group(1)
    assert "closeDemoStage()" in body


def test_no_stylesheet_leaked_into_the_script():
    """CSS 를 script 안에 넣으면 첫 줄에서 파싱이 죽고 화면이 통째로 멈춥니다.

    실제로 한 번 그랬습니다 — 삽입 기준으로 쓴 주석이 두 곳에 있었습니다.
    """
    leaked = [ln for ln in SCRIPT.splitlines()
              if re.match(r"^[.#][a-zA-Z][\w.#>: -]*\{", ln)]
    assert not leaked, f"script 안에 CSS 가 있습니다: {leaked[:3]}"


def test_nothing_references_the_removed_token_header():
    """URL 토큰 경로를 걷을 때 `auth` 를 지웠습니다 — 쓰는 곳이 남으면 그 요청이 죽습니다."""
    assert "...auth" not in SCRIPT
    assert "headers: auth" not in SCRIPT
