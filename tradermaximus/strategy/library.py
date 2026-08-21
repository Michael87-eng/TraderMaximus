"""Core strategy library (spec section 6.2).

Each of the ten strategies is expressed as a :class:`StrategySpec` whose entry
conditions mirror the specification clause by clause. Where the spec gives a
numeric threshold it appears verbatim; where it describes a qualitative idea
("volume dries up in the handle") the measurable proxy is named in the
condition and explained in a comment.
"""

from __future__ import annotations

from typing import Mapping

from ..core.types import AssetClass, Side
from .spec import (
    Condition,
    EvalContext,
    Fundamentals,
    StopSpec,
    StrategySpec,
    custom,
    feature_above,
    feature_below,
    feature_between,
    flag_is_true,
    price_above_feature,
)

EQUITIES_ONLY = frozenset({AssetClass.EQUITIES})
ALL_ASSETS = frozenset({AssetClass.EQUITIES, AssetClass.CRYPTO, AssetClass.FOREX})
FOREX_ONLY = frozenset({AssetClass.FOREX})


# ---------------------------------------------------------------------------
# Shared conditions
# ---------------------------------------------------------------------------


def _near_52w_high(max_distance: float = 0.05) -> Condition:
    """Price within ``max_distance`` of its 52-week high — proven leadership."""

    def predicate(ctx: EvalContext) -> tuple[bool, str]:
        distance = ctx.features.number("distance_from_52w_high")
        if distance is None:
            return False, "52-week high unavailable"
        return distance <= max_distance, f"{distance:.2%} below 52w high (want <= {max_distance:.0%})"

    return Condition("within_5pct_of_52w_high", predicate)


def _uptrend_structure() -> Condition:
    """Price above the 50- and 200-period averages, both rising."""

    def predicate(ctx: EvalContext) -> tuple[bool, str]:
        f = ctx.features
        ma50, ma200 = f.number("ma50"), f.number("ma200")
        if ma50 is None or ma200 is None:
            return False, "insufficient history for MA50/MA200"
        above = f.close > ma50 and f.close > ma200
        stacked = ma50 > ma200
        return above and stacked, (
            f"close={f.close:.4g} ma50={ma50:.4g} ma200={ma200:.4g} "
            f"(above={above}, stacked={stacked})"
        )

    return Condition("established_uptrend", predicate)


def _sector_strength_positive() -> Condition:
    """Sector outperforming the benchmark. Neutral when data is absent."""

    def predicate(ctx: EvalContext) -> tuple[bool, str]:
        if ctx.sector_strength is None:
            # Unknown sector strength is not a failure: it is one supporting
            # factor among many, and the scoring engine already withholds the
            # points. Failing here would block every trade without sector data.
            return True, "sector strength unavailable (treated as neutral)"
        return ctx.sector_strength > 0, f"sector strength {ctx.sector_strength:+.2%}"

    return Condition("sector_strength_positive", predicate)


def _can_slim_fundamentals() -> Condition:
    """CAN SLIM fundamental gate (section 6.2.5).

    Deliberately strict: if the fundamental data is missing the condition
    fails. A CAN SLIM breakout without the fundamentals is a different, unvalidated
    strategy wearing the same name.
    """

    def predicate(ctx: EvalContext) -> tuple[bool, str]:
        f: Fundamentals = ctx.fundamentals
        if not f.is_available:
            return False, "fundamental data unavailable"

        checks: list[tuple[str, bool]] = [
            ("eps_rating>80", (f.eps_rating or 0) > 80),
            ("rs_rating>80", (f.rs_rating or 0) > 80),
            ("industry_top_40", (f.industry_rank or 999) <= 40),
            ("earnings_growth>25%", (f.earnings_growth or 0.0) > 0.25),
            ("roe>20%", (f.roe or 0.0) > 0.20),
            ("acc_dis_A_or_B", (f.acc_dis_rating or "E").upper() in {"A", "B"}),
        ]
        failed = [name for name, ok in checks if not ok]
        if failed:
            return False, "failed: " + ", ".join(failed)
        return True, "all CAN SLIM fundamental criteria met"

    return Condition("can_slim_fundamentals", predicate)


