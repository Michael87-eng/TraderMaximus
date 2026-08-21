"""Deterministic technical indicators (spec section 4).

Every function is pure: same input, same output, no hidden state, no I/O. Each
returns a list aligned to the input length, with ``None`` occupying the warm-up
region so callers can never silently index a value that had not yet formed.
That alignment is what makes point-in-time correctness checkable in the
backtester.

Wilder's smoothing (RSI, ATR, ADX) is used where the original author specified
it, so values match standard charting packages rather than a simplified EMA.
"""

from __future__ import annotations

import math
import statistics
from typing import Sequence

Number = float | None


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------


def sma(values: Sequence[float], period: int) -> list[Number]:
    """Simple moving average."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[Number] = [None] * len(values)
    if len(values) < period:
        return out
    window_sum = sum(values[:period])
    out[period - 1] = window_sum / period
    for i in range(period, len(values)):
        window_sum += values[i] - values[i - period]
        out[i] = window_sum / period
    return out


def ema(values: Sequence[float], period: int) -> list[Number]:
    """Exponential moving average, seeded with the SMA of the first window."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[Number] = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1.0)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = (values[i] - prev) * alpha + prev
        out[i] = prev
    return out


def wilder_smooth(values: Sequence[float], period: int) -> list[Number]:
    """Wilder's smoothing: an EMA with alpha = 1/period, SMA-seeded."""
    out: list[Number] = [None] * len(values)
    if len(values) < period or period <= 0:
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = prev + (values[i] - prev) / period
        out[i] = prev
    return out


def ma_slope(values: Sequence[float], period: int = 20, lookback: int = 5) -> Number:
    """Normalised slope of an MA: fractional change per bar over ``lookback``.

    Normalising by price level makes the value comparable across a $5 stock and
    a $60,000 coin, which the regime engine depends on.
    """
    line = sma(values, period)
    recent = [v for v in line[-(lookback + 1):] if v is not None]
    if len(recent) < 2 or recent[0] == 0:
        return None
    return (recent[-1] - recent[0]) / abs(recent[0]) / (len(recent) - 1)


# ---------------------------------------------------------------------------
# Momentum
# ---------------------------------------------------------------------------


