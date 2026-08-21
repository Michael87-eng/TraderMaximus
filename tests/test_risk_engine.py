"""Risk authorisation, sizing, portfolio limits, breakers and the kill switch."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tradermaximus.core.config import Layer, SystemConfig
from tradermaximus.core.types import (
    AssetClass,
    Instrument,
    ScoreBreakdown,
    Side,
    Signal,
)
from tradermaximus.observability.journal import Journal
from tradermaximus.portfolio.book import PositionBook
from tradermaximus.risk.circuit_breakers import BreakerScope, CircuitBreakerEngine
from tradermaximus.risk.engine import RiskContext, RiskEngine, Rule
from tradermaximus.risk.kill_switch import KillSwitch, KillTrigger, SystemSignals
from tradermaximus.risk.limits import HardLimits, limits_for
from tradermaximus.risk.portfolio import PortfolioRiskEngine
from tradermaximus.risk.sizing import PositionSizingEngine, SizingRejection
from tradermaximus.risk.trailing import TrailingStopEngine

NOW = datetime(2024, 6, 3, 14, 0, tzinfo=timezone.utc)
AAPL = Instrument("AAPL", AssetClass.EQUITIES, "NASDAQ", "Technology", 0.01, 1.0, 1.0)


def make_signal(score: float = 75.0, structure: float = 94.0, entry: float = 100.0) -> Signal:
    return Signal(
        symbol="AAPL",
        asset_class=AssetClass.EQUITIES,
        strategy_id="STRATEGY_001",
        side=Side.LONG,
        entry_price=entry,
        timestamp=NOW,
        score=ScoreBreakdown({"total": score}),
        structure_level=structure,
        atr=2.5,
        regime="TREND_UP",
        regime_confidence=0.9,
        features={"volatility_percentile": 0.4, "strategy_type": "momentum"},
    )


class TestPositionSizing(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = PositionSizingEngine(HardLimits())

    def size(self, **kwargs):
        defaults = dict(
            instrument=AAPL,
            equity=100_000.0,
            entry_price=100.0,
            stop_distance=6.0,
            volatility=0.25,
            average_daily_volume=5_000_000.0,
            apply_volatility_scaling=False,
        )
        defaults.update(kwargs)
        return self.engine.size(**defaults)

    def test_risk_budget_drives_the_quantity(self) -> None:
        """0.5% of 100k = 500 risk budget, over a 6.00 stop = 83 shares."""
        decision = self.size()
        self.assertTrue(decision.approved)
        self.assertEqual(decision.quantity, 83.0)
        self.assertEqual(decision.binding_cap, "risk_budget")

    def test_realised_risk_never_exceeds_the_limit(self) -> None:
        """Flooring to the lot grid can only reduce risk, never increase it."""
        decision = self.size()
        self.assertLessEqual(decision.risk_pct, 0.005 + 1e-12)

    def test_exposure_cap_binds_on_a_tight_stop(self) -> None:
        decision = self.size(stop_distance=0.5)
        self.assertTrue(decision.approved)
        self.assertIn(decision.binding_cap, {"max_exposure_per_trade", "max_single_position"})

    def test_liquidity_cap_binds_on_thin_volume(self) -> None:
        decision = self.size(average_daily_volume=500.0)
        self.assertEqual(decision.binding_cap, "market_impact")

    def test_regime_multiplier_halves_the_position(self) -> None:
        full = self.size()
        half = self.size(regime_multiplier=0.5)
        self.assertEqual(half.quantity, 41.0)
        self.assertLess(half.quantity, full.quantity)

    def test_undersized_position_is_rejected_not_rounded_up(self) -> None:
        decision = self.size(equity=1_000.0, stop_distance=50.0)
        self.assertFalse(decision.approved)
        self.assertIs(decision.rejection, SizingRejection.BELOW_MINIMUM_SIZE)

    def test_volatility_scaling_only_reduces(self) -> None:
        calm = self.size(volatility=0.10, apply_volatility_scaling=True)
        wild = self.size(volatility=0.80, apply_volatility_scaling=True)
        self.assertLessEqual(calm.volatility_adjustment, 1.0)
        self.assertLess(wild.quantity, calm.quantity)

    def test_unknown_volatility_uses_the_safest_band(self) -> None:
        limits = limits_for(AssetClass.EQUITIES)
        self.assertEqual(limits.risk_per_trade(None), min(b.risk_per_trade for b in limits.risk_bands))


class TestRiskAuthorization(unittest.TestCase):
    def setUp(self) -> None:
        self.journal = Journal()
        self.book = PositionBook(100_000)
        self.engine = RiskEngine(journal=self.journal)

    def context(self, **kwargs) -> RiskContext:
        defaults = dict(
            equity=100_000.0,
            snapshot=self.book.snapshot(NOW),
            average_daily_volume=5_000_000.0,
            volatility=0.25,
            minimum_score=60.0,
            spread_bps=8.0,
            estimated_slippage_bps=5.0,
        )
        defaults.update(kwargs)
        return RiskContext(**defaults)

    def test_clean_signal_is_approved(self) -> None:
        verdict = self.engine.authorize(make_signal(), AAPL, self.context())
        self.assertTrue(verdict.approved)
        self.assertGreater(verdict.quantity, 0)
        self.assertIsNotNone(verdict.stop_price)

    def test_wide_structural_stop_is_rejected(self) -> None:
        verdict = self.engine.authorize(make_signal(structure=91.0), AAPL, self.context())
        self.assertFalse(verdict.approved)
        self.assertEqual(verdict.rule, Rule.STOP_REJECTED)

    def test_low_score_is_rejected(self) -> None:
        verdict = self.engine.authorize(make_signal(score=45.0), AAPL, self.context())
        self.assertEqual(verdict.rule, Rule.SIGNAL_SCORE)

    def test_untradeable_regime_is_rejected(self) -> None:
        verdict = self.engine.authorize(make_signal(), AAPL, self.context(minimum_score=None))
        self.assertEqual(verdict.rule, Rule.REGIME_INELIGIBLE)

    def test_loss_limits_block_new_risk(self) -> None:
        for field, rule in (
            ("daily_loss", Rule.DAILY_LOSS),
            ("weekly_loss", Rule.WEEKLY_LOSS),
            ("monthly_drawdown", Rule.MONTHLY_DRAWDOWN),
            ("portfolio_drawdown", Rule.PORTFOLIO_DRAWDOWN),
        ):
            verdict = self.engine.authorize(make_signal(), AAPL, self.context(**{field: 0.99}))
            self.assertEqual(verdict.rule, rule, f"{field} did not trigger {rule}")

    def test_execution_conditions_block_entry(self) -> None:
        self.assertEqual(
            self.engine.authorize(make_signal(), AAPL, self.context(spread_bps=80.0)).rule,
            Rule.SPREAD,
        )
        self.assertEqual(
            self.engine.authorize(
                make_signal(), AAPL, self.context(estimated_slippage_bps=45.0)
            ).rule,
            Rule.SLIPPAGE,
        )

    def test_data_quality_failure_blocks_entry(self) -> None:
        verdict = self.engine.authorize(
            make_signal(),
            AAPL,
            self.context(data_quality_passed=False, data_quality_reason="stale data"),
        )
        self.assertEqual(verdict.rule, Rule.DATA_QUALITY)

    def test_kill_switch_takes_precedence_over_every_other_reason(self) -> None:
        """The recorded reason must be the condition that actually stopped it."""
        kill = KillSwitch()
        kill.engage(KillTrigger.MANUAL, "operator halt")
        engine = RiskEngine(kill_switch=kill, journal=self.journal)
        verdict = engine.authorize(
            make_signal(score=5.0), AAPL, self.context(daily_loss=0.9, spread_bps=999.0)
        )
        self.assertEqual(verdict.rule, Rule.KILL_SWITCH)

    def test_circuit_breaker_blocks_entry(self) -> None:
        breakers = CircuitBreakerEngine()
        breakers.start_session(100_000, NOW)
        breakers.evaluate(97_000)
        engine = RiskEngine(breakers=breakers, journal=self.journal)
        verdict = engine.authorize(make_signal(), AAPL, self.context())
        self.assertEqual(verdict.rule, Rule.CIRCUIT_BREAKER)

    def test_duplicate_position_is_rejected(self) -> None:
        self.book.open_position(
            symbol="AAPL", asset_class=AssetClass.EQUITIES, side=Side.LONG, quantity=10,
            entry_price=100.0, strategy_id="S", stop_price=94.0, sector="Technology", timestamp=NOW,
        )
        verdict = self.engine.authorize(
            make_signal(), AAPL, self.context(snapshot=self.book.snapshot(NOW))
        )
        self.assertEqual(verdict.rule, Rule.DUPLICATE_POSITION)

    def test_concentration_blocks_the_marginal_trade(self) -> None:
        self.book.open_position(
            symbol="MSFT", asset_class=AssetClass.EQUITIES, side=Side.LONG, quantity=70,
            entry_price=400.0, strategy_id="S", stop_price=380.0, sector="Technology", timestamp=NOW,
        )
        self.book.update_price("MSFT", 400.0)
        verdict = self.engine.authorize(
            make_signal(), AAPL, self.context(snapshot=self.book.snapshot(NOW))
        )
        self.assertFalse(verdict.approved)
        self.assertIn(verdict.rule, {Rule.SINGLE_POSITION, Rule.SECTOR_EXPOSURE, Rule.PORTFOLIO_EXPOSURE})

    def test_rejections_are_aggregated_in_the_journal(self) -> None:
        self.engine.authorize(make_signal(score=10.0), AAPL, self.context())
        self.engine.authorize(make_signal(score=11.0), AAPL, self.context())
        self.assertEqual(self.journal.rejection_summary().get(Rule.SIGNAL_SCORE), 2)


class TestPortfolioLimits(unittest.TestCase):
    def test_diversified_book_is_compliant(self) -> None:
        book = PositionBook(100_000)
        for symbol, quantity, price, sector in [
            ("AAPL", 30, 180.0, "Technology"),
            ("XLE", 20, 90.0, "Energy"),
            ("JPM", 20, 140.0, "Financials"),
            ("KO", 30, 60.0, "Staples"),
        ]:
            book.open_position(
                symbol=symbol, asset_class=AssetClass.EQUITIES, side=Side.LONG, quantity=quantity,
                entry_price=price, strategy_id="S", stop_price=price * 0.95, sector=sector, timestamp=NOW,
            )
            book.update_price(symbol, price)
        engine = PortfolioRiskEngine()
        metrics = engine.compute(book.snapshot(NOW))
        self.assertEqual(engine.check(metrics), [])

    def test_concentrated_book_breaches_multiple_limits(self) -> None:
        book = PositionBook(100_000)
        for symbol, quantity, price in [("AAPL", 60, 180.0), ("MSFT", 40, 400.0), ("NVDA", 30, 900.0)]:
            book.open_position(
                symbol=symbol, asset_class=AssetClass.EQUITIES, side=Side.LONG, quantity=quantity,
                entry_price=price, strategy_id="S", stop_price=price * 0.94,
                sector="Technology", timestamp=NOW,
            )
            book.update_price(symbol, price)
        engine = PortfolioRiskEngine()
        breaches = {b.rule for b in engine.check(engine.compute(book.snapshot(NOW)))}
        self.assertIn("SECTOR_EXPOSURE:Technology", breaches)
        self.assertIn("SINGLE_POSITION", breaches)
        self.assertIn("PORTFOLIO_EXPOSURE", breaches)

    def test_correlated_cluster_detection_is_transitive(self) -> None:
        """A~B and B~C makes one cluster, even if A and C are not direct."""
        book = PositionBook(100_000)
        for symbol in ("AAA", "BBB", "CCC"):
            book.open_position(
                symbol=symbol, asset_class=AssetClass.EQUITIES, side=Side.LONG, quantity=10,
                entry_price=100.0, strategy_id="S", stop_price=94.0, sector="Tech", timestamp=NOW,
            )
            book.update_price(symbol, 100.0)
        base = [0.01, -0.02, 0.03, -0.01, 0.02, 0.005, -0.015, 0.02]
        returns = {"AAA": base, "BBB": base, "CCC": base}
        metrics = PortfolioRiskEngine().compute(book.snapshot(NOW), returns_by_symbol=returns)
        self.assertEqual(len(metrics.correlated_clusters), 1)
        self.assertEqual(set(metrics.correlated_clusters[0]), {"AAA", "BBB", "CCC"})


class TestCircuitBreakers(unittest.TestCase):
    def test_daily_loss_latches_and_survives_recovery(self) -> None:
        breakers = CircuitBreakerEngine()
        breakers.start_session(100_000, NOW)
        self.assertEqual(breakers.evaluate(99_500), [])
        trips = breakers.evaluate(98_400)
        self.assertEqual([t.rule for t in trips], ["DAILY_LOSS"])
        self.assertTrue(breakers.blocks_new_trades)
        # A recovery within the same session must not clear the latch.
        breakers.evaluate(101_000)
        self.assertTrue(breakers.active)

    def test_daily_latch_clears_next_session(self) -> None:
        breakers = CircuitBreakerEngine()
        breakers.start_session(100_000, NOW)
        breakers.evaluate(98_000)
        self.assertTrue(breakers.active)
        breakers.start_session(98_000, NOW.replace(day=4))
        self.assertFalse(breakers.active)

    def test_monthly_drawdown_requires_human_review(self) -> None:
        breakers = CircuitBreakerEngine()
        breakers.start_session(100_000, NOW)
        breakers.evaluate(93_000)
        self.assertTrue(breakers.requires_human_review)
        scopes = {t.scope for t in breakers.trips}
        self.assertIn(BreakerScope.MONTHLY, scopes)

    def test_trade_count_limit(self) -> None:
        breakers = CircuitBreakerEngine(max_daily_trades=5)
        breakers.start_session(100_000, NOW)
        for _ in range(6):
            breakers.record_trade()
        self.assertIn("DAILY_TRADE_COUNT", [t.rule for t in breakers.evaluate(100_000)])


class TestKillSwitch(unittest.TestCase):
    def test_healthy_signals_do_not_engage(self) -> None:
        kill = KillSwitch()
        self.assertEqual(kill.check(SystemSignals(session_drawdown=0.02, leverage=0.5)), [])
        self.assertFalse(kill.active)

    def test_multiple_triggers_are_all_recorded(self) -> None:
        kill = KillSwitch()
        events = kill.check(
            SystemSignals(
                session_drawdown=0.12,
                broker_position_mismatch=0.08,
                consecutive_order_failures=4,
            )
        )
        self.assertEqual(len(events), 3)
        self.assertTrue(kill.active)

    def test_unknown_signals_are_not_treated_as_healthy(self) -> None:
        """A signal the system cannot observe must skip its check, not pass it."""
        kill = KillSwitch()
        kill.check(SystemSignals())
        self.assertFalse(kill.active)

    def test_release_requires_operator_and_reason(self) -> None:
        kill = KillSwitch()
        kill.engage(KillTrigger.MANUAL, "test")
        with self.assertRaises(ValueError):
            kill.release(operator="", reason="")
        kill.release(operator="michael", reason="verified and reconciled")
        self.assertFalse(kill.active)

    def test_journal_receives_critical_alerts(self) -> None:
        """Regression: an empty Journal is falsy and was silently discarded."""
        journal = Journal()
        kill = KillSwitch(journal=journal)
        kill.check(SystemSignals(session_drawdown=0.15))
        self.assertGreaterEqual(len(journal.events(severity="critical")), 1)


class TestTrailingRatchet(unittest.TestCase):
    def test_stop_only_ever_tightens(self) -> None:
        from tradermaximus.core.types import Position

        position = Position(
            symbol="AAPL", asset_class=AssetClass.EQUITIES, side=Side.LONG, quantity=100,
            entry_price=100.0, opened_at=NOW, stop_price=94.0,
        )
        engine = TrailingStopEngine()
        stops = []
        for price in (102, 106, 111, 118, 130, 120, 105):
            engine.update(position, price, atr=2.0, ema=price * 0.96)
            stops.append(position.stop_price)
        self.assertEqual(stops, sorted(stops), "the trailing stop moved backwards")

    def test_breakeven_at_five_percent(self) -> None:
        from tradermaximus.core.types import Position

        position = Position(
            symbol="AAPL", asset_class=AssetClass.EQUITIES, side=Side.LONG, quantity=100,
            entry_price=100.0, opened_at=NOW, stop_price=94.0,
        )
        TrailingStopEngine().update(position, 106.0, atr=2.0)
        self.assertAlmostEqual(position.stop_price, 100.0)


class TestConfigGovernance(unittest.TestCase):
    def test_ai_layer_cannot_raise_a_hard_limit(self) -> None:
        config = SystemConfig.defaults()
        rejected = config.apply_ai_recommendation({"max_risk_per_trade": 0.05})
        self.assertTrue(rejected)
        self.assertEqual(config.require_float("max_risk_per_trade"), 0.005)

    def test_ai_layer_may_tighten(self) -> None:
        config = SystemConfig.defaults()
        rejected = config.apply_ai_recommendation({"max_spread_bps": 20.0})
        self.assertEqual(rejected, ())
        self.assertEqual(config.get("max_spread_bps"), 20.0)

    def test_lower_layer_cannot_loosen(self) -> None:
        config = SystemConfig.defaults()
        config.governor.apply(Layer.STRATEGY, {"max_sector_exposure": 0.90}, "strategy")
        self.assertEqual(config.get("max_sector_exposure"), 0.30)
        self.assertTrue(config.governor.violations)


if __name__ == "__main__":
    unittest.main()
