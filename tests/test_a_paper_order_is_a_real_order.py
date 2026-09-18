""""가상 체결" 이라고 쓰여 있는데 주문은 진짜로 나갑니다.

    모의 매매 — 실시간 시세, 가상 체결        ← 화면이 이렇게 씁니다

`kr_kis_paper` 는 `paper_trading: true` 입니다. 어댑터 문서가 직접 말합니다:

    · mode: dry_run                        — 모의투자를 읽고, 아무것도 안 보냄
    · mode: dry_run + paper_trading: true  — 모의투자에 **진짜 주문**

둘은 다른 일입니다. 엔진이 혼자 체결을 흉내 내면 **주문이 거절되는 것도,
호가단위가 안 맞는 것도, 체결이 늦는 것도 영영 못 봅니다** — 예행연습의
요점이 바로 그것들인데요.

그리고 이 오해는 한쪽으로만 기웁니다: 진짜 주문이 나가는 설정을 "가상 체결"
로 읽은 사람은 **실제보다 안전하다고 믿습니다.**
"""
from __future__ import annotations

import pytest

from quant.cli import _load
from quant.strategy.glossary import MODE, mode_label
from tests.conftest import shipped_configs


def test_simulated_fills_and_venue_paper_orders_read_differently():
    quiet = mode_label("dry_run", sends_orders=False)
    real = mode_label("dry_run", sends_orders=True)
    assert quiet != real
    assert "가상 체결" in quiet
    assert "진짜 주문" in real and "모의계좌" in real


def test_live_and_backtest_are_untouched():
    assert mode_label("live") == MODE["live"]
    assert mode_label("backtest") == MODE["backtest"]
    assert mode_label("live", sends_orders=True) == MODE["live"]


def test_an_unknown_mode_returns_itself_rather_than_vanishing():
    assert mode_label("weird") == "weird"


@pytest.mark.parametrize("path", [p for p in shipped_configs()
                                  if _load(p).broker.params.get("paper_trading")])
def test_every_venue_paper_config_says_orders_go_out(path):
    config = _load(path)
    said = mode_label(config.mode.value,
                      bool(config.broker.params.get("paper_trading")))
    assert "가상 체결" not in said, (
        f"{path}: 주문이 증권사로 나가는데 화면은 '가상 체결' 이라고 합니다")


def test_the_screen_asks_the_server_instead_of_guessing_from_mode():
    """`dry_run` 이라는 이유만으로 "가상 체결" 을 쓰면 안 됩니다."""
    from pathlib import Path

    html = Path("quant/api/static/index.html").read_text(encoding="utf-8")
    assert '["dry_run", "모의투자 · 실시간 시세, 가상 체결"]' not in html
    server = Path("quant/api/server.py").read_text(encoding="utf-8")
    assert "glossary.mode_label(" in server
    assert '"sends_orders"' in server


def test_the_api_reports_whether_orders_leave_the_process():
    from pathlib import Path

    server = Path("quant/api/server.py").read_text(encoding="utf-8")
    block = server[server.index('"sends_orders"'):][:220]
    assert "paper_trading" in block and "RunMode.LIVE" in block
