"""잔고와 설정이 어긋나서 **신규 진입이 한 건도 못 나가는** 상태를 말합니다.

이건 오류가 아닙니다. 예외도 없고, 거절 로그도 없고, 봇은 멀쩡히 돌고,
손절과 청산은 그대로 나갑니다. 그냥 아무것도 안 삽니다. 그래서 화면에는
"실행 중" 만 뜨고, 사용자는 그것이 조용한 장인지 고장인지 구분할 방법이
없는 채로 하루를 기다립니다 — 실제로 그렇게 끝납니다.

`quant validate` 도 같은 산수를 하지만 거기서는 잔고를 모릅니다. 사이징이
읽는 것은 설정의 `starting_cash` 가 아니라 증권사가 말하는 진짜 평가액이라,
증권사에 붙은 **뒤에** 다시 재야 합니다.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from quant.config.preflight import sizing_alarm
from quant.config.schema import StrategyConfig

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "quant" / "api" / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "quant" / "api" / "static" / "app.css").read_text(encoding="utf-8")
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.S))


def _whole_fn(name: str) -> str:
    match = re.search(rf"((?:async )?function {name}\([^)]*\) \{{.*?\n\}})", SCRIPT, re.S)
    assert match, f"{name} 함수를 찾지 못했습니다"
    return match.group(1)


def _engine() -> str | None:
    jsc = ("/System/Library/Frameworks/JavaScriptCore.framework"
           "/Versions/A/Helpers/jsc")
    return shutil.which("node") or (jsc if Path(jsc).exists() else None)


def cfg(**kw) -> StrategyConfig:
    base = {
        "name": "t", "mode": "live",
        "portfolio": {"max_position_weight": 0.30, "cash_reserve_pct": 0.05,
                      "base_currency": "USD"},
        "execution": {"min_order_notional": 200},
        "broker": {"type": "toss", "max_order_notional": 300,
                   "live_trading_confirmed": True},
        "limits": {"max_daily_notional": 500},
    }
    for section, values in kw.items():
        base[section] = ({**base[section], **values}
                         if isinstance(base.get(section), dict) else values)
    return StrategyConfig(**base)


# ── 산수 ─────────────────────────────────────────────────────────────────
def test_inside_the_window_says_nothing():
    assert sizing_alarm(cfg(), 900) is None


def test_too_small_an_account_says_entries_are_skipped():
    note = sizing_alarm(cfg(), 400)
    assert note and "나가지 않습니다" in note
    assert "114" in note                      # 400 * 0.30 * 0.95 = 114
    assert "min_order_notional" in note


def test_too_large_an_account_says_entries_are_refused():
    note = sizing_alarm(cfg(), 5_000)
    assert note and "전부 거절됩니다" in note
    assert "max_order_notional" in note


def test_both_messages_say_that_exits_still_go_out():
    """이 문장이 없으면 "지금 포지션에 갇혔다" 로 읽힙니다. 사실이 아니고,
    그렇게 읽은 사람은 증권사 앱에서 손으로 팝니다."""
    for equity in (400, 5_000):
        assert "청산과 손절은 그대로 나갑니다" in sizing_alarm(cfg(), equity)


def test_an_unknown_or_empty_equity_says_nothing():
    """모르는 것을 경고로 만들면, 진짜일 때 아무도 안 읽습니다."""
    assert sizing_alarm(cfg(), 0) is None
    assert sizing_alarm(cfg(), -1) is None


def test_the_paper_broker_raises_no_alarm_at_any_size():
    """주문당 상한은 실제 어댑터에만 있습니다."""
    paper = cfg(mode="backtest",
                broker={"type": "paper", "live_trading_confirmed": False})
    for equity in (1, 400, 5_000, 10_000_000):
        assert sizing_alarm(paper, equity) is None


# ── 봇이 그 값을 들고 있는가 ─────────────────────────────────────────────
def test_the_trader_exposes_it_and_starts_clean(tmp_path):
    """시작 전에는 null 입니다 — 아직 잔고를 못 봤으니 할 말이 없습니다.
    모르는 것을 경고로 내보내면 화면이 첫 폴링마다 거짓말을 합니다."""
    from quant.live.trader import LiveTrader

    trader = LiveTrader(cfg(mode="dry_run",
                            broker={"type": "paper",
                                    "live_trading_confirmed": False},
                            universe={"symbols": [{"ticker": "AAA", "venue": "SIM"}]},
                            alpha=[{"type": "ema_cross"}]),
                        state_path=str(tmp_path / "s.db"))
    assert "sizing_alarm" in trader.status()
    assert trader.status()["sizing_alarm"] is None

    source = Path(ROOT / "quant" / "live" / "trader.py").read_text(encoding="utf-8")
    assert '"sizing_alarm": self.sizing_alarm' in source, (
        "status() 가 이 값을 내보내지 않으면 화면은 이유를 알 수 없습니다")
    assert "self.sizing_alarm = sizing_alarm(cfg, portfolio.equity)" in source, (
        "설정이 아니라 **실계좌 평가액** 으로 재야 합니다 — 그게 validate 와 "
        "다른 유일한 점입니다")


# ── 화면이 그것을 말하는가 ───────────────────────────────────────────────
def test_the_banner_has_a_state_that_is_not_an_error():
    """손절이 그대로 나가는 상태를 붉게 칠하면 화면이 거짓말을 합니다."""
    assert '.run-safety[data-state="warn"]' in CSS
    assert "sizing_alarm" in SCRIPT


@pytest.mark.skipif(_engine() is None, reason="JavaScript 엔진이 없습니다")
def test_the_running_bot_that_cannot_buy_says_so_instead_of_running_normally():
    engine = _engine()
    source = _whole_fn("renderRunSummary") + r"""
