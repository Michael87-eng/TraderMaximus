"""Portfolio risk engine (spec sections 13, 13.2) — **corrected ES definition**.

Expected Shortfall, correctly
-----------------------------

::

    VaR_95 = the 95th percentile of the loss distribution
    ES_95  = mean(loss | loss >= VaR_95)

The common bug is to compute a percentile and label it "Expected Shortfall".
That understates tail risk by construction: VaR is the *threshold* the worst 5%
of days begin at, while ES is the *average severity* of those days. ES >= VaR
always, and the gap between them is exactly the information VaR discards — how
bad it gets once the threshold is breached. A portfolio can sit comfortably
inside a VaR limit while its tail losses are catastrophic.

Both are computed by historical simulation over a 250-day window (section
13.2), and both are reported as positive fractions of equity: ``ES_95 = 0.04``
means "the average loss on the worst 5% of days is 4% of equity".

Quantile convention
-------------------

The tail is selected *positionally*: with ``n`` returns sorted worst-first, the
tail is the worst ``floor((1 - confidence) * n)`` observations (at least one).
``VaR`` is the smallest loss in that tail — the threshold the tail begins at —
and ``ES`` is the mean of every loss at or beyond it.

Selecting positionally rather than by value comparison matters. An earlier
value-based filter (``[l for l in losses if l >= losses[k]]``) silently
collapsed to the whole distribution whenever the quantile index landed on a
repeated value, reporting a near-zero ES for a portfolio with a genuinely fat
tail. Positional selection cannot degenerate that way.

Sign convention: both are returned as loss fractions, so positive numbers mean
losses. A *negative* VaR/ES is meaningful and is reported as-is — it says even
the worst days in the window were profitable — rather than being clamped to
zero, which would break the ``ES >= VaR`` invariant.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from itertools import combinations
from typing import Mapping, Sequence

from ..portfolio.book import PortfolioSnapshot
from .limits import HardLimits

#: Section 13.2 historical-simulation window.
DEFAULT_WINDOW = 250

#: Minimum observations before a tail estimate means anything. Below this the
#: engine reports ``None`` rather than a number computed from three data points.
MIN_OBSERVATIONS = 30


# ---------------------------------------------------------------------------
# Tail risk
# ---------------------------------------------------------------------------


def _tail_losses(
    returns: Sequence[float],
    confidence: float,
    window: int,
) -> list[float] | None:
    """The worst ``(1 - confidence)`` share of losses, ascending.

    Returns ``None`` when the window holds too few observations to say
    anything about the tail.
    """
    recent = list(returns[-window:]) if window > 0 else list(returns)
    n = len(recent)
    if n == 0:
        return None
    losses = sorted((-r for r in recent), reverse=True)  # worst loss first
    tail_count = max(1, math.floor((1.0 - confidence) * n))
    return sorted(losses[:tail_count])


def tail_risk(
    returns: Sequence[float],
    confidence: float = 0.95,
    *,
    window: int = DEFAULT_WINDOW,
    min_observations: int = MIN_OBSERVATIONS,
) -> tuple[float | None, float | None]:
    """``(VaR, ES)`` from a single pass, guaranteed mutually consistent.

    ``VaR`` is the threshold the worst ``1 - confidence`` of days begins at.
    ``ES`` is ``mean(loss | loss >= VaR)`` — the average severity once that
    threshold is breached. ``ES >= VaR`` always holds.
    """
    recent = list(returns[-window:]) if window > 0 else list(returns)
    if len(recent) < min_observations:
        return None, None

    tail = _tail_losses(recent, confidence, window)
    if not tail:
        return None, None

    var = tail[0]  # smallest loss in the tail = the VaR threshold
    es = statistics.fmean(tail)
    return var, es


def value_at_risk(
    returns: Sequence[float],
    confidence: float = 0.95,
    *,
    window: int = DEFAULT_WINDOW,
    min_observations: int = MIN_OBSERVATIONS,
) -> float | None:
    """Historical VaR as a loss fraction (positive means a loss).

    The loss level that the worst ``1 - confidence`` of periods begins at.
    """
    return tail_risk(returns, confidence, window=window, min_observations=min_observations)[0]


def expected_shortfall(
    returns: Sequence[float],
    confidence: float = 0.95,
    *,
    window: int = DEFAULT_WINDOW,
    min_observations: int = MIN_OBSERVATIONS,
) -> float | None:
    """Expected Shortfall: ``mean(loss | loss >= VaR)``.

    The conditional tail expectation — the *average* loss on days that breach
    VaR, not the threshold itself. This is the corrected V4 definition; using
    VaR in its place understates tail risk by exactly the amount VaR discards.
    """
    return tail_risk(returns, confidence, window=window, min_observations=min_observations)[1]


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


def pearson_correlation(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Pearson correlation over the overlapping prefix of two return series."""
    length = min(len(a), len(b))
    if length < 3:
        return None
    x, y = list(a[-length:]), list(b[-length:])
    mean_x, mean_y = statistics.fmean(x), statistics.fmean(y)
    cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    var_x = sum((xi - mean_x) ** 2 for xi in x)
    var_y = sum((yi - mean_y) ** 2 for yi in y)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / math.sqrt(var_x * var_y)


