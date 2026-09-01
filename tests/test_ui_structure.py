"""대시보드 화면의 구조 — 눈으로 보고 넘어가면 조용히 무너지는 것들.

여기서 검사하는 것은 스타일이 아니라 약속입니다.

* 사용자가 정해지기 전에는 대시보드가 존재하지 않는다.
* 세션 토큰은 자바스크립트가 만지지 않는다 (HttpOnly 쿠키).
* 초기 설정은 **증권사 연동 → 하루 한도 → 운영자 → 투자 성향** 순서다.
  성향 진단이 첫 화면이던 시절로 돌아가면 이 파일이 먼저 깨집니다.
* 저장한 자격증명은 어떤 경로로도 화면에 다시 그려지지 않는다.

문자열 몇 개를 세는 검사가 아니라 문서 순서와 코드 경로를 봅니다.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "quant" / "api" / "static" / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    # 스타일이 app.css 로 갈라졌습니다. 화면을 검사하는 쪽에서는 여전히
    # 한 화면이라 합쳐서 봅니다.
    from tests.screen import screen

    return screen()


@pytest.fixture(scope="module")
def script(html: str) -> str:
    """페이지가 실제로 실행하는 자바스크립트만."""
    body = re.search(r"<script>\n(.*?)</script>", html, re.S)
    assert body, "페이지에 스크립트가 없습니다"
    return body.group(1)


@pytest.fixture(scope="module")
def markup(html: str) -> str:
    """<style> 를 뺀 마크업 — CSS 안의 셀렉터가 검사에 섞이지 않도록."""
    return re.sub(r"<style>.*?</style>", "", html, flags=re.S)


class Tags(HTMLParser):
    """(태그, 속성dict) 를 문서 순서대로 모읍니다."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {k: (v or "") for k, v in attrs}))

    handle_startendtag = handle_starttag


@pytest.fixture(scope="module")
def setup_sheet(markup: str) -> str:
    """초기 설정 오버레이만 — 마이페이지에도 '투자 성향' 이 있기 때문입니다."""
    # Dialog semantics may add attributes after the stable id.  Locating the
    # element by that identity keeps this fixture focused on setup content.
    match = re.search(r'<div\s+class="overlay"\s+id="setup"(?:\s[^>]*)?>', markup)
    assert match, "초기 설정 overlay가 없습니다"
    start = match.start()
    return markup[start:markup.index('<div class="tape', start)]


@pytest.fixture(scope="module")
def tags(html: str) -> list[tuple[str, dict[str, str]]]:
    parser = Tags()
    parser.feed(html)
    return parser.tags


def _by_id(tags, tag_id):
    return [(t, a) for t, a in tags if a.get("id") == tag_id]


def _ids_in_order(tags, wanted):
    """문서에 나타난 순서대로, 관심 있는 id 만."""
    return [a["id"] for _, a in tags if a.get("id") in wanted]


# ── 로그인 · 회원가입 ────────────────────────────────────────────────────
def test_both_auth_forms_exist(tags):
    assert _by_id(tags, "loginForm"), "로그인 폼이 없습니다"
    assert _by_id(tags, "registerForm"), "회원가입 폼이 없습니다"


def test_login_form_asks_for_email_and_password(tags):
    fields = {a.get("name"): a for _, a in tags
              if a.get("id", "").startswith("li") and a.get("name")}
    assert set(fields) == {"email", "password"}, fields
    assert fields["email"]["type"] == "email"
    assert fields["password"]["type"] == "password"


def test_register_form_asks_for_email_password_and_name(tags):
    fields = {a.get("name"): a for _, a in tags
              if a.get("id", "").startswith("rg") and a.get("name")}
    assert set(fields) == {"email", "password", "display_name"}, fields
    assert fields["password"]["type"] == "password"
    # 새 비밀번호 칸에 저장된 비밀번호가 자동으로 채워지면 안 됩니다.
    assert fields["password"]["autocomplete"] == "new-password"


