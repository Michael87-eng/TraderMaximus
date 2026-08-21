"""Strategy drift detection (spec section 21).

A strategy that passed every validation gate can still stop working: the
regime it exploited ends, the edge gets arbitraged away, or execution quality
degrades. Section 21 defines ten drift metrics measured over a rolling window
of recent trades against the validated baseline, and an escalating response —
monitor, halve size, disable.

The comparison is always *recent vs baseline*, never *recent vs the last
window*. Drift is a slow divergence from what was validated, and comparing
each window only to its predecessor would let a strategy walk arbitrarily far
from its baseline in steps too small to trigger anything.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Sequence

from ..core.types import TradeRecord
from ..observability.metrics import PerformanceMetrics, compute_metrics


class DriftSeverity(str, Enum):
    NONE = "none"
    MINOR = "minor"        # 1 metric breached
    MODERATE = "moderate"  # 2-3 breached
    SEVERE = "severe"      # 4+ breached


class DriftAction(str, Enum):
    MONITOR = "monitor"
    REDUCE_SIZE_50 = "reduce_size_50"
    DISABLE = "disable"
    PAPER_TEST = "paper_test"
    REQUIRE_VALIDATION = "require_revalidation"


@dataclass(frozen=True, slots=True)
class DriftThresholds:
    """Section 21.1 thresholds, all expressed as relative change."""

    sharpe_change: float = 0.30
    win_rate_change: float = 0.20
    avg_win_change: float = 0.25
    avg_loss_change: float = 0.25
    expectancy_change: float = 0.30
    drawdown_multiple: float = 1.50
    correlation_change: float = 0.50
    slippage_increase: float = 0.50
    volatility_change: float = 0.50
    window_trades: int = 30


@dataclass(frozen=True, slots=True)
class DriftMetric:
    name: str
    baseline: float
    current: float
    change: float
    threshold: float
    breached: bool

    def __str__(self) -> str:
        mark = "BREACH" if self.breached else "ok"
        return (
            f"[{mark}] {self.name}: {self.baseline:.4g} -> {self.current:.4g} "
            f"({self.change:+.1%}, limit {self.threshold:.0%})"
        )


@dataclass(frozen=True, slots=True)
class DriftReport:
    strategy_id: str
    severity: DriftSeverity
    metrics: tuple[DriftMetric, ...]
    actions: tuple[DriftAction, ...]
    sample_size: int
    reason: str = ""

    @property
    def breaches(self) -> tuple[DriftMetric, ...]:
        return tuple(m for m in self.metrics if m.breached)

    @property
    def should_disable(self) -> bool:
        return DriftAction.DISABLE in self.actions

    @property
    def size_multiplier(self) -> float:
        if DriftAction.DISABLE in self.actions:
            return 0.0
        if DriftAction.REDUCE_SIZE_50 in self.actions:
            return 0.5
        return 1.0

    def as_dict(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "severity": self.severity.value,
            "actions": [a.value for a in self.actions],
            "size_multiplier": self.size_multiplier,
            "sample_size": self.sample_size,
            "breaches": [str(m) for m in self.breaches],
            "reason": self.reason,
        }


#: Section 21.2 response table.
RESPONSES: Mapping[DriftSeverity, tuple[DriftAction, ...]] = {
    DriftSeverity.NONE: (),
    DriftSeverity.MINOR: (DriftAction.MONITOR,),
    DriftSeverity.MODERATE: (DriftAction.REDUCE_SIZE_50, DriftAction.PAPER_TEST),
    DriftSeverity.SEVERE: (
        DriftAction.DISABLE,
        DriftAction.PAPER_TEST,
        DriftAction.REQUIRE_VALIDATION,
    ),
}


def _relative_change(baseline: float, current: float) -> float:
    """Signed relative change, guarding a zero baseline."""
    if baseline == 0:
        return 0.0 if current == 0 else 1.0
    return (current - baseline) / abs(baseline)


class DriftDetector:
    """Compares recent live performance against the validated baseline."""

    def __init__(self, thresholds: DriftThresholds | None = None) -> None:
        self.thresholds = thresholds if thresholds is not None else DriftThresholds()

    def evaluate(
        self,
        strategy_id: str,
        baseline: PerformanceMetrics,
        recent_trades: Sequence[TradeRecord],
        *,
        recent_equity_curve: Sequence[float] = (),
        baseline_slippage_bps: float | None = None,
        recent_slippage_bps: float | None = None,
    ) -> DriftReport:
        """Assess drift for one strategy.

        Args:
            baseline: metrics from validation (in-sample or paper trading).
            recent_trades: the trailing window of live trades.
            recent_equity_curve: equity points over the same window, for
                Sharpe and drawdown.
        """
        t = self.thresholds
        trades = list(recent_trades)[-t.window_trades:]

        if len(trades) < max(10, t.window_trades // 3):
            return DriftReport(
                strategy_id,
                DriftSeverity.NONE,
                (),
                (),
                len(trades),
                reason=(
                    f"only {len(trades)} recent trades; need at least "
                    f"{max(10, t.window_trades // 3)} before drift is measurable"
                ),
            )

        current = compute_metrics(trades, recent_equity_curve)
        metrics: list[DriftMetric] = []

        def add(name: str, base: float, curr: float, threshold: float, *, direction: str = "any") -> None:
            change = _relative_change(base, curr)
            if direction == "down":
                breached = change < -threshold      # only deterioration counts
            elif direction == "up":
                breached = change > threshold
            else:
                breached = abs(change) > threshold
            metrics.append(DriftMetric(name, base, curr, change, threshold, breached))

        # Performance metrics: only *deterioration* is drift. A strategy that
        # suddenly earns more than its baseline is not misbehaving.
        add("sharpe", baseline.sharpe, current.sharpe, t.sharpe_change, direction="down")
        add("win_rate", baseline.win_rate, current.win_rate, t.win_rate_change, direction="down")
        add("expectancy", baseline.expectancy, current.expectancy, t.expectancy_change, direction="down")
        add("avg_win", baseline.avg_win, current.avg_win, t.avg_win_change, direction="down")

        # A *larger* average loss is the deterioration here.
        add("avg_loss", baseline.avg_loss, current.avg_loss, t.avg_loss_change, direction="up")

        if baseline.max_drawdown > 0 and recent_equity_curve:
            limit = baseline.max_drawdown * t.drawdown_multiple
            metrics.append(
                DriftMetric(
                    "max_drawdown",
                    baseline.max_drawdown,
                    current.max_drawdown,
                    _relative_change(baseline.max_drawdown, current.max_drawdown),
                    t.drawdown_multiple - 1.0,
                    current.max_drawdown > limit,
                )
            )

        if baseline_slippage_bps is not None and recent_slippage_bps is not None:
            add("slippage", baseline_slippage_bps, recent_slippage_bps, t.slippage_increase, direction="up")

        # Return volatility of the recent window vs baseline realised volatility.
        if len(recent_equity_curve) >= 20:
            from ..observability.metrics import returns_from_equity

            recent_returns = returns_from_equity(recent_equity_curve)
            if len(recent_returns) >= 2 and baseline.max_drawdown > 0:
                recent_vol = statistics.pstdev(recent_returns)
                baseline_vol = baseline.max_drawdown / 3.0  # rough baseline proxy
                add("return_volatility", baseline_vol, recent_vol, t.volatility_change, direction="up")

        breach_count = sum(1 for m in metrics if m.breached)
        severity = self._severity(breach_count)
        actions = RESPONSES[severity]
        reason = (
            f"{breach_count} of {len(metrics)} drift metrics breached over "
            f"{len(trades)} recent trades"
        )
        return DriftReport(strategy_id, severity, tuple(metrics), actions, len(trades), reason)

    @staticmethod
    def _severity(breaches: int) -> DriftSeverity:
        if breaches == 0:
            return DriftSeverity.NONE
        if breaches == 1:
            return DriftSeverity.MINOR
        if breaches <= 3:
            return DriftSeverity.MODERATE
        return DriftSeverity.SEVERE


@dataclass(slots=True)
class StrategyMonitor:
    """Tracks drift across every deployed strategy (section 28.2)."""

    detector: DriftDetector = field(default_factory=DriftDetector)
    baselines: dict[str, PerformanceMetrics] = field(default_factory=dict)
    disabled: set[str] = field(default_factory=set)
    size_multipliers: dict[str, float] = field(default_factory=dict)

    def set_baseline(self, strategy_id: str, metrics: PerformanceMetrics) -> None:
        self.baselines[strategy_id] = metrics
        self.size_multipliers[strategy_id] = 1.0

    def review(
        self,
        strategy_id: str,
        recent_trades: Sequence[TradeRecord],
        *,
        recent_equity_curve: Sequence[float] = (),
    ) -> DriftReport | None:
        baseline = self.baselines.get(strategy_id)
        if baseline is None:
            return None
        report = self.detector.evaluate(
            strategy_id, baseline, recent_trades, recent_equity_curve=recent_equity_curve
        )
        self.size_multipliers[strategy_id] = report.size_multiplier
        if report.should_disable:
            self.disabled.add(strategy_id)
        return report

    def is_enabled(self, strategy_id: str) -> bool:
        return strategy_id not in self.disabled

    def multiplier_for(self, strategy_id: str) -> float:
        return self.size_multipliers.get(strategy_id, 1.0)

    def reinstate(self, strategy_id: str, *, operator: str, reason: str) -> None:
        """Re-enable after revalidation. Requires an operator and a reason."""
        if not operator.strip() or not reason.strip():
            raise ValueError("reinstating a strategy requires both an operator and a reason")
        self.disabled.discard(strategy_id)
        self.size_multipliers[strategy_id] = 0.5  # resume at half size
