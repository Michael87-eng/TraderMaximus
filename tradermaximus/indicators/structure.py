"""Market structure: swings, support/resistance, breakouts, bases, Fibonacci.

These are the inputs the CAN SLIM-family strategies need. Base detection here
is deliberately conservative — it reports the geometry it can measure (depth,
length, tightness, handle position, volume dry-up) and leaves the accept/reject
decision to the strategy, which also weighs fundamentals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .core import Number, sma


@dataclass(frozen=True, slots=True)
class SwingPoint:
    index: int
    price: float
    is_high: bool


@dataclass(frozen=True, slots=True)
class Level:
    """A horizontal support/resistance level and how often price respected it."""

    price: float
    touches: int
    is_resistance: bool

    def distance_from(self, price: float) -> float:
        return abs(price - self.price) / price if price > 0 else float("inf")


@dataclass(frozen=True, slots=True)
class BasePattern:
    """A measured consolidation. ``kind`` is the geometry, not a verdict."""

    kind: str
    start_index: int
    end_index: int
    depth: float
    length_bars: int
    pivot: float
    low: float
    handle_depth: float | None = None
    volume_dryup: float | None = None
    tight_closes: bool = False

    @property
    def length_weeks(self) -> float:
        return self.length_bars / 5.0


def find_swing_points(
    highs: Sequence[float],
    lows: Sequence[float],
    lookback: int = 3,
) -> list[SwingPoint]:
    """Fractal swing detection: a bar that dominates ``lookback`` neighbours.

    Confirmed swings only — the final ``lookback`` bars are excluded because
    their status is not yet decidable without future data.
    """
    points: list[SwingPoint] = []
    n = len(highs)
    for i in range(lookback, n - lookback):
        window = range(i - lookback, i + lookback + 1)
        if all(highs[i] >= highs[j] for j in window) and any(highs[i] > highs[j] for j in window if j != i):
            points.append(SwingPoint(i, highs[i], True))
        if all(lows[i] <= lows[j] for j in window) and any(lows[i] < lows[j] for j in window if j != i):
            points.append(SwingPoint(i, lows[i], False))
    return points


def support_resistance(
    highs: Sequence[float],
    lows: Sequence[float],
    *,
    lookback: int = 3,
    tolerance: float = 0.02,
) -> tuple[list[Level], list[Level]]:
    """Cluster swing points into support and resistance levels.

    Returns ``(supports, resistances)``, each sorted by touch count descending
    so the strongest level is first.
    """
    swings = find_swing_points(highs, lows, lookback)
    supports = _cluster([s for s in swings if not s.is_high], tolerance, is_resistance=False)
    resistances = _cluster([s for s in swings if s.is_high], tolerance, is_resistance=True)
    return supports, resistances


def _cluster(points: Sequence[SwingPoint], tolerance: float, *, is_resistance: bool) -> list[Level]:
    clusters: list[list[float]] = []
    for point in sorted(points, key=lambda p: p.price):
        if clusters and abs(point.price - clusters[-1][0]) / max(clusters[-1][0], 1e-9) <= tolerance:
            clusters[-1].append(point.price)
        else:
            clusters.append([point.price])
    levels = [
        Level(price=sum(group) / len(group), touches=len(group), is_resistance=is_resistance)
        for group in clusters
    ]
    return sorted(levels, key=lambda lv: (-lv.touches, lv.price))


def nearest_support(price: float, supports: Sequence[Level]) -> Level | None:
    """Closest support strictly below ``price`` — the structural stop for a long."""
    below = [lv for lv in supports if lv.price < price]
    return max(below, key=lambda lv: lv.price) if below else None


def nearest_resistance(price: float, resistances: Sequence[Level]) -> Level | None:
    """Closest resistance strictly above ``price`` — the first natural target."""
    above = [lv for lv in resistances if lv.price > price]
    return min(above, key=lambda lv: lv.price) if above else None


def rolling_high(values: Sequence[float], period: int, offset: int = 1) -> Number:
    """Highest value over ``period`` bars ending ``offset`` bars back.

    ``offset=1`` excludes the current bar, which is what a breakout test needs:
    "did today exceed the prior 20-day high", not "including today".
    """
    end = len(values) - offset
    if end < period:
        return None
    return max(values[end - period: end])


def rolling_low(values: Sequence[float], period: int, offset: int = 1) -> Number:
    end = len(values) - offset
    if end < period:
        return None
    return min(values[end - period: end])


def is_breakout(
    closes: Sequence[float],
    highs: Sequence[float],
    period: int = 20,
    *,
    basis: str = "close",
) -> bool:
    """True when the latest close breaks out above the prior ``period`` bars.

    ``basis`` selects what the close is measured against:

    ``"close"`` (default)
        ``close > max(prior period closes)`` — a ``period``-day *closing* high.
        This is the standard closing-basis breakout, and the one §6.2.1
        implies when it specifies "Entry: On breakout candle close".

    ``"high"``
        ``close > max(prior period intraday highs)`` — the close must clear
        every intraday print of the window.

    The default changed from ``"high"`` to ``"close"`` after measurement:
    comparing a *close* against prior *intraday highs* is a mismatched
    comparison that made the condition roughly twice as strict as intended
    (6.0% of bars versus 13.3%), for no stated reason in the specification. A
    close above the prior 20 closes is a 20-day high on the basis the entry
    rule itself uses.
    """
    reference = closes if basis == "close" else highs
    prior = rolling_high(reference, period, offset=1)
    return prior is not None and closes[-1] > prior


def is_breakdown(
    closes: Sequence[float],
    lows: Sequence[float],
    period: int = 20,
    *,
    basis: str = "close",
) -> bool:
    """Mirror of :func:`is_breakout` for downside breaks."""
    reference = closes if basis == "close" else lows
    prior = rolling_low(reference, period, offset=1)
    return prior is not None and closes[-1] < prior


def higher_highs_and_lows(highs: Sequence[float], lows: Sequence[float], lookback: int = 3) -> bool:
    """Classic uptrend structure test over the last two confirmed swings."""
    swings = find_swing_points(highs, lows, lookback)
    swing_highs = [s.price for s in swings if s.is_high][-2:]
    swing_lows = [s.price for s in swings if not s.is_high][-2:]
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return False
    return swing_highs[-1] > swing_highs[0] and swing_lows[-1] > swing_lows[0]


def distance_from_high(closes: Sequence[float], period: int = 252) -> Number:
    """How far below its ``period``-bar high the latest close sits, as a fraction."""
    window = closes[-period:]
    if not window:
        return None
    peak = max(window)
    if peak <= 0:
        return None
    return (peak - closes[-1]) / peak


def fibonacci_levels(swing_low: float, swing_high: float) -> dict[str, float]:
    """Retracements and extensions. Confluence support only, never a primary signal."""
    span = swing_high - swing_low
    return {
        "0.0": swing_high,
        "23.6": swing_high - span * 0.236,
        "38.2": swing_high - span * 0.382,
        "50.0": swing_high - span * 0.500,
        "61.8": swing_high - span * 0.618,
        "78.6": swing_high - span * 0.786,
        "100.0": swing_low,
        "ext_127.2": swing_high + span * 0.272,
        "ext_161.8": swing_high + span * 0.618,
        "ext_261.8": swing_high + span * 1.618,
    }


def consolidation_range(
    highs: Sequence[float],
    lows: Sequence[float],
    period: int = 20,
    *,
    exclude_recent: int = 0,
) -> tuple[float, float, float]:
    """``(low, high, depth)`` of the trailing window; depth is a fraction of the high.

    ``exclude_recent`` drops the most recent bars from the measurement. Base
    detectors pass 1 so that the resulting pivot describes the consolidation
    the price is trying to break *out of*, not a window that already contains
    the breakout bar.
    """
    end = len(highs) - exclude_recent
    if end <= 0:
        return 0.0, 0.0, 0.0
    start = max(0, end - period)
    window_high = max(highs[start:end])
    window_low = min(lows[start:end])
    depth = (window_high - window_low) / window_high if window_high > 0 else 0.0
    return window_low, window_high, depth


def tight_closes(closes: Sequence[float], period: int = 5, max_spread: float = 0.03) -> bool:
    """Weekly-tight-close test: the trailing window's range is under ``max_spread``."""
    window = closes[-period:]
    if len(window) < period:
        return False
    high, low = max(window), min(window)
    return high > 0 and (high - low) / high <= max_spread


