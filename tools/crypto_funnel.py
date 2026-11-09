"""Stage-by-stage rejection funnel for the crypto path (Phase 0 item 5d).

`gate_autopsy.py` stops at the signal score. This carries every bar all the way
through the stop engine, the sizer and the risk engine, which is where the
interesting failures actually live: the previous crypto run produced six
signals in seven years, and a funnel that stops at scoring cannot tell you
whether the other candidates died at a condition, a threshold, a stop, or a
size cap.

Every bar lands in exactly one bucket, and the buckets are ordered, so the
columns sum to the bars examined. Each kill is additionally tagged with the
regime that was active when it happened, because "mean reversion never fires"
and "mean reversion never fires in a trend" are different diagnoses with
different fixes.

Run:
    python tools/crypto_funnel.py                      # daily BTC/ETH
    python tools/crypto_funnel.py --strategy STRATEGY_004
    python tools/crypto_funnel.py --by-regime
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradermaximus.core.types import AssetClass, Signal, Timeframe
from tradermaximus.data.csv_loader import load_directory
from tradermaximus.indicators.engine import FeatureEngine
from tradermaximus.portfolio.book import PositionBook
from tradermaximus.regime.engine import (
    MarketContext,
    RegimeEngine,
    effective_minimum_score,
    family_minimum_score,
    is_strategy_type_allowed,
    minimum_score,
)
from tradermaximus.research.backtest import derived_scoring_inputs
from tradermaximus.risk.engine import RiskContext, RiskEngine
from tradermaximus.risk.stops import StopLossEngine
from tradermaximus.strategy.library import ALL_STRATEGIES, STRATEGIES_BY_ID
from tradermaximus.strategy.scoring import SignalScoringEngine
from tradermaximus.strategy.spec import EvalContext, Fundamentals, StrategySpec

#: Ordered funnel stages. A bar is attributed to the FIRST one it fails.
STAGES = (
    "bar not eligible (warm-up / no features)",
    "regime permits no new positions",
    "strategy family vetoed by regime",
    "entry conditions not met",
    "score below family minimum",
    "stop rejected",
    "sizing rejected",
    "risk engine rejected",
    "AUTHORISED",
)


@dataclass
class Funnel:
    strategy_id: str
    pair: str
    bars: int = 0
    killed: Counter = field(default_factory=Counter)
    #: stage -> regime -> count
    by_regime: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    #: the specific condition that failed, when the kill was at entry conditions
    condition_kills: Counter = field(default_factory=Counter)
    #: verbatim reasons from the risk engine, for reproduction
    risk_reasons: Counter = field(default_factory=Counter)
    examples: dict[str, str] = field(default_factory=dict)

    def record(self, stage: str, regime: str, detail: str = "", example: str = "") -> None:
        self.killed[stage] += 1
        self.by_regime[stage][regime] += 1
        if detail:
            (self.condition_kills if stage == STAGES[3] else self.risk_reasons)[detail] += 1
        if example and stage not in self.examples:
            self.examples[stage] = example


def run(
    spec: StrategySpec,
    directory: Path,
    timeframe: Timeframe,
    *,
    warmup: int,
    equity: float,
) -> list[Funnel]:
    features_engine = FeatureEngine()
    regime_engine = RegimeEngine()
    scorer = SignalScoringEngine()
    stops = StopLossEngine()
    risk = RiskEngine()

    loaded = load_directory(
        directory, timeframe=timeframe, asset_class=AssetClass.CRYPTO, min_history=warmup
    )
    usable = [l for l in loaded.values() if l.usable]
    if not usable:
        raise SystemExit(f"no usable crypto series in {directory}")
    benchmark = max((l.series for l in usable), key=len)

    funnels: list[Funnel] = []
    for load in usable:
        series = load.series
        instrument = series.instrument
        funnel = Funnel(spec.id, instrument.symbol)
        # An empty book: the funnel measures the chance of a FIRST position,
        # not portfolio interaction. Portfolio effects are a separate question
        # and mixing them in here would hide single-signal failures.
        book = PositionBook(equity, cash_reserve=0.0)

        for index in range(warmup, len(series)):
            now = series.timestamps[index]
            funnel.bars += 1
            visible = series.slice_until(now).tail(400)
            if len(visible) < warmup:
                funnel.record(STAGES[0], "n/a")
                continue

            features = features_engine.compute(visible)
            bench_visible = benchmark.slice_until(now).tail(400)
            if len(bench_visible) < 60:
                funnel.record(STAGES[0], "n/a")
                continue
            bench_features = features_engine.compute(bench_visible, detect_bases=False)
            regime = regime_engine.classify(
                bench_features,
                MarketContext(breadth_index=0.5, new_highs_ratio=0.55, correlation_change=0.15),
                benchmark_closes=bench_visible.closes,
            )
            tag = regime.primary.value

            if not regime.is_tradeable or minimum_score(regime) is None:
                funnel.record(STAGES[1], tag)
                continue

            allowed, veto = is_strategy_type_allowed(spec.strategy_type, regime)
            if not allowed:
                funnel.record(STAGES[2], tag, example=veto)
                continue

            context = EvalContext(features=features, fundamentals=Fundamentals())
            evaluation = spec.evaluate(context)
            if not evaluation.triggered:
                failed = next(
                    (r.name for r in evaluation.results if not r.passed), "unknown"
                )
                detail = next(
                    (r.detail for r in evaluation.results if not r.passed), ""
                )
                funnel.record(STAGES[3], tag, detail=failed, example=f"{failed}: {detail}")
                continue

            inputs = derived_scoring_inputs(
                features, spec, benchmark_return=bench_features.number("roc_10"), spread_bps=5.0
            )
            score = scorer.score(
                spec=spec, evaluation=evaluation, features=features,
                regime=regime, context=context, inputs=inputs,
            )
            required = family_minimum_score(spec.strategy_type)
            if score.total < required:
                funnel.record(
                    STAGES[4], tag,
                    example=f"scored {score.total:.1f} < {required:.0f}",
                )
                continue

            # The stop, the size and the authorisation all belong to the risk
            # engine. Recomputing any of them here would measure this tool's
            # copy of the rules rather than the rules, which is the whole class
            # of error this funnel exists to catch.
            # Use the spec's OWN accessor, which honours StopSpec.use_structure.
            # An earlier revision of this file called features.nearest_support()
            # directly and so handed a structural level to strategies that had
            # declared use_structure=False, inventing 9 stop rejections that the
            # real pipeline would never produce. That is the exact failure mode
            # the docstring above warns about, committed by this tool.
            signal = Signal(
                symbol=instrument.symbol,
                asset_class=AssetClass.CRYPTO,
                strategy_id=spec.id,
                side=spec.side,
                entry_price=features.close,
                timestamp=now,
                timeframe=timeframe,
                score=score,
                structure_level=spec.structure_level(features),
                atr=features.number("atr_14"),
                regime=regime.primary.value,
                regime_confidence=regime.confidence,
                features={
                    "strategy_type": spec.strategy_type,
                    "volatility_percentile": features.number("volatility_percentile"),
                },
            )
            verdict = risk.authorize(
                signal, instrument,
                RiskContext(
                    equity=book.equity, snapshot=book.snapshot(now),
                    average_daily_volume=features.number("avg_volume_50"),
                    volatility=features.number("realized_vol_20"),
                    minimum_score=effective_minimum_score(spec.strategy_type, regime),
                    available_cash=book.cash, data_quality_passed=True,
                ),
            )
            if not verdict.approved:
                rule = verdict.rule or "unknown"
                if "STOP" in rule:
                    stage = STAGES[5]
                elif "SIZING" in rule:
                    stage = STAGES[6]
                else:
                    stage = STAGES[7]
                funnel.record(stage, tag, detail=rule, example=verdict.reason)
                continue

            funnel.record(STAGES[8], tag)

        funnels.append(funnel)
    return funnels


def render(funnel: Funnel, *, by_regime: bool) -> str:
    lines = [
        f"  {funnel.strategy_id}  {funnel.pair}   bars examined: {funnel.bars:,}",
        f"  {'stage':46}{'count':>9}{'% of bars':>11}{'survivors':>11}",
        f"  {'-'*46} {'-'*8} {'-'*10} {'-'*10}",
    ]
    survivors = funnel.bars
    for stage in STAGES:
        n = funnel.killed.get(stage, 0)
        if stage != STAGES[-1]:
            survivors -= n
        share = n / funnel.bars if funnel.bars else 0.0
        marker = "  <-- AUTHORISED" if stage == STAGES[-1] and n else ""
        lines.append(
            f"  {stage:46}{n:>9,}{share:>11.2%}"
            f"{(survivors if stage != STAGES[-1] else n):>11,}{marker}"
        )

    if funnel.condition_kills:
        lines.append("\n    entry conditions, by which one failed first:")
        for name, n in funnel.condition_kills.most_common(6):
            lines.append(f"      {n:>8,}  {name}")
    if funnel.risk_reasons:
        lines.append("\n    stop / sizing / risk rejections:")
        for name, n in funnel.risk_reasons.most_common(6):
            lines.append(f"      {n:>8,}  {name}")
    if funnel.examples:
        lines.append("\n    reproducible example per stage:")
        for stage, example in funnel.examples.items():
            if stage in (STAGES[0], STAGES[-1]):
                continue
            lines.append(f"      {stage[:34]:34} {example[:84]}")
    if by_regime:
        lines.append("\n    kills by active regime:")
        for stage in STAGES:
            counts = funnel.by_regime.get(stage)
            if not counts:
                continue
            spread = ", ".join(f"{r}={n}" for r, n in counts.most_common(4))
            lines.append(f"      {stage[:34]:34} {spread}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default="data/real/crypto")
    parser.add_argument("--timeframe", default="1d", choices=["1h", "4h", "1d"])
    parser.add_argument("--warmup", type=int, default=300)
    parser.add_argument("--equity", type=float, default=100_000.0)
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--by-regime", action="store_true")
    args = parser.parse_args()

    timeframe = {"1h": Timeframe.H1, "4h": Timeframe.H4, "1d": Timeframe.D1}[args.timeframe]
    specs = (
        [STRATEGIES_BY_ID[args.strategy]] if args.strategy
        else [s for s in ALL_STRATEGIES if AssetClass.CRYPTO in s.asset_classes]
    )

    print(f"CRYPTO REJECTION FUNNEL  ({args.timeframe} bars, {args.warmup}-bar warm-up)\n")
    totals: Counter = Counter()
    bars_total = 0
    for spec in specs:
        print("=" * 84)
        print(f"{spec.id}  {spec.name}  [{spec.strategy_type}, {spec.side.value}]")
        print("=" * 84)
        for funnel in run(spec, Path(args.data_dir), timeframe,
                          warmup=args.warmup, equity=args.equity):
            print(render(funnel, by_regime=args.by_regime))
            print()
            totals.update(funnel.killed)
            bars_total += funnel.bars

    print("=" * 84)
    print("COMBINED across every strategy and pair")
    print("=" * 84)
    print(f"  {'stage':46}{'count':>9}{'% of bars':>11}")
    for stage in STAGES:
        n = totals.get(stage, 0)
        print(f"  {stage:46}{n:>9,}{(n / bars_total if bars_total else 0):>11.2%}")
    print(f"\n  bar-evaluations: {bars_total:,}   authorised: {totals.get(STAGES[-1], 0):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