def test_gate_uses_the_four_auth_endpoints(script):
    for path in ("/api/auth/register", "/api/auth/login",
                 "/api/auth/logout", "/api/auth/me"):
        assert path in script, f"{path} 를 부르지 않습니다"


def test_no_session_token_is_ever_held_by_javascript(script):
    """세션은 HttpOnly 쿠키입니다 — 자바스크립트가 보관하면 그 전제가 깨집니다."""
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "document.cookie" not in script


def test_every_request_sends_the_session_cookie(script):
    """fetch 는 기본값이 바뀔 수 있습니다. 전부 명시적으로 same-origin."""
    calls = script.count("fetch(")
    explicit = script.count('credentials: "same-origin"')
    assert calls == explicit, f"fetch {calls}건 중 {explicit}건만 쿠키를 보냅니다"


# ── 로그인 전에는 아무것도 없다 ──────────────────────────────────────────
def test_page_starts_in_the_checking_state(html):
    assert re.search(r'<body class="booting">', html)


def test_dashboard_chrome_is_hidden_until_a_user_exists(html):
    """스크립트가 숨기는 게 아니라 기본값이 숨김이어야 합니다.

    스크립트로 숨기면 느린 /api/auth/me 나 자바스크립트 오류 하나가
    남의 트레이딩 플로어를 잠깐 비추게 됩니다.
    """
    assert re.search(r"\.app-only\{display:none\}", html)
    assert re.search(r"body\.authed \.app-only\{display:block\}", html)
    # 설정 마법사 같은 오버레이도 마찬가지입니다.
    assert re.search(r"body:not\(\.authed\) \.overlay\{display:none\}", html)


@pytest.mark.parametrize("selector", [
    r'<header class="top app-only">',
    r'<main class="wrap app-only"\s+id="mainContent"',
    r'<footer class="app-only">',
    r'<div class="tape app-only">',
])
def test_every_dashboard_region_is_gated(html, selector):
    assert re.search(selector, html), f"{selector} 가 로그인 게이트 밖에 있습니다"


def test_dashboard_has_a_named_main_region_and_skip_link(tags):
    main = _by_id(tags, "mainContent")
    assert main == [("main", {
        "class": "wrap app-only",
        "id": "mainContent",
        "aria-labelledby": "dashboardTitle",
        "tabindex": "-1",
    })]

    heading = _by_id(tags, "dashboardTitle")
    assert heading == [("h1", {"class": "sr-only", "id": "dashboardTitle"})]

    skip_links = [
        attrs for tag, attrs in tags
        if tag == "a" and attrs.get("href") == "#mainContent"
    ]
    assert len(skip_links) == 1
    assert {"skip-link", "app-only"}.issubset(
        set(skip_links[0].get("class", "").split())
    )


def test_nothing_is_fetched_before_the_user_is_known(script):
    """부팅은 whoami 하나로 시작하고, 나머지는 start() 안에 있어야 합니다."""
    boot = script[script.index("function start()"):]
    for call in ("refresh()", "connect()", "refreshManual()", "loadSetup()"):
        assert call in boot, f"{call} 이 start() 밖에서 불립니다"
    # 스크립트 맨 끝에서 곧바로 대시보드를 켜던 옛 부팅 코드가 남아 있으면 안 됩니다.
    tail = script[script.index("(async () => {\n  const user = await whoami();"):]
    assert "refresh(); connect();" not in tail


# ── 초기 설정 순서 ───────────────────────────────────────────────────────
REQUIRED_STEP_ORDER = ["stepVenues", "stepLimits", "stepOperator", "stepProfile"]


def test_setup_steps_are_in_the_order_the_owner_asked_for(tags):
    """1) 증권사·거래소 2) 하루 한도 3) 운영자 4) 투자 성향."""
    assert _ids_in_order(tags, set(REQUIRED_STEP_ORDER)) == REQUIRED_STEP_ORDER


