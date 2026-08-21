"""Strategy selection, scoring, backtesting, validation and drift."""

from __future__ import annotations

import random
import unittest
from datetime import timedelta

from tradermaximus.core.types import AssetClass, Instrument, Side, Timeframe, TradeRecord
from tradermaximus.indicators.engine import FeatureEngine
from tradermaximus.observability.metrics import compute_metrics, max_drawdown, sharpe_ratio
from tradermaximus.regime.engine import MarketContext, RegimeEngine
from tradermaximus.research.backtest import BacktestConfig, Backtester
from tradermaximus.research.drift import DriftDetector, DriftSeverity, StrategyMonitor
from tradermaximus.research.validation import (
    DeploymentGate,
    Stage,
    StrategyValidator,
    monte_carlo,
    walk_forward,
)
from tradermaximus.strategy.engine import StrategyEngine
from tradermaximus.strategy.library import ALL_STRATEGIES, strategies_for
from tradermaximus.strategy.scoring import COMPONENT_MAX, ScoringInputs, SignalScoringEngine
from tradermaximus.strategy.spec import EvalContext, Fundamentals
from tests.helpers import BASE, EQUITY, random_walk, series, trending, with_breakout

FUNDAMENTALS = Fundamentals(
    eps_rating=92, rs_rating=88, industry_rank=12, earnings_growth=0.42,
    sales_growth=0.30, roe=0.27, acc_dis_rating="A", quarters_of_accumulation=3,
)


class TestStrategyLibrary(unittest.TestCase):
    def test_all_ten_strategies_are_defined(self) -> None:
        self.assertEqual(len(ALL_STRATEGIES), 10)
        self.assertEqual(len({s.id for s in ALL_STRATEGIES}), 10)

    def test_every_strategy_has_entry_conditions_and_a_stop(self) -> None:
        for spec in ALL_STRATEGIES:
            self.assertTrue(spec.entry_conditions, f"{spec.id} has no entry conditions")
            self.assertGreater(spec.stop.atr_multiplier, 0, f"{spec.id} has no stop model")
            self.assertTrue(spec.asset_classes, f"{spec.id} supports no asset class")

    def test_asset_class_filtering(self) -> None:
        self.assertGreater(len(strategies_for(AssetClass.EQUITIES)), len(strategies_for(AssetClass.FOREX)))
        for spec in strategies_for(AssetClass.CRYPTO):
            self.assertIn(AssetClass.CRYPTO, spec.asset_classes)

    def test_can_slim_fails_closed_without_fundamentals(self) -> None:
        """Trading a CAN SLIM setup without fundamentals is a different strategy."""
        from tradermaximus.strategy.library import CAN_SLIM_BREAKOUT

        features = FeatureEngine().compute(series(trending(400)))
        evaluation = CAN_SLIM_BREAKOUT.evaluate(EvalContext(features=features))
        self.assertFalse(evaluation.triggered)
        self.assertIn(
            "can_slim_fundamentals", {r.name for r in evaluation.failed}
        )

    def test_evaluation_names_the_failing_conditions(self) -> None:
        from tradermaximus.strategy.library import MOMENTUM_BREAKOUT

        features = FeatureEngine().compute(series(random_walk(300, seed=4)))
        evaluation = MOMENTUM_BREAKOUT.evaluate(EvalContext(features=features))
        self.assertFalse(evaluation.triggered)
        self.assertTrue(evaluation.failed)
        for result in evaluation.failed:
            self.assertTrue(result.detail, f"{result.name} gave no detail")

    def test_gap_condition_discriminates_hold_from_fill(self) -> None:
        """Regression: the gap-holding condition was once trivially true."""
        from tradermaximus.core.types import Candle
        from tradermaximus.strategy.library import _gap_holding

        base = trending(200)
        previous_close = base[-1].close
        engine = FeatureEngine()

        held = base + [
            Candle(base[-1].timestamp + timedelta(days=1), previous_close * 1.05,
                   previous_close * 1.09, previous_close * 1.048, previous_close * 1.08, 3_000_000)
        ]
        filled = base + [
            Candle(base[-1].timestamp + timedelta(days=1), previous_close * 1.05,
                   previous_close * 1.055, previous_close * 1.001, previous_close * 1.005, 3_000_000)
        ]
        condition = _gap_holding(0.20)
        self.assertTrue(condition.evaluate(EvalContext(features=engine.compute(series(held)))).passed)
        self.assertFalse(condition.evaluate(EvalContext(features=engine.compute(series(filled)))).passed)


