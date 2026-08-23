"""Multi-asset validation on real market history.

Runs the section 20 validation pipeline separately for equities, crypto and
FOREX, because the interesting questions are per-asset-class: does the FOREX
mean-reversion spec actually work on FX, and does volatility scaling shrink
crypto positions enough to survive crypto volatility?

For each asset class:

* **In-sample** — the first 70% of history, chronologically.
* **Out-of-sample** — the remaining 30%, never used for any decision.
* **Walk-forward** — rolling train/test windows across the whole span.
* **Monte Carlo** — bootstrap resampling of the realised trades.

The split is strictly chronological. Shuffling before splitting would leak
future information into the in-sample period and produce an out-of-sample
result that means nothing.

Usage::

    python tools/validate_real_data.py                  # all asset classes
    python tools/validate_real_data.py --asset crypto   # one class
    python tools/validate_real_data.py --data-dir data/real

Expects CSVs under ``<data-dir>/<asset_class>/<SYMBOL>.csv`` with at least
timestamp, open, high, low, close columns (volume strongly preferred — several
strategies gate on it).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradermaximus.core.types import AssetClass, Candle, Instrument, TradeRecord
from tradermaximus.data.csv_loader import LoadResult, load_directory
from tradermaximus.indicators.core import realized_volatility
from tradermaximus.observability.metrics import PerformanceMetrics, compute_metrics
from tradermaximus.regime.engine import MarketContext
from tradermaximus.research.backtest import BacktestConfig, Backtester, BacktestResult
from tradermaximus.research.validation import (
    AcceptanceCriteria,
    StrategyValidator,
    monte_carlo,
)
from tradermaximus.risk.limits import limits_for

#: The gate the user set for opening a pull request.
OOS_MIN_PROFIT_FACTOR = 1.3
OOS_MIN_SHARPE = 0.7

ASSET_DIRS: Mapping[str, AssetClass] = {
    "equities": AssetClass.EQUITIES,
    "crypto": AssetClass.CRYPTO,
    "forex": AssetClass.FOREX,
}


@dataclass
class SegmentResult:
    name: str
    result: BacktestResult | None = None
    error: str = ""

    @property
    def metrics(self) -> PerformanceMetrics:
        return self.result.metrics if self.result else PerformanceMetrics()

    @property
    def ok(self) -> bool:
        return self.result is not None


@dataclass
class AssetClassReport:
    asset_class: AssetClass
    symbols: list[str] = field(default_factory=list)
    bars: int = 0
    loaded: dict[str, str] = field(default_factory=dict)
    in_sample: SegmentResult | None = None
    out_of_sample: SegmentResult | None = None
    walk_forward: list[PerformanceMetrics] = field(default_factory=list)
    monte_carlo_positive: float | None = None
    volatility_profile: dict[str, float] = field(default_factory=dict)
    strategy_breakdown: dict[str, dict[str, float]] = field(default_factory=dict)
    skipped: str = ""

    @property
    def passes_gate(self) -> bool:
        """The user's OOS gate: PF > 1.3 and Sharpe > 0.7."""
        if not (self.out_of_sample and self.out_of_sample.ok):
            return False
        m = self.out_of_sample.metrics
        if m.trade_count == 0:
            return False
        return m.profit_factor > OOS_MIN_PROFIT_FACTOR and m.sharpe > OOS_MIN_SHARPE


def _split(candles: Sequence[Candle], fraction: float) -> tuple[list[Candle], list[Candle]]:
    """Chronological split. Never shuffle before splitting."""
    cut = int(len(candles) * fraction)
    return list(candles[:cut]), list(candles[cut:])


def _run_segment(
    name: str,
    universe: Mapping[Instrument, Sequence[Candle]],
    benchmark: Sequence[Candle] | None,
    *,
    warmup: int,
    equity: float,
) -> SegmentResult:
    usable = {i: list(c) for i, c in universe.items() if len(c) > warmup + 30}
    if not usable:
        return SegmentResult(name, error=f"no symbol has more than {warmup + 30} bars")
    try:
        backtester = Backtester(
            BacktestConfig(starting_equity=equity, warmup_bars=warmup, seed=7)
        )
        result = backtester.run(
            usable,
            benchmark=list(benchmark) if benchmark else None,
            market_context=MarketContext(
                breadth_index=0.5, new_highs_ratio=0.55, correlation_change=0.15
            ),
        )
        return SegmentResult(name, result=result)
    except Exception as exc:  # noqa: BLE001 - report rather than abort the whole run
        return SegmentResult(name, error=f"{type(exc).__name__}: {exc}")


