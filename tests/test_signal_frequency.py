"""Signal-frequency regression guard.

A trading system can fail in two opposite directions, and both are silent:

* **Too few signals.** Conjunctions of individually-reasonable conditions
  multiply into near-impossibility, and a broken condition looks the same as a
  quiet market. Three CAN SLIM strategies once produced exactly zero signals
  because their pivot spanned the current bar — a defect no correctness test
  caught, because every unit behaved as written.
* **Too many signals.** A future "make it trade more" change loosens something
  that was load-bearing, and the system starts taking setups it should refuse.

These tests pin the portfolio-level rate inside a band. They are calibration
guards, not correctness tests: they use the shared synthetic generator, so
they measure *the pipeline's selectivity*, never whether an edge exists.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.market_sim import make

from tradermaximus.core.types import AssetClass, Instrument
from tradermaximus.regime.engine import MarketContext
from tradermaximus.research.backtest import BacktestConfig, Backtester
from tradermaximus.risk.limits import HardLimits, limits_for
from tradermaximus.strategy.spec import Fundamentals

FUNDAMENTALS = Fundamentals(
    eps_rating=92, rs_rating=88, industry_rank=12, earnings_growth=0.42,
    sales_growth=0.30, roe=0.27, acc_dis_rating="A", quarters_of_accumulation=3,
)

UNIVERSE_SPEC = [
    ("AAA", "Technology", "uptrend", 1),
    ("BBB", "Energy", "uptrend", 2),
    ("CCC", "Healthcare", "range", 3),
    ("DDD", "Financials", "uptrend", 4),
    ("EEE", "Industrials", "volatile", 5),
    ("FFF", "Staples", "range", 6),
    ("GGG", "Materials", "uptrend", 7),
    ("HHH", "Utilities", "bear", 8),
]


def _run_backtest():
    universe = {}
    fundamentals = {}
    for symbol, sector, character, seed in UNIVERSE_SPEC:
        instrument = Instrument(symbol, AssetClass.EQUITIES, "NASDAQ", sector, 0.01, 1.0, 1.0)
        universe[instrument] = make(character, 940, seed)
        fundamentals[symbol] = FUNDAMENTALS

    backtester = Backtester(
        BacktestConfig(starting_equity=100_000, warmup_bars=300, seed=7),
        fundamentals=fundamentals,
    )
    return backtester.run(
        universe,
        benchmark=make("benchmark", 940, 999),
        market_context=MarketContext(
            breadth_index=0.55, new_highs_ratio=0.65, correlation_change=0.10
        ),
    )


class TestSignalFrequency(unittest.TestCase):
    """The pipeline produces a workable number of signals, and not more."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = _run_backtest()

    def test_the_system_actually_trades(self) -> None:
        """The floor. Zero signals means a gate is broken, not that markets are quiet."""
        self.assertGreater(
            self.result.signals_generated, 0,
            "no signals at all over 640 bars across 8 symbols: a gate is broken",
        )

    def test_signal_rate_is_in_the_expected_band(self) -> None:
        """Between 6 and 25 signals per 640 bars across the universe.

        Below the floor means something has become unsatisfiable. Above the
        ceiling means a filter that was doing real work has been loosened.
        """
        per_640 = self.result.signals_generated / self.result.bars_processed * 640
        self.assertGreaterEqual(
            per_640, 6.0, f"only {per_640:.1f} signals per 640 bars; a gate has tightened"
        )
        self.assertLessEqual(
            per_640, 25.0, f"{per_640:.1f} signals per 640 bars; a filter has been loosened"
        )

    def test_trades_reach_execution(self) -> None:
        """Signals must survive risk authorisation and actually fill."""
        self.assertGreater(len(self.result.trades), 0)

    def test_more_than_one_strategy_participates(self) -> None:
        """A single strategy carrying everything hides nine broken ones."""
        strategies = {trade.strategy_id for trade in self.result.trades}
        self.assertGreaterEqual(
            len(strategies), 2, f"only {strategies} produced trades"
        )

    def test_r_multiple_reflects_initial_risk(self) -> None:
        """A profit or loss must never report as zero R.

        Regression: R was computed against the *current* stop, so any winner
        whose stop had trailed up to breakeven reported R = 0 regardless of
        what it made. Drift detection and validation both average R, so the
        defect silently flattened both. A breakeven stop-out with no P&L
        legitimately gives R ~ 0 and is excluded.
        """
        for trade in self.result.trades:
            if abs(trade.pnl) < 1e-6:
                continue
            self.assertNotEqual(
                trade.r_multiple, 0.0,
                f"{trade.symbol} made {trade.pnl:+.2f} but reported R = 0",
            )

    def test_losses_respect_the_risk_budget(self) -> None:
        """No single stop-out may exceed roughly 1R.

        Gaps through a stop can exceed 1R, so the bound is generous; the point
        is to catch sizing that ignored the stop distance entirely.
        """
        for trade in self.result.trades:
            if trade.exit_reason == "stop_loss":
                self.assertGreater(
                    trade.r_multiple, -2.5,
                    f"{trade.symbol} lost {trade.r_multiple:.2f}R on a stop",
                )


class TestHardLimitsUntouched(unittest.TestCase):
    """Calibration work must never move a section 8.1 limit.

    This is the guard on the guards: any future change that improves signal
    frequency by loosening a hard risk limit fails here.
    """

    EXPECTED = {
        "max_risk_per_trade": 0.005,
        "max_total_portfolio_risk": 0.02,
        "max_portfolio_exposure": 0.50,
        "max_net_exposure": 0.50,
        "max_daily_loss": 0.015,
        "max_weekly_loss": 0.03,
        "max_monthly_drawdown": 0.06,
        "max_portfolio_drawdown": 0.10,
        "max_leverage": 1.0,
        "max_var_95": 0.03,
        "max_expected_shortfall_95": 0.05,
        "max_sector_exposure": 0.30,
        "max_correlated_exposure": 0.20,
        "max_single_position": 0.15,
        "max_daily_trades": 20,
        "max_spread_bps": 50.0,
        "max_slippage_bps": 30.0,
    }

    def test_portfolio_limits(self) -> None:
        limits = HardLimits()
        for name, expected in self.EXPECTED.items():
            self.assertEqual(
                getattr(limits, name), expected,
                f"hard limit {name} moved from {expected} to {getattr(limits, name)}",
            )

    def test_stop_tolerances(self) -> None:
        equities = limits_for(AssetClass.EQUITIES)
        self.assertEqual(equities.max_stop_pct, 0.07)
        self.assertEqual(equities.min_stop_pct, 0.04)
        self.assertEqual(limits_for(AssetClass.CRYPTO).max_stop_pct, 0.05)
        self.assertEqual(limits_for(AssetClass.FOREX).max_stop_pct, 0.05)

    def test_spec_mandated_strategy_thresholds(self) -> None:
        """Thresholds the specification states numerically stay as written.

        Section 6.2.1 fixes relative volume at 1.4, RSI at 55-80 and the
        52-week-high proximity at 5%. These were candidates for loosening
        during calibration and were deliberately left alone; the pipeline was
        fixed instead.
        """
        from tradermaximus.strategy.library import MOMENTUM_BREAKOUT

        names = {c.name for c in MOMENTUM_BREAKOUT.entry_conditions}
        self.assertIn("relative_volume_above_1.4", names)
        self.assertIn("rsi_55_to_80", names)
        self.assertIn("within_5pct_of_52w_high", names)


if __name__ == "__main__":
    unittest.main()