def test_setup_step_headings_are_numbered_to_match(setup_sheet):
    """설정 시트 안의 제목만 봅니다.

    전체 문서를 보면 첫 화면(랜딩)의 소제목이 먼저 잡힙니다 — 그건 설정
    단계가 아니라 서비스 소개라서, 번호를 요구할 대상이 아닙니다.
    """
    headings = re.findall(r"<h3>(.*?)</h3>", setup_sheet, re.S)
    numbered = [re.sub(r"\s+", " ", h).strip() for h in headings]
    assert numbered[0].startswith("1 · 증권사 · 거래소 연동")
    assert numbered[1].startswith("2 · 하루 거래 한도")
    assert numbered[2].startswith("3 · 운영자 정보")
    assert numbered[3].startswith("4 · 투자 성향")


def test_the_venue_step_comes_before_the_quiz_in_the_setup_sheet(setup_sheet):
    """봇을 살리는 단계가 성향 진단보다 먼저 나와야 합니다."""
    assert setup_sheet.index("증권사 · 거래소 연동") < setup_sheet.index("투자 성향")


def test_the_quiz_can_be_skipped(tags, setup_sheet, script):
    assert _by_id(tags, "quizSkip"), "성향 진단을 건너뛸 방법이 없습니다"
    assert re.search(r'id="quizSkip">건너뛰기', setup_sheet)
    assert "profileSkipped = true" in script
    # 건너뛴 사람은 중립 프로필로 돕니다 — 새 진단값을 만들어 저장하지 않습니다.
    handler = script[script.index('$("#quizSkip").onclick'):]
    handler = handler[:handler.index("\n};")]
    assert "post(" not in handler, "건너뛰기가 서버에 성향을 저장합니다"


def test_a_skipper_can_take_the_quiz_later_from_my_page(markup, script):
    assert 'id="pRetake"' in markup
    retake = script[script.index('$("#pRetake").onclick'):]
    retake = retake[:retake.index("\n};")]
    assert "profileSkipped = false" in retake
    assert "openSetup()" in retake


def test_setup_shows_which_steps_remain(tags, script):
    assert _by_id(tags, "setupSteps"), "남은 단계를 보여주는 자리가 없습니다"
    assert "남은 필수 단계" in script
    # 단계마다 완료/남음 표시가 붙어 있어야 합니다.
    marks = ["markVenues", "markLimits", "markOperator", "markProfile"]
    assert _ids_in_order(tags, set(marks)) == marks
    for mark in marks:
        assert f'mark("#{mark}"' in script


# ── 마이페이지 ───────────────────────────────────────────────────────────
def test_my_page_has_logout_password_change_and_unlink(tags, script):
    assert _by_id(tags, "logoutBtn"), "로그아웃 버튼이 없습니다"
    assert _by_id(tags, "passwordForm"), "비밀번호 변경 폼이 없습니다"
    assert _by_id(tags, "venueLinks"), "연동 목록이 없습니다"
    assert "data-unlink" in script, "연동 해제 버튼이 없습니다"


def test_password_change_form_confirms_the_new_password(tags, script):
    ids = {a["id"] for _, a in tags if a.get("id", "").startswith("pw")}
    assert {"pwCurrent", "pwNew", "pwConfirm"} <= ids
    assert 'next !== $("#pwConfirm").value' in script


def test_password_change_returns_to_the_gate(script):
    """서버가 모든 세션을 끊습니다. 화면도 그 사실대로 굴어야 합니다."""
    handler = script[script.index('$("#passwordForm").addEventListener'):]
    handler = handler[:handler.index("\n});")]
    assert "/api/auth/password" in handler
    assert "signedOut()" in handler
    # 입력한 비밀번호는 성공 즉시 지웁니다.
    assert '$(s).value = ""' in handler


