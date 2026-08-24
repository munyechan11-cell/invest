"""실거래가 실제로 실거래인가 — 그리고 켜기 전에 사람이 알 수 있는가.

이 파일은 "실거래가 되게 해 달라" 는 요청을 지키는 쪽과, 그 요청이 사고로
이어지지 않게 막는 쪽을 동시에 검사합니다. 둘은 대립하지 않습니다: 실거래를
막는 것이 아니라, **켜는 사람이 무엇을 켜는지 알고 켜게** 하는 것입니다.
"""
from __future__ import annotations

import glob
import re
from pathlib import Path

import pytest

from quant.config.loader import load_config
from quant.core.types import RunMode

HTML = Path("quant/api/static/index.html").read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))


def _configs():
    for path in sorted(glob.glob("configs/*.yaml")):
        try:
            yield path, load_config(path)
        except Exception:
            continue          # 전략이 아닌 YAML(파라미터 공간 등)


LIVE = [(p, c) for p, c in _configs() if c.mode is RunMode.LIVE]


def test_there_is_at_least_one_live_strategy():
    """이 파일의 전제. 전부 모의로 돌아가면 아래 검사는 지킬 대상이 없습니다."""
    assert LIVE, "실거래 전략이 하나도 없습니다"


@pytest.mark.parametrize("path,cfg", LIVE, ids=lambda v: getattr(v, "name", str(v)))
def test_every_live_strategy_has_a_loss_cap(path, cfg):
    """비율만으로는 부족합니다.

    스키마는 네 한도 중 **하나만** 있으면 통과시킵니다. 그런데 비율 한도는
    자산이 클수록 커지고, 거래대금 한도는 손실을 재지 않습니다. 실거래에서
    마지막 방어선은 "오늘 얼마를 잃었나" 이므로 그것을 요구합니다.
    """
    assert cfg.limits.max_daily_loss, (
        f"{path}: 실거래인데 하루 실현손실 한도가 없습니다 — 거래대금·비율 "
        "한도만으로는 '오늘 얼마까지 잃어도 되는가' 를 못 정합니다.")
    assert cfg.limits.max_daily_orders, f"{path}: 하루 주문 건수 한도가 없습니다"
    assert cfg.limits.max_daily_notional, f"{path}: 하루 거래대금 한도가 없습니다"


@pytest.mark.parametrize("path,cfg", LIVE, ids=lambda v: getattr(v, "name", str(v)))
def test_a_single_order_cannot_blow_the_daily_cap(path, cfg):
    """주문 하나가 하루 한도를 넘으면 그 한도는 한 번도 안 걸립니다."""
    per_order = cfg.broker.max_order_notional
    assert per_order, f"{path}: 주문당 상한이 없습니다"
    assert per_order <= cfg.limits.max_daily_notional, (
        f"{path}: 주문 하나({per_order:,.0f})가 하루 거래대금 한도"
        f"({cfg.limits.max_daily_notional:,.0f})보다 큽니다.")


@pytest.mark.parametrize("path,cfg", LIVE, ids=lambda v: getattr(v, "name", str(v)))
def test_a_live_strategy_says_so_in_its_own_file(path, cfg):
    """파일을 연 사람이 첫 화면에서 알아야 합니다."""
    head = Path(path).read_text(encoding="utf-8")[:1400]
    assert "실거래" in head, (
        f"{path}: 실거래 설정인데 파일 머리말이 그 사실을 말하지 않습니다.")


