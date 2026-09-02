"""Execution lifecycle, broker fills, reconciliation and safety checks."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tradermaximus.core.errors import ExecutionError
from tradermaximus.core.types import (
    AssetClass,
    Instrument,
    Order,
    OrderSide,
    OrderType,
    ScoreBreakdown,
    Side,
    Signal,
)
from tradermaximus.execution.brokers.base import OrderAck
from tradermaximus.execution.brokers.paper import MarketState, PaperBroker, PaperFillModel
from tradermaximus.execution.engine import ExecutionEngine
from tradermaximus.execution.safety import ExecutionConditions, run_checklist
from tradermaximus.execution.states import (
    InvalidTransition,
    OrderLifecycle,
    OrderState,
    can_transition,
)
from tradermaximus.observability.journal import Journal
from tradermaximus.portfolio.book import PositionBook
from tradermaximus.portfolio.reconciliation import DiscrepancyType, ReconciliationEngine
from tradermaximus.risk.engine import RiskContext, RiskEngine

NOW = datetime(2024, 6, 3, tzinfo=timezone.utc)
AAPL = Instrument("AAPL", AssetClass.EQUITIES, "NASDAQ", "Technology", 0.01, 1.0, 1.0)

SIGNAL = Signal(
    symbol="AAPL",
    asset_class=AssetClass.EQUITIES,
    strategy_id="STRATEGY_001",
    side=Side.LONG,
    entry_price=100.0,
    timestamp=NOW,
    score=ScoreBreakdown({"total": 75.0}),
    structure_level=94.0,
    atr=2.5,
    regime="TREND_UP",
    regime_confidence=0.9,
    features={"strategy_type": "momentum", "adx_14": 30.0, "volatility_percentile": 0.4},
)


class TestStateMachine(unittest.TestCase):
    def test_happy_path(self) -> None:
        lifecycle = OrderLifecycle("O1")
        for state in (
            OrderState.VALIDATED, OrderState.RISK_APPROVED, OrderState.ORDER_CREATED,
            OrderState.ORDER_SUBMITTED, OrderState.BROKER_ACKNOWLEDGED, OrderState.FILLED,
            OrderState.PROTECTIVE_ORDER_ATTACHED, OrderState.POSITION_RECONCILED,
            OrderState.MONITORED, OrderState.EXITED, OrderState.RECONCILED, OrderState.JOURNALED,
        ):
            lifecycle.advance(state)
        self.assertIs(lifecycle.state, OrderState.JOURNALED)
        self.assertTrue(lifecycle.is_protected)

    def test_illegal_skip_is_refused(self) -> None:
        """Skipping straight to FILLED would bypass protection entirely."""
        lifecycle = OrderLifecycle("O2")
        lifecycle.advance(OrderState.VALIDATED)
        with self.assertRaises(InvalidTransition):
            lifecycle.advance(OrderState.FILLED)

    def test_protection_cannot_be_skipped(self) -> None:
        self.assertFalse(can_transition(OrderState.FILLED, OrderState.MONITORED))
        self.assertTrue(can_transition(OrderState.FILLED, OrderState.PROTECTIVE_ORDER_ATTACHED))

    def test_failure_states_are_reachable_from_live_states(self) -> None:
        self.assertTrue(can_transition(OrderState.ORDER_SUBMITTED, OrderState.REJECTED))
        self.assertTrue(can_transition(OrderState.BROKER_ACKNOWLEDGED, OrderState.TIMEOUT))


class TestPaperBroker(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = PaperBroker(
            starting_cash=100_000,
            model=PaperFillModel(partial_fill_probability=0.0),
            seed=5,
        )
        self.broker.set_market("AAPL", MarketState(price=100.0, bar_volume=1_000_000, spread_bps=6.0))

    def test_buy_crosses_the_spread(self) -> None:
        ack = self.broker.submit(Order("O1", "AAPL", OrderSide.BUY, 100, OrderType.MARKET))
        self.assertTrue(ack.accepted)
        self.assertGreater(ack.fills[0].price, 100.0)

    def test_impact_grows_with_participation(self) -> None:
        """Slippage rises with the share of the bar consumed.

        Priced at 1.00 so a large *share count* stays inside buying power —
        the point under test is participation, not funding.
        """
        self.broker.set_market("PENNY", MarketState(price=1.0, bar_volume=1_000_000, spread_bps=6.0))
        small = self.broker.submit(Order("O1", "PENNY", OrderSide.BUY, 100, OrderType.MARKET))
        large = self.broker.submit(Order("O2", "PENNY", OrderSide.BUY, 90_000, OrderType.MARKET))
        self.assertTrue(small.fills and large.fills, "both orders should fill")
        self.assertGreater(large.fills[0].slippage_bps, small.fills[0].slippage_bps)

    def test_participation_cap_produces_a_partial_fill(self) -> None:
        """An order beyond the bar's absorbable volume fills only partly."""
        self.broker.set_market("PENNY", MarketState(price=1.0, bar_volume=10_000, spread_bps=6.0))
        ack = self.broker.submit(Order("O1", "PENNY", OrderSide.BUY, 50_000, OrderType.MARKET))
        self.assertTrue(ack.accepted)
        self.assertGreater(ack.remaining, 0)
        self.assertLess(ack.filled_quantity, 50_000)

    def test_unfundable_order_is_refused(self) -> None:
        """500k shares at 100 needs $50m against $100k of cash."""
        ack = self.broker.submit(Order("O1", "AAPL", OrderSide.BUY, 500_000, OrderType.MARKET))
        self.assertFalse(ack.accepted)
        self.assertIn("buying power", ack.message)

    def test_stop_order_rests_until_triggered(self) -> None:
        self.broker.submit(Order("B1", "AAPL", OrderSide.BUY, 100, OrderType.MARKET))
        self.broker.submit(Order("S1", "AAPL", OrderSide.SELL, 100, OrderType.STOP, stop_price=94.0))
        self.assertEqual(len(self.broker.trigger_working_orders("AAPL", 96.0)), 0)
        fills = self.broker.trigger_working_orders("AAPL", 93.0)
        self.assertEqual(len(fills), 1)

    def test_closing_sell_is_always_fundable(self) -> None:
        self.broker.submit(Order("B1", "AAPL", OrderSide.BUY, 500, OrderType.MARKET))
        ack = self.broker.submit(Order("S1", "AAPL", OrderSide.SELL, 500, OrderType.MARKET))
        self.assertTrue(ack.accepted)


