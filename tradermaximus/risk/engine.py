"""Risk engine (spec section 8) — the authorisation layer.

Strategies *propose*; this engine *authorises*. Section 30 makes the separation
explicit: "The AI may recommend. The risk engine decides." Nothing downstream
may create an order without a :class:`RiskVerdict` that says ``approved``.

Checks run in a deliberate order — system-level halts first, then trade-level
sizing, then the post-trade portfolio projection. Two reasons:

1. **Cost.** A kill switch check is free; projecting portfolio correlation is
   not. Rejecting early avoids work that cannot change the outcome.
2. **Honesty of the reason.** When the kill switch is engaged *and* the
   position is oversized, the recorded reason should be the kill switch. It is
   the condition that actually stopped the trade.

Every rejection carries a stable rule id so the weekly review can aggregate
rejections by cause without parsing prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Sequence

from ..core.types import AssetClass, Instrument, Side, Signal, utc_now
from ..observability.journal import Journal, NULL_JOURNAL
from ..portfolio.book import PortfolioSnapshot, PositionBook
from .circuit_breakers import CircuitBreakerEngine
from .kill_switch import KillSwitch
from .limits import HardLimits, limits_for
from .portfolio import PortfolioRiskEngine, PortfolioRiskMetrics
from .sizing import PositionSizingEngine, SizingDecision
from .stops import StopDecision, StopLossEngine


class Rule:
    """Stable rejection rule identifiers (section 8.2)."""

    KILL_SWITCH = "R21_KILL_SWITCH_ACTIVE"
    CIRCUIT_BREAKER = "R22_CIRCUIT_BREAKER_ACTIVE"
    DATA_QUALITY = "R19_DATA_QUALITY_FAILURE"
    EXECUTION_SAFETY = "R20_EXECUTION_SAFETY_VIOLATION"
    REGIME_INELIGIBLE = "R00_REGIME_INELIGIBLE"
    SIGNAL_SCORE = "R00_SIGNAL_SCORE_BELOW_THRESHOLD"
    STOP_REJECTED = "R00_STOP_PLACEMENT_REJECTED"
    SIZING_REJECTED = "R00_POSITION_SIZING_REJECTED"
    TRADE_RISK = "R01_TRADE_RISK_EXCEEDS_LIMIT"
    PORTFOLIO_RISK = "R02_PORTFOLIO_RISK_EXCEEDS_LIMIT"
    PORTFOLIO_EXPOSURE = "R03_PORTFOLIO_EXPOSURE_EXCEEDS_LIMIT"
    NET_EXPOSURE = "R04_NET_EXPOSURE_EXCEEDS_LIMIT"
    DAILY_LOSS = "R05_DAILY_LOSS_EXCEEDS_LIMIT"
    WEEKLY_LOSS = "R06_WEEKLY_LOSS_EXCEEDS_LIMIT"
    MONTHLY_DRAWDOWN = "R07_MONTHLY_DRAWDOWN_EXCEEDS_LIMIT"
    PORTFOLIO_DRAWDOWN = "R08_PORTFOLIO_DRAWDOWN_EXCEEDS_LIMIT"
    POSITION_COUNT = "R09_POSITION_COUNT_EXCEEDS_LIMIT"
    SECTOR_EXPOSURE = "R10_SECTOR_EXPOSURE_EXCEEDS_LIMIT"
    ASSET_CLASS_EXPOSURE = "R11_ASSET_CLASS_EXPOSURE_EXCEEDS_LIMIT"
    CORRELATED_EXPOSURE = "R12_CORRELATED_EXPOSURE_EXCEEDS_LIMIT"
    LEVERAGE = "R13_LEVERAGE_EXCEEDS_LIMIT"
    DAILY_TRADES = "R14_DAILY_TRADE_COUNT_EXCEEDS_LIMIT"
    ORDER_SIZE = "R15_ORDER_SIZE_EXCEEDS_LIQUIDITY_LIMIT"
    SPREAD = "R16_SPREAD_EXCEEDS_LIMIT"
    SLIPPAGE = "R17_ESTIMATED_SLIPPAGE_EXCEEDS_LIMIT"
    EXPECTED_SHORTFALL = "R18_EXPECTED_SHORTFALL_EXCEEDS_LIMIT"
    DUPLICATE_POSITION = "R00_POSITION_ALREADY_OPEN"
    SINGLE_POSITION = "R00_SINGLE_POSITION_EXCEEDS_LIMIT"


@dataclass(frozen=True, slots=True)
class RiskContext:
    """Everything the engine needs beyond the signal itself."""

    equity: float
    snapshot: PortfolioSnapshot
    daily_loss: float = 0.0
    weekly_loss: float = 0.0
    monthly_drawdown: float = 0.0
    portfolio_drawdown: float = 0.0
    trades_today: int = 0
    spread_bps: float | None = None
    estimated_slippage_bps: float | None = None
    average_daily_volume: float | None = None
    volatility: float | None = None
    regime_multiplier: float = 1.0
    data_quality_passed: bool = True
    data_quality_reason: str = ""
    execution_safe: bool = True
    execution_reason: str = ""
    portfolio_returns: Sequence[float] = ()
    returns_by_symbol: Mapping[str, Sequence[float]] = field(default_factory=dict)
    available_cash: float | None = None
    minimum_score: float | None = None


@dataclass(frozen=True, slots=True)
class RiskVerdict:
    """The authorisation decision plus everything used to reach it."""

    approved: bool
    symbol: str
    rule: str | None = None
    reason: str = ""
    stop: StopDecision | None = None
    sizing: SizingDecision | None = None
    metrics: PortfolioRiskMetrics | None = None
    checks_run: tuple[str, ...] = ()
    timestamp: datetime = field(default_factory=utc_now)

    @property
    def quantity(self) -> float:
        return self.sizing.quantity if self.sizing and self.sizing.approved else 0.0

    @property
    def stop_price(self) -> float | None:
        return self.stop.stop_price if self.stop and self.stop.accepted else None

    def as_dict(self) -> dict[str, object]:
        return {
            "approved": self.approved,
            "symbol": self.symbol,
            "rule": self.rule,
            "reason": self.reason,
            "quantity": self.quantity,
            "stop_price": self.stop_price,
            "checks_run": len(self.checks_run),
        }


class RiskEngine:
    """Single authorisation gate for every trade the system attempts."""

    def __init__(
        self,
        limits: HardLimits | None = None,
        *,
        kill_switch: KillSwitch | None = None,
        breakers: CircuitBreakerEngine | None = None,
        journal: Journal | None = None,
    ) -> None:
        self.limits = limits if limits is not None else HardLimits()
        self.kill_switch = kill_switch
        self.breakers = breakers
        self.journal = journal if journal is not None else NULL_JOURNAL
        self.stops = StopLossEngine()
        self.sizer = PositionSizingEngine(self.limits)
        self.portfolio = PortfolioRiskEngine(self.limits)

    # -- main entry point ---------------------------------------------------

    def authorize(
        self,
        signal: Signal,
        instrument: Instrument,
        context: RiskContext,
    ) -> RiskVerdict:
        """Authorise or reject one proposed trade."""
        checks: list[str] = []

        def reject(rule: str, reason: str, **extra: object) -> RiskVerdict:
            verdict = RiskVerdict(
                approved=False,
                symbol=signal.symbol,
                rule=rule,
                reason=reason,
                checks_run=tuple(checks),
                **extra,  # type: ignore[arg-type]
            )
            self.journal.risk_decision(False, reason, symbol=signal.symbol, rule=rule, strategy=signal.strategy_id)
            return verdict

        # ---- 1. system-level halts ----------------------------------------
        checks.append(Rule.KILL_SWITCH)
        if self.kill_switch is not None and self.kill_switch.active:
            triggers = ", ".join(e.trigger.value for e in self.kill_switch.events)
            return reject(Rule.KILL_SWITCH, f"kill switch is engaged ({triggers})")

        checks.append(Rule.CIRCUIT_BREAKER)
        if self.breakers is not None and self.breakers.blocks_new_trades:
            tripped = ", ".join(t.rule for t in self.breakers.trips)
            return reject(Rule.CIRCUIT_BREAKER, f"circuit breaker active ({tripped})")

        checks.append(Rule.DATA_QUALITY)
        if not context.data_quality_passed:
            return reject(Rule.DATA_QUALITY, context.data_quality_reason or "data quality validation failed")

        checks.append(Rule.EXECUTION_SAFETY)
        if not context.execution_safe:
            return reject(Rule.EXECUTION_SAFETY, context.execution_reason or "execution safety check failed")

        # ---- 2. account-level loss and drawdown gates ----------------------
        checks.append(Rule.DAILY_LOSS)
        if context.daily_loss > self.limits.max_daily_loss:
            return reject(
                Rule.DAILY_LOSS,
                f"daily loss {context.daily_loss:.2%} exceeds limit {self.limits.max_daily_loss:.2%}",
            )

        checks.append(Rule.WEEKLY_LOSS)
        if context.weekly_loss > self.limits.max_weekly_loss:
            return reject(
                Rule.WEEKLY_LOSS,
                f"weekly loss {context.weekly_loss:.2%} exceeds limit {self.limits.max_weekly_loss:.2%}",
            )

        checks.append(Rule.MONTHLY_DRAWDOWN)
        if context.monthly_drawdown > self.limits.max_monthly_drawdown:
            return reject(
                Rule.MONTHLY_DRAWDOWN,
                f"monthly drawdown {context.monthly_drawdown:.2%} exceeds limit "
                f"{self.limits.max_monthly_drawdown:.2%}",
            )

        checks.append(Rule.PORTFOLIO_DRAWDOWN)
        if context.portfolio_drawdown > self.limits.max_portfolio_drawdown:
            return reject(
                Rule.PORTFOLIO_DRAWDOWN,
                f"portfolio drawdown {context.portfolio_drawdown:.2%} exceeds limit "
                f"{self.limits.max_portfolio_drawdown:.2%}",
            )

        checks.append(Rule.DAILY_TRADES)
        if context.trades_today >= self.limits.max_daily_trades:
            return reject(
                Rule.DAILY_TRADES,
                f"{context.trades_today} trades today reaches the {self.limits.max_daily_trades} limit",
            )

        # ---- 3. execution conditions --------------------------------------
        checks.append(Rule.SPREAD)
        if context.spread_bps is not None and context.spread_bps > self.limits.max_spread_bps:
            return reject(
                Rule.SPREAD,
                f"spread {context.spread_bps:.1f}bps exceeds limit {self.limits.max_spread_bps:.1f}bps",
            )

        checks.append(Rule.SLIPPAGE)
        if (
            context.estimated_slippage_bps is not None
            and context.estimated_slippage_bps > self.limits.max_slippage_bps
        ):
            return reject(
                Rule.SLIPPAGE,
                f"estimated slippage {context.estimated_slippage_bps:.1f}bps exceeds limit "
                f"{self.limits.max_slippage_bps:.1f}bps",
            )

        # ---- 4. signal quality --------------------------------------------
        checks.append(Rule.SIGNAL_SCORE)
        if context.minimum_score is None:
            return reject(
                Rule.REGIME_INELIGIBLE,
                f"regime {signal.regime} permits no new positions",
            )
        if signal.total_score < context.minimum_score:
            return reject(
                Rule.SIGNAL_SCORE,
                f"signal score {signal.total_score:.1f} below the {context.minimum_score:.1f} "
                f"minimum for regime {signal.regime}",
            )

        # ---- 5. position count and duplication ----------------------------
        checks.append(Rule.DUPLICATE_POSITION)
        if any(p.symbol == signal.symbol for p in context.snapshot.positions):
            return reject(Rule.DUPLICATE_POSITION, f"a position in {signal.symbol} is already open")

        checks.append(Rule.POSITION_COUNT)
        counts = context.snapshot.count_by_asset_class()
        if context.snapshot.position_count >= self.limits.max_positions_total:
            return reject(
                Rule.POSITION_COUNT,
                f"{context.snapshot.position_count} open positions reaches the total limit "
                f"{self.limits.max_positions_total}",
            )
        class_limit = self.limits.max_positions_for(signal.asset_class)
        if counts.get(signal.asset_class, 0) >= class_limit:
            return reject(
                Rule.POSITION_COUNT,
                f"{counts.get(signal.asset_class, 0)} open {signal.asset_class.value} positions "
                f"reaches the class limit {class_limit}",
            )

        # ---- 6. stop placement (corrected section 10.1) --------------------
        checks.append(Rule.STOP_REJECTED)
        stop = self.stops.compute(
            entry_price=signal.entry_price,
            side=signal.side,
            asset_class=signal.asset_class,
            atr=signal.atr,
            structure_level=signal.structure_level,
            volatility_percentile=signal.features.get("volatility_percentile"),
        )
        if not stop.accepted:
            return reject(Rule.STOP_REJECTED, stop.reason, stop=stop)

        # ---- 7. sizing -----------------------------------------------------
        checks.append(Rule.SIZING_REJECTED)
        sizing = self.sizer.size(
            instrument=instrument,
            equity=context.equity,
            entry_price=signal.entry_price,
            stop_distance=stop.stop_distance or 0.0,
            volatility=context.volatility,
            average_daily_volume=context.average_daily_volume,
            available_cash=context.available_cash,
            regime_multiplier=context.regime_multiplier,
        )
        if not sizing.approved:
            return reject(Rule.SIZING_REJECTED, sizing.reason, stop=stop, sizing=sizing)

        # ---- 8. trade-level risk -------------------------------------------
        checks.append(Rule.TRADE_RISK)
        trade_risk = sizing.risk_amount / context.equity if context.equity > 0 else 1.0
        if trade_risk > self.limits.max_risk_per_trade * (1.0 + 1e-9):
            return reject(
                Rule.TRADE_RISK,
                f"trade risk {trade_risk:.3%} exceeds per-trade limit "
                f"{self.limits.max_risk_per_trade:.3%}",
                stop=stop,
                sizing=sizing,
            )

        checks.append(Rule.ORDER_SIZE)
        if context.average_daily_volume:
            participation = sizing.quantity / context.average_daily_volume
            asset_cap = limits_for(signal.asset_class).max_position_volume_pct
            cap = min(self.limits.max_order_size, asset_cap)
            if participation > cap * (1.0 + 1e-9):
                return reject(
                    Rule.ORDER_SIZE,
                    f"order is {participation:.2%} of average daily volume, above the {cap:.2%} cap",
                    stop=stop,
                    sizing=sizing,
                )

        # ---- 9. post-trade portfolio projection -----------------------------
        projected = self._project(context.snapshot, signal, instrument, sizing)
        metrics = self.portfolio.compute(
            projected,
            portfolio_returns=context.portfolio_returns,
            returns_by_symbol=context.returns_by_symbol,
            drawdown_current=context.portfolio_drawdown,
        )

        checks.append(Rule.PORTFOLIO_RISK)
        projected_risk = metrics.total_risk + trade_risk
        if projected_risk > self.limits.max_total_portfolio_risk * (1.0 + 1e-9):
            return reject(
                Rule.PORTFOLIO_RISK,
                f"portfolio risk would reach {projected_risk:.3%}, above the "
                f"{self.limits.max_total_portfolio_risk:.3%} limit",
                stop=stop,
                sizing=sizing,
                metrics=metrics,
            )

        portfolio_rules: tuple[tuple[str, str, float | None, float], ...] = (
            (Rule.PORTFOLIO_EXPOSURE, "portfolio exposure", metrics.total_exposure, self.limits.max_portfolio_exposure),
            (Rule.NET_EXPOSURE, "net exposure", abs(metrics.net_exposure), self.limits.max_net_exposure),
            (Rule.SINGLE_POSITION, "single position", metrics.largest_position, self.limits.max_single_position),
            (
                Rule.CORRELATED_EXPOSURE,
                "correlated exposure",
                metrics.correlated_exposure,
                self.limits.max_correlated_exposure,
            ),
            (Rule.LEVERAGE, "leverage", metrics.leverage, max(0.0, self.limits.max_leverage - 1.0)),
            (
                Rule.EXPECTED_SHORTFALL,
                "expected shortfall (95%)",
                metrics.expected_shortfall_95,
                self.limits.max_expected_shortfall_95,
            ),
        )
        for rule, label, value, limit in portfolio_rules:
            checks.append(rule)
            if value is not None and value > limit * (1.0 + 1e-9):
                return reject(
                    rule,
                    f"projected {label} {value:.3%} exceeds limit {limit:.3%}",
                    stop=stop,
                    sizing=sizing,
                    metrics=metrics,
                )

        checks.append(Rule.SECTOR_EXPOSURE)
        sector_exposure = metrics.sector_exposure.get(instrument.sector, 0.0)
        if sector_exposure > self.limits.max_sector_exposure * (1.0 + 1e-9):
            return reject(
                Rule.SECTOR_EXPOSURE,
                f"projected {instrument.sector} exposure {sector_exposure:.2%} exceeds "
                f"{self.limits.max_sector_exposure:.2%}",
                stop=stop,
                sizing=sizing,
                metrics=metrics,
            )

        checks.append(Rule.ASSET_CLASS_EXPOSURE)
        class_exposure = metrics.asset_class_exposure.get(signal.asset_class.value, 0.0)
        if class_exposure > self.limits.max_asset_class_exposure * (1.0 + 1e-9):
            return reject(
                Rule.ASSET_CLASS_EXPOSURE,
                f"projected {signal.asset_class.value} exposure {class_exposure:.2%} exceeds "
                f"{self.limits.max_asset_class_exposure:.2%}",
                stop=stop,
                sizing=sizing,
                metrics=metrics,
            )

        # ---- approved ------------------------------------------------------
        reason = (
            f"approved {sizing.quantity:g} units of {signal.symbol} at {signal.entry_price:g}, "
            f"stop {stop.stop_price:g} ({stop.stop_pct:.2%}), risking {trade_risk:.3%} of equity"
        )
        self.journal.risk_decision(
            True,
            reason,
            symbol=signal.symbol,
            strategy=signal.strategy_id,
            quantity=sizing.quantity,
            risk_pct=trade_risk,
            checks_run=len(checks),
        )
        return RiskVerdict(
            approved=True,
            symbol=signal.symbol,
            reason=reason,
            stop=stop,
            sizing=sizing,
            metrics=metrics,
            checks_run=tuple(checks),
        )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _project(
        snapshot: PortfolioSnapshot,
        signal: Signal,
        instrument: Instrument,
        sizing: SizingDecision,
    ) -> PortfolioSnapshot:
        """Snapshot as it *would* look with the proposed position added.

        Checking the projection rather than the current state is what stops the
        portfolio from being walked past a limit one compliant trade at a time.
        """
        from ..core.types import Position  # local import avoids a cycle

        candidate = Position(
            symbol=signal.symbol,
            asset_class=signal.asset_class,
            side=signal.side,
            quantity=sizing.quantity,
            entry_price=signal.entry_price,
            opened_at=signal.timestamp,
            strategy_id=signal.strategy_id,
            stop_price=None,
            sector=instrument.sector,
        )
        prices = dict(snapshot.prices)
        prices[signal.symbol] = signal.entry_price
        return PortfolioSnapshot(
            timestamp=snapshot.timestamp,
            equity=snapshot.equity,
            cash=snapshot.cash,
            positions=snapshot.positions + (candidate,),
            prices=prices,
        )

    # -- portfolio-level monitoring ------------------------------------------

    def review_portfolio(
        self,
        book: PositionBook,
        *,
        portfolio_returns: Sequence[float] = (),
        returns_by_symbol: Mapping[str, Sequence[float]] | None = None,
    ) -> tuple[PortfolioRiskMetrics, list[str]]:
        """Assess the live portfolio and return metrics plus breach messages."""
        snapshot = book.snapshot()
        metrics = self.portfolio.compute(
            snapshot,
            portfolio_returns=portfolio_returns or book.returns(),
            returns_by_symbol=returns_by_symbol or {},
            drawdown_current=book.drawdown,
        )
        breaches = self.portfolio.check(metrics)
        for breach in breaches:
            self.journal.risk_decision(False, breach.message, rule=breach.rule, scope="portfolio")
        return metrics, [b.message for b in breaches]