def _base_present(kind: str, *, min_dryup: float | None = 0.8) -> Condition:
    """A base of the given geometry formed, with volume drying up in it."""

    def predicate(ctx: EvalContext) -> tuple[bool, str]:
        base = ctx.features.base_of(kind)
        if base is None:
            return False, f"no {kind} detected"
        if min_dryup is not None:
            dryup = base.volume_dryup
            if dryup is None:
                return False, f"{kind} found but volume dry-up not measurable"
            if dryup > min_dryup:
                return False, f"{kind} volume dry-up {dryup:.2f} above {min_dryup:.2f} threshold"
        return True, (
            f"{kind}: depth={base.depth:.1%}, {base.length_weeks:.1f} weeks, pivot={base.pivot:.4g}"
        )

    return Condition(f"{kind}_formed", predicate)


def _breakout_above_pivot(kind: str) -> Condition:
    """Close above the base's pivot point."""

    def predicate(ctx: EvalContext) -> tuple[bool, str]:
        base = ctx.features.base_of(kind)
        if base is None:
            return False, f"no {kind} to break out from"
        close = ctx.features.close
        return close > base.pivot, f"close={close:.4g} vs pivot={base.pivot:.4g}"

    return Condition(f"breakout_above_{kind}_pivot", predicate)


def _bullish_reversal_at_support(max_distance: float = 0.03) -> Condition:
    """A bullish candle pattern printing near a tested support level.

    Context is the whole point (section 6.2.4): the same hammer is noise in
    mid-range and a signal at support.
    """

    def predicate(ctx: EvalContext) -> tuple[bool, str]:
        f = ctx.features
        bullish = [m for m in f.candle_patterns if m.bullish and m.name != "doji"]
        if not bullish:
            return False, "no bullish reversal pattern"
        support = f.nearest_support()
        if support is None:
            return False, f"{bullish[0].name} found but no support level identified"
        distance = abs(f.close - support.price) / f.close if f.close > 0 else 1.0
        if distance > max_distance:
            return False, f"{bullish[0].name} is {distance:.2%} from support (want <= {max_distance:.0%})"
        return True, f"{bullish[0].name} (strength {bullish[0].strength:.2f}) at support {support.price:.4g}"

    return Condition("bullish_reversal_at_support", predicate)


def _pullback_to_moving_average(keys: tuple[str, ...] = ("ema10", "ema20", "ma50"), tolerance: float = 0.02) -> Condition:
    """Price has retraced into one of the trend's moving averages."""

    def predicate(ctx: EvalContext) -> tuple[bool, str]:
        f = ctx.features
        for key in keys:
            value = f.number(key)
            if value is None or value <= 0:
                continue
            if abs(f.close - value) / value <= tolerance:
                return True, f"close={f.close:.4g} within {tolerance:.0%} of {key}={value:.4g}"
        return False, f"close={f.close:.4g} not near any of {', '.join(keys)}"

    return Condition("pullback_to_moving_average", predicate)


def _deviation_from_mean(atr_multiple: float = 2.0) -> Condition:
    """Price stretched more than ``atr_multiple`` ATRs from its 20-period mean."""

    def predicate(ctx: EvalContext) -> tuple[bool, str]:
        f = ctx.features
        mean = f.number("bb_middle")
        atr = f.number("atr_14")
        if mean is None or atr is None or atr <= 0:
            return False, "mean or ATR unavailable"
        deviation = abs(f.close - mean) / atr
        return deviation > atr_multiple, f"{deviation:.2f} ATRs from mean (want > {atr_multiple:g})"

    return Condition("stretched_from_mean", predicate)


def _gap_up(min_gap: float) -> Condition:
    def predicate(ctx: EvalContext) -> tuple[bool, str]:
        gap = ctx.features.number("gap_pct")
        if gap is None:
            return False, "gap unavailable"
        return gap >= min_gap, f"gap {gap:+.2%} (want >= {min_gap:.0%})"

    return Condition(f"gap_up_{min_gap:.0%}", predicate)


