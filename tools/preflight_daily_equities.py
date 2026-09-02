"""Pre-flight check for Phase 1: does daily equity data actually fix stops?

Run this the moment daily equity bars land, BEFORE re-running validation. It
answers one question — has the structural stop-rejection problem gone away —
and it answers it in under a minute rather than after a twenty-minute
validation run that produces two trades again.

The prediction it tests was made before the data existed, from a measurement
that isolates the bar interval. On the same crypto instruments, same 5% cap and
same code, only the interval changing:

    interval   median distance   beyond cap   median in sigma
    daily              3.83%          41%              1.25
    weekly             9.92%          73%              1.32

Structural support sits about 1.3 standard deviations below price whatever the
interval — that is the scale-free constant. What changes is the denominator: a
weekly sigma is sqrt(5) times a daily one, while the stop cap stays fixed in
percent, so the same cap is 2.2x tighter in sigma terms on weekly bars.

Applying that to equities, whose mean realised volatility is about 33%:

    weekly sigma = 33% / sqrt(52)  = 4.58%  ->  a 7% cap is 1.53 sigma
    daily  sigma = 33% / sqrt(252) = 2.08%  ->  a 7% cap is 3.37 sigma

The measured weekly rejection rate is 52%. Daily bars give more than twice the
headroom in sigma terms, so the prediction is that daily rejection falls well
below 25%. If it does not, daily data was not the blocker and the equity
strategies need a different diagnosis — do not proceed to re-validate on the
assumption that it worked.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradermaximus.core.types import AssetClass, Timeframe
from tradermaximus.data.csv_loader import load_directory
from tradermaximus.indicators.core import realized_volatility
from tradermaximus.indicators.engine import FeatureEngine
from tradermaximus.risk.limits import limits_for

#: The measured weekly baseline this is compared against.
WEEKLY_BEYOND_CAP = 0.52
#: Below this, daily data has done its job and validation is worth running.
ACCEPTANCE_BEYOND_CAP = 0.25


def measure(directory: Path, timeframe: Timeframe, periods_per_year: int, samples: int) -> dict:
    asset_class = AssetClass.EQUITIES
    cap = limits_for(asset_class).max_stop_pct
    engine = FeatureEngine()
    loads = [
        load for load in load_directory(
            directory, timeframe=timeframe, asset_class=asset_class, min_history=300
        ).values()
        if load.usable
    ]
    if not loads:
        raise SystemExit(f"no usable series in {directory}")

    distances: list[float] = []
    sigmas: list[float] = []
    for load in loads:
        series = load.series
        step = max(1, (len(series) - 320) // samples)
        for index in range(320, len(series), step):
            visible = series.slice_until(series.timestamps[index]).tail(400)
            features = engine.compute(visible)
            support = features.nearest_support()
            if support is None or features.close <= 0:
                continue
            distance = abs(features.close - support.price) / features.close
            distances.append(distance)
            vol = realized_volatility(visible.closes, 20, periods_per_year)
            if vol:
                sigmas.append(distance / (vol / periods_per_year ** 0.5))

    distances.sort()
    n = len(distances)
    return {
        "symbols": [load.series.instrument.symbol for load in loads],
        "bars": max(len(load.series) for load in loads),
        "cap": cap,
        "n": n,
        "median": distances[n // 2] if n else float("nan"),
        "p75": distances[int(n * 0.75)] if n else float("nan"),
        "beyond_cap": sum(1 for d in distances if d > cap) / n if n else float("nan"),
        "median_sigma": statistics.median(sigmas) if sigmas else float("nan"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default="data/real/equities_daily")
    parser.add_argument("--samples", type=int, default=200)
    args = parser.parse_args()

    result = measure(Path(args.data_dir), Timeframe.D1, 252, args.samples)

    print("PRE-FLIGHT: structural stop distance on daily equity bars\n")
    print(f"  symbols        {', '.join(result['symbols'])}")
    print(f"  longest series {result['bars']:,} bars")
    print(f"  max stop cap   {result['cap']:.0%}")
    print(f"  samples        {result['n']:,}\n")
    print(f"  median distance      {result['median']:.2%}")
    print(f"  75th percentile      {result['p75']:.2%}")
    print(f"  median in sigma      {result['median_sigma']:.2f}")
    print(f"  beyond the cap       {result['beyond_cap']:.0%}"
          f"   (weekly baseline {WEEKLY_BEYOND_CAP:.0%})\n")

    passed = result["beyond_cap"] < ACCEPTANCE_BEYOND_CAP
    if passed:
        print(f"  PASS — below the {ACCEPTANCE_BEYOND_CAP:.0%} acceptance bar. "
              "Daily data resolved the stop-placement blocker; re-run validation.")
        return 0
    print(f"  FAIL — still at or above the {ACCEPTANCE_BEYOND_CAP:.0%} acceptance bar.")
    print("  Daily data was not the blocker. Do NOT assume validation will now")
    print("  produce trades: diagnose the equity strategies directly before")
    print("  spending another validation cycle on them.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
