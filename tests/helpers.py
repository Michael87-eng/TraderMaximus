"""Shared fixtures for the test suite."""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from tradermaximus.core.types import AssetClass, Candle, Instrument, Timeframe
from tradermaximus.data.series import CandleSeries

BASE = datetime(2020, 1, 1, tzinfo=timezone.utc)

EQUITY = Instrument("TEST", AssetClass.EQUITIES, "NASDAQ", "Technology", 0.01, 1.0, 1.0)
CRYPTO = Instrument("BTCUSD", AssetClass.CRYPTO, "BINANCE", "Crypto", 0.01, 0.0001, 0.0001)


def candle(index: int, close: float, *, volume: float = 1_000_000.0, spread: float = 0.01) -> Candle:
    """One well-formed bar around ``close``."""
    return Candle(
        timestamp=BASE + timedelta(days=index),
        open=close * (1 - spread / 2),
        high=close * (1 + spread),
        low=close * (1 - spread),
        close=close,
        volume=volume,
    )


def random_walk(
    n: int = 400,
    *,
    seed: int = 1,
    drift: float = 0.0008,
    volatility: float = 0.012,
    start: float = 100.0,
    calm_tail: int = 0,
    calm_factor: float = 0.35,
) -> list[Candle]:
    """A reproducible price series.

    ``calm_tail`` reduces volatility over the final N bars, which is how a
    low-volatility base before a breakout is simulated.
    """
    rng = random.Random(seed)
    out: list[Candle] = []
    price = start
    for i in range(n):
        vol = volatility * calm_factor if (calm_tail and i >= n - calm_tail) else volatility
        price = max(1.0, price * (1 + rng.gauss(drift, vol)))
        out.append(
            Candle(
                timestamp=BASE + timedelta(days=i),
                open=price * (1 + rng.gauss(0, vol / 4)),
                high=price * (1 + abs(rng.gauss(0, vol))),
                low=price * (1 - abs(rng.gauss(0, vol))),
                close=price,
                volume=1_200_000 * rng.uniform(0.7, 1.4),
            )
        )
    return out


def with_breakout(candles: list[Candle], *, volume_multiple: float = 2.0) -> list[Candle]:
    """Append a high-volume bar closing above the prior 20-bar high."""
    high = max(c.high for c in candles[-20:])
    previous = candles[-1]
    return candles + [
        Candle(
            timestamp=previous.timestamp + timedelta(days=1),
            open=previous.close * 1.002,
            high=high * 1.03,
            low=previous.close,
            close=high * 1.025,
            volume=previous.volume * volume_multiple,
        )
    ]


def series(candles: list[Candle], instrument: Instrument = EQUITY) -> CandleSeries:
    return CandleSeries.build(instrument, Timeframe.D1, candles)


def trending(n: int = 400, *, seed: int = 1, slope: float = 0.0012) -> list[Candle]:
    """A smooth low-volatility uptrend with mild oscillation."""
    out: list[Candle] = []
    price = 100.0
    for i in range(n):
        price = price * (1 + slope) * (1 + 0.0008 * math.sin(i / 3.0))
        out.append(
            Candle(
                timestamp=BASE + timedelta(days=i),
                open=price * 0.999,
                high=price * 1.004,
                low=price * 0.996,
                close=price,
                volume=1_000_000 * (1.15 if i % 3 else 0.85),
            )
        )
    return out
