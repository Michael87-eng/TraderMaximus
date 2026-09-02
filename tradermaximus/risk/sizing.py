"""Position sizing engine (spec sections 9, 18.3).

Size follows from risk, never from conviction::

    risk_budget   = equity * risk_per_trade(volatility)
    position_size = risk_budget / stop_distance

That quotient is then reduced — never increased — by every applicable cap:
per-trade notional exposure, market-impact liquidity limits, available cash,
the regime confidence multiplier and volatility scaling. The smallest cap wins,
the result is floored onto the instrument's lot grid, and a size below the
minimum tradeable unit produces NO TRADE rather than a token position whose
costs would swamp its edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from ..core.types import AssetClass, Instrument
from .limits import AssetLimits, HardLimits, limits_for


class SizingRejection(str, Enum):
    BELOW_MINIMUM_SIZE = "position_size_below_minimum_tradeable_unit"
    PRICE_BELOW_MINIMUM = "instrument_price_below_asset_class_minimum"
    NO_RISK_BUDGET = "risk_budget_is_zero_or_negative"
    INVALID_STOP_DISTANCE = "stop_distance_is_zero_or_negative"
    INSUFFICIENT_CASH = "insufficient_available_cash"
    NO_LIQUIDITY = "insufficient_liquidity_for_any_position"


@dataclass(frozen=True, slots=True)
class SizingDecision:
    """Sized position with the full cap ladder retained for audit."""

    approved: bool
    symbol: str
    quantity: float = 0.0
    notional: float = 0.0
    risk_amount: float = 0.0
    risk_budget: float = 0.0
    risk_pct: float = 0.0
    stop_distance: float = 0.0
    raw_quantity: float = 0.0
    binding_cap: str = "none"
    caps: Mapping[str, float] = field(default_factory=dict)
    volatility_adjustment: float = 1.0
    regime_multiplier: float = 1.0
    rejection: SizingRejection | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "approved": self.approved,
            "symbol": self.symbol,
            "quantity": self.quantity,
            "notional": round(self.notional, 2),
            "risk_amount": round(self.risk_amount, 2),
            "risk_pct": round(self.risk_pct, 6),
            "raw_quantity": round(self.raw_quantity, 6),
            "binding_cap": self.binding_cap,
            "caps": {k: round(v, 6) for k, v in self.caps.items()},
            "volatility_adjustment": round(self.volatility_adjustment, 4),
            "regime_multiplier": self.regime_multiplier,
            "rejection": self.rejection.value if self.rejection else None,
            "reason": self.reason,
        }


class PositionSizingEngine:
    """Turns a risk budget and a stop distance into a tradeable quantity."""

    def __init__(
        self,
        limits: HardLimits | None = None,
        *,
        baseline_volatility: Mapping[AssetClass, float] | None = None,
        volatility_target: float | None = None,
        max_volatility_scale: float = 1.0,
    ) -> None:
        self.limits = limits or HardLimits()
        # Section 18.3 reference volatilities. A market running hotter than its
        # baseline is scaled down proportionally.
        self.baseline_volatility = baseline_volatility or {
            AssetClass.EQUITIES: 0.25,
            AssetClass.CRYPTO: 0.60,
            AssetClass.FOREX: 0.10,
        }
        # -- volatility targeting (Phase 5) ---------------------------------
        #
        # When set, one annualised volatility figure replaces the per-class
        # baselines, so every instrument is sized toward the same volatility
        # contribution instead of toward its own class's normal.
        #
        # ``max_volatility_scale`` caps the scale-up. It defaults to 1.0, which
        # means targeting can only ever shrink a position, because raising it
        # is the one direction that adds risk: EURUSD runs at about 4%
        # annualised volatility, so an uncapped 15% target would ask for 3.75x
        # the position and breach section 18.3's "only ever scales down". Raise
        # this only deliberately; the section 8.1 per-trade risk cap still
        # binds either way.
        self.volatility_target = volatility_target
        self.max_volatility_scale = max(0.0, max_volatility_scale)

    def size(
        self,
        *,
        instrument: Instrument,
        equity: float,
        entry_price: float,
        stop_distance: float,
        volatility: float | None = None,
        average_daily_volume: float | None = None,
        available_cash: float | None = None,
        regime_multiplier: float = 1.0,
        risk_pct_override: float | None = None,
        apply_volatility_scaling: bool = True,
    ) -> SizingDecision:
        """Compute the position size.

        Args:
            equity: account equity the risk budget is a percentage of.
            stop_distance: risk per unit, from :class:`~tradermaximus.risk.stops.StopLossEngine`.
            volatility: annualised realised volatility, selecting the risk band.
            average_daily_volume: for the market-impact cap.
            available_cash: cash cap, honouring the section 25.1 reserve.
            regime_multiplier: 0.5 when regime confidence is low (section 5.3).
            risk_pct_override: strategy-level risk budget, still capped by the
                hard per-trade limit.
        """
        asset_limits: AssetLimits = limits_for(instrument.asset_class)
        symbol = instrument.symbol

        if entry_price < asset_limits.min_price:
            return SizingDecision(
                approved=False,
                symbol=symbol,
                rejection=SizingRejection.PRICE_BELOW_MINIMUM,
                reason=f"price {entry_price} below {asset_limits.min_price} minimum for {instrument.asset_class.value}",
            )
        if stop_distance <= 0:
            return SizingDecision(
                approved=False,
                symbol=symbol,
                rejection=SizingRejection.INVALID_STOP_DISTANCE,
                reason=f"stop distance {stop_distance} must be positive",
            )

        # -- risk budget ----------------------------------------------------
        asset_risk_pct = asset_limits.risk_per_trade(volatility)
        if risk_pct_override is not None:
            asset_risk_pct = min(asset_risk_pct, risk_pct_override)
        # The hard per-trade limit is the ceiling regardless of asset band.
        risk_pct = min(asset_risk_pct, self.limits.max_risk_per_trade)

        volatility_adjustment = 1.0
        if apply_volatility_scaling and volatility and volatility > 0:
            reference = (
                self.volatility_target
                if self.volatility_target is not None
                else self.baseline_volatility.get(instrument.asset_class, volatility)
            )
            volatility_adjustment = min(
                self.max_volatility_scale, reference / volatility
            )

        effective_multiplier = max(0.0, regime_multiplier) * volatility_adjustment
        risk_budget = equity * risk_pct * effective_multiplier

        if risk_budget <= 0:
            return SizingDecision(
                approved=False,
                symbol=symbol,
                risk_pct=risk_pct,
                rejection=SizingRejection.NO_RISK_BUDGET,
                reason=f"risk budget is {risk_budget:.4f}; equity={equity}, multiplier={effective_multiplier:.3f}",
            )

        # -- the core quotient ----------------------------------------------
        raw_quantity = risk_budget / stop_distance

        # -- cap ladder (every entry is an upper bound in units) -------------
        caps: dict[str, float] = {"risk_budget": raw_quantity}

        caps["max_exposure_per_trade"] = (equity * asset_limits.max_exposure_per_trade) / entry_price
        caps["max_single_position"] = (equity * self.limits.max_single_position) / entry_price

        if average_daily_volume is not None and average_daily_volume > 0:
            # Section 18.2: never take more of the tape than we could exit.
            caps["market_impact"] = average_daily_volume * asset_limits.max_position_volume_pct
            caps["max_order_size"] = average_daily_volume * self.limits.max_order_size

        if available_cash is not None:
            caps["available_cash"] = max(0.0, available_cash) / entry_price

        binding_cap = min(caps, key=lambda name: caps[name])
        capped_quantity = caps[binding_cap]

        if capped_quantity <= 0:
            rejection = (
                SizingRejection.INSUFFICIENT_CASH
                if binding_cap == "available_cash"
                else SizingRejection.NO_LIQUIDITY
            )
            return SizingDecision(
                approved=False,
                symbol=symbol,
                risk_budget=risk_budget,
                risk_pct=risk_pct,
                stop_distance=stop_distance,
                raw_quantity=raw_quantity,
                binding_cap=binding_cap,
                caps=caps,
                rejection=rejection,
                reason=f"cap {binding_cap!r} permits no position",
            )

        quantity = instrument.round_size(capped_quantity)

        if quantity < instrument.min_order_size or quantity <= 0:
            return SizingDecision(
                approved=False,
                symbol=symbol,
                quantity=quantity,
                risk_budget=risk_budget,
                risk_pct=risk_pct,
                stop_distance=stop_distance,
                raw_quantity=raw_quantity,
                binding_cap=binding_cap,
                caps=caps,
                volatility_adjustment=volatility_adjustment,
                regime_multiplier=regime_multiplier,
                rejection=SizingRejection.BELOW_MINIMUM_SIZE,
                reason=(
                    f"sized {quantity} units, below the {instrument.min_order_size} minimum; "
                    "the position is too small to justify its transaction costs"
                ),
            )

        notional = quantity * entry_price
        risk_amount = quantity * stop_distance

        return SizingDecision(
            approved=True,
            symbol=symbol,
            quantity=quantity,
            notional=notional,
            risk_amount=risk_amount,
            risk_budget=risk_budget,
            risk_pct=risk_amount / equity if equity > 0 else 0.0,
            stop_distance=stop_distance,
            raw_quantity=raw_quantity,
            binding_cap=binding_cap,
            caps=caps,
            volatility_adjustment=volatility_adjustment,
            regime_multiplier=regime_multiplier,
            reason=(
                f"{quantity:g} units risking {risk_amount:,.2f} "
                f"({risk_amount / equity:.3%} of equity); binding cap: {binding_cap}"
            ),
        )
