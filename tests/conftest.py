from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolate_process_local_toss_rate_gates():
    """A venue cooldown is process-global in production, not test-global."""
    from quant.brokerage import toss_broker

    toss_broker._TOKENS.clear()
    toss_broker._RATE_GATES.clear()
    toss_broker._AUTH_COOLDOWNS.clear()
    yield
    toss_broker._TOKENS.clear()
    toss_broker._RATE_GATES.clear()
    toss_broker._AUTH_COOLDOWNS.clear()


def shipped_configs(mode=None) -> list:
    """지금 **출하되는** 설정 목록 — 이름을 박아 두지 않습니다.

    설정 하나를 보관 폴더로 옮길 때마다 여러 테스트 파일의 상수를 같이
    고쳐야 했고, 안 고치면 "출하 설정 전부를 검사한다" 던 가드가 없는 파일을
    찾다가 깨졌습니다. 가드가 지켜야 하는 것은 **그때그때 목록에 있는 것들**
    이므로, 목록을 여기서 한 번만 읽습니다.

    `configs/` 를 한 겹만 봅니다 — `strategy_catalog()` 과 같은 규칙이라
    보관 폴더(`configs/archive/`)는 화면에도 여기에도 안 잡힙니다.
    """
    import os

    from quant.config.loader import load_config

    keys = ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_NO",
            "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO",
            "KIS_PAPER_APP_KEY", "KIS_PAPER_APP_SECRET", "KIS_PAPER_ACCOUNT_NO",
            "KIS_ACCOUNT_PRD_CD", "KIS_PAPER_ACCOUNT_PRD_CD",
            "BINANCE_KEY", "BINANCE_SECRET", "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY")
    restore = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.setdefault(k, "x")
    try:
        out = []
        for path in sorted((ROOT / "configs").glob("*.yaml")):
            try:
                config = load_config(str(path))
            except Exception:
                continue          # 전략이 아닌 YAML (space.yaml 등)
            if mode is None or config.mode.value == mode:
                out.append(f"configs/{path.name}")
        return out
    finally:
        for k, value in restore.items():
            if value is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = value


LIVE_CONFIGS = shipped_configs("live")
BACKTEST_CONFIGS = shipped_configs("backtest")
