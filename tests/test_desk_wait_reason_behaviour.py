"""화면이 **어떤 상황에서 무슨 말을 하는가.**

앞서 쓴 검사들은 `index.html` 안에 특정 식별자가 들어 있는지만 봤습니다.
그건 구현을 베낀 것이라, 함수 첫 줄에 `return {kind:"waiting", ...}` 하나만
넣어도 — 즉 고친 것을 통째로 되돌려도 — 리터럴이 아래 죽은 코드에 남아 있는
한 전부 통과합니다. 적대적 검증이 실제로 그렇게 재현해 보였습니다.

그래서 여기서는 함수를 **실행**합니다. 자바스크립트 엔진에 `deskWaitReason` 을
넣고 서버 응답을 그대로 흉내 낸 입력을 준 뒤, 나오는 문구를 봅니다. 엔진이
없는 환경에서는 건너뜁니다 — 그때는 앞 파일의 정적 검사가 최소한을 지킵니다.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

HTML = Path("quant/api/static/index.html").read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))

_ENGINES = [
    (shutil.which("node"), []),
    ("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc", []),
]


def _engine():
    for path, args in _ENGINES:
        if path and Path(path).exists():
            return path, args
    return None


def _fn(name: str) -> str:
    m = re.search(rf"\nfunction {name}\([^)]*\) \{{.*?\n\}}", SCRIPT, re.S)
    assert m, f"{name} 을 찾지 못했습니다"
    return m.group(0)


#: 화면 함수가 기대는 것 중 이 테스트에 필요한 최소한. 진짜 DOM 은 없습니다 —
#: 우리가 보는 것은 `deskWaitReason` 이 **무엇을 돌려주는가** 이고, 그리는 쪽은
#: 별개입니다.
STUB = """
var document = { body: { classList: {
  contains: function (c) { return RUNNING && c === 'running'; } } } };
"""


def _run(cases: list[dict]) -> list[dict]:
    """각 상황에서 `deskWaitReason()` 이 무엇을 돌려주는지."""
    engine = _engine()
    assert engine, "엔진 없음"
    path, args = engine
    src = "\n".join([
        _fn("deskWaitReason"),
        _fn("shownStrategy"),
        "var out = [];",
        "for (var i = 0; i < CASES.length; i++) {",
        "  var c = CASES[i];",
        "  strategies = c.strategies; deskState = c.deskState;",
        "  botState = c.botState; runningStrategy = c.runningStrategy || '';",
        "  RUNNING = !!c.running; PICKED = c.picked;",
        "  try { out.push(deskWaitReason()); } catch (e) { out.push({error: String(e)}); }",
        "}",
        "var write = (typeof console !== 'undefined' && console.log) "
        "  ? console.log : print;",
        "write(JSON.stringify(out));",
    ])
    prelude = (
        "var RUNNING = false, PICKED = '';\n"
        "var strategies = [], deskState = null, botState = null, runningStrategy = '';\n"
        "function chartStrategy() { return PICKED; }\n"
        + STUB
        + f"var CASES = {json.dumps(cases, ensure_ascii=False)};\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(prelude + src)
        js = fh.name
    try:
        proc = subprocess.run([path, *args, js], capture_output=True, text=True,
                              timeout=60)
        assert proc.returncode == 0, proc.stderr or proc.stdout
        return json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        Path(js).unlink(missing_ok=True)


def _render_after_switch() -> dict:
    """옛 심의가 있는 상태에서 규칙 전략으로 바꾼 뒤 실제 방 문구를 봅니다."""
    engine = _engine()
    assert engine, "엔진 없음"
    path, args = engine
    src = "\n".join([
        _fn("deskWaitReason"),
        _fn("shownStrategy"),
        _fn("renderDeskAvailability"),
        "renderDeskAvailability();",
        "var out = {subject: elements.deskSubject.textContent, rooms: {}};",
        "for (var i = 0; i < roomNames.length; i++) {",
        "  var room = roomNames[i];",
        "  out.rooms[room] = elements['say-' + room].line.textContent;",
        "}",
        "write(JSON.stringify(out));",
    ])
    prelude = r"""