var written = {};
var body = {classList: {toggle: function () {}}};
var nodes = {};
function node(id) {
  if (!nodes[id]) nodes[id] = {id: id, dataset: {},
    set textContent(v) { written[id] = String(v); },
    get textContent() { return written[id] || ""; }};
  return nodes[id];
}
var document = {body: body, getElementById: node};
function $(sel) { return node(sel); }
function note() {}
function stLabel() { return "전략"; }
function fmtNum(v) { return String(v); }
function shownStrategy() { return {mode: "live", limits: {}}; }
var lastHaltReason = "";
var botState = null;

function render(state) {
  nodes = {}; written = {}; lastHaltReason = "";
  botState = state;
  renderRunSummary(true);
  return {state: node("#runSafety").dataset.state,
          label: written["#runSafetyLabel"] || "",
          text: written["#runSafetyText"] || ""};
}

var healthy  = render({running: true, mode: "live"});
var blocked  = render({running: true, mode: "live",
                       sizing_alarm: "신규 진입이 전부 거절됩니다 — 어쩌고"});
var upkeep   = render({running: true, mode: "live", maintenance_error: "호가 실패",
                       sizing_alarm: "신규 진입이 전부 거절됩니다 — 어쩌고"});
var write = (typeof console !== "undefined" && console.log) ? console.log : print;
write(JSON.stringify({healthy: healthy, blocked: blocked, upkeep: upkeep}));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(source)
        path = Path(handle.name)
    try:
        result = subprocess.run([engine, str(path)], capture_output=True,
                                text=True, timeout=30, check=True)
    finally:
        path.unlink(missing_ok=True)
    out = json.loads(result.stdout.strip().splitlines()[-1])

    # 평소에는 아무것도 달라지지 않습니다.
    assert out["healthy"]["state"] == "live"
    assert "신규 진입" not in out["healthy"]["label"]

    # 못 사는 상태는 라벨과 본문 양쪽에서 말합니다 — 그리고 오류색이 아닙니다.
    assert out["blocked"]["state"] == "warn"
    assert "신규 진입 안 나감" in out["blocked"]["label"]
    assert out["blocked"]["text"] == "신규 진입이 전부 거절됩니다 — 어쩌고"

    # 더 급한 것이 있으면 그쪽이 이깁니다. 손절이 평가되지 않는 것은
    # 못 사는 것보다 급합니다.
    assert out["upkeep"]["state"] == "error"
    assert "점검 주기가 실패" in out["upkeep"]["text"]
