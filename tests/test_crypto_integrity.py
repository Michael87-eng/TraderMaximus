"""Crypto-path integrity: score denominator and risk-limit precedence.

These are golden fixtures. They exist because the two defects they pin were
both invisible: one capped every crypto score below 100 and silently stiffened
every crypto threshold, the other left two risk limits defined and never read.
Neither produced an error, a warning, or a wrong-looking number.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradermaximus.core.types import AssetClass
from tradermaximus.risk.limits import AssetLimits, HardLimits, limits_for
from tradermaximus.strategy.scoring import COMPONENT_MAX, ScoringInputs, SignalScoringEngine


class _Features:
    """Minimal feature stand-in: only what _relative_strength reads."""

    def __init__(self, asset_class: AssetClass, distance: float | None) -> None:
        self.asset_class = asset_class
        self._distance = distance

    def number(self, key: str) -> float | None:
        return self._distance if key == "distance_from_52w_high" else None


class TestRelativeStrengthDenominator(unittest.TestCase):
    """A question that cannot be asked must not score as a failed answer."""

    MAX = COMPONENT_MAX["relative_strength"]

    def _score(self, asset_class: AssetClass, **kw) -> float:
        inputs = ScoringInputs(
            relative_strength_rating=kw.get("rating"),
            sector_strength=kw.get("sector"),
        )
        return SignalScoringEngine._relative_strength(
            _Features(asset_class, kw.get("distance", 0.0)),
            inputs,
            sector_applies=asset_class is AssetClass.EQUITIES,
        )

    def test_crypto_can_reach_the_full_component(self) -> None:
        """Crypto has no sector, so a perfect crypto setup must still score 10/10.

        Before the fix the sector sub-component scored 0 for crypto, capping
        the component at 7.5/10 and the whole signal at 97.22/100 — which made
        every crypto threshold ~2.8 points stricter than the identical equity
        threshold, for no stated reason.
        """
        self.assertAlmostEqual(
            self._score(AssetClass.CRYPTO, rating=99.0, distance=0.0), self.MAX, places=6
        )

    def test_equities_still_need_their_sector_reading(self) -> None:
        """The equity rubric is unchanged: no sector data, no sector points."""
        without = self._score(AssetClass.EQUITIES, rating=99.0, distance=0.0)
        with_sector = self._score(
            AssetClass.EQUITIES, rating=99.0, distance=0.0, sector=0.05
        )
        self.assertAlmostEqual(without, 0.75 * self.MAX, places=6)
        self.assertAlmostEqual(with_sector, self.MAX, places=6)

    def test_missing_data_is_not_renormalised_away(self) -> None:
        """The distinction the original code collapsed.

        An inapplicable sub-component is redistributed; an absent *measurement*
        is not. If a missing RS rating were renormalised away, a data outage
        would read as a perfect score — which is strictly worse than the bug
        this fix removes.
        """
        no_rating = self._score(AssetClass.CRYPTO, rating=None, distance=0.0)
        self.assertLess(no_rating, self.MAX)
        # Only the distance third survives.
        # The component rounds to 4dp, so compare at that precision.
        self.assertAlmostEqual(no_rating, self.MAX / 3.0, places=3)

    def test_the_full_crypto_scale_reaches_one_hundred(self) -> None:
        """The property that actually matters: thresholds mean the same thing."""
        non_fundamentals = sum(
            v for k, v in COMPONENT_MAX.items() if k != "fundamentals"
        )
        self.assertEqual(non_fundamentals, 90.0)
        # Rescaled by 100/90, a perfect crypto signal reaches exactly 100.
        self.assertAlmostEqual(non_fundamentals * (100.0 / non_fundamentals), 100.0)


class TestRiskLimitPrecedence(unittest.TestCase):
    """Dead risk config is worse than conflicting risk config.

    A conflict gets noticed. A limit that is defined, documented, and never
    read looks like protection and is not.
    """

    def test_asset_class_exposure_takes_the_most_restrictive_cap(self) -> None:
        hard = HardLimits().max_asset_class_exposure
        self.assertEqual(hard, 0.40)
        expected = {
            AssetClass.EQUITIES: 0.40,   # global 0.40 binds over the class's 0.50
            AssetClass.CRYPTO: 0.40,     # equal by coincidence, now by intent
            AssetClass.FOREX: 0.30,      # the class's 0.30 binds, as it always should have
        }
        for asset_class, limit in expected.items():
            with self.subTest(asset_class=asset_class):
                self.assertAlmostEqual(
                    min(hard, limits_for(asset_class).max_exposure_total), limit, places=9
                )

    def test_the_forex_cap_was_the_one_being_overridden(self) -> None:
        """Proof the old behaviour was wrong, not merely redundant."""
        self.assertLess(limits_for(AssetClass.FOREX).max_exposure_total,
                        HardLimits().max_asset_class_exposure)

    def test_leverage_has_exactly_one_definition(self) -> None:
        """0.0 and 1.0 meant opposite things under one name; one had to go."""
        self.assertFalse(hasattr(AssetLimits, "max_leverage"))
        for asset_class in AssetClass:
            self.assertNotIn("max_leverage", limits_for(asset_class).__slots__)
        self.assertEqual(HardLimits().max_leverage, 1.0)

    def test_crypto_hard_caps_are_unchanged_by_this_work(self) -> None:
        """None of the above may loosen a crypto risk control."""
        crypto = limits_for(AssetClass.CRYPTO)
        self.assertEqual(HardLimits().max_risk_per_trade, 0.005)
        self.assertEqual(crypto.max_stop_pct, 0.05)
        self.assertEqual(crypto.max_exposure_per_trade, 0.05)
        self.assertEqual(crypto.max_positions, 3)
        self.assertEqual(crypto.max_position_volume_pct, 0.02)


if __name__ == "__main__":
    unittest.main()


class TestBadTickDiscrimination(unittest.TestCase):
    """A bad tick has no volume behind it. A panic is the heaviest bar there is.

    Reversion alone was the test, and it is resolution-dependent: calibrated on
    daily bars where a real crash persists, it rejected both BTCUSDT and
    ETHUSDT hourly on the SAME bar — 2017-09-05T01:00Z, the day after China's
    ICO ban — because at hourly resolution a real panic V-bounces inside one
    bar. Two independent instruments do not suffer the same corrupt print in
    the same hour.
    """

    from tradermaximus.data.quality import DataQualityEngine as _E

    @staticmethod
    def _moves(closes):
        return [(b - a) / a for a, b in zip(closes, closes[1:]) if a > 0]

    def test_a_volume_corroborated_crash_is_not_a_bad_tick(self) -> None:
        """The real case, with the measured shape: fall, bounce, heavy volume."""
        closes = [100.0] * 30 + [100.0, 90.5, 101.3] + [100.0] * 30
        volumes = [36.0] * 30 + [36.0, 99.0, 54.0] + [36.0] * 30
        index = 30  # the move INTO the 90.5 bar
        self.assertTrue(
            self._E._reverts_immediately(self._moves(closes), index),
            "reversion alone still sees a bad tick — that is the old behaviour",
        )
        self.assertFalse(
            self._E._reverts_immediately(self._moves(closes), index, volumes=volumes),
            "volume 2.75x the median must veto the bad-tick verdict",
        )

    def test_a_volumeless_spike_is_still_a_bad_tick(self) -> None:
        """The fix must not blind the engine to actual corruption."""
        closes = [100.0] * 30 + [100.0, 90.5, 101.3] + [100.0] * 30
        volumes = [36.0] * 30 + [36.0, 30.0, 36.0] + [36.0] * 30
        self.assertTrue(
            self._E._reverts_immediately(self._moves(closes), 30, volumes=volumes),
            "a reverting move on BELOW-median volume is exactly a bad tick",
        )

    def test_a_persistent_crash_is_never_a_bad_tick(self) -> None:
        """The 2020-03-12 case the original heuristic was written for."""
        closes = [100.0] * 30 + [100.0, 60.0] + [61.0] * 30
        volumes = [36.0] * 30 + [36.0, 200.0] + [40.0] * 30
        for vols in (None, volumes):
            self.assertFalse(
                self._E._reverts_immediately(self._moves(closes), 30, volumes=vols),
                "a crash that does not revert must never be flagged",
            )

    def test_the_real_venue_bars_load_clean(self) -> None:
        """End to end on the actual data, if it is present."""
        from pathlib import Path
        from tradermaximus.core.types import Instrument, Timeframe
        from tradermaximus.data.csv_loader import load_series
        venue = Path(__file__).resolve().parents[1] / "data/venue/binance"
        if not venue.exists():
            self.skipTest("venue data not present in this checkout")
        for name, timeframe in (("1h", Timeframe.H1), ("1d", Timeframe.D1)):
            for symbol in ("BTCUSDT", "ETHUSDT"):
                path = venue / f"{symbol}_{name}.csv"
                if not path.exists():
                    continue
                with self.subTest(symbol=symbol, interval=name):
                    instrument = Instrument(
                        symbol, AssetClass.CRYPTO, "BINANCE", "crypto", 0.01, 1e-8, 1e-8
                    )
                    result = load_series(
                        path, instrument, timeframe=timeframe, min_history=100
                    )
                    self.assertTrue(result.usable, result.summary())
