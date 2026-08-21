"""TraderMaximus — a risk-first algorithmic trading system.

Implements the TraderMaximus master specification: a deterministic pipeline in
which strategies propose and the risk engine disposes.

    market data -> quality -> features -> regime -> strategy -> scoring
                -> risk authorisation -> sizing -> execution -> reconciliation
                -> journal

The system defaults to paper mode everywhere, and the two V4 corrections are
implemented and tested:

* Expected Shortfall is ``mean(loss | loss >= VaR_95)`` — see
  :mod:`tradermaximus.risk.portfolio`.
* Stop distance is ``min(max(atr, structure, min_pct), max_pct)``, and a trade
  whose structural stop exceeds risk tolerance is *rejected* rather than having
  its stop tightened — see :mod:`tradermaximus.risk.stops`.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "SystemConfig",
    "TraderMaximus",
    "Journal",
    "PositionBook",
    "RiskEngine",
    "StrategyEngine",
    "Backtester",
]


def __getattr__(name: str):
    """Lazily expose the main entry points without importing everything eagerly."""
    if name == "SystemConfig":
        from .core.config import SystemConfig

        return SystemConfig
    if name == "TraderMaximus":
        from .orchestrator import TraderMaximus

        return TraderMaximus
    if name == "Journal":
        from .observability.journal import Journal

        return Journal
    if name == "PositionBook":
        from .portfolio.book import PositionBook

        return PositionBook
    if name == "RiskEngine":
        from .risk.engine import RiskEngine

        return RiskEngine
    if name == "StrategyEngine":
        from .strategy.engine import StrategyEngine

        return StrategyEngine
    if name == "Backtester":
        from .research.backtest import Backtester

        return Backtester
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
