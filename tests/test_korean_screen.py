"""화면에 남은 영어.

사용자 말: "영어로 나타내지말고 한국어로 좀 적어줘 접근성 높이자."

전략 목록이 `kr-toss-desk · dry_run` 로 떴습니다. 그 줄은 이 서비스에서
사람이 **자기 돈을 어디에 걸지 고르는** 유일한 자리인데, 거기 적힌 세
낱말 중 읽을 수 있는 것이 하나도 없었습니다. 상태 배지는 `OFFLINE`,
보유 종목은 `005930`, 이벤트 로그는 `order_rejected · buy` 였습니다.

여기서 검사하는 것은 문구의 품질이 아닙니다. **낡는 방식**입니다.

* 사전과 레지스트리가 갈라지는가. 좌석 스키마에 낱말을 하나 더하거나
  보호 장치를 하나 만들면, 화면 사전은 조용히 뒤처집니다. 그때 화면에
  뜨는 것은 빈칸이 아니라 영어라서 아무도 고장으로 신고하지 않습니다.
* 마크업에 영어가 다시 새어 들어오는가. 한 번 걷어낸 자리는 다음 사람이
  아무 생각 없이 되돌려 놓기 쉽습니다.
* 종목 코드만 단독으로 그리는 경로가 생기는가.

브랜드 표기(QUANT TRADING FLOOR)는 로고이므로 아래 허용 목록에 있습니다.
티커와 전략 식별자(`name`)도 남깁니다 — 그건 이름이 아니라 주소이고,
로그·설정과 대조할 때 쓰는 값입니다.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from quant.alpha.seats import HEAD_SCHEMA, RESEARCH_PLAN_SCHEMA, TRADER_SCHEMA
from quant.api.server import create_app, strategy_catalog
from quant.config.loader import load_config
from quant.core.events import EventType
from quant.risk.protections import BUILTIN_PROTECTIONS

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "quant/api/static/index.html"
HTML = PAGE.read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))
MARKUP = HTML[: HTML.index("<script")]

HANGUL = re.compile(r"[가-힣]")


# ── 자바스크립트에서 표 하나 꺼내기 ─────────────────────────────────────
def _object_literal(name: str) -> str:
    """`const NAME = { … };` 의 중괄호 안쪽. 중첩된 중괄호까지 셉니다."""
    start = SCRIPT.index(f"const {name} = {{")
    i = SCRIPT.index("{", start)
    depth, j = 0, i
    while j < len(SCRIPT):
        if SCRIPT[j] == "{":
            depth += 1
        elif SCRIPT[j] == "}":
            depth -= 1
            if depth == 0:
                return SCRIPT[i + 1:j]
        j += 1
    raise AssertionError(f"{name} 의 닫는 괄호를 찾지 못했습니다")


def _keys(name: str) -> set[str]:
    return set(re.findall(r"(\w+)\s*:", _object_literal(name)))


def _pairs(name: str) -> dict[str, str]:
    return dict(re.findall(r'(\w+)\s*:\s*"([^"]*)"', _object_literal(name)))


def _function_body(name: str) -> str:
    m = re.search(rf"function {re.escape(name)}\(.*?\) \{{(.*?)\n\}}", SCRIPT, re.S)
    assert m, f"{name} 함수를 찾지 못했습니다"
    return m.group(1)


# ── 1. 전략 이름 ─────────────────────────────────────────────────────────
def _strategies() -> dict[str, object]:
    out = {}
    for name, path in strategy_catalog(ROOT / "configs").items():
        try:
            out[name] = load_config(str(path))
        except Exception:
            continue          # 전략이 아닌 YAML(파라미터 공간 등)
    return out


def test_every_strategy_a_user_can_pick_has_a_korean_name():
    """목록에 뜨는데 이름이 없으면, 그 줄은 고를 수 없는 줄입니다."""
    missing = [n for n, cfg in _strategies().items()
               if not HANGUL.search(cfg.label_ko or "")]
    assert not missing, f"한국어 이름이 없는 전략: {missing}"


def test_the_korean_name_does_not_repeat_the_mode():
    """모드는 `mode` 한 곳에서만 읽습니다.

    이름에 "과거 검증용" 을 적어 두면, 나중에 `mode` 를 dry_run 으로 올리는
    사람은 그 문자열을 같이 고치지 않습니다. 그 순간 화면은 실시간으로 도는
    전략을 "과거 검증용" 이라고 자신 있게 소개합니다.
    """
    for name, cfg in _strategies().items():
        label = cfg.label_ko or ""
        for word in ("과거 검증", "모의", "실거래", "dry_run", "backtest", "live"):
            assert word not in label, (
                f"{name}: 이름에 모드({word})가 박혀 있습니다 — "
                "mode 를 바꾸면 이 문자열이 거짓말이 됩니다")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_SECRET_KEY", "k" * 48)
    monkeypatch.setenv("QUANT_USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setenv("QUANT_USER_DATA", str(tmp_path / "userdata"))
    monkeypatch.setenv("QUANT_ENV_FILE", str(tmp_path / "env.test"))
    monkeypatch.setenv("QUANT_CONFIG_DIR", str(ROOT / "configs"))
    app = create_app(None, state_path=str(tmp_path / "state.db"))
    with TestClient(app, base_url="https://desk.example") as c:
        r = c.post("/api/auth/register",
                   json={"email": "a@example.com", "password": "abcd1234efgh"})
        assert r.status_code == 201, r.text
        yield c


def test_the_api_hands_the_korean_name_to_the_screen(client):
    """설정에 이름이 있어도 응답에 실리지 않으면 화면은 그것을 모릅니다."""
    rows = client.get("/api/strategies").json()["strategies"]
    assert rows, "전략 목록이 비었습니다"
    for row in rows:
        assert HANGUL.search(row.get("label_ko") or ""), (
            f"{row['id']}: 응답에 한국어 이름이 없습니다 — {row}")
        assert HANGUL.search(row.get("mode_ko") or ""), f"{row['id']}: 모드가 영어입니다"


def test_an_unnamed_strategy_still_appears(tmp_path, monkeypatch):
    """이름이 없다고 목록에서 사라지면, 그건 고칠 수도 없는 전략이 됩니다."""
    root = tmp_path / "configs"
    root.mkdir()
    (root / "nameless.yaml").write_text(
        yaml.safe_dump({"name": "nameless-one", "alpha": [{"type": "ema_cross"}],
                        "universe": {"symbols": [{"ticker": "AAA", "venue": "SIM"}]}},
                       allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("QUANT_SECRET_KEY", "k" * 48)
    monkeypatch.setenv("QUANT_USERS_DB", str(tmp_path / "users.db"))
    monkeypatch.setenv("QUANT_USER_DATA", str(tmp_path / "userdata"))
    monkeypatch.setenv("QUANT_ENV_FILE", str(tmp_path / "env.test"))
    monkeypatch.setenv("QUANT_CONFIG_DIR", str(root))
    app = create_app(None, state_path=str(tmp_path / "state.db"))
    with TestClient(app, base_url="https://desk.example") as c:
        assert c.post("/api/auth/register",
                      json={"email": "a@example.com",
                            "password": "abcd1234efgh"}).status_code == 201
        rows = c.get("/api/strategies").json()["strategies"]
    assert [r["id"] for r in rows] == ["nameless"]
    assert rows[0]["label_ko"] == ""      # 없으면 빈 값 — 화면이 name 으로 떨어집니다


def test_the_picker_shows_the_korean_name_not_the_identifier():
    """드롭다운 한 줄이 이 서비스에서 사람이 읽는 첫 문장입니다."""
    body = _function_body("loadStrategies")
    assert "stLabel(st)" in body, "목록이 한국어 이름을 쓰지 않습니다"
    assert "esc(st.name)" not in body, "아직 식별자를 그대로 그립니다"
    assert "esc(st.mode)" not in body, "모드 값을 영어로 그대로 그립니다"


def test_the_picker_falls_back_to_the_identifier():
    """이름이 비어도 줄은 남아야 합니다 — 빈 줄은 고를 수 없습니다."""
    assert re.search(r"st\.label_ko \|\| st\.name", SCRIPT), \
        "이름이 없을 때 떨어질 곳이 없습니다"


# ── 2. 마크업에 남은 영어 ────────────────────────────────────────────────
#: 영어로 남기기로 한 것들. 여기에 낱말을 더하는 것은 결정이어야 합니다.
#:
#: * QUANT / TRADING / FLOOR — 로고 표기. 이름이지 설명이 아닙니다.
#: * API / AI — 한국어 문장 안에서 이미 그대로 쓰이는 낱말.
ALLOWED_WORDS = {"QUANT", "TRADING", "FLOOR", "API", "AI"}

#: 브라우저에게 하는 말이지 사람에게 하는 말이 아닌 속성들.
_INVISIBLE_ATTRS = {"content", "href", "src", "type", "name", "id", "class",
                    "rel", "charset", "value", "data-page", "role", "style",
                    "autocomplete", "inputmode", "for", "lang", "dir", "step"}
_VISIBLE_ATTRS = {"aria-label", "title", "placeholder", "alt"}


class _Visible(HTMLParser):
    """사람 눈에 닿는 문자열만 모읍니다."""

    def __init__(self) -> None:
        super().__init__()
        self.skip = 0
        self.out: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self.skip += 1
        for key, value in attrs:
            if key in _VISIBLE_ATTRS and value:
                self.out.append((f"@{key}", value))

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.out.append(("text", data.strip()))


def test_nothing_a_reader_sees_is_english_except_the_wordmark():
    parser = _Visible()
    parser.feed(MARKUP)
    offenders = []
    for where, text in parser.out:
        for word in re.findall(r"[A-Za-z]{2,}", text):
            if word.upper() in ALLOWED_WORDS:
                continue
            offenders.append((where, word, text[:60]))
    assert not offenders, f"화면에 영어가 남아 있습니다: {offenders[:6]}"


def test_the_mode_badge_speaks_korean():
    """머리말 배지는 지금 실거래인지 모의인지 말하는 유일한 자리입니다."""
    badge = _pairs("MODE_BADGE")
    for mode in ("live", "dry_run", "backtest", "offline"):
        assert mode in badge, f"{mode} 배지 이름이 없습니다"
        assert HANGUL.search(badge[mode]), f"{mode} 배지가 영어입니다: {badge[mode]}"
    # 실거래는 그 사실을 분명히 말해야 합니다.
    assert "실거래" in badge["live"]


# ── 3. 사전이 레지스트리와 갈라지지 않는가 ───────────────────────────────
def test_every_event_the_feed_shows_has_a_korean_name():
    """이벤트 로그는 "봇이 방금 무엇을 했는가" 를 보는 유일한 칸입니다."""
    shown = set(re.findall(r'case "(\w+)":', _function_body("pushEvent")))
    assert shown, "이벤트 종류를 하나도 읽지 못했습니다"
    known = _pairs("EVENT_KO")
    missing = sorted(shown - set(known))
    assert not missing, f"한국어 이름이 없는 이벤트: {missing}"
    for key, value in known.items():
        assert HANGUL.search(value), f"{key} 가 영어입니다: {value}"


def test_the_event_dictionary_does_not_invent_events():
    """실재하지 않는 종류가 사전에 있으면, 그건 낡았다는 뜻입니다."""
    real = {e.value for e in EventType}
    unknown = sorted(set(_pairs("EVENT_KO")) - real)
    assert not unknown, f"EventType 에 없는 이벤트: {unknown}"


def test_every_protection_has_a_korean_name():
    """보호 장치를 하나 더하면 이벤트 로그에 영어 이름이 그대로 뜹니다."""
    assert set(BUILTIN_PROTECTIONS) == set(_pairs("PROTECTION_KO")), (
        "보호 장치 목록과 화면 사전이 갈라졌습니다: "
        f"{sorted(set(BUILTIN_PROTECTIONS) ^ set(_pairs('PROTECTION_KO')))}")


def test_every_word_a_seat_can_say_has_a_korean_name():
    """좌석은 스키마의 enum 안에서만 말합니다. 그 낱말 전부를 덮어야 합니다."""
    styles = set(TRADER_SCHEMA["properties"]["entry_style"]["enum"])
    assert styles <= set(_pairs("ENTRY_STYLE_KO")), \
        f"주문 방식에 한국어가 없습니다: {sorted(styles - set(_pairs('ENTRY_STYLE_KO')))}"

    verdicts = (set(HEAD_SCHEMA["properties"]["action"]["enum"])
                | set(RESEARCH_PLAN_SCHEMA["properties"]["rating"]["enum"]))
    assert verdicts <= _keys("ACTION_STYLE"), \
        f"판정에 한국어가 없습니다: {sorted(verdicts - _keys('ACTION_STYLE'))}"

    assert {"bullish", "bearish", "neutral"} <= set(_pairs("STANCE_KO"))


# ── 4. 종목은 이름으로 부르되 코드를 지우지 않는다 ───────────────────────
#: 종목 코드가 사람 눈에 닿는 자리들. 여기 함수 하나가 코드만 그리면,
#: 그 화면만 다시 "005930" 으로 돌아갑니다.
SYMBOL_SCREENS = ["renderTape", "renderHud", "pushEvent", "renderFlow",
                  "refreshSymbols", "syncChartSymbols", "loadTradeLog"]


@pytest.mark.parametrize("fn", SYMBOL_SCREENS)
def test_no_screen_draws_a_bare_code(fn):
    assert "symLabel(" in _function_body(fn), \
        f"{fn} 이 종목 이름 없이 코드만 그립니다"


def test_the_bar_period_is_named_the_same_way_everywhere():
    """`1d 봉` 과 `일봉` 이 한 화면에 같이 뜨면 같은 값으로 안 읽힙니다."""
    assert "function timeframeKo" in SCRIPT
    brief = _function_body("renderStrategyBrief")
    assert "timeframeKo(st.timeframe)" in brief, "전략 설명이 봉 주기를 코드로 그립니다"
    assert "esc(st.timeframe" not in brief
    # 오른쪽 시세 칸의 선택기가 쓰는 말과 같은 어휘여야 합니다.
    options = re.findall(r'<option value="(\w+)">([^<]+)</option>',
                         MARKUP[MARKUP.index('id="cTf"'):])
    assert options, "봉 주기 선택기를 찾지 못했습니다"
    for value, label in options:
        assert HANGUL.search(label), f"{value} 선택지가 영어입니다: {label}"


_ENGINES = [
    (shutil.which("node"), []),
    ("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc", []),
]


def _engine():
    for path, args in _ENGINES:
        if path and Path(path).exists():
            return path, args
    return None


#: (티커, 이름) → 화면에 뜨는 글자.
SYMBOL_CASES = [
    ("005930", "삼성전자", "삼성전자 (005930)"),
    ("kis:005930", "삼성전자", "삼성전자 (005930)"),
    # 이름을 못 받았을 때. 빈 괄호는 "이름이 없다" 는 사실조차 못 알려 줍니다.
    ("005930", "", "005930"),
    ("005930", None, "005930"),
    # 서버가 이름 대신 코드를 되돌려준 경우 — 같은 글자를 두 번 쓰지 않습니다.
    ("005930", "005930", "005930"),
    ("AAPL", "  ", "AAPL"),
]


def _call_in_page(fn: str, calls: list[list]) -> list[str]:
    """화면에 실제로 실려 있는 함수를 그대로 떼어 내 돌려 봅니다.

    베껴서 검사하면 구현이 틀려도 통과합니다 — 여기서 도는 것은 브라우저가
    읽는 바로 그 소스입니다. 함께 딸려 오는 헬퍼(`TF_UNIT` 같은 상수)는
    이름으로 찾아 앞에 붙입니다.
    """
    path, args = _engine()
    src = re.search(rf"function {re.escape(fn)}\(.*?\) \{{.*?\n\}}", SCRIPT, re.S)
    assert src, f"{fn} 을 찾지 못했습니다"
    helpers = "".join(m.group(0) for m in
                      re.finditer(r"const TF_UNIT = \{[^}]*\};\n", SCRIPT))
    program = (helpers + src.group(0) + "\n"
               + f"var cases = {json.dumps(calls, ensure_ascii=False)};\n"
               # 한 줄에 하나씩 찍으면 빈 문자열이 줄바꿈에 먹혀 사라집니다 —
               # "이름이 없으면 빈 값" 이 바로 검사하려던 경우인데.
               + "var out = [];\n"
               + f"for (var i = 0; i < cases.length; i++) out.push({fn}.apply(null, cases[i]));\n"
               # Node 는 console.log, JavaScriptCore 는 print 를 제공합니다.
               # 어느 한쪽 이름을 고정하면 로컬은 통과하고 CI 만 깨집니다.
               + "var write = (typeof console !== 'undefined' && console.log) "
               + "  ? console.log : print;\n"
               + "write(JSON.stringify(out));\n")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(program)
        js = fh.name
    try:
        proc = subprocess.run([path, *args, js], capture_output=True, text=True,
                              timeout=60)
        assert proc.returncode == 0, proc.stderr or proc.stdout
        return json.loads(proc.stdout.strip())
    finally:
        Path(js).unlink(missing_ok=True)


@pytest.mark.skipif(_engine() is None, reason="자바스크립트 엔진이 없습니다")
def test_a_symbol_is_named_first_and_coded_in_parentheses():
    """코드를 지우면 안 됩니다 — 주문을 확인할 때 사람이 대조하는 것은 코드입니다."""
    got = _call_in_page("symLabel", [[t, n] for t, n, _ in SYMBOL_CASES])
    want = [expected for _, _, expected in SYMBOL_CASES]
    assert got == want, f"기대: {want}\n실제: {got}"


#: 봉 주기 코드 → 화면에 뜨는 글자.
TIMEFRAME_CASES = [
    ("1d", "일봉"), ("1w", "주봉"), ("4h", "4시간봉"),
    ("1h", "1시간봉"), ("15m", "15분봉"), ("5m", "5분봉"),
    # 모르는 모양은 지어내지 않고 그대로 둡니다.
    ("tick", "tick"), ("", ""),
]


@pytest.mark.skipif(_engine() is None, reason="자바스크립트 엔진이 없습니다")
def test_the_bar_period_reads_as_korean():
    got = _call_in_page("timeframeKo", [[tf] for tf, _ in TIMEFRAME_CASES])
    want = [expected for _, expected in TIMEFRAME_CASES]
    assert got == want, f"기대: {want}\n실제: {got}"
