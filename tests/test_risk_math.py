"""Tests for the two V4 corrections: Expected Shortfall and stop distance.

These are the highest-stakes calculations in the system. ES understates tail
risk if computed as a percentile; a stop tightened past market structure
destroys the edge it was meant to protect. Both are pinned here against the
specification's own worked examples.
"""

from __future__ import annotations

import random
import unittest

from tradermaximus.core.types import AssetClass, Side
from tradermaximus.risk.limits import HardLimits
from tradermaximus.risk.portfolio import (
    expected_shortfall,
    pearson_correlation,
    tail_risk,
    value_at_risk,
)
from tradermaximus.risk.stops import StopLossEngine, StopRejection, atr_multiplier_for
from tradermaximus.core.errors import ConfigurationError


class TestExpectedShortfall(unittest.TestCase):
    """ES_95 = mean(loss | loss >= VaR_95), not a percentile."""

    def setUp(self) -> None:
        # 95 small gains and 5 explicit losses, so the tail is hand-checkable.
        self.returns = [0.001] * 95 + [-0.02, -0.03, -0.04, -0.05, -0.10]

    def test_var_is_the_tail_threshold(self) -> None:
        var = value_at_risk(self.returns, 0.95, min_observations=10)
        self.assertAlmostEqual(var, 0.02, places=12)

    def test_es_is_the_mean_of_the_tail(self) -> None:
        es = expected_shortfall(self.returns, 0.95, min_observations=10)
        # mean(0.02, 0.03, 0.04, 0.05, 0.10) = 0.048
        self.assertAlmostEqual(es, 0.048, places=12)

    def test_es_exceeds_var_on_a_fat_tail(self) -> None:
        var, es = tail_risk(self.returns, 0.95, min_observations=10)
        self.assertGreater(es, var)
        # The gap is exactly what a percentile-based ES would have discarded.
        self.assertAlmostEqual(es - var, 0.028, places=12)

    def test_es_never_below_var_across_random_windows(self) -> None:
        """ES >= VaR is a mathematical invariant, not an empirical tendency."""
        for seed in range(300):
            rng = random.Random(seed)
            returns = [rng.gauss(0.0003, 0.02) for _ in range(250)]
            var, es = tail_risk(returns)
            self.assertIsNotNone(var)
            self.assertIsNotNone(es)
            self.assertGreaterEqual(es, var, f"ES < VaR at seed {seed}")

    def test_insufficient_history_returns_none(self) -> None:
        """Too few observations must yield None, never a number."""
        self.assertEqual(tail_risk([0.01, -0.01]), (None, None))
        self.assertIsNone(value_at_risk([0.01] * 5))

    def test_all_gains_produce_a_negative_loss_figure(self) -> None:
        """A window with no losing days reports a negative (profitable) tail."""
        var, es = tail_risk([0.01] * 60, min_observations=10)
        self.assertLess(var, 0)
        self.assertLessEqual(es, 0)
        self.assertGreaterEqual(es, var)

    def test_window_limits_the_lookback(self) -> None:
        """Only the trailing window is considered (section 13.2: 250 days)."""
        old_disaster = [-0.5] * 10
        recent_calm = [0.001] * 250
        var, _ = tail_risk(old_disaster + recent_calm, window=250)
        self.assertLess(var, 0.01, "the old crash should have rolled out of the window")

    def test_ties_at_the_threshold_stay_in_the_tail(self) -> None:
        """Repeated values at the VaR boundary must not collapse the tail.

        This is the regression: a value-based filter over a distribution with
        many identical entries once swallowed the whole sample.
        """
        returns = [-0.001] * 95 + [-0.05] * 5
        var, es = tail_risk(returns, 0.95, min_observations=10)
        self.assertAlmostEqual(var, 0.05, places=12)
        self.assertAlmostEqual(es, 0.05, places=12)

    def test_es_limit_must_exceed_var_limit(self) -> None:
        """A config where the ES limit sits below VaR could never bind."""
        with self.assertRaises(ConfigurationError):
            HardLimits(max_var_95=0.06, max_expected_shortfall_95=0.05)


