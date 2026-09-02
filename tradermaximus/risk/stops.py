"""Stop-loss engine (spec section 10) — **corrected V4 formula**.

The rule
--------

::

    atr_distance       = ATR(14) * atr_multiplier
    structure_distance = |entry - nearest structural level|
    minimum_distance   = min_stop_pct * entry
    maximum_distance   = max_stop_pct * entry

    raw_stop_distance  = max(atr_distance, structure_distance, minimum_distance)
    stop_distance      = min(raw_stop_distance, maximum_distance)

    if structure_distance > maximum_distance:
        REJECT THE TRADE

Why the rejection clause matters
--------------------------------

The naive implementation clamps the stop to ``maximum_distance`` and takes the
trade anyway. That is the single most damaging bug available here, because it
*looks* conservative — the loss per share is capped — while actually destroying
the edge. Placing the stop inside the structural level means normal noise
around that level takes the position out before the thesis has had a chance to
be right or wrong. The result is a strategy that loses small amounts
repeatedly and never realises the wins the backtest promised.

So when the market's own structure demands more room than the risk budget
allows, the correct answer is *not to take the trade*. Risk is defined by
market structure; the account's tolerance decides whether that structure is
affordable, not where the stop goes.

Note the asymmetry: an ATR distance wider than the maximum is *clamped*, not
rejected. ATR is a volatility estimate, not a level the market must respect, so
capping it costs nothing structural.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from ..core.types import AssetClass, Side
from ..indicators.structure import Level, nearest_resistance, nearest_support
from .limits import AssetLimits, limits_for


class StopModel(str, Enum):
    VOLATILITY = "volatility"
    STRUCTURE = "structure"
    PERCENTAGE = "percentage"
    TIME = "time"


class StopRejection(str, Enum):
    """Why a stop could not be placed within tolerance."""

    STRUCTURE_TOO_WIDE = "structural_stop_exceeds_maximum_risk_tolerance"
    INVALID_ENTRY = "invalid_entry_price"
    NO_VOLATILITY_ESTIMATE = "no_volatility_estimate_available"
    DEGENERATE_DISTANCE = "computed_stop_distance_is_not_positive"


#: Relative tolerance for the boundary comparison. Floating-point products
#: like ``0.07 * 100.0`` land a few ULPs off the exact value; without a
#: tolerance a structural stop sitting *exactly* at the maximum could be
#: rejected or accepted depending on the entry price's binary representation.
#: The spec rejects only when the structural stop genuinely *exceeds* the
#: tolerance, so an exact match is accepted.
_BOUNDARY_TOLERANCE = 1e-9


#: Section 10.2: ATR multiplier by volatility regime.
#: ATR multipliers by volatility percentile.
#:
#: Widened from 1.2/1.5/1.8 to 1.5/1.75/2.0 under the Phase 5 brief ("1.5x ATR
#: for low volatility, 2.0x ATR for high volatility"). The intent is that a
#: volatile market gets more room before the stop is hit, rather than being
#: stopped out by noise the strategy expected.
#:
#: This does not loosen risk. The stop *distance* only ever widens; the risk
#: budget per trade is unchanged, so the sizer takes a proportionally smaller
#: position and the money at risk stays capped at the section 8.1 limit. The
#: section 8.1 max_stop_pct ceiling (7% equities, 5% crypto and FX) still
#: clamps the result, and a structure distance beyond that ceiling still
#: rejects the trade outright.
ATR_MULTIPLIERS: dict[str, float] = {
    "low": 1.5,
    "moderate": 1.75,
    "high": 2.0,
}


def atr_multiplier_for(volatility_percentile: float | None) -> float:
    """Pick the ATR multiplier from the volatility percentile.

    Unknown volatility uses the moderate multiplier: widening the stop on an
    unknown is not conservative (it enlarges the loss), and tightening it
    invites noise stop-outs.
    """
    if volatility_percentile is None:
        return ATR_MULTIPLIERS["moderate"]
    if volatility_percentile < 0.25:
        return ATR_MULTIPLIERS["low"]
    if volatility_percentile > 0.75:
        return ATR_MULTIPLIERS["high"]
    return ATR_MULTIPLIERS["moderate"]


@dataclass(frozen=True, slots=True)
class StopDecision:
    """The stop calculation with every intermediate value preserved.

    Keeping the components means a journal entry answers "why was this trade
    rejected" without re-running the calculation.
    """

    accepted: bool
    entry_price: float
    side: Side
    stop_price: float | None = None
    stop_distance: float | None = None
    atr_distance: float | None = None
    structure_distance: float | None = None
    minimum_distance: float | None = None
    maximum_distance: float | None = None
    raw_distance: float | None = None
    model: StopModel = StopModel.VOLATILITY
    clamped: bool = False
    rejection: StopRejection | None = None
    reason: str = ""

    @property
    def stop_pct(self) -> float | None:
        if self.stop_distance is None or self.entry_price <= 0:
            return None
        return self.stop_distance / self.entry_price

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "stop_price": self.stop_price,
            "stop_distance": self.stop_distance,
            "stop_pct": round(self.stop_pct, 6) if self.stop_pct is not None else None,
            "atr_distance": self.atr_distance,
            "structure_distance": self.structure_distance,
            "minimum_distance": self.minimum_distance,
            "maximum_distance": self.maximum_distance,
            "raw_distance": self.raw_distance,
            "clamped": self.clamped,
            "model": self.model.value,
            "rejection": self.rejection.value if self.rejection else None,
            "reason": self.reason,
        }


class StopLossEngine:
    """Computes protective stops under the corrected section 10.1 formula."""

    def __init__(self, *, default_atr_multiplier: float = 1.5) -> None:
        self.default_atr_multiplier = default_atr_multiplier

    def compute(
        self,
        *,
        entry_price: float,
        side: Side,
        asset_class: AssetClass,
        atr: float | None = None,
        structure_level: float | None = None,
        volatility_percentile: float | None = None,
        atr_multiplier: float | None = None,
        max_stop_pct: float | None = None,
        min_stop_pct: float | None = None,
    ) -> StopDecision:
        """Compute the stop, or reject the trade.

        Args:
            entry_price: intended entry.
            side: long or short; determines which way the stop sits.
            asset_class: supplies default min/max stop percentages.
            atr: ATR(14) in price units.
            structure_level: the support (long) or resistance (short) the stop
                must sit beyond. ``None`` means no structural constraint.
            volatility_percentile: selects the ATR multiplier when one is not
                supplied explicitly.
            atr_multiplier: strategy-level override.
            max_stop_pct / min_stop_pct: strategy-level overrides of the
                asset-class defaults.
        """
        limits: AssetLimits = limits_for(asset_class)
        max_pct = max_stop_pct if max_stop_pct is not None else limits.max_stop_pct
        min_pct = min_stop_pct if min_stop_pct is not None else limits.min_stop_pct

        if entry_price <= 0:
            return StopDecision(
                accepted=False,
                entry_price=entry_price,
                side=side,
                rejection=StopRejection.INVALID_ENTRY,
                reason=f"entry price {entry_price} must be positive",
            )

        # A min above the max can never be satisfied; treat the max as binding
        # rather than rejecting every trade for the asset class.
        if min_pct > max_pct:
            min_pct = max_pct

        multiplier = atr_multiplier if atr_multiplier is not None else atr_multiplier_for(volatility_percentile)
        if atr_multiplier is None and volatility_percentile is None:
            multiplier = self.default_atr_multiplier

        maximum_distance = max_pct * entry_price
        minimum_distance = min_pct * entry_price
        atr_distance = atr * multiplier if atr is not None and atr > 0 else None

        structure_distance: float | None = None
        if structure_level is not None:
            # Only a level on the protective side of the entry constrains the
            # stop: resistance above a long tells us nothing about its stop.
            if side is Side.LONG and structure_level < entry_price:
                structure_distance = entry_price - structure_level
            elif side is Side.SHORT and structure_level > entry_price:
                structure_distance = structure_level - entry_price

        if atr_distance is None and structure_distance is None:
            # Percentage stop is the last resort, and only if a floor exists.
            if minimum_distance <= 0:
                return StopDecision(
                    accepted=False,
                    entry_price=entry_price,
                    side=side,
                    rejection=StopRejection.NO_VOLATILITY_ESTIMATE,
                    reason="no ATR, structural level or minimum percentage available to size the stop",
                )

        # ---- The corrected formula ----------------------------------------
        candidates = [d for d in (atr_distance, structure_distance, minimum_distance) if d is not None]
        raw_distance = max(candidates)
        stop_distance = min(raw_distance, maximum_distance)
        clamped = raw_distance > maximum_distance * (1.0 + _BOUNDARY_TOLERANCE)

        # Structural stops are respected or the trade is skipped — never tightened.
        if structure_distance is not None and structure_distance > maximum_distance * (1.0 + _BOUNDARY_TOLERANCE):
            return StopDecision(
                accepted=False,
                entry_price=entry_price,
                side=side,
                atr_distance=atr_distance,
                structure_distance=structure_distance,
                minimum_distance=minimum_distance,
                maximum_distance=maximum_distance,
                raw_distance=raw_distance,
                rejection=StopRejection.STRUCTURE_TOO_WIDE,
                reason=(
                    f"structural stop {structure_distance:.4f} "
                    f"({structure_distance / entry_price:.2%}) exceeds maximum tolerance "
                    f"{maximum_distance:.4f} ({max_pct:.2%}); tightening it would place the stop "
                    "inside market structure and forfeit the edge"
                ),
            )

        if stop_distance <= 0:
            return StopDecision(
                accepted=False,
                entry_price=entry_price,
                side=side,
                rejection=StopRejection.DEGENERATE_DISTANCE,
                reason=f"computed stop distance {stop_distance} is not positive",
            )

        stop_price = (
            entry_price - stop_distance if side is Side.LONG else entry_price + stop_distance
        )

        model = StopModel.VOLATILITY
        if structure_distance is not None and structure_distance == raw_distance:
            model = StopModel.STRUCTURE
        elif minimum_distance == raw_distance:
            model = StopModel.PERCENTAGE

        return StopDecision(
            accepted=True,
            entry_price=entry_price,
            side=side,
            stop_price=round(stop_price, 8),
            stop_distance=stop_distance,
            atr_distance=atr_distance,
            structure_distance=structure_distance,
            minimum_distance=minimum_distance,
            maximum_distance=maximum_distance,
            raw_distance=raw_distance,
            model=model,
            clamped=clamped,
            reason=(
                f"stop {stop_distance / entry_price:.2%} from entry via {model.value} model"
                + (" (clamped to maximum tolerance)" if clamped else "")
            ),
        )

    def from_levels(
        self,
        *,
        entry_price: float,
        side: Side,
        asset_class: AssetClass,
        supports: Sequence[Level] = (),
        resistances: Sequence[Level] = (),
        atr: float | None = None,
        volatility_percentile: float | None = None,
        **kwargs: object,
    ) -> StopDecision:
        """Convenience wrapper that picks the structural level from detected levels."""
        if side is Side.LONG:
            level = nearest_support(entry_price, supports)
        else:
            level = nearest_resistance(entry_price, resistances)
        return self.compute(
            entry_price=entry_price,
            side=side,
            asset_class=asset_class,
            atr=atr,
            structure_level=level.price if level else None,
            volatility_percentile=volatility_percentile,
            **kwargs,  # type: ignore[arg-type]
        )


def breakeven_stop(entry_price: float, side: Side) -> float:
    """Section 10.3: move the stop to breakeven after a 10% gain."""
    return entry_price
