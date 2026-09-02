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

#: Slope at which the trend component saturates its ``[-1, 1]`` range.
#:
#: Section 5.1 lists a ``trend_slope_threshold`` of 0.01 but does not say what
#: unit the slope is in. Read as a *per-bar* fractional change on daily bars,
#: 0.01 means the 20-day average rising 1% every single day — roughly 250% a
#: year. Using it as the normalisation divisor pins the trend component near
#: its neutral midpoint for every realistic market, which caps the composite
#: score below the 0.70 ``TREND_UP`` cutoff and makes that regime effectively
#: unreachable. Since ``TREND_UP`` gates the entire momentum and CAN SLIM
#: library, the miscalibration would silently disable most of the system.
#:
#: 0.002/bar (~0.2% a day, ~5% a month) is the reference for a strong sustained
#: daily trend, so that is the saturation point. The spec's 0.01 is retained
#: below as the "is it trending at all" threshold it reads as.
TREND_SLOPE_REFERENCE = 0.002

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
    # Section 5.4 states TREND_UP's disallowed set as "Reversal, Gap (except
    # gap-up), Mean Reversion". The parenthetical carves gap-up *out* of the
    # prohibition, so a long gap-up continuation belongs in the allowed set.
    # Omitting it left the gap strategy eligible in no regime at all: it was
    # neither permitted nor forbidden anywhere, and the permission check
    # required explicit membership.
    Regime.TREND_UP: frozenset(
        {"momentum", "breakout", "can_slim", "pullback", "institutional", "gap", "mean_reversion"}
    ),
    Regime.TREND_DOWN: frozenset({"short", "cash", "defensive"}),
    Regime.RANGE: frozenset({"reversal", "mean_reversion", "oscillator"}),
    Regime.HIGH_VOLATILITY: frozenset({"reversal", "mean_reversion", "defensive"}),
    Regime.LOW_VOLATILITY: frozenset({"breakout", "can_slim", "momentum", "pullback", "institutional", "mean_reversion"}),
    Regime.VOLATILITY_EXPANSION: frozenset({"defensive", "momentum", "mean_reversion"}),
    Regime.VOLATILITY_COMPRESSION: frozenset({"breakout", "can_slim", "reversal", "mean_reversion"}),
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
    # Mean reversion is no longer vetoed by trend regimes: under the
    # conservative-fallback protocol it is permitted wherever trading is
    # permitted at all, and sized down instead (see
    # REGIME_FAMILY_SIZE_MULTIPLIER). Reversal keeps its trend veto.
    Regime.TREND_UP: frozenset({"reversal"}),
    Regime.TREND_DOWN: frozenset({"momentum", "breakout", "can_slim", "pullback", "gap", "institutional"}),
    Regime.RANGE: frozenset({"momentum", "breakout", "can_slim"}),
    Regime.HIGH_VOLATILITY: frozenset({"breakout", "momentum", "can_slim", "gap"}),
    Regime.LOW_VOLATILITY: frozenset(),
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


#: Minimum signal score by strategy family.
#:
#: Section 7.2 sets minimum scores per *regime*. Measured against real market
#: history that produced a contradiction: section 5.4 routes counter-trend
#: strategies into RANGE and HIGH_VOLATILITY, section 7.2 gives those regimes
#: the highest bars (70 and 75), and section 7.1 scores counter-trend setups
#: lowest there. Across 2,500 bars each of real EURUSD and ETH-USD, every
#: setup that met all its entry conditions scored 32-60 against a threshold of
#: 75. Not one could ever have traded.
#:
#: The threshold is therefore keyed to the strategy family, which is what the
#: score is actually measuring the quality of. A regime that permits no trading
#: still blocks outright — this changes the bar a signal must clear, never
#: whether the regime allows the strategy at all.
FAMILY_MIN_SCORE: Mapping[str, float] = {
    "momentum": 75.0,
    "breakout": 75.0,
    "gap": 75.0,
    "can_slim": 65.0,
    "institutional": 65.0,
    "pullback": 65.0,
    # Counter-trend families sit at 60 per the tuning brief. Mean reversion is
    # the one exception, held at 50 under the conservative-fallback protocol:
    # it is the only family whose setups measurably could not clear 60 on real
    # FX history. Reversal was tried at 50 too and had to be put back — at that
    # bar it won the selection on every synthetic equity bar it triggered and
    # crowded every other family out of the book.
    "reversal": 60.0,
    "mean_reversion": 50.0,
    "oscillator": 60.0,
    "short": 75.0,
    "defensive": 60.0,
    "cash": 60.0,
}

