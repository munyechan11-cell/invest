"""실거래 설정이 **조용히** 아무것도 안 하게 되는 두 경로를 막습니다.

둘 다 예외도 로그도 남기지 않습니다. 화면은 평소와 똑같고, 봇은 돌고 있고,
다만 사야 할 때 사지 않거나 문제가 생겨도 아무도 모릅니다. 실거래에서 가장
비싼 종류의 결함이라, 코드가 아니라 **설정** 을 검사합니다.
"""
import asyncio
import math
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant.alpha.technical import CrossSectionalMomentumAlpha
from quant.cli import _load
from quant.core.account import Portfolio
from quant.core.clock import Clock
from quant.core.context import Context
from quant.core.events import EventBus
from quant.core.types import UTC, Bar, RunMode, Symbol
from quant.data.universe import LimitFilter, StaticSource, UniverseSelector
from tests.conftest import LIVE_CONFIGS  # noqa: E402 — 목록은 한 곳에서

#: 사람이 못 보고 지나가면 돈이 되는 사건들. 체결·청산만 알리는 봇은
#: "조용하다 = 잘 되고 있다" 로 읽히는데, 하루 손실 한도로 멈춘 봇도
#: 똑같이 조용합니다.
MUST_ALERT = {"order_rejected", "error", "state"}


@pytest.fixture
def env(monkeypatch):
    for var in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_NO",
                "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO",
                "BINANCE_KEY", "BINANCE_SECRET", "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_CHAT_ID", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(var, "x")


# ── (1) 말없이 멈추지 않는가 ─────────────────────────────────────────────
@pytest.mark.parametrize("path", LIVE_CONFIGS)
def test_every_live_config_says_when_an_order_is_refused(path, env):
    config = _load(path)
    assert config.mode is RunMode.LIVE, f"{path} 가 더 이상 실거래가 아닙니다"
    missing = MUST_ALERT - set(config.notify.on_events)
    assert not missing, (
        f"{path} 는 {sorted(missing)} 를 알리지 않습니다. 거절·오류·정지를 "
        "말하지 않는 봇은 멈춰 있을 때와 잘 돌 때가 똑같이 조용합니다"
    )


@pytest.mark.parametrize("path", LIVE_CONFIGS)
def test_every_live_config_has_somewhere_to_send_it(path, env):
    config = _load(path)
    assert config.notify.telegram_bot_token and config.notify.telegram_chat_id, (
        f"{path} 에 알림 수신처가 없습니다"
    )


# ── (2) 유니버스 상한이 알파를 굶기지 않는가 ─────────────────────────────
def _limit_caps(config) -> list[int]:
    return [int(f.params["max_symbols"]) for f in config.universe.filters
            if f.type == "limit" and "max_symbols" in f.params]


@pytest.mark.parametrize("path", LIVE_CONFIGS)
def test_the_universe_cap_leaves_the_ranking_something_to_rank(path, env):
    """`limit` 은 순위가 아니라 **위치로** 자릅니다 — 파일 앞쪽 N 개만
    남습니다. 그 N 이 `min_universe` 보다 작으면 상대강도 알파는 단 한 번도
    인사이트를 내지 않고, 그 침묵은 어디에도 기록되지 않습니다."""
    config = _load(path)
    caps = _limit_caps(config)
    if not caps:
        return
    cap = min(caps)
    for spec in config.alpha:
        if spec.type != "xs_momentum":
            continue
        need = int(spec.params.get("min_universe", 6))
        assert cap >= need, (
            f"{path}: universe.limit 이 후보를 {cap} 종목으로 자르는데 "
            f"xs_momentum 은 {need} 종목을 요구합니다 — 이 알파는 영원히 "
            "침묵합니다"
        )
        top_n = int(spec.params.get("top_n", 5))
        assert cap > top_n, (
            f"{path}: 후보 {cap} 개 중 {top_n} 개를 고르는 것은 순위가 아닙니다"
        )


def test_the_universe_selector_truncates_from_the_very_first_bar():
    """상한이 첫 봉부터 걸린다는 사실이, 위 검사를 '언젠가' 가 아니라
    '처음부터' 의 문제로 만듭니다 (`due()` 는 0 % N == 0 입니다)."""
    selector = UniverseSelector(StaticSource([]),
                                filters=[LimitFilter(max_symbols=4)],
                                refresh_every_bars=21)
    assert selector.due() is True


# ── 알파가 실제로 침묵하는가 (설정이 아니라 동작으로) ────────────────────
class _Fixed(Clock):
    def __init__(self, t):
        self._t = t

    def now(self):
        return self._t

    async def sleep_until(self, when):      # pragma: no cover - 안 불립니다
        pass


def _ctx_with(n_symbols: int) -> Context:
    t0 = datetime(2026, 9, 14, tzinfo=UTC)
    ctx = Context(_Fixed(t0), Portfolio(1_000.0), EventBus(), timeframe="1d")
    for i in range(n_symbols):
        sym = Symbol(f"S{i}", venue="toss", tick_size=Decimal("0.01"),
                     lot_size=Decimal("1"))
        price, bars = 100.0, []
        for k in range(200):
            price *= 1.0 + 0.002 * (i + 1) + 0.001 * math.sin(k)
            bars.append(Bar(symbol=sym, ts=t0 - timedelta(days=200 - k),
                            open=price, high=price, low=price, close=price,
                            volume=1_000_000))
        ctx.seed_history(sym, bars)
        ctx.universe.append(sym)
    return ctx


@pytest.mark.parametrize("size,expected", [(8, 4), (6, 4), (5, 0), (4, 0)])
def test_xs_momentum_emits_nothing_below_min_universe(size, expected):
    alpha = CrossSectionalMomentumAlpha(lookback=126, skip=21, top_n=4,
                                        rebalance_every=21, min_universe=6)
    out = asyncio.run(alpha.update(_ctx_with(size), {}))
    assert len(out) == expected