def _institutional_accumulation() -> Condition:
    """Two-plus quarters of accumulation, confirmed by up/down volume."""

    def predicate(ctx: EvalContext) -> tuple[bool, str]:
        f = ctx.fundamentals
        quarters = f.quarters_of_accumulation or 0
        rating = (f.acc_dis_rating or "E").upper()
        ratio = ctx.features.number("up_down_volume_ratio")
        if quarters < 2:
            return False, f"only {quarters} quarters of accumulation (want >= 2)"
        if rating not in {"A", "B"}:
            return False, f"ACC/DIS rating {rating} (want A or B)"
        if ratio is None or ratio < 1.5:
            return False, f"up/down volume ratio {ratio} (want >= 1.5)"
        return True, f"{quarters} quarters accumulating, ACC/DIS {rating}, U/D {ratio:.2f}"

    return Condition("institutional_accumulation", predicate)


def _gap_holding(max_fill: float = 0.20) -> Condition:
    """The gap held rather than filled (section 6.2.3).

    A gap that immediately retraces is a liquidity event, not a repricing. The
    test is how much of the gap distance the bar gave back intraday: filling
    more than ``max_fill`` of it means sellers met the move.
    """

    def predicate(ctx: EvalContext) -> tuple[bool, str]:
        f = ctx.features
        prev_close = f.number("prev_close")
        bar_open = f.number("bar_open")
        bar_low = f.number("bar_low")
        if prev_close is None or bar_open is None or bar_low is None:
            return False, "bar detail unavailable"

        gap_distance = bar_open - prev_close
        if gap_distance <= 0:
            return False, "no upward gap on this bar"

        filled = max(0.0, (bar_open - bar_low) / gap_distance)
        if filled > max_fill:
            return False, f"gap {filled:.0%} filled intraday (want <= {max_fill:.0%})"
        if f.close <= prev_close:
            return False, f"close {f.close:.4g} back below the pre-gap close {prev_close:.4g}"
        return True, f"gap held, only {filled:.0%} filled, close {f.close:.4g} > {prev_close:.4g}"

    return Condition("gap_holding", predicate)


# ---------------------------------------------------------------------------
# 6.2.1 Momentum Breakout (equities)
# ---------------------------------------------------------------------------

MOMENTUM_BREAKOUT = StrategySpec(
    id="STRATEGY_001",
    name="Momentum Breakout with Volume Confirmation",
    strategy_type="momentum",
    side=Side.LONG,
    asset_classes=EQUITIES_ONLY,
    entry_conditions=(
        flag_is_true("breakout_20", name="breakout_above_20day_high"),
        feature_above("relative_volume", 1.4, name="relative_volume_above_1.4"),
        feature_between("rsi_14", 55.0, 80.0, name="rsi_55_to_80"),
        feature_above("macd_histogram", 0.0, name="macd_histogram_positive"),
        _near_52w_high(0.05),
        _uptrend_structure(),
        _sector_strength_positive(),
    ),
    stop=StopSpec(atr_multiplier=1.5, max_stop_pct=0.07, min_stop_pct=0.04),
    target_ladder="momentum",
    invalidation_conditions=(
        feature_below("relative_volume", 0.8, name="volume_dried_up_after_entry"),
    ),
    min_data_points=252,
    max_holding_days=10,
    notes="Section 6.2.1. Entry on breakout close with volume confirmation.",
)


# ---------------------------------------------------------------------------
# 6.2.2 Pullback / MA bounce (all asset classes)
# ---------------------------------------------------------------------------

PULLBACK_MA_BOUNCE = StrategySpec(
    id="STRATEGY_002",
    name="Pullback to Moving Average",
    strategy_type="pullback",
    side=Side.LONG,
    asset_classes=ALL_ASSETS,
    entry_conditions=(
        _uptrend_structure(),
        _pullback_to_moving_average(),
        feature_below("relative_volume", 0.7, name="pullback_volume_drying_up"),
        feature_between("rsi_14", 40.0, 55.0, name="rsi_40_to_55_healthy_retracement"),
        _bullish_reversal_at_support(0.05),
        flag_is_true("stochastic_cross_up", name="stochastic_k_crossing_above_d"),
    ),
    stop=StopSpec(atr_multiplier=1.2, max_stop_pct=0.07),
    target_ladder="pullback",
    min_data_points=200,
    max_holding_days=15,
    notes="Section 6.2.2. Buys strength on weakness inside an established trend.",
)


# ---------------------------------------------------------------------------
# 6.2.3 Gap strategy (equities / crypto)
# ---------------------------------------------------------------------------

