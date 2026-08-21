"""Orchestrator workflow, readiness gating and safety guards."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tradermaximus.core.config import SystemConfig
from tradermaximus.core.types import AssetClass, Instrument, Timeframe
from tradermaximus.data.providers import DEFAULT_INSTRUMENTS, ProviderChain, SyntheticProvider
from tradermaximus.execution.brokers.paper import MarketState, PaperBroker, PaperFillModel
from tradermaximus.mcp.registry import ToolRegistry
from tradermaximus.news.events import (
    EventCategory,
    EventEngine,
    EventImpact,
    MarketEvent,
    Posture,
)
from tradermaximus.observability.journal import Journal
from tradermaximus.orchestrator import TraderMaximus
from tradermaximus.portfolio.book import PositionBook
from tradermaximus.regime.engine import MarketContext
from tradermaximus.risk.kill_switch import KillTrigger

NOW = datetime(2026, 8, 21, 13, 0, tzinfo=timezone.utc)
CONTEXT = MarketContext(breadth_index=0.6, new_highs_ratio=0.7, correlation_change=0.1)


def build(*, paper_mode: bool = True, broker=None, registry=None) -> TraderMaximus:
    journal = Journal()
    instruments = [DEFAULT_INSTRUMENTS[s] for s in ("AAPL", "MSFT", "NVDA", "XLE")]
    benchmark = Instrument("SPY", AssetClass.EQUITIES, "NYSE", "Index", 0.01, 1.0, 1.0)
    chain = ProviderChain([SyntheticProvider(seed=3, annual_volatility=0.22)], journal=journal)

    if broker is None:
        broker = PaperBroker(
            starting_cash=100_000,
            model=PaperFillModel(partial_fill_probability=0.0),
            seed=11,
        )
    for instrument in instruments:
        series = chain.fetch(instrument, Timeframe.D1, 400).series
        broker.set_market(
            instrument.symbol,
            MarketState(price=series.last.close, bar_volume=series.average_volume(50), spread_bps=5.0),
        )

    return TraderMaximus(
        config=SystemConfig.defaults(),
        providers=chain,
        broker=broker,
        book=PositionBook(100_000, cash_reserve=0.30),
        journal=journal,
        registry=registry,
        instruments=instruments,
        benchmark=benchmark,
        paper_mode=paper_mode,
    )


class TestReadiness(unittest.TestCase):
    def test_paper_setup_with_direct_providers_is_ready(self) -> None:
        report = build().readiness_report()
        self.assertTrue(report["ready"], report["failing"])

    def test_mcp_requirement_applies_once_the_registry_is_used(self) -> None:
        """An unused registry must not report a healthy system as unready."""
        registry = ToolRegistry()
        registry.register("finnhub-mcp", lambda *a: {})
        report = build(registry=registry).readiness_report()
        self.assertFalse(report["ready"])
        self.assertIn("required_mcp_tools_available", report["failing"])

    def test_live_mode_on_synthetic_data_is_refused(self) -> None:
        class LiveBroker(PaperBroker):
            name = "live-venue"
            is_paper = False

        live = LiveBroker(starting_cash=100_000, seed=1)
        system = build(paper_mode=False, broker=live)
        report = system.readiness_report()
        self.assertFalse(report["ready"])
        self.assertIn("not_live_on_synthetic_data", report["failing"])

        pre = system.pre_market(now=NOW, market_context=CONTEXT)
        self.assertFalse(pre.ready)
        self.assertTrue(any("synthetic" in b for b in pre.blockers))


class TestWorkflow(unittest.TestCase):
    def test_full_session_runs_and_reconciles(self) -> None:
        system = build()
        pre = system.pre_market(now=NOW, market_context=CONTEXT)
        self.assertTrue(pre.ready, pre.blockers)
        self.assertIsNotNone(pre.regime)
        self.assertTrue(all(r.passed for r in pre.data_reports.values()))

        cycle = system.cycle(now=NOW, market_context=CONTEXT)
        self.assertFalse(cycle.halted)
        self.assertGreater(cycle.evaluated, 0)

        post = system.post_market(now=NOW + timedelta(hours=7))
        self.assertTrue(post.reconciliation.reconciled, post.reconciliation.summary())

    def test_kill_switch_halts_the_cycle(self) -> None:
        system = build()
        system.pre_market(now=NOW, market_context=CONTEXT)
        system.kill_switch.engage(KillTrigger.MANUAL, "operator halt")
        cycle = system.cycle(now=NOW, market_context=CONTEXT)
        self.assertTrue(cycle.halted)
        self.assertIn("kill switch", cycle.halt_reason)

    def test_kill_switch_blocks_pre_market(self) -> None:
        system = build()
        system.kill_switch.engage(KillTrigger.MANUAL, "operator halt")
        pre = system.pre_market(now=NOW, market_context=CONTEXT)
        self.assertFalse(pre.ready)
        self.assertTrue(any("kill switch" in b for b in pre.blockers))

    def test_event_posture_blocks_new_positions(self) -> None:
        system = build()
        system.pre_market(now=NOW, market_context=CONTEXT)
        system.events.add(
            MarketEvent(
                "FOMC rate decision",
                EventCategory.MONETARY_POLICY,
                scheduled_at=NOW + timedelta(minutes=20),
                impact=EventImpact.HIGH,
            )
        )
        cycle = system.cycle(now=NOW, market_context=CONTEXT)
        self.assertTrue(cycle.halted)
        self.assertIn("FOMC", cycle.halt_reason)

    def test_untradeable_regime_blocks_pre_market(self) -> None:
        system = build()
        pre = system.pre_market(
            now=NOW, market_context=MarketContext(spread_ratio=3.0, volume_ratio=0.3)
        )
        self.assertFalse(pre.ready)
        self.assertTrue(any("LIQUIDITY_STRESS" in b for b in pre.blockers))

    def test_journal_records_the_session(self) -> None:
        system = build()
        system.pre_market(now=NOW, market_context=CONTEXT)
        system.cycle(now=NOW, market_context=CONTEXT)
        system.post_market(now=NOW + timedelta(hours=7))
        counts = system.journal.counts()
        self.assertIn("data", counts)
        self.assertIn("system", counts)


class TestEventEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = EventEngine()

    def test_no_events_means_normal_posture(self) -> None:
        posture = self.engine.posture(now=NOW)
        self.assertIs(posture.posture, Posture.NORMAL)
        self.assertTrue(posture.allow_new_positions)

    def test_thirty_minutes_before_halves_size(self) -> None:
        self.engine.add(
            MarketEvent("FOMC", EventCategory.MONETARY_POLICY,
                        scheduled_at=NOW + timedelta(minutes=20), impact=EventImpact.HIGH)
        )
        posture = self.engine.posture(now=NOW)
        self.assertIs(posture.posture, Posture.REDUCED)
        self.assertEqual(posture.size_multiplier, 0.5)
        self.assertFalse(posture.allow_new_positions)

    def test_well_before_the_event_is_normal(self) -> None:
        self.engine.add(
            MarketEvent("FOMC", EventCategory.MONETARY_POLICY,
                        scheduled_at=NOW + timedelta(hours=6), impact=EventImpact.HIGH)
        )
        self.assertIs(self.engine.posture(now=NOW).posture, Posture.NORMAL)

    def test_aftermath_resumes_with_reduced_size(self) -> None:
        self.engine.add(
            MarketEvent("FOMC", EventCategory.MONETARY_POLICY,
                        scheduled_at=NOW, impact=EventImpact.HIGH)
        )
        posture = self.engine.posture(now=NOW + timedelta(minutes=30))
        self.assertIs(posture.posture, Posture.REDUCED)
        self.assertTrue(posture.allow_new_positions)

    def test_unscheduled_shock_goes_defensive(self) -> None:
        self.engine.detect_from_headlines([("Fed announces emergency rate cut", -0.9)])
        posture = self.engine.posture(now=datetime.now(timezone.utc), volatility_spike=0.45)
        self.assertIs(posture.posture, Posture.DEFENSIVE)
        self.assertEqual(posture.size_multiplier, 0.0)

    def test_weak_sentiment_is_ignored(self) -> None:
        detected = self.engine.detect_from_headlines([("Company announces minor partnership", 0.2)])
        self.assertEqual(detected, [])

    def test_headline_categorisation(self) -> None:
        detected = self.engine.detect_from_headlines(
            [("CPI inflation comes in hot", -0.85), ("SEC opens probe into exchange", -0.9)]
        )
        categories = {e.category for e in detected}
        self.assertIn(EventCategory.INFLATION, categories)
        self.assertIn(EventCategory.REGULATORY, categories)


class TestCli(unittest.TestCase):
    @staticmethod
    def run_cli(argv: list[str]) -> tuple[int, str]:
        """Run a CLI command, capturing stdout so tests stay readable."""
        import contextlib
        import io

        from tradermaximus.cli import main

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def test_limits_command_prints_resolved_limits(self) -> None:
        code, output = self.run_cli(["limits"])
        self.assertEqual(code, 0)
        self.assertIn("max_expected_shortfall_95", output)
        self.assertIn("RISK / builtin-default", output)

    def test_tools_command_lists_the_catalogue(self) -> None:
        code, output = self.run_cli(["tools"])
        self.assertEqual(code, 0)
        self.assertIn("polygon-mcp [REQUIRED]", output)

    def test_no_live_subcommand_exists(self) -> None:
        """Going live must be a configuration act, not a reachable flag."""
        from tradermaximus.cli import build_parser

        actions = build_parser()._subparsers._group_actions[0].choices  # type: ignore[union-attr]
        self.assertNotIn("live", actions)


if __name__ == "__main__":
    unittest.main()
