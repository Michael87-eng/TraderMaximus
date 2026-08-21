"""Feature engine: turns a candle series into one point-in-time snapshot.

Strategies read a :class:`FeatureSet`, never raw candles, which keeps them
declarative and makes it impossible for a strategy to peek at a bar that had
not closed: the snapshot is built from a series already truncated to the
decision timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from ..core.types import AssetClass, Candle
from ..data.series import CandleSeries
from . import core, patterns, structure
from .patterns import PatternMatch
from .structure import BasePattern, Level

#: Bars needed before the slowest indicator (MA200) is meaningful.
MIN_HISTORY = 200


@dataclass(frozen=True, slots=True)
class FeatureSet:
    """Every indicator value a strategy may consult, as of ``timestamp``."""

    symbol: str
    asset_class: AssetClass
    timestamp: datetime
    close: float
    values: Mapping[str, Any] = field(default_factory=dict)
    supports: tuple[Level, ...] = ()
    resistances: tuple[Level, ...] = ()
    bases: tuple[BasePattern, ...] = ()
    candle_patterns: tuple[PatternMatch, ...] = ()
    bars_available: int = 0

    # -- typed access -------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        value = self.values.get(key, default)
        return default if value is None else value

    def number(self, key: str) -> float | None:
        value = self.values.get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def require(self, key: str) -> float:
        value = self.number(key)
        if value is None:
            raise KeyError(f"feature {key!r} unavailable for {self.symbol}")
        return value

    def has(self, *keys: str) -> bool:
        """True when every requested feature has formed."""
        return all(self.number(key) is not None for key in keys)

    @property
    def atr(self) -> float | None:
        return self.number("atr_14")

    def nearest_support(self) -> Level | None:
        return structure.nearest_support(self.close, self.supports)

    def nearest_resistance(self) -> Level | None:
        return structure.nearest_resistance(self.close, self.resistances)

    def base_of(self, kind: str) -> BasePattern | None:
        for base in self.bases:
            if base.kind == kind:
                return base
        return None

    def pattern(self, name: str) -> PatternMatch | None:
        for match in self.candle_patterns:
            if match.name == name:
                return match
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),
            "close": self.close,
            "bars": self.bars_available,
            **{k: (round(v, 6) if isinstance(v, float) else v) for k, v in self.values.items()},
        }


class FeatureEngine:
    """Computes the full indicator set for a series.

    Args:
        volume_baseline: lookback for relative-volume and dry-up comparisons.
        swing_lookback: fractal width for swing/level detection.
        periods_per_year: annualisation factor; 252 for equities/FX daily bars,
            365 for crypto which trades every day.
    """

    def __init__(
        self,
        *,
        volume_baseline: int = 50,
        swing_lookback: int = 3,
        periods_per_year: int = 252,
    ) -> None:
        self.volume_baseline = volume_baseline
        self.swing_lookback = swing_lookback
        self.periods_per_year = periods_per_year

    def compute(self, series: CandleSeries, *, detect_bases: bool = True) -> FeatureSet:
        if len(series) == 0:
            raise ValueError("cannot compute features on an empty series")

        closes = series.closes
        highs = series.highs
        lows = series.lows
        volumes = series.volumes
        candles: Sequence[Candle] = series.candles
        last = closes[-1]

        annualisation = 365 if series.instrument.asset_class is AssetClass.CRYPTO else self.periods_per_year

        macd_line, macd_signal, macd_hist = core.macd(closes)
        stoch_k, stoch_d = core.stochastic(highs, lows, closes)
        plus_di, minus_di, adx_line = core.directional_movement(highs, lows, closes)
        bb_upper, bb_mid, bb_lower = core.bollinger_bands(closes)
        obv_line = core.obv(closes, volumes)
        ad_line = core.accumulation_distribution(highs, lows, closes, volumes)

        values: dict[str, Any] = {
            # Moving averages
            "ema10": core.last_value(core.ema(closes, 10)),
            "ema20": core.last_value(core.ema(closes, 20)),
            "ema50": core.last_value(core.ema(closes, 50)),
            "ma50": core.last_value(core.sma(closes, 50)),
            "ma100": core.last_value(core.sma(closes, 100)),
            "ma200": core.last_value(core.sma(closes, 200)),
            "ma_slope_20": core.ma_slope(closes, 20),
            "ma_slope_200": core.ma_slope(closes, 200),
            "ma_alignment": structure.ma_alignment(closes),
            # Momentum
            "rsi_14": core.last_value(core.rsi(closes, 14)),
            "macd": core.last_value(macd_line),
            "macd_signal": core.last_value(macd_signal),
            "macd_histogram": core.last_value(macd_hist),
            "stochastic_k": core.last_value(stoch_k),
            "stochastic_d": core.last_value(stoch_d),
            "stochastic_cross_up": _cross_up(stoch_k, stoch_d),
            "roc_10": core.last_value(core.roc(closes, 10)),
            "cci_20": core.last_value(core.cci(highs, lows, closes, 20)),
            # Volatility
            "atr_14": core.last_value(core.atr(highs, lows, closes, 14)),
            "atr_pct": _safe_ratio(core.last_value(core.atr(highs, lows, closes, 14)), last),
            "bb_upper": core.last_value(bb_upper),
            "bb_middle": core.last_value(bb_mid),
            "bb_lower": core.last_value(bb_lower),
            "bb_position": _bb_position(last, core.last_value(bb_upper), core.last_value(bb_lower)),
            "realized_vol_20": core.realized_volatility(closes, 20, annualisation),
            "volatility_percentile": core.volatility_percentile(closes, 20),
            # Trend
            "adx_14": core.last_value(adx_line),
            "plus_di": core.last_value(plus_di),
            "minus_di": core.last_value(minus_di),
            "higher_highs_lows": structure.higher_highs_and_lows(highs, lows, self.swing_lookback),
            # Volume
            "volume": volumes[-1] if volumes else None,
            "avg_volume_50": series.average_volume(self.volume_baseline),
            "relative_volume": core.relative_volume(volumes, self.volume_baseline),
            "obv": obv_line[-1] if obv_line else None,
            "obv_rising": _rising(obv_line, 10),
            "acc_dist": ad_line[-1] if ad_line else None,
            "acc_dist_rising": _rising(ad_line, 10),
            "up_down_volume_ratio": core.up_down_volume_ratio(closes, volumes, self.volume_baseline),
            "volume_dryup": structure.volume_dryup(volumes),
            # Structure
            "high_20": structure.rolling_high(highs, 20),
            "low_20": structure.rolling_low(lows, 20),
            "high_52w": max(highs[-252:]) if highs else None,
            "low_52w": min(lows[-252:]) if lows else None,
            "distance_from_52w_high": structure.distance_from_high(closes, 252),
            "breakout_20": structure.is_breakout(closes, highs, 20),
            "breakdown_20": structure.is_breakdown(closes, lows, 20),
            "tight_closes": structure.tight_closes(closes),
            "gap_pct": _gap_pct(candles),
            # Raw bar values, needed by conditions that reason about the shape
            # of the current bar itself (gap fill, intraday rejection).
            "bar_open": candles[-1].open,
            "bar_high": candles[-1].high,
            "bar_low": candles[-1].low,
            "prev_close": candles[-2].close if len(candles) >= 2 else None,
        }
        values.update(core.ichimoku(highs, lows, closes))

        supports, resistances = structure.support_resistance(highs, lows, lookback=self.swing_lookback)
        bases = structure.detect_bases(highs, lows, closes, volumes) if detect_bases else []
        matches = patterns.detect_patterns(candles)

        # Fibonacci from the most recent confirmed swing range.
        swing_low = min(lows[-60:]) if len(lows) >= 20 else None
        swing_high = max(highs[-60:]) if len(highs) >= 20 else None
        if swing_low is not None and swing_high is not None and swing_high > swing_low:
            values["fib"] = structure.fibonacci_levels(swing_low, swing_high)

        return FeatureSet(
            symbol=series.symbol,
            asset_class=series.instrument.asset_class,
            timestamp=series.last.timestamp,
            close=last,
            values=values,
            supports=tuple(supports),
            resistances=tuple(resistances),
            bases=tuple(bases),
            candle_patterns=tuple(matches),
            bars_available=len(series),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_ratio(numerator: float | None, denominator: float) -> float | None:
    if numerator is None or denominator <= 0:
        return None
    return numerator / denominator


def _bb_position(price: float, upper: float | None, lower: float | None) -> float | None:
    """Where price sits inside the bands: 0 at the lower band, 1 at the upper."""
    if upper is None or lower is None or upper <= lower:
        return None
    return (price - lower) / (upper - lower)


def _cross_up(fast: Sequence[float | None], slow: Sequence[float | None]) -> bool:
    """True when ``fast`` crossed above ``slow`` on the latest bar."""
    if len(fast) < 2 or len(slow) < 2:
        return False
    f_prev, f_now = fast[-2], fast[-1]
    s_prev, s_now = slow[-2], slow[-1]
    if None in (f_prev, f_now, s_prev, s_now):
        return False
    return f_prev <= s_prev and f_now > s_now  # type: ignore[operator]


def _rising(values: Sequence[float], lookback: int) -> bool:
    if len(values) <= lookback:
        return False
    return values[-1] > values[-1 - lookback]


def _gap_pct(candles: Sequence[Candle]) -> float | None:
    """Overnight gap: this bar's open versus the previous bar's close."""
    if len(candles) < 2:
        return None
    prev_close = candles[-2].close
    if prev_close <= 0:
        return None
    return (candles[-1].open - prev_close) / prev_close
