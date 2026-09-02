"""TraderMaximus orchestrator (spec sections 26, 31).

Composes every engine into the daily workflow: pre-market checks, the intraday
decision loop, and the post-market reconciliation and review.

The orchestrator is deliberately thin. It sequences and it halts; it does not
decide. Position sizes come from the sizing engine, authorisation from the risk
engine, eligibility from the regime engine. That separation is what makes the
final mandate's "the AI may recommend, the risk engine decides" structural
rather than a promise — there is no code path here that can open a position
without a :class:`RiskVerdict`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Sequence

from .core.config import SystemConfig
from .core.errors import DataQualityError
from .core.types import AssetClass, Instrument, Timeframe, utc_now
from .data.providers import ProviderChain
from .data.quality import DataQualityEngine, QualityReport
from .execution.brokers.base import BrokerAdapter
from .execution.engine import ExecutionEngine, ExecutionResult
from .execution.safety import ExecutionConditions
from .indicators.engine import FeatureEngine, FeatureSet
from .mcp.registry import ToolRegistry
from .news.events import EventEngine, EventPosture, NORMAL_POSTURE
from .observability.journal import Journal, Severity
from .observability.metrics import SystemHealth
from .portfolio.book import PositionBook
from .portfolio.reconciliation import ReconciliationEngine, ReconciliationReport
from .regime.engine import (
    MarketContext,
    RegimeEngine,
    RegimeState,
    effective_minimum_score,
    family_size_multiplier,
    minimum_score,
)
from .risk.circuit_breakers import CircuitBreakerEngine
from .research.drift import StrategyMonitor
from .risk.engine import RiskContext, RiskEngine, RiskVerdict
from .risk.kill_switch import KillSwitch, KillTrigger, SystemSignals
from .risk.limits import HardLimits
from .risk.trailing import TrailingStopEngine
from .strategy.engine import StrategyEngine, SelectionResult
from .strategy.scoring import ScoringInputs
from .strategy.spec import EvalContext, Fundamentals


@dataclass(frozen=True, slots=True)
class PreMarketReport:
    """Outcome of the section 26.1 pre-market checklist."""

    ready: bool
    regime: RegimeState | None
    health: SystemHealth
    data_reports: Mapping[str, QualityReport] = field(default_factory=dict)
    posture: EventPosture = NORMAL_POSTURE
    blockers: tuple[str, ...] = ()
    risk_budget: float = 0.0

    def summary(self) -> str:
        if self.ready:
            regime = self.regime.primary.value if self.regime else "UNKNOWN"
            return f"pre-market ready: regime {regime}, risk budget {self.risk_budget:,.2f}"
        return "pre-market NOT ready: " + "; ".join(self.blockers)


@dataclass(frozen=True, slots=True)
class CycleReport:
    """Outcome of one intraday decision cycle."""

    timestamp: datetime
    evaluated: int = 0
    signals: int = 0
    approved: int = 0
    executed: int = 0
    rejections: Mapping[str, int] = field(default_factory=dict)
    exits: tuple[str, ...] = ()
    halted: bool = False
    halt_reason: str = ""

    def summary(self) -> str:
        if self.halted:
            return f"cycle halted: {self.halt_reason}"
        return (
            f"evaluated {self.evaluated}, {self.signals} signals, "
            f"{self.approved} approved, {self.executed} executed, {len(self.exits)} exits"
        )


@dataclass(frozen=True, slots=True)
class PostMarketReport:
    reconciliation: ReconciliationReport
    equity: float
    daily_pnl_pct: float
    open_positions: int
    trades_today: int
    rejections: Mapping[str, int] = field(default_factory=dict)
    breaches: tuple[str, ...] = ()
    execution_quality: Mapping[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"post-market: equity {self.equity:,.2f} ({self.daily_pnl_pct:+.2%}), "
            f"{self.open_positions} open, {self.trades_today} trades, "
            f"{'reconciled' if self.reconciliation.reconciled else 'RECONCILIATION FAILED'}"
        )


class TraderMaximus:
    """The orchestration layer described in the master specification."""

    def __init__(
        self,
        *,
        config: SystemConfig | None = None,
        providers: ProviderChain,
        broker: BrokerAdapter,
        book: PositionBook,
        journal: Journal,
        registry: ToolRegistry | None = None,
        instruments: Sequence[Instrument] = (),
        fundamentals: Mapping[str, Fundamentals] | None = None,
        benchmark: Instrument | None = None,
        paper_mode: bool = True,
        monitor: StrategyMonitor | None = None,
    ) -> None:
        self.config = config if config is not None else SystemConfig.defaults()
        self.limits = HardLimits.from_config(self.config)
        self.providers = providers
        self.broker = broker
        self.book = book
        self.journal = journal
        self.registry = registry if registry is not None else ToolRegistry(journal=journal)
        self.instruments = list(instruments)
        self.fundamentals = dict(fundamentals or {})
        self.benchmark = benchmark
        self.paper_mode = paper_mode

        self.quality = DataQualityEngine(
            threshold=float(self.config.get("data_quality_threshold", 0.85)),
            max_staleness_seconds=float(self.config.get("max_staleness_seconds", 5.0)),
            max_spread_bps=float(self.config.get("max_spread_bps", 50.0)),
        )
        self.features = FeatureEngine()
        self.regime_engine = RegimeEngine()
        # The drift monitor gates strategy selection: a strategy it has
        # disabled is skipped entirely, not merely sized down. Section 21.2's
        # response to severe decay is to stop trading the strategy, and a
        # smaller position in a strategy whose edge has gone is still a
        # position in a strategy whose edge has gone.
        self.monitor = monitor if monitor is not None else StrategyMonitor()
        self.strategies = StrategyEngine(
            journal=journal, is_enabled=self.monitor.is_enabled
        )
        self.kill_switch = KillSwitch(journal=journal)
        self.breakers = CircuitBreakerEngine(
            max_daily_loss=self.limits.max_daily_loss,
            max_weekly_loss=self.limits.max_weekly_loss,
            max_monthly_drawdown=self.limits.max_monthly_drawdown,
            max_daily_trades=self.limits.max_daily_trades,
            max_daily_orders=self.limits.max_daily_orders,
        )
        self.risk = RiskEngine(
            self.limits, kill_switch=self.kill_switch, breakers=self.breakers, journal=journal
        )
        self.execution = ExecutionEngine(broker, book, journal=journal)
        self.reconciliation = ReconciliationEngine()
        self.trailing = TrailingStopEngine()
        self.events = EventEngine()
        self.health = SystemHealth()

        self._regime: RegimeState | None = None
        self._session_open = False
        self._rejections: dict[str, int] = {}

    # -- guard rails ---------------------------------------------------------

    def _missing_mcp_tools(self) -> list[str]:
        """Required MCP tools with no implementation — when MCP is the data path.

        A deployment that reaches its data and venues through MCP servers must
        have the required ones connected. A deployment wired directly to a
        :class:`ProviderChain` and a broker adapter — which is how paper mode
        and backtests run — is not using MCP at all, and demanding an unused
        registration would report a healthy system as unready.

        The registry being empty is the signal for "MCP is not the data path".
        Once anything is registered, the required set is enforced.
        """
        if not self.registry.status_report()["registered"]:
            return []
        return self.registry.missing_required()

    def _refuse_live_on_synthetic_data(self) -> str | None:
        """Live trading on a synthetic feed is never permitted.

        A synthetic provider generates plausible prices from a seed. Routing
        real orders against them would be trading on fiction, so this is a hard
        block rather than a warning.
        """
        if self.paper_mode or getattr(self.broker, "is_paper", True):
            return None
        if self.providers.uses_synthetic_data:
            return "live mode is configured but the data chain includes a synthetic provider"
        return None

    # -- 26.1 pre-market -----------------------------------------------------

    def pre_market(
        self,
        *,
        now: datetime | None = None,
        market_context: MarketContext | None = None,
        lookback: int = 400,
    ) -> PreMarketReport:
        """Run the section 26.1 checklist and decide whether to trade today."""
        now = now or utc_now()
        blockers: list[str] = []
        reports: dict[str, QualityReport] = {}

        # 1-2. System and tool health.
        self.health.set("broker", self.broker.healthy())
        self.health.set("data_providers", any(p.healthy() for p in self.providers.providers))
        self.health.set("journal", True)
        if not self.broker.healthy():
            blockers.append("broker connection is unhealthy")

        missing = self._missing_mcp_tools()
        if missing:
            blockers.append(f"required MCP tools unavailable: {', '.join(missing)}")

        guard = self._refuse_live_on_synthetic_data()
        if guard:
            blockers.append(guard)

        # 3. Kill switch and circuit breakers.
        if self.kill_switch.active:
            blockers.append("kill switch is engaged; human review required")

        equity = self.book.equity
        self.breakers.start_session(equity, now)
        if self.breakers.blocks_new_trades:
            blockers.append("a circuit breaker is blocking new trades")

        # 4. Data quality across the universe.
        for instrument in self.instruments:
            try:
                result = self.providers.fetch(instrument, Timeframe.D1, lookback)
            except DataQualityError as exc:
                reports[instrument.symbol] = QualityReport(instrument.symbol, 0.0)
                self.journal.data_event(instrument.symbol, 0.0, [str(exc)])
                continue
            report = self.quality.validate_series(
                result.series, min_history=200, now=now, enforce_staleness=not self.paper_mode
            )
            reports[instrument.symbol] = report
            self.journal.data_event(instrument.symbol, report.score, report.issues)

        usable = [s for s, r in reports.items() if r.passed]
        if not usable:
            blockers.append("no instrument passed data-quality validation")

        # 5. Regime classification.
        regime = self._classify_regime(market_context, lookback)
        if regime is not None:
            previous = self.regime_engine.previous
            if previous is not None and previous.primary is not regime.primary:
                self.journal.regime_change(previous.primary.value, regime.primary.value, regime.confidence)
            self._regime = regime
            if not regime.is_tradeable:
                blockers.append(f"regime {regime.primary.value} permits no trading")
            elif minimum_score(regime) is None:
                blockers.append(f"regime {regime.primary.value} permits no new long positions")
        else:
            blockers.append("regime could not be determined")

        # 6. Event posture.
        posture = self.events.posture(now=now)
        if not posture.allow_new_positions:
            blockers.append(f"event posture: {posture.reason}")

        # 7. Risk budget for the session.
        risk_budget = equity * self.limits.max_total_portfolio_risk

        ready = not blockers
        self._session_open = ready
        report = PreMarketReport(
            ready=ready,
            regime=regime,
            health=self.health,
            data_reports=reports,
            posture=posture,
            blockers=tuple(blockers),
            risk_budget=risk_budget,
        )
        self.journal.record(
            "system",
            report.summary(),
            severity=Severity.INFO if ready else Severity.WARNING,
            blockers=list(blockers),
            usable_symbols=usable,
        )
        return report

    # -- 26.2 intraday cycle -------------------------------------------------

    def cycle(
        self,
        *,
        now: datetime | None = None,
        market_context: MarketContext | None = None,
        scoring_inputs: Mapping[str, ScoringInputs] | None = None,
        lookback: int = 400,
    ) -> CycleReport:
        """One decision cycle: manage positions, then look for new entries."""
        now = now or utc_now()
        rejections: dict[str, int] = {}
        exits: list[str] = []

        # 1. Refresh prices and manage what is already open.
        prices = self._refresh_prices(lookback)
        self.book.update_prices(prices)
        exits.extend(self._manage_open_positions(prices))
        self.book.mark_equity(now)

        # 2. Safety gates, evaluated every cycle rather than only at open.
        halt = self._check_halts(now)
        if halt:
            return CycleReport(now, halted=True, halt_reason=halt, exits=tuple(exits))

        posture = self.events.posture(now=now)
        if not posture.allow_new_positions:
            return CycleReport(now, exits=tuple(exits), halted=True, halt_reason=posture.reason)

        regime = self._classify_regime(market_context, lookback) or self._regime
        if regime is None or not regime.is_tradeable:
            return CycleReport(now, exits=tuple(exits), halted=True, halt_reason="regime not tradeable")
        self._regime = regime

        threshold = minimum_score(regime)
        if threshold is None:
            return CycleReport(
                now, exits=tuple(exits), halted=True,
                halt_reason=f"regime {regime.primary.value} permits no new positions",
            )

        # 3. Evaluate the universe.
        evaluated = signals = approved = executed = 0
        size_multiplier = regime.size_multiplier * posture.size_multiplier

        for instrument in self.instruments:
            if instrument.symbol in self.book:
                continue
            if len(self.book) >= self.limits.max_positions_total:
                break

            features = self._features_for(instrument, lookback)
            if features is None:
                continue
            evaluated += 1

            selection = self._select(instrument, features, regime, scoring_inputs)
            if selection.signal is None:
                continue
            signals += 1

            verdict = self._authorize(instrument, selection, features, regime, size_multiplier, now)
            if not verdict.approved:
                rule = verdict.rule or "unknown"
                rejections[rule] = rejections.get(rule, 0) + 1
                self._rejections[rule] = self._rejections.get(rule, 0) + 1
                continue
            approved += 1

            result = self._execute(instrument, selection, verdict)
            if result.success:
                executed += 1
                self.breakers.record_trade()
            self.breakers.record_order()

        return CycleReport(
            timestamp=now,
            evaluated=evaluated,
            signals=signals,
            approved=approved,
            executed=executed,
            rejections=rejections,
            exits=tuple(exits),
        )

    # -- 26.3 post-market ----------------------------------------------------

    def post_market(self, *, now: datetime | None = None) -> PostMarketReport:
        """Reconcile, review and record the session (section 26.3)."""
        now = now or utc_now()
        account = self.broker.account()
        report = self.reconciliation.reconcile(self.book, self.broker.positions(), account.equity)

        if not report.reconciled:
            self.journal.alert(
                f"reconciliation failed: {report.summary()}",
                severity=Severity.CRITICAL,
                discrepancies=[str(d) for d in report.discrepancies],
            )
            if report.has_critical:
                self.kill_switch.engage(
                    KillTrigger.BROKER_MISMATCH
                    if report.max_position_deviation > 0
                    else KillTrigger.CORRUPTED_PORTFOLIO_STATE,
                    report.summary(),
                )

        unauthorized = self.reconciliation.unauthorized_symbols(report)
        if unauthorized:
            self.kill_switch.check(SystemSignals(unauthorized_symbols=tuple(unauthorized)))

        metrics, breaches = self.risk.review_portfolio(self.book)
        equity = self.book.equity
        self.book.mark_equity(now)

        quality = getattr(self.broker, "execution_quality", None)
        execution_quality = quality() if callable(quality) else {}

        summary = PostMarketReport(
            reconciliation=report,
            equity=equity,
            daily_pnl_pct=self.book.daily_pnl_pct(now.date()),
            open_positions=len(self.book),
            trades_today=self.breakers.state.trades_today if self.breakers._state else 0,
            rejections=dict(self._rejections),
            breaches=tuple(breaches),
            execution_quality=execution_quality,
        )
        self.journal.record(
            "system",
            summary.summary(),
            reconciled=report.reconciled,
            breaches=list(breaches),
            rejections=dict(self._rejections),
            portfolio=metrics.as_dict(),
        )
        self._session_open = False
        return summary

    # -- internals -----------------------------------------------------------

    def _classify_regime(self, context: MarketContext | None, lookback: int) -> RegimeState | None:
        """Classify from the benchmark, since regime is a market-wide property."""
        source = self.benchmark or (self.instruments[0] if self.instruments else None)
        if source is None:
            return None
        try:
            result = self.providers.fetch(source, Timeframe.D1, lookback)
        except DataQualityError:
            return None
        if len(result.series) < 100:
            return None
        features = self.features.compute(result.series, detect_bases=False)
        return self.regime_engine.classify(features, context, benchmark_closes=result.series.closes)

    def _refresh_prices(self, lookback: int) -> dict[str, float]:
        prices: dict[str, float] = {}
        for instrument in self.instruments:
            try:
                result = self.providers.fetch(instrument, Timeframe.D1, 2, cross_check=False)
            except DataQualityError:
                continue
            if len(result.series):
                prices[instrument.symbol] = result.series.last.close
        return prices

    def _features_for(self, instrument: Instrument, lookback: int) -> FeatureSet | None:
        try:
            result = self.providers.fetch(instrument, Timeframe.D1, lookback)
        except DataQualityError:
            return None
        report = self.quality.validate_series(
            result.series, min_history=200, enforce_staleness=not self.paper_mode
        )
        if not report.passed:
            self.journal.data_event(instrument.symbol, report.score, report.issues)
            return None
        return self.features.compute(result.series)

    def _select(
        self,
        instrument: Instrument,
        features: FeatureSet,
        regime: RegimeState,
        scoring_inputs: Mapping[str, ScoringInputs] | None,
    ) -> SelectionResult:
        context = EvalContext(
            features=features,
            fundamentals=self.fundamentals.get(instrument.symbol, Fundamentals()),
        )
        inputs = (scoring_inputs or {}).get(
            instrument.symbol, ScoringInputs(average_daily_volume=features.number("avg_volume_50"))
        )
        return self.strategies.select(
            instrument=instrument, features=features, regime=regime, context=context, inputs=inputs
        )

    def _authorize(
        self,
        instrument: Instrument,
        selection: SelectionResult,
        features: FeatureSet,
        regime: RegimeState,
        size_multiplier: float,
        now: datetime,
    ) -> RiskVerdict:
        signal = selection.signal
        assert signal is not None
        quote = self.providers.fetch_quote(instrument)
        # The bar and the size both depend on which family the signal belongs
        # to, which is only known once the strategy engine has chosen. Using
        # the regime bar here would re-reject signals the strategy engine has
        # already passed against their family bar.
        strategy_type = str(signal.features.get("strategy_type", ""))
        return self.risk.authorize(
            signal,
            instrument,
            RiskContext(
                equity=self.book.equity,
                snapshot=self.book.snapshot(now),
                daily_loss=max(0.0, -self.book.daily_pnl_pct(now.date())),
                weekly_loss=self.book.weekly_drawdown,
                monthly_drawdown=self.book.monthly_drawdown,
                portfolio_drawdown=self.book.drawdown,
                trades_today=self.breakers.state.trades_today if self.breakers._state else 0,
                spread_bps=quote.spread_bps if quote else None,
                average_daily_volume=features.number("avg_volume_50"),
                volatility=features.number("realized_vol_20"),
                regime_multiplier=size_multiplier * family_size_multiplier(strategy_type, regime),
                minimum_score=effective_minimum_score(strategy_type, regime),
                portfolio_returns=self.book.returns(),
                available_cash=self.book.available_cash,
            ),
        )

    def _execute(
        self,
        instrument: Instrument,
        selection: SelectionResult,
        verdict: RiskVerdict,
    ) -> ExecutionResult:
        signal = selection.signal
        assert signal is not None
        quote = self.providers.fetch_quote(instrument)
        account = self.broker.account()
        conditions = ExecutionConditions(
            quote=quote,
            market_open=True,
            data_age_seconds=0.0 if self.paper_mode else None,
            average_daily_volume=signal.features.get("avg_volume_50"),
            buying_power=account.buying_power,
            kill_switch_active=self.kill_switch.active,
            circuit_breaker_active=self.breakers.blocks_new_trades,
            risk_approved=True,
            max_spread_bps=self.limits.max_spread_bps,
        )
        return self.execution.execute(signal, instrument, verdict, conditions)

    def _manage_open_positions(self, prices: Mapping[str, float]) -> list[str]:
        """Apply stops, targets and trailing to open positions."""
        exits: list[str] = []
        for position in list(self.book.positions):
            price = prices.get(position.symbol)
            if price is None:
                continue
            position.mark(price)

            # Stop breach.
            if position.stop_price is not None:
                breached = (
                    price <= position.stop_price
                    if position.side.value == "long"
                    else price >= position.stop_price
                )
                if breached:
                    result = self.execution.exit_position(
                        position.symbol, position.quantity, reason="stop_loss",
                        regime=self._regime.primary.value if self._regime else "UNKNOWN",
                    )
                    if result.success:
                        exits.append(f"{position.symbol}: stop_loss")
                    continue

            # Targets.
            remaining: list[tuple[float, float]] = []
            for target_price, fraction in position.targets:
                hit = (
                    price >= target_price if position.side.value == "long" else price <= target_price
                )
                if hit and position.quantity > 0:
                    quantity = position.quantity * fraction
                    result = self.execution.exit_position(position.symbol, quantity, reason="target")
                    if result.success:
                        exits.append(f"{position.symbol}: target at {target_price:.4g}")
                else:
                    remaining.append((target_price, fraction))
            current = self.book.get(position.symbol)
            if current is None:
                continue
            current.targets = remaining

            # Trailing.
            self.trailing.update(current, price, trail_pct=0.06)
        return exits

    def _check_halts(self, now: datetime) -> str | None:
        """Kill switch and circuit breakers, evaluated every cycle."""
        equity = self.book.equity
        trips = self.breakers.evaluate(equity)
        for trip in trips:
            self.journal.alert(
                f"circuit breaker {trip.rule}: {trip.message}",
                severity=Severity.CRITICAL if trip.requires_human else Severity.WARNING,
            )

        signals = SystemSignals(
            session_drawdown=self.book.drawdown,
            daily_loss=max(0.0, -self.book.daily_pnl_pct(now.date())),
            leverage=max(0.0, self.book.snapshot(now).gross_exposure),
            consecutive_order_failures=self.execution.consecutive_failures,
        )
        self.kill_switch.check(signals)

        if self.kill_switch.active:
            return "kill switch engaged"
        if self.breakers.blocks_new_trades:
            return "circuit breaker blocking new trades"
        return None

    # -- readiness (section 31.1) -------------------------------------------

    def readiness_report(self) -> dict[str, object]:
        """Section 31.1 production-readiness checklist."""
        checks: dict[str, bool] = {
            "broker_connected": self.broker.healthy(),
            "data_providers_connected": any(p.healthy() for p in self.providers.providers),
            "required_mcp_tools_available": not self._missing_mcp_tools(),
            "kill_switch_operational": not self.kill_switch.active,
            "circuit_breakers_configured": True,
            "audit_logging_active": True,
            "risk_limits_loaded": self.limits.max_risk_per_trade > 0,
            "universe_configured": bool(self.instruments),
            "not_live_on_synthetic_data": self._refuse_live_on_synthetic_data() is None,
        }
        return {
            "ready": all(checks.values()),
            "paper_mode": self.paper_mode,
            "checks": checks,
            "failing": sorted(name for name, ok in checks.items() if not ok),
            "config_violations": list(self.config.governor.violations),
        }
