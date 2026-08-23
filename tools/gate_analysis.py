"""Measure where the decision pipeline actually stops trades.

The system is a chain of conjunctions: regime eligibility, then every entry
condition, then risk authorisation. When it trades too rarely, the useful
question is not "is it too strict" but *which specific gate* is responsible —
and whether that gate is strict because the market genuinely does not qualify,
or because a threshold is miscalibrated for the data it sees.

This tool answers that by measuring, over many bars and several market
characters:

* the regime distribution, and how often each strategy *type* is eligible
* the **marginal** pass rate of every individual entry condition
* the **joint** trigger rate of each strategy
* which single condition most often blocks an otherwise-complete setup

Marginal rates are the important output. A conjunction of seven conditions
each passing 30% of the time fires 0.02% of the time even though no single
condition looks unreasonable. That arithmetic is invisible without measurement.

Run: ``python tools/gate_analysis.py``
"""

from __future__ import annotations

import math
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Sequence

sys.path.insert(0, ".")

from tradermaximus.core.types import AssetClass, Candle, Instrument, Timeframe
from tradermaximus.data.series import CandleSeries
from tradermaximus.indicators.engine import FeatureEngine, FeatureSet
from tradermaximus.regime.engine import (
    MarketContext,
    RegimeEngine,
    is_strategy_type_allowed,
    minimum_score,
)
from tradermaximus.strategy.library import ALL_STRATEGIES
from tradermaximus.strategy.spec import EvalContext, Fundamentals, StrategySpec

BASE = datetime(2018, 1, 1, tzinfo=timezone.utc)

FUNDAMENTALS = Fundamentals(
    eps_rating=92, rs_rating=88, industry_rank=12, earnings_growth=0.42,
    sales_growth=0.30, roe=0.27, acc_dis_rating="A", quarters_of_accumulation=3,
)


# ---------------------------------------------------------------------------
# Market generators — several characters, so calibration is not fitted to one
# ---------------------------------------------------------------------------


def _bar(index: int, price: float, vol: float, volume: float, rng: random.Random) -> Candle:
    return Candle(
        timestamp=BASE + timedelta(days=index),
        open=price * (1 + rng.gauss(0, vol / 4)),
        high=price * (1 + abs(rng.gauss(0, vol))),
        low=price * (1 - abs(rng.gauss(0, vol))),
        close=price,
        volume=max(1.0, volume),
    )


def steady_uptrend(n: int, seed: int) -> list[Candle]:
    """A trend that advances in waves with genuine pullbacks."""
    rng = random.Random(seed)
    out: list[Candle] = []
    price = 100.0
    for i in range(n):
        phase = 1.0 if (i % 55) < 40 else -0.7          # advance, then rest
        price = max(1.0, price * (1 + rng.gauss(0.0013 * phase, 0.012)))
        # Volume expands on advances and dries up in pullbacks, as real bases do.
        volume = 1_200_000 * (rng.uniform(0.95, 1.7) if phase > 0 else rng.uniform(0.45, 0.9))
        out.append(_bar(i, price, 0.012, volume, rng))
    return out


def choppy_range(n: int, seed: int) -> list[Candle]:
    """Mean-reverting sideways action."""
    rng = random.Random(seed)
    out: list[Candle] = []
    price = 100.0
    for i in range(n):
        pull = (100.0 - price) * 0.03
        price = max(1.0, price + pull + rng.gauss(0, 1.1))
        out.append(_bar(i, price, 0.011, 1_000_000 * rng.uniform(0.7, 1.3), rng))
    return out


def volatile_drift(n: int, seed: int) -> list[Candle]:
    """High volatility with a weak upward drift."""
    rng = random.Random(seed)
    out: list[Candle] = []
    price = 100.0
    for i in range(n):
        price = max(1.0, price * (1 + rng.gauss(0.0006, 0.026)))
        out.append(_bar(i, price, 0.026, 1_100_000 * rng.uniform(0.6, 1.8), rng))
    return out


def bear_market(n: int, seed: int) -> list[Candle]:
    rng = random.Random(seed)
    out: list[Candle] = []
    price = 200.0
    for i in range(n):
        price = max(1.0, price * (1 + rng.gauss(-0.0011, 0.016)))
        out.append(_bar(i, price, 0.016, 1_000_000 * rng.uniform(0.7, 1.5), rng))
    return out


GENERATORS: dict[str, Callable[[int, int], list[Candle]]] = {
    "uptrend": steady_uptrend,
    "range": choppy_range,
    "volatile": volatile_drift,
    "bear": bear_market,
}


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


@dataclass
class Tally:
    samples: int = 0
    regime_primary: Counter = field(default_factory=Counter)
    type_eligible: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    type_samples: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    condition_pass: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    condition_seen: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    strategy_triggered: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    strategy_evaluated: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    sole_blocker: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))