def volume_dryup(volumes: Sequence[float], period: int = 10, baseline: int = 50) -> Number:
    """Recent average volume divided by the longer baseline average.

    Below ~0.8 is the classic "supply has dried up" reading that should precede
    a valid breakout.
    """
    if len(volumes) < baseline + period:
        return None
    recent = volumes[-period:]
    older = volumes[-(baseline + period): -period]
    base_avg = sum(older) / len(older)
    if base_avg <= 0:
        return None
    return (sum(recent) / len(recent)) / base_avg


# ---------------------------------------------------------------------------
# Base patterns
# ---------------------------------------------------------------------------


#: Bars excluded from every pivot calculation.
#:
#: The pivot is the level a breakout must clear, so it has to be established
#: by the base's own formation and be *fixed* before the breakout bar prints.
#: Measuring it over a window that includes the current bar makes
#: ``close > pivot`` equivalent to ``close > this bar's own high`` — which is
#: impossible by definition, and silently left three CAN SLIM strategies
#: unable to generate a single signal.
PIVOT_EXCLUDE_RECENT = 1


def detect_flat_base(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    *,
    min_bars: int = 25,
    max_depth: float = 0.15,
) -> BasePattern | None:
    """Flat base: 5-6+ weeks sideways, correction no deeper than ~15%."""
    if len(closes) < min_bars + 5 + PIVOT_EXCLUDE_RECENT:
        return None
    low, high, depth = consolidation_range(
        highs, lows, min_bars, exclude_recent=PIVOT_EXCLUDE_RECENT
    )
    if depth > max_depth or high <= 0:
        return None
    return BasePattern(
        kind="flat_base",
        start_index=len(closes) - min_bars - PIVOT_EXCLUDE_RECENT,
        end_index=len(closes) - 1 - PIVOT_EXCLUDE_RECENT,
        depth=depth,
        length_bars=min_bars,
        pivot=high,
        low=low,
        volume_dryup=volume_dryup(volumes),
        tight_closes=tight_closes(closes),
    )