class TestShortEquityAccounting(unittest.TestCase):
    """Equity must not move when a position opens, long or short.

    Regression: the equity property added a short's entry notional on top of
    the cash its proceeds had already credited, so opening a short instantly
    inflated equity by the position's full notional. The defect was invisible
    while the strategy library was long-only. It matters because every risk
    quantity is a fraction of equity: an over-stated equity means an
    over-sized risk budget, a per-trade exposure cap that lets more through
    than it should, an understated drawdown, and a distorted return series
    feeding VaR and ES.
    """

    def _book_with(self, side: Side) -> PositionBook:
        book = PositionBook(100_000)
        book.open_position(
            symbol="X",
            asset_class=AssetClass.EQUITIES,
            side=side,
            quantity=100,
            entry_price=50.0,
            timestamp=NOW,
            strategy_id="S",
            stop_price=52.0 if side is Side.SHORT else 48.0,
        )
        return book

    def test_opening_a_position_does_not_change_equity(self) -> None:
        for side in (Side.LONG, Side.SHORT):
            with self.subTest(side=side):
                self.assertAlmostEqual(self._book_with(side).equity, 100_000.0, places=6)

    def test_a_short_gains_when_price_falls_and_loses_when_it_rises(self) -> None:
        book = self._book_with(Side.SHORT)
        book.update_prices({"X": 45.0})
        self.assertAlmostEqual(book.equity, 100_500.0, places=6)
        book.update_prices({"X": 55.0})
        self.assertAlmostEqual(book.equity, 99_500.0, places=6)

    def test_a_long_and_a_short_of_equal_size_offset(self) -> None:
        """A hedged pair marks flat: neither leg may contribute its notional."""
        book = PositionBook(100_000)
        for symbol, side in (("L", Side.LONG), ("S", Side.SHORT)):
            book.open_position(
                symbol=symbol, asset_class=AssetClass.EQUITIES, side=side,
                quantity=100, entry_price=50.0, timestamp=NOW, strategy_id="S",
                stop_price=48.0 if side is Side.LONG else 52.0,
            )
        book.update_prices({"L": 60.0, "S": 60.0})
        self.assertAlmostEqual(book.equity, 100_000.0, places=6)

    def test_realised_equity_matches_the_mark_it_closed_at(self) -> None:
        """Closing a short must not move equity beyond the last mark."""
        book = self._book_with(Side.SHORT)
        book.update_prices({"X": 45.0})
        marked = book.equity
        book.reduce_position("X", 100, 45.0, timestamp=NOW, reason="target")
        self.assertAlmostEqual(book.equity, marked, places=6)
        self.assertAlmostEqual(book.equity, 100_500.0, places=6)


class TestExecutionEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.journal = Journal()
        self.book = PositionBook(100_000)
        self.broker = PaperBroker(
            starting_cash=100_000,
            model=PaperFillModel(partial_fill_probability=0.0),
            seed=5,
        )
        self.broker.set_market("AAPL", MarketState(price=100.0, bar_volume=2_000_000, spread_bps=6.0))
        self.engine = ExecutionEngine(self.broker, self.book, journal=self.journal, backoff_seconds=0.0)
        self.verdict = RiskEngine(journal=self.journal).authorize(
            SIGNAL,
            AAPL,
            RiskContext(
                equity=100_000.0,
                snapshot=self.book.snapshot(NOW),
                average_daily_volume=2_000_000.0,
                volatility=0.25,
                minimum_score=60.0,
                spread_bps=6.0,
                estimated_slippage_bps=4.0,
            ),
        )
        self.conditions = ExecutionConditions(
            quote=self.broker.market("AAPL").quote,
            market_open=True,
            data_age_seconds=1.0,
            average_daily_volume=2_000_000.0,
            buying_power=100_000.0,
            risk_approved=True,
        )

    def test_successful_execution_attaches_protection(self) -> None:
        result = self.engine.execute(SIGNAL, AAPL, self.verdict, self.conditions)
        self.assertTrue(result.success)
        self.assertIs(result.state, OrderState.MONITORED)
        self.assertIsNotNone(result.stop_order_id)
        position = self.book.get("AAPL")
        self.assertIsNotNone(position)
        self.assertIsNotNone(position.stop_price)
        self.assertTrue(position.targets)

    def test_safety_checklist_blocks_bad_conditions(self) -> None:
        bad = ExecutionConditions(
            quote=self.broker.market("AAPL").quote,
            market_open=False,
            data_age_seconds=900.0,
            average_daily_volume=2_000_000.0,
            buying_power=100_000.0,
            risk_approved=True,
        )
        result = self.engine.execute(SIGNAL, AAPL, self.verdict, bad)
        self.assertFalse(result.success)
        self.assertIs(result.state, OrderState.REJECTED)
        names = {c.name for c in result.safety.failures}
        self.assertIn("market_open", names)
        self.assertIn("data_fresh", names)

    def test_network_failure_retries_then_gives_up(self) -> None:
        calls = {"n": 0}

        def unreachable(order):
            calls["n"] += 1
            raise ExecutionError("network unreachable")

        self.broker.submit = unreachable  # type: ignore[method-assign]
        result = self.engine.execute(SIGNAL, AAPL, self.verdict, self.conditions)
        self.assertFalse(result.success)
        self.assertEqual(calls["n"], 4)  # initial attempt plus 3 retries
        self.assertIs(result.state, OrderState.NETWORK_ERROR)

    def test_venue_rejection_is_not_retried(self) -> None:
        """A rejection is a decision; resubmitting risks a duplicate order."""
        calls = {"n": 0}

        def rejecting(order):
            calls["n"] += 1
            return OrderAck(order.order_id, False, message="rejected by venue")

        self.broker.submit = rejecting  # type: ignore[method-assign]
        result = self.engine.execute(SIGNAL, AAPL, self.verdict, self.conditions)
        self.assertFalse(result.success)
        self.assertEqual(calls["n"], 1)

    def test_failed_protection_flattens_the_position(self) -> None:
        """An unprotected position is worse than the cost of exiting it."""
        original = self.broker.submit

        def stop_fails(order):
            if order.purpose == "stop":
                return OrderAck(order.order_id, False, message="venue rejected stop")
            return original(order)

        self.broker.submit = stop_fails  # type: ignore[method-assign]
        result = self.engine.execute(SIGNAL, AAPL, self.verdict, self.conditions)
        self.assertFalse(result.success)
        self.assertIs(result.state, OrderState.PROTECTIVE_ORDER_FAILURE)
        self.assertNotIn("AAPL", self.book)
        self.assertGreaterEqual(len(self.journal.events(severity="critical")), 1)

    def test_exit_records_a_closed_trade(self) -> None:
        self.engine.execute(SIGNAL, AAPL, self.verdict, self.conditions)
        self.broker.update_price("AAPL", 110.0)
        result = self.engine.exit_position("AAPL", self.book.get("AAPL").quantity, reason="target")
        self.assertTrue(result.success)
        self.assertEqual(len(self.book.closed_trades), 1)
        self.assertGreater(self.book.closed_trades[0].pnl, 0)