#: Phase 3 correlation sizing. Above this correlation with something already
#: held, a new position is not diversification — it is more of the same trade.
CORRELATION_THRESHOLD = 0.70
#: How much of the position to give up when that is the case.
CORRELATION_REDUCTION = 0.30
#: Rolling window, in bars, the correlation is measured over.
CORRELATION_WINDOW = 20


def correlation_size_multiplier(
    candidate: Sequence[float],
    held: Mapping[str, Sequence[float]],
    *,
    threshold: float = CORRELATION_THRESHOLD,
    reduction: float = CORRELATION_REDUCTION,
    window: int = CORRELATION_WINDOW,
) -> tuple[float, float | None]:
    """Shrink a position that duplicates risk already on the book.

    Returns ``(multiplier, worst_correlation)``. Two positions correlated at
    0.9 are not two bets, they are one bet at double size, and the per-trade
    risk cap does not see that because it looks at each trade alone.

    Measured over the most recent ``window`` bars, so it tracks the correlation
    the portfolio has *now* rather than a long-run average that may predate the
    regime. Returns a multiplier of 1.0 and ``None`` when there is nothing held
    to compare against, or too little overlapping history to measure — an
    unmeasurable correlation is not evidence of independence, but neither is it
    grounds for a reduction nobody computed.
    """
    if not held or len(candidate) < 3:
        return 1.0, None

    recent = list(candidate[-window:])
    worst: float | None = None
    for series in held.values():
        value = pearson_correlation(recent, list(series[-window:]))
        if value is None:
            continue
        worst = value if worst is None else max(worst, value)

    if worst is None or worst <= threshold:
        return 1.0, worst
    return max(0.0, 1.0 - reduction), worst


def correlation_matrix(returns_by_symbol: Mapping[str, Sequence[float]]) -> dict[tuple[str, str], float]:
    """Pairwise correlations, keyed by sorted symbol pair."""
    matrix: dict[tuple[str, str], float] = {}
    for left, right in combinations(sorted(returns_by_symbol), 2):
        value = pearson_correlation(returns_by_symbol[left], returns_by_symbol[right])
        if value is not None:
            matrix[(left, right)] = value
    return matrix


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PortfolioRiskMetrics:
    """The section 13.1 metric set, computed from one snapshot."""

    equity: float
    position_count: int
    total_exposure: float
    net_exposure: float
    gross_exposure: float
    total_risk: float
    sector_exposure: Mapping[str, float] = field(default_factory=dict)
    asset_class_exposure: Mapping[str, float] = field(default_factory=dict)
    top3_concentration: float = 0.0
    top5_concentration: float = 0.0
    largest_position: float = 0.0
    effective_positions: float = 0.0
    diversification_ratio: float = 1.0
    avg_pairwise_correlation: float | None = None
    max_pairwise_correlation: float | None = None
    correlated_exposure: float = 0.0
    correlated_clusters: tuple[tuple[str, ...], ...] = ()
    var_95: float | None = None
    expected_shortfall_95: float | None = None
    realized_volatility: float | None = None
    drawdown_current: float = 0.0
    drawdown_maximum: float = 0.0
    leverage: float = 0.0

    def as_dict(self) -> dict[str, object]:
        def r(value: float | None, places: int = 4) -> float | None:
            return None if value is None else round(value, places)

        return {
            "equity": round(self.equity, 2),
            "positions": self.position_count,
            "total_exposure": r(self.total_exposure),
            "net_exposure": r(self.net_exposure),
            "gross_exposure": r(self.gross_exposure),
            "total_risk": r(self.total_risk),
            "sector_exposure": {k: round(v, 4) for k, v in self.sector_exposure.items()},
            "asset_class_exposure": {k: round(v, 4) for k, v in self.asset_class_exposure.items()},
            "top3_concentration": r(self.top3_concentration),
            "largest_position": r(self.largest_position),
            "effective_positions": r(self.effective_positions, 2),
            "diversification_ratio": r(self.diversification_ratio),
            "avg_pairwise_correlation": r(self.avg_pairwise_correlation),
            "correlated_exposure": r(self.correlated_exposure),
            "var_95": r(self.var_95),
            "expected_shortfall_95": r(self.expected_shortfall_95),
            "drawdown_current": r(self.drawdown_current),
            "leverage": r(self.leverage),
        }


