"""Trailing stop engine (spec section 12).

A trailing stop may only ever move in the favourable direction. Every method
here returns a candidate, and :meth:`TrailingStopEngine.update` enforces the
ratchet — section 10.3's "never widen a stop to avoid a loss" is applied
structurally rather than left to a caller to remember.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..core.types import Position, Side


class TrailMethod(str, Enum):
    ATR = "atr"
    EMA = "ema"
    PERCENTAGE = "percentage"
    STATIC = "static"


@dataclass(frozen=True, slots=True)
class TrailDecision:
    """Outcome of one trailing update."""

    stop_price: float | None
    changed: bool
    method: TrailMethod
    stage: str
    reason: str = ""


#: Section 12.2 activation ladder, as ``(minimum gain, stage label)``.
STAGES: tuple[tuple[float, str], ...] = (
    (0.25, "aggressive"),
    (0.15, "tightening"),
    (0.10, "trailing"),
    (0.05, "breakeven"),
    (0.00, "static"),
)


def stage_for(gain_pct: float) -> str:
    for threshold, label in STAGES:
        if gain_pct >= threshold:
            return label
    return "static"


class TrailingStopEngine:
    """Computes trailing stops and enforces the one-way ratchet."""

    def __init__(self, *, atr_multiplier: float = 2.0) -> None:
        self.atr_multiplier = atr_multiplier

    def update(
        self,
        position: Position,
        price: float,
        *,
        atr: float | None = None,
        ema: float | None = None,
        trail_pct: float | None = None,
        method: TrailMethod | None = None,
    ) -> TrailDecision:
        """Compute the new stop for ``position`` at ``price``.

        Returns a decision whose ``stop_price`` is ``None`` when nothing should
        change. The stop never loosens: a candidate worse than the current stop
        is discarded.
        """
        position.mark(price)
        gain = position.gain_pct(price)
        stage = stage_for(gain)
        chosen = method or self._method_for(stage, atr, ema, trail_pct)

        if stage == "static":
            return TrailDecision(None, False, TrailMethod.STATIC, stage, "below trailing activation")

        if stage == "breakeven":
            candidate = position.entry_price
            reason = "gain above 5%: stop moved to breakeven"
        else:
            candidate = self._candidate(position, chosen, atr, ema, trail_pct, stage)
            reason = f"{chosen.value} trail at stage {stage}"
            if candidate is None:
                return TrailDecision(None, False, chosen, stage, "no input available for the chosen trail method")

        # The ratchet: only ever tighten.
        current = position.stop_price
        if current is not None:
            if position.side is Side.LONG and candidate <= current:
                return TrailDecision(None, False, chosen, stage, "candidate would loosen the stop; ignored")
            if position.side is Side.SHORT and candidate >= current:
                return TrailDecision(None, False, chosen, stage, "candidate would loosen the stop; ignored")

        # A trailing stop must never be placed through the current price.
        if position.side is Side.LONG and candidate >= price:
            return TrailDecision(None, False, chosen, stage, "candidate is at or above current price; ignored")
        if position.side is Side.SHORT and candidate <= price:
            return TrailDecision(None, False, chosen, stage, "candidate is at or below current price; ignored")

        position.stop_price = candidate
        position.trailing_active = stage != "breakeven"
        position.stop_moved_to_breakeven = position.stop_moved_to_breakeven or stage == "breakeven"
        return TrailDecision(candidate, True, chosen, stage, reason)

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _method_for(
        stage: str,
        atr: float | None,
        ema: float | None,
        trail_pct: float | None,
    ) -> TrailMethod:
        if stage in {"tightening", "aggressive"} and ema is not None:
            return TrailMethod.EMA
        if atr is not None:
            return TrailMethod.ATR
        if trail_pct is not None:
            return TrailMethod.PERCENTAGE
        return TrailMethod.STATIC

    def _candidate(
        self,
        position: Position,
        method: TrailMethod,
        atr: float | None,
        ema: float | None,
        trail_pct: float | None,
        stage: str,
    ) -> float | None:
        # Tighten the ATR multiplier as gains grow (section 12.2).
        multiplier = {
            "trailing": self.atr_multiplier,
            "tightening": self.atr_multiplier * 0.75,
            "aggressive": self.atr_multiplier * 0.5,
        }.get(stage, self.atr_multiplier)

        anchor_high = position.highest_price or position.entry_price
        anchor_low = position.lowest_price or position.entry_price

        if method is TrailMethod.ATR and atr:
            distance = atr * multiplier
            return anchor_high - distance if position.side is Side.LONG else anchor_low + distance
        if method is TrailMethod.EMA and ema is not None:
            return ema
        if method is TrailMethod.PERCENTAGE and trail_pct:
            factor = {"trailing": 1.0, "tightening": 0.75, "aggressive": 0.5}.get(stage, 1.0)
            distance = trail_pct * factor
            return (
                anchor_high * (1.0 - distance)
                if position.side is Side.LONG
                else anchor_low * (1.0 + distance)
            )
        return None