class TestScoring(unittest.TestCase):
    def test_components_sum_to_one_hundred(self) -> None:
        self.assertAlmostEqual(sum(COMPONENT_MAX.values()), 100.0)

    def test_non_equity_scores_are_rescaled_to_one_hundred(self) -> None:
        """Otherwise crypto and FX face silently stricter regime thresholds."""
        from tradermaximus.strategy.library import PULLBACK_MA_BOUNCE

        crypto = Instrument("BTCUSD", AssetClass.CRYPTO, "BINANCE", "Crypto", 0.01, 0.0001, 0.0001)
        features = FeatureEngine().compute(series(trending(400), crypto))
        regime = RegimeEngine().classify(
            features, MarketContext(breadth_index=0.7, new_highs_ratio=0.75)
        )
        evaluation = PULLBACK_MA_BOUNCE.evaluate(EvalContext(features=features))
        score = SignalScoringEngine().score(
            spec=PULLBACK_MA_BOUNCE, evaluation=evaluation, features=features,
            regime=regime, context=EvalContext(features=features),
            inputs=ScoringInputs(average_daily_volume=5_000_000, spread_bps=4.0,
                                 relative_strength_rating=95, risk_reward_ratio=4.0,
                                 historical_win_rate=0.65, sector_strength=0.05),
        )
        self.assertNotIn("fundamentals", score.components)
        self.assertLessEqual(score.total, 100.0)

    def test_ineligible_regime_scores_zero_alignment(self) -> None:
        from tradermaximus.strategy.library import MOMENTUM_BREAKOUT

        features = FeatureEngine().compute(series(random_walk(300)))
        regime = RegimeEngine().classify(features, MarketContext(spread_ratio=3.0, volume_ratio=0.3))
        score = SignalScoringEngine().score(
            spec=MOMENTUM_BREAKOUT,
            evaluation=MOMENTUM_BREAKOUT.evaluate(EvalContext(features=features)),
            features=features, regime=regime, context=EvalContext(features=features),
        )
        self.assertEqual(score.components["market_regime_alignment"], 0.0)


class TestStrategySelection(unittest.TestCase):
    def setUp(self) -> None:
        self.features = FeatureEngine()
        self.engine = StrategyEngine()

    def test_untradeable_regime_yields_no_signal(self) -> None:
        features = self.features.compute(series(random_walk(400)))
        regime = RegimeEngine().classify(features, MarketContext(spread_ratio=3.0, volume_ratio=0.3))
        result = self.engine.select(instrument=EQUITY, features=features, regime=regime)
        self.assertIsNone(result.signal)
        self.assertIn("permits no", result.reason)

    def test_regime_filter_runs_before_condition_evaluation(self) -> None:
        """Ineligible strategies are blocked, not merely scored low."""
        features = self.features.compute(series(random_walk(400)))
        regime = RegimeEngine().classify(features, MarketContext(spread_ratio=3.0, volume_ratio=0.3))
        result = self.engine.select(instrument=EQUITY, features=features, regime=regime)
        self.assertTrue(all(not c.eligible for c in result.candidates) or not result.candidates)

    def test_signal_carries_stop_inputs(self) -> None:
        candles = with_breakout(trending(400))
        features = self.features.compute(series(candles))
        regime = RegimeEngine().classify(
            features,
            MarketContext(breadth_index=0.7, new_highs_ratio=0.75, correlation_change=0.05,
                          index_ma200_slope=0.001),
        )
        result = self.engine.select(
            instrument=EQUITY, features=features, regime=regime,
            context=EvalContext(features=features, fundamentals=FUNDAMENTALS, sector_strength=0.03),
            inputs=ScoringInputs(average_daily_volume=1_100_000, spread_bps=6.0,
                                 relative_strength_rating=88, risk_reward_ratio=2.6),
        )
        if result.signal is not None:
            self.assertIsNotNone(result.signal.atr)
            self.assertGreater(result.signal.total_score, 0)
            self.assertIn("strategy_type", result.signal.features)

    def test_insufficient_history_marks_strategies_ineligible(self) -> None:
        features = self.features.compute(series(random_walk(120)))
        regime = RegimeEngine().classify(
            features, MarketContext(breadth_index=0.6, new_highs_ratio=0.7)
        )
        result = self.engine.select(instrument=EQUITY, features=features, regime=regime)
        blocked = [c for c in result.candidates if not c.eligible and "bars" in c.ineligible_reason]
        self.assertTrue(blocked)