#: Position-size multiplier applied when a family trades in a regime that
#: tolerates it but does not favour it. Counter-trend strategies are permitted
#: in HIGH_VOLATILITY because that is where dislocations occur, but volatility
#: is exactly what makes them dangerous, so they go in smaller.
REGIME_FAMILY_SIZE_MULTIPLIER: Mapping[tuple[Regime, str], float] = {
    (Regime.HIGH_VOLATILITY, "mean_reversion"): 0.5,
    (Regime.HIGH_VOLATILITY, "reversal"): 0.5,
    (Regime.VOLATILITY_EXPANSION, "mean_reversion"): 0.5,
    (Regime.VOLATILITY_EXPANSION, "reversal"): 0.5,
}


#: Families each regime actively favours, and the weight the Phase 3 brief
#: assigns them. **Not applied by default** — see below.
FAVOURED_FAMILY_WEIGHT: Mapping[tuple[Regime, str], float] = {
    (Regime.TREND_UP, "momentum"): 1.5,
    (Regime.TREND_UP, "breakout"): 1.5,
    (Regime.TREND_UP, "can_slim"): 1.5,
    (Regime.TREND_UP, "pullback"): 1.5,
    (Regime.BULL_MARKET, "momentum"): 1.5,
    (Regime.BULL_MARKET, "breakout"): 1.5,
    (Regime.BULL_MARKET, "can_slim"): 1.5,
    (Regime.RANGE, "mean_reversion"): 1.5,
    (Regime.RANGE, "oscillator"): 1.5,
    (Regime.HIGH_VOLATILITY, "reversal"): 1.5,
    (Regime.VOLATILITY_EXPANSION, "reversal"): 1.5,
}

#: The weights actually in force. Empty: the mechanism is built, tested and
#: available, and it is switched off.
#:
#: It was applied and measured. On the only asset class with enough trades to
#: measure anything — nine years of real EURUSD and GBPUSD — weighting mean
#: reversion up in RANGE took the trade count from 48 to 72 and in-sample
#: profit factor from 1.47 to 1.04. The 24 additional trades were, in
#: aggregate, losers.
#:
#: The reason is worth stating because the design note originally got it wrong.
#: Clamping the weighted value to the component's own maximum does keep the
#: score inside 0-100, and it was argued from that that "the bar a signal must
#: clear stays the bar it was set at". True, and beside the point: the *score*
#: rises, so setups that previously fell short of their family bar now clear
#: it. Where the component was not already at its maximum — which is the normal
#: case — a 1.5x weight is a threshold loosening wearing the clothes of a
#: preference. Mean reversion already trades on a lowered bar of 50; adding a
#: score bonus on top double-counts the same preference.
#:
#: Set this to FAVOURED_FAMILY_WEIGHT to re-enable, and re-validate before
#: trusting it.
REGIME_FAMILY_WEIGHT: Mapping[tuple[Regime, str], float] = {}


