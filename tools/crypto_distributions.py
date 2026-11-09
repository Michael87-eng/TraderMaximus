"""Measure crypto's own distributions, so regime thresholds can be derived (Task 2a).

Every threshold in the current regime engine was inherited from equity and FX
defaults. This measures the distributions those thresholds are supposed to cut,
on the actual asset class, so the recalibration has a basis rather than a guess.

Run:  python tools/crypto_distributions.py
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradermaximus.core.types import AssetClass, Instrument, Timeframe
from tradermaximus.data.csv_loader import load_series
from tradermaximus.indicators import core as ind
from tradermaximus.indicators.engine import FeatureEngine

PCTS = (10, 25, 50, 75, 90)
VENUE = Path("data/venue/binance")
PAIRS = ("BTCUSDT", "ETHUSDT")


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def phase_durations(flags: list[bool]) -> list[int]:
    """Lengths of consecutive True runs."""
    runs, current = [], 0
    for f in flags:
        if f:
            current += 1
        elif current:
            runs.append(current)
            current = 0
    if current:
        runs.append(current)
    return runs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out", type=Path, default=Path("data/venue/crypto_distributions.json"))
    args = ap.parse_args()

    engine = FeatureEngine()
    out: dict[str, dict] = {}

    print("CRYPTO DISTRIBUTIONS — daily bars, Binance venue archive\n")
    for symbol in PAIRS:
        instrument = Instrument(symbol, AssetClass.CRYPTO, "BINANCE", "crypto", 0.01, 1e-8, 1e-8)
        result = load_series(VENUE / f"{symbol}_1d.csv", instrument,
                             timeframe=Timeframe.D1, min_history=100)
        if not result.usable:
            print(f"{symbol}: {result.summary()}")
            continue
        series = result.series
        closes, highs, lows = series.closes, series.highs, series.lows

        realized, atr_pct, adx, drawdown = [], [], [], []
        trend_up_flags, low_adx_flags = [], []
        peak = closes[0]

        atr_line = ind.atr(highs, lows, closes, 14)
        adx_line = ind.adx(highs, lows, closes, 14)

        for i in range(260, len(closes)):
            # 20-day realised volatility, annualised at 365 (crypto trades daily)
            rv = ind.realized_volatility(closes[: i + 1], 20, 365)
            if rv is not None:
                realized.append(rv)
            a = atr_line[i] if i < len(atr_line) else None
            if a is not None and closes[i] > 0:
                atr_pct.append(a / closes[i])
            d = adx_line[i] if i < len(adx_line) else None
            if d is not None:
                adx.append(d)
                low_adx_flags.append(d < 20.0)
            peak = max(peak, closes[i])
            drawdown.append((peak - closes[i]) / peak if peak > 0 else 0.0)
            ma200 = statistics.fmean(closes[i - 200:i]) if i >= 200 else None
            trend_up_flags.append(ma200 is not None and closes[i] > ma200)

        rows = {
            "realized_vol_20d_annualised": realized,
            "atr_14_pct_of_price": atr_pct,
            "adx_14": adx,
            "drawdown_from_rolling_peak": drawdown,
        }
        print(f"=== {symbol} — {len(closes):,} bars, "
              f"{series.timestamps[0].date()} .. {series.timestamps[-1].date()} ===")
        print(f"  {'measure':34}" + "".join(f"{f'p{p}':>10}" for p in PCTS) + f"{'n':>8}")
        symbol_out: dict[str, dict] = {}
        for name, values in rows.items():
            qs = {f"p{p}": pct(values, p) for p in PCTS}
            symbol_out[name] = {**qs, "n": len(values)}
            fmt = (lambda v: f"{v:>10.1%}") if name != "adx_14" else (lambda v: f"{v:>10.1f}")
            print(f"  {name:34}" + "".join(fmt(qs[f'p{p}']) for p in PCTS) + f"{len(values):>8}")

        up = phase_durations(trend_up_flags)
        down = phase_durations([not f for f in trend_up_flags])
        rng = phase_durations(low_adx_flags)
        print(f"\n  phase durations in bars (median / p75 / p90 / max):")
        for label, runs in (("above MA200", up), ("below MA200", down), ("ADX < 20", rng)):
            if runs:
                print(f"    {label:14}{pct(runs,50):>8.0f}{pct(runs,75):>8.0f}"
                      f"{pct(runs,90):>8.0f}{max(runs):>8.0f}   ({len(runs)} phases)")
                symbol_out[f"phase_{label.replace(' ','_').replace('<','lt')}"] = {
                    "median": pct(runs, 50), "p75": pct(runs, 75),
                    "p90": pct(runs, 90), "max": max(runs), "phases": len(runs),
                }
        print()
        out[symbol] = symbol_out

    # Pooled percentiles across both pairs — what a single threshold must cut.
    print("=== POOLED across BTC and ETH (what one threshold must serve) ===")
    pooled: dict[str, dict] = {}
    for measure in ("realized_vol_20d_annualised", "atr_14_pct_of_price",
                    "adx_14", "drawdown_from_rolling_peak"):
        combined: list[float] = []
        for symbol in out:
            # Re-derive from the per-symbol percentiles' n by reloading is
            # wasteful; instead pool the percentile estimates by averaging,
            # which is adequate for threshold selection and stated as such.
            pass
        vals = [out[s][measure] for s in out if measure in out[s]]
        if not vals:
            continue
        avg = {f"p{p}": statistics.fmean(v[f"p{p}"] for v in vals) for p in PCTS}
        pooled[measure] = avg
        fmt = (lambda v: f"{v:>10.1%}") if measure != "adx_14" else (lambda v: f"{v:>10.1f}")
        print(f"  {measure:34}" + "".join(fmt(avg[f'p{p}']) for p in PCTS))
    print("\n  (pooled rows are the mean of each pair's percentile, which is")
    print("   adequate for choosing a threshold and is not a pooled quantile)")

    args.json_out.write_text(json.dumps({"per_symbol": out, "pooled_mean_of_percentiles": pooled},
                                        indent=2))
    print(f"\nwritten: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
