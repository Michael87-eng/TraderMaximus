"""Phase 4 and Phase 5 machinery: fold planning, gates, sizing and ladders.

These are tests of the *validation and risk machinery itself*, not of whether
an edge exists. A gate that silently mis-plans its folds, or a Monte Carlo
that never actually runs 5,000 simulations, would let a strategy through on a
verdict nobody computed.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradermaximus.core.types import AssetClass, Instrument, Side, TradeRecord
from tradermaximus.research.validation import (
    AcceptanceCriteria,
    monte_carlo,
    plan_walk_forward_folds,
)
from tradermaximus.risk.sizing import PositionSizingEngine
from tradermaximus.risk.stops import ATR_MULTIPLIERS, atr_multiplier_for
from tradermaximus.risk.targets import SCALE_OUT, TakeProfitEngine

NOW = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _trades(pnls: list[float]) -> list[TradeRecord]:
    return [
        TradeRecord(
            "X", "S", Side.LONG, 1.0, 100.0, 100.0 + pnl,
            NOW + timedelta(days=i), NOW + timedelta(days=i + 1),
            pnl, pnl / 10.0, 0.0, "target", "RANGE",
        )
        for i, pnl in enumerate(pnls)
    ]


class TestFoldPlanning(unittest.TestCase):
    def test_five_folds_with_non_overlapping_test_windows(self) -> None:
        folds = plan_walk_forward_folds(2500, train_bars=756, test_bars=252, folds=5)
        self.assertEqual(len(folds), 5)
        for earlier, later in zip(folds, folds[1:]):
            self.assertEqual(earlier.test_end, later.test_start,
                             "test windows must abut without overlapping")
        for fold in folds:
            self.assertEqual(fold.train_bars, 756)
            self.assertEqual(fold.test_bars, 252)

    def test_the_plan_is_anchored_to_the_end_of_history(self) -> None:
        """Recent regimes are what a deployment decision is about."""
        folds = plan_walk_forward_folds(2500, train_bars=756, test_bars=252, folds=5)
        self.assertEqual(folds[-1].test_end, 2500)

    def test_indicator_warmup_raises_a_short_train_window(self) -> None:
        """Three years of weekly bars is 156 — fewer than an MA200 needs."""
        folds = plan_walk_forward_folds(
            1400, train_bars=156, test_bars=52, folds=5, min_train_bars=300
        )
        self.assertEqual(len(folds), 5)
        for fold in folds:
            self.assertEqual(fold.train_bars, 300)

    def test_too_little_history_drops_folds_rather_than_faking_them(self) -> None:
        self.assertEqual(plan_walk_forward_folds(300, train_bars=756, test_bars=252, folds=5), ())
        partial = plan_walk_forward_folds(1200, train_bars=756, test_bars=252, folds=5)
        self.assertLess(len(partial), 5)
        self.assertTrue(all(f.train_start >= 0 for f in partial))

    def test_degenerate_inputs_return_no_folds(self) -> None:
        for kwargs in (
            {"train_bars": 0, "test_bars": 252, "folds": 5},
            {"train_bars": 756, "test_bars": 0, "folds": 5},
            {"train_bars": 756, "test_bars": 252, "folds": 0},
        ):
            self.assertEqual(plan_walk_forward_folds(2500, **kwargs), ())


class TestHardenedCriteria(unittest.TestCase):
    def test_the_gates_are_what_was_asked_for(self) -> None:
        c = AcceptanceCriteria()
        self.assertEqual(c.min_profit_factor_oos, 1.3)
        self.assertEqual(c.min_walk_forward_window_pf, 1.3)
        self.assertEqual(c.required_walk_forward_folds, 5)
        self.assertEqual(c.monte_carlo_simulations, 5_000)
        self.assertEqual(c.min_monte_carlo_profitable, 0.90)
        self.assertEqual(c.max_drawdown, 0.05)
        self.assertEqual(c.min_oos_sharpe_ratio, 0.80)

    def test_monte_carlo_runs_the_requested_number_of_simulations(self) -> None:
        result = monte_carlo(_trades([5.0, -3.0] * 15), simulations=5_000, seed=1)
        assert result is not None
        self.assertEqual(result.simulations, 5_000)
        self.assertEqual(result.simulations, AcceptanceCriteria().monte_carlo_simulations)

    def test_monte_carlo_separates_a_real_edge_from_a_marginal_one(self) -> None:
        """The gate must be able to tell 90% from not-90%."""
        strong = monte_carlo(_trades([10.0] * 25 + [-1.0] * 5), simulations=2_000, seed=1)
        marginal = monte_carlo(_trades([5.0] * 15 + [-5.0] * 15), simulations=2_000, seed=1)
        assert strong is not None and marginal is not None
        self.assertGreaterEqual(strong.positive_fraction, 0.90)
        self.assertLess(marginal.positive_fraction, 0.90)

    def test_too_few_trades_returns_no_verdict_rather_than_a_false_one(self) -> None:
        self.assertIsNone(monte_carlo(_trades([1.0] * 9)))


class TestAdaptiveStops(unittest.TestCase):
    def test_multipliers_match_the_phase_5_geometry(self) -> None:
        self.assertEqual(ATR_MULTIPLIERS["low"], 1.5)
        self.assertEqual(ATR_MULTIPLIERS["high"], 2.0)

    INSTRUMENT = Instrument("X", AssetClass.EQUITIES, "NASDAQ", "Tech", 0.01, 0.0001, 1.0)

    def _size(self, stop_distance: float):
        return PositionSizingEngine().size(
            instrument=self.INSTRUMENT, equity=100_000.0, entry_price=100.0,
            stop_distance=stop_distance, volatility=0.25,
            apply_volatility_scaling=False,
        )

    def test_a_wider_stop_is_the_same_money_at_risk(self) -> None:
        """The point of the widening: same risk, more room.

        When the risk budget is the binding cap — the ordinary case — a 2.0x
        ATR stop is a third wider than a 1.5x one, so the sizer takes a
        proportionally smaller position and the money at risk is unchanged.
        """
        atr = 4.0
        risks = []
        for multiplier in (atr_multiplier_for(0.10), atr_multiplier_for(0.90)):
            decision = self._size(atr * multiplier)
            self.assertEqual(decision.binding_cap, "risk_budget")
            risks.append(decision.quantity * atr * multiplier)
        # Equal to within one lot of rounding: sizes are floored onto the
        # instrument's lot grid, which can only ever round risk down.
        self.assertAlmostEqual(risks[0], risks[1], delta=atr * 2.0 * self.INSTRUMENT.lot_size)

    def test_an_exposure_capped_position_still_respects_the_hard_risk_cap(self) -> None:
        """The exception, stated rather than hidden.

        When the *exposure* cap binds instead of the risk budget, quantity is
        fixed by notional and no longer shrinks as the stop widens — so a wider
        stop does put more money at risk. That is only safe because the section
        8.1 per-trade cap is checked independently downstream. This test pins
        both halves: the risk does rise, and it stays under the hard cap.
        """
        atr = 2.0
        risks = []
        for multiplier in (atr_multiplier_for(0.10), atr_multiplier_for(0.90)):
            decision = self._size(atr * multiplier)
            self.assertEqual(decision.binding_cap, "max_exposure_per_trade")
            risks.append(decision.quantity * atr * multiplier)
        self.assertGreater(risks[1], risks[0])
        hard_cap = PositionSizingEngine().limits.max_risk_per_trade
        for risk in risks:
            self.assertLessEqual(risk / 100_000.0, hard_cap + 1e-9)


class TestVolatilityTargeting(unittest.TestCase):
    INSTRUMENT = Instrument("X", AssetClass.CRYPTO, "BINANCE", "crypto", 0.01, 1e-6, 1.0)

    def _risk(self, sizer: PositionSizingEngine, volatility: float) -> float:
        decision = sizer.size(
            instrument=self.INSTRUMENT, equity=100_000.0, entry_price=100.0,
            stop_distance=5.0, volatility=volatility,
        )
        return decision.quantity * 5.0 / 100_000.0

    def test_a_hotter_market_gets_a_smaller_position(self) -> None:
        sizer = PositionSizingEngine(volatility_target=0.15)
        self.assertGreater(self._risk(sizer, 0.30), self._risk(sizer, 0.80))

    def test_targeting_never_scales_up_by_default(self) -> None:
        """Section 18.3 permits scaling down only.

        EURUSD runs near 4% annualised; an uncapped 15% target would ask for
        nearly four times the position. The cap is what stops a volatility
        *target* from becoming a leverage instruction.
        """
        targeted = PositionSizingEngine(volatility_target=0.15)
        plain = PositionSizingEngine()
        self.assertLessEqual(self._risk(targeted, 0.04), self._risk(plain, 0.04))

    def test_the_per_trade_hard_cap_still_binds(self) -> None:
        loose = PositionSizingEngine(volatility_target=0.15, max_volatility_scale=10.0)
        self.assertLessEqual(self._risk(loose, 0.01), loose.limits.max_risk_per_trade + 1e-9)


class TestScaleOutLadder(unittest.TestCase):
    def test_the_percentage_ladder_is_50_30_20(self) -> None:
        self.assertEqual([(t.gain_pct, t.fraction) for t in SCALE_OUT.targets],
                         [(0.05, 0.50), (0.10, 0.30)])
        self.assertAlmostEqual(SCALE_OUT.remainder, 0.20, places=9)

    def test_the_atr_ladder_travels_with_the_instrument(self) -> None:
        """A 5% rung is a year away on EURUSD and a Tuesday on SOL.

        The ATR-denominated form of the same 50/30/20 shape puts the first rung
        at the same *opportunity* distance on both, which is the only way one
        ladder can serve three asset classes.
        """
        engine = TakeProfitEngine()
        fx = engine.atr_scale_out(atr=0.007, entry_price=1.09)
        crypto = engine.atr_scale_out(atr=8.0, entry_price=150.0)
        self.assertLess(fx.targets[0].gain_pct, 0.02)
        self.assertGreater(crypto.targets[0].gain_pct, 0.05)
        for ladder in (fx, crypto):
            self.assertEqual([t.fraction for t in ladder.targets], [0.50, 0.30])
            self.assertAlmostEqual(ladder.remainder, 0.20, places=9)

    def test_a_degenerate_atr_falls_back_to_the_percentage_ladder(self) -> None:
        engine = TakeProfitEngine()
        self.assertIs(engine.atr_scale_out(atr=0.0, entry_price=100.0), SCALE_OUT)
        self.assertIs(engine.atr_scale_out(atr=1.0, entry_price=0.0), SCALE_OUT)


if __name__ == "__main__":
    unittest.main()


class TestCorrelationSizing(unittest.TestCase):
    """Phase 3: a position that duplicates open risk goes on smaller."""

    @staticmethod
    def _series(seed: int, n: int = 40) -> list[float]:
        import random
        rng = random.Random(seed)
        return [rng.gauss(0.0, 0.01) for _ in range(n)]

    def test_a_correlated_candidate_is_cut_by_30_percent(self) -> None:
        from tradermaximus.risk.portfolio import (
            CORRELATION_REDUCTION,
            correlation_size_multiplier,
        )
        base = self._series(1)
        duplicate = [value * 1.01 for value in base]
        multiplier, correlation = correlation_size_multiplier(base, {"A": duplicate})
        assert correlation is not None
        self.assertGreater(correlation, 0.70)
        self.assertAlmostEqual(multiplier, 1.0 - CORRELATION_REDUCTION, places=9)

    def test_an_independent_candidate_is_untouched(self) -> None:
        from tradermaximus.risk.portfolio import correlation_size_multiplier
        multiplier, _ = correlation_size_multiplier(self._series(1), {"A": self._series(2)})
        self.assertEqual(multiplier, 1.0)

    def test_an_unmeasurable_correlation_is_not_treated_as_independence(self) -> None:
        """...nor as a reason to cut. Both would be inventing a measurement."""
        from tradermaximus.risk.portfolio import correlation_size_multiplier
        multiplier, correlation = correlation_size_multiplier(self._series(1), {})
        self.assertEqual((multiplier, correlation), (1.0, None))
        multiplier, correlation = correlation_size_multiplier([0.01, 0.02], {"A": [0.01, 0.02]})
        self.assertEqual(multiplier, 1.0)


class TestRegimeFamilyWeighting(unittest.TestCase):
    """Phase 3: a regime that favours a family weights it up — bounded."""

    @staticmethod
    def _state(primary, tags=frozenset(), confidence: float = 1.0):
        from tradermaximus.regime.engine import RegimeState
        return RegimeState(primary=primary, tags=tags, score=50.0, confidence=confidence)

    def test_each_regime_favours_the_family_it_should(self) -> None:
        from tradermaximus.regime.engine import (
            FAVOURED_FAMILY_WEIGHT,
            Regime,
            family_regime_weight,
        )
        cases = [
            (Regime.TREND_UP, "momentum", 1.5),
            (Regime.TREND_UP, "mean_reversion", 1.0),
            (Regime.RANGE, "mean_reversion", 1.5),
            (Regime.RANGE, "momentum", 1.0),
            (Regime.HIGH_VOLATILITY, "reversal", 1.5),
            (Regime.HIGH_VOLATILITY, "momentum", 1.0),
        ]
        for regime, family, expected in cases:
            with self.subTest(regime=regime, family=family):
                self.assertEqual(
                    family_regime_weight(family, self._state(regime), FAVOURED_FAMILY_WEIGHT),
                    expected,
                )

    def test_the_weighting_is_off_by_default(self) -> None:
        """Measured, and it made things worse. So it is built and switched off.

        Applied to real EURUSD and GBPUSD it took the trade count from 48 to 72
        and in-sample profit factor from 1.47 to 1.04: the extra trades were
        losers. Clamping the weighted component to its own maximum keeps the
        total inside 0-100 but does not stop a below-bar setup from clearing
        its bar, because the component is not normally at its maximum.
        """
        from tradermaximus.regime.engine import REGIME_FAMILY_WEIGHT, Regime, family_regime_weight

        self.assertEqual(dict(REGIME_FAMILY_WEIGHT), {})
        for regime in Regime:
            for family in ("momentum", "mean_reversion", "reversal", "breakout"):
                self.assertEqual(family_regime_weight(family, self._state(regime)), 1.0)

    def test_the_weight_cannot_push_a_signal_past_its_own_bar(self) -> None:
        """The reason this weights a component and not the total.

        Scaling a signal total by 1.5 would let a 50 present as a 75 and clear
        a momentum bar it never earned. Weighting the regime-alignment
        component expresses the same preference on the 20 points that exist for
        it, and the component stays inside its own maximum.
        """
        from tradermaximus.regime.engine import Regime
        from tradermaximus.strategy.library import MOMENTUM_BREAKOUT
        from tradermaximus.strategy.scoring import COMPONENT_MAX, SignalScoringEngine

        maximum = COMPONENT_MAX["market_regime_alignment"]
        favoured = SignalScoringEngine._regime_alignment(
            MOMENTUM_BREAKOUT, self._state(Regime.TREND_UP)
        )
        self.assertLessEqual(favoured, maximum)
        self.assertGreater(favoured, 0.0)

    def test_an_ambiguous_regime_takes_the_most_generous_reading(self) -> None:
        """A family is not penalised for the classifier being unsure."""
        from tradermaximus.regime.engine import (
            FAVOURED_FAMILY_WEIGHT,
            Regime,
            family_regime_weight,
        )
        state = self._state(Regime.RANGE, tags=frozenset({Regime.HIGH_VOLATILITY}))
        weights = FAVOURED_FAMILY_WEIGHT
        self.assertEqual(family_regime_weight("mean_reversion", state, weights), 1.5)
        self.assertEqual(family_regime_weight("reversal", state, weights), 1.5)
        self.assertEqual(family_regime_weight("momentum", state, weights), 1.0)


class TestPerAssetClassPositionCaps(unittest.TestCase):
    """Phase 6: one asset class cannot fill the whole book."""

    def test_each_class_carries_its_own_seat_count(self) -> None:
        from tradermaximus.core.types import AssetClass
        from tradermaximus.risk.limits import limits_for
        self.assertEqual(limits_for(AssetClass.EQUITIES).max_positions, 10)
        self.assertEqual(limits_for(AssetClass.CRYPTO).max_positions, 3)
        self.assertEqual(limits_for(AssetClass.FOREX).max_positions, 4)

    def test_phase_6_risk_and_exposure_configuration(self) -> None:
        from tradermaximus.core.types import AssetClass
        from tradermaximus.risk.limits import HardLimits, limits_for
        crypto = limits_for(AssetClass.CRYPTO)
        equities = limits_for(AssetClass.EQUITIES)
        self.assertEqual(
            sorted(b.risk_per_trade for b in crypto.risk_bands), [0.0025, 0.0050, 0.0075]
        )
        self.assertEqual(
            sorted(b.risk_per_trade for b in equities.risk_bands), [0.005, 0.010, 0.015]
        )
        self.assertEqual(crypto.max_exposure_per_trade, 0.05)
        self.assertEqual(equities.max_exposure_per_trade, 0.10)
        limits = HardLimits()
        self.assertEqual(limits.max_portfolio_exposure, 0.50)
        self.assertEqual(limits.max_correlated_exposure, 0.20)

    def test_the_hard_cap_still_overrides_every_asset_band(self) -> None:
        """Phase 6 asks equities for 0.5-1.5%; section 8.1 caps at 0.5%.

        The band is not raised to meet the request and the cap is not lowered
        to meet the band: the cap wins, so the effective equities risk is a
        flat 0.5% however quiet the market is. Stated here so the gap between
        what the configuration says and what the system does is visible.
        """
        from tradermaximus.core.types import AssetClass, Instrument
        from tradermaximus.risk.limits import HardLimits
        from tradermaximus.risk.sizing import PositionSizingEngine
        instrument = Instrument("X", AssetClass.EQUITIES, "NASDAQ", "Tech", 5.0, 0.01, 1.0)
        decision = PositionSizingEngine().size(
            instrument=instrument, equity=100_000.0, entry_price=50.0,
            stop_distance=5.0, volatility=0.10, apply_volatility_scaling=False,
        )
        self.assertLessEqual(
            decision.quantity * 5.0 / 100_000.0, HardLimits().max_risk_per_trade + 1e-9
        )


class TestDriftGate(unittest.TestCase):
    """Phase 7: a strategy the monitor disabled is not a candidate."""

    def test_the_strategy_engine_skips_disabled_strategies(self) -> None:
        from tradermaximus.research.drift import StrategyMonitor
        from tradermaximus.strategy.engine import StrategyEngine
        from tradermaximus.strategy.library import ALL_STRATEGIES

        monitor = StrategyMonitor()
        monitor.disabled.add("STRATEGY_001")
        engine = StrategyEngine(is_enabled=monitor.is_enabled)
        self.assertFalse(engine.is_enabled("STRATEGY_001"))
        self.assertTrue(engine.is_enabled("STRATEGY_004"))
        # The library itself is untouched; only selection is gated.
        self.assertIn("STRATEGY_001", {s.id for s in ALL_STRATEGIES})

    def test_drift_thresholds_are_the_ones_asked_for(self) -> None:
        from tradermaximus.research.drift import DriftThresholds
        thresholds = DriftThresholds()
        self.assertEqual(thresholds.sharpe_change, 0.30)
        self.assertEqual(thresholds.win_rate_change, 0.20)
        self.assertEqual(thresholds.window_trades, 30)

    def test_the_orchestrator_gates_selection_on_the_monitor(self) -> None:
        """Wiring test: the monitor must actually reach the strategy engine."""
        import inspect
        from tradermaximus import orchestrator
        source = inspect.getsource(orchestrator)
        self.assertIn("is_enabled=self.monitor.is_enabled", source)


class TestDashboard(unittest.TestCase):
    """Phase 7: the daily view. Reads state, never changes it."""

    def _book(self):
        from tradermaximus.core.types import AssetClass
        from tradermaximus.portfolio.book import PositionBook
        book = PositionBook(100_000)
        book.open_position(
            symbol="AAA", asset_class=AssetClass.EQUITIES, side=Side.LONG,
            quantity=100, entry_price=50.0, timestamp=NOW, strategy_id="STRATEGY_001",
            stop_price=47.0,
        )
        book.open_position(
            symbol="BBB", asset_class=AssetClass.FOREX, side=Side.SHORT,
            quantity=1000, entry_price=1.10, timestamp=NOW, strategy_id="STRATEGY_011",
            stop_price=1.15,
        )
        book.update_prices({"AAA": 55.0, "BBB": 1.05})
        book.mark_equity(NOW)
        return book

    def test_it_reports_both_sides_correctly(self) -> None:
        from tradermaximus.observability.dashboard import build_dashboard
        board = build_dashboard(self._book(), now=NOW)
        by_symbol = {p.symbol: p for p in board.positions}
        self.assertAlmostEqual(by_symbol["AAA"].unrealised, 500.0, places=6)
        self.assertAlmostEqual(by_symbol["BBB"].unrealised, 50.0, places=6)
        self.assertGreater(by_symbol["AAA"].distance_to_stop, 0)
        self.assertGreater(by_symbol["BBB"].distance_to_stop, 0)

    def test_gross_exposure_counts_a_short_as_exposure(self) -> None:
        """Net would net the two off; gross is what the book is carrying."""
        from tradermaximus.observability.dashboard import build_dashboard
        board = build_dashboard(self._book(), now=NOW)
        self.assertGreater(board.gross_exposure, abs(board.net_exposure))

    def test_it_names_the_strategies_drift_has_disabled(self) -> None:
        from tradermaximus.observability.dashboard import build_dashboard
        from tradermaximus.research.drift import StrategyMonitor
        monitor = StrategyMonitor()
        monitor.disabled.add("STRATEGY_001")
        monitor.size_multipliers["STRATEGY_011"] = 0.5
        board = build_dashboard(
            self._book(), now=NOW, trades=_trades([5.0, -3.0] * 6), monitor=monitor
        )
        rendered = board.render()
        self.assertIn("DISABLED", rendered)
        self.assertIn("STRATEGY_001", board.disabled_strategies)

    def test_it_renders_with_an_empty_book(self) -> None:
        from tradermaximus.observability.dashboard import build_dashboard
        from tradermaximus.portfolio.book import PositionBook
        board = build_dashboard(PositionBook(100_000), now=NOW)
        rendered = board.render()
        self.assertIn("none", rendered)
        self.assertEqual(board.gross_exposure, 0.0)

    def test_it_does_not_mutate_the_book(self) -> None:
        from tradermaximus.observability.dashboard import build_dashboard
        book = self._book()
        before = (book.equity, book.cash, len(book.positions))
        build_dashboard(book, now=NOW).render()
        self.assertEqual((book.equity, book.cash, len(book.positions)), before)
