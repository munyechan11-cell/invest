"""KIS history must honor the DataProvider closed-candle contract."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from quant.core.types import UTC, Symbol
from quant.data.providers.kis import KisProvider


@pytest.mark.asyncio
async def test_kis_drops_the_current_still_forming_daily_row():
    now = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)
    symbol = Symbol("005930", venue="kis", quote_currency="KRW")
    provider = KisProvider.__new__(KisProvider)
    calls = 0

    async def fake_get(_path, _tr_id, _params):
        nonlocal calls
        calls += 1
        if calls > 1:
            return {"output2": []}
        return {"output2": [
            {
                "stck_bsop_date": "20260901",
                "stck_oprc": "100", "stck_hgpr": "200",
                "stck_lwpr": "50", "stck_clpr": "180", "acml_vol": "99",
            },
            {
                "stck_bsop_date": "20260831",
                "stck_oprc": "90", "stck_hgpr": "101",
                "stck_lwpr": "89", "stck_clpr": "100", "acml_vol": "10",
            },
        ]}

    provider._get = fake_get

    bars = await provider.history(symbol, "1d", now - timedelta(days=5), now)

    assert [bar.ts.date().isoformat() for bar in bars] == ["2026-08-31"]
    assert all(bar.end_ts <= now for bar in bars)
