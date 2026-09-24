"""`scripts/desk_cost.py` 가 출하 데스크(Jev, 토론 1라운드)의 비용을 읽을 수 있게 적는가.

운영자는 이 계산기로 `max_symbols_per_run` 과 요금제 상한을 정합니다. 예전에는
기본값이 아무도 돌리지 않는 토론 2라운드(18회)였고, 금액을 `.3f`·`.2f` 로 고정해
Jev 행이 "$0.001 / $0.01 / $0.00" — 하루 비용이 공짜처럼 읽혔습니다.

네트워크는 나가지 않습니다(계산만 합니다).
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import yaml

from tests.conftest import shipped_configs

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "desk_cost.py"


def _run(monkeypatch, capsys, *argv: str) -> str:
    spec = importlib.util.spec_from_file_location("desk_cost_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)   # dataclass 가 모듈을 찾습니다
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", ["desk_cost.py", *argv])
    assert module.main() == 0
    return capsys.readouterr().out


def test_the_default_is_the_shipped_desk_and_the_jev_row_is_not_zero(monkeypatch, capsys):
    out = _run(monkeypatch, capsys)
    rounds = {int((m.get("params") or {}).get("debate_rounds", 2))
              for raw in (yaml.safe_load(Path(p).read_text(encoding="utf-8"))
                          for p in shipped_configs())
              for m in raw.get("alpha") or [] if m.get("type") == "desk"}
    assert rounds == {1}, rounds                       # 출하 데스크는 전부 1라운드
    assert "토론 1라운드" in out and "LLM 호출 16회" in out, out
    row = next(line for line in out.splitlines() if "typesafe-ai/jev" in line)
    values = [float(v) for v in re.findall(r"\$([\d.,]+)", row.split("출력")[0])]
    assert len(values) == 3 and all(v > 0 for v in values), row
    assert re.search(r"\$0\.00\d+", row), row           # 유효 숫자가 보입니다


def test_other_rows_keep_their_usual_precision(monkeypatch, capsys):
    out = _run(monkeypatch, capsys, "--debate-rounds", "2")
    assert "LLM 호출 18회" in out
    row = next(line for line in out.splitlines() if line.strip().startswith("claude-opus-5"))
    assert re.search(r"\$\d+\.\d{2}\b", row), row
