"""Gate autopsy: a sequential funnel through the decision pipeline.

`gate_analysis.py` measures each condition *marginally* — how often it passes
on its own. This tool measures them *sequentially*, in pipeline order, so each
line answers "of the bars still alive, how many die here". That is the only
view that identifies the true bottleneck: a condition with a poor marginal rate
may be irrelevant if an earlier gate already removed those bars.

The funnel mirrors the live pipeline order:

    all bars
      -> regime tradeable          (LIQUIDITY_STRESS / UNKNOWN halt everything)
      -> strategy type eligible    (section 5.4 eligibility matrix)
      -> each entry condition      (in the order the spec lists them)
      -> signal score >= regime minimum   (section 7.2)

Run: ``python tools/gate_autopsy.py [--bars 640] [--strategy STRATEGY_001]``
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Sequence

sys.path.insert(0, ".")

from tools.market_sim import CHARACTERS, make

from pathlib import Path

from tradermaximus.core.types import AssetClass, Instrument, Timeframe
from tradermaximus.data.csv_loader import load_directory
from tradermaximus.data.series import CandleSeries
from tradermaximus.indicators.engine import FeatureEngine
from tradermaximus.regime.engine import (
    MarketContext,
    RegimeEngine,
    is_strategy_type_allowed,
    minimum_score,
)
from tradermaximus.strategy.library import ALL_STRATEGIES, STRATEGIES_BY_ID
from tradermaximus.strategy.scoring import ScoringInputs, SignalScoringEngine
from tradermaximus.strategy.spec import EvalContext, Fundamentals, StrategySpec

FUNDAMENTALS = Fundamentals(
    eps_rating=92, rs_rating=88, industry_rank=12, earnings_growth=0.42,
    sales_growth=0.30, roe=0.27, acc_dis_rating="A", quarters_of_accumulation=3,
)


@dataclass
class Funnel:
    """Survivor counts through an ordered sequence of gates."""

    strategy_id: str
    total_bars: int = 0
    stages: list[tuple[str, int, int]] = field(default_factory=list)  # (name, killed, survivors)
    scores: list[float] = field(default_factory=list)
    thresholds: list[float] = field(default_factory=list)
    signals: int = 0

    def render(self) -> str:
        lines = [f"  {'GATE':44} {'killed':>8} {'survive':>8}  {'kill %':>7}"]
        lines.append(f"  {'-' * 44} {'-' * 8} {'-' * 8}  {'-' * 7}")
        lines.append(f"  {'bars examined':44} {'':>8} {self.total_bars:8,}")
        worst = ("", 0)
        for name, killed, survivors in self.stages:
            entering = killed + survivors
            share = killed / entering if entering else 0.0
            if killed > worst[1]:
                worst = (name, killed)
            flag = ""
            lines.append(f"  {name:44} {killed:8,} {survivors:8,}  {share:7.1%}{flag}")
        if worst[0]:
            lines.append(f"\n  #1 killer: {worst[0]} ({worst[1]:,} bars)")
        return "\n".join(lines)


#: Bar interval per asset-class directory, matching validate_real_data.py.
REAL_TIMEFRAMES = {
    "equities": Timeframe.W1,
    "crypto": Timeframe.D1,
    "forex": Timeframe.D1,
}

REAL_ASSET_CLASSES = {
    "equities": AssetClass.EQUITIES,
    "crypto": AssetClass.CRYPTO,
    "forex": AssetClass.FOREX,
}


def real_series(label: str, root: Path, *, warmup: int) -> list[tuple[CandleSeries, CandleSeries]]:
    """Load real history as (symbol series, benchmark series) pairs.

    The benchmark is the longest series in the directory, which is what the
    validation harness uses, so the regime the autopsy sees is the regime the
    backtest saw.
    """
    directory = root / label
    loaded = load_directory(
        directory,
        timeframe=REAL_TIMEFRAMES[label],
        asset_class=REAL_ASSET_CLASSES[label],
        min_history=warmup,
    )
    usable = [load.series for load in loaded.values() if load.usable]
    if not usable:
        return []
    benchmark = max(usable, key=len)
    return [(series, benchmark) for series in usable]


def run_funnel(
    spec: StrategySpec,
    *,
    bars: int = 640,
    warmup: int = 300,
    seeds: Sequence[int] = (1, 2, 3, 4),
    characters: Sequence[str] = CHARACTERS,
    score_gate: bool = True,
    series: Sequence[tuple[CandleSeries, CandleSeries]] | None = None,
) -> Funnel:
    engine = FeatureEngine()
    regime_engine = RegimeEngine()
    scorer = SignalScoringEngine()
    instrument = Instrument("TEST", AssetClass.EQUITIES, "NASDAQ", "Technology", 0.01, 1.0, 1.0)

    funnel = Funnel(spec.id)
    names = ["regime tradeable", f"strategy type eligible ({spec.strategy_type})"]
    names += [c.name for c in spec.entry_conditions]
    if score_gate:
        names.append("signal score >= regime minimum")
    killed = [0] * len(names)
    entering = [0] * len(names)

    if series is None:
        series = [
            (
                CandleSeries.build(instrument, Timeframe.D1, make(character, bars + warmup, seed)),
                CandleSeries.build(
                    instrument, Timeframe.D1, make("benchmark", bars + warmup, seed + 500)
                ),
            )
            for character in characters
            for seed in seeds
        ]

    if True:
        for full, bench in series:
            instrument = full.instrument

            for index in range(warmup, len(full)):
                when = full.timestamps[index]
                visible = full.slice_until(when).tail(400)
                bench_visible = bench.slice_until(when).tail(400)
                if len(visible) < 260:
                    continue

                funnel.total_bars += 1
                features = engine.compute(visible)
                bench_features = engine.compute(bench_visible, detect_bases=False)
                regime = regime_engine.classify(
                    bench_features,
                    MarketContext(breadth_index=0.45, new_highs_ratio=0.60, correlation_change=0.15),
                    benchmark_closes=bench_visible.closes,
                )
                context = EvalContext(
                    features=features, fundamentals=FUNDAMENTALS, sector_strength=0.02
                )

                stage = 0
                # -- gate 1: regime tradeable at all --------------------------
                entering[stage] += 1
                threshold = minimum_score(regime)
                if not regime.is_tradeable or threshold is None:
                    killed[stage] += 1
                    continue
                stage += 1

                # -- gate 2: strategy type eligible in this regime ------------
                entering[stage] += 1
                allowed, _ = is_strategy_type_allowed(spec.strategy_type, regime)
                if not allowed:
                    killed[stage] += 1
                    continue
                stage += 1

                # -- gates 3..n: entry conditions, in spec order --------------
                blocked = False
                for condition in spec.entry_conditions:
                    entering[stage] += 1
                    if not condition.evaluate(context).passed:
                        killed[stage] += 1
                        blocked = True
                        break
                    stage += 1
                if blocked:
                    continue

                # -- final gate: signal score vs regime minimum ---------------
                if score_gate:
                    entering[stage] += 1
                    evaluation = spec.evaluate(context)
                    score = scorer.score(
                        spec=spec, evaluation=evaluation, features=features, regime=regime,
                        context=context,
                        inputs=ScoringInputs(
                            average_daily_volume=features.number("avg_volume_50"),
                            spread_bps=6.0, relative_strength_rating=88,
                            sector_strength=0.02, risk_reward_ratio=2.6,
                            historical_win_rate=0.52,
                        ),
                    )
                    funnel.scores.append(score.total)
                    funnel.thresholds.append(threshold)
                    if score.total < threshold:
                        killed[stage] += 1
                        continue
                funnel.signals += 1

    survivors_running = funnel.total_bars
    for i, name in enumerate(names):
        survivors_running = entering[i] - killed[i] if entering[i] else 0
        funnel.stages.append((name, killed[i], survivors_running))
    return funnel


def main() -> int:
    parser = argparse.ArgumentParser(description="Sequential gate autopsy")
    parser.add_argument("--bars", type=int, default=640)
    parser.add_argument("--strategy", default=None, help="limit to one strategy id")
    parser.add_argument(
        "--real",
        choices=sorted(REAL_TIMEFRAMES),
        help="run against real market history in data/real/<class> instead of the generator",
    )
    parser.add_argument("--data-dir", default="data/real")
    parser.add_argument("--warmup", type=int, default=300)
    args = parser.parse_args()

    real = None
    if args.real:
        real = real_series(args.real, Path(args.data_dir), warmup=args.warmup)
        if not real:
            print(f"no usable series in {args.data_dir}/{args.real}")
            return 1
        asset_class = REAL_ASSET_CLASSES[args.real]
        print(f"real data: {args.real} — {len(real)} symbols, "
              f"{sum(len(f) for f, _ in real):,} bars total\n")
    else:
        asset_class = AssetClass.EQUITIES

    specs = [STRATEGIES_BY_ID[args.strategy]] if args.strategy else list(ALL_STRATEGIES)
    specs = [s for s in specs if asset_class in s.asset_classes]

    summary: list[tuple[str, int, int, str]] = []
    for spec in specs:
        funnel = run_funnel(spec, bars=args.bars, warmup=args.warmup, series=real)
        print("=" * 74)
        print(f"{spec.id}  {spec.name}")
        print("=" * 74)
        print(funnel.render())
        per_640 = funnel.signals / funnel.total_bars * 640 if funnel.total_bars else 0.0
        print(f"\n  SIGNALS: {funnel.signals:,} over {funnel.total_bars:,} bars "
              f"= {per_640:.1f} per 640 bars")
        if funnel.scores:
            scores = sorted(funnel.scores)
            median = scores[len(scores) // 2]
            print(f"  score of setups reaching the gate: median {median:.1f}, "
                  f"min {scores[0]:.1f}, max {scores[-1]:.1f}, threshold ~{funnel.thresholds[0]:.0f}")
        worst = max(funnel.stages, key=lambda s: s[1]) if funnel.stages else ("", 0, 0)
        summary.append((spec.id, funnel.signals, round(per_640), worst[0]))
        print()

    print("=" * 74)
    print("SUMMARY")
    print("=" * 74)
    print(f"  {'strategy':14} {'signals':>8} {'per 640 bars':>13}  #1 killer")
    for sid, signals, per_640, killer in summary:
        print(f"  {sid:14} {signals:8,} {per_640:13}  {killer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
