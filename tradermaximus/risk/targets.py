"""Take-profit engine (spec section 11).

Targets are scale-out ladders defined per strategy family. Section 11.2 forbids
lowering a target out of fear or raising it out of euphoria, so the ladder is
fixed at entry and only the *regime* may adjust it — and only through the
documented multiplier below, never ad hoc.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from ..core.types import Position, Side


@dataclass(frozen=True, slots=True)
class ProfitTarget:
    """One rung of the ladder: take ``fraction`` of the position at ``gain_pct``."""

    gain_pct: float
    fraction: float
    label: str = ""

    def price_for(self, entry_price: float, side: Side) -> float:
        return entry_price * (1.0 + self.gain_pct * side.sign)


@dataclass(frozen=True, slots=True)
class TargetLadder:
    targets: tuple[ProfitTarget, ...]
    trail_remainder: bool = True
    hold_rule: str = ""

    @property
    def scaled_fraction(self) -> float:
        return sum(t.fraction for t in self.targets)

    @property
    def remainder(self) -> float:
        return max(0.0, 1.0 - self.scaled_fraction)

    def prices(self, entry_price: float, side: Side) -> list[tuple[float, float]]:
        """``(price, fraction)`` pairs, ready to store on a position."""
        return [(t.price_for(entry_price, side), t.fraction) for t in self.targets]


#: Section 11.1 ladders keyed by strategy family.
LADDERS: Mapping[str, TargetLadder] = {
    "momentum": TargetLadder(
        targets=(
            ProfitTarget(0.10, 0.33, "TP1"),
            ProfitTarget(0.15, 0.33, "TP2"),
        ),
        hold_rule="trail final 34% with ATR x 2",
    ),
    "breakout": TargetLadder(
        targets=(
            ProfitTarget(0.10, 0.33, "TP1"),
            ProfitTarget(0.15, 0.33, "TP2"),
        ),
        hold_rule="trail final 34% with ATR x 2",
    ),
    "can_slim": TargetLadder(
        targets=(
            ProfitTarget(0.20, 0.25, "TP1"),
            ProfitTarget(0.40, 0.25, "TP2"),
            ProfitTarget(0.75, 0.25, "TP3"),
        ),
        # Section 6.2.5: a 20% gain inside three weeks marks a potential
        # market leader, and cutting it early is how the biggest winners are
        # lost. The 8-week hold overrides the ladder in that case.
        hold_rule="if +20% within 3 weeks, hold at least 8 weeks",
    ),
    "pullback": TargetLadder(
        targets=(ProfitTarget(0.08, 0.50, "TP1"),),
        hold_rule="trail final 50% with EMA10",
    ),
    "gap": TargetLadder(
        targets=(ProfitTarget(0.10, 0.50, "TP1"),),
        hold_rule="trail final 50% with EMA5",
    ),
    "reversal": TargetLadder(
        targets=(ProfitTarget(0.10, 1.00, "TP1"),),
        trail_remainder=False,
        hold_rule="exit fully at the next structural level",
    ),
    "mean_reversion": TargetLadder(
        targets=(ProfitTarget(0.05, 0.50, "TP1"),),
        hold_rule="first target is the 20-period mean; trail the rest with EMA10",
    ),
    "institutional": TargetLadder(
        targets=(
            ProfitTarget(0.20, 0.25, "TP1"),
            ProfitTarget(0.50, 0.25, "TP2"),
            ProfitTarget(1.00, 0.25, "TP3"),
        ),
        hold_rule="hold core position, trail with the 50-day MA",
    ),
}

#: The Phase 5 scale-out ladder: 50% at +5%, 30% at +10%, trail the last 20%
#: with 2x ATR.
#:
#: Expressed in raw percentage of entry price, which is what was specified.
#: Note what that means per asset class, because it is not neutral: EURUSD's
#: realised volatility is about 4% annualised, so a 5% move is roughly a year
#: of travel and this ladder's first rung is effectively unreachable on FX
#: inside a five-day holding limit. On crypto, where daily moves of 5% are
#: routine, the same rung fires almost immediately. A percentage ladder is an
#: implicit bet on the asset's volatility; ATR_SCALE_OUT below states the same
#: shape in units that travel with the market.
SCALE_OUT = TargetLadder(
    targets=(
        ProfitTarget(0.05, 0.50, "TP1"),
        ProfitTarget(0.10, 0.30, "TP2"),
    ),
    hold_rule="trail final 20% with ATR x 2",
)

#: The same 50/30/20 shape denominated in ATR multiples rather than percent, so
#: one rung means the same thing on EURUSD as on SOL-USD. ``gain_pct`` here is
#: filled in per trade by :meth:`TakeProfitEngine.ladder_for` from the
#: instrument's own ATR; the constants are the multiples.
ATR_SCALE_OUT_MULTIPLES: tuple[tuple[float, float, str], ...] = (
    (1.5, 0.50, "TP1"),
    (3.0, 0.30, "TP2"),
)

LADDERS = dict(LADDERS) | {"scale_out": SCALE_OUT}

DEFAULT_LADDER = LADDERS["momentum"]


class TakeProfitEngine:
    """Builds target ladders and decides which rungs a price has reached."""

    def __init__(self, *, regime_adjustment: bool = True) -> None:
        self.regime_adjustment = regime_adjustment

    @staticmethod
    def atr_scale_out(atr: float, entry_price: float) -> TargetLadder:
        """The 50/30/20 scale-out ladder in ATR multiples, not raw percent.

        One ATR is what the instrument actually moves in a bar, so a rung set
        at 1.5 ATR is the same distance in opportunity terms on EURUSD as on
        SOL-USD. The percentage form of the same ladder is not: on a 4%-vol
        currency pair a +5% rung is a year away, and on crypto it is a Tuesday.
        """
        if atr <= 0 or entry_price <= 0:
            return SCALE_OUT
        return TargetLadder(
            targets=tuple(
                ProfitTarget(multiple * atr / entry_price, fraction, label)
                for multiple, fraction, label in ATR_SCALE_OUT_MULTIPLES
            ),
            hold_rule="trail final 20% with ATR x 2",
        )

    def ladder_for(
        self,
        strategy_type: str,
        *,
        trend_strength: float | None = None,
    ) -> TargetLadder:
        """Return the ladder, optionally stretched or compressed by trend strength.

        Section 11.2 permits adjusting targets by regime — wider in a strong
        trend, tighter in a weak one — and nothing else. ``trend_strength`` is
        an ADX reading; the multiplier stays within [0.8, 1.25] so no
        adjustment can transform the ladder into a different strategy.
        """
        ladder = LADDERS.get(strategy_type, DEFAULT_LADDER)
        if not self.regime_adjustment or trend_strength is None:
            return ladder

        if trend_strength >= 40:
            multiplier = 1.25
        elif trend_strength >= 25:
            multiplier = 1.0
        else:
            multiplier = 0.8

        if multiplier == 1.0:
            return ladder
        return TargetLadder(
            targets=tuple(
                ProfitTarget(t.gain_pct * multiplier, t.fraction, t.label) for t in ladder.targets
            ),
            trail_remainder=ladder.trail_remainder,
            hold_rule=ladder.hold_rule,
        )

    @staticmethod
    def reached(position: Position, price: float, targets: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
        """Target rungs the current price has hit, in ladder order."""
        hit: list[tuple[float, float]] = []
        for target_price, fraction in targets:
            if position.side is Side.LONG and price >= target_price:
                hit.append((target_price, fraction))
            elif position.side is Side.SHORT and price <= target_price:
                hit.append((target_price, fraction))
        return hit

    @staticmethod
    def is_potential_leader(position: Position, price: float, *, now_days: float) -> bool:
        """Section 6.2.5 super-winner test: +20% within three weeks."""
        return position.gain_pct(price) >= 0.20 and now_days <= 21