def analyse(
    *,
    bars: int = 700,
    warmup: int = 300,
    step: int = 2,
    seeds: Sequence[int] = (1, 2, 3),
    characters: Iterable[str] = ("uptrend", "range", "volatile", "bear"),
) -> Tally:
    features_engine = FeatureEngine()
    regime_engine = RegimeEngine()
    tally = Tally()
    instrument = Instrument("TEST", AssetClass.EQUITIES, "NASDAQ", "Technology", 0.01, 1.0, 1.0)

    for character in characters:
        generator = GENERATORS[character]
        for seed in seeds:
            candles = generator(bars, seed)
            benchmark_candles = generator(bars, seed + 500)
            full = CandleSeries.build(instrument, Timeframe.D1, candles)
            benchmark = CandleSeries.build(instrument, Timeframe.D1, benchmark_candles)

            for index in range(warmup, bars, step):
                when = full.timestamps[index]
                visible = full.slice_until(when).tail(400)
                bench_visible = benchmark.slice_until(when).tail(400)
                if len(visible) < 260:
                    continue

                features = features_engine.compute(visible)
                bench_features = features_engine.compute(bench_visible, detect_bases=False)
                regime = regime_engine.classify(
                    bench_features,
                    MarketContext(breadth_index=0.45, new_highs_ratio=0.60, correlation_change=0.15),
                    benchmark_closes=bench_visible.closes,
                )

                tally.samples += 1
                tally.regime_primary[regime.primary.value] += 1
                context = EvalContext(features=features, fundamentals=FUNDAMENTALS, sector_strength=0.02)

                for spec in ALL_STRATEGIES:
                    if not spec.supports(instrument.asset_class):
                        continue
                    tally.type_samples[spec.strategy_type] += 1
                    allowed, _ = is_strategy_type_allowed(spec.strategy_type, regime)
                    if allowed and minimum_score(regime) is not None:
                        tally.type_eligible[spec.strategy_type] += 1

                    # Marginal condition rates are measured regardless of regime
                    # eligibility, so the two gates can be separated.
                    _tally_conditions(tally, spec, context)

    return tally


def _tally_conditions(tally: Tally, spec: StrategySpec, context: EvalContext) -> None:
    results = [condition.evaluate(context) for condition in spec.entry_conditions]
    tally.strategy_evaluated[spec.id] += 1

    failed = [r for r in results if not r.passed]
    for result in results:
        key = (spec.id, result.name)
        tally.condition_seen[key] += 1
        if result.passed:
            tally.condition_pass[key] += 1

    if not failed:
        tally.strategy_triggered[spec.id] += 1
    elif len(failed) == 1:
        # The setup was otherwise complete: this one condition alone blocked it.
        tally.sole_blocker[spec.id][failed[0].name] += 1


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def report(tally: Tally) -> None:
    print(f"Samples: {tally.samples:,} bar-observations\n")

    print("=" * 78)
    print("REGIME DISTRIBUTION")
    print("=" * 78)
    for name, count in tally.regime_primary.most_common():
        print(f"  {name:22} {count:6,}  {count / tally.samples:6.1%}")

    print()
    print("=" * 78)
    print("STRATEGY-TYPE ELIGIBILITY (regime gate alone)")
    print("=" * 78)
    for stype in sorted(tally.type_samples):
        seen = tally.type_samples[stype]
        ok = tally.type_eligible.get(stype, 0)
        print(f"  {stype:18} eligible {ok / seen:6.1%}  ({ok:,}/{seen:,})")

    print()
    print("=" * 78)
    print("PER-CONDITION MARGINAL PASS RATES  (independent of regime)")
    print("=" * 78)
    for spec in ALL_STRATEGIES:
        evaluated = tally.strategy_evaluated.get(spec.id, 0)
        if not evaluated:
            continue
        triggered = tally.strategy_triggered.get(spec.id, 0)
        joint = triggered / evaluated

        # Expected rate if the conditions were independent, for comparison.
        rates = []
        for condition in spec.entry_conditions:
            key = (spec.id, condition.name)
            seen = tally.condition_seen.get(key, 0)
            rates.append((tally.condition_pass.get(key, 0) / seen) if seen else 0.0)
        independent = math.prod(rates) if rates else 0.0

        print(f"\n  {spec.id}  {spec.name}")
        print(f"    joint trigger rate: {joint:.3%}   (independent estimate {independent:.3%})")
        for condition, rate in zip(spec.entry_conditions, rates):
            marker = "  <-- BOTTLENECK" if rate < 0.05 else ("  <- tight" if rate < 0.15 else "")
            print(f"      {rate:7.2%}  {condition.name}{marker}")

        blockers = tally.sole_blocker.get(spec.id)
        if blockers:
            top = blockers.most_common(3)
            joined = ", ".join(f"{name} ({count})" for name, count in top)
            print(f"    sole blocker of otherwise-complete setups: {joined}")


def main() -> int:
    tally = analyse()
    report(tally)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
