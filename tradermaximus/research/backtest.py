"""Backtesting framework (spec section 20).

The backtester runs the *same* engines the live system runs — features,
regime, strategy selection, risk authorisation, execution — over historical
bars. Sharing the code is the point: a strategy validated here and deployed
live cannot diverge, because there is only one implementation.

Bias controls (section 20.3) are structural rather than advisory:

* **Look-ahead** — each bar's decision sees only
  :meth:`CandleSeries.slice_until` up to that bar's close. Future bars are not
  merely ignored, they are absent from the object the decision reads.
* **Execution timing** — a signal generated on bar *i* fills on bar *i+1*'s
  open, never on the close that produced it. Filling at the signal bar's close
  is the most common way a backtest invents returns that were never available.
* **Costs** — the paper broker's spread, square-root market impact and
  commission apply, so an edge thinner than its own transaction costs shows up
  as a loss rather than a profit.
* **Survivorship** — the caller supplies the universe; delisted symbols should
  be included with their real final bars.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Mapping, Sequence

from ..core.types import (
    AssetClass,
    Candle,
    Instrument,
    Side,
    Timeframe,
    TradeRecord,
)
from ..data.series import CandleSeries
from ..execution.brokers.paper import MarketState, PaperBroker, PaperFillModel
from ..indicators.engine import FeatureEngine, FeatureSet
from ..observability.journal import Journal, NULL_JOURNAL
from ..observability.metrics import PerformanceMetrics, compute_metrics
from ..portfolio.book import PositionBook
from ..regime.engine import MarketContext, RegimeEngine, RegimeState, minimum_score
from ..risk.engine import RiskContext, RiskEngine
from ..risk.limits import HardLimits
from ..risk.stops import StopLossEngine
from ..risk.targets import TakeProfitEngine
from ..risk.trailing import TrailingStopEngine
from ..strategy.engine import StrategyEngine
from ..strategy.scoring import ScoringInputs
from ..strategy.spec import EvalContext, Fundamentals


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    starting_equity: float = 100_000.0
    warmup_bars: int = 250
    commission_bps: float = 1.0
    impact_coefficient_bps: float = 120.0
    partial_fill_probability: float = 0.0
    random_slippage_bps: float = 2.0
    seed: int = 20240101
    #: Bars of history each decision sees. Bounds per-bar cost so runtime is
    #: linear in the number of bars rather than quadratic. Must exceed the
    #: slowest indicator's warm-up (MA200 plus the 250-bar volatility
    #: percentile lookback); beyond that, older bars change nothing.
    decision_window: int = 400
    #: Periods per year, used to annualise Sharpe and Sortino.
    #:
    #: This MUST match the bar interval. Annualising weekly bars at 252 inflates
    #: Sharpe by sqrt(252/52) ~ 2.2x, which would turn a Sharpe of 0.3 into 0.67
    #: and could carry a strategy through a deployment gate it never earned.
    #: 252 for equities/FX daily, 365 for crypto daily (it trades every day),
    #: 52 for weekly.
    periods_per_year: int = 252
    max_positions: int = 10
    allow_shorts: bool = False
    benchmark_symbol: str | None = None


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Everything needed to judge whether a strategy is deployable."""

    trades: tuple[TradeRecord, ...]
    equity_curve: tuple[tuple[datetime, float], ...]
    metrics: PerformanceMetrics
    signals_generated: int = 0
    signals_rejected: int = 0
    rejection_reasons: Mapping[str, int] = field(default_factory=dict)
    starting_equity: float = 0.0
    final_equity: float = 0.0
    bars_processed: int = 0

    @property
    def equity_values(self) -> list[float]:
        return [equity for _, equity in self.equity_curve]

    @property
    def total_return(self) -> float:
        if self.starting_equity <= 0:
            return 0.0
        return (self.final_equity - self.starting_equity) / self.starting_equity

    def summary(self) -> dict[str, object]:
        return {
            "trades": len(self.trades),
            "signals_generated": self.signals_generated,
            "signals_rejected": self.signals_rejected,
            "total_return": round(self.total_return, 4),
            "final_equity": round(self.final_equity, 2),
            "bars": self.bars_processed,
            **self.metrics.as_dict(),
        }


