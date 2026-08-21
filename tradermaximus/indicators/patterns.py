"""Candlestick pattern recognition (spec section 2.2, strategy 6.2.4).

Each detector takes the trailing candles it needs and returns a
:class:`PatternMatch` with a 0-1 strength, or ``None``. Strength reflects how
textbook the formation is (shadow-to-body ratios, engulfing margin), letting
the scoring engine prefer clean patterns over marginal ones.

Patterns are *context-free* here: "is this a hammer" is answered without asking
whether it sits at support. The strategy layer supplies that context, because a
hammer in mid-range is noise while the same shape at a tested level is a
signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from ..core.types import Candle


@dataclass(frozen=True, slots=True)
class PatternMatch:
    name: str
    bullish: bool
    strength: float  # 0-1
    bars: int

    def __str__(self) -> str:
        direction = "bullish" if self.bullish else "bearish"
        return f"{self.name} ({direction}, strength={self.strength:.2f})"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _is_doji(candle: Candle, threshold: float = 0.1) -> bool:
    return candle.range > 0 and candle.body / candle.range <= threshold


# ---------------------------------------------------------------------------
# Single-bar patterns
# ---------------------------------------------------------------------------


def hammer(candles: Sequence[Candle]) -> PatternMatch | None:
    """Long lower shadow, small body near the top — rejection of lower prices."""
    if not candles:
        return None
    c = candles[-1]
    if c.range <= 0 or c.body <= 0:
        return None
    if c.lower_shadow < 2 * c.body:
        return None
    if c.upper_shadow > c.body:
        return None
    strength = _clamp((c.lower_shadow / c.range) * (1.0 - c.upper_shadow / c.range))
    return PatternMatch("hammer", True, strength, 1)


def shooting_star(candles: Sequence[Candle]) -> PatternMatch | None:
    """Hammer's mirror image: long upper shadow rejecting higher prices."""
    if not candles:
        return None
    c = candles[-1]
    if c.range <= 0 or c.body <= 0:
        return None
    if c.upper_shadow < 2 * c.body:
        return None
    if c.lower_shadow > c.body:
        return None
    strength = _clamp((c.upper_shadow / c.range) * (1.0 - c.lower_shadow / c.range))
    return PatternMatch("shooting_star", False, strength, 1)


def hanging_man(candles: Sequence[Candle]) -> PatternMatch | None:
    """Hammer shape after an advance — bearish only with prior uptrend context."""
    if len(candles) < 4:
        return None
    match = hammer(candles)
    if match is None:
        return None
    prior = candles[-4:-1]
    if not all(prior[i].close <= prior[i + 1].close for i in range(len(prior) - 1)):
        return None
    return PatternMatch("hanging_man", False, match.strength * 0.9, 1)


def doji(candles: Sequence[Candle]) -> PatternMatch | None:
    """Indecision. Neutral by construction, reported as non-directional."""
    if not candles:
        return None
    c = candles[-1]
    if not _is_doji(c):
        return None
    return PatternMatch("doji", True, _clamp(1.0 - c.body / c.range if c.range else 0.0), 1)


# ---------------------------------------------------------------------------
# Two-bar patterns
# ---------------------------------------------------------------------------


def bullish_engulfing(candles: Sequence[Candle]) -> PatternMatch | None:
    if len(candles) < 2:
        return None
    prev, curr = candles[-2], candles[-1]
    if prev.is_bullish or not curr.is_bullish:
        return None
    if not (curr.close > prev.open and curr.open < prev.close):
        return None
    if prev.body <= 0:
        return None
    strength = _clamp(curr.body / (prev.body * 2.0))
    return PatternMatch("bullish_engulfing", True, strength, 2)


def bearish_engulfing(candles: Sequence[Candle]) -> PatternMatch | None:
    if len(candles) < 2:
        return None
    prev, curr = candles[-2], candles[-1]
    if not prev.is_bullish or curr.is_bullish:
        return None
    if not (curr.close < prev.open and curr.open > prev.close):
        return None
    if prev.body <= 0:
        return None
    strength = _clamp(curr.body / (prev.body * 2.0))
    return PatternMatch("bearish_engulfing", False, strength, 2)


