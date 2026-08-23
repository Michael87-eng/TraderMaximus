"""Data quality validation, provider fallback and regime classification."""

from __future__ import annotations

import unittest
from datetime import timedelta

from tradermaximus.core.errors import DataQualityError, ToolUnavailableError
from tradermaximus.core.types import AssetClass, Candle, Instrument, Quote, Timeframe, utc_now
from tradermaximus.data.providers import (
    MarketDataProvider,
    ProviderChain,
    StaticProvider,
    SyntheticProvider,
)
from tradermaximus.data.quality import DataQualityEngine, QualityIssue
from tradermaximus.indicators.engine import FeatureEngine
from tradermaximus.mcp.registry import ToolRegistry
from tradermaximus.observability.journal import Journal
from tradermaximus.regime.engine import (
    MarketContext,
    Regime,
    RegimeEngine,
    WEIGHTS,
    is_strategy_type_allowed,
    minimum_score,
)
from tests.helpers import BASE, EQUITY, candle, random_walk, series, trending


class TestDataQuality(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = DataQualityEngine()

    def validate(self, candles, **kwargs):
        defaults = {"enforce_staleness": False, "min_history": 50}
        defaults.update(kwargs)
        return self.engine.validate_series(series(candles), **defaults)

    def test_clean_series_passes(self) -> None:
        report = self.validate([candle(i, 100 + i, volume=500_000) for i in range(60)])
        self.assertTrue(report.passed)
        self.assertEqual(report.score, 1.0)

    def test_insufficient_history_is_fatal(self) -> None:
        report = self.validate([candle(i, 100 + i) for i in range(5)])
        self.assertFalse(report.passed)
        self.assertTrue(report.has_fatal)

    def test_malformed_bar_is_detected(self) -> None:
        candles = [candle(i, 100 + i, volume=500_000) for i in range(60)]
        bad = candles[30]
        candles[30] = Candle(bad.timestamp, 100, 101, 99, 50.0, 500_000)
        report = self.validate(candles)
        self.assertFalse(report.passed)
        self.assertIn(QualityIssue.INVALID_OHLC, [f.issue for f in report.findings])

    def test_unadjusted_split_is_flagged(self) -> None:
        candles = []
        for i in range(60):
            factor = 0.5 if i >= 30 else 1.0
            price = (100 + i) * factor
            candles.append(
                Candle(BASE + timedelta(days=i), price, price * 1.01, price * 0.99, price, 500_000)
            )
        report = self.validate(candles)
        self.assertFalse(report.passed)
        self.assertIn(
            QualityIssue.UNADJUSTED_CORPORATE_ACTION, [f.issue for f in report.findings]
        )

    def test_duplicate_timestamps_are_detected(self) -> None:
        candles = [candle(i, 100 + i, volume=500_000) for i in range(60)]
        candles.append(candles[-1])
        report = self.validate(candles)
        issues = [f.issue for f in report.findings]
        self.assertTrue(
            QualityIssue.DUPLICATE_TIMESTAMPS in issues
            or QualityIssue.NON_MONOTONIC_TIMESTAMPS in issues
        )

    def test_stale_data_is_detected(self) -> None:
        candles = [candle(i, 100 + i, volume=500_000) for i in range(60)]
        report = self.engine.validate_series(series(candles), min_history=50)
        self.assertFalse(report.passed)
        self.assertIn(QualityIssue.STALE_DATA, [f.issue for f in report.findings])

    def test_thin_liquidity_is_detected(self) -> None:
        report = self.validate([candle(i, 100 + i, volume=100) for i in range(60)])
        self.assertIn(QualityIssue.INSUFFICIENT_LIQUIDITY, [f.issue for f in report.findings])

    def test_wide_spread_quote_fails(self) -> None:
        quote = Quote(utc_now(), 100.0, 101.0)
        self.assertFalse(self.engine.validate_quote(quote).passed)

    def test_provider_disagreement_is_reported(self) -> None:
        report = self.engine.compare_providers(100.0, 101.0, symbol="AAPL")
        self.assertIn(QualityIssue.PROVIDER_DISAGREEMENT, [f.issue for f in report.findings])


class TestProviderChain(unittest.TestCase):
    def setUp(self) -> None:
        self.journal = Journal()
        self.synthetic = SyntheticProvider(seed=7)

    def test_synthetic_provider_is_deterministic(self) -> None:
        first = self.synthetic.fetch(EQUITY, Timeframe.D1, 200)
        second = self.synthetic.fetch(EQUITY, Timeframe.D1, 200)
        self.assertEqual(first.closes, second.closes)

    def test_chain_falls_back_when_primary_fails(self) -> None:
        class Dead(MarketDataProvider):
            name = "dead"

            def fetch(self, *args):
                raise ToolUnavailableError("offline")

        chain = ProviderChain([Dead(), self.synthetic], journal=self.journal)
        result = chain.fetch(EQUITY, Timeframe.D1, 100)
        self.assertEqual(result.provider, "synthetic")
        self.assertTrue(result.notes)

    def test_chain_halts_when_exhausted(self) -> None:
        class Dead(MarketDataProvider):
            name = "dead"

            def fetch(self, *args):
                raise ToolUnavailableError("offline")

        chain = ProviderChain([Dead()], journal=self.journal)
        with self.assertRaises(DataQualityError):
            chain.fetch(EQUITY, Timeframe.D1, 100)

    def test_synthetic_data_is_flagged(self) -> None:
        chain = ProviderChain([self.synthetic])
        self.assertTrue(chain.uses_synthetic_data)
        static = StaticProvider()
        static.load(EQUITY, Timeframe.D1, random_walk(100))
        self.assertFalse(ProviderChain([static]).uses_synthetic_data)


class TestRegimeEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.features = FeatureEngine()
        self.engine = RegimeEngine()

    def test_weights_sum_to_one(self) -> None:
        self.assertAlmostEqual(sum(WEIGHTS.values()), 1.0)

    def test_trending_calm_market_is_trend_up(self) -> None:
        features = self.features.compute(series(trending(400)))
        state = self.engine.classify(
            features,
            MarketContext(
                breadth_index=0.7, new_highs_ratio=0.75, correlation_change=0.05,
                index_ma200_slope=0.001,
            ),
        )
        self.assertIs(state.primary, Regime.TREND_UP)
        self.assertGreater(state.confidence, 0.7)

    def test_liquidity_stress_halts_everything(self) -> None:
        features = self.features.compute(series(random_walk(300)))
        state = self.engine.classify(features, MarketContext(spread_ratio=3.0, volume_ratio=0.3))
        self.assertIs(state.primary, Regime.LIQUIDITY_STRESS)
        self.assertFalse(state.is_tradeable)
        self.assertIsNone(minimum_score(state))
        allowed, _ = is_strategy_type_allowed("momentum", state)
        self.assertFalse(allowed)

    def test_high_volatility_tag_vetoes_breakouts(self) -> None:
        """A veto from any active regime blocks the strategy, not just primary."""
        features = self.features.compute(series(random_walk(400, seed=2, volatility=0.03)))
        state = self.engine.classify(
            features, MarketContext(breadth_index=0.8, new_highs_ratio=0.9)
        )
        if Regime.HIGH_VOLATILITY in state.all_regimes:
            allowed, reason = is_strategy_type_allowed("breakout", state)
            self.assertFalse(allowed)
            self.assertIn("HIGH_VOLATILITY", reason)

    def test_low_confidence_halves_size(self) -> None:
        features = self.features.compute(series(random_walk(120)))
        state = self.engine.classify(features)  # no market context at all
        self.assertLess(state.confidence, 0.70)
        self.assertEqual(state.size_multiplier, 0.5)

    def test_missing_inputs_reduce_confidence(self) -> None:
        features = self.features.compute(series(trending(400)))
        full = self.engine.classify(
            features,
            MarketContext(breadth_index=0.7, new_highs_ratio=0.75, correlation_change=0.05),
        )
        sparse = self.engine.classify(features, MarketContext())
        self.assertGreater(full.confidence, sparse.confidence)

    def test_minimum_score_takes_the_strictest_active_regime(self) -> None:
        features = self.features.compute(series(trending(400)))
        state = self.engine.classify(
            features,
            MarketContext(
                breadth_index=0.7, new_highs_ratio=0.75, correlation_change=0.05,
                index_ma200_slope=0.001,
            ),
        )
        threshold = minimum_score(state)
        self.assertIsNotNone(threshold)
        # TREND_UP is 60, BULL_MARKET 50, LOW_VOLATILITY 55: the max applies.
        self.assertEqual(threshold, 60.0)

    def test_trend_slope_normalisation_permits_trend_up(self) -> None:
        """Regression: normalising by 0.01/bar made TREND_UP unreachable."""
        from tradermaximus.regime.engine import TREND_SLOPE_REFERENCE

        self.assertLess(TREND_SLOPE_REFERENCE, 0.01)
        features = self.features.compute(series(trending(400, slope=0.0015)))
        state = self.engine.classify(
            features,
            MarketContext(
                breadth_index=0.7, new_highs_ratio=0.75, correlation_change=0.05,
                index_ma200_slope=0.001,
            ),
        )
        self.assertGreater(state.score, 0.70)


class TestToolRegistry(unittest.TestCase):
    def test_unregistered_tool_raises_rather_than_defaulting(self) -> None:
        registry = ToolRegistry(backoff_seconds=0.0)
        with self.assertRaises(ToolUnavailableError):
            registry.call("polygon-mcp", "AAPL")

    def test_retry_then_success(self) -> None:
        attempts = {"n": 0}

        def flaky(symbol: str) -> dict[str, object]:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("upstream 502")
            return {"symbol": symbol}

        registry = ToolRegistry(backoff_seconds=0.0)
        registry.register("polygon-mcp", flaky)
        result = registry.call("polygon-mcp", "AAPL")
        self.assertEqual(result.attempts, 3)
        self.assertFalse(result.from_fallback)

    def test_fallback_is_used_when_primary_dies(self) -> None:
        registry = ToolRegistry(backoff_seconds=0.0)
        registry.register("polygon-mcp", lambda s: (_ for _ in ()).throw(RuntimeError("dead")))
        registry.register("alphavantage-mcp", lambda s: {"symbol": s, "source": "av"})
        result = registry.call("polygon-mcp", "AAPL")
        self.assertTrue(result.from_fallback)
        self.assertEqual(result.tool, "alphavantage-mcp")

    def test_required_tools_are_reported_missing(self) -> None:
        registry = ToolRegistry()
        self.assertIn("polygon-mcp", registry.missing_required())
        registry.register("alphavantage-mcp", lambda s: {})
        self.assertEqual(registry.missing_required(), [])


if __name__ == "__main__":
    unittest.main()


class TestEligibilitySemantics(unittest.TestCase):
    """Permission and veto are asymmetric, and both must stay conservative."""

    def setUp(self) -> None:
        self.features = FeatureEngine()
        self.engine = RegimeEngine()

    def _trending_state(self):
        features = self.features.compute(series(trending(400)))
        return self.engine.classify(
            features,
            MarketContext(
                breadth_index=0.7, new_highs_ratio=0.75, correlation_change=0.05,
                index_ma200_slope=0.001,
            ),
        )

    def test_gap_up_is_permitted_in_trend_up(self) -> None:
        """Section 5.4 disallows 'Gap (except gap-up)' in TREND_UP.

        Regression: the carve-out was never added to the allowed set, so the
        gap strategy was eligible in no regime at all — 100% of bars killed.
        """
        from tradermaximus.regime.engine import ALLOWED_STRATEGY_TYPES, Regime

        self.assertIn("gap", ALLOWED_STRATEGY_TYPES[Regime.TREND_UP])
        allowed, _ = is_strategy_type_allowed("gap", self._trending_state())
        self.assertTrue(allowed)

    def test_permission_may_come_from_an_active_tag(self) -> None:
        """A tag that permits a strategy counts, not only the primary regime."""
        state = self._trending_state()
        self.assertIn(Regime.BULL_MARKET, state.all_regimes)
        allowed, reason = is_strategy_type_allowed("momentum", state)
        self.assertTrue(allowed)
        self.assertIn("eligible via", reason)

    def test_veto_still_overrides_any_permission(self) -> None:
        """Widening permission must not let a vetoed strategy through."""
        features = self.features.compute(series(random_walk(300)))
        stressed = self.engine.classify(
            features, MarketContext(spread_ratio=3.0, volume_ratio=0.3)
        )
        for strategy_type in ("momentum", "breakout", "reversal", "gap"):
            allowed, _ = is_strategy_type_allowed(strategy_type, stressed)
            self.assertFalse(allowed, f"{strategy_type} slipped past LIQUIDITY_STRESS")

    def test_unlisted_strategy_type_is_still_refused(self) -> None:
        """A type no active regime permits stays blocked by the union rule."""
        state = self._trending_state()
        allowed, reason = is_strategy_type_allowed("oscillator", state)
        self.assertFalse(allowed)
        self.assertIn("not in the allowed set", reason)

    def test_explicit_veto_is_reported_as_a_veto(self) -> None:
        """Short selling is disallowed in a bull market, and says so."""
        allowed, reason = is_strategy_type_allowed("short", self._trending_state())
        self.assertFalse(allowed)
        self.assertIn("disallowed in BULL_MARKET", reason)
