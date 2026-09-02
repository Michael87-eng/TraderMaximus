"""Strategy validation and deployment gates (spec sections 20.2, 20.4, 23).

A profitable backtest is not evidence. Section 32 is explicit — "A profitable
backtest can still be wrong" — so a strategy must survive out-of-sample
testing, walk-forward windows, parameter sensitivity and Monte Carlo
resampling before any capital is committed, and then must still pass through
paper, shadow and controlled-live stages.

This module encodes those gates as checks that return a verdict plus the
reasons, so "is this deployable" has a mechanical answer rather than a
judgement call made under the influence of an attractive equity curve.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Sequence

from ..core.types import TradeRecord
from ..observability.metrics import PerformanceMetrics, compute_metrics, max_drawdown


class Stage(str, Enum):
    """Section 23.1 deployment pipeline stages, in order."""

    RESEARCH = "research"
    BACKTEST = "backtest"
    OUT_OF_SAMPLE = "out_of_sample"
    WALK_FORWARD = "walk_forward"
    PAPER_TRADING = "paper_trading"
    SHADOW_LIVE = "shadow_live"
    CONTROLLED_LIVE = "controlled_live"
    SCALED_LIVE = "scaled_live"
    FULL_DEPLOYMENT = "full_deployment"


STAGE_ORDER: tuple[Stage, ...] = (
    Stage.RESEARCH,
    Stage.BACKTEST,
    Stage.OUT_OF_SAMPLE,
    Stage.WALK_FORWARD,
    Stage.PAPER_TRADING,
    Stage.SHADOW_LIVE,
    Stage.CONTROLLED_LIVE,
    Stage.SCALED_LIVE,
    Stage.FULL_DEPLOYMENT,
)


@dataclass(frozen=True, slots=True)
class AcceptanceCriteria:
    """Section 20.2 thresholds."""

    min_profit_factor_is: float = 1.5
    min_profit_factor_oos: float = 1.3
    min_oos_sharpe_ratio: float = 0.80      # OOS Sharpe as a fraction of in-sample
    max_drawdown_multiple: float = 1.5      # vs the Monte Carlo expectation

    # -- hardened walk-forward and Monte Carlo gates ------------------------
    #: Every rolling test window must clear this profit factor on its own.
    #: A mean across windows hides a single window carrying four losers.
    min_walk_forward_window_pf: float = 1.3
    #: Folds required before a walk-forward verdict means anything.
    required_walk_forward_folds: int = 5
    #: Bootstrap simulations, and the fraction of them that must be profitable.
    monte_carlo_simulations: int = 5_000
    min_monte_carlo_profitable: float = 0.90
    #: Absolute portfolio drawdown ceiling for deployment.
    max_drawdown: float = 0.05
    min_win_rate: float = 0.40
    min_win_loss_ratio: float = 2.5         # alternative to the win-rate floor
    min_trades_is: int = 100
    min_trades_oos: int = 50
    min_profitable_regimes: int = 3
    max_sharpe_degradation: float = 0.30    # under a +/-20% parameter shift
    min_monte_carlo_positive: float = 0.95


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        return f"[{'PASS' if self.passed else 'FAIL'}] {self.name}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ValidationReport:
    stage: Stage
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if not c.passed)

    def summary(self) -> str:
        if self.passed:
            return f"{self.stage.value}: all {len(self.checks)} gates passed"
        return f"{self.stage.value}: {len(self.failures)} gate(s) failed - " + "; ".join(
            c.name for c in self.failures
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage.value,
            "passed": self.passed,
            "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MonteCarloResult:
    """Distribution of outcomes when the trade *sequence* is randomised."""

    simulations: int
    positive_fraction: float
    median_return: float
    p5_return: float
    p95_return: float
    median_drawdown: float
    p95_drawdown: float
    worst_drawdown: float

    def as_dict(self) -> dict[str, object]:
        return {
            "simulations": self.simulations,
            "positive_fraction": round(self.positive_fraction, 4),
            "median_return": round(self.median_return, 4),
            "p5_return": round(self.p5_return, 4),
            "p95_return": round(self.p95_return, 4),
            "median_drawdown": round(self.median_drawdown, 4),
            "p95_drawdown": round(self.p95_drawdown, 4),
            "worst_drawdown": round(self.worst_drawdown, 4),
        }


def monte_carlo(
    trades: Sequence[TradeRecord],
    *,
    starting_equity: float = 100_000.0,
    simulations: int = 1000,
    seed: int = 42,
    method: str = "bootstrap",
) -> MonteCarloResult | None:
    """Resample the trade history to separate edge from luck.

    Two methods, and the difference matters:

    ``bootstrap`` (default)
        Draw ``n`` trades *with replacement* from the realised set. Both the
        composition and the order vary, so the result is a genuine
        distribution of outcomes the strategy could have produced.

    ``shuffle``
        Reorder the same trades. This tests sequence risk — how bad the
        drawdown could have been if the losers had clustered — but note that
        the final equity is *identical* in every permutation, because addition
        commutes. A "% of simulations profitable" statistic computed from
        shuffling is therefore always 0% or 100% and carries no information.
        Use it for drawdown analysis only.

    Section 20.2 requires 95% of simulations to show positive expectancy, which
    is only a meaningful test under bootstrap resampling.
    """
    trades = list(trades)
    if len(trades) < 10:
        return None
    if method not in {"bootstrap", "shuffle"}:
        raise ValueError(f"unknown Monte Carlo method {method!r}; use 'bootstrap' or 'shuffle'")

    rng = random.Random(seed)
    pnls = [t.pnl for t in trades]
    n = len(pnls)

    returns: list[float] = []
    drawdowns: list[float] = []
    for _ in range(simulations):
        if method == "bootstrap":
            sample = [pnls[rng.randrange(n)] for _ in range(n)]
        else:
            sample = pnls[:]
            rng.shuffle(sample)
        equity = starting_equity
        curve = [equity]
        for pnl in sample:
            equity += pnl
            curve.append(equity)
        returns.append((curve[-1] - starting_equity) / starting_equity)
        drawdowns.append(max_drawdown(curve))

    returns.sort()
    drawdowns.sort()

    def percentile(values: Sequence[float], q: float) -> float:
        if not values:
            return 0.0
        index = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
        return values[index]

    return MonteCarloResult(
        simulations=simulations,
        positive_fraction=sum(1 for r in returns if r > 0) / len(returns),
        median_return=percentile(returns, 0.50),
        p5_return=percentile(returns, 0.05),
        p95_return=percentile(returns, 0.95),
        median_drawdown=percentile(drawdowns, 0.50),
        p95_drawdown=percentile(drawdowns, 0.95),
        worst_drawdown=drawdowns[-1],
    )


# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    index: int
    train_metrics: PerformanceMetrics
    test_metrics: PerformanceMetrics

    @property
    def sharpe_retention(self) -> float:
        """Test Sharpe as a fraction of train Sharpe. Negative train Sharpe -> 0."""
        if self.train_metrics.sharpe <= 0:
            return 0.0
        return self.test_metrics.sharpe / self.train_metrics.sharpe


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    windows: tuple[WalkForwardWindow, ...]

    @property
    def mean_retention(self) -> float:
        if not self.windows:
            return 0.0
        return statistics.fmean(w.sharpe_retention for w in self.windows)

    @property
    def profitable_windows(self) -> int:
        return sum(1 for w in self.windows if w.test_metrics.total_pnl > 0)

    @property
    def consistency(self) -> float:
        if not self.windows:
            return 0.0
        return self.profitable_windows / len(self.windows)

    def as_dict(self) -> dict[str, object]:
        return {
            "windows": len(self.windows),
            "profitable_windows": self.profitable_windows,
            "consistency": round(self.consistency, 4),
            "mean_sharpe_retention": round(self.mean_retention, 4),
        }


@dataclass(frozen=True, slots=True)
class Fold:
    """One rolling train/test fold, as bar indices into the full history.

    ``train`` is never scored. In a rule-based system there are no parameters
    fitted on it, so it serves only as the history the indicators need before
    the first decision in ``test`` can be made — which is exactly the role the
    warm-up plays in the in-sample/out-of-sample split.
    """

    index: int
    train_start: int
    train_end: int
    test_end: int

    @property
    def test_start(self) -> int:
        return self.train_end

    @property
    def train_bars(self) -> int:
        return self.train_end - self.train_start

    @property
    def test_bars(self) -> int:
        return self.test_end - self.train_end


def plan_walk_forward_folds(
    total_bars: int,
    *,
    train_bars: int,
    test_bars: int,
    folds: int,
    min_train_bars: int = 0,
) -> tuple[Fold, ...]:
    """Lay out ``folds`` rolling train/test folds, newest-fitting-first.

    Test windows never overlap, so no bar is scored twice and the folds
    together cover a contiguous, strictly out-of-sample stretch of history.

    The plan is anchored to the *end* of the history: if there is not room for
    every requested fold, the earliest ones are dropped rather than the latest,
    because recent regimes are the ones a deployment decision is about.

    ``min_train_bars`` raises the training window when the indicator set needs
    more history than the requested train period contains — an MA200 on weekly
    bars needs 200 weeks whatever "three years" says. Returns an empty tuple
    when not even one fold fits.
    """
    if train_bars <= 0 or test_bars <= 0 or folds <= 0:
        return ()
    train_bars = max(train_bars, min_train_bars)

    # Anchor on the end and walk backwards in whole test windows.
    plan: list[Fold] = []
    test_end = total_bars
    for _ in range(folds):
        train_end = test_end - test_bars
        train_start = train_end - train_bars
        if train_start < 0:
            break
        plan.append(Fold(0, train_start, train_end, test_end))
        test_end -= test_bars

    plan.reverse()
    return tuple(
        Fold(i, f.train_start, f.train_end, f.test_end) for i, f in enumerate(plan)
    )


def walk_forward(
    run: Callable[[int, int], tuple[Sequence[TradeRecord], Sequence[float]]],
    *,
    total_bars: int,
    train_bars: int,
    test_bars: int,
    step: int | None = None,
) -> WalkForwardResult:
    """Roll train/test windows across the history (section 20.4).

    Args:
        run: called as ``run(start, end)``, returning ``(trades, equity_curve)``
            for that bar range.
        total_bars: length of the full history.
        train_bars / test_bars: window sizes.
        step: advance per window; defaults to ``test_bars`` (non-overlapping
            test periods, so no bar is scored twice).
    """
    step = step or test_bars
    windows: list[WalkForwardWindow] = []
    index = 0
    start = 0

    while start + train_bars + test_bars <= total_bars:
        train_trades, train_curve = run(start, start + train_bars)
        test_trades, test_curve = run(start + train_bars, start + train_bars + test_bars)
        windows.append(
            WalkForwardWindow(
                index=index,
                train_metrics=compute_metrics(train_trades, train_curve),
                test_metrics=compute_metrics(test_trades, test_curve),
            )
        )
        index += 1
        start += step

    return WalkForwardResult(tuple(windows))


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


class StrategyValidator:
    """Applies the section 20.2 acceptance criteria and section 23.2 gates."""

    def __init__(self, criteria: AcceptanceCriteria | None = None) -> None:
        self.criteria = criteria if criteria is not None else AcceptanceCriteria()

    def validate_backtest(
        self,
        metrics: PerformanceMetrics,
        *,
        regime_results: Mapping[str, float] | None = None,
    ) -> ValidationReport:
        """In-sample gate (section 23.2 Backtest -> Out-of-Sample)."""
        c = self.criteria
        checks: list[Check] = [
            Check(
                "profit_factor",
                metrics.profit_factor >= c.min_profit_factor_is,
                f"{metrics.profit_factor:.2f} (need >= {c.min_profit_factor_is})",
            ),
            Check(
                "trade_count",
                metrics.trade_count >= c.min_trades_is,
                f"{metrics.trade_count} trades (need >= {c.min_trades_is} for significance)",
            ),
            Check("sharpe", metrics.sharpe >= 1.0, f"{metrics.sharpe:.2f} (need >= 1.0)"),
        ]

        # Either a decent win rate or a large enough payoff ratio to carry a
        # low one. Requiring both would reject valid trend-following profiles.
        win_loss = (metrics.avg_win / metrics.avg_loss) if metrics.avg_loss > 0 else float("inf")
        checks.append(
            Check(
                "win_rate_or_payoff",
                metrics.win_rate >= c.min_win_rate or win_loss >= c.min_win_loss_ratio,
                f"win rate {metrics.win_rate:.1%}, win/loss {win_loss:.2f} "
                f"(need win rate >= {c.min_win_rate:.0%} or ratio >= {c.min_win_loss_ratio})",
            )
        )

        regime_results = regime_results or metrics.regime_performance
        profitable = sum(1 for pnl in regime_results.values() if pnl > 0)
        checks.append(
            Check(
                "regime_stability",
                profitable >= c.min_profitable_regimes,
                f"profitable in {profitable} regime(s) (need >= {c.min_profitable_regimes})",
            )
        )
        return ValidationReport(Stage.BACKTEST, tuple(checks))

    def validate_out_of_sample(
        self,
        in_sample: PerformanceMetrics,
        out_of_sample: PerformanceMetrics,
    ) -> ValidationReport:
        """Out-of-sample gate: does the edge survive on unseen data?"""
        c = self.criteria
        retention = (out_of_sample.sharpe / in_sample.sharpe) if in_sample.sharpe > 0 else 0.0
        checks = (
            Check(
                "oos_profit_factor",
                out_of_sample.profit_factor >= c.min_profit_factor_oos,
                f"{out_of_sample.profit_factor:.2f} (need >= {c.min_profit_factor_oos})",
            ),
            Check(
                "oos_sharpe_retention",
                retention >= c.min_oos_sharpe_ratio,
                f"OOS Sharpe is {retention:.0%} of in-sample (need >= {c.min_oos_sharpe_ratio:.0%})",
            ),
            Check(
                "oos_trade_count",
                out_of_sample.trade_count >= c.min_trades_oos,
                f"{out_of_sample.trade_count} trades (need >= {c.min_trades_oos})",
            ),
            Check(
                "oos_profitable",
                out_of_sample.total_pnl > 0,
                f"out-of-sample P&L {out_of_sample.total_pnl:+,.2f}",
            ),
        )
        return ValidationReport(Stage.OUT_OF_SAMPLE, checks)

    def validate_walk_forward(self, result: WalkForwardResult) -> ValidationReport:
        checks = (
            Check(
                "windows_tested",
                len(result.windows) >= 3,
                f"{len(result.windows)} windows (need >= 3)",
            ),
            Check(
                "consistency",
                result.consistency >= 0.60,
                f"{result.consistency:.0%} of test windows profitable (need >= 60%)",
            ),
            Check(
                "sharpe_retention",
                result.mean_retention >= self.criteria.min_oos_sharpe_ratio,
                f"mean retention {result.mean_retention:.0%} "
                f"(need >= {self.criteria.min_oos_sharpe_ratio:.0%})",
            ),
        )
        return ValidationReport(Stage.WALK_FORWARD, checks)

    def validate_monte_carlo(
        self,
        result: MonteCarloResult | None,
        realised_drawdown: float,
    ) -> ValidationReport:
        if result is None:
            return ValidationReport(
                Stage.WALK_FORWARD,
                (Check("monte_carlo", False, "fewer than 10 trades; cannot simulate"),),
            )
        c = self.criteria
        limit = result.median_drawdown * c.max_drawdown_multiple
        checks = (
            Check(
                "positive_expectancy",
                result.positive_fraction >= c.min_monte_carlo_positive,
                f"{result.positive_fraction:.1%} of {result.simulations} reorderings profitable "
                f"(need >= {c.min_monte_carlo_positive:.0%})",
            ),
            Check(
                "drawdown_within_expectation",
                realised_drawdown <= limit or limit == 0,
                f"realised drawdown {realised_drawdown:.2%} vs "
                f"{c.max_drawdown_multiple}x median simulated {result.median_drawdown:.2%}",
            ),
            Check(
                "tail_return_acceptable",
                result.p5_return > -0.25,
                f"5th-percentile return {result.p5_return:.2%} (need > -25%)",
            ),
        )
        return ValidationReport(Stage.WALK_FORWARD, checks)

    def validate_sensitivity(
        self,
        baseline: PerformanceMetrics,
        variants: Mapping[str, PerformanceMetrics],
    ) -> ValidationReport:
        """Parameter robustness: a +/-20% shift must not collapse performance.

        A strategy that only works at one exact parameter value was fitted to
        the sample, not discovered in it.
        """
        checks: list[Check] = []
        if baseline.sharpe <= 0:
            return ValidationReport(
                Stage.WALK_FORWARD,
                (Check("sensitivity", False, "baseline Sharpe is not positive"),),
            )

        for name, variant in variants.items():
            degradation = 1.0 - (variant.sharpe / baseline.sharpe)
            checks.append(
                Check(
                    f"sensitivity[{name}]",
                    degradation <= self.criteria.max_sharpe_degradation,
                    f"Sharpe degraded {degradation:.0%} "
                    f"(limit {self.criteria.max_sharpe_degradation:.0%})",
                )
            )
        if not checks:
            checks.append(Check("sensitivity", False, "no parameter variants tested"))
        return ValidationReport(Stage.WALK_FORWARD, tuple(checks))


@dataclass(slots=True)
class DeploymentGate:
    """Tracks a strategy's position in the section 23.1 pipeline.

    Promotion is one stage at a time and requires the current stage's report to
    pass. There is no path from backtest to live capital that skips paper and
    shadow trading.
    """

    strategy_id: str
    stage: Stage = Stage.RESEARCH
    history: list[tuple[Stage, bool, str]] = field(default_factory=list)

    @property
    def is_live_eligible(self) -> bool:
        return STAGE_ORDER.index(self.stage) >= STAGE_ORDER.index(Stage.CONTROLLED_LIVE)

    def next_stage(self) -> Stage | None:
        index = STAGE_ORDER.index(self.stage)
        return STAGE_ORDER[index + 1] if index + 1 < len(STAGE_ORDER) else None

    def promote(self, report: ValidationReport) -> bool:
        """Advance one stage if the report passes. Returns whether it advanced."""
        self.history.append((self.stage, report.passed, report.summary()))
        if not report.passed:
            return False
        target = self.next_stage()
        if target is None:
            return False
        self.stage = target
        return True

    def demote(self, reason: str) -> None:
        """Send a strategy back to paper trading after live degradation."""
        self.history.append((self.stage, False, reason))
        self.stage = Stage.PAPER_TRADING

    def status(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "stage": self.stage.value,
            "live_eligible": self.is_live_eligible,
            "history": [{"stage": s.value, "passed": p, "detail": d} for s, p, d in self.history],
        }