def family_regime_weight(
    strategy_type: str,
    state: RegimeState,
    weights: Mapping[tuple[Regime, str], float] | None = None,
) -> float:
    """How strongly the current regime favours this family.

    The largest applicable weight across every active regime wins: a market
    that is both TREND_UP and RANGE is ambiguous, and a family should not be
    penalised for the classifier being unsure.

    Returns 1.0 for everything under the default weights, which are empty.
    """
    table = REGIME_FAMILY_WEIGHT if weights is None else weights
    applicable = [
        table[(regime, strategy_type)]
        for regime in state.all_regimes
        if (regime, strategy_type) in table
    ]
    return max(applicable) if applicable else 1.0


def family_minimum_score(strategy_type: str) -> float:
    """The score a signal of this family must reach (see FAMILY_MIN_SCORE)."""
    return FAMILY_MIN_SCORE.get(strategy_type, 75.0)


def effective_minimum_score(strategy_type: str, state: RegimeState) -> float | None:
    """The score bar a signal of this family must clear in this regime.

    ``None`` still means "this regime permits no new positions at all" — the
    regime keeps its power of veto. What the family threshold replaces is only
    the *height* of the bar for a regime that does permit trading, so that the
    strategy engine and the risk engine agree. They used to disagree: the
    strategy engine judged a candidate against its family bar and the risk
    engine re-judged the survivor against the regime bar, so every
    mean-reversion signal the strategy engine passed was rejected one layer
    later and no trade could reach the broker.
    """
    if minimum_score(state) is None:
        return None
    return family_minimum_score(strategy_type)


def family_size_multiplier(strategy_type: str, state: RegimeState) -> float:
    """Size multiplier for this family in the current regime.

    The smallest applicable multiplier across every active regime wins, so a
    market that is both TREND_UP and HIGH_VOLATILITY sizes for the volatility.
    """
    multipliers = [
        REGIME_FAMILY_SIZE_MULTIPLIER[(regime, strategy_type)]
        for regime in state.all_regimes
        if (regime, strategy_type) in REGIME_FAMILY_SIZE_MULTIPLIER
    ]
    return min(multipliers) if multipliers else 1.0


def is_strategy_type_allowed(strategy_type: str, state: RegimeState) -> tuple[bool, str]:
    """Check a strategy family against every active regime.

    The two directions are deliberately asymmetric, and both are conservative:

    * A **veto** from *any* active regime blocks the strategy. This is what
      makes a ``TREND_UP`` market that is also ``HIGH_VOLATILITY`` refuse
      breakouts — the risk reading wins over the trend reading.
    * A **permission** may come from the primary regime *or* any active tag.

    Permission was originally taken from the primary regime alone, which made
    the two directions inconsistent: a strategy permitted by an active
    ``BULL_MARKET`` backdrop was still blocked because the primary ``TREND_UP``
    label happened not to list it. Since a veto from any regime still
    overrides, widening permission to the union cannot authorise anything an
    active regime objects to.
    """
    if not state.is_tradeable:
        return False, f"regime {state.primary.value} permits no trading"

    for regime in sorted(state.all_regimes, key=lambda r: r.value):
        if strategy_type in DISALLOWED_STRATEGY_TYPES.get(regime, frozenset()):
            return False, f"strategy type {strategy_type!r} is disallowed in {regime.value}"

    permitted_by = [
        regime.value
        for regime in sorted(state.all_regimes, key=lambda r: r.value)
        if strategy_type in ALLOWED_STRATEGY_TYPES.get(regime, frozenset())
    ]
    if not permitted_by:
        return False, (
            f"strategy type {strategy_type!r} is not in the allowed set for any active regime "
            f"({', '.join(sorted(r.value for r in state.all_regimes))})"
        )
    return True, f"eligible via {', '.join(permitted_by)}"


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

        # Trend slope: per-bar fractional MA change, saturating at
        # TREND_SLOPE_REFERENCE (see its docstring for why not 0.01).
        slope = features.number("ma_slope_20")
        if slope is None:
            missing.append("trend_slope")
        trend_norm = _symmetric_norm(
            _clamp(slope / TREND_SLOPE_REFERENCE, -1.0, 1.0) if slope is not None else 0.0
        )

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