def test_unlink_is_two_step_and_deletes_the_stored_key(script):
    handler = script[script.index("async function unlink(btn)"):]
    handler = handler[:handler.index("\n}\n")]
    assert 'btn.dataset.armed !== "1"' in handler, "한 번 눌러 바로 지워집니다"
    assert "/api/setup/disconnect/" in handler


def test_signing_out_stops_the_polling(script):
    handler = script[script.index("function signedOut()"):]
    handler = handler[:handler.index("\n}\n")]
    assert "beginIdentityTransition" in handler
    runtime = script[script.index("function stopIdentityRuntime()") :]
    runtime = runtime[: runtime.index("\n}\n")]
    assert "clearInterval" in runtime
    assert "s.close()" in runtime, "웹소켓이 로그아웃 뒤에도 살아 있습니다"


# ── 자격증명은 화면으로 돌아오지 않는다 ─────────────────────────────────
def test_no_input_in_the_page_carries_a_value(tags):
    """정적 마크업에도, 템플릿에도 value 는 없습니다."""
    for tag, attrs in tags:
        if tag == "input":
            assert "value" not in attrs, f"{attrs.get('id') or attrs} 에 value 가 있습니다"


def test_credential_field_template_never_writes_a_value(script):
    row = script[script.index("function fieldRow("):]
    row = row[:row.index("\n}\n")]
    assert "<input" in row
    assert "value=" not in row, "저장된 비밀을 입력칸에 되돌려 놓습니다"
    assert "placeholder=" in row


def test_only_the_last_four_characters_can_ever_be_drawn(script):
    """서버가 힌트만 준다고 믿지 않습니다 — 화면에서 한 번 더 자릅니다."""
    hint = script[script.index("function keyHint("):]
    hint = hint[:hint.index("\n}\n")]
    assert ".slice(-4)" in hint
    # 저장된 값을 읽는 경로가 이 함수 하나여야 합니다. 다른 곳에서 한 번만
    # 더 읽으면 자르는 규칙을 통째로 우회하게 됩니다.
    assert script.count("configuredKeys[") == hint.count("configuredKeys[") == 1


def test_saved_inputs_are_cleared_after_the_save(script):
    handler = script[script.index('$("#setupSave").onclick'):]
    handler = handler[:handler.index("\n};")]
    assert '(el.value = "")' in handler, "입력한 키가 화면에 그대로 남습니다"
    assert handler.index("post(") < handler.index('(el.value = "")')


def test_secrets_are_typed_as_password_fields(script):
    row = script[script.index("function fieldRow("):]
    row = row[:row.index("\n}\n")]
    assert 'isSecret ? "password" : "text"' in row
    assert re.search(r"/key\|secret\|token\|password/i", row)


def test_the_page_never_prints_a_secret_into_a_message(script):
    """저장 결과 메시지는 건수만 말합니다."""
    handler = script[script.index('$("#setupSave").onclick'):]
    handler = handler[:handler.index("\n};")]
    assert "values[" not in handler.split("post(")[1]


# ── 비정상 종료 실거래 실행 복구 ────────────────────────────────────────
def test_reconciliation_card_is_hidden_and_named_by_default(tags):
    card = _by_id(tags, "reconciliationCard")
    assert card == [("section", {
        "class": "panel recovery-card",
        "id": "reconciliationCard",
        "hidden": "",
        "aria-labelledby": "reconciliationTitle",
    })]
    assert _by_id(tags, "reconciliationTitle")


