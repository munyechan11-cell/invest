"""대시보드의 스크립트가 **파싱되는가.**

`syncChartSymbols()` 안에 `const names` 가 두 번 선언된 적이 있습니다. 한
줄은 원래 있던 것이고, 다른 한 줄은 "전략을 바꾸면 옛 종목을 지운다" 를
넣으면서 딸려 들어갔습니다. 두 줄 다 그 자리에서는 멀쩡해 보였습니다.

결과는 그 함수 하나가 고장난 것이 아니었습니다. 같은 블록에서 `const` 를
두 번 선언하면 **파싱 단계에서** SyntaxError 가 나고, 스크립트 전체가
한 줄도 실행되지 않습니다. 로그인도, 전략 목록도, 차트도, 데스크도 —
화면이 통째로 죽습니다. 브라우저 콘솔에는 딱 한 줄이 뜹니다:

    SyntaxError: Identifier 'names' has already been declared

그리고 서비스 워커가 예전 파일을 들고 있으면 그것마저 안 보입니다. 사람은
"어제까지 되던 게 오늘 안 된다" 를 겪고, 원인은 화면 어디에도 없습니다.

파이썬 테스트가 자바스크립트를 검사하는 것이 이상해 보이지만, 이 파일은
빌드 단계가 없어서 파싱 오류를 잡아 줄 도구가 여기 말고는 없습니다.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

HTML = Path("quant/api/static/index.html").read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))

#: 같은 화면이 함께 읽는 자바스크립트 전부. 어느 하나가 파싱되지 않으면
#: 증상은 똑같습니다 — 차트가 안 그려지거나, 서비스 워커가 등록되지 않고
#: 콘솔에 한 줄만 남습니다. 한 파일만 지키는 것은 지키는 척입니다.
SOURCES: dict[str, str] = {
    "index.html <script>": SCRIPT,
    "chart.js": Path("quant/api/static/chart.js").read_text(encoding="utf-8"),
    "sw.js": Path("quant/api/static/sw.js").read_text(encoding="utf-8"),
}

#: 진짜 파서. 있으면 쓰고, 없으면 아래 정적 검사로 갑니다. `jsc` 는 macOS 에
#: 항상 있지만 리눅스에는 없고, `node` 는 그 반대일 수 있습니다.
_ENGINES = [
    (shutil.which("node"), ["--check"]),
    (shutil.which("deno"), ["check"]),
    ("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc", []),
]


def _engine() -> tuple[str, list[str]] | None:
    for path, args in _ENGINES:
        if path and Path(path).exists():
            return path, args
    return None


def _strip(src: str) -> str:
    """문자열·템플릿·정규식·주석을 같은 길이의 공백으로 바꾼다.

    길이를 유지하는 이유는 하나입니다 — 오류가 났을 때 줄 번호가 원본과
    맞아야 사람이 그 줄을 찾아갑니다.

    정규식 리터럴을 빼먹으면 이 검사기 자체가 조용히 망가집니다. `/[^{]/`
    같은 것 하나가 중괄호 균형을 깨뜨리고, 그때부터 스코프 계산이 전부
    어긋나서 **아무것도 못 잡는 검사기**가 됩니다. 실제로 첫 판이 그랬습니다.
    """
    out: list[str] = []
    i, n = 0, len(src)
    #: 직전에 나온 뜻 있는 글자. `/` 가 나눗셈인지 정규식 시작인지는 이것으로만
    #: 갈립니다 — 식별자·숫자·닫는 괄호 뒤면 나눗셈, 그 밖이면 정규식.
    prev = ""

    def blank(text: str) -> str:
        return "".join(ch if ch == "\n" else " " for ch in text)

    def skip_quoted(j: int, quote: str) -> int:
        j += 1
        while j < n:
            if src[j] == "\\":
                j += 2
                continue
            if src[j] == quote:
                return j + 1
            j += 1
        return n

    def skip_template(j: int) -> int:
        """백틱 문자열. `${ }` 안에 또 백틱이 올 수 있어서 깊이를 셉니다."""
        j += 1
        while j < n:
            c = src[j]
            if c == "\\":
                j += 2
                continue
            if c == "`":
                return j + 1
            if c == "$" and j + 1 < n and src[j + 1] == "{":
                depth, j = 1, j + 2
                while j < n and depth:
                    c2 = src[j]
                    if c2 == "\\":
                        j += 2
                        continue
                    if c2 in "\"'":
                        j = skip_quoted(j, c2)
                        continue
                    if c2 == "`":
                        j = skip_template(j)
                        continue
                    depth += (c2 == "{") - (c2 == "}")
                    j += 1
                continue
            j += 1
        return n

    def skip_regex(j: int) -> int:
        j += 1
        in_class = False
        while j < n:
            c = src[j]
            if c == "\\":
                j += 2
                continue
            if c == "\n":
                return -1                 # 정규식은 줄을 넘지 못합니다 — 나눗셈이었던 것
            if c == "[":
                in_class = True
            elif c == "]":
                in_class = False
            elif c == "/" and not in_class:
                j += 1
                while j < n and src[j].isalpha():   # 플래그
                    j += 1
                return j
            j += 1
        return -1

    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(blank(src[i:j]))
            i = j
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(blank(src[i:j]))
            i = j
        elif c in "\"'":
            j = skip_quoted(i, c)
            out.append(blank(src[i:j]))
            i, prev = j, c
        elif c == "`":
            j = skip_template(i)
            out.append(blank(src[i:j]))
            i, prev = j, c
        elif c == "/" and prev not in ")]}" and not (prev.isalnum() or prev in "_$"):
            j = skip_regex(i)
            if j < 0:                     # 정규식이 아니라 나눗셈이었다
                out.append(c)
                i, prev = i + 1, c
            else:
                out.append(blank(src[i:j]))
                i, prev = j, "/"
        else:
            out.append(c)
            if not c.isspace():
                prev = c
            i += 1
    return "".join(out)


_DECL = re.compile(r"\b(?:const|let)\s+([A-Za-z_$][\w$]*)")


def _duplicate_lexical_declarations(src: str) -> list[tuple[str, int]]:
    """같은 중괄호 블록에서 두 번 선언된 `const`/`let` 이름.

    완전한 파서가 아닙니다. 괄호 안(`for (const x of …)`)은 자기 스코프를
    만들므로 건너뛰고, 구조분해(`const {a, b} = …`)는 첫 이름만 봅니다.
    놓치는 경우는 있어도 없는 것을 만들어 내지는 않습니다 — 테스트가
    거짓으로 실패하면 아무도 믿지 않게 되니까요.
    """
    clean = _strip(src)
    scopes: list[dict[str, int]] = [{}]
    paren = 0
    dupes: list[tuple[str, int]] = []
    i, line = 0, 1
    while i < len(clean):
        c = clean[i]
        if c == "\n":
            line += 1
        elif c == "(":
            paren += 1
        elif c == ")":
            paren = max(0, paren - 1)
        elif c == "{":
            scopes.append({})
        elif c == "}":
            if len(scopes) > 1:
                scopes.pop()
        elif paren == 0 and (c in "cl"):
            m = _DECL.match(clean, i)
            if m:
                name = m.group(1)
                if name in scopes[-1]:
                    dupes.append((name, line))
                else:
                    scopes[-1][name] = line
                i = m.end()
                continue
        i += 1
    return dupes


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_no_duplicate_const_in_the_same_block(name):
    """이것 하나로 화면 전체가 죽습니다 — 함수 하나가 아니라.

    엔진이 없는 환경(배포 서버는 우분투에 node 도 jsc 도 없습니다)에서
    실제로 도는 것은 아래 진짜-파서 검사가 아니라 이쪽입니다.
    """
    dupes = _duplicate_lexical_declarations(SOURCES[name])
    assert not dupes, (
        f"{name}: 같은 블록에서 두 번 선언된 이름 — "
        + ", ".join(f"{n} ({ln}번째 줄)" for n, ln in dupes)
        + " — SyntaxError 가 나면 그 파일 전체가 한 줄도 실행되지 않습니다.")


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_brackets_balance(name):
    """괄호가 맞는가 — 그리고 이 검사기가 소스를 제대로 읽고 있는가.

    균형이 안 맞으면 둘 중 하나입니다: 소스가 정말 깨졌거나, `_strip` 이
    정규식·템플릿을 잘못 건너뛰어 **검사기 자신이 어긋난** 것입니다.
    후자가 실제로 있었습니다 — 그때 위 검사는 아무것도 못 잡으면서 조용히
    통과했습니다. 지키는 척하는 테스트가 없는 테스트보다 나쁩니다.
    """
    clean = _strip(SOURCES[name])
    for open_c, close_c in ("{}", "()", "[]"):
        assert clean.count(open_c) == clean.count(close_c), (
            f"{name}: {open_c}{close_c} 균형이 맞지 않습니다 "
            f"({clean.count(open_c)} vs {clean.count(close_c)})")


def test_the_scanner_actually_catches_it():
    """검사기가 진짜 잡는지. 안 잡는 검사기는 아무것도 지키지 않습니다."""
    broken = """
    function f(syms) {
      const names = (syms || []).map(s => s.ticker);
      if (names.length) return;
      const names = (syms || []).map(x => x.ticker).filter(Boolean);
    }
    """
    assert _duplicate_lexical_declarations(broken) == [("names", 5)]


def test_the_scanner_does_not_cry_wolf():
    """다른 블록의 같은 이름, 반복문 헤더, 문자열 속 코드는 중복이 아닙니다."""
    fine = """
    function a() { const names = 1; return names; }
    function b() { const names = 2; return names; }
    for (const x of xs) { const y = x; }
    for (const x of ys) { const y = x; }
    const s = "const names = 1; const names = 2;";
    const t = `const names = 3; const names = 4;`;
    // const names = 5; const names = 6;
    """
    assert _duplicate_lexical_declarations(fine) == []


@pytest.mark.skipif(_engine() is None, reason="자바스크립트 엔진이 없습니다")
@pytest.mark.parametrize("name", sorted(SOURCES))
def test_the_whole_script_parses(name):
    """엔진이 있으면 진짜로 파싱해 봅니다 — 정적 검사가 놓치는 것까지.

    함수 표현식으로 감싸서 넘깁니다. 부르지 않으므로 **파싱만** 되고 한 줄도
    실행되지 않습니다 — 그냥 넘기면 `document` 를 찾다가 죽는데, 그건 문법
    오류가 아니라 여기에 브라우저가 없다는 뜻일 뿐입니다.
    """
    path, args = _engine()
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write("(function () {\n" + SOURCES[name] + "\n});\n")
        js = fh.name
    try:
        proc = subprocess.run([path, *args, js], capture_output=True, text=True,
                              timeout=60)
        assert proc.returncode == 0, (
            f"{name} 이 파싱되지 않습니다 ({Path(path).name}):\n"
            + (proc.stderr or proc.stdout)[:2000])
    finally:
        Path(js).unlink(missing_ok=True)
