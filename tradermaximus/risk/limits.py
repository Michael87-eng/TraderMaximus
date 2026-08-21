"""Hard risk limits and asset-specific calibration (spec sections 8, 9.2).

These values are the system's non-negotiable boundary. Section 30 states that
no strategy, model, forecast or AI judgement may override them, which this
module enforces structurally: the limits are frozen, and the only sanctioned
way to change one is a config layer at RISK precedence or above.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from ..core.config import SystemConfig
from ..core.errors import ConfigurationError
from ..core.types import AssetClass


@dataclass(frozen=True, slots=True)
class VolatilityBand:
    """Risk budget for one volatility band, per section 8.1."""

    max_volatility: float  # upper bound of the band (annualised)
    risk_per_trade: float


@dataclass(frozen=True, slots=True)
class AssetLimits:
    """Per-asset-class calibration (section 9.2)."""

    asset_class: AssetClass
    risk_bands: tuple[VolatilityBand, ...]
    max_exposure_per_trade: float
    max_position_volume_pct: float
    max_exposure_total: float
    min_price: float
    min_liquidity: float
    max_stop_pct: float
    min_stop_pct: float
    max_leverage: float = 0.0
    trailing_pct: float = 0.05

    def risk_per_trade(self, volatility: float | None) -> float:
        """Risk budget for the given annualised volatility.

        Unknown volatility resolves to the *most conservative* band, never the
        most generous — uncertainty must reduce risk, not enable it.
        """
        if volatility is None:
            return min(band.risk_per_trade for band in self.risk_bands)
        for band in self.risk_bands:
            if volatility < band.max_volatility:
                return band.risk_per_trade
        return self.risk_bands[-1].risk_per_trade


#: Section 8.1 / 9.2 defaults.
EQUITIES_LIMITS = AssetLimits(
    asset_class=AssetClass.EQUITIES,
    risk_bands=(
        VolatilityBand(0.40, 0.015),
        VolatilityBand(0.70, 0.010),
        VolatilityBand(float("inf"), 0.005),
    ),
    max_exposure_per_trade=0.10,
    max_position_volume_pct=0.05,
    max_exposure_total=0.50,
    min_price=5.00,
    min_liquidity=100_000.0,
    max_stop_pct=0.07,
    min_stop_pct=0.04,
    trailing_pct=0.06,
)

CRYPTO_LIMITS = AssetLimits(
    asset_class=AssetClass.CRYPTO,
    risk_bands=(
        VolatilityBand(0.60, 0.0075),
        VolatilityBand(0.85, 0.0050),
        VolatilityBand(float("inf"), 0.0025),
    ),
    max_exposure_per_trade=0.05,
    max_position_volume_pct=0.02,
    max_exposure_total=0.40,
    min_price=0.01,
    min_liquidity=100_000.0,
    max_stop_pct=0.05,
    min_stop_pct=0.02,
    trailing_pct=0.11,
)

FOREX_LIMITS = AssetLimits(
    asset_class=AssetClass.FOREX,
    risk_bands=(
        VolatilityBand(0.30, 0.015),
        VolatilityBand(0.60, 0.010),
        VolatilityBand(float("inf"), 0.005),
    ),
    max_exposure_per_trade=0.05,
    max_position_volume_pct=0.005,
    max_exposure_total=0.30,
    min_price=0.0001,
    min_liquidity=1_000_000.0,
    max_stop_pct=0.05,
    min_stop_pct=0.015,
    trailing_pct=0.04,
)

ASSET_LIMITS: Mapping[AssetClass, AssetLimits] = {
    AssetClass.EQUITIES: EQUITIES_LIMITS,
    AssetClass.CRYPTO: CRYPTO_LIMITS,
    AssetClass.FOREX: FOREX_LIMITS,
}


@dataclass(frozen=True, slots=True)
class HardLimits:
    """Portfolio-wide hard limits (section 8.1)."""

    max_risk_per_trade: float = 0.005
    max_total_portfolio_risk: float = 0.02
    max_portfolio_exposure: float = 0.50
    max_net_exposure: float = 0.50
    max_gross_exposure: float = 1.00
    max_daily_loss: float = 0.015
    max_weekly_loss: float = 0.03
    max_monthly_drawdown: float = 0.06
    max_portfolio_drawdown: float = 0.10
    max_positions_total: int = 15
    max_positions_by_class: Mapping[str, int] = field(
        default_factory=lambda: {"equities": 10, "crypto": 5, "forex": 3}
    )
    max_sector_exposure: float = 0.30
    max_asset_class_exposure: float = 0.40
    max_correlated_exposure: float = 0.20
    correlation_threshold: float = 0.70
    max_top3_concentration: float = 0.40
    max_top5_concentration: float = 0.60
    max_single_position: float = 0.15
    max_avg_pairwise_correlation: float = 0.60
    min_diversification_ratio: float = 0.30
    max_leverage: float = 1.0
    max_daily_trades: int = 20
    max_daily_orders: int = 50
    max_order_size: float = 0.10
    max_spread_bps: float = 50.0
    max_slippage_bps: float = 30.0
    max_var_95: float = 0.03
    max_expected_shortfall_95: float = 0.05
    cash_reserve: float = 0.30

    def __post_init__(self) -> None:
        if self.max_risk_per_trade <= 0 or self.max_risk_per_trade > 0.10:
            raise ConfigurationError(
                f"max_risk_per_trade {self.max_risk_per_trade} is outside the sane range (0, 0.10]"
            )
        if self.max_total_portfolio_risk < self.max_risk_per_trade:
            raise ConfigurationError(
                "max_total_portfolio_risk must be at least max_risk_per_trade; "
                f"got {self.max_total_portfolio_risk} < {self.max_risk_per_trade}"
            )
        if self.max_expected_shortfall_95 <= self.max_var_95:
            # ES is an average of losses beyond VaR, so it is mathematically at
            # least VaR. A limit that inverts them could never bind.
            raise ConfigurationError(
                f"max_expected_shortfall_95 ({self.max_expected_shortfall_95}) must exceed "
                f"max_var_95 ({self.max_var_95}); ES is always >= VaR by construction"
            )

    @classmethod
    def from_config(cls, config: SystemConfig) -> "HardLimits":
        """Build limits from a governed configuration."""
        return cls(
            max_risk_per_trade=config.require_float("max_risk_per_trade"),
            max_total_portfolio_risk=config.require_float("max_total_portfolio_risk"),
            max_portfolio_exposure=config.require_float("max_portfolio_exposure"),
            max_net_exposure=config.require_float("max_net_exposure"),
            max_gross_exposure=float(config.get("max_gross_exposure", 1.0)),
            max_daily_loss=config.require_float("max_daily_loss"),
            max_weekly_loss=config.require_float("max_weekly_loss"),
            max_monthly_drawdown=config.require_float("max_monthly_drawdown"),
            max_portfolio_drawdown=config.require_float("max_portfolio_drawdown"),
            max_positions_total=int(config.get("max_positions_total", 15)),
            max_positions_by_class={
                "equities": int(config.get("max_positions_equities", 10)),
                "crypto": int(config.get("max_positions_crypto", 5)),
                "forex": int(config.get("max_positions_forex", 3)),
            },
            max_sector_exposure=float(config.get("max_sector_exposure", 0.30)),
            max_asset_class_exposure=float(config.get("max_asset_class_exposure", 0.40)),
            max_correlated_exposure=float(config.get("max_correlated_exposure", 0.20)),
            correlation_threshold=float(config.get("correlation_threshold", 0.70)),
            max_top3_concentration=float(config.get("max_top3_concentration", 0.40)),
            max_top5_concentration=float(config.get("max_top5_concentration", 0.60)),
            max_single_position=float(config.get("max_single_position", 0.15)),
            max_avg_pairwise_correlation=float(config.get("max_avg_pairwise_correlation", 0.60)),
            min_diversification_ratio=float(config.get("min_diversification_ratio", 0.30)),
            max_leverage=float(config.get("max_leverage", 1.0)),
            max_daily_trades=int(config.get("max_daily_trades", 20)),
            max_daily_orders=int(config.get("max_daily_orders", 50)),
            max_order_size=float(config.get("max_order_size", 0.10)),
            max_spread_bps=float(config.get("max_spread_bps", 50.0)),
            max_slippage_bps=float(config.get("max_slippage_bps", 30.0)),
            max_var_95=float(config.get("max_var_95", 0.03)),
            max_expected_shortfall_95=config.require_float("max_expected_shortfall_95"),
            cash_reserve=float(config.get("cash_reserve", 0.30)),
        )

    def max_positions_for(self, asset_class: AssetClass) -> int:
        return int(self.max_positions_by_class.get(asset_class.value, self.max_positions_total))


def limits_for(asset_class: AssetClass) -> AssetLimits:
    limits = ASSET_LIMITS.get(asset_class)
    if limits is None:
        raise ConfigurationError(f"No risk calibration defined for asset class {asset_class}")
    return limits