var roomNames = ["analyst", "debate", "risk", "decision"];
function sayBox() {
  var who = {textContent: "옛 심의"};
  var line = {textContent: "총자산 800,000원"};
  return {className: "saybox", who: who, line: line,
    querySelector: function (selector) { return selector === ".who" ? who : line; }};
}
var elements = {deskSubject: {textContent: "옛 종목 결정 완료"}};
for (var i = 0; i < roomNames.length; i++) elements["say-" + roomNames[i]] = sayBox();
var RUNNING = false;
var document = {
  body: {classList: {
    contains: function (name) { return RUNNING && name === "running"; },
    toggle: function () {}
  }},
  getElementById: function (id) { return elements[id] || null; }
};
var $ = function (selector) {
  return document.getElementById(String(selector).replace(/^#/, ""));
};
var RULES = {id:"rules", name:"rules", label_ko:"규칙 전략",
  signals:[{id:"ema_cross"}], requires:[], tickers:[]};
var strategies = [RULES], deskState = {deliberations:[{rationale:"총자산 800,000원"}]};
var botState = null, runningStrategy = "";
function chartStrategy() { return "rules"; }
function stLabel(st) { return st.label_ko || st.name || ""; }
function renderTryNow() {}
function announceDesk() {}
var write = (typeof console !== "undefined" && console.log) ? console.log : print;
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(prelude + src)
        js = fh.name
    try:
        proc = subprocess.run([path, *args, js], capture_output=True, text=True,
                              timeout=60)
        assert proc.returncode == 0, proc.stderr or proc.stdout
        return json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        Path(js).unlink(missing_ok=True)


DESK = {"id": "kr_toss_desk", "name": "kr-toss-desk",
        "signals": [{"id": "desk"}], "requires": [], "tickers": []}
RULES = {"id": "kr_toss", "name": "kr-toss-flow",
         "signals": [{"id": "ema_cross"}], "requires": [], "tickers": []}

pytestmark = pytest.mark.skipif(_engine() is None,
                                reason="자바스크립트 엔진이 없습니다")


def test_each_situation_gets_its_own_answer():
    """여섯 상황이 여섯 가지로 갈려야 합니다 — 하나의 "대기 중" 이 아니라."""
    cases = [
        {"강": "데스크 없는 전략", "strategies": [RULES], "picked": "kr_toss",
         "running": False, "deskState": None, "botState": None},
        {"강": "아직 시작 안 함", "strategies": [DESK], "picked": "kr_toss_desk",
         "running": False, "deskState": None, "botState": None},
        {"강": "휴장", "strategies": [DESK], "picked": "kr_toss_desk", "running": True,
         "deskState": {"disabled_reason": "", "deliberations": []},
         "botState": {"universe": ["005930"],
                      "market": {"calendar": "krx", "open": False,
                                 "minutes_to_open": 812}}},
        {"강": "볼 종목 없음", "strategies": [DESK], "picked": "kr_toss_desk",
         "running": True, "deskState": {"disabled_reason": "", "deliberations": []},
         "botState": {"universe": [],
                      "market": {"calendar": "krx", "open": True,
                                 "minutes_to_open": 0}}},
        {"강": "데스크 꺼짐", "strategies": [DESK], "picked": "kr_toss_desk",
         "running": True,
         "deskState": {"disabled_reason": "LLM 사전 점검 실패: 401",
                       "deliberations": []},
         "botState": {"universe": ["005930"],
                      "market": {"calendar": "krx", "open": True,
                                 "minutes_to_open": 0}}},
        {"강": "정상 대기", "strategies": [DESK], "picked": "kr_toss_desk",
         "running": True, "deskState": {"disabled_reason": "", "deliberations": []},
         "botState": {"universe": ["005930"],
                      "market": {"calendar": "krx", "open": True,
                                 "minutes_to_open": 0}}},
    ]
    got = _run(cases)
    # 여섯 상황이 여섯 가지 **말**로 갈려야 합니다. `kind` 는 색을 정하는
    # 값이라 둘이 같을 수 있습니다("볼 종목 없음" 과 "데스크 꺼짐" 은 둘 다
    # 빨강) — 그래도 사람이 읽는 문장은 달라야 합니다. 원인이 다르면 할 일도
    # 다르기 때문입니다.
    said = [(r.get("who"), r.get("line")) for r in got]
    assert len(set(said)) == 6, (
        "상황이 뭉쳤습니다: "
        + str(list(zip([c["강"] for c in cases], [w for w, _ in said]))))

    # 기다려도 영영 안 되는 둘은 눈에 다르게 보여야 합니다.
    by = dict(zip([c["강"] for c in cases], got))
    assert by["볼 종목 없음"]["kind"] == "broken"
    assert by["데스크 꺼짐"]["kind"] == "broken"
    # 서버가 만들어 둔 진짜 이유가 그대로 나와야 합니다.
    assert "401" in by["데스크 꺼짐"]["line"], by["데스크 꺼짐"]["line"]
    # 기다리면 되는 쪽은 얼마나 기다려야 하는지 말해야 합니다.
    assert "13시간" in by["휴장"]["line"], by["휴장"]["line"]


def test_a_refresh_does_not_make_it_talk_about_another_strategy():
    """새로고침하면 선택기는 첫 전략으로 리셋됩니다 — 그래도 화면은 돌고 있는
    전략 이야기를 해야 합니다.

    선택기는 봇이 도는 동안 감춰져 있어서 사용자가 고칠 수도 없습니다. 그
    값으로 판단하면, 데스크가 죽어 있는데 "이 전략은 데스크를 쓰지 않습니다"
    가 뜨고 진짜 이유는 화면 어디에도 안 나옵니다.
    """
    got = _run([{
        "strategies": [RULES, DESK],
        "picked": "kr_toss",                 # 새로고침이 리셋해 놓은 값
        "runningStrategy": "kr-toss-desk",   # 서버가 말하는 진실
        "running": True,
        "deskState": {"disabled_reason": "LLM 사용량이 소진되었습니다",
                      "deliberations": []},
        "botState": {"universe": ["005930"],
                     "market": {"calendar": "krx", "open": True,
                                "minutes_to_open": 0}},
    }])[0]
    assert got["kind"] == "broken", got
    assert "소진" in got["line"], got["line"]


def test_switching_to_a_rules_strategy_erases_the_old_800k_deliberation():
    got = _render_after_switch()
    assert got["subject"] == "심의 없음"
    assert set(got["rooms"]) == {"analyst", "debate", "risk", "decision"}
    for line in got["rooms"].values():
        assert "규칙 기반" in line, got
        assert "800,000" not in line, got


def test_reverting_the_fix_fails_this_test():
    """이 테스트가 진짜로 무는지 — 함수를 통째로 되돌려서 확인합니다.

    앞선 정적 검사들은 이 조작을 통과시켰습니다. 그게 이 파일을 쓴 이유입니다.
    """
    engine = _engine()
    assert engine
    path, args = engine
    naive = ("function deskWaitReason() {\n"
             "  return {kind: 'waiting', who: '대기 중', line: '다음 봉이 닫히면'};\n"
             "}\n")
    src = (naive + "var out = [deskWaitReason(), deskWaitReason()];\n"
           "var write = (typeof console !== 'undefined' && console.log) "
           "  ? console.log : print;\n"
           "write(JSON.stringify(out));\n")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(src)
        js = fh.name
    try:
        proc = subprocess.run([path, *args, js], capture_output=True, text=True,
                              timeout=30)
        rows = json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        Path(js).unlink(missing_ok=True)
    assert len({r["kind"] for r in rows}) == 1, (
        "되돌린 구현이 상황을 구분합니다 — 이 테스트의 전제가 틀렸습니다")
