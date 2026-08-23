"""Realistic synthetic market generator for calibration work.

Calibrating thresholds against unrealistic data is worse than not calibrating
at all: it tunes the system to an artefact of the generator and then ships
that tuning to real markets.

The first version of this generator drew volume from ``uniform(0.6, 1.8)``.
That distribution has a hard ceiling: relative volume — today's volume over its
50-day average — could never exceed about 1.35. Every volume-gated condition in
the library (``> 1.4`` for breakouts, ``> 2.0`` for gaps) therefore failed
100% of the time, and the obvious "fix" would have been to lower thresholds
that are in fact correct.

What this generator models instead, because each property is load-bearing for
some strategy the library contains:

* **Lognormal volume with a fat right tail.** Real daily volume relative to its
  average is approximately lognormal. 2× days are common.
* **Volume correlated with price movement.** Big moves come on big volume.
  Without this, "breakout *with* volume confirmation" can never co-occur, and
  the conjunction looks impossible when it is merely conditional.
* **Volume dry-up during consolidation.** The precondition every base pattern
  in the CAN SLIM family requires.
* **Overnight gaps.** Occasional, sized like earnings reactions. The gap
  strategy is untestable without them.
* **Regime persistence.** Trends and ranges arrive in runs, not bar by bar, so
  the regime engine sees something it can actually classify.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from tradermaximus.core.types import Candle

BASE = datetime(2018, 1, 1, tzinfo=timezone.utc)

#: Lognormal parameters for daily volume relative to its own average.
#: mu = -sigma^2/2 keeps the mean at 1.0; sigma = 0.42 puts ~16% of days above
#: 1.4x and ~3% above 2.0x, which is the right order for liquid equities.
_VOLUME_SIGMA = 0.42
_VOLUME_MU = -(_VOLUME_SIGMA ** 2) / 2.0


@dataclass(frozen=True, slots=True)
class MarketProfile:
    """Parameters describing one market character."""

    name: str
    drift: float
    volatility: float
    #: Length of a trend/rest cycle in bars; 0 disables cycling.
    cycle_bars: int = 0
    #: Multiplier applied to drift during the rest phase of a cycle.
    rest_factor: float = -0.7
    #: Fraction of the cycle spent advancing.
    advance_fraction: float = 0.72
    #: Probability per bar of an overnight gap event.
    gap_probability: float = 0.012
    mean_reversion: float = 0.0
    start_price: float = 100.0
    base_volume: float = 1_200_000.0


PROFILES: dict[str, MarketProfile] = {
    # A leader-like advance: sustained trend, real pullbacks, volume dry-up in
    # the rests and expansion on the advances.
    "uptrend": MarketProfile("uptrend", drift=0.0015, volatility=0.013, cycle_bars=55),
    # Sideways, mean-reverting: what the reversal and mean-reversion specs want.
    "range": MarketProfile("range", drift=0.0, volatility=0.011, mean_reversion=0.03),
    # High volatility with weak drift.
    "volatile": MarketProfile("volatile", drift=0.0006, volatility=0.026, gap_probability=0.02),
    # Sustained decline: everything long should stand down here.
    "bear": MarketProfile("bear", drift=-0.0012, volatility=0.017, start_price=200.0),
    # Broad market benchmark: calmer than any single name.
    "benchmark": MarketProfile("benchmark", drift=0.0008, volatility=0.008, cycle_bars=70),
}


def generate(profile: MarketProfile | str, bars: int, seed: int) -> list[Candle]:
    """Generate ``bars`` daily candles for one market character."""
    if isinstance(profile, str):
        profile = PROFILES[profile]
    rng = random.Random(seed)

    candles: list[Candle] = []
    price = profile.start_price

    for index in range(bars):
        # -- direction ------------------------------------------------------
        phase_advancing = True
        drift = profile.drift
        if profile.cycle_bars:
            position = index % profile.cycle_bars
            phase_advancing = position < profile.cycle_bars * profile.advance_fraction
            drift = profile.drift * (1.0 if phase_advancing else profile.rest_factor)

        shock = rng.gauss(drift, profile.volatility)
        if profile.mean_reversion:
            shock += (profile.start_price - price) / profile.start_price * profile.mean_reversion

        previous_close = price
        # -- overnight gap ---------------------------------------------------
        gap = 0.0
        if rng.random() < profile.gap_probability:
            # Earnings-sized reaction, skewed to the upside in an uptrend.
            magnitude = abs(rng.gauss(0.045, 0.02))
            direction = 1 if rng.random() < (0.62 if profile.drift > 0 else 0.4) else -1
            gap = direction * magnitude

        open_price = max(0.01, previous_close * (1.0 + gap))
        close_price = max(0.01, open_price * (1.0 + shock))
        price = close_price

        move = abs(close_price - previous_close) / previous_close if previous_close > 0 else 0.0

        # -- volume ----------------------------------------------------------
        # Lognormal base, amplified by how far the bar moved, damped during the
        # rest phase of a cycle so that bases show genuine dry-up.
        base_multiple = math.exp(rng.gauss(_VOLUME_MU, _VOLUME_SIGMA))
        move_amplification = 1.0 + 3.0 * (move / max(profile.volatility, 1e-9)) * 0.25
        phase_damping = 1.0 if phase_advancing else 0.62
        if abs(gap) > 0:
            move_amplification *= 1.8  # gaps trade heavily
        volume = profile.base_volume * base_multiple * move_amplification * phase_damping

        # -- wicks -----------------------------------------------------------
        wick_up = abs(rng.gauss(0, profile.volatility * 0.6))
        wick_down = abs(rng.gauss(0, profile.volatility * 0.6))
        high = max(open_price, close_price) * (1.0 + wick_up)
        low = min(open_price, close_price) * (1.0 - wick_down)

        candles.append(
            Candle(
                timestamp=BASE + timedelta(days=index),
                open=round(open_price, 4),
                high=round(high, 4),
                low=round(max(0.01, low), 4),
                close=round(close_price, 4),
                volume=round(max(1.0, volume), 2),
            )
        )
    return candles


def make(character: str, bars: int, seed: int) -> list[Candle]:
    return generate(PROFILES[character], bars, seed)


CHARACTERS: tuple[str, ...] = ("uptrend", "range", "volatile", "bear")


def describe(candles: list[Candle]) -> dict[str, float]:
    """Summary statistics, used to confirm the generator is realistic."""
    volumes = [c.volume for c in candles]
    relative = [
        volumes[i] / (sum(volumes[i - 50:i]) / 50.0)
        for i in range(50, len(volumes))
        if sum(volumes[i - 50:i]) > 0
    ]
    closes = [c.close for c in candles]
    gaps = [
        (candles[i].open - closes[i - 1]) / closes[i - 1]
        for i in range(1, len(candles))
        if closes[i - 1] > 0
    ]
    return {
        "bars": len(candles),
        "total_return": (closes[-1] - closes[0]) / closes[0],
        "rel_volume_max": max(relative) if relative else 0.0,
        "pct_above_1.4x": sum(1 for r in relative if r > 1.4) / len(relative) if relative else 0.0,
        "pct_above_2.0x": sum(1 for r in relative if r > 2.0) / len(relative) if relative else 0.0,
        "pct_below_0.7x": sum(1 for r in relative if r < 0.7) / len(relative) if relative else 0.0,
        "pct_gap_over_3pct": sum(1 for g in gaps if abs(g) > 0.03) / len(gaps) if gaps else 0.0,
    }