class TestReconciliation(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = ReconciliationEngine()
        self.book = PositionBook(100_000)

    def open(self, symbol: str, quantity: float, price: float) -> None:
        self.book.open_position(
            symbol=symbol, asset_class=AssetClass.EQUITIES, side=Side.LONG, quantity=quantity,
            entry_price=price, strategy_id="S", stop_price=price * 0.94, timestamp=NOW,
        )
        self.book.update_price(symbol, price)

    def test_matching_state_reconciles(self) -> None:
        from tradermaximus.execution.brokers.base import BrokerPosition

        self.open("AAPL", 100, 100.0)
        report = self.engine.reconcile(
            self.book, [BrokerPosition("AAPL", 100.0, 100.0, 10_000.0)], self.book.equity
        )
        self.assertTrue(report.reconciled)

    def test_unknown_broker_position_is_critical(self) -> None:
        from tradermaximus.execution.brokers.base import BrokerPosition

        report = self.engine.reconcile(
            self.book, [BrokerPosition("GME", 500.0, 20.0, 10_000.0)], self.book.equity
        )
        self.assertFalse(report.reconciled)
        self.assertTrue(report.has_critical)
        self.assertEqual(self.engine.unauthorized_symbols(report), ["GME"])

    def test_quantity_mismatch_is_critical(self) -> None:
        from tradermaximus.execution.brokers.base import BrokerPosition

        self.open("AAPL", 100, 100.0)
        report = self.engine.reconcile(
            self.book, [BrokerPosition("AAPL", 50.0, 100.0, 5_000.0)], self.book.equity
        )
        self.assertTrue(report.has_critical)
        self.assertIn(
            DiscrepancyType.QUANTITY_MISMATCH, [d.type for d in report.discrepancies]
        )
        self.assertGreater(report.max_position_deviation, 0)

    def test_missing_at_broker_is_detected(self) -> None:
        self.open("AAPL", 100, 100.0)
        report = self.engine.reconcile(self.book, [], self.book.equity)
        self.assertIn(
            DiscrepancyType.MISSING_AT_BROKER, [d.type for d in report.discrepancies]
        )


class TestSafetyChecklist(unittest.TestCase):
    def test_all_checks_pass_under_good_conditions(self) -> None:
        from tradermaximus.core.types import Quote

        order = Order("O1", "AAPL", OrderSide.BUY, 100, OrderType.MARKET)
        conditions = ExecutionConditions(
            quote=Quote(NOW, 99.97, 100.03),
            market_open=True,
            data_age_seconds=1.0,
            average_daily_volume=1_000_000.0,
            buying_power=50_000.0,
            risk_approved=True,
        )
        report = run_checklist(order, AAPL, conditions, stop_price=94.0)
        self.assertTrue(report.passed, report.reason())

    def test_missing_risk_approval_fails(self) -> None:
        from tradermaximus.core.types import Quote

        order = Order("O1", "AAPL", OrderSide.BUY, 100, OrderType.MARKET)
        conditions = ExecutionConditions(
            quote=Quote(NOW, 99.97, 100.03), data_age_seconds=1.0,
            average_daily_volume=1_000_000.0, buying_power=50_000.0, risk_approved=False,
        )
        report = run_checklist(order, AAPL, conditions)
        self.assertFalse(report.passed)
        self.assertIn("risk_engine_approved", {c.name for c in report.failures})

    def test_unknown_data_age_fails_rather_than_passing(self) -> None:
        from tradermaximus.core.types import Quote

        order = Order("O1", "AAPL", OrderSide.BUY, 100, OrderType.MARKET)
        conditions = ExecutionConditions(
            quote=Quote(NOW, 99.97, 100.03), data_age_seconds=None,
            average_daily_volume=1_000_000.0, risk_approved=True,
        )
        report = run_checklist(order, AAPL, conditions)
        self.assertIn("data_fresh", {c.name for c in report.failures})


if __name__ == "__main__":
    unittest.main()
