"""Strategy selection engine (spec section 6.3).

Implements the section 6.3 pipeline for one candidate instrument:

1. asset class supported
2. sufficient history
3. regime determined (supplied by the caller)
4. strategies filtered to those eligible in that regime
5. entry conditions evaluated
6. signal scored
7. multi-timeframe confirmation
8. highest-scoring strategy above the regime minimum wins, else NO TRADE

The regime filter runs *before* condition evaluation. That ordering is the
system's central discipline: a textbook momentum breakout in a bear market is
never evaluated at all, so it can never be "too good to pass up".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from ..core.types import AssetClass, Instrument, ScoreBreakdown, Signal, Timeframe
from ..indicators.engine import FeatureEngine, FeatureSet
from ..observability.journal import Journal, NULL_JOURNAL
from ..regime.engine import RegimeState, is_strategy_type_allowed, minimum_score
from .library import ALL_STRATEGIES
from .scoring import ScoringInputs, SignalScoringEngine
from .spec import EvalContext, Evaluation, StrategySpec


@dataclass(frozen=True, slots=True)
class Candidate:
    """One strategy's evaluation against one instrument."""

    spec: StrategySpec
    evaluation: Evaluation
    score: ScoreBreakdown
    eligible: bool
    ineligible_reason: str = ""

    @property
    def total(self) -> float:
        return self.score.total

    @property
    def triggered(self) -> bool:
        return self.evaluation.triggered


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """The outcome for one instrument, whether or not it produced a signal."""

    symbol: str
    signal: Signal | None
    candidates: tuple[Candidate, ...] = ()
    reason: str = ""
    minimum_score: float | None = None

    @property
    def has_signal(self) -> bool:
        return self.signal is not None

    def explain(self) -> str:
        if self.signal is not None:
            return (
                f"{self.symbol}: {self.signal.strategy_id} scored {self.signal.total_score:.1f} "
                f"(minimum {self.minimum_score:.0f})"
            )
        return f"{self.symbol}: NO TRADE - {self.reason}"


