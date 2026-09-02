"""Daily monitoring dashboard (spec section 28, Phase 7).

One screen that answers the questions an operator actually has each morning:
what is open, how far down are we, what did today cost or make, and which
strategies are still trusted.

The dashboard reads state — it never changes it. Nothing here can open,
close, resize or re-enable anything: a monitoring surface that can also act is
a second, untested trading path, and the whole point of section 8's precedence
rules is that there is exactly one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Mapping, Sequence

from ..core.types import Side, TradeRecord
from ..observability.metrics import PerformanceMetrics, compute_metrics
from ..portfolio.book import PositionBook
from ..research.drift import StrategyMonitor
from ..risk.limits import HardLimits


def _bar(fraction: float, width: int = 20) -> str:
    """A proportion of a limit, drawn. Over-limit renders full and is flagged."""
    filled = max(0, min(width, round(fraction * width)))
    return "#" * filled + "." * (width - filled)


@dataclass(frozen=True, slots=True)
class PositionLine:
    symbol: str
    strategy_id: str
    side: Side
    quantity: float
    entry_price: float
    current_price: float
    stop_price: float | None

    @property
    def unrealised(self) -> float:
        return (self.current_price - self.entry_price) * self.quantity * self.side.sign

    @property
    def unrealised_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (self.current_price - self.entry_price) / self.entry_price * self.side.sign

    @property
    def distance_to_stop(self) -> float | None:
        """How far price is from the stop, as a fraction of price.

        Negative means the stop is already through — which should be
        impossible, and is exactly why it is shown rather than assumed away.
        """
        if self.stop_price is None or self.current_price <= 0:
            return None
        return (self.current_price - self.stop_price) * self.side.sign / self.current_price


@dataclass
class Dashboard:
    """A point-in-time view of the book, the risk budget and strategy health."""

    generated_at: datetime
    equity: float
    starting_equity: float
    cash: float
    daily_pnl: float
    daily_pnl_pct: float
    drawdown: float
    weekly_drawdown: float
    monthly_drawdown: float
    positions: tuple[PositionLine, ...] = ()
    strategy_metrics: Mapping[str, PerformanceMetrics] = field(default_factory=dict)
    disabled_strategies: tuple[str, ...] = ()
    size_multipliers: Mapping[str, float] = field(default_factory=dict)
    limits: HardLimits = field(default_factory=HardLimits)

    @property
    def gross_exposure(self) -> float:
        if self.equity <= 0:
            return 0.0
        return sum(
            abs(p.current_price * p.quantity) for p in self.positions
        ) / self.equity

    @property
    def net_exposure(self) -> float:
        if self.equity <= 0:
            return 0.0
        return sum(
            p.current_price * p.quantity * p.side.sign for p in self.positions
        ) / self.equity

    def as_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "equity": round(self.equity, 2),
            "daily_pnl": round(self.daily_pnl, 2),
            "daily_pnl_pct": round(self.daily_pnl_pct, 6),
            "drawdown": round(self.drawdown, 6),
            "gross_exposure": round(self.gross_exposure, 6),
            "net_exposure": round(self.net_exposure, 6),
            "open_positions": len(self.positions),
            "disabled_strategies": list(self.disabled_strategies),
        }

    # -- rendering ----------------------------------------------------------

    def render(self) -> str:
        width = 78
        lines = [
            "=" * width,
            f"  TRADERMAXIMUS — {self.generated_at:%Y-%m-%d %H:%M UTC}",
            "=" * width,
            "",
            f"  equity {self.equity:>14,.2f}   cash {self.cash:>14,.2f}   "
            f"since start {(self.equity / self.starting_equity - 1) if self.starting_equity else 0:>+7.2%}",
            f"  today  {self.daily_pnl:>+14,.2f}   ({self.daily_pnl_pct:+.2%})",
            "",
            "  RISK BUDGET",
        ]
        for label, value, limit in (
            ("daily loss", max(0.0, -self.daily_pnl_pct), self.limits.max_daily_loss),
            ("weekly drawdown", self.weekly_drawdown, self.limits.max_weekly_loss),
            ("monthly drawdown", self.monthly_drawdown, self.limits.max_monthly_drawdown),
            ("portfolio drawdown", self.drawdown, self.limits.max_portfolio_drawdown),
            ("gross exposure", self.gross_exposure, self.limits.max_portfolio_exposure),
        ):
            used = value / limit if limit > 0 else 0.0
            flag = "  <-- AT LIMIT" if used >= 1.0 else ""
            lines.append(
                f"    {label:20} {value:7.2%} of {limit:6.2%}  [{_bar(used)}] {used:5.0%}{flag}"
            )

        lines += ["", f"  OPEN POSITIONS ({len(self.positions)})"]
        if not self.positions:
            lines.append("    none")
        else:
            lines.append(
                f"    {'symbol':10}{'strategy':14}{'side':6}{'entry':>10}{'last':>10}"
                f"{'P&L':>10}{'to stop':>9}"
            )
            for p in sorted(self.positions, key=lambda x: x.symbol):
                distance = p.distance_to_stop
                stop_text = "n/a" if distance is None else f"{distance:.1%}"
                lines.append(
                    f"    {p.symbol:10}{p.strategy_id:14}{p.side.value:6}"
                    f"{p.entry_price:10.4g}{p.current_price:10.4g}"
                    f"{p.unrealised:+10.2f}{stop_text:>9}"
                )

        lines += ["", "  STRATEGY HEALTH"]
        # Every strategy the monitor has an opinion about appears, whether or
        # not it closed a trade in this window. A disabled strategy dropping
        # off the table for want of recent trades is exactly the row an
        # operator most needs to see: it stopped trading because it was
        # stopped, and silence would read as health.
        rows = sorted(set(self.strategy_metrics) | set(self.disabled_strategies))
        if not rows:
            lines.append("    no closed trades yet")
        else:
            lines.append(
                f"    {'strategy':14}{'trades':>8}{'win rate':>10}{'PF':>8}"
                f"{'avg R':>8}{'size':>7}  status"
            )
            for strategy_id in rows:
                m = self.strategy_metrics.get(strategy_id)
                multiplier = self.size_multipliers.get(strategy_id, 1.0)
                status = "DISABLED" if strategy_id in self.disabled_strategies else "active"
                if m is None:
                    lines.append(
                        f"    {strategy_id:14}{'-':>8}{'-':>10}{'-':>8}"
                        f"{'-':>8}{multiplier:7.0%}  {status}"
                    )
                    continue
                pf = "inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
                lines.append(
                    f"    {strategy_id:14}{m.trade_count:8}{m.win_rate:10.1%}{pf:>8}"
                    f"{m.avg_r_multiple:8.2f}{multiplier:7.0%}  {status}"
                )

        if self.disabled_strategies:
            lines += [
                "",
                "  " + ", ".join(self.disabled_strategies)
                + " disabled by drift detection. Reinstatement is a human decision "
                "(section 21.2); the monitor never re-enables on its own.",
            ]
        lines.append("")
        return "\n".join(lines)


def build_dashboard(
    book: PositionBook,
    *,
    now: datetime,
    trades: Sequence[TradeRecord] = (),
    monitor: StrategyMonitor | None = None,
    limits: HardLimits | None = None,
    today: date | None = None,
) -> Dashboard:
    """Assemble the dashboard from live state. Reads only."""
    equity = book.equity
    positions = tuple(
        PositionLine(
            symbol=p.symbol,
            strategy_id=p.strategy_id,
            side=p.side,
            quantity=p.quantity,
            entry_price=p.entry_price,
            current_price=book.price_of(p.symbol),
            stop_price=p.stop_price,
        )
        for p in book.positions
    )

    by_strategy: dict[str, list[TradeRecord]] = {}
    for trade in trades:
        by_strategy.setdefault(trade.strategy_id, []).append(trade)

    daily_pct = book.daily_pnl_pct(today or now.date())
    return Dashboard(
        generated_at=now,
        equity=equity,
        starting_equity=book.starting_equity,
        cash=book.cash,
        daily_pnl=daily_pct * equity,
        daily_pnl_pct=daily_pct,
        drawdown=book.drawdown,
        weekly_drawdown=book.weekly_drawdown,
        monthly_drawdown=book.monthly_drawdown,
        positions=positions,
        strategy_metrics={k: compute_metrics(v) for k, v in by_strategy.items()},
        disabled_strategies=tuple(sorted(monitor.disabled)) if monitor else (),
        size_multipliers=dict(monitor.size_multipliers) if monitor else {},
        limits=limits or HardLimits(),
    )