GAP_STRATEGY = StrategySpec(
    id="STRATEGY_003",
    name="Gap Up Continuation",
    strategy_type="gap",
    side=Side.LONG,
    asset_classes=frozenset({AssetClass.EQUITIES, AssetClass.CRYPTO}),
    entry_conditions=(
        _gap_up(0.03),
        feature_above("relative_volume", 2.0, name="relative_volume_above_2.0"),
        _uptrend_structure(),
        _gap_holding(0.20),
    ),
    stop=StopSpec(atr_multiplier=1.5, max_stop_pct=0.07, use_structure=True),
    target_ladder="gap",
    min_data_points=100,
    max_holding_days=10,
    notes="Section 6.2.3. Requires the gap to hold rather than fill.",
)


# ---------------------------------------------------------------------------
# 6.2.4 Reversal (all asset classes)
# ---------------------------------------------------------------------------

REVERSAL = StrategySpec(
    id="STRATEGY_004",
    name="Candlestick Reversal at Support",
    strategy_type="reversal",
    side=Side.LONG,
    asset_classes=ALL_ASSETS,
    entry_conditions=(
        _bullish_reversal_at_support(0.03),
        feature_below("rsi_14", 45.0, name="rsi_not_extended"),
        feature_below("bb_position", 0.35, name="price_in_lower_band_region"),
    ),
    stop=StopSpec(atr_multiplier=1.5, max_stop_pct=0.06),
    target_ladder="reversal",
    min_data_points=100,
    max_holding_days=10,
    notes="Section 6.2.4. Pattern strength is scored, so marginal shapes rank low.",
)


# ---------------------------------------------------------------------------
# 6.2.5 CAN SLIM cup-with-handle breakout (equities only)
# ---------------------------------------------------------------------------

CAN_SLIM_BREAKOUT = StrategySpec(
    id="STRATEGY_005",
    name="CAN SLIM Cup with Handle Breakout",
    strategy_type="can_slim",
    side=Side.LONG,
    asset_classes=EQUITIES_ONLY,
    entry_conditions=(
        _base_present("cup_with_handle", min_dryup=0.85),
        _breakout_above_pivot("cup_with_handle"),
        feature_above("relative_volume", 1.4, name="breakout_volume_above_1.4"),
        _can_slim_fundamentals(),
        _uptrend_structure(),
    ),
    stop=StopSpec(atr_multiplier=1.5, max_stop_pct=0.08, min_stop_pct=0.04),
    target_ladder="can_slim",
    min_data_points=252,
    requires_fundamentals=True,
    max_holding_days=None,
    notes="Section 6.2.5. 8-week hold rule applies to a 20% gain inside 3 weeks.",
)


# ---------------------------------------------------------------------------
# 6.2.6 Double bottom (equities only)
# ---------------------------------------------------------------------------

DOUBLE_BOTTOM = StrategySpec(
    id="STRATEGY_006",
    name="Double Bottom Breakout",
    strategy_type="can_slim",
    side=Side.LONG,
    asset_classes=EQUITIES_ONLY,
    entry_conditions=(
        _base_present("double_bottom", min_dryup=0.9),
        _breakout_above_pivot("double_bottom"),
        feature_above("relative_volume", 1.4, name="breakout_volume_above_1.4"),
        _can_slim_fundamentals(),
    ),
    stop=StopSpec(atr_multiplier=1.5, max_stop_pct=0.08, min_stop_pct=0.04),
    target_ladder="can_slim",
    min_data_points=252,
    requires_fundamentals=True,
    notes="Section 6.2.6. The second bottom must undercut the first (the shakeout).",
)


# ---------------------------------------------------------------------------
# 6.2.7 Flat base breakout (equities only)
# ---------------------------------------------------------------------------

