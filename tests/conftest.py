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
