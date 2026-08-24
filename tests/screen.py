"""화면 파일을 읽는 한 자리.

스타일은 `app.css` 로, 마크업과 동작은 `index.html` 로 갈라졌습니다 — 디자인과
로직을 동시에 만질 수 있게 하려고요. 그런데 화면을 검사하는 테스트 입장에서는
여전히 **한 화면** 입니다. "이 버튼이 손가락만 한가" 같은 질문은 두 파일에
걸쳐 있고, 파일이 갈라졌다고 그 질문이 둘로 나뉘지는 않습니다.

파일이 또 갈라질 때 고칠 자리를 하나로 둡니다.
"""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path("quant/api/static")
PAGE = STATIC / "index.html"
STYLE = STATIC / "app.css"


def markup() -> str:
    """마크업과 스크립트만 — 스타일은 빠집니다."""
    return PAGE.read_text(encoding="utf-8")


def style() -> str:
    return STYLE.read_text(encoding="utf-8")


def screen() -> str:
    """스타일까지 합친 화면 전체. 규칙을 찾을 때 씁니다.

    `<style>` 로 감싸서 붙입니다 — 예전처럼 한 파일이었을 때와 같은 모양이라,
    이걸 읽는 검사들이 갈라짐을 몰라도 됩니다.
    """
    page = markup()
    return page.replace('<link rel="stylesheet" href="/static/app.css">',
                        "<style>\n" + style() + "\n</style>", 1)


def script() -> str:
    return "\n".join(re.findall(r"<script>(.*?)</script>", markup(), re.S))