def rsi(closes: Sequence[float], period: int = 14) -> list[Number]:
    """Wilder's Relative Strength Index, 0-100."""
    out: list[Number] = [None] * len(closes)
    if len(closes) <= period:
        return out

    gains: list[float] = [0.0]
    losses: list[float] = [0.0]
    for prev, curr in zip(closes, closes[1:]):
        change = curr - prev
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))

    avg_gain = sum(gains[1: period + 1]) / period
    avg_loss = sum(losses[1: period + 1]) / period
    out[period] = _rsi_from_averages(avg_gain, avg_loss)

    for i in range(period + 1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = _rsi_from_averages(avg_gain, avg_loss)
    return out


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(
    closes: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[Number], list[Number], list[Number]]:
    """MACD line, signal line and histogram."""
    fast_line = ema(closes, fast)
    slow_line = ema(closes, slow)
    macd_line: list[Number] = [
        (f - s) if (f is not None and s is not None) else None for f, s in zip(fast_line, slow_line)
    ]

    formed = [(i, v) for i, v in enumerate(macd_line) if v is not None]
    signal_line: list[Number] = [None] * len(closes)
    if len(formed) >= signal:
        values = [v for _, v in formed]
        smoothed = ema(values, signal)
        for (index, _), sig in zip(formed, smoothed):
            signal_line[index] = sig

    histogram: list[Number] = [
        (m - s) if (m is not None and s is not None) else None for m, s in zip(macd_line, signal_line)
    ]
    return macd_line, signal_line, histogram


def stochastic(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
    smooth_d: int = 3,
) -> tuple[list[Number], list[Number]]:
    """Stochastic oscillator %K and %D, 0-100."""
    n = len(closes)
    k_line: list[Number] = [None] * n
    for i in range(period - 1, n):
        window_high = max(highs[i - period + 1: i + 1])
        window_low = min(lows[i - period + 1: i + 1])
        span = window_high - window_low
        # A flat window has no oscillator meaning; 50 is the neutral reading.
        k_line[i] = 50.0 if span == 0 else (closes[i] - window_low) / span * 100.0

    formed = [(i, v) for i, v in enumerate(k_line) if v is not None]
    d_line: list[Number] = [None] * n
    if len(formed) >= smooth_d:
        smoothed = sma([v for _, v in formed], smooth_d)
        for (index, _), d in zip(formed, smoothed):
            d_line[index] = d
    return k_line, d_line


def roc(closes: Sequence[float], period: int = 10) -> list[Number]:
    """Rate of change, as a percentage."""
    out: list[Number] = [None] * len(closes)
    for i in range(period, len(closes)):
        base = closes[i - period]
        if base != 0:
            out[i] = (closes[i] - base) / base * 100.0
    return out


def cci(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 20,
) -> list[Number]:
    """Commodity Channel Index using mean absolute deviation."""
    typical = [(h + l + c) / 3.0 for h, l, c in zip(highs, lows, closes)]
    out: list[Number] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = typical[i - period + 1: i + 1]
        mean = sum(window) / period
        mad = sum(abs(v - mean) for v in window) / period
        if mad > 0:
            out[i] = (typical[i] - mean) / (0.015 * mad)
    return out


# ---------------------------------------------------------------------------
# Volatility
# ---------------------------------------------------------------------------


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    """True range; the first bar falls back to the high-low span."""
    out = [highs[0] - lows[0]] if closes else []
    for i in range(1, len(closes)):
        prev_close = closes[i - 1]
        out.append(max(highs[i] - lows[i], abs(highs[i] - prev_close), abs(lows[i] - prev_close)))
    return out


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> list[Number]:
    """Average True Range using Wilder's smoothing."""
    if not closes:
        return []
    return wilder_smooth(true_range(highs, lows, closes), period)


def bollinger_bands(
    closes: Sequence[float],
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[list[Number], list[Number], list[Number]]:
    """Bollinger upper, middle and lower bands (population standard deviation)."""
    middle = sma(closes, period)
    upper: list[Number] = [None] * len(closes)
    lower: list[Number] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1: i + 1]
        deviation = statistics.pstdev(window)
        centre = middle[i]
        if centre is not None:
            upper[i] = centre + num_std * deviation
            lower[i] = centre - num_std * deviation
    return upper, middle, lower


def log_returns(closes: Sequence[float]) -> list[float]:
    out: list[float] = []
    for prev, curr in zip(closes, closes[1:]):
        if prev > 0 and curr > 0:
            out.append(math.log(curr / prev))
    return out


def realized_volatility(
    closes: Sequence[float],
    period: int = 20,
    periods_per_year: int = 252,
) -> Number:
    """Annualised realised volatility over the trailing ``period`` returns."""
    returns = log_returns(closes[-(period + 1):])
    if len(returns) < 2:
        return None
    return statistics.pstdev(returns) * math.sqrt(periods_per_year)


def volatility_series(
    closes: Sequence[float],
    period: int = 20,
    periods_per_year: int = 252,
) -> list[Number]:
    """Rolling annualised volatility, aligned to ``closes``."""
    out: list[Number] = [None] * len(closes)
    for i in range(period, len(closes)):
        window = closes[i - period: i + 1]
        returns = log_returns(window)
        if len(returns) >= 2:
            out[i] = statistics.pstdev(returns) * math.sqrt(periods_per_year)
    return out


def volatility_percentile(closes: Sequence[float], period: int = 20, lookback: int = 250) -> Number:
    """Where current volatility sits in its own trailing distribution, 0-1.

    The regime engine consumes this rather than raw volatility so thresholds
    stay meaningful across assets with wildly different volatility levels.
    """
    series = [v for v in volatility_series(closes, period)[-lookback:] if v is not None]
    if len(series) < 10:
        return None
    current = series[-1]
    below = sum(1 for v in series if v <= current)
    return below / len(series)


# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------


def directional_movement(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> tuple[list[Number], list[Number], list[Number]]:
    """Wilder's +DI, -DI and ADX."""
    n = len(closes)
    plus_dm: list[float] = [0.0]
    minus_dm: list[float] = [0.0]
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        # Only the dominant direction records movement on any given bar.
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)

    tr = true_range(highs, lows, closes)
    smoothed_tr = wilder_smooth(tr, period)
    smoothed_plus = wilder_smooth(plus_dm, period)
    smoothed_minus = wilder_smooth(minus_dm, period)

    plus_di: list[Number] = [None] * n
    minus_di: list[Number] = [None] * n
    dx: list[Number] = [None] * n
    for i in range(n):
        t, p, m = smoothed_tr[i], smoothed_plus[i], smoothed_minus[i]
        if t is None or p is None or m is None or t == 0:
            continue
        plus_di[i] = p / t * 100.0
        minus_di[i] = m / t * 100.0
        total = plus_di[i] + minus_di[i]
        if total > 0:
            dx[i] = abs(plus_di[i] - minus_di[i]) / total * 100.0

    formed = [(i, v) for i, v in enumerate(dx) if v is not None]
    adx_line: list[Number] = [None] * n
    if len(formed) >= period:
        smoothed = wilder_smooth([v for _, v in formed], period)
        for (index, _), value in zip(formed, smoothed):
            adx_line[index] = value
    return plus_di, minus_di, adx_line


def adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> list[Number]:
    return directional_movement(highs, lows, closes, period)[2]


def ichimoku(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    conversion: int = 9,
    base: int = 26,
    span_b: int = 52,
) -> dict[str, Number]:
    """Latest Ichimoku values. Returns ``None`` entries when history is short."""

    def midpoint(period: int) -> Number:
        if len(highs) < period:
            return None
        return (max(highs[-period:]) + min(lows[-period:])) / 2.0

    tenkan = midpoint(conversion)
    kijun = midpoint(base)
    senkou_a = (tenkan + kijun) / 2.0 if tenkan is not None and kijun is not None else None
    return {
        "tenkan": tenkan,
        "kijun": kijun,
        "senkou_a": senkou_a,
        "senkou_b": midpoint(span_b),
        "chikou": closes[-base] if len(closes) >= base else None,
    }


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------


def obv(closes: Sequence[float], volumes: Sequence[float]) -> list[float]:
    """On-Balance Volume, cumulative from zero."""
    out = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            out.append(out[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            out.append(out[-1] - volumes[i])
        else:
            out.append(out[-1])
    return out


def accumulation_distribution(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
) -> list[float]:
    """Chaikin Accumulation/Distribution line."""
    out: list[float] = []
    running = 0.0
    for h, l, c, v in zip(highs, lows, closes, volumes):
        span = h - l
        multiplier = 0.0 if span == 0 else ((c - l) - (h - c)) / span
        running += multiplier * v
        out.append(running)
    return out


def relative_volume(volumes: Sequence[float], period: int = 50) -> Number:
    """Latest volume divided by its trailing average (excluding itself)."""
    if len(volumes) < period + 1:
        return None
    baseline = volumes[-(period + 1):-1]
    average = sum(baseline) / len(baseline)
    if average <= 0:
        return None
    return volumes[-1] / average


def up_down_volume_ratio(closes: Sequence[float], volumes: Sequence[float], period: int = 50) -> Number:
    """Volume on up bars divided by volume on down bars (section 4.1)."""
    if len(closes) < 2:
        return None
    window = min(period, len(closes) - 1)
    up = down = 0.0
    for i in range(len(closes) - window, len(closes)):
        if closes[i] > closes[i - 1]:
            up += volumes[i]
        elif closes[i] < closes[i - 1]:
            down += volumes[i]
    if down <= 0:
        return math.inf if up > 0 else None
    return up / down


def last_value(series: Sequence[Number]) -> Number:
    """Most recent formed value in an aligned indicator series."""
    for value in reversed(series):
        if value is not None:
            return value
    return None