def test_reconciliation_requires_five_explicit_toss_checks(markup, tags):
    checks = [
        "reconciliationOpenOrders",
        "reconciliationTodayFills",
        "reconciliationHoldings",
        "reconciliationCash",
        "reconciliationDailyLoss",
    ]
    assert _by_id(tags, "reconciliationChecks") == [(
        "fieldset", {"class": "recovery-checks", "id": "reconciliationChecks"}
    )]
    assert re.search(
        r'<fieldset class="recovery-checks" id="reconciliationChecks">\s*'
        r'<legend>[^<]*5개</legend>', markup,
    )
    for field_id in checks:
        assert _by_id(tags, field_id) == [(
            "input", {"type": "checkbox", "id": field_id}
        )]
        assert re.search(
            rf"<label>\s*<input type=\"checkbox\" id=\"{field_id}\">",
            markup,
        ), f"{field_id} 에 접근 가능한 label이 없습니다"


def test_reconciliation_reason_and_acknowledgement_are_labeled(tags):
    reason = _by_id(tags, "reconciliationReason")
    assert reason == [("textarea", {
        "id": "reconciliationReason",
        "minlength": "10",
        "maxlength": "500",
        "rows": "4",
        "required": "",
        "aria-describedby": "reconciliationReasonHelp",
    })]
    acknowledgement = _by_id(tags, "reconciliationAck")
    assert acknowledgement == [("input", {
        "id": "reconciliationAck",
        "type": "text",
        "autocomplete": "off",
        "spellcheck": "false",
        "aria-describedby": "reconciliationAckPhrase",
        "required": "",
    })]
    labels = {attrs.get("for") for tag, attrs in tags if tag == "label"}
    assert {"reconciliationReason", "reconciliationAck"} <= labels


def test_reconciliation_result_is_announced_and_cannot_submit_on_load(tags):
    message = _by_id(tags, "reconciliationMsg")
    assert message == [("div", {
        "class": "msg recovery-message",
        "id": "reconciliationMsg",
        "role": "status",
        "aria-live": "polite",
        "tabindex": "-1",
    })]
    submit = _by_id(tags, "reconciliationSubmit")
    assert submit == [("button", {
        "class": "btn tap warn",
        "type": "button",
        "id": "reconciliationSubmit",
        "disabled": "",
    })]


def test_reconciliation_card_precedes_the_live_account_balance(markup):
    """위험 상태는 잔고 숫자보다 먼저 보여야 운영자가 놓치지 않습니다."""
    assert markup.index('id="reconciliationCard"') < markup.index('id="brokerAcct"')


def test_reconciliation_card_has_an_explicit_mobile_collapse(html):
    assert re.search(
        r"@media\(max-width:520px\)\{.*?\.recovery-meta"
        r"\{grid-template-columns:minmax\(0,1fr\)\}", html, re.S,
    )
    assert re.search(
        r"@media\(max-width:520px\)\{.*?\.recovery-actions \.btn"
        r"\{width:100%;min-width:0\}", html, re.S,
    )


# ── 휴대폰 ───────────────────────────────────────────────────────────────
def test_viewport_meta_is_intact(tags):
    metas = [a for t, a in tags if t == "meta" and a.get("name") == "viewport"]
    assert metas and "width=device-width" in metas[0]["content"]


def test_new_controls_are_finger_sized(html):
    """44px 은 손가락 하나의 크기입니다. 그보다 작으면 옆 칸이 눌립니다.

    선택자의 **모양**이 아니라 그 칸이 규칙에 걸리는지를 봅니다. 처음에는
    `.pwform .fld input` 처럼 좁게 짚었는데, 그 래퍼 밖에 있던 추천 코드
    칸이 19px 로 남아 있었습니다 — 좁은 선택자를 검사하면 그 선택자가
    닿지 않는 칸을 놓칩니다.
    """
    assert re.search(r"\.btn\.tap\{min-height:44px", html)
    assert re.search(r"\.tabs \.tab\{[^}]*min-height:44px", html)
    assert re.search(r"\.fld input[^{]*\{[^}]*min-height:44px", html), \
        "입력칸 전반에 걸리는 44px 규칙이 없습니다"
    assert re.search(r"\.pwform input\{[^}]*min-height:44px", html), \
        ".pwform 안의 칸이 .fld 래퍼 밖에 있어도 44px 이어야 합니다"
    assert re.search(r"\.venue \.head\{min-height:44px\}", html)
    assert re.search(r"\.actions \.btn\{min-width:44px\}", html), \
        "수동 매수/매도 버튼이 43px 이었습니다 — 폭도 손가락 크기여야 합니다"


