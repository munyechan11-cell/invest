"""시작 버튼이 계량되지 않는 LLM 지출 통로가 되지 않는가.

봇을 켜면 봉을 기다리지 않고 그 자리에서 심의를 한 번 합니다 — 일봉 전략에서
첫 발언까지 하루를 기다리게 하지 않으려고 넣은 것입니다. 그런데 그 심의가
어디에도 안 잡히면, 껐다 켜기를 반복하는 것만으로 LLM 이 무한히 나갑니다.

데스크 자신의 `cost_limit_usd` 는 여기서 아무것도 막지 못합니다. 봇을 새로
세울 때마다 데스크도 새로 만들어져서 그 값이 0 부터 다시 세기 때문입니다.

`/api/evaluate` 는 처음부터 두 가지를 했습니다 — 부르기 전에 요금제를 묻고,
끝나면 실제 비용을 적습니다. 봇 경로도 같아야 합니다.
"""
from __future__ import annotations

import re
from pathlib import Path

TRADER = Path("quant/live/trader.py").read_text(encoding="utf-8")
REGISTRY = Path("quant/webapp/registry.py").read_text(encoding="utf-8")


def _opening() -> str:
    """지금 한 번 심의하는 코드. docstring 은 뺍니다.

    이 함수의 docstring 이 `desk.update()` 를 언급하는데, 그걸 코드로 세면
    "심의를 먼저 부르고 나중에 묻는다" 는 없는 결함이 잡힙니다. 실제로
    잡혔습니다.
    """
    m = re.search(r"async def _deliberate_now\(self[^)]*\).*?\n\n    (?:async )?def",
                  TRADER, re.S)
    assert m, "심의 본문을 찾지 못했습니다"
    return re.sub(r'"""..*?"""', "", m.group(0), count=1, flags=re.S)


def test_it_asks_before_spending():
    body = _opening()
    assert "self.meter" in body, "요금제를 묻지 않고 심의를 부릅니다"
    assert "meter.allow" in body, "허용 여부를 묻지 않습니다"
    # 거절당하면 부르지 않아야 합니다 — 물어보고 무시하면 안 묻는 것과 같습니다.
    ask = body.index("meter.allow")
    call = body.index("desk.update")
    assert ask < call, "심의를 먼저 부르고 나중에 묻습니다"


def test_it_records_even_when_the_deliberation_fails():
    """실패해도 부른 만큼은 청구됩니다.

    성공만 계량하면 실패한 호출의 비용이 아무 계정에도 안 잡히고, 나중에
    소급해서 만들 수도 없습니다.
    """
    body = _opening()
    assert "finally:" in body, "예외가 나면 계량을 건너뜁니다"
    tail = body[body.index("finally:"):]
    assert "meter.record" in tail, "finally 안에서 계량하지 않습니다"


def test_the_trader_does_not_need_to_know_about_accounts():
    """`LiveTrader` 가 요금제·계정을 알면 CLI 단독 실행이 그것에 묶입니다."""
    assert "usage" not in TRADER.lower() or "UsageStore" not in TRADER
    assert "from quant.webapp" not in TRADER, (
        "라이브 트레이더가 웹 계층을 import 합니다 — 단일 사용자 CLI 가 "
        "계정 DB 없이는 못 돌게 됩니다.")


def test_the_registry_wires_a_meter_for_every_bot():
    assert "meter=self._meter(" in REGISTRY, "봇에 계량기를 물리지 않습니다"
    m = re.search(r"    def _meter\(self.*?\n    (?:async )?def ", REGISTRY, re.S)
    assert m, "_meter 를 찾지 못했습니다"
    body = m.group(0)
    assert "usage.allow" in body and "usage.record_spend" in body
    # 자기 키면 상한 면제 — 다만 "이름이 등록됐는가" 가 아니라 "그 값이 실제로
    # 데스크에 들어갔는가" 로 판정해야 합니다.
    assert "desk_owns_key" in body, (
        "자기 키 판정을 이름 등록 여부로 합니다 — 아무 문자열이나 저장하면 "
        "상한이 사라지면서 정작 심의는 운영자 키로 나갑니다.")


def test_a_single_user_deployment_still_runs_without_a_meter():
    """계정이 없는 배포에는 셀 사람이 없습니다. 그때도 봇은 떠야 합니다."""
    body = _opening()
    assert "if self.meter is not None" in body, (
        "계량기가 없는 배포에서 개장 전 심의가 죽습니다")
    assert "meter: SpendMeter | None = None" in TRADER, (
        "계량기가 필수 인자입니다 — CLI 단독 실행이 깨집니다")