class StrategyEngine:
    """Selects at most one strategy per instrument per cycle."""

    def __init__(
        self,
        strategies: Sequence[StrategySpec] = ALL_STRATEGIES,
        *,
        scorer: SignalScoringEngine | None = None,
        features: FeatureEngine | None = None,
        journal: Journal | None = None,
        require_multi_timeframe: bool = True,
    ) -> None:
        self.strategies = list(strategies)
        self.scorer = scorer if scorer is not None else SignalScoringEngine()
        self.features = features if features is not None else FeatureEngine()
        self.journal = journal if journal is not None else NULL_JOURNAL
        self.require_multi_timeframe = require_multi_timeframe

    def select(
        self,
        *,
        instrument: Instrument,
        features: FeatureSet,
        regime: RegimeState,
        context: EvalContext | None = None,
        inputs: ScoringInputs | None = None,
        higher_timeframe: FeatureSet | None = None,
        timeframe: Timeframe = Timeframe.D1,
    ) -> SelectionResult:
        """Run the full section 6.3 pipeline for one instrument."""
        context = context if context is not None else EvalContext(features=features)
        threshold = minimum_score(regime)

        if not regime.is_tradeable or threshold is None:
            reason = f"regime {regime.primary.value} permits no new positions"
            self.journal.trade_decision(instrument.symbol, "NO_TRADE", reason, regime=regime.primary.value)
            return SelectionResult(instrument.symbol, None, (), reason, threshold)

        candidates: list[Candidate] = []
        for spec in self.strategies:
            candidate = self._evaluate(spec, instrument, features, regime, context, inputs)
            if candidate is not None:
                candidates.append(candidate)

        triggered = [c for c in candidates if c.eligible and c.triggered]
        if not triggered:
            reason = self._no_trade_reason(candidates, instrument.asset_class)
            self.journal.trade_decision(instrument.symbol, "NO_TRADE", reason, regime=regime.primary.value)
            return SelectionResult(instrument.symbol, None, tuple(candidates), reason, threshold)

        best = max(triggered, key=lambda c: c.total)

        if best.total < threshold:
            reason = (
                f"best candidate {best.spec.id} scored {best.total:.1f}, "
                f"below the {threshold:.0f} minimum for {regime.primary.value}"
            )
            self.journal.trade_decision(instrument.symbol, "NO_TRADE", reason, score=best.total)
            return SelectionResult(instrument.symbol, None, tuple(candidates), reason, threshold)

        if self.require_multi_timeframe and higher_timeframe is not None:
            confirmed, detail = self._confirm_higher_timeframe(best.spec, higher_timeframe)
            if not confirmed:
                reason = f"{best.spec.id} not confirmed on the higher timeframe: {detail}"
                self.journal.trade_decision(instrument.symbol, "NO_TRADE", reason)
                return SelectionResult(instrument.symbol, None, tuple(candidates), reason, threshold)

        signal = self._build_signal(best, instrument, features, regime, timeframe)
        self.journal.trade_decision(
            instrument.symbol,
            "TRADE",
            f"{best.spec.name} triggered, score {best.total:.1f} (minimum {threshold:.0f})",
            strategy=best.spec.id,
            score=best.total,
            regime=regime.primary.value,
            conditions=[str(r) for r in best.evaluation.results],
        )
        return SelectionResult(instrument.symbol, signal, tuple(candidates), "signal generated", threshold)

    # -- internals ----------------------------------------------------------

    def _evaluate(
        self,
        spec: StrategySpec,
        instrument: Instrument,
        features: FeatureSet,
        regime: RegimeState,
        context: EvalContext,
        inputs: ScoringInputs | None,
    ) -> Candidate | None:
        if not spec.supports(instrument.asset_class):
            return None
        if features.bars_available < spec.min_data_points:
            return Candidate(
                spec,
                Evaluation(spec.id, False, ()),
                ScoreBreakdown({}),
                eligible=False,
                ineligible_reason=(
                    f"needs {spec.min_data_points} bars, {features.bars_available} available"
                ),
            )

        allowed, why = is_strategy_type_allowed(spec.strategy_type, regime)
        if not allowed:
            return Candidate(
                spec, Evaluation(spec.id, False, ()), ScoreBreakdown({}), eligible=False, ineligible_reason=why
            )

        if spec.requires_fundamentals and not context.fundamentals.is_available:
            return Candidate(
                spec,
                Evaluation(spec.id, False, ()),
                ScoreBreakdown({}),
                eligible=False,
                ineligible_reason="requires fundamental data, none supplied",
            )

        evaluation = spec.evaluate(context)
        score = self.scorer.score(
            spec=spec,
            evaluation=evaluation,
            features=features,
            regime=regime,
            context=context,
            inputs=inputs,
        )
        return Candidate(spec, evaluation, score, eligible=True)

    @staticmethod
    def _confirm_higher_timeframe(spec: StrategySpec, higher: FeatureSet) -> tuple[bool, str]:
        """Require the higher timeframe not to contradict the signal.

        Deliberately a veto rather than a second entry test: the higher
        timeframe's job is to stop a long being taken into a broken weekly
        chart, not to re-run the setup at a slower cadence.
        """
        ma50 = higher.number("ma50")
        if ma50 is None:
            return True, "higher timeframe has insufficient history (not treated as a veto)"

        if spec.strategy_type in {"mean_reversion", "reversal"}:
            return True, "counter-trend strategy, higher-timeframe trend not required"

        if higher.close < ma50:
            return False, f"higher-timeframe close {higher.close:.4g} below MA50 {ma50:.4g}"
        return True, f"higher-timeframe close {higher.close:.4g} above MA50 {ma50:.4g}"

    @staticmethod
    def _build_signal(
        candidate: Candidate,
        instrument: Instrument,
        features: FeatureSet,
        regime: RegimeState,
        timeframe: Timeframe,
    ) -> Signal:
        spec = candidate.spec
        return Signal(
            symbol=instrument.symbol,
            asset_class=instrument.asset_class,
            strategy_id=spec.id,
            side=spec.side,
            entry_price=features.close,
            timestamp=features.timestamp,
            timeframe=timeframe,
            score=candidate.score,
            structure_level=spec.structure_level(features),
            atr=features.atr,
            regime=regime.primary.value,
            regime_confidence=regime.confidence,
            features={
                "volatility_percentile": features.number("volatility_percentile"),
                "relative_volume": features.number("relative_volume"),
                "adx_14": features.number("adx_14"),
                "rsi_14": features.number("rsi_14"),
                "target_ladder": spec.target_ladder,
                "strategy_type": spec.strategy_type,
                "max_holding_days": spec.max_holding_days,
            },
            invalidation=tuple(c.name for c in spec.invalidation_conditions),
        )

    @staticmethod
    def _no_trade_reason(candidates: Sequence[Candidate], asset_class: AssetClass) -> str:
        if not candidates:
            return f"no strategies support {asset_class.value}"
        eligible = [c for c in candidates if c.eligible]
        if not eligible:
            reasons = {c.ineligible_reason for c in candidates}
            return "no eligible strategies: " + "; ".join(sorted(reasons)[:3])
        best = max(eligible, key=lambda c: c.evaluation.pass_ratio)
        return f"no entry conditions fully met; closest was {best.evaluation.summary()}"


def evaluate_exits(
    spec: StrategySpec,
    context: EvalContext,
) -> list[str]:
    """Invalidation conditions currently true — the strategy's own exit reasons."""
    return [f"{r.name}: {r.detail}" for r in spec.check_invalidation(context)]