class TestStopDistance(unittest.TestCase):
    """Stop = min(max(atr, structure, min_pct), max_pct), reject if structure > max."""

    def setUp(self) -> None:
        self.engine = StopLossEngine()

    def compute(self, **kwargs):
        defaults = dict(
            entry_price=100.0,
            side=Side.LONG,
            asset_class=AssetClass.EQUITIES,
            atr_multiplier=1.5,
        )
        defaults.update(kwargs)
        return self.engine.compute(**defaults)

    def test_spec_example_rejects_when_structure_exceeds_tolerance(self) -> None:
        """Spec worked example: entry 100, ATR 2.5x1.5, structure at 92 (8%)."""
        decision = self.compute(atr=2.5, structure_level=92.0)
        self.assertFalse(decision.accepted)
        self.assertIs(decision.rejection, StopRejection.STRUCTURE_TOO_WIDE)
        self.assertIsNone(decision.stop_price)

    def test_spec_example_accepts_a_six_percent_structural_stop(self) -> None:
        """Spec worked example: structure at 94 -> raw 6, final 6."""
        decision = self.compute(atr=2.5, structure_level=94.0)
        self.assertTrue(decision.accepted)
        self.assertAlmostEqual(decision.raw_distance, 6.0)
        self.assertAlmostEqual(decision.stop_distance, 6.0)
        self.assertAlmostEqual(decision.stop_price, 94.0)
        self.assertEqual(decision.model.value, "structure")

    def test_minimum_percentage_acts_as_a_floor(self) -> None:
        """ATR of 3.75 is under the 4% floor, so the floor binds."""
        decision = self.compute(atr=2.5)
        self.assertTrue(decision.accepted)
        self.assertAlmostEqual(decision.stop_distance, 4.0)
        self.assertEqual(decision.model.value, "percentage")

    def test_wide_atr_is_clamped_not_rejected(self) -> None:
        """ATR is an estimate, not a level; capping it forfeits nothing."""
        decision = self.compute(atr=10.0)
        self.assertTrue(decision.accepted)
        self.assertAlmostEqual(decision.stop_distance, 7.0)
        self.assertTrue(decision.clamped)

    def test_structure_exactly_at_the_limit_is_accepted(self) -> None:
        """The spec rejects only when structure *exceeds* tolerance."""
        decision = self.compute(atr=1.0, structure_level=93.0)
        self.assertTrue(decision.accepted)
        self.assertAlmostEqual(decision.stop_distance, 7.0)
        self.assertFalse(decision.clamped)

    def test_stop_is_never_tightened_inside_structure(self) -> None:
        """The core invariant, swept across the whole structural range."""
        for structure in [x / 10.0 for x in range(880, 1000)]:
            decision = self.compute(atr=2.0, structure_level=structure)
            if not decision.accepted:
                continue
            self.assertLessEqual(
                decision.stop_price,
                structure + 1e-9,
                f"stop {decision.stop_price} placed inside structure at {structure}",
            )

    def test_short_side_mirrors_the_long_logic(self) -> None:
        ok = self.compute(side=Side.SHORT, atr=2.5, structure_level=106.0)
        self.assertTrue(ok.accepted)
        self.assertAlmostEqual(ok.stop_price, 106.0)

        too_wide = self.compute(side=Side.SHORT, atr=2.5, structure_level=109.0)
        self.assertFalse(too_wide.accepted)
        self.assertIs(too_wide.rejection, StopRejection.STRUCTURE_TOO_WIDE)

    def test_wrong_side_level_is_ignored(self) -> None:
        """Resistance above a long says nothing about where its stop belongs."""
        decision = self.compute(atr=2.5, structure_level=105.0)
        self.assertTrue(decision.accepted)
        self.assertIsNone(decision.structure_distance)

    def test_asset_class_tolerances_differ(self) -> None:
        """A 6% structural stop passes for equities (7%) and fails crypto (5%)."""
        equities = self.compute(atr=2.5, structure_level=94.0)
        self.assertTrue(equities.accepted)

        crypto = self.engine.compute(
            entry_price=50_000.0,
            side=Side.LONG,
            asset_class=AssetClass.CRYPTO,
            atr=1_500.0,
            structure_level=47_000.0,
        )
        self.assertFalse(crypto.accepted)

    def test_invalid_entry_is_rejected(self) -> None:
        decision = self.compute(entry_price=-5.0, atr=1.0)
        self.assertFalse(decision.accepted)
        self.assertIs(decision.rejection, StopRejection.INVALID_ENTRY)

    def test_atr_multiplier_scales_with_volatility(self) -> None:
        self.assertEqual(atr_multiplier_for(0.10), 1.2)
        self.assertEqual(atr_multiplier_for(0.50), 1.5)
        self.assertEqual(atr_multiplier_for(0.90), 1.8)
        # Unknown volatility uses the moderate multiplier, not an extreme.
        self.assertEqual(atr_multiplier_for(None), 1.5)


class TestCorrelation(unittest.TestCase):
    def test_perfect_and_inverse_correlation(self) -> None:
        series = [0.01, -0.02, 0.03, -0.01, 0.02, 0.005]
        self.assertAlmostEqual(pearson_correlation(series, series), 1.0, places=12)
        self.assertAlmostEqual(
            pearson_correlation(series, [-x for x in series]), -1.0, places=12
        )

    def test_constant_series_has_no_correlation(self) -> None:
        self.assertIsNone(pearson_correlation([1.0] * 10, [0.01, 0.02] * 5))

    def test_too_short_returns_none(self) -> None:
        self.assertIsNone(pearson_correlation([0.01], [0.02]))


if __name__ == "__main__":
    unittest.main()