def validate_asset_class(
    label: str,
    directory: Path,
    *,
    equity: float,
    warmup: int,
    in_sample_fraction: float,
    walk_forward_windows: int,
) -> AssetClassReport:
    asset_class = ASSET_DIRS[label]
    report = AssetClassReport(asset_class=asset_class)

    if not directory.is_dir() or not any(directory.glob("*.csv")):
        report.skipped = f"no CSV files in {directory}"
        return report

    loaded: dict[Instrument, LoadResult] = load_directory(
        directory, asset_class=asset_class, min_history=warmup
    )
    universe: dict[Instrument, list[Candle]] = {}
    for instrument, load in loaded.items():
        report.loaded[instrument.symbol] = load.summary()
        if not load.usable:
            continue
        universe[instrument] = list(load.series.candles)
        report.symbols.append(instrument.symbol)
        report.bars = max(report.bars, len(load.series))
        vol = realized_volatility(
            load.series.closes, 20, 365 if asset_class is AssetClass.CRYPTO else 252
        )
        if vol is not None:
            report.volatility_profile[instrument.symbol] = vol

    if not universe:
        report.skipped = "no symbol passed data-quality validation"
        return report

    benchmark = max(universe.values(), key=len)

    # -- in-sample / out-of-sample, chronological -------------------------
    in_universe: dict[Instrument, list[Candle]] = {}
    out_universe: dict[Instrument, list[Candle]] = {}
    for instrument, candles in universe.items():
        head, tail = _split(candles, in_sample_fraction)
        in_universe[instrument] = head
        # The out-of-sample segment carries the warm-up bars it needs, taken
        # from history that precedes it — indicators need history, but no
        # decision is ever made on an in-sample bar.
        out_universe[instrument] = candles[max(0, len(head) - warmup):]

    bench_head, _ = _split(benchmark, in_sample_fraction)
    bench_tail = benchmark[max(0, len(bench_head) - warmup):]

    report.in_sample = _run_segment(
        "in_sample", in_universe, bench_head, warmup=warmup, equity=equity
    )
    report.out_of_sample = _run_segment(
        "out_of_sample", out_universe, bench_tail, warmup=warmup, equity=equity
    )

    # -- walk-forward ------------------------------------------------------
    longest = len(benchmark)
    window = longest // max(1, walk_forward_windows + 1)
    if window > warmup + 60:
        for index in range(walk_forward_windows):
            start = index * window
            end = start + window * 2
            slice_universe = {
                inst: candles[start:end]
                for inst, candles in universe.items()
                if len(candles[start:end]) > warmup + 30
            }
            if not slice_universe:
                continue
            segment = _run_segment(
                f"wf_{index}", slice_universe, benchmark[start:end], warmup=warmup, equity=equity
            )
            if segment.ok:
                report.walk_forward.append(segment.metrics)

    # -- Monte Carlo on the combined trade set -----------------------------
    trades: list[TradeRecord] = []
    for segment in (report.in_sample, report.out_of_sample):
        if segment and segment.ok:
            trades.extend(segment.result.trades)
    simulation = monte_carlo(trades, starting_equity=equity)
    if simulation is not None:
        report.monte_carlo_positive = simulation.positive_fraction

    # -- per-strategy breakdown -------------------------------------------
    by_strategy: dict[str, list[TradeRecord]] = {}
    for trade in trades:
        by_strategy.setdefault(trade.strategy_id, []).append(trade)
    for strategy_id, strategy_trades in sorted(by_strategy.items()):
        metrics = compute_metrics(strategy_trades)
        report.strategy_breakdown[strategy_id] = {
            "trades": metrics.trade_count,
            "win_rate": metrics.win_rate,
            "profit_factor": metrics.profit_factor,
            "expectancy": metrics.expectancy,
            "avg_r": metrics.avg_r_multiple,
        }
    return report


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _fmt(value: float, spec: str = ".2f") -> str:
    if value in (float("inf"), float("-inf")):
        return "inf"
    return format(value, spec)


