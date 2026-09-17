"""시세를 못 받았을 때, **왜** 못 받았는지가 사람에게 닿는가.

`gather_history` 는 실패를 빈 목록으로 바꿉니다 — 한 종목이 안 읽혔다고
나머지를 버릴 이유가 없으니 그건 맞습니다. 그런데 이유는 `log.warning` 에만
남았습니다. 사람은 로그를 읽지 않습니다.

그래서 화면에 닿는 문장은 이것뿐이었습니다:

    시세를 받지 못해 시작할 수 없습니다 (AAPL, MSFT). 증권사 키가 맞는지…

키는 멀쩡한데 키를 의심하러 가게 됩니다. 실제 이유가 "이 환경은 해외 시세를
제공하지 않는다" 였다면, 그 사람은 하루를 키 재발급에 씁니다.

**일부만 빠지는 쪽이 더 조용합니다.** 8종목 중 6이 빠져도 봇은 돌고, 화면은
"가동 중" 입니다. 그런데 후보가 `min_universe` 아래로 내려가면 상대강도
알파는 발화를 멈춥니다 — 화면에서 그건 "대기 중" 과 구별되지 않습니다.
"""
from __future__ import annotations

import asyncio

from quant.core.events import EventType
from quant.live.trader import LiveTrader
from tests.test_shutdown_and_reporting import TICKERS, live_config

WHY = "KIS 해외 시세를 읽지 못했습니다 (AAPL): 없는 종목. paper=True"


class _Provider:
    """일부 종목에서만 터지는 제공자."""

    def __init__(self, breaks, bars=60):
        self.breaks = breaks
        self.bars = bars

    async def history(self, symbol, timeframe, start, end):
        if symbol.ticker in self.breaks:
            raise RuntimeError(WHY)
        from datetime import timedelta

        from quant.core.types import Bar
        step = timedelta(days=1)
        return [Bar(symbol, start + step * i, 100.0, 101.0, 99.0, 100.0, 1000.0,
                    timeframe) for i in range(1, self.bars + 1)]

    async def quote(self, symbol):
        return None

    async def close(self):
        pass


def warm(breaks, tmp_path) -> tuple[Exception | None, list[dict]]:
    trader = LiveTrader(live_config(), state_path=str(tmp_path / "w.db"))
    trader.provider = _Provider(breaks)
    seen: list[dict] = []
    trader.engine.ctx.bus.on(EventType.ERROR, lambda event: seen.append(event.payload))

    async def go():
        try:
            await trader.warmup()
        except Exception as exc:          # noqa: BLE001 — 문장을 보려는 것입니다
            return exc
        return None

    return asyncio.run(go()), seen


# ── 전부 실패 ────────────────────────────────────────────────────────────
def test_the_startup_error_carries_the_reason_the_venue_gave(tmp_path):
    error, _events = warm(TICKERS, tmp_path)
    assert error is not None, "전부 실패했는데 시작했습니다"
    assert "시세를 받지 못해 시작할 수 없습니다" in str(error)
    assert "해외 시세를 읽지 못했습니다" in str(error), (
        "증권사가 말한 이유가 사라졌습니다 — 사람은 멀쩡한 키를 의심하러 갑니다")


def test_the_reason_does_not_replace_the_advice(tmp_path):
    """이유가 붙었다고 "키를 확인하세요" 를 지우면, 진짜 키 문제일 때
    무엇을 해야 하는지가 없어집니다."""
    error, _ = warm(TICKERS, tmp_path)
    assert "증권사 키가 맞는지" in str(error)


def test_the_same_reason_is_not_repeated_once_per_symbol(tmp_path):
    """8종목이 같은 이유로 실패하면 같은 문장이 8번 붙습니다 — 그러면
    아무도 안 읽습니다."""
    error, _ = warm(TICKERS, tmp_path)
    assert str(error).count("paper=True") <= 2


# ── 일부만 실패 ──────────────────────────────────────────────────────────
def test_a_partial_loss_is_announced_not_just_logged(tmp_path):
    error, events = warm(TICKERS[:1], tmp_path)
    assert error is None, "일부만 빠졌는데 시작을 막았습니다"
    said = [e for e in events if "후보에서 빠졌습니다" in str(e.get("error", ""))]
    assert said, f"빠진 종목을 아무도 말하지 않았습니다: {events}"
    assert "해외 시세를 읽지 못했습니다" in said[0]["error"]
    assert said[0]["universe_size"] == len(TICKERS) - 1


def test_the_announcement_names_which_symbols_went_missing(tmp_path):
    _error, events = warm(TICKERS[:1], tmp_path)
    payload = [e for e in events if "후보에서 빠졌습니다" in str(e.get("error", ""))][0]
    assert TICKERS[0] in payload["error"]
    assert payload["warmup_failures"][TICKERS[0]] == WHY


def test_nothing_is_said_when_nothing_is_missing(tmp_path):
    """멀쩡히 돈 시작마다 경고를 띄우면 그 경고는 읽히지 않게 됩니다."""
    error, events = warm([], tmp_path)
    assert error is None
    assert not [e for e in events if "후보에서 빠졌습니다" in str(e.get("error", ""))]


# ── gather_history 계약 ──────────────────────────────────────────────────
def test_gather_history_still_returns_empty_for_the_failed_symbol(tmp_path):
    """이유를 모으는 것이 "실패하면 멈춘다" 로 바뀌면 안 됩니다 — 한 종목
    때문에 나머지를 버릴 이유가 없습니다."""
    from datetime import datetime, timedelta

    from quant.core.types import UTC, Symbol
    from quant.data.provider import gather_history

    symbols = [Symbol("AAA", venue="SIM"), Symbol("BBB", venue="SIM")]
    end = datetime(2026, 9, 17, tzinfo=UTC)
    failures: dict[str, str] = {}
    out = asyncio.run(gather_history(_Provider({"AAA"}), symbols, "1d",
                                     end - timedelta(days=90), end,
                                     failures=failures))
    assert out[symbols[0].key] == [] and out[symbols[1].key]
    assert failures[symbols[0].key] == WHY
    assert symbols[1].key not in failures


def test_the_failures_dict_is_optional():
    """기존 호출자를 깨지 않습니다."""
    from datetime import datetime, timedelta

    from quant.core.types import UTC, Symbol
    from quant.data.provider import gather_history

    end = datetime(2026, 9, 17, tzinfo=UTC)
    out = asyncio.run(gather_history(_Provider({"AAA"}), [Symbol("AAA", venue="SIM")],
                                     "1d", end - timedelta(days=90), end))
    assert out == {"SIM:AAA": []}
