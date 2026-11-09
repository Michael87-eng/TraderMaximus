"""Configuration governance (spec section 29).

Configuration arrives from several places at once: emergency toggles, hard risk
limits, broker constraints, portfolio caps, strategy parameters, signal
preferences and — last — whatever the AI layer suggests. Section 29.1 fixes the
precedence of those layers, and this module is the only place that ordering is
encoded.

The important property is *directional*: a lower-precedence layer can never
loosen a value set by a higher-precedence one. An AI recommendation may tighten
``max_risk_per_trade`` but can never raise it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import ConfigurationError

try:  # PyYAML is optional; JSON configs work without it.
    import yaml
except ImportError:  # pragma: no cover - exercised only on minimal installs
    yaml = None  # type: ignore[assignment]


class Layer(IntEnum):
    """Configuration layers, lowest number wins (section 29.1)."""

    EMERGENCY = 1
    RISK = 2
    BROKER = 3
    PORTFOLIO = 4
    STRATEGY = 5
    SIGNAL = 6
    AI = 7

    @classmethod
    def from_name(cls, name: str) -> "Layer":
        try:
            return cls[name.strip().upper()]
        except KeyError as exc:
            raise ConfigurationError(f"Unknown configuration layer: {name!r}") from exc


#: Keys that only EMERGENCY or RISK layers may ever set. Anything below those
#: layers proposing one of these is a governance violation, not a merge.
HARD_RISK_KEYS: frozenset[str] = frozenset(
    {
        "max_risk_per_trade",
        "max_total_portfolio_risk",
        "max_portfolio_exposure",
        "max_net_exposure",
        "max_daily_loss",
        "max_weekly_loss",
        "max_monthly_drawdown",
        "max_portfolio_drawdown",
        "max_leverage",
        "max_expected_shortfall_95",
        "max_var_95",
    }
)

#: For these keys "safer" means smaller. Every hard limit is of this kind: a
#: lower cap is always the more conservative choice.
_TIGHTEN_IS_LOWER: frozenset[str] = HARD_RISK_KEYS | frozenset(
    {
        "max_sector_exposure",
        "max_asset_class_exposure",
        "max_correlated_exposure",
        "max_positions",
        "max_daily_trades",
        "max_order_size",
        "max_spread_bps",
        "max_slippage_bps",
        "max_staleness_seconds",
    }
)

#: For these keys "safer" means larger — raising a floor tightens the system.
_TIGHTEN_IS_HIGHER: frozenset[str] = frozenset(
    {
        "min_signal_score",
        "min_liquidity",
        "min_regime_confidence",
        "data_quality_threshold",
    }
)


@dataclass(frozen=True, slots=True)
class Setting:
    """One resolved value plus the provenance needed for the audit trail."""

    key: str
    value: Any
    layer: Layer
    source: str = "default"

    def describe(self) -> str:
        return f"{self.key}={self.value!r} (layer={self.layer.name}, source={self.source})"


class ConfigGovernor:
    """Resolves settings across layers under section 29.1 precedence."""

    def __init__(self) -> None:
        self._settings: dict[str, Setting] = {}
        self._violations: list[str] = []

    # -- population ---------------------------------------------------------

    def apply(self, layer: Layer | str, values: Mapping[str, Any], source: str = "config") -> None:
        """Merge one layer's values, honouring precedence.

        A proposal is accepted when it comes from a higher-precedence layer, or
        when it comes from a lower one *and* tightens the setting.
        """
        layer = layer if isinstance(layer, Layer) else Layer.from_name(layer)
        for key, value in values.items():
            self._apply_one(layer, key, value, source)

    def _apply_one(self, layer: Layer, key: str, value: Any, source: str) -> None:
        if key in HARD_RISK_KEYS and layer > Layer.RISK:
            self._violations.append(
                f"{layer.name} layer ({source}) attempted to set hard risk key {key!r}; rejected"
            )
            return

        current = self._settings.get(key)
        if current is None:
            self._settings[key] = Setting(key, value, layer, source)
            return

        if layer <= current.layer:
            # Same or higher precedence: authoritative.
            self._settings[key] = Setting(key, value, layer, source)
            return

        # Lower precedence: only accepted if it makes the system safer.
        if self._is_tightening(key, current.value, value):
            self._settings[key] = Setting(key, value, layer, source)
        else:
            self._violations.append(
                f"{layer.name} layer ({source}) tried to loosen {key!r} "
                f"from {current.value!r} to {value!r}; rejected"
            )

    @staticmethod
    def _is_tightening(key: str, current: Any, proposed: Any) -> bool:
        if not isinstance(current, (int, float)) or not isinstance(proposed, (int, float)):
            return False
        if isinstance(current, bool) or isinstance(proposed, bool):
            return False
        if key in _TIGHTEN_IS_LOWER:
            return proposed < current
        if key in _TIGHTEN_IS_HIGHER:
            return proposed > current
        return False

    # -- access -------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        setting = self._settings.get(key)
        return default if setting is None else setting.value

    def require(self, key: str) -> Any:
        setting = self._settings.get(key)
        if setting is None:
            raise ConfigurationError(f"Required configuration key missing: {key!r}")
        return setting.value

    def provenance(self, key: str) -> Setting | None:
        return self._settings.get(key)

    @property
    def violations(self) -> tuple[str, ...]:
        """Rejected override attempts, surfaced for review rather than hidden."""
        return tuple(self._violations)

    def as_dict(self) -> dict[str, Any]:
        return {key: setting.value for key, setting in sorted(self._settings.items())}

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            key: {"value": s.value, "layer": s.layer.name, "source": s.source}
            for key, s in sorted(self._settings.items())
        }


# ---------------------------------------------------------------------------
# Defaults straight from the specification
# ---------------------------------------------------------------------------

DEFAULT_EMERGENCY: dict[str, Any] = {
    "kill_switch_active": False,
    "forced_closure_triggered": False,
    "emergency_mode_active": False,
    "override_allowed": False,
}

DEFAULT_RISK: dict[str, Any] = {
    "max_risk_per_trade": 0.005,
    "max_total_portfolio_risk": 0.02,
    "max_portfolio_exposure": 0.50,
    "max_net_exposure": 0.50,
    "max_gross_exposure": 1.00,
    "max_daily_loss": 0.015,
    "max_weekly_loss": 0.03,
    "max_monthly_drawdown": 0.06,
    "max_portfolio_drawdown": 0.10,
    "max_leverage": 1.0,
    "max_var_95": 0.03,
    "max_expected_shortfall_95": 0.05,
}

DEFAULT_PORTFOLIO: dict[str, Any] = {
    "max_positions_total": 15,
    "max_positions_equities": 10,
    "max_positions_crypto": 5,
    "max_positions_forex": 3,
    "max_sector_exposure": 0.30,
    "max_asset_class_exposure": 0.40,
    "max_correlated_exposure": 0.20,
    "correlation_threshold": 0.70,
    "max_top3_concentration": 0.40,
    "max_top5_concentration": 0.60,
    "max_single_position": 0.15,
    "max_avg_pairwise_correlation": 0.60,
    "min_diversification_ratio": 0.30,
    "cash_reserve": 0.30,
}

DEFAULT_EXECUTION: dict[str, Any] = {
    "max_daily_trades": 20,
    "max_daily_orders": 50,
    "max_spread_bps": 50.0,
    "max_slippage_bps": 30.0,
    "max_order_size": 0.10,
    "latency_tolerance_ms": 2000,
    "taker_fee_bps": 10.0,
    "maker_fee_bps": 5.0,
    "max_order_retries": 3,
}

DEFAULT_DATA: dict[str, Any] = {
    "data_quality_threshold": 0.85,
    "max_staleness_seconds": 5.0,
    "provider_disagreement_tolerance": 0.005,
    "min_regime_confidence": 0.70,
    "fail_mode": "HALT",
}


@dataclass(slots=True)
class SystemConfig:
    """Fully resolved system configuration.

    Construct via :meth:`load` (YAML/JSON on disk) or :meth:`defaults`.
    """

    governor: ConfigGovernor = field(default_factory=ConfigGovernor)

    # -- construction -------------------------------------------------------

    @classmethod
    def defaults(cls) -> "SystemConfig":
        cfg = cls()
        cfg.governor.apply(Layer.EMERGENCY, DEFAULT_EMERGENCY, "builtin-default")
        cfg.governor.apply(Layer.RISK, DEFAULT_RISK, "builtin-default")
        cfg.governor.apply(Layer.PORTFOLIO, DEFAULT_PORTFOLIO, "builtin-default")
        cfg.governor.apply(Layer.BROKER, DEFAULT_EXECUTION, "builtin-default")
        cfg.governor.apply(Layer.PORTFOLIO, DEFAULT_DATA, "builtin-default")
        return cfg

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "SystemConfig":
        """Load defaults, then overlay a config file if one is supplied."""
        cfg = cls.defaults()
        if path is None:
            return cfg
        payload = _read_structured(Path(path))
        for layer_name, values in payload.items():
            if not isinstance(values, Mapping):
                raise ConfigurationError(
                    f"Configuration section {layer_name!r} must be a mapping, got {type(values).__name__}"
                )
            cfg.governor.apply(Layer.from_name(layer_name), values, str(path))
        return cfg

    # -- convenience accessors ---------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        return self.governor.get(key, default)

    def require(self, key: str) -> Any:
        return self.governor.require(key)

    def require_float(self, key: str) -> float:
        return float(self.require(key))

    def require_int(self, key: str) -> int:
        return int(self.require(key))

    def apply_ai_recommendation(self, values: Mapping[str, Any], source: str = "ai") -> tuple[str, ...]:
        """Let the AI layer propose settings; returns the rejected attempts.

        This is the seam where "the AI may recommend, the risk engine decides"
        becomes mechanical rather than aspirational.
        """
        before = len(self.governor.violations)
        self.governor.apply(Layer.AI, values, source)
        return self.governor.violations[before:]

    @property
    def kill_switch_active(self) -> bool:
        return bool(self.get("kill_switch_active", False))

    def max_positions_for(self, asset_class: str) -> int:
        return int(self.get(f"max_positions_{asset_class}", self.get("max_positions_total", 15)))


def _read_structured(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        raise ConfigurationError(f"Configuration file not found: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        if yaml is None:
            raise ConfigurationError(
                "PyYAML is required to read YAML configuration; install pyyaml or use JSON"
            )
        data = yaml.safe_load(text) or {}
    else:
        import json

        data = json.loads(text or "{}")
    if not isinstance(data, Mapping):
        raise ConfigurationError(f"Configuration root must be a mapping: {path}")
    return data


def merge_layers(pairs: Iterable[tuple[Layer | str, Mapping[str, Any]]]) -> ConfigGovernor:
    """Helper for tests and ad-hoc scripts: build a governor from layer pairs."""
    governor = ConfigGovernor()
    for layer, values in pairs:
        governor.apply(layer, values)
    return governor