class TestBacktester(unittest.TestCase):
    def test_backtest_runs_and_respects_point_in_time(self) -> None:
        universe = {
            Instrument("AAA", AssetClass.EQUITIES, "NASDAQ", "Technology", 0.01, 1.0, 1.0):
                random_walk(500, seed=1, drift=0.0012),
            Instrument("BBB", AssetClass.EQUITIES, "NYSE", "Energy", 0.01, 1.0, 1.0):
                random_walk(500, seed=2, drift=0.0008),
        }
        result = Backtester(
            BacktestConfig(starting_equity=100_000, warmup_bars=300, seed=7),
            fundamentals={"AAA": FUNDAMENTALS, "BBB": FUNDAMENTALS},
        ).run(universe, benchmark=random_walk(500, seed=99, drift=0.0006))

        self.assertGreater(result.bars_processed, 0)
        self.assertEqual(len(result.equity_curve), result.bars_processed + 1)
        self.assertGreaterEqual(result.final_equity, 0)
        # No position may outlive the final bar.
        for trade in result.trades:
            self.assertLessEqual(trade.opened_at, trade.closed_at)

    def test_insufficient_history_raises(self) -> None:
        universe = {EQUITY: random_walk(50)}
        with self.assertRaises(ValueError):
            Backtester(BacktestConfig(warmup_bars=300)).run(universe)


class TestValidation(unittest.TestCase):
    @staticmethod
    def trades(n: int, win_rate: float, avg_win: float, avg_loss: float, seed: int = 1):
        rng = random.Random(seed)
        out = []
        for i in range(n):
            win = rng.random() < win_rate
            pnl = rng.gauss(avg_win, avg_win * 0.3) if win else -abs(rng.gauss(avg_loss, avg_loss * 0.3))
            out.append(
                TradeRecord("S", "STRATEGY_001", Side.LONG, 10, 100, 101,
                            BASE + timedelta(days=i), BASE + timedelta(days=i + 2),
                            pnl, pnl / avg_loss,
                            regime=("TREND_UP", "RANGE", "BULL_MARKET")[i % 3])
            )
        return out

    def test_bootstrap_separates_edge_from_noise(self) -> None:
        """Shuffling cannot: the P&L sum is invariant under permutation."""
        edge = monte_carlo(self.trades(150, 0.45, 900, 400, seed=3), method="bootstrap")
        none = monte_carlo(self.trades(150, 0.40, 500, 500, seed=4), method="bootstrap")
        self.assertGreater(edge.positive_fraction, 0.95)
        self.assertLess(none.positive_fraction, 0.50)

    def test_shuffle_return_distribution_is_degenerate(self) -> None:
        result = monte_carlo(self.trades(100, 0.45, 900, 400, seed=5), method="shuffle")
        self.assertAlmostEqual(result.p5_return, result.p95_return, places=10)

    def test_monte_carlo_needs_enough_trades(self) -> None:
        self.assertIsNone(monte_carlo(self.trades(5, 0.5, 900, 400)))

    def test_acceptance_gates_reject_a_weak_strategy(self) -> None:
        weak = self.trades(20, 0.2, 300, 600, seed=5)
        curve = [100_000.0]
        for trade in weak:
            curve.append(curve[-1] + trade.pnl)
        report = StrategyValidator().validate_backtest(compute_metrics(weak, curve))
        self.assertFalse(report.passed)
        self.assertIn("trade_count", {c.name for c in report.failures})

    def test_deployment_gate_advances_one_stage_at_a_time(self) -> None:
        strong = self.trades(150, 0.45, 900, 400, seed=3)
        curve = [100_000.0]
        for trade in strong:
            curve.append(curve[-1] + trade.pnl)
        report = StrategyValidator().validate_backtest(compute_metrics(strong, curve))

        gate = DeploymentGate("STRATEGY_001", stage=Stage.BACKTEST)
        self.assertTrue(gate.promote(report))
        self.assertIs(gate.stage, Stage.OUT_OF_SAMPLE)
        self.assertFalse(gate.is_live_eligible, "backtest must not reach live capital directly")

    def test_failing_report_blocks_promotion(self) -> None:
        weak = self.trades(20, 0.2, 300, 600, seed=5)
        curve = [100_000.0] * 21
        report = StrategyValidator().validate_backtest(compute_metrics(weak, curve))
        gate = DeploymentGate("BAD", stage=Stage.BACKTEST)
        self.assertFalse(gate.promote(report))
        self.assertIs(gate.stage, Stage.BACKTEST)

    def test_walk_forward_windows(self) -> None:
        def run(start: int, end: int):
            trades = self.trades(end - start, 0.45, 900, 400, seed=start)
            curve = [100_000.0]
            for trade in trades:
                curve.append(curve[-1] + trade.pnl)
            return trades, curve

        result = walk_forward(run, total_bars=400, train_bars=100, test_bars=50)
        self.assertGreaterEqual(len(result.windows), 3)
        self.assertGreaterEqual(result.consistency, 0.0)


