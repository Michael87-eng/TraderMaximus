"""Indicator determinism, alignment and known-value tests."""

from __future__ import annotations

import unittest

from tradermaximus.indicators import core
from tradermaximus.indicators.engine import FeatureEngine
from tradermaximus.indicators.patterns import (
    bearish_engulfing,
    bullish_engulfing,
    dark_cloud_cover,
    detect_patterns,
    hammer,
    morning_star,
    shooting_star,
)
from tradermaximus.indicators.structure import (
    fibonacci_levels,
    is_breakout,
    rolling_high,
    support_resistance,
)
from tests.helpers import candle, random_walk, series


class TestKnownValues(unittest.TestCase):
    def test_sma(self) -> None:
        self.assertEqual(core.sma([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])

    def test_ema_seeded_with_sma(self) -> None:
        result = core.ema([1, 2, 3, 4, 5], 3)
        self.assertIsNone(result[1])
        self.assertAlmostEqual(result[2], 2.0)
        self.assertAlmostEqual(result[3], 3.0)

    def test_rsi_bounds(self) -> None:
        rising = [float(i) for i in range(1, 60)]
        falling = list(reversed(rising))
        self.assertAlmostEqual(core.last_value(core.rsi(rising, 14)), 100.0)
        self.assertAlmostEqual(core.last_value(core.rsi(falling, 14)), 0.0)
        # A flat series has no up or down pressure; 50 is the neutral reading.
        self.assertAlmostEqual(core.last_value(core.rsi([50.0] * 60, 14)), 50.0)

    def test_obv_accumulates_directionally(self) -> None:
        self.assertEqual(core.obv([10, 11, 10, 12], [100, 200, 300, 400]), [0.0, 200.0, -100.0, 300.0])

    def test_true_range_uses_previous_close(self) -> None:
        tr = core.true_range([2, 3, 4], [1, 2, 3], [1.5, 2.5, 3.5])
        self.assertAlmostEqual(tr[0], 1.0)
        self.assertAlmostEqual(tr[1], 1.5)

    def test_bollinger_bands_collapse_on_flat_input(self) -> None:
        upper, middle, lower = core.bollinger_bands([50.0] * 60)
        self.assertAlmostEqual(core.last_value(upper), 50.0)
        self.assertAlmostEqual(core.last_value(middle), 50.0)
        self.assertAlmostEqual(core.last_value(lower), 50.0)

    def test_fast_stdev_matches_stdlib(self) -> None:
        import random as _random
        import statistics

        values = [_random.Random(7).gauss(0, 1) for _ in range(200)]
        self.assertAlmostEqual(core._pstdev(values), statistics.pstdev(values), places=10)


class TestAlignmentAndWarmup(unittest.TestCase):
    """Indicators return input-aligned series with None during warm-up."""

    def setUp(self) -> None:
        self.candles = random_walk(300, seed=3)
        self.closes = [c.close for c in self.candles]
        self.highs = [c.high for c in self.candles]
        self.lows = [c.low for c in self.candles]

    def test_all_series_align_to_input_length(self) -> None:
        n = len(self.closes)
        for name, values in {
            "sma": core.sma(self.closes, 20),
            "ema": core.ema(self.closes, 20),
            "rsi": core.rsi(self.closes, 14),
            "atr": core.atr(self.highs, self.lows, self.closes, 14),
            "adx": core.adx(self.highs, self.lows, self.closes, 14),
            "roc": core.roc(self.closes, 10),
            "cci": core.cci(self.highs, self.lows, self.closes, 20),
        }.items():
            self.assertEqual(len(values), n, f"{name} is misaligned")

    def test_warmup_region_is_none(self) -> None:
        result = core.sma(self.closes, 20)
        self.assertTrue(all(v is None for v in result[:19]))
        self.assertIsNotNone(result[19])

    def test_short_series_yields_all_none(self) -> None:
        self.assertEqual(core.sma([1, 2], 20), [None, None])
        self.assertTrue(all(v is None for v in core.rsi([1, 2, 3], 14)))


class TestDeterminism(unittest.TestCase):
    def test_identical_inputs_give_identical_features(self) -> None:
        candles = random_walk(300, seed=11)
        engine = FeatureEngine()
        first = engine.compute(series(candles))
        second = engine.compute(series(list(candles)))
        self.assertEqual(first.as_dict(), second.as_dict())

    def test_bounded_window_matches_full_history(self) -> None:
        """A 400-bar window gives the same readings as the full series.

        This is what makes the backtester's bounded decision window safe.
        """
        candles = random_walk(900, seed=5)
        engine = FeatureEngine()
        full = engine.compute(series(candles))
        bounded = engine.compute(series(candles).tail(400))
        for key in ("rsi_14", "atr_14", "ma200", "bb_upper", "volatility_percentile"):
            self.assertAlmostEqual(
                full.number(key), bounded.number(key), places=6, msg=f"{key} diverged"
            )


class TestStructure(unittest.TestCase):
    def test_rolling_high_excludes_the_current_bar(self) -> None:
        values = [1.0, 5.0, 3.0, 2.0, 9.0]
        self.assertEqual(rolling_high(values, 3, offset=1), 5.0)

    def test_breakout_detection(self) -> None:
        closes = [10.0] * 25 + [20.0]
        highs = [10.5] * 25 + [20.5]
        self.assertTrue(is_breakout(closes, highs, 20))
        self.assertFalse(is_breakout([10.0] * 26, [10.5] * 26, 20))

    def test_fibonacci_levels(self) -> None:
        levels = fibonacci_levels(80.0, 100.0)
        self.assertAlmostEqual(levels["50.0"], 90.0)
        self.assertAlmostEqual(levels["61.8"], 87.64)
        self.assertAlmostEqual(levels["100.0"], 80.0)

    def test_support_resistance_finds_levels(self) -> None:
        candles = random_walk(200, seed=8)
        supports, resistances = support_resistance([c.high for c in candles], [c.low for c in candles])
        self.assertTrue(supports)
        self.assertTrue(resistances)


class TestPatterns(unittest.TestCase):
    def test_hammer(self) -> None:
        match = hammer([candle(0, 100).__class__(candle(0, 100).timestamp, 100, 101, 94, 100.5, 1000)])
        self.assertIsNotNone(match)
        self.assertTrue(match.bullish)

    def test_shooting_star(self) -> None:
        c = candle(0, 100)
        bar = type(c)(c.timestamp, 100, 107, 99.5, 100.5, 1000)
        match = shooting_star([bar])
        self.assertIsNotNone(match)
        self.assertFalse(match.bullish)

    def test_engulfing_patterns(self) -> None:
        c = candle(0, 100)
        make = type(c)
        down = make(c.timestamp, 105, 105.5, 100, 100.5, 1000)
        up = make(c.timestamp, 100, 107, 99.5, 106, 1000)
        self.assertIsNotNone(bullish_engulfing([down, up]))
        self.assertIsNone(bearish_engulfing([down, up]))

    def test_morning_star_requires_a_small_middle_bar(self) -> None:
        c = candle(0, 100)
        make = type(c)
        first = make(c.timestamp, 110, 110.5, 100, 100.5, 1000)
        middle = make(c.timestamp, 100, 101, 99, 100.2, 1000)
        last = make(c.timestamp, 100.5, 108, 100, 107, 1000)
        self.assertIsNotNone(morning_star([first, middle, last]))
        # A large middle bar breaks the pattern.
        big_middle = make(c.timestamp, 100, 115, 99, 114, 1000)
        self.assertIsNone(morning_star([first, big_middle, last]))

    def test_dark_cloud_cover(self) -> None:
        c = candle(0, 100)
        make = type(c)
        up = make(c.timestamp, 100, 110.5, 99, 110, 1000)
        down = make(c.timestamp, 111, 112, 103, 104, 1000)
        self.assertIsNotNone(dark_cloud_cover([up, down]))

    def test_detect_patterns_sorts_by_strength(self) -> None:
        c = candle(0, 100)
        bar = type(c)(c.timestamp, 100, 101, 94, 100.5, 1000)
        matches = detect_patterns([bar])
        self.assertTrue(matches)
        strengths = [m.strength for m in matches]
        self.assertEqual(strengths, sorted(strengths, reverse=True))


if __name__ == "__main__":
    unittest.main()


class TestBreakoutBasis(unittest.TestCase):
    """The breakout comparison basis, recalibrated after measurement."""

    def test_close_basis_is_the_default(self) -> None:
        """close > max(prior closes) is the standard closing-basis breakout."""
        from tradermaximus.indicators.structure import is_breakout

        closes = [10.0] * 25 + [12.0]
        highs = [15.0] * 25 + [12.5]
        # Above the prior closing high, but below the prior intraday highs.
        self.assertTrue(is_breakout(closes, highs, 20))
        self.assertFalse(is_breakout(closes, highs, 20, basis="high"))

    def test_high_basis_still_available(self) -> None:
        from tradermaximus.indicators.structure import is_breakout

        closes = [10.0] * 25 + [20.0]
        highs = [10.5] * 25 + [20.5]
        self.assertTrue(is_breakout(closes, highs, 20, basis="high"))

    def test_no_breakout_on_flat_series(self) -> None:
        from tradermaximus.indicators.structure import is_breakout

        self.assertFalse(is_breakout([10.0] * 26, [10.5] * 26, 20))

    def test_breakdown_mirrors_breakout(self) -> None:
        from tradermaximus.indicators.structure import is_breakdown

        closes = [10.0] * 25 + [8.0]
        lows = [5.0] * 25 + [7.5]
        self.assertTrue(is_breakdown(closes, lows, 20))
        self.assertFalse(is_breakdown(closes, lows, 20, basis="high"))


class TestBasePivot(unittest.TestCase):
    """Regression: a pivot spanning the current bar can never be broken.

    The pivot was measured over a window that included the current bar, so
    ``close > pivot`` reduced to ``close > this bar's own high`` — impossible
    by definition. Three CAN SLIM strategies could not produce a single signal.
    """

    def setUp(self) -> None:
        from tests.helpers import random_walk, series

        self.candles = random_walk(400, seed=21, drift=0.0012)
        self.series = series(self.candles)

    def test_pivot_excludes_the_current_bar(self) -> None:
        from tradermaximus.indicators.structure import detect_flat_base

        base = detect_flat_base(
            self.series.highs, self.series.lows, self.series.closes, self.series.volumes
        )
        if base is None:
            self.skipTest("no flat base formed in this fixture")
        # The pivot must come from bars before the current one, so it can be
        # exceeded without requiring a close above its own bar's high.
        self.assertLessEqual(base.end_index, len(self.series) - 2)

    def test_close_above_pivot_is_reachable(self) -> None:
        """Sweep a series and confirm the breakout condition can actually fire."""
        from tradermaximus.indicators.structure import detect_flat_base
        from tests.helpers import random_walk, series

        detected = breakouts = 0
        candles = random_walk(600, seed=5, drift=0.0015, calm_tail=0)
        full = series(candles)
        for index in range(300, len(full)):
            visible = full.slice_until(full.timestamps[index]).tail(300)
            base = detect_flat_base(
                visible.highs, visible.lows, visible.closes, visible.volumes
            )
            if base is None:
                continue
            detected += 1
            if visible.last.close > base.pivot:
                breakouts += 1
        self.assertGreater(detected, 0, "no base ever formed; fixture is unusable")
        self.assertGreater(
            breakouts, 0, "close never exceeded the pivot: the pivot bug has returned"
        )

    def test_consolidation_range_honours_exclusion(self) -> None:
        from tradermaximus.indicators.structure import consolidation_range

        highs = [10.0] * 20 + [99.0]
        lows = [9.0] * 21
        _, high_including, _ = consolidation_range(highs, lows, 21, exclude_recent=0)
        _, high_excluding, _ = consolidation_range(highs, lows, 20, exclude_recent=1)
        self.assertEqual(high_including, 99.0)
        self.assertEqual(high_excluding, 10.0)
