"""Performance and system-health metrics (spec sections 22.1, 28.1).

Pure functions over a list of :class:`~tradermaximus.core.types.TradeRecord`
plus an equity curve. Kept free of I/O so the backtester and the live system
compute identical numbers from identical inputs.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from ..core.types import TradeRecord

TRADING_DAYS = 252


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    """Strategy/portfolio performance summary."""

    trade_count: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    expectancy: float = 0.0
    profit_factor: float = 0.0
    total_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    max_drawdown: float = 0.0
    return_pct: float = 0.0
    risk_adjusted_return: float = 0.0
    avg_r_multiple: float = 0.0
    avg_slippage_bps: float = 0.0
    regime_performance: Mapping[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "trade_count": self.trade_count,
            "win_rate": round(self.win_rate, 4),
            "avg_win": round(self.avg_win, 4),
            "avg_loss": round(self.avg_loss, 4),
            "expectancy": round(self.expectancy, 4),
            "profit_factor": round(self.profit_factor, 4),
            "total_pnl": round(self.total_pnl, 4),
            "sharpe": round(self.sharpe, 4),
            "sortino": round(self.sortino, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "return_pct": round(self.return_pct, 4),
            "risk_adjusted_return": round(self.risk_adjusted_return, 4),
            "avg_r_multiple": round(self.avg_r_multiple, 4),
            "regime_performance": {k: round(v, 4) for k, v in self.regime_performance.items()},
        }


def max_drawdown(equity_curve: Sequence[float]) -> float:
    """Largest peak-to-trough decline as a positive fraction."""
    peak = -math.inf
    worst = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def returns_from_equity(equity_curve: Sequence[float]) -> list[float]:
    """Simple period-over-period returns; zero-or-negative equity ends the series."""
    out: list[float] = []
    for prev, curr in zip(equity_curve, equity_curve[1:]):
        if prev <= 0:
            break
        out.append((curr - prev) / prev)
    return out


def sharpe_ratio(returns: Sequence[float], periods_per_year: int = TRADING_DAYS) -> float:
    if len(returns) < 2:
        return 0.0
    stdev = statistics.pstdev(returns)
    if stdev == 0:
        return 0.0
    return statistics.fmean(returns) / stdev * math.sqrt(periods_per_year)


def sortino_ratio(returns: Sequence[float], periods_per_year: int = TRADING_DAYS) -> float:
    """Sharpe's downside-only sibling: only negative deviations are penalised."""
    if len(returns) < 2:
        return 0.0
    downside = [r for r in returns if r < 0]
    if not downside:
        return 0.0
    downside_dev = math.sqrt(statistics.fmean([r * r for r in downside]))
    if downside_dev == 0:
        return 0.0
    return statistics.fmean(returns) / downside_dev * math.sqrt(periods_per_year)


def compute_metrics(
    trades: Iterable[TradeRecord],
    equity_curve: Sequence[float] = (),
    *,
    periods_per_year: int = TRADING_DAYS,
) -> PerformanceMetrics:
    trades = list(trades)
    if not trades:
        return PerformanceMetrics(max_drawdown=max_drawdown(equity_curve))

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))

    avg_win = statistics.fmean([t.pnl for t in wins]) if wins else 0.0
    avg_loss = statistics.fmean([abs(t.pnl) for t in losses]) if losses else 0.0
    win_rate = len(wins) / len(trades)
    expectancy = statistics.fmean([t.pnl for t in trades])

    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = math.inf if gross_profit > 0 else 0.0

    returns = returns_from_equity(equity_curve)
    dd = max_drawdown(equity_curve)
    total_pnl = sum(t.pnl for t in trades)

    return_pct = 0.0
    if len(equity_curve) >= 2 and equity_curve[0] > 0:
        return_pct = (equity_curve[-1] - equity_curve[0]) / equity_curve[0]

    by_regime: dict[str, list[float]] = {}
    for trade in trades:
        by_regime.setdefault(trade.regime, []).append(trade.pnl)

    return PerformanceMetrics(
        trade_count=len(trades),
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        expectancy=expectancy,
        profit_factor=profit_factor,
        total_pnl=total_pnl,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        sharpe=sharpe_ratio(returns, periods_per_year),
        sortino=sortino_ratio(returns, periods_per_year),
        max_drawdown=dd,
        return_pct=return_pct,
        risk_adjusted_return=(return_pct / dd) if dd > 0 else 0.0,
        avg_r_multiple=statistics.fmean([t.r_multiple for t in trades]),
        regime_performance={k: sum(v) for k, v in by_regime.items()},
    )


@dataclass(slots=True)
class SystemHealth:
    """Live component-health view backing the section 22.1 dashboard."""

    components: dict[str, bool] = field(default_factory=dict)
    latencies_ms: dict[str, float] = field(default_factory=dict)

    def set(self, component: str, healthy: bool, latency_ms: float | None = None) -> None:
        self.components[component] = healthy
        if latency_ms is not None:
            self.latencies_ms[component] = latency_ms

    @property
    def all_healthy(self) -> bool:
        return all(self.components.values()) if self.components else False

    def unhealthy(self) -> list[str]:
        return sorted(name for name, ok in self.components.items() if not ok)

    def as_dict(self) -> dict[str, object]:
        return {
            "all_healthy": self.all_healthy,
            "components": dict(self.components),
            "unhealthy": self.unhealthy(),
            "latencies_ms": {k: round(v, 2) for k, v in self.latencies_ms.items()},
        }