@pytest.mark.parametrize("path,cfg", LIVE, ids=lambda v: getattr(v, "name", str(v)))
def test_the_position_cap_fits_inside_the_order_cap(path, cfg):
    """비중 상한이 주문당 상한보다 크면 큰 주문이 전부 거절됩니다.

    거절은 안전해 보이지만, 그 상태의 봇은 매수 신호가 날 때마다 조용히
    실패하면서 아무것도 사지 않습니다 — 돌고 있는데 돌지 않는 상태입니다.
    """
    want = cfg.portfolio.starting_cash * cfg.portfolio.max_position_weight
    assert want <= cfg.broker.max_order_notional * 1.0000001, (
        f"{path}: 한 종목 최대 비중이 {want:,.0f} 인데 주문당 상한이 "
        f"{cfg.broker.max_order_notional:,.0f} 입니다 — 목표 비중을 채우는 "
        "주문이 전부 거절됩니다.")


def test_the_screen_makes_you_type_the_name_before_going_live():
    """예/아니오 버튼은 손이 먼저 움직입니다. 이름을 적는 동안은 읽게 됩니다."""
    handler = re.search(r'getElementById\("runStart"\)\.onclick = async \(\) => \{(.*?)\n\};',
                        SCRIPT, re.S)
    assert handler, "시작 핸들러를 찾지 못했습니다"
    body = handler.group(1)
    assert 'st.mode === "live"' in body, "실거래를 모의와 같은 길로 시작합니다"
    assert "prompt(" in body, "실거래에 확인 절차가 없습니다"
    assert "st.name" in body, "이름을 대조하지 않습니다"
    assert 'body.confirm' in body, "서버가 요구하는 확인값을 안 보냅니다"


def test_the_confirmation_shows_what_is_at_stake():
    """한도를 모르는 채로 켜면 안 됩니다."""
    handler = re.search(r'getElementById\("runStart"\)\.onclick = async \(\) => \{(.*?)\n\};',
                        SCRIPT, re.S).group(1)
    assert "daily_loss" in handler and "daily_notional" in handler, (
        "확인 창이 하루 한도를 보여주지 않습니다 — '얼마까지 잃어도 되는가' 를 "
        "모르는 채로 실거래를 켜게 됩니다.")


def test_the_screen_looks_different_while_real_money_moves():
    """모의와 실거래가 같은 화면이면 어느 쪽인지 헷갈리는 순간이 옵니다."""
    assert "body.live" in HTML, "실거래 상태를 나타내는 스타일이 없습니다"
    assert 'classList.toggle("live"' in SCRIPT, "실거래 표시를 켜는 곳이 없습니다"
    summary = re.search(r"function renderRunSummary\(running\) \{(.*?)\n\}", SCRIPT, re.S).group(1)
    assert "botState" in summary, (
        "실거래 여부를 선택기에서 읽습니다 — 봇이 도는 동안 그 값은 돌고 있는 "
        "전략이 아닙니다.")


def test_a_manual_order_does_not_wait_for_the_next_bar():
    """일봉 전략에서 "다음 봉" 은 내일입니다. 그건 수동매매가 아닙니다."""
    engine = Path("quant/core/engine.py").read_text(encoding="utf-8")
    trader = Path("quant/live/trader.py").read_text(encoding="utf-8")
    assert "async def flush_manual" in engine, "수동 대기열만 비우는 길이 없습니다"
    assert "flush_manual" in trader, "라이브 루프가 수동 대기열을 안 비웁니다"
    body = re.search(r"async def _sleep_serving_manual\(self.*?\n    async def",
                     trader, re.S)
    assert body, "봉을 기다리는 동안 수동 주문을 내보내는 곳이 없습니다"
    assert "MANUAL_FLUSH_S" in body.group(0), "비우는 주기가 정해져 있지 않습니다"


def test_a_manual_order_still_passes_the_daily_caps():
    """"내가 직접 낸다" 가 "한도를 무시한다" 는 아닙니다."""
    engine = Path("quant/core/engine.py").read_text(encoding="utf-8")
    body = re.search(r"async def flush_manual\(self\).*?\n    async def",
                     engine, re.S).group(0)
    assert "_submit" in body, (
        "수동 주문이 브로커의 가드를 건너뛰고 나갑니다 — 하루 한도도, "
        "주문당 상한도, 중복창도 타지 않습니다.")