class Backtester:
    """Event-driven backtest over aligned daily bars."""

    def __init__(
        self,
        config: BacktestConfig | None = None,
        *,
        strategy_engine: StrategyEngine | None = None,
        limits: HardLimits | None = None,
        journal: Journal | None = None,
        fundamentals: Mapping[str, Fundamentals] | None = None,
        scoring_inputs: Callable[[str, FeatureSet], ScoringInputs] | None = None,
    ) -> None:
        self.config = config if config is not None else BacktestConfig()
        self.strategy_engine = strategy_engine if strategy_engine is not None else StrategyEngine()
        self.limits = limits if limits is not None else HardLimits()
        self.journal = journal if journal is not None else NULL_JOURNAL
        self.fundamentals = dict(fundamentals or {})
        self.scoring_inputs = scoring_inputs
        self.features = FeatureEngine()
        self.stops = StopLossEngine()
        self.trailing = TrailingStopEngine()
        self.targets = TakeProfitEngine()

    def run(
        self,
        universe: Mapping[Instrument, Sequence[Candle]],
        *,
        timeframe: Timeframe = Timeframe.D1,
        benchmark: Sequence[Candle] | None = None,
        market_context: MarketContext | None = None,
    ) -> BacktestResult:
        """Run the backtest over the supplied universe."""
        cfg = self.config
        series_by_symbol: dict[str, CandleSeries] = {
            inst.symbol: CandleSeries.build(inst, timeframe, candles) for inst, candles in universe.items()
        }
        instruments = {inst.symbol: inst for inst in universe}

        timeline = self._timeline(series_by_symbol)
        if len(timeline) <= cfg.warmup_bars + 1:
            raise ValueError(
                f"insufficient history: {len(timeline)} bars, need more than {cfg.warmup_bars + 1}"
            )

        book = PositionBook(cfg.starting_equity, cash_reserve=0.0)
        broker = PaperBroker(
            starting_cash=cfg.starting_equity,
            model=PaperFillModel(
                commission_bps=cfg.commission_bps,
                impact_coefficient_bps=cfg.impact_coefficient_bps,
                partial_fill_probability=cfg.partial_fill_probability,
                random_slippage_bps=cfg.random_slippage_bps,
                latency_ms=0.0,
                latency_jitter_ms=0.0,
            ),
            seed=cfg.seed,
        )
        risk = RiskEngine(self.limits, journal=self.journal)
        regime_engine = RegimeEngine()

        benchmark_series = (
            CandleSeries.build(
                Instrument("BENCHMARK", AssetClass.EQUITIES), timeframe, benchmark
            )
            if benchmark
            else None
        )

        signals_generated = 0
        signals_rejected = 0
        rejections: dict[str, int] = {}
        pending: list[tuple[str, object]] = []  # (symbol, verdict+signal) to fill next bar

        for index in range(cfg.warmup_bars, len(timeline)):
            now = timeline[index]

            # -- 1. mark existing positions to this bar's close ---------------
            prices = self._prices_at(series_by_symbol, now)
            for symbol, price in prices.items():
                book.update_price(symbol, price)
                bar = self._bar_at(series_by_symbol, symbol, now)
                broker.update_price(symbol, price, bar_volume=bar.volume if bar else None)

            # -- 2. fill orders that were queued on the previous bar ----------
            for symbol, payload in pending:
                self._fill_pending(book, broker, instruments, symbol, payload, series_by_symbol, now)
            pending = []

            # -- 3. manage open positions (stops, targets, trailing) ----------
            self._manage_positions(book, broker, prices, series_by_symbol, now)

            book.mark_equity(now)

            # -- 4. look for new signals --------------------------------------
            if len(book) >= cfg.max_positions:
                continue

            regime = self._regime(regime_engine, benchmark_series, series_by_symbol, now, market_context)
            threshold = minimum_score(regime)
            if threshold is None:
                continue

            for symbol, series in series_by_symbol.items():
                if symbol in book or len(book) >= cfg.max_positions:
                    continue
                # Point-in-time truncation: the decision cannot see past `now`.
                visible = series.slice_until(now)
                if len(visible) < cfg.warmup_bars:
                    continue

                features = self.features.compute(visible.tail(cfg.decision_window))
                if features.timestamp != now:
                    continue  # this symbol did not trade on this bar

                instrument = instruments[symbol]
                context = EvalContext(
                    features=features,
                    fundamentals=self.fundamentals.get(symbol, Fundamentals()),
                )
                inputs = (
                    self.scoring_inputs(symbol, features)
                    if self.scoring_inputs
                    else ScoringInputs(average_daily_volume=features.number("avg_volume_50"))
                )
                selection = self.strategy_engine.select(
                    instrument=instrument,
                    features=features,
                    regime=regime,
                    context=context,
                    inputs=inputs,
                    timeframe=timeframe,
                )
                if selection.signal is None:
                    continue

                signals_generated += 1
                verdict = risk.authorize(
                    selection.signal,
                    instrument,
                    RiskContext(
                        equity=book.equity,
                        snapshot=book.snapshot(now),
                        portfolio_drawdown=book.drawdown,
                        average_daily_volume=features.number("avg_volume_50"),
                        volatility=features.number("realized_vol_20"),
                        minimum_score=threshold,
                        regime_multiplier=regime.size_multiplier,
                        portfolio_returns=book.returns(),
                        available_cash=book.cash,
                        data_quality_passed=True,
                    ),
                )
                if not verdict.approved:
                    signals_rejected += 1
                    rejections[verdict.rule or "unknown"] = rejections.get(verdict.rule or "unknown", 0) + 1
                    continue

                # Queue for the *next* bar's open: never fill on the bar that
                # produced the signal.
                pending.append((symbol, (selection.signal, verdict, regime.primary.value)))

        # -- close anything still open at the final bar ------------------------
        final = timeline[-1]
        for position in list(book.positions):
            price = book.price_of(position.symbol)
            book.close_position(position.symbol, price, timestamp=final, reason="backtest_end")
        book.mark_equity(final)

        equity_curve = tuple(book.equity_curve)
        metrics = compute_metrics(
            book.closed_trades,
            [e for _, e in equity_curve],
            periods_per_year=cfg.periods_per_year,
        )

        return BacktestResult(
            trades=tuple(book.closed_trades),
            equity_curve=equity_curve,
            metrics=metrics,
            signals_generated=signals_generated,
            signals_rejected=signals_rejected,
            rejection_reasons=rejections,
            starting_equity=cfg.starting_equity,
            final_equity=book.equity,
            bars_processed=len(timeline) - cfg.warmup_bars,
        )

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _timeline(series_by_symbol: Mapping[str, CandleSeries]) -> list[datetime]:
        stamps: set[datetime] = set()
        for series in series_by_symbol.values():
            stamps.update(series.timestamps)
        return sorted(stamps)

    @staticmethod
    def _bar_at(series_by_symbol: Mapping[str, CandleSeries], symbol: str, when: datetime) -> Candle | None:
        series = series_by_symbol.get(symbol)
        if series is None:
            return None
        for candle in reversed(series.candles):
            if candle.timestamp == when:
                return candle
            if candle.timestamp < when:
                return None
        return None

    def _prices_at(
        self,
        series_by_symbol: Mapping[str, CandleSeries],
        when: datetime,
    ) -> dict[str, float]:
        prices: dict[str, float] = {}
        for symbol, series in series_by_symbol.items():
            visible = series.slice_until(when)
            if len(visible):
                prices[symbol] = visible.last.close
        return prices

    def _regime(
        self,
        engine: RegimeEngine,
        benchmark: CandleSeries | None,
        series_by_symbol: Mapping[str, CandleSeries],
        when: datetime,
        context: MarketContext | None,
    ) -> RegimeState:
        """Classify the regime from the benchmark, falling back to the universe.

        Regime is a property of the market, not of one instrument, so the
        benchmark is used whenever one is supplied.
        """
        source = benchmark if benchmark is not None else next(iter(series_by_symbol.values()))
        visible = source.slice_until(when).tail(self.config.decision_window)
        features = self.features.compute(visible, detect_bases=False)
        return engine.classify(features, context, benchmark_closes=visible.closes)

    def _fill_pending(
        self,
        book: PositionBook,
        broker: PaperBroker,
        instruments: Mapping[str, Instrument],
        symbol: str,
        payload: object,
        series_by_symbol: Mapping[str, CandleSeries],
        now: datetime,
    ) -> None:
        """Fill a queued signal at this bar's open (never the signal bar's close)."""
        from ..core.types import Order, OrderSide, OrderType

        signal, verdict, regime_name = payload  # type: ignore[misc]
        if symbol in book:
            return

        bar = self._bar_at(series_by_symbol, symbol, now)
        if bar is None:
            return  # symbol did not trade; the signal simply expires

        broker.update_price(symbol, bar.open, bar_volume=bar.volume)
        order = Order(
            order_id=f"BT-{symbol}-{now.date()}",
            symbol=symbol,
            side=OrderSide.BUY if signal.side is Side.LONG else OrderSide.SELL,
            quantity=verdict.quantity,
            order_type=OrderType.MARKET,
        )
        ack = broker.submit(order)
        if not ack.accepted or not ack.fills:
            return

        filled = sum(f.quantity for f in ack.fills)
        average = sum(f.price * f.quantity for f in ack.fills) / filled

        # Re-derive the stop from the actual fill so the risk taken matches the
        # risk authorised, rather than the risk assumed at signal time.
        stop_distance = verdict.stop.stop_distance if verdict.stop else None
        stop_price = (
            average - stop_distance
            if (stop_distance and signal.side is Side.LONG)
            else (average + stop_distance if stop_distance else None)
        )
        ladder = self.targets.ladder_for(
            str(signal.features.get("strategy_type", "momentum")),
            trend_strength=signal.features.get("adx_14"),
        )
        book.open_position(
            symbol=symbol,
            asset_class=signal.asset_class,
            side=signal.side,
            quantity=filled,
            entry_price=average,
            strategy_id=signal.strategy_id,
            stop_price=stop_price,
            targets=ladder.prices(average, signal.side),
            sector=instruments[symbol].sector,
            timestamp=now,
            commission=sum(f.commission for f in ack.fills),
        )

    def _manage_positions(
        self,
        book: PositionBook,
        broker: PaperBroker,
        prices: Mapping[str, float],
        series_by_symbol: Mapping[str, CandleSeries],
        now: datetime,
    ) -> None:
        """Apply stops, targets, trailing and time stops to open positions.

        Stops are checked against the bar's *low* (for longs), not its close:
        a stop that was breached intraday was hit, and pretending otherwise
        systematically understates losses.
        """
        for position in list(book.positions):
            symbol = position.symbol
            price = prices.get(symbol)
            if price is None:
                continue
            bar = self._bar_at(series_by_symbol, symbol, now)
            position.mark(price)

            # 1. Stop-out, measured against the bar extreme.
            if position.stop_price is not None and bar is not None:
                breached = (
                    bar.low <= position.stop_price
                    if position.side is Side.LONG
                    else bar.high >= position.stop_price
                )
                if breached:
                    # Fill at the stop, or worse if the bar gapped through it.
                    fill = (
                        min(position.stop_price, bar.open)
                        if position.side is Side.LONG
                        else max(position.stop_price, bar.open)
                    )
                    book.close_position(symbol, fill, timestamp=now, reason="stop_loss")
                    continue

            # 2. Profit targets.
            remaining_targets: list[tuple[float, float]] = []
            for target_price, fraction in position.targets:
                hit = (
                    price >= target_price if position.side is Side.LONG else price <= target_price
                )
                if hit and position.quantity > 0:
                    quantity = position.quantity * fraction
                    if quantity > 0:
                        book.reduce_position(symbol, quantity, target_price, timestamp=now, reason="target")
                else:
                    remaining_targets.append((target_price, fraction))
            if symbol in book:
                book.get(symbol).targets = remaining_targets  # type: ignore[union-attr]
            else:
                continue

            # 3. Trailing stop.
            visible = series_by_symbol[symbol].slice_until(now)
            features_atr = None
            if len(visible) >= 20:
                from ..indicators.core import atr as atr_fn, last_value

                features_atr = last_value(atr_fn(visible.highs, visible.lows, visible.closes, 14))
            self.trailing.update(book.get(symbol), price, atr=features_atr)  # type: ignore[arg-type]

            # 4. Time stop.
            holding_days = (now - position.opened_at).total_seconds() / 86400.0
            if holding_days > 60:
                book.close_position(symbol, price, timestamp=now, reason="time_stop")