@dataclass(frozen=True, slots=True)
class LimitBreach:
    rule: str
    metric: str
    value: float
    limit: float
    message: str


class PortfolioRiskEngine:
    """Computes portfolio metrics and checks them against section 13.3 rules."""

    def __init__(self, limits: HardLimits | None = None, *, window: int = DEFAULT_WINDOW) -> None:
        self.limits = limits or HardLimits()
        self.window = window

    def compute(
        self,
        snapshot: PortfolioSnapshot,
        *,
        portfolio_returns: Sequence[float] = (),
        returns_by_symbol: Mapping[str, Sequence[float]] | None = None,
        drawdown_current: float = 0.0,
        drawdown_maximum: float = 0.0,
    ) -> PortfolioRiskMetrics:
        weights = snapshot.position_weights()

        # Top-N concentration is measured against *equity*, not against total
        # exposure. Against exposure the metric is degenerate — with three
        # positions the top three are always 100% of exposure — and it would
        # be inconsistent with ``max_single_position``, which is an equity
        # fraction. Against equity, "top 3 < 40%" is a real constraint.
        notionals = sorted(
            (snapshot.notional(p) for p in snapshot.positions), reverse=True
        )
        equity = snapshot.equity if snapshot.equity > 0 else 0.0
        top3 = sum(notionals[:3]) / equity if equity > 0 else 0.0
        top5 = sum(notionals[:5]) / equity if equity > 0 else 0.0

        # Herfindahl-Hirschman on exposure weights. ``1 / HHI`` is the
        # "effective number of positions"; dividing by the actual count
        # normalises it to (0, 1] so the section 13.3 threshold of 0.30 is
        # meaningful — 1.0 means perfectly equal weights.
        hhi = sum(w * w for w in weights.values())
        effective_positions = (1.0 / hhi) if hhi > 0 else 0.0
        diversification_ratio = (effective_positions / len(weights)) if weights else 1.0

        matrix = correlation_matrix(returns_by_symbol or {})
        correlations = list(matrix.values())
        avg_corr = statistics.fmean(correlations) if correlations else None
        max_corr = max(correlations) if correlations else None

        clusters = self._correlated_clusters(matrix)
        correlated_exposure = self._correlated_exposure(clusters, snapshot)

        var, es = tail_risk(portfolio_returns, 0.95, window=self.window)

        realized_vol = None
        if len(portfolio_returns) >= 20:
            realized_vol = statistics.pstdev(list(portfolio_returns)[-20:]) * math.sqrt(252)

        return PortfolioRiskMetrics(
            equity=snapshot.equity,
            position_count=snapshot.position_count,
            total_exposure=snapshot.total_exposure,
            net_exposure=snapshot.net_exposure,
            gross_exposure=snapshot.gross_exposure,
            total_risk=snapshot.total_risk,
            sector_exposure=snapshot.exposure_by("sector"),
            asset_class_exposure=snapshot.exposure_by("asset_class"),
            top3_concentration=top3,
            top5_concentration=top5,
            largest_position=(notionals[0] / equity if notionals and equity > 0 else 0.0),
            effective_positions=effective_positions,
            diversification_ratio=diversification_ratio,
            avg_pairwise_correlation=avg_corr,
            max_pairwise_correlation=max_corr,
            correlated_exposure=correlated_exposure,
            correlated_clusters=clusters,
            var_95=var,
            expected_shortfall_95=es,
            realized_volatility=realized_vol,
            drawdown_current=drawdown_current,
            drawdown_maximum=drawdown_maximum,
            leverage=max(0.0, snapshot.gross_exposure - 1.0),
        )

    # -- limit checks -------------------------------------------------------

    def check(self, metrics: PortfolioRiskMetrics) -> list[LimitBreach]:
        """Evaluate every section 13.3 portfolio rule.

        Returns the breaches; an empty list means the portfolio is compliant.
        """
        limits = self.limits
        breaches: list[LimitBreach] = []

        def check_max(rule: str, metric: str, value: float | None, limit: float) -> None:
            if value is not None and value > limit:
                breaches.append(
                    LimitBreach(rule, metric, value, limit, f"{metric} {value:.4f} exceeds limit {limit:.4f}")
                )

        check_max("PORTFOLIO_EXPOSURE", "total_exposure", metrics.total_exposure, limits.max_portfolio_exposure)
        check_max("NET_EXPOSURE", "net_exposure", abs(metrics.net_exposure), limits.max_net_exposure)
        check_max("GROSS_EXPOSURE", "gross_exposure", metrics.gross_exposure, limits.max_gross_exposure)
        check_max("PORTFOLIO_RISK", "total_risk", metrics.total_risk, limits.max_total_portfolio_risk)
        check_max("TOP_3_POSITIONS", "top3_concentration", metrics.top3_concentration, limits.max_top3_concentration)
        check_max("TOP_5_POSITIONS", "top5_concentration", metrics.top5_concentration, limits.max_top5_concentration)
        check_max("SINGLE_POSITION", "largest_position", metrics.largest_position, limits.max_single_position)
        check_max("CORRELATED_EXPOSURE", "correlated_exposure", metrics.correlated_exposure, limits.max_correlated_exposure)
        check_max("LEVERAGE", "leverage", metrics.leverage, max(0.0, limits.max_leverage - 1.0))
        check_max("PORTFOLIO_DRAWDOWN", "drawdown_current", metrics.drawdown_current, limits.max_portfolio_drawdown)
        check_max("VAR_95", "var_95", metrics.var_95, limits.max_var_95)
        check_max("ES_95", "expected_shortfall_95", metrics.expected_shortfall_95, limits.max_expected_shortfall_95)

        if metrics.avg_pairwise_correlation is not None:
            check_max(
                "PAIRWISE_CORRELATION_AVG",
                "avg_pairwise_correlation",
                metrics.avg_pairwise_correlation,
                limits.max_avg_pairwise_correlation,
            )

        for sector, exposure in metrics.sector_exposure.items():
            check_max(f"SECTOR_EXPOSURE:{sector}", f"sector_exposure[{sector}]", exposure, limits.max_sector_exposure)
        for asset_class, exposure in metrics.asset_class_exposure.items():
            check_max(
                f"ASSET_CLASS_EXPOSURE:{asset_class}",
                f"asset_class_exposure[{asset_class}]",
                exposure,
                limits.max_asset_class_exposure,
            )

        # Diversification is a floor, and only meaningful with enough positions
        # for concentration to be a real choice rather than an artefact.
        if metrics.position_count >= 3 and metrics.diversification_ratio < limits.min_diversification_ratio:
            breaches.append(
                LimitBreach(
                    "DIVERSIFICATION_RATIO",
                    "diversification_ratio",
                    metrics.diversification_ratio,
                    limits.min_diversification_ratio,
                    f"diversification ratio {metrics.diversification_ratio:.4f} below minimum "
                    f"{limits.min_diversification_ratio:.4f}",
                )
            )
        return breaches

    def exposure_reduction_required(self, metrics: PortfolioRiskMetrics) -> float:
        """Fraction of exposure to shed to bring ES_95 back inside its limit.

        Tail risk scales roughly linearly with exposure, so the required cut is
        the proportional overshoot. Section 13.2 requires restoring compliance
        rather than merely reporting the breach.
        """
        es = metrics.expected_shortfall_95
        limit = self.limits.max_expected_shortfall_95
        if es is None or es <= limit or es <= 0:
            return 0.0
        return min(1.0, 1.0 - (limit / es))

    # -- helpers ------------------------------------------------------------

    def _correlated_clusters(self, matrix: Mapping[tuple[str, str], float]) -> tuple[tuple[str, ...], ...]:
        """Group symbols linked by correlation above the threshold.

        Uses union-find over the correlation graph, so a chain A~B~C counts as
        one cluster even when A and C are not directly correlated: the hidden
        sector bet the spec warns about is exactly this transitive kind.
        """
        threshold = self.limits.correlation_threshold
        parent: dict[str, str] = {}

        def find(symbol: str) -> str:
            parent.setdefault(symbol, symbol)
            while parent[symbol] != symbol:
                parent[symbol] = parent[parent[symbol]]
                symbol = parent[symbol]
            return symbol

        def union(left: str, right: str) -> None:
            root_l, root_r = find(left), find(right)
            if root_l != root_r:
                parent[root_r] = root_l

        for (left, right), value in matrix.items():
            if abs(value) > threshold:
                union(left, right)

        groups: dict[str, list[str]] = {}
        for symbol in parent:
            groups.setdefault(find(symbol), []).append(symbol)
        return tuple(tuple(sorted(members)) for members in groups.values() if len(members) > 1)

    @staticmethod
    def _correlated_exposure(
        clusters: Sequence[Sequence[str]],
        snapshot: PortfolioSnapshot,
    ) -> float:
        """Largest cluster's combined exposure as a fraction of equity."""
        if not clusters or snapshot.equity <= 0:
            return 0.0
        by_symbol = {p.symbol: snapshot.notional(p) for p in snapshot.positions}
        return max(
            sum(by_symbol.get(symbol, 0.0) for symbol in cluster) / snapshot.equity
            for cluster in clusters
        )