FLAT_BASE_BREAKOUT = StrategySpec(
    id="STRATEGY_007",
    name="Flat Base Breakout",
    strategy_type="can_slim",
    side=Side.LONG,
    asset_classes=EQUITIES_ONLY,
    entry_conditions=(
        _base_present("flat_base", min_dryup=0.85),
        _breakout_above_pivot("flat_base"),
        feature_above("relative_volume", 1.4, name="breakout_volume_above_1.4"),
        flag_is_true("tight_closes", name="tight_weekly_closes"),
        custom(
            "rs_rating_above_80",
            lambda ctx: (
                (ctx.fundamentals.rs_rating or 0) > 80,
                f"RS rating {ctx.fundamentals.rs_rating}",
            ),
        ),
    ),
    stop=StopSpec(atr_multiplier=1.5, max_stop_pct=0.08, min_stop_pct=0.04),
    target_ladder="can_slim",
    min_data_points=200,
    requires_fundamentals=True,
    notes="Section 6.2.7. 5-6+ weeks sideways, correction no deeper than 15%.",
)


# ---------------------------------------------------------------------------
# 6.2.8 High tight flag (equities only) - rare
# ---------------------------------------------------------------------------

HIGH_TIGHT_FLAG = StrategySpec(
    id="STRATEGY_008",
    name="High Tight Flag",
    strategy_type="can_slim",
    side=Side.LONG,
    asset_classes=EQUITIES_ONLY,
    entry_conditions=(
        _base_present("high_tight_flag", min_dryup=None),
        _breakout_above_pivot("high_tight_flag"),
        feature_above("relative_volume", 1.5, name="breakout_volume_above_1.5"),
        _can_slim_fundamentals(),
    ),
    stop=StopSpec(atr_multiplier=1.5, max_stop_pct=0.08, min_stop_pct=0.04),
    target_ladder="can_slim",
    min_data_points=252,
    requires_fundamentals=True,
    notes="Section 6.2.8. Rare by design: needs a 100%+ advance in 4-8 weeks first.",
)


# ---------------------------------------------------------------------------
# 6.2.9 Institutional accumulation (all asset classes)
# ---------------------------------------------------------------------------

INSTITUTIONAL_ACCUMULATION = StrategySpec(
    id="STRATEGY_009",
    name="Institutional Accumulation",
    strategy_type="institutional",
    side=Side.LONG,
    asset_classes=ALL_ASSETS,
    entry_conditions=(
        _institutional_accumulation(),
        _uptrend_structure(),
        flag_is_true("acc_dist_rising", name="accumulation_distribution_rising"),
        feature_above("relative_volume", 1.2, name="relative_volume_above_1.2"),
    ),
    stop=StopSpec(atr_multiplier=1.5, max_stop_pct=0.08),
    target_ladder="institutional",
    min_data_points=252,
    requires_fundamentals=True,
    notes="Section 6.2.9. Long-horizon: scales out at 20/50/100/200%.",
)


# ---------------------------------------------------------------------------
# 6.2.10 ATR-based mean reversion (FOREX)
# ---------------------------------------------------------------------------

FOREX_MEAN_REVERSION = StrategySpec(
    id="STRATEGY_010",
    name="ATR-Based Mean Reversion",
    strategy_type="mean_reversion",
    side=Side.LONG,
    asset_classes=FOREX_ONLY,
    entry_conditions=(
        _deviation_from_mean(2.0),
        feature_below("rsi_14", 30.0, name="rsi_oversold"),
        feature_below("bb_position", 0.05, name="price_at_lower_bollinger_band"),
        _bullish_reversal_at_support(0.05),
    ),
    stop=StopSpec(atr_multiplier=1.5, max_stop_pct=0.05, use_structure=True),
    target_ladder="mean_reversion",
    min_data_points=100,
    max_holding_days=5,
    notes="Section 6.2.10. Long side only; the short mirror is a separate spec.",
)


ALL_STRATEGIES: tuple[StrategySpec, ...] = (
    MOMENTUM_BREAKOUT,
    PULLBACK_MA_BOUNCE,
    GAP_STRATEGY,
    REVERSAL,
    CAN_SLIM_BREAKOUT,
    DOUBLE_BOTTOM,
    FLAT_BASE_BREAKOUT,
    HIGH_TIGHT_FLAG,
    INSTITUTIONAL_ACCUMULATION,
    FOREX_MEAN_REVERSION,
)

STRATEGIES_BY_ID: Mapping[str, StrategySpec] = {s.id: s for s in ALL_STRATEGIES}


def strategies_for(asset_class: AssetClass) -> tuple[StrategySpec, ...]:
    return tuple(s for s in ALL_STRATEGIES if s.supports(asset_class))