def print_report(report: AssetClassReport) -> None:
    name = report.asset_class.value.upper()
    print("=" * 78)
    print(f"  {name}")
    print("=" * 78)

    if report.skipped:
        print(f"  SKIPPED: {report.skipped}\n")
        return

    print(f"  symbols: {', '.join(report.symbols) or 'none usable'}")
    for symbol, summary in sorted(report.loaded.items()):
        print(f"    {summary}")

    if report.volatility_profile:
        vols = list(report.volatility_profile.values())
        limits = limits_for(report.asset_class)
        mean_vol = statistics.fmean(vols)
        band = limits.risk_per_trade(mean_vol)
        print(f"\n  realised volatility (annualised): "
              f"{', '.join(f'{s} {v:.0%}' for s, v in sorted(report.volatility_profile.items()))}")
        print(f"  -> mean {mean_vol:.0%} selects the {band:.2%} risk-per-trade band "
              f"(volatility scaling then shrinks size further)")

    print(f"\n  {'segment':16}{'trades':>8}{'win rate':>10}{'profit factor':>15}{'sharpe':>9}{'max DD':>9}{'return':>9}")
    for segment in (report.in_sample, report.out_of_sample):
        if segment is None:
            continue
        if not segment.ok:
            print(f"  {segment.name:16}{'ERROR':>8}  {segment.error}")
            continue
        m = segment.metrics
        print(f"  {segment.name:16}{m.trade_count:8}{m.win_rate:10.1%}"
              f"{_fmt(m.profit_factor):>15}{m.sharpe:9.2f}{m.max_drawdown:9.1%}{m.return_pct:9.1%}")

    if report.walk_forward:
        profitable = sum(1 for m in report.walk_forward if m.total_pnl > 0)
        print(f"\n  walk-forward: {profitable}/{len(report.walk_forward)} windows profitable; "
              f"mean PF {_fmt(statistics.fmean([m.profit_factor for m in report.walk_forward if m.profit_factor != float('inf')] or [0.0]))}")
    else:
        print("\n  walk-forward: not enough history for multiple windows")

    if report.monte_carlo_positive is not None:
        print(f"  monte carlo (bootstrap): {report.monte_carlo_positive:.1%} of resamples profitable "
              f"(need >= 95%)")

    if report.strategy_breakdown:
        print(f"\n  {'strategy':16}{'trades':>8}{'win rate':>10}{'PF':>8}{'avg R':>8}")
        for strategy_id, stats in report.strategy_breakdown.items():
            print(f"  {strategy_id:16}{int(stats['trades']):8}{stats['win_rate']:10.1%}"
                  f"{_fmt(stats['profit_factor']):>8}{stats['avg_r']:8.2f}")

    gate = "PASS" if report.passes_gate else "FAIL"
    oos = report.out_of_sample.metrics if report.out_of_sample and report.out_of_sample.ok else None
    detail = ""
    if oos:
        detail = (f"  (OOS PF {_fmt(oos.profit_factor)} vs >{OOS_MIN_PROFIT_FACTOR}, "
                  f"Sharpe {oos.sharpe:.2f} vs >{OOS_MIN_SHARPE}, {oos.trade_count} trades)")
    print(f"\n  OOS DEPLOYMENT GATE: {gate}{detail}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate on real market history")
    parser.add_argument("--data-dir", default="data/real")
    parser.add_argument("--asset", choices=sorted(ASSET_DIRS), action="append")
    parser.add_argument("--equity", type=float, default=100_000.0)
    parser.add_argument("--warmup", type=int, default=300)
    parser.add_argument("--in-sample", type=float, default=0.70, dest="in_sample")
    parser.add_argument("--wf-windows", type=int, default=3, dest="wf_windows")
    parser.add_argument("--json", action="store_true", help="emit machine-readable summary")
    args = parser.parse_args()

    root = Path(args.data_dir)
    labels = args.asset or list(ASSET_DIRS)

    print("REAL-MARKET VALIDATION")
    print(f"data root: {root.resolve()}")
    print(f"in-sample {args.in_sample:.0%} / out-of-sample {1 - args.in_sample:.0%}, "
          f"chronological split, {args.warmup}-bar warm-up\n")

    reports: list[AssetClassReport] = []
    for label in labels:
        report = validate_asset_class(
            label,
            root / label,
            equity=args.equity,
            warmup=args.warmup,
            in_sample_fraction=args.in_sample,
            walk_forward_windows=args.wf_windows,
        )
        reports.append(report)
        print_report(report)

    print("=" * 78)
    print("  SUMMARY")
    print("=" * 78)
    print(f"  {'asset class':14}{'OOS trades':>12}{'OOS PF':>10}{'OOS Sharpe':>12}{'OOS win':>10}{'gate':>8}")
    any_data = False
    for report in reports:
        if report.skipped:
            print(f"  {report.asset_class.value:14}{'no data':>12}")
            continue
        any_data = True
        oos = report.out_of_sample.metrics if report.out_of_sample and report.out_of_sample.ok else PerformanceMetrics()
        print(f"  {report.asset_class.value:14}{oos.trade_count:12}{_fmt(oos.profit_factor):>10}"
              f"{oos.sharpe:12.2f}{oos.win_rate:10.1%}{'PASS' if report.passes_gate else 'FAIL':>8}")

    if not any_data:
        print("\n  No real data found. Place CSVs under:")
        for label in ASSET_DIRS:
            print(f"    {root / label}/<SYMBOL>.csv")
        print("\n  Required columns: timestamp, open, high, low, close (volume strongly preferred).")
        return 2

    passing = [r for r in reports if r.passes_gate]
    print(f"\n  {len(passing)}/{len([r for r in reports if not r.skipped])} asset classes clear "
          f"PF > {OOS_MIN_PROFIT_FACTOR} and Sharpe > {OOS_MIN_SHARPE} out-of-sample.")
    if not passing:
        print("  No asset class clears the gate. Do not deploy; diagnose per strategy above.")

    if args.json:
        print("\n" + json.dumps(
            {
                r.asset_class.value: {
                    "skipped": r.skipped,
                    "symbols": r.symbols,
                    "oos": r.out_of_sample.metrics.as_dict() if r.out_of_sample and r.out_of_sample.ok else None,
                    "in_sample": r.in_sample.metrics.as_dict() if r.in_sample and r.in_sample.ok else None,
                    "passes_gate": r.passes_gate,
                }
                for r in reports
            },
            indent=2,
        ))
    return 0 if passing else 1


if __name__ == "__main__":
    raise SystemExit(main())