def detect_cup_with_handle(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    *,
    min_bars: int = 35,
    max_bars: int = 325,
    min_depth: float = 0.12,
    max_depth: float = 0.33,
) -> BasePattern | None:
    """Cup with handle (spec 6.2.5 geometry).

    Measures: a left rim, a rounded bottom, a right rim near the left, then a
    shallow handle drifting down in the cup's upper half.
    """
    n = len(closes)
    if n < min_bars + 5 + PIVOT_EXCLUDE_RECENT:
        return None

    # The pattern is measured on bars that had already formed, so the pivot it
    # yields is a level the current bar can actually break through.
    formed = n - PIVOT_EXCLUDE_RECENT
    window = min(formed, max_bars)
    segment_high = highs[formed - window:formed]
    segment_low = lows[formed - window:formed]
    segment_close = closes[formed - window:formed]

    # Left rim: the highest bar in the first half of the window.
    half = max(2, len(segment_high) // 2)
    left_rim_index = max(range(half), key=lambda i: segment_high[i])
    left_rim = segment_high[left_rim_index]

    # Cup bottom: the lowest low after the left rim.
    after = range(left_rim_index + 1, len(segment_low))
    if not after:
        return None
    bottom_index = min(after, key=lambda i: segment_low[i])
    bottom = segment_low[bottom_index]

    depth = (left_rim - bottom) / left_rim if left_rim > 0 else 0.0
    if not (min_depth <= depth <= max_depth):
        return None

    # Right side: from the bottom onward the price must recover toward the rim.
    right = range(bottom_index + 1, len(segment_high))
    if not right:
        return None
    right_rim_index = max(right, key=lambda i: segment_high[i])
    right_rim = segment_high[right_rim_index]
    if right_rim < left_rim * 0.85:
        return None  # never recovered; not a cup

    length_bars = right_rim_index - left_rim_index
    if length_bars < min_bars:
        return None

    # Handle: everything after the right rim, required to sit in the cup's
    # upper half and to be shallower than the cup itself.
    handle_slice = segment_low[right_rim_index + 1:]
    handle_depth: float | None = None
    if handle_slice:
        handle_low = min(handle_slice)
        handle_depth = (right_rim - handle_low) / right_rim if right_rim > 0 else None
        midpoint = bottom + (left_rim - bottom) / 2.0
        if handle_low < midpoint or (handle_depth or 0.0) > depth * 0.6:
            return None

    # The pivot is the right rim (or the handle's high where one formed):
    # the level the base must be cleared above.
    handle_high = max(segment_high[right_rim_index:]) if right_rim_index < len(segment_high) else right_rim
    pivot = max(right_rim, handle_high)
    return BasePattern(
        kind="cup_with_handle",
        start_index=formed - window + left_rim_index,
        end_index=formed - 1,
        depth=depth,
        length_bars=length_bars,
        pivot=max(pivot, right_rim),
        low=bottom,
        handle_depth=handle_depth,
        volume_dryup=volume_dryup(volumes),
        tight_closes=tight_closes(closes),
    )


def detect_double_bottom(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    *,
    lookback: int = 3,
    max_bottom_spread: float = 0.06,
) -> BasePattern | None:
    """W pattern where the second bottom undercuts the first (the shakeout)."""
    swings = find_swing_points(highs, lows, lookback)
    swing_lows = [s for s in swings if not s.is_high][-2:]
    if len(swing_lows) < 2:
        return None

    first, second = swing_lows
    if first.price <= 0:
        return None
    # The undercut is the point of the pattern: it flushes weak holders.
    if second.price >= first.price:
        return None
    if abs(second.price - first.price) / first.price > max_bottom_spread:
        return None

    middle = [s for s in swings if s.is_high and first.index < s.index < second.index]
    if not middle:
        return None
    peak = max(middle, key=lambda s: s.price)

    depth = (peak.price - second.price) / peak.price if peak.price > 0 else 0.0
    return BasePattern(
        kind="double_bottom",
        start_index=first.index,
        end_index=len(closes) - 1,
        depth=depth,
        length_bars=second.index - first.index,
        pivot=peak.price,
        low=second.price,
        volume_dryup=volume_dryup(volumes),
        tight_closes=tight_closes(closes),
    )


def detect_high_tight_flag(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    *,
    run_bars: int = 40,
    min_advance: float = 1.00,
    flag_bars: int = 20,
    max_flag_depth: float = 0.25,
) -> BasePattern | None:
    """High tight flag: a 100%+ advance, then a shallow 3-5 week rest. Rare."""
    if len(closes) < run_bars + flag_bars + PIVOT_EXCLUDE_RECENT:
        return None

    formed = len(closes) - PIVOT_EXCLUDE_RECENT
    run_start = closes[formed - (run_bars + flag_bars)]
    run_peak = max(highs[formed - (run_bars + flag_bars): formed - flag_bars])
    if run_start <= 0:
        return None
    advance = (run_peak - run_start) / run_start
    if advance < min_advance:
        return None

    flag_low = min(lows[formed - flag_bars:formed])
    flag_high = max(highs[formed - flag_bars:formed])
    depth = (run_peak - flag_low) / run_peak if run_peak > 0 else 1.0
    if depth > max_flag_depth:
        return None

    return BasePattern(
        kind="high_tight_flag",
        start_index=formed - (run_bars + flag_bars),
        end_index=formed - 1,
        depth=depth,
        length_bars=flag_bars,
        pivot=flag_high,
        low=flag_low,
        volume_dryup=volume_dryup(volumes),
        tight_closes=tight_closes(closes),
    )


def detect_bases(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
) -> list[BasePattern]:
    """Run every base detector and return whatever formed."""
    found = [
        detect_high_tight_flag(highs, lows, closes, volumes),
        detect_cup_with_handle(highs, lows, closes, volumes),
        detect_double_bottom(highs, lows, closes, volumes),
        detect_flat_base(highs, lows, closes, volumes),
    ]
    return [b for b in found if b is not None]


def ma_alignment(closes: Sequence[float]) -> bool:
    """Textbook bullish stack: price > MA20 > MA50 > MA200."""
    ma20, ma50, ma200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    values = [ma20[-1] if ma20 else None, ma50[-1] if ma50 else None, ma200[-1] if ma200 else None]
    if any(v is None for v in values):
        return False
    return closes[-1] > values[0] > values[1] > values[2]  # type: ignore[operator]
