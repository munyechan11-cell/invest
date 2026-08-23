"""Local file providers: CSV / Parquet directories, plus a synthetic generator.

The CSV provider is the reproducibility anchor — pin a dataset to disk and a
backtest becomes bit-for-bit repeatable regardless of what a vendor rewrites.
"""
from __future__ import annotations

import csv
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

from quant.core.types import UTC, Bar, Symbol, timeframe_delta
from quant.data.provider import DataProvider, register_provider


def _parse_ts(raw: str | float | int) -> datetime:
    if isinstance(raw, (int, float)) or str(raw).isdigit():
        v = float(raw)
        if v > 1e11:      # milliseconds
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=UTC)
    text = str(raw).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@register_provider("csv")
class CsvProvider(DataProvider):
    """Reads `{directory}/{ticker}-{timeframe}.csv` with an OHLCV header.

    Accepted column spellings cover the usual exports: date/time/timestamp,
    open/high/low/close/volume (case-insensitive).
    """

    name = "csv"

    def __init__(self, directory: str = "data", pattern: str = "{ticker}-{timeframe}.csv"):
        self.directory = Path(directory)
        self.pattern = pattern
        self._cache: dict[tuple[str, str], list[Bar]] = {}

    def _path(self, symbol: Symbol, timeframe: str) -> Path:
        safe = symbol.ticker.replace("/", "_").replace(":", "_")
        return self.directory / self.pattern.format(ticker=safe, timeframe=timeframe)

    def _load(self, symbol: Symbol, timeframe: str) -> list[Bar]:
        key = (symbol.key, timeframe)
        if key in self._cache:
            return self._cache[key]
        path = self._path(symbol, timeframe)
        if not path.exists():
            raise FileNotFoundError(f"no data file for {symbol} {timeframe}: {path}")
        bars: list[Bar] = []
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            cols = {c.lower().strip(): c for c in (reader.fieldnames or [])}
            ts_col = next((cols[c] for c in ("timestamp", "date", "time", "datetime") if c in cols), None)
            if ts_col is None:
                raise ValueError(f"{path}: no timestamp column")
            for row in reader:
                try:
                    bars.append(
                        Bar(
                            symbol=symbol,
                            ts=_parse_ts(row[ts_col]),
                            open=float(row[cols["open"]]),
                            high=float(row[cols["high"]]),
                            low=float(row[cols["low"]]),
                            close=float(row[cols["close"]]),
                            volume=float(row.get(cols.get("volume", ""), 0) or 0),
                            timeframe=timeframe,
                        )
                    )
                except (KeyError, ValueError):
                    continue        # skip malformed rows rather than abort a backtest
        bars.sort(key=lambda b: b.ts)
        self._cache[key] = bars
        return bars

    async def history(self, symbol, timeframe, start, end):
        return [b for b in self._load(symbol, timeframe) if start <= b.ts < end]


@register_provider("synthetic")
class SyntheticProvider(DataProvider):
    """Seeded GBM with a configurable trend/vol regime.

    Used by the test-suite and by `quant demo` so the whole stack is runnable
    with no API keys at all.

    The path is anchored to a fixed epoch and cached, so `history(a, b)` is
    always a slice of one canonical series. Generating from the requested start
    instead would make a warm-up window silently produce a *different* price
    path than the test window — which would quietly invalidate every
    walk-forward fold and every warm-up comparison.
    """

    name = "synthetic"
    EPOCH = datetime(2015, 1, 1, tzinfo=UTC)

    def __init__(
        self,
        seed: int = 7,
        start_price: float = 100.0,
        annual_drift: float = 0.08,
        annual_vol: float = 0.35,
        regime_switch: bool = True,
    ):
        self.seed = seed
        self.start_price = start_price
        self.drift = annual_drift
        self.vol = annual_vol
        self.regime_switch = regime_switch
        self._paths: dict[tuple[str, str], list[Bar]] = {}

    def _path(self, symbol: Symbol, timeframe: str, until: datetime) -> list[Bar]:
        key = (symbol.key, timeframe)
        cached = self._paths.get(key)
        if cached and cached[-1].ts >= until:
            return cached

        step = timeframe_delta(timeframe)
        per_year = 365 * 24 * 3600 / step.total_seconds()
        rng = random.Random(f"{self.seed}:{symbol.key}:{timeframe}")
        dt = 1.0 / per_year
        price = self.start_price * (1 + rng.uniform(-0.3, 0.3))
        drift, vol = self.drift, self.vol
        bars: list[Bar] = []
        ts = self.EPOCH
        i = 0
        while ts <= until:
            if self.regime_switch and i % 180 == 0:
                # flip between trending and choppy so strategies cannot overfit
                # one regime and look brilliant
                drift = self.drift * rng.choice([-1.5, -0.5, 0.5, 1.0, 2.0])
                vol = self.vol * rng.uniform(0.6, 1.8)
            shock = rng.gauss(0, 1)
            ret = (drift - 0.5 * vol**2) * dt + vol * math.sqrt(dt) * shock
            open_ = price
            price = max(price * math.exp(ret), 0.01)
            hi = max(open_, price) * (1 + abs(rng.gauss(0, 0.002)))
            lo = min(open_, price) * (1 - abs(rng.gauss(0, 0.002)))
            bars.append(
                Bar(symbol, ts, open_, hi, lo, price,
                    volume=abs(rng.gauss(1_000_000, 250_000)), timeframe=timeframe)
            )
            ts += step
            i += 1
        self._paths[key] = bars
        return bars

    async def history(self, symbol, timeframe, start, end):
        return [b for b in self._path(symbol, timeframe, end) if start <= b.ts < end]

    async def resolve(self, ticker: str):
        return Symbol(ticker.upper(), venue="SIM")
