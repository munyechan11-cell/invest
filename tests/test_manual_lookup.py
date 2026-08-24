"""직접 고르는 매매.

자동매매는 종목까지 봇이 고릅니다. 수동은 내가 고르는 쪽이라, 먼저 찾을 수
있어야 합니다 — 그런데 지금까지는 전략에 미리 박힌 종목 중에서만 고를 수
있었습니다. 데스크가 어떤 종목을 사라고 했을 때 정작 그 종목을 지정할 방법이
없었다는 뜻입니다.

여기서 검사하는 것 중 가장 중요한 것: **종목코드를 지어내지 않는가.**
잘못된 코드는 다른 회사를 사는 것입니다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SERVER = Path("quant/api/server.py").read_text(encoding="utf-8")
STATE = Path("quant/live/state.py").read_text(encoding="utf-8")
USAGE = Path("quant/webapp/usage.py").read_text(encoding="utf-8")
REGISTRY = Path("quant/webapp/registry.py").read_text(encoding="utf-8")
HTML = Path("quant/api/static/index.html").read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))


def test_no_hardcoded_ticker_table_was_invented():
    """상장 종목 목록을 손으로 적으면 반드시 틀린 코드가 섞입니다.

    검색은 연동된 증권사에 물어보고, 이름 검색은 아는 것 안에서만 합니다.
    """
    body = re.search(r'@app\.get\("/api/lookup"\).*?@app\.get', SERVER, re.S).group(0)
    # 6자리 숫자 리터럴이 목록처럼 여러 개 박혀 있으면 지어낸 것입니다.
    codes = re.findall(r'"\d{6}"', body)
    assert len(codes) == 0, f"종목코드를 코드에 박아 넣었습니다: {codes[:5]}"
    assert "provider.describe" in body, "증권사에 물어보지 않습니다"


def test_a_looked_up_ticker_is_remembered():
    """한 번 찾은 것은 다음부터 이름으로도 찾혀야 합니다."""
    assert "def remember_ticker" in STATE
    assert "def known_tickers" in STATE
    # run 과 무관해야 합니다 — 봇을 껐다 켜도 찾아본 기록은 남습니다.
    table = re.search(r"CREATE TABLE IF NOT EXISTS known_symbols \((.*?)\);",
                      STATE, re.S).group(1)
    assert "run_id" not in table, "조회 기록이 실행에 묶여 있습니다"


def test_an_empty_name_never_overwrites_a_known_one():
    """이름 없이 다시 저장하면 알던 이름을 잃습니다."""
    body = re.search(r"def remember_ticker.*?self\.conn\.commit\(\)", STATE, re.S).group(0)
    assert "CASE WHEN excluded.name <> ''" in body


# ── 개별 심의 ────────────────────────────────────────────────────────────
def test_the_evaluate_request_model_is_at_module_level():
    """함수 안에 두면 FastAPI 가 쿼리 파라미터로 해석해 전부 422 가 됩니다.

    실제로 한 번 그랬습니다.
    """
    assert re.search(r"^class EvaluateRequest\(BaseModel\):", SERVER, re.M)


def test_evaluation_is_metered_before_and_after():
    body = re.search(r'@app\.post\("/api/evaluate"\).*?@app\.get', SERVER, re.S).group(0)
    # 부르기 전에 한도를 봅니다.
    assert "usage.allow" in body
    assert "HTTPException(429" in body
    # 끝나면 실제 비용을 적습니다. 실패해도 적어야 합니다 — 부른 만큼은
    # 청구되고, 성공만 계량하면 그 비용이 아무 계정에도 안 잡힙니다.
    assert "usage.record_spend" in body
    assert "finally:" in body
    finally_block = body[body.index("finally:"):]
    assert "record_spend" in finally_block, "실패한 호출의 비용이 계량되지 않습니다"


def test_the_meter_is_actually_wired_to_the_registry():
    """UsageStore 가 만들어져만 있고 아무도 안 쓰면 상한은 장식입니다."""
    assert "self.usage = UsageStore" in REGISTRY


def test_recorded_spend_is_not_recomputed_from_one_model_name():
    """데스크는 분석석과 결정석에 다른 모델을 씁니다.

    하나의 이름으로 뭉쳐 다시 계산하면 싼 쪽을 비싸게(또는 그 반대로) 칩니다.
    """
    assert "def record_spend" in USAGE
    body = re.search(r"def record_spend.*?self\.conn\.commit\(\)", USAGE, re.S).group(0)
    assert "price_for" not in body


def test_evaluation_refuses_without_enough_history():
    """봉이 없으면 16명이 아무것도 못 봅니다 — 그건 심의가 아니라 추측입니다."""
    body = re.search(r'@app\.post\("/api/evaluate"\).*?@app\.get', SERVER, re.S).group(0)
    assert "len(bars) < 60" in body


def test_evaluation_uses_the_strategy_the_user_picked():
    """`run_config()` 는 봇이 없으면 프로세스 기본값으로 물러섭니다.

    그 값을 그대로 쓰면 사용자가 무엇을 골랐든 데모 전략으로 심의합니다.
    """
    body = re.search(r'@app\.post\("/api/evaluate"\).*?@app\.get', SERVER, re.S).group(0)
    assert "seat.running()" in body


def test_the_result_says_it_is_not_an_order():
    """의견과 주문을 헷갈리면 안 됩니다."""
    assert "이건 의견이고 주문이 아닙니다" in SCRIPT


@pytest.mark.parametrize("el", ["lkQ", "lkGo", "lkRes", "mAsk", "mAskOut"])
def test_the_screen_has_the_controls(el):
    assert f'id="{el}"' in HTML


def test_picking_a_ticker_moves_the_chart_too():
    """한 곳에만 꽂으면 보고 있는 것과 주문 나가는 것이 달라집니다.

    인자 목록은 고정하지 않습니다 — 지키려는 것은 "두 곳에 함께 꽂힌다" 이지
    `pickTicker` 가 몇 개를 받는가가 아닙니다. 종목명을 함께 넘기도록 인자가
    하나 늘었을 때, 이 검사가 정규식 때문에 깨지면 그건 규칙이 깨진 게 아니라
    검사가 엉뚱한 것을 붙잡고 있었다는 뜻입니다.
    """
    body = re.search(r"function pickTicker\([^)]*\) \{(.*?)\n\}", SCRIPT, re.S).group(1)
    assert "#mSymbol" in body and "#cSym" in body