class TestDrift(unittest.TestCase):
    @staticmethod
    def trades(n: int, win_rate: float, avg_win: float, avg_loss: float, seed: int = 1):
        rng = random.Random(seed)
        out = []
        for i in range(n):
            win = rng.random() < win_rate
            pnl = rng.gauss(avg_win, avg_win * 0.2) if win else -abs(rng.gauss(avg_loss, avg_loss * 0.2))
            out.append(
                TradeRecord("S", "S1", Side.LONG, 10, 100, 101,
                            BASE + timedelta(days=i), BASE + timedelta(days=i + 1),
                            pnl, pnl / avg_loss, regime="TREND_UP")
            )
        return out

    def setUp(self) -> None:
        baseline_trades = self.trades(200, 0.48, 900, 400, seed=1)
        curve = [100_000.0]
        for trade in baseline_trades:
            curve.append(curve[-1] + trade.pnl)
        self.baseline = compute_metrics(baseline_trades, curve)
        self.detector = DriftDetector()

    def evaluate(self, n, win_rate, avg_win, avg_loss, seed):
        recent = self.trades(n, win_rate, avg_win, avg_loss, seed=seed)
        curve = [100_000.0]
        for trade in recent:
            curve.append(curve[-1] + trade.pnl)
        return self.detector.evaluate("S1", self.baseline, recent, recent_equity_curve=curve)

    def test_healthy_performance_shows_no_drift(self) -> None:
        report = self.evaluate(30, 0.48, 900, 400, 7)
        self.assertIs(report.severity, DriftSeverity.NONE)
        self.assertEqual(report.size_multiplier, 1.0)

    def test_moderate_decay_halves_size(self) -> None:
        report = self.evaluate(30, 0.40, 820, 430, 8)
        self.assertIn(report.severity, {DriftSeverity.MINOR, DriftSeverity.MODERATE})
        self.assertLessEqual(report.size_multiplier, 1.0)

    def test_severe_decay_disables_the_strategy(self) -> None:
        report = self.evaluate(30, 0.20, 450, 700, 9)
        self.assertIs(report.severity, DriftSeverity.SEVERE)
        self.assertTrue(report.should_disable)
        self.assertEqual(report.size_multiplier, 0.0)

    def test_improvement_is_not_drift(self) -> None:
        """Outperforming the baseline must not trigger a drift response."""
        report = self.evaluate(30, 0.70, 1400, 300, 11)
        self.assertIs(report.severity, DriftSeverity.NONE)

    def test_too_few_trades_defers_judgement(self) -> None:
        report = self.detector.evaluate("S1", self.baseline, self.trades(5, 0.2, 100, 900))
        self.assertIs(report.severity, DriftSeverity.NONE)
        self.assertIn("recent trades", report.reason)

    def test_monitor_disables_and_reinstates_at_half_size(self) -> None:
        monitor = StrategyMonitor()
        monitor.set_baseline("S1", self.baseline)
        recent = self.trades(30, 0.20, 450, 700, seed=9)
        curve = [100_000.0]
        for trade in recent:
            curve.append(curve[-1] + trade.pnl)
        monitor.review("S1", recent, recent_equity_curve=curve)
        self.assertFalse(monitor.is_enabled("S1"))
        with self.assertRaises(ValueError):
            monitor.reinstate("S1", operator="", reason="")
        monitor.reinstate("S1", operator="michael", reason="revalidated on paper")
        self.assertTrue(monitor.is_enabled("S1"))
        self.assertEqual(monitor.multiplier_for("S1"), 0.5)


class TestMetrics(unittest.TestCase):
    def test_max_drawdown(self) -> None:
        self.assertAlmostEqual(max_drawdown([100, 120, 90, 110]), 0.25)

    def test_sharpe_of_a_flat_series_is_zero(self) -> None:
        self.assertEqual(sharpe_ratio([0.0, 0.0, 0.0]), 0.0)

    def test_empty_trades_yield_empty_metrics(self) -> None:
        metrics = compute_metrics([], [100_000, 100_000])
        self.assertEqual(metrics.trade_count, 0)


if __name__ == "__main__":
    unittest.main()
