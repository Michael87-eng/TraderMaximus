"""Signal scoring engine (spec section 7).

Nine weighted components produce a 0-100 score that is compared against the
regime's minimum threshold (section 7.2). The score is not a probability and
must never be read as one — it ranks candidates against each other and gates
weak setups, nothing more.

Fundamentals and asset classes
------------------------------

Section 7.1 allocates 10 points to fundamentals and marks them "equities
only". Taken literally, a crypto or FX signal could score at most 90, which
would silently make every regime threshold ~11% stricter for those assets —
a hidden asset-class bias rather than a deliberate one. So when fundamentals
do not apply, the remaining components are rescaled to a 100-point maximum.
Thresholds then mean the same thing everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ..core.types import AssetClass, ScoreBreakdown, Side
from ..indicators.engine import FeatureSet
from ..regime.engine import RegimeState
from .spec import EvalContext, Evaluation, StrategySpec

#: Section 7.1 component maxima.
COMPONENT_MAX: Mapping[str, float] = {
    "market_regime_alignment": 20.0,
    "trend_alignment": 15.0,
    "setup_quality": 15.0,
    "volume_confirmation": 10.0,
    "relative_strength": 10.0,
    "volatility_conditions": 10.0,
    "fundamentals": 10.0,
    "liquidity": 5.0,
    "risk_reward": 5.0,
}


def _scale(value: float, low: float, high: float, maximum: float) -> float:
    """Map ``value`` from ``[low, high]`` onto ``[0, maximum]``, clamped."""
    if high <= low:
        return 0.0
    fraction = (value - low) / (high - low)
    return max(0.0, min(1.0, fraction)) * maximum


@dataclass(frozen=True, slots=True)
class ScoringInputs:
    """Optional context the score can use when it is available."""

    average_daily_volume: float | None = None
    spread_bps: float | None = None
    relative_strength_rating: float | None = None  # 0-99
    sector_strength: float | None = None
    risk_reward_ratio: float | None = None
    historical_win_rate: float | None = None


class SignalScoringEngine:
    """Produces the 0-100 signal score and its component breakdown."""

    def score(
        self,
        *,
        spec: StrategySpec,
        evaluation: Evaluation,
        features: FeatureSet,
        regime: RegimeState,
        context: EvalContext,
        inputs: ScoringInputs | None = None,
    ) -> ScoreBreakdown:
        inputs = inputs or ScoringInputs()
        components: dict[str, float] = {
            "market_regime_alignment": self._regime_alignment(spec, regime),
            "trend_alignment": self._trend_alignment(features, spec.side),
            "setup_quality": self._setup_quality(evaluation, features, inputs),
            "volume_confirmation": self._volume_confirmation(features),
            "relative_strength": self._relative_strength(features, inputs),
            "volatility_conditions": self._volatility_conditions(features, spec),
            "liquidity": self._liquidity(features, inputs),
            "risk_reward": self._risk_reward(inputs),
        }

        applies = features.asset_class is AssetClass.EQUITIES and context.fundamentals.is_available
        if applies:
            components["fundamentals"] = self._fundamentals(context)
            return ScoreBreakdown(components)

        # Rescale the remaining components to a 100-point maximum so that
        # regime thresholds carry the same meaning for every asset class.
        available_max = sum(COMPONENT_MAX[k] for k in components)
        factor = 100.0 / available_max if available_max > 0 else 1.0
        return ScoreBreakdown({k: round(v * factor, 4) for k, v in components.items()})

    # -- components ---------------------------------------------------------

    @staticmethod
    def _regime_alignment(spec: StrategySpec, regime: RegimeState) -> float:
        """Points for the strategy sitting in a regime that suits it.

        Weighted by regime confidence: a perfect match in an unclear regime is
        worth less than a good match in an obvious one.
        """
        from ..regime.engine import ALLOWED_STRATEGY_TYPES, is_strategy_type_allowed

        allowed, _ = is_strategy_type_allowed(spec.strategy_type, regime)
        if not allowed:
            return 0.0

        maximum = COMPONENT_MAX["market_regime_alignment"]
        # Base: the primary regime explicitly favours this strategy type.
        base = 0.7 if spec.strategy_type in ALLOWED_STRATEGY_TYPES.get(regime.primary, frozenset()) else 0.45
        # Supporting tags that also favour it add a little more.
        supporting = sum(
            1
            for tag in regime.tags
            if spec.strategy_type in ALLOWED_STRATEGY_TYPES.get(tag, frozenset())
        )
        base = min(1.0, base + 0.10 * supporting)
        return round(base * maximum * regime.confidence, 4)

    @staticmethod
    def _trend_alignment(features: FeatureSet, side: Side) -> float:
        maximum = COMPONENT_MAX["trend_alignment"]
        score = 0.0

        # Moving-average stack (up to 40% of the component).
        if features.get("ma_alignment", False):
            score += 0.40 * maximum
        else:
            ma50, ma200 = features.number("ma50"), features.number("ma200")
            if ma50 is not None and ma200 is not None and ma50 > ma200:
                score += 0.20 * maximum

        # Trend strength via ADX (up to 35%): 25+ is the conventional
        # threshold for "trending"; 40+ is a strong trend.
        adx = features.number("adx_14")
        if adx is not None:
            score += _scale(adx, 15.0, 40.0, 0.35 * maximum)

        # Confirmed higher highs and higher lows (25%).
        if features.get("higher_highs_lows", False):
            score += 0.25 * maximum

        if side is Side.SHORT:
            # Mirror the reading for short setups.
            score = maximum - score
        return round(min(maximum, max(0.0, score)), 4)

    @staticmethod
    def _setup_quality(evaluation: Evaluation, features: FeatureSet, inputs: ScoringInputs) -> float:
        """How cleanly the setup formed, not merely whether it triggered."""
        maximum = COMPONENT_MAX["setup_quality"]
        # Conditions met (50%). A triggered signal scores full marks here;
        # the ratio matters when ranking near-miss candidates.
        score = evaluation.pass_ratio * 0.50 * maximum

        # Base geometry (30%): a well-formed base with volume dry-up.
        if features.bases:
            base = features.bases[0]
            dryup = base.volume_dryup
            quality = 1.0 if (dryup is not None and dryup < 0.8) else 0.6
            if base.tight_closes:
                quality = min(1.0, quality + 0.2)
            score += quality * 0.30 * maximum
        elif features.candle_patterns:
            score += features.candle_patterns[0].strength * 0.30 * maximum

        # Historical precedent (20%), when a backtested win rate is supplied.
        if inputs.historical_win_rate is not None:
            score += _scale(inputs.historical_win_rate, 0.35, 0.65, 0.20 * maximum)

        return round(min(maximum, score), 4)

    @staticmethod
    def _volume_confirmation(features: FeatureSet) -> float:
        maximum = COMPONENT_MAX["volume_confirmation"]
        score = 0.0
        relative = features.number("relative_volume")
        if relative is not None:
            # 1.0 is average, 1.4 is the spec's confirmation threshold, 2.5+ is
            # exceptional.
            score += _scale(relative, 1.0, 2.5, 0.60 * maximum)
        if features.get("obv_rising", False):
            score += 0.20 * maximum
        ratio = features.number("up_down_volume_ratio")
        if ratio is not None:
            score += _scale(min(ratio, 5.0), 1.0, 2.0, 0.20 * maximum)
        return round(min(maximum, score), 4)

    @staticmethod
    def _relative_strength(features: FeatureSet, inputs: ScoringInputs) -> float:
        maximum = COMPONENT_MAX["relative_strength"]
        score = 0.0
        if inputs.relative_strength_rating is not None:
            score += _scale(inputs.relative_strength_rating, 50.0, 95.0, 0.50 * maximum)
        if inputs.sector_strength is not None:
            score += _scale(inputs.sector_strength, -0.02, 0.05, 0.25 * maximum)
        distance = features.number("distance_from_52w_high")
        if distance is not None:
            # Closer to the 52-week high is stronger; invert the distance.
            score += _scale(-distance, -0.25, 0.0, 0.25 * maximum)
        return round(min(maximum, score), 4)

    @staticmethod
    def _volatility_conditions(features: FeatureSet, spec: StrategySpec) -> float:
        """Points for volatility that suits the strategy family.

        Breakouts want calm before the move; mean reversion needs volatility to
        create the dislocation it trades. A single "low volatility is good"
        rule would misprice one of them.
        """
        maximum = COMPONENT_MAX["volatility_conditions"]
        percentile = features.number("volatility_percentile")
        if percentile is None:
            return 0.5 * maximum  # neutral when unknown

        if spec.strategy_type in {"mean_reversion", "reversal"}:
            ideal_low, ideal_high = 0.55, 0.90
        else:
            ideal_low, ideal_high = 0.15, 0.55

        if ideal_low <= percentile <= ideal_high:
            score = maximum
        else:
            distance = min(abs(percentile - ideal_low), abs(percentile - ideal_high))
            # Fall off linearly; 0.35 away from the band scores zero.
            score = maximum * max(0.0, 1.0 - distance / 0.35)
        return round(score, 4)

    @staticmethod
    def _fundamentals(context: EvalContext) -> float:
        maximum = COMPONENT_MAX["fundamentals"]
        f = context.fundamentals
        score = 0.0
        if f.earnings_growth is not None:
            score += _scale(f.earnings_growth, 0.10, 0.50, 0.30 * maximum)
        if f.sales_growth is not None:
            score += _scale(f.sales_growth, 0.10, 0.40, 0.20 * maximum)
        if f.roe is not None:
            score += _scale(f.roe, 0.10, 0.30, 0.20 * maximum)
        if f.industry_rank is not None:
            score += _scale(-float(f.industry_rank), -100.0, -1.0, 0.15 * maximum)
        if f.acc_dis_rating is not None:
            score += (0.15 * maximum) if f.acc_dis_rating.upper() in {"A", "B"} else 0.0
        return round(min(maximum, score), 4)

    @staticmethod
    def _liquidity(features: FeatureSet, inputs: ScoringInputs) -> float:
        maximum = COMPONENT_MAX["liquidity"]
        score = 0.0
        volume = inputs.average_daily_volume or features.number("avg_volume_50")
        if volume is not None:
            score += _scale(volume, 100_000.0, 2_000_000.0, 0.60 * maximum)
        if inputs.spread_bps is not None:
            # Tighter is better; 50bps is the hard limit, 5bps is excellent.
            score += _scale(-inputs.spread_bps, -50.0, -5.0, 0.40 * maximum)
        else:
            score += 0.20 * maximum  # unknown spread earns partial credit only
        return round(min(maximum, score), 4)

    @staticmethod
    def _risk_reward(inputs: ScoringInputs) -> float:
        maximum = COMPONENT_MAX["risk_reward"]
        if inputs.risk_reward_ratio is None:
            return 0.0
        # 2:1 is the section 7.1 floor, 4:1 earns full marks.
        return round(_scale(inputs.risk_reward_ratio, 1.5, 4.0, maximum), 4)
