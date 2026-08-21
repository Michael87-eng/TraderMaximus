"""Machine-testable strategy specifications (spec section 6.1).

Section 6.1 requires every strategy to be a specification a machine can
evaluate, not prose a human interprets. A :class:`StrategySpec` is therefore
data: a list of named :class:`Condition` predicates over a
:class:`~tradermaximus.indicators.engine.FeatureSet`, plus the stop model,
target ladder and regime eligibility.

Two consequences follow, and both matter more than the tidiness:

* Every entry decision produces an :class:`Evaluation` naming exactly which
  conditions passed and which failed, so "why did it not trade" is answerable
  from the journal alone.
* The same object drives live trading and the backtester, so a strategy cannot
  drift between what was validated and what runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from ..core.types import AssetClass, Side
from ..indicators.engine import FeatureSet


@dataclass(frozen=True, slots=True)
class Fundamentals:
    """Fundamental inputs for the CAN SLIM family (section 6.2.5).

    These cannot be derived from price data and must be supplied by a data
    provider. When absent, fundamentals-gated strategies simply do not qualify
    — the system does not guess an EPS rating.
    """

    eps_rating: float | None = None          # 0-99
    rs_rating: float | None = None           # 0-99
    industry_rank: int | None = None         # 1 = strongest industry group
    earnings_growth: float | None = None     # most recent quarter, YoY
    sales_growth: float | None = None
    roe: float | None = None
    smr_rating: str | None = None            # A-E
    acc_dis_rating: str | None = None        # A-E
    institutional_owners_change: float | None = None
    quarters_of_accumulation: int | None = None
    years_since_ipo: float | None = None

    @property
    def is_available(self) -> bool:
        return self.eps_rating is not None or self.rs_rating is not None


@dataclass(frozen=True, slots=True)
class EvalContext:
    """Everything a condition may consult beyond the feature set."""

    features: FeatureSet
    fundamentals: Fundamentals = field(default_factory=Fundamentals)
    sector_strength: float | None = None   # relative performance vs benchmark
    extras: Mapping[str, object] = field(default_factory=dict)


#: A condition returns ``(passed, human-readable detail)``.
Predicate = Callable[[EvalContext], "tuple[bool, str]"]


@dataclass(frozen=True, slots=True)
class Condition:
    """One named, testable entry/exit rule."""

    name: str
    predicate: Predicate
    description: str = ""

    def evaluate(self, context: EvalContext) -> "ConditionResult":
        try:
            passed, detail = self.predicate(context)
        except (KeyError, TypeError, ValueError) as exc:
            # A missing feature means the condition cannot be shown to hold,
            # which is a failure — never a pass by default.
            return ConditionResult(self.name, False, f"could not evaluate: {exc}")
        return ConditionResult(self.name, passed, detail)


@dataclass(frozen=True, slots=True)
class ConditionResult:
    name: str
    passed: bool
    detail: str = ""

    def __str__(self) -> str:
        return f"[{'PASS' if self.passed else 'FAIL'}] {self.name}: {self.detail}"


@dataclass(frozen=True, slots=True)
class Evaluation:
    """Result of testing every entry condition for one strategy."""

    strategy_id: str
    triggered: bool
    results: tuple[ConditionResult, ...]
    side: Side = Side.LONG

    @property
    def passed(self) -> tuple[ConditionResult, ...]:
        return tuple(r for r in self.results if r.passed)

    @property
    def failed(self) -> tuple[ConditionResult, ...]:
        return tuple(r for r in self.results if not r.passed)

    @property
    def pass_ratio(self) -> float:
        return len(self.passed) / len(self.results) if self.results else 0.0

    def summary(self) -> str:
        if self.triggered:
            return f"{self.strategy_id}: all {len(self.results)} entry conditions met"
        failures = "; ".join(f"{r.name} ({r.detail})" for r in self.failed)
        return f"{self.strategy_id}: {len(self.failed)}/{len(self.results)} conditions failed - {failures}"


@dataclass(frozen=True, slots=True)
class StopSpec:
    """Stop parameters this strategy hands to the stop engine."""

    atr_multiplier: float = 1.5
    max_stop_pct: float | None = None   # None defers to the asset-class limit
    min_stop_pct: float | None = None
    use_structure: bool = True
    time_stop_days: int | None = None


@dataclass(frozen=True, slots=True)
class StrategySpec:
    """A complete, machine-testable strategy definition."""

    id: str
    name: str
    strategy_type: str                       # momentum | breakout | can_slim | ...
    side: Side
    asset_classes: frozenset[AssetClass]
    entry_conditions: tuple[Condition, ...]
    stop: StopSpec = field(default_factory=StopSpec)
    target_ladder: str = "momentum"
    confirmation_conditions: tuple[Condition, ...] = ()
    invalidation_conditions: tuple[Condition, ...] = ()
    min_data_points: int = 100
    max_holding_days: int | None = None
    requires_fundamentals: bool = False
    notes: str = ""

    def supports(self, asset_class: AssetClass) -> bool:
        return asset_class in self.asset_classes

    def evaluate(self, context: EvalContext) -> Evaluation:
        """Test every entry condition. All must pass for a trigger."""
        results = tuple(c.evaluate(context) for c in self.entry_conditions)
        triggered = bool(results) and all(r.passed for r in results)
        return Evaluation(self.id, triggered, results, self.side)

    def check_invalidation(self, context: EvalContext) -> list[ConditionResult]:
        """Invalidation conditions currently true — reasons to exit."""
        return [r for c in self.invalidation_conditions if (r := c.evaluate(context)).passed]

    def structure_level(self, features: FeatureSet) -> float | None:
        """The level the stop must sit beyond, if this strategy uses structure."""
        if not self.stop.use_structure:
            return None
        level = features.nearest_support() if self.side is Side.LONG else features.nearest_resistance()
        return level.price if level else None


# ---------------------------------------------------------------------------
# Condition builders — small helpers that keep the library declarative
# ---------------------------------------------------------------------------


def feature_between(key: str, low: float, high: float, *, name: str | None = None) -> Condition:
    def predicate(ctx: EvalContext) -> tuple[bool, str]:
        value = ctx.features.number(key)
        if value is None:
            return False, f"{key} unavailable"
        return low <= value <= high, f"{key}={value:.4g} (want {low:g}..{high:g})"

    return Condition(name or f"{key}_between_{low:g}_{high:g}", predicate)


def feature_above(key: str, threshold: float, *, name: str | None = None) -> Condition:
    def predicate(ctx: EvalContext) -> tuple[bool, str]:
        value = ctx.features.number(key)
        if value is None:
            return False, f"{key} unavailable"
        return value > threshold, f"{key}={value:.4g} (want > {threshold:g})"

    return Condition(name or f"{key}_above_{threshold:g}", predicate)


def feature_below(key: str, threshold: float, *, name: str | None = None) -> Condition:
    def predicate(ctx: EvalContext) -> tuple[bool, str]:
        value = ctx.features.number(key)
        if value is None:
            return False, f"{key} unavailable"
        return value < threshold, f"{key}={value:.4g} (want < {threshold:g})"

    return Condition(name or f"{key}_below_{threshold:g}", predicate)


def flag_is_true(key: str, *, name: str | None = None) -> Condition:
    def predicate(ctx: EvalContext) -> tuple[bool, str]:
        value = ctx.features.get(key, False)
        return bool(value), f"{key}={value}"

    return Condition(name or key, predicate)


def price_above_feature(key: str, *, name: str | None = None) -> Condition:
    def predicate(ctx: EvalContext) -> tuple[bool, str]:
        value = ctx.features.number(key)
        if value is None:
            return False, f"{key} unavailable"
        close = ctx.features.close
        return close > value, f"close={close:.4g} vs {key}={value:.4g}"

    return Condition(name or f"price_above_{key}", predicate)


def custom(name: str, predicate: Predicate, description: str = "") -> Condition:
    return Condition(name, predicate, description)


def all_of(name: str, conditions: Sequence[Condition]) -> Condition:
    """Combine conditions so that any one failing fails the group."""

    def predicate(ctx: EvalContext) -> tuple[bool, str]:
        results = [c.evaluate(ctx) for c in conditions]
        failed = [r for r in results if not r.passed]
        if failed:
            return False, "; ".join(f"{r.name} failed" for r in failed)
        return True, f"all {len(results)} sub-conditions met"

    return Condition(name, predicate)


def any_of(name: str, conditions: Sequence[Condition]) -> Condition:
    def predicate(ctx: EvalContext) -> tuple[bool, str]:
        results = [c.evaluate(ctx) for c in conditions]
        passed = [r for r in results if r.passed]
        if passed:
            return True, f"{passed[0].name} met"
        return False, "no alternative met"

    return Condition(name, predicate)