def piercing_pattern(candles: Sequence[Candle]) -> PatternMatch | None:
    """Gap down, then close back above the midpoint of the prior down bar."""
    if len(candles) < 2:
        return None
    prev, curr = candles[-2], candles[-1]
    if prev.is_bullish or not curr.is_bullish:
        return None
    midpoint = (prev.open + prev.close) / 2.0
    if not (curr.open < prev.low and curr.close > midpoint and curr.close < prev.open):
        return None
    penetration = (curr.close - prev.close) / prev.body if prev.body > 0 else 0.0
    return PatternMatch("piercing", True, _clamp(penetration), 2)


def dark_cloud_cover(candles: Sequence[Candle]) -> PatternMatch | None:
    if len(candles) < 2:
        return None
    prev, curr = candles[-2], candles[-1]
    if not prev.is_bullish or curr.is_bullish:
        return None
    midpoint = (prev.open + prev.close) / 2.0
    if not (curr.open > prev.high and curr.close < midpoint and curr.close > prev.open):
        return None
    penetration = (prev.close - curr.close) / prev.body if prev.body > 0 else 0.0
    return PatternMatch("dark_cloud_cover", False, _clamp(penetration), 2)


def harami_cross(candles: Sequence[Candle]) -> PatternMatch | None:
    """A doji contained inside the prior bar's body — momentum stalling."""
    if len(candles) < 2:
        return None
    prev, curr = candles[-2], candles[-1]
    if not _is_doji(curr) or prev.body <= 0:
        return None
    body_high, body_low = max(prev.open, prev.close), min(prev.open, prev.close)
    if not (body_low < curr.open < body_high and body_low < curr.close < body_high):
        return None
    return PatternMatch("harami_cross", not prev.is_bullish, _clamp(prev.body / prev.range if prev.range else 0.0), 2)


# ---------------------------------------------------------------------------
# Three-bar patterns
# ---------------------------------------------------------------------------


def morning_star(candles: Sequence[Candle]) -> PatternMatch | None:
    """Down bar, small-bodied pause, strong up bar closing into the first body."""
    if len(candles) < 3:
        return None
    first, middle, last = candles[-3], candles[-2], candles[-1]
    if first.is_bullish or not last.is_bullish:
        return None
    if middle.body > first.body * 0.5:
        return None
    midpoint = (first.open + first.close) / 2.0
    if last.close <= midpoint:
        return None
    recovery = (last.close - first.close) / first.body if first.body > 0 else 0.0
    return PatternMatch("morning_star", True, _clamp(recovery / 2.0), 3)


def evening_star(candles: Sequence[Candle]) -> PatternMatch | None:
    if len(candles) < 3:
        return None
    first, middle, last = candles[-3], candles[-2], candles[-1]
    if not first.is_bullish or last.is_bullish:
        return None
    if middle.body > first.body * 0.5:
        return None
    midpoint = (first.open + first.close) / 2.0
    if last.close >= midpoint:
        return None
    decline = (first.close - last.close) / first.body if first.body > 0 else 0.0
    return PatternMatch("evening_star", False, _clamp(decline / 2.0), 3)


DETECTORS: tuple[Callable[[Sequence[Candle]], PatternMatch | None], ...] = (
    hammer,
    shooting_star,
    hanging_man,
    doji,
    bullish_engulfing,
    bearish_engulfing,
    piercing_pattern,
    dark_cloud_cover,
    harami_cross,
    morning_star,
    evening_star,
)


def detect_patterns(candles: Sequence[Candle]) -> list[PatternMatch]:
    """Run every detector; strongest first."""
    matches = [m for detector in DETECTORS if (m := detector(candles)) is not None]
    return sorted(matches, key=lambda m: -m.strength)


def bullish_reversal_patterns(candles: Sequence[Candle]) -> list[PatternMatch]:
    return [m for m in detect_patterns(candles) if m.bullish and m.name != "doji"]


def bearish_reversal_patterns(candles: Sequence[Candle]) -> list[PatternMatch]:
    return [m for m in detect_patterns(candles) if not m.bullish]


def strongest_bullish(candles: Sequence[Candle]) -> PatternMatch | None:
    matches = bullish_reversal_patterns(candles)
    return matches[0] if matches else None
