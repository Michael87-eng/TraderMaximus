"""Market regime engine (spec section 5).

Regime is decided *before* strategies are considered, and it decides which
strategies are even allowed to run. That ordering is the point: it stops a
momentum breakout from being taken in a bear market simply because the pattern
looked good.

**Scoring.** Section 5.1 gives six weighted inputs summing to 1.0. Each is
normalised into ``[0, 1]`` before weighting, so the composite score is also in
``[0, 1]`` and the section 5.2 thresholds (``score > 0.7`` etc.) apply directly:

===================== ====== =====================================================
Input                 Weight Normalisation
===================== ====== =====================================================
trend_slope           0.30   ``[-1, 1]`` mapped to ``[0, 1]`` via ``(x + 1) / 2``
volatility_percentile 0.20   inverted: calm markets score higher
breadth_index         0.20   ``[-1, 1]`` mapped to ``[0, 1]``
new_highs_ratio       0.15   already ``[0, 1]``
volume_flow           0.10   ``r / (1 + r)``, so a 1.0 up/down ratio maps to 0.5
correlation_change    0.05   inverted: rising cross-asset correlation is risk-off
===================== ====== =====================================================

The last row resolves an ambiguity in the spec, which weights
``correlation_change`` positively without fixing its direction. Rising
correlation means diversification is failing, so it is treated as a negative
and inverted here.

**Output.** Classification yields a *primary* regime plus a set of concurrent
*tags*. A market can be simultaneously ``TREND_UP`` and ``HIGH_VOLATILITY``;
collapsing that to one label would silently discard the risk information, so
strategy eligibility is checked against the whole set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping, Sequence

from ..core.types import utc_now
from ..indicators import core as ind
from ..indicators.engine import FeatureSet


class Regime(str, Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    VOLATILITY_EXPANSION = "VOLATILITY_EXPANSION"
    VOLATILITY_COMPRESSION = "VOLATILITY_COMPRESSION"
    BULL_MARKET = "BULL_MARKET"
    BEAR_MARKET = "BEAR_MARKET"
    MARKET_CORRECTION = "MARKET_CORRECTION"
    EVENT_DRIVEN = "EVENT_DRIVEN"
    LIQUIDITY_STRESS = "LIQUIDITY_STRESS"
    UNKNOWN = "UNKNOWN"


#: Section 5.1 weights. Must sum to 1.0.
WEIGHTS: Mapping[str, float] = {
    "trend_slope": 0.30,
    "volatility_percentile": 0.20,
    "breadth_index": 0.20,
    "new_highs_ratio": 0.15,
    "volume_flow": 0.10,
    "correlation_change": 0.05,
}

#: Section 5.1 thresholds.
THRESHOLDS: Mapping[str, float] = {
    "trend_slope": 0.01,
    "volatility_percentile": 0.70,
    "breadth_index": 0.50,
    "new_highs_ratio": 0.60,
    "volume_flow": 1.0,
    "correlation_change": 0.20,
}


@dataclass(frozen=True, slots=True)
class MarketContext:
    """Market-wide inputs a single instrument cannot supply.

    All fields are optional. Missing breadth data does not fabricate a neutral
    reading silently — it lowers the confidence score, which in turn shrinks
    position sizes under section 5.3.
    """

    breadth_index: float | None = None          # advance/decline, [-1, 1]
    new_highs_ratio: float | None = None        # new highs / (new highs + new lows)
    correlation_change: float | None = None     # cross-asset correlation change, [0, 1]
    vix: float | None = None
    vix_change: float | None = None
    index_drawdown: float | None = None         # benchmark decline from its peak
    index_ma200_slope: float | None = None
    spread_ratio: float | None = None           # current spread / normal spread
    volume_ratio: float | None = None           # current volume / normal volume
    high_impact_event: bool = False
    volatility_spike: float | None = None       # e.g. 0.35 for a 35% jump

    @property
    def has_breadth(self) -> bool:
        return self.breadth_index is not None or self.new_highs_ratio is not None


@dataclass(frozen=True, slots=True)
class RegimeState:
    """Classification result carried through the rest of the pipeline."""

    primary: Regime
    tags: frozenset[Regime]
    score: float
    confidence: float
    components: Mapping[str, float] = field(default_factory=dict)
    raw_inputs: Mapping[str, float | None] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=utc_now)
    notes: tuple[str, ...] = ()

    def __contains__(self, regime: Regime) -> bool:
        return regime is self.primary or regime in self.tags

    @property
    def all_regimes(self) -> frozenset[Regime]:
        return self.tags | {self.primary}

    @property
    def is_tradeable(self) -> bool:
        """Section 5.4: two regimes forbid all strategies outright."""
        return not (
            Regime.LIQUIDITY_STRESS in self.all_regimes or self.primary is Regime.UNKNOWN
        )

    @property
    def size_multiplier(self) -> float:
        """Section 5.3: below 0.70 confidence, halve size and open nothing new."""
        return 1.0 if self.confidence >= 0.70 else 0.5

    def as_dict(self) -> dict[str, object]:
        return {
            "primary": self.primary.value,
            "tags": sorted(t.value for t in self.tags),
            "score": round(self.score, 4),
            "confidence": round(self.confidence, 4),
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Strategy eligibility (section 5.4)
# ---------------------------------------------------------------------------

ALLOWED_STRATEGY_TYPES: Mapping[Regime, frozenset[str]] = {
    Regime.TREND_UP: frozenset({"momentum", "breakout", "can_slim", "pullback", "institutional"}),
    Regime.TREND_DOWN: frozenset({"short", "cash", "defensive"}),
    Regime.RANGE: frozenset({"reversal", "mean_reversion", "oscillator"}),
    Regime.HIGH_VOLATILITY: frozenset({"reversal", "mean_reversion", "defensive"}),
    Regime.LOW_VOLATILITY: frozenset({"breakout", "can_slim", "momentum", "pullback", "institutional"}),
    Regime.VOLATILITY_EXPANSION: frozenset({"defensive", "momentum"}),
    Regime.VOLATILITY_COMPRESSION: frozenset({"breakout", "can_slim", "reversal"}),
    Regime.BULL_MARKET: frozenset(
        {"momentum", "breakout", "can_slim", "pullback", "institutional", "gap", "reversal", "mean_reversion"}
    ),
    Regime.BEAR_MARKET: frozenset({"cash", "short", "defensive"}),
    Regime.MARKET_CORRECTION: frozenset({"cash", "defensive"}),
    Regime.EVENT_DRIVEN: frozenset({"defensive", "gap"}),
    Regime.LIQUIDITY_STRESS: frozenset(),
    Regime.UNKNOWN: frozenset(),
}

DISALLOWED_STRATEGY_TYPES: Mapping[Regime, frozenset[str]] = {
    Regime.TREND_UP: frozenset({"reversal", "mean_reversion"}),
    Regime.TREND_DOWN: frozenset({"momentum", "breakout", "can_slim", "pullback", "gap", "institutional"}),
    Regime.RANGE: frozenset({"momentum", "breakout", "can_slim"}),
    Regime.HIGH_VOLATILITY: frozenset({"breakout", "momentum", "can_slim", "gap"}),
    Regime.LOW_VOLATILITY: frozenset({"mean_reversion"}),
    Regime.VOLATILITY_EXPANSION: frozenset({"breakout", "can_slim", "gap"}),
    Regime.VOLATILITY_COMPRESSION: frozenset(),
    Regime.BULL_MARKET: frozenset({"short"}),
    Regime.BEAR_MARKET: frozenset({"momentum", "breakout", "can_slim", "pullback", "gap", "institutional"}),
    Regime.MARKET_CORRECTION: frozenset(
        {"momentum", "breakout", "can_slim", "pullback", "gap", "institutional", "reversal", "mean_reversion"}
    ),
    Regime.EVENT_DRIVEN: frozenset({"momentum", "breakout", "can_slim", "mean_reversion"}),
    Regime.LIQUIDITY_STRESS: frozenset(
        {
            "momentum", "breakout", "can_slim", "pullback", "gap",
            "institutional", "reversal", "mean_reversion", "short",
        }
    ),
    Regime.UNKNOWN: frozenset(
        {
            "momentum", "breakout", "can_slim", "pullback", "gap",
            "institutional", "reversal", "mean_reversion", "short",
        }
    ),
}

#: Section 7.2 minimum signal scores, keyed by the primary regime.
MIN_SIGNAL_SCORE: Mapping[Regime, float | None] = {
    Regime.TREND_UP: 60.0,
    Regime.TREND_DOWN: None,        # no long strategies permitted
    Regime.RANGE: 70.0,
    Regime.HIGH_VOLATILITY: 75.0,
    Regime.LOW_VOLATILITY: 55.0,
    Regime.BULL_MARKET: 50.0,
    Regime.BEAR_MARKET: None,
    Regime.VOLATILITY_EXPANSION: 65.0,
    Regime.VOLATILITY_COMPRESSION: 65.0,
    Regime.MARKET_CORRECTION: None,
    Regime.EVENT_DRIVEN: 80.0,
    Regime.LIQUIDITY_STRESS: None,
    Regime.UNKNOWN: None,
}


def is_strategy_type_allowed(strategy_type: str, state: RegimeState) -> tuple[bool, str]:
    """Check a strategy family against every active regime.

    A veto from *any* active regime blocks the strategy. This is what makes a
    ``TREND_UP`` market that is also ``HIGH_VOLATILITY`` refuse breakouts.
    """
    if not state.is_tradeable:
        return False, f"regime {state.primary.value} permits no trading"

    for regime in sorted(state.all_regimes, key=lambda r: r.value):
        if strategy_type in DISALLOWED_STRATEGY_TYPES.get(regime, frozenset()):
            return False, f"strategy type {strategy_type!r} is disallowed in {regime.value}"

    allowed = ALLOWED_STRATEGY_TYPES.get(state.primary, frozenset())
    if strategy_type not in allowed:
        return False, f"strategy type {strategy_type!r} is not in the allowed set for {state.primary.value}"
    return True, "eligible"


def minimum_score(state: RegimeState) -> float | None:
    """Highest applicable minimum across active regimes; ``None`` means no trading."""
    thresholds: list[float] = []
    for regime in state.all_regimes:
        value = MIN_SIGNAL_SCORE.get(regime, None)
        if value is None and regime in {
            Regime.TREND_DOWN, Regime.BEAR_MARKET, Regime.MARKET_CORRECTION,
            Regime.LIQUIDITY_STRESS, Regime.UNKNOWN,
        }:
            return None
        if value is not None:
            thresholds.append(value)
    return max(thresholds) if thresholds else None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class RegimeEngine:
    """Computes the weighted regime score and classifies it."""

    def __init__(self, *, min_confidence: float = 0.70) -> None:
        self.min_confidence = min_confidence
        self._previous: RegimeState | None = None

    def classify(
        self,
        features: FeatureSet,
        context: MarketContext | None = None,
        *,
        benchmark_closes: Sequence[float] | None = None,
    ) -> RegimeState:
        """Classify the regime from instrument features plus market context.

        Args:
            features: the instrument (or benchmark) feature snapshot.
            context: market-wide breadth/volatility inputs; may be ``None``.
            benchmark_closes: benchmark history used for the 200-day slope and
                drawdown when ``context`` does not supply them.
        """
        context = context or MarketContext()
        raw, components, missing = self._score_components(features, context)
        score = round(sum(components.values()), 6)
        confidence = self._confidence(features, context, missing, score)
        primary, tags, notes = self._classify(score, raw, features, context, benchmark_closes)

        state = RegimeState(
            primary=primary,
            tags=frozenset(tags),
            score=score,
            confidence=confidence,
            components=components,
            raw_inputs=raw,
            timestamp=features.timestamp,
            notes=tuple(notes),
        )
        self._previous = state
        return state

    @property
    def previous(self) -> RegimeState | None:
        return self._previous

    # -- scoring ------------------------------------------------------------

    def _score_components(
        self,
        features: FeatureSet,
        context: MarketContext,
    ) -> tuple[dict[str, float | None], dict[str, float], list[str]]:
        missing: list[str] = []

        # Trend slope: per-bar fractional MA change, scaled so that the section
        # 5.1 threshold of 0.01 maps to a clearly positive normalised reading.
        slope = features.number("ma_slope_20")
        if slope is None:
            missing.append("trend_slope")
        trend_norm = _symmetric_norm(_clamp(slope / 0.01, -1.0, 1.0) if slope is not None else 0.0)

        vol_pct = features.number("volatility_percentile")
        if vol_pct is None:
            missing.append("volatility_percentile")
            vol_pct = 0.5
        vol_norm = 1.0 - _clamp(vol_pct, 0.0, 1.0)  # calm scores high

        breadth = context.breadth_index
        if breadth is None:
            missing.append("breadth_index")
            breadth = 0.0
        breadth_norm = _symmetric_norm(_clamp(breadth, -1.0, 1.0))

        new_highs = context.new_highs_ratio
        if new_highs is None:
            missing.append("new_highs_ratio")
            new_highs = 0.5
        new_highs_norm = _clamp(new_highs, 0.0, 1.0)

        flow = features.number("up_down_volume_ratio")
        if flow is None:
            missing.append("volume_flow")
            flow = 1.0
        flow = min(flow, 10.0)  # cap so one lopsided session cannot dominate
        flow_norm = flow / (1.0 + flow)

        corr_change = context.correlation_change
        if corr_change is None:
            missing.append("correlation_change")
            corr_change = 0.0
        # Inverted: rising correlation means diversification is breaking down.
        corr_norm = 1.0 - _clamp(corr_change, 0.0, 1.0)

        components = {
            "trend_slope": trend_norm * WEIGHTS["trend_slope"],
            "volatility_percentile": vol_norm * WEIGHTS["volatility_percentile"],
            "breadth_index": breadth_norm * WEIGHTS["breadth_index"],
            "new_highs_ratio": new_highs_norm * WEIGHTS["new_highs_ratio"],
            "volume_flow": flow_norm * WEIGHTS["volume_flow"],
            "correlation_change": corr_norm * WEIGHTS["correlation_change"],
        }
        raw: dict[str, float | None] = {
            "trend_slope": slope,
            "volatility_percentile": vol_pct,
            "breadth_index": breadth,
            "new_highs_ratio": new_highs,
            "volume_flow": flow,
            "correlation_change": corr_change,
        }
        return raw, components, missing

    def _confidence(
        self,
        features: FeatureSet,
        context: MarketContext,
        missing: Sequence[str],
        score: float,
    ) -> float:
        """Confidence falls with missing inputs, thin history and a mid-range score.

        A score sitting at 0.5 is genuinely ambiguous — neither trending nor
        ranging decisively — and the system should size down rather than pick a
        label and act on it.
        """
        confidence = 1.0

        # Each missing input costs its own weight in confidence.
        for name in missing:
            confidence -= WEIGHTS.get(name, 0.05)

        if features.bars_available < 200:
            confidence -= 0.15
        if features.bars_available < 100:
            confidence -= 0.15

        # Decisiveness: distance from the ambiguous midpoint, scaled to [0, 1].
        decisiveness = min(1.0, abs(score - 0.5) / 0.25)
        confidence *= 0.6 + 0.4 * decisiveness

        if context.high_impact_event:
            confidence -= 0.10

        return round(_clamp(confidence, 0.0, 1.0), 4)

    # -- classification -----------------------------------------------------

    def _classify(
        self,
        score: float,
        raw: Mapping[str, float | None],
        features: FeatureSet,
        context: MarketContext,
        benchmark_closes: Sequence[float] | None,
    ) -> tuple[Regime, set[Regime], list[str]]:
        tags: set[Regime] = set()
        notes: list[str] = []
        vol_pct = raw.get("volatility_percentile") or 0.5

        # -- volatility tags (concurrent with any trend regime) --------------
        if vol_pct > 0.75:
            tags.add(Regime.HIGH_VOLATILITY)
        if vol_pct < 0.25:
            tags.add(Regime.LOW_VOLATILITY)

        spike = context.volatility_spike
        if spike is not None:
            if spike > 0.20:
                tags.add(Regime.VOLATILITY_EXPANSION)
                notes.append(f"volatility expanding {spike:+.0%}")
            elif spike < -0.20:
                tags.add(Regime.VOLATILITY_COMPRESSION)
                notes.append(f"volatility compressing {spike:+.0%}")

        # -- market phase ----------------------------------------------------
        ma200_slope = context.index_ma200_slope
        if ma200_slope is None and benchmark_closes:
            ma200_slope = ind.ma_slope(benchmark_closes, 200, lookback=20)
        if ma200_slope is None:
            ma200_slope = features.number("ma_slope_200")

        if ma200_slope is not None:
            if ma200_slope > 0 and score > 0.6:
                tags.add(Regime.BULL_MARKET)
            elif ma200_slope < 0 and score < 0.4:
                tags.add(Regime.BEAR_MARKET)

        # -- override conditions, most severe first --------------------------
        if _is_liquidity_stressed(context):
            notes.append("liquidity stress: spreads widened and volume collapsed")
            return Regime.LIQUIDITY_STRESS, tags, notes

        if context.high_impact_event and (spike is None or spike > 0.30):
            notes.append("high-impact event with volatility spike")
            return Regime.EVENT_DRIVEN, tags, notes

        drawdown = context.index_drawdown
        if drawdown is None and benchmark_closes:
            drawdown = _drawdown_from_peak(benchmark_closes)
        if drawdown is not None and drawdown > 0.08 and score < 0.4:
            notes.append(f"benchmark {drawdown:.1%} below peak")
            return Regime.MARKET_CORRECTION, tags, notes

        if Regime.BEAR_MARKET in tags:
            return Regime.BEAR_MARKET, tags, notes

        # -- primary trend classification (section 5.2) ----------------------
        breadth = raw.get("breadth_index") or 0.0
        calm = vol_pct < 0.60

        if score > 0.70 and calm and breadth >= 0.0:
            primary = Regime.TREND_UP
        elif score < 0.30 and calm and breadth < 0.5:
            primary = Regime.TREND_DOWN
        elif 0.30 <= score <= 0.70 and calm:
            primary = Regime.RANGE
        elif Regime.HIGH_VOLATILITY in tags:
            primary = Regime.HIGH_VOLATILITY
        elif score > 0.70:
            primary = Regime.TREND_UP
        elif score < 0.30:
            primary = Regime.TREND_DOWN
        else:
            primary = Regime.RANGE

        # BULL_MARKET is a backdrop, not a substitute for the active trend
        # reading, so it stays a tag rather than becoming primary.
        return primary, tags, notes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _symmetric_norm(value: float) -> float:
    """Map ``[-1, 1]`` onto ``[0, 1]``."""
    return (value + 1.0) / 2.0


def _is_liquidity_stressed(context: MarketContext) -> bool:
    """Section 5.2: spreads more than doubled *and* volume more than halved."""
    spread_bad = context.spread_ratio is not None and context.spread_ratio > 2.0
    volume_bad = context.volume_ratio is not None and context.volume_ratio < 0.5
    return spread_bad and volume_bad


def _drawdown_from_peak(closes: Sequence[float]) -> float | None:
    if not closes:
        return None
    peak = max(closes)
    if peak <= 0:
        return None
    return (peak - closes[-1]) / peak
