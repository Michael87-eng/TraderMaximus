"""Before-and-after regime distribution and veto rates (Tasks 2c, 2e).

Runs the identical bars through the inherited DEFAULT_PROFILE and the
recalibrated CRYPTO_PROFILE and reports what changed. The point is to show
whether the recalibration removed a mis-specified filter or simply removed a
constraint — those look identical in a trade count and different here.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradermaximus.core.types import AssetClass, Instrument, Timeframe
from tradermaximus.data.csv_loader import load_series
from tradermaximus.indicators.engine import FeatureEngine
from tradermaximus.regime.engine import (
    CRYPTO_PROFILE,
    DEFAULT_PROFILE,
    MarketContext,
    RegimeEngine,
    family_size_multiplier,
    is_strategy_type_allowed,
    minimum_score,
)

FAMILIES = ("momentum", "breakout", "pullback", "reversal", "mean_reversion", "institutional")
VENUE = Path("data/venue/binance")


def run(symbol: str, profile) -> tuple[Counter, dict[str, Counter], int, dict[str, list[float]]]:
    inst = Instrument(symbol, AssetClass.CRYPTO, "BINANCE", "crypto", 0.01, 1e-8, 1e-8)
    res = load_series(VENUE / f"{symbol}_1d.csv", inst, timeframe=Timeframe.D1, min_history=100)
    series = res.series
    eng, feat = RegimeEngine(profile=profile), FeatureEngine()
    primary: Counter = Counter()
    vetoed = {f: Counter() for f in FAMILIES}
    sizes: dict[str, list[float]] = {f: [] for f in FAMILIES}
    notrade = 0

    for i in range(320, len(series)):
        now = series.timestamps[i]
        v = series.slice_until(now).tail(400)
        f = feat.compute(v, detect_bases=False)
        st = eng.classify(f, MarketContext(breadth_index=0.5, new_highs_ratio=0.55,
                                           correlation_change=0.15),
                          benchmark_closes=v.closes)
        primary[st.primary.value] += 1
        if minimum_score(st) is None:
            notrade += 1
        for fam in FAMILIES:
            ok, _ = is_strategy_type_allowed(fam, st)
            if not ok:
                vetoed[fam][st.primary.value] += 1
            else:
                sizes[fam].append(family_size_multiplier(fam, st))
    return primary, vetoed, notrade, sizes


def main() -> int:
    for symbol in ("BTCUSDT", "ETHUSDT"):
        print("=" * 78)
        print(f"  {symbol}")
        print("=" * 78)
        results = {}
        for label, profile in (("BEFORE (default)", DEFAULT_PROFILE),
                               ("AFTER  (crypto)", CRYPTO_PROFILE)):
            results[label] = run(symbol, profile)

        total = sum(results["BEFORE (default)"][0].values())
        print(f"\n  REGIME DISTRIBUTION ({total:,} classified bars)")
        regimes = sorted(
            set(results["BEFORE (default)"][0]) | set(results["AFTER  (crypto)"][0])
        )
        print(f"  {'regime':22}{'before':>12}{'after':>12}{'change':>12}")
        for r in regimes:
            b = results["BEFORE (default)"][0].get(r, 0)
            a = results["AFTER  (crypto)"][0].get(r, 0)
            print(f"  {r:22}{b/total:>11.2%}{a/total:>12.2%}{(a-b)/total:>+12.2%}")

        for label in results:
            nt = results[label][2]
            print(f"  {'permits NO trading':22}{'':>12}" if label.startswith("AFTER") else "", end="")
        print()
        nb = results["BEFORE (default)"][2] / total
        na = results["AFTER  (crypto)"][2] / total
        print(f"  {'permits NO trading':22}{nb:>11.2%}{na:>12.2%}{na-nb:>+12.2%}")

        print(f"\n  FAMILY VETO RATE, and mean size when permitted")
        print(f"  {'family':16}{'veto before':>13}{'veto after':>12}{'change':>10}{'mean size':>11}")
        for fam in FAMILIES:
            vb = sum(results["BEFORE (default)"][1][fam].values()) / total
            va = sum(results["AFTER  (crypto)"][1][fam].values()) / total
            sz = results["AFTER  (crypto)"][3][fam]
            mean_sz = (sum(sz) / len(sz)) if sz else float("nan")
            print(f"  {fam:16}{vb:>12.2%}{va:>12.2%}{va-vb:>+10.2%}{mean_sz:>11.2f}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
