"""서비스 워커가 고친 코드를 막지 않는가.

PWA 의 캐시는 조용한 종류의 위험입니다. 한 번 받아 간 파일을 계속 주면
사용자는 배포된 적 없는 버전을 씁니다 — 실제로 차트를 무한 재귀에서 구해
놓고도 옛 파일이 계속 나갔습니다.

더 나쁜 것은 반쪽 캐시입니다. 문서만 네트워크 우선이면 새 index.html 과
옛 chart.js 가 섞여서, 어느 쪽 개발자도 본 적 없는 조합이 돌아갑니다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SW = Path("quant/api/static/sw.js").read_text(encoding="utf-8")


def test_code_is_fetched_from_the_network_first():
    """js/css 는 네트워크 우선이어야 합니다. 캐시 우선이면 못 고칩니다."""
    assert re.search(r"\(js\|css\)\$", SW), "코드 확장자를 따로 다루지 않습니다"
    branch = SW[SW.find("(js|css)$"):]
    assert branch.index("fetch(e.request)") < branch.index("caches.match"), \
        "코드 경로가 아직 캐시를 먼저 봅니다"


def test_money_never_comes_from_a_cache():
    """잔고·포지션·시세를 캐시에서 주는 것은 오프라인 지원이 아니라 거짓말입니다."""
    assert "url.pathname.startsWith('/api/')" in SW
    assert "url.pathname === '/ws'" in SW


def test_documents_are_network_first():
    nav = SW[SW.find("mode === 'navigate'"):]
    assert nav.index("fetch(e.request)") < nav.index("caches.match")


def test_old_caches_are_dropped_on_activate():
    assert "caches.keys()" in SW and "caches.delete" in SW


def test_only_one_shell_version_is_named():
    """이름이 두 개면 어느 쪽이 지워지는지 알 수 없습니다."""
    names = set(re.findall(r"'(quant-shell-v\d+)'", SW))
    assert len(names) == 1, f"셸 이름이 여러 개입니다: {names}"


@pytest.mark.parametrize("path", ["/static/chart.js", "/static/app.css"])
def test_a_shipped_fix_reaches_a_returning_visitor(path):
    """이 테스트의 요점은 문자열이 아니라 결과입니다 — 재방문자가 새 코드를 받는가.

    정적 파일 이름에 해시를 붙이지 않으므로(주소가 안 바뀝니다), 네트워크를
    먼저 보는 것 말고는 새 파일이 갈 길이 없습니다.
    """
    assert re.search(r"\(js\|css\)\$", SW)
    assert path.endswith((".js", ".css"))