def test_every_gate_and_account_button_is_a_tap_target(tags):
    """새 화면의 버튼은 전부 44px 짜리 .tap 이어야 합니다."""
    new_buttons = {"loginSubmit", "registerSubmit", "logoutBtn", "acctSetupBtn",
                   "pwSubmit", "quizSkip", "setupSave", "setupClose"}
    seen = set()
    for tag, attrs in tags:
        if tag == "button" and attrs.get("id") in new_buttons:
            seen.add(attrs["id"])
            assert "tap" in attrs.get("class", "").split(), attrs
    assert seen == new_buttons, new_buttons - seen


def test_the_narrow_layout_still_exists(html):
    assert "@media(max-width:760px)" in html


def test_wide_new_blocks_wrap_instead_of_scrolling(html):
    """가로 스크롤이 생기면 휴대폰에서 버튼이 화면 밖으로 나갑니다."""
    for rule in (r"\.steps\{[^}]*flex-wrap:wrap", r"\.link\{[^}]*flex-wrap:wrap"):
        assert re.search(rule, html), rule
    assert re.search(r"\.pwform\{[^}]*minmax\(180px,1fr\)", html)


def test_the_strategy_list_waits_for_the_credentials(script):
    """연동 정보를 받기 전에 목록을 그리면 전부 "연동 필요" 로 남습니다.

    `configuredKeys` 가 아직 비어 있으면 어떤 전략도 필요한 키를 갖췄다고
    판정되지 않습니다. 실제로 연동을 마친 사람이 자기 전략을 고를 수 없게
    되고, 화면은 그 이유를 말해 주지 않습니다.
    """
    import re
    body = re.search(r"function start\(\) \{(.*?)\n\}", script, re.S).group(1)
    assert "loadStrategies" in body
    # loadSetup 안에서 불려야 합니다 — 그 전에 부르면 빈 목록을 보고 그립니다.
    assert body.index("loadSetup") < body.index("loadStrategies"), \
        "연동 정보보다 먼저 전략 목록을 그립니다"


def test_initial_strategy_selection_refreshes_its_own_universe(script):
    import re
    body = re.search(r"async function loadStrategies\(\) \{(.*?)\n\}",
                     script, re.S).group(1)
    assert "await refreshSymbols(true)" in body
    assert "return epoch === authEpoch" in body


def test_setup_save_stops_if_the_authenticated_identity_changes(script):
    import re
    body = re.search(
        r'\$\("#setupSave"\)\.onclick = async \(\) => \{(.*?)\n\};',
        script, re.S,
    ).group(1)
    assert "const epoch = authEpoch" in body
    assert "const identity = authIdentity(me)" in body
    assert body.count("stillCurrent()") >= 6
    assert "isStaleAuthResponse(e) || !stillCurrent()" in body


def test_startup_setup_callback_is_bound_to_the_signed_in_identity(script):
    import re
    body = re.search(r"function start\(\) \{(.*?)\n\}", script, re.S).group(1)
    assert "const epoch = authEpoch" in body
    assert "const identity = authIdentity(me)" in body
    assert body.count("authActionIsCurrent(epoch, identity)") >= 2


def test_saving_credentials_refreshes_the_strategy_list(script):
    """방금 넣은 키로 쓸 수 있게 된 전략이 계속 "연동 필요" 면 같은 고장입니다."""
    import re
    body = re.search(r'\$\("#setupSave"\)\.onclick = async \(\) => \{(.*?)\n\};',
                     script, re.S).group(1)
    assert "loadStrategies()" in body
