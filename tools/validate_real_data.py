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

from tradermaximus.core.types import AssetClass, Candle, Instrument, Timeframe, TradeRecord
from tradermaximus.data.csv_loader import LoadResult, load_directory
from tradermaximus.indicators.core import realized_volatility
from tradermaximus.observability.metrics import PerformanceMetrics, compute_metrics
from tradermaximus.regime.engine import MarketContext
from tradermaximus.research.backtest import BacktestConfig, Backtester, BacktestResult
from tradermaximus.research.validation import (
    AcceptanceCriteria,
    Fold,
    StrategyValidator,
    monte_carlo,
    plan_walk_forward_folds,
)
from tradermaximus.risk.limits import limits_for

#: The gates that must all hold before deployment.
CRITERIA = AcceptanceCriteria()
OOS_MIN_PROFIT_FACTOR = CRITERIA.min_profit_factor_oos      # 1.3
OOS_MIN_SHARPE = 0.7
MAX_DRAWDOWN = CRITERIA.max_drawdown                        # 5%
WF_FOLDS = CRITERIA.required_walk_forward_folds             # 5
WF_MIN_WINDOW_PF = CRITERIA.min_walk_forward_window_pf      # 1.3 per window
MC_SIMULATIONS = CRITERIA.monte_carlo_simulations           # 5000
MC_MIN_PROFITABLE = CRITERIA.min_monte_carlo_profitable     # 90%
#: OOS Sharpe must retain this fraction of in-sample Sharpe, or the strategy
#: is judged to have decayed out of sample and is disabled rather than tuned.
MIN_SHARPE_RETENTION = CRITERIA.min_oos_sharpe_ratio        # 80%

ASSET_DIRS: Mapping[str, AssetClass] = {
    "equities": AssetClass.EQUITIES,
    "crypto": AssetClass.CRYPTO,
    "forex": AssetClass.FOREX,
}

#: Bar interval and annualisation factor per asset class.
#:
#: Alpha Vantage's free tier gates daily equity history behind a premium plan
#: (only 100 bars), so equities are validated on *weekly* bars while crypto and
#: FX use daily. The annualisation factor must follow the bar interval or the
#: Sharpe comparison across classes is meaningless.
TIMEFRAMES: Mapping[str, tuple[Timeframe, int]] = {
    "equities": (Timeframe.W1, 52),
    "crypto": (Timeframe.D1, 365),   # crypto trades every calendar day
    "forex": (Timeframe.D1, 252),
}

#: Walk-forward fold geometry: (train bars, test bars) for "three years train,
#: one year test" expressed in each class's own bar interval. Equities are
#: weekly, so three years is 156 bars — fewer than an MA200 needs, which is why
#: the planner also takes the indicator warm-up as a floor.
WF_GEOMETRY: Mapping[str, tuple[int, int]] = {
    "equities": (156, 52),      # 3 years / 1 year of weekly bars
    "crypto": (1095, 365),      # crypto trades every calendar day
    "forex": (756, 252),        # 252 trading days a year
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
    folds: list[Fold] = field(default_factory=list)
    fold_note: str = ""
    monte_carlo_positive: float | None = None
    monte_carlo_simulations: int = 0
    signals_generated: int = 0
    signals_rejected: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    volatility_profile: dict[str, float] = field(default_factory=dict)
    timeframe: str = "1d"
    periods_per_year: int = 252
    strategy_breakdown: dict[str, dict[str, float]] = field(default_factory=dict)
    skipped: str = ""

    # -- individual gates ---------------------------------------------------

    @property
    def gate_oos(self) -> tuple[bool, str]:
        """Out-of-sample profit factor and Sharpe."""
        if not (self.out_of_sample and self.out_of_sample.ok):
            return False, "no out-of-sample result"
        m = self.out_of_sample.metrics
        if m.trade_count == 0:
            return False, "no out-of-sample trades"
        ok = m.profit_factor > OOS_MIN_PROFIT_FACTOR and m.sharpe > OOS_MIN_SHARPE
        return ok, (
            f"OOS PF {m.profit_factor:.2f} (need >{OOS_MIN_PROFIT_FACTOR}), "
            f"Sharpe {m.sharpe:.2f} (need >{OOS_MIN_SHARPE}), {m.trade_count} trades"
        )

    @property
    def gate_walk_forward(self) -> tuple[bool, str]:
        """Every rolling test window must clear the profit-factor bar itself."""
        if len(self.walk_forward) < WF_FOLDS:
            return False, (
                f"{len(self.walk_forward)} of {WF_FOLDS} folds ran"
                + (f" ({self.fold_note})" if self.fold_note else "")
            )
        clearing = [m for m in self.walk_forward if m.profit_factor > WF_MIN_WINDOW_PF]
        pfs = ", ".join(
            ("inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}")
            for m in self.walk_forward
        )
        return len(clearing) == len(self.walk_forward), (
            f"{len(clearing)}/{len(self.walk_forward)} windows above PF "
            f"{WF_MIN_WINDOW_PF} [{pfs}]"
        )

    @property
    def gate_monte_carlo(self) -> tuple[bool, str]:
        if self.monte_carlo_positive is None:
            return False, "too few trades to resample (fewer than 10)"
        return self.monte_carlo_positive >= MC_MIN_PROFITABLE, (
            f"{self.monte_carlo_positive:.1%} of {self.monte_carlo_simulations} "
            f"simulations profitable (need >={MC_MIN_PROFITABLE:.0%})"
        )

    @property
    def gate_drawdown(self) -> tuple[bool, str]:
        segments = [s for s in (self.in_sample, self.out_of_sample) if s and s.ok]
        if not segments:
            return False, "no segment produced a drawdown figure"
        worst = max(s.metrics.max_drawdown for s in segments)
        return worst < MAX_DRAWDOWN, (
            f"worst segment drawdown {worst:.2%} (need <{MAX_DRAWDOWN:.0%})"
        )

    @property
    def gate_sharpe_retention(self) -> tuple[bool, str]:
        """OOS Sharpe must retain 80% of in-sample Sharpe.

        A strategy that decays this far out of sample is disabled, not tuned:
        tuning it against the same out-of-sample window is how that window
        stops being out of sample.
        """
        if not (self.in_sample and self.in_sample.ok and self.out_of_sample and self.out_of_sample.ok):
            return False, "need both segments"
        is_sharpe = self.in_sample.metrics.sharpe
        oos_sharpe = self.out_of_sample.metrics.sharpe
        if is_sharpe <= 0:
            return False, f"in-sample Sharpe {is_sharpe:.2f} is not positive"
        retention = oos_sharpe / is_sharpe
        return retention >= MIN_SHARPE_RETENTION, (
            f"OOS Sharpe {oos_sharpe:.2f} is {retention:.0%} of in-sample "
            f"{is_sharpe:.2f} (need >={MIN_SHARPE_RETENTION:.0%})"
        )

    @property
    def gates(self) -> dict[str, tuple[bool, str]]:
        return {
            "OOS PF > 1.3 and Sharpe > 0.7": self.gate_oos,
            f"walk-forward: {WF_FOLDS} folds, each PF > {WF_MIN_WINDOW_PF}": self.gate_walk_forward,
            f"Monte Carlo: >={MC_MIN_PROFITABLE:.0%} profitable": self.gate_monte_carlo,
            f"max drawdown < {MAX_DRAWDOWN:.0%}": self.gate_drawdown,
            f"OOS Sharpe >= {MIN_SHARPE_RETENTION:.0%} of IS": self.gate_sharpe_retention,
        }

    @property
    def passes_gate(self) -> bool:
        """Every gate, not just the headline one."""
        return all(ok for ok, _ in self.gates.values())


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
    timeframe: Timeframe = Timeframe.D1,
    periods_per_year: int = 252,
) -> SegmentResult:
    usable = {i: list(c) for i, c in universe.items() if len(c) > warmup + 30}
    if not usable:
        return SegmentResult(name, error=f"no symbol has more than {warmup + 30} bars")
    try:
        backtester = Backtester(
            BacktestConfig(
                starting_equity=equity,
                warmup_bars=warmup,
                seed=7,
                periods_per_year=periods_per_year,
            )
        )
        result = backtester.run(
            usable,
            timeframe=timeframe,
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
    timeframe, periods_per_year = TIMEFRAMES[label]
    report = AssetClassReport(asset_class=asset_class)
    report.timeframe = timeframe.value
    report.periods_per_year = periods_per_year

    if not directory.is_dir() or not any(directory.glob("*.csv")):
        report.skipped = f"no CSV files in {directory}"
        return report

    loaded: dict[Instrument, LoadResult] = load_directory(
        directory, timeframe=timeframe, asset_class=asset_class, min_history=warmup
    )
    universe: dict[Instrument, list[Candle]] = {}
    for instrument, load in loaded.items():
        report.loaded[instrument.symbol] = load.summary()
        if not load.usable:
            continue
        universe[instrument] = list(load.series.candles)
        report.symbols.append(instrument.symbol)
        report.bars = max(report.bars, len(load.series))
        vol = realized_volatility(load.series.closes, 20, periods_per_year)
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
        "in_sample", in_universe, bench_head, warmup=warmup, equity=equity,
        timeframe=timeframe, periods_per_year=periods_per_year,
    )
    report.out_of_sample = _run_segment(
        "out_of_sample", out_universe, bench_tail, warmup=warmup, equity=equity,
        timeframe=timeframe, periods_per_year=periods_per_year,
    )

    # -- walk-forward: rolling train/test folds ----------------------------
    #
    # Each fold scores only its *test* window. The train window is passed to
    # the backtester as its warm-up, so no decision is ever made on a bar the
    # fold has already been scored on, and successive test windows do not
    # overlap. Folds are anchored to the end of history: when there is not room
    # for all of them the earliest are dropped, because a deployment decision
    # is about recent regimes.
    train_bars, test_bars = WF_GEOMETRY[label]
    folds = plan_walk_forward_folds(
        len(benchmark),
        train_bars=train_bars,
        test_bars=test_bars,
        folds=walk_forward_windows,
        min_train_bars=warmup,
    )
    report.folds = list(folds)
    if not folds:
        report.fold_note = (
            f"{len(benchmark)} bars is too few for {walk_forward_windows} folds of "
            f"{max(train_bars, warmup)} train + {test_bars} test"
        )
    for fold in folds:
        slice_universe = {
            inst: candles[fold.train_start:fold.test_end]
            for inst, candles in universe.items()
            if len(candles[fold.train_start:fold.test_end]) > fold.train_bars
        }
        if not slice_universe:
            continue
        segment = _run_segment(
            f"wf_{fold.index}",
            slice_universe,
            benchmark[fold.train_start:fold.test_end],
            warmup=fold.train_bars,
            equity=equity,
            timeframe=timeframe,
            periods_per_year=periods_per_year,
        )
        if segment.ok:
            report.walk_forward.append(segment.metrics)

    # -- Monte Carlo on the combined trade set -----------------------------
    trades: list[TradeRecord] = []
    for segment in (report.in_sample, report.out_of_sample):
        if segment and segment.ok:
            trades.extend(segment.result.trades)
            report.signals_generated += segment.result.signals_generated
            report.signals_rejected += segment.result.signals_rejected
            for rule, count in segment.result.rejection_reasons.items():
                report.rejection_reasons[rule] = report.rejection_reasons.get(rule, 0) + count
    simulation = monte_carlo(
        trades, starting_equity=equity, simulations=MC_SIMULATIONS
    )
    if simulation is not None:
        report.monte_carlo_positive = simulation.positive_fraction
        report.monte_carlo_simulations = simulation.simulations

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

    print(f"  timeframe: {report.timeframe} (annualised at {report.periods_per_year}/yr)")
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
        print(f"\n  {'fold':10}{'bars tested':>13}{'trades':>8}{'win rate':>10}{'PF':>8}{'sharpe':>9}")
        for fold, m in zip(report.folds, report.walk_forward):
            print(f"  wf_{fold.index:<7}{fold.test_bars:13}{m.trade_count:8}{m.win_rate:10.1%}"
                  f"{_fmt(m.profit_factor):>8}{m.sharpe:9.2f}")
    else:
        print(f"\n  walk-forward: no folds ran ({report.fold_note or 'unknown reason'})")

    if report.signals_generated:
        print(f"\n  signals generated {report.signals_generated}, "
              f"rejected by risk {report.signals_rejected}")
        for rule, count in sorted(report.rejection_reasons.items(), key=lambda kv: -kv[1]):
            print(f"      {rule:44}{count:6}")

    if report.strategy_breakdown:
        print(f"\n  {'strategy':16}{'trades':>8}{'win rate':>10}{'PF':>8}{'avg R':>8}")
        for strategy_id, stats in report.strategy_breakdown.items():
            print(f"  {strategy_id:16}{int(stats['trades']):8}{stats['win_rate']:10.1%}"
                  f"{_fmt(stats['profit_factor']):>8}{stats['avg_r']:8.2f}")

    print(f"\n  {'DEPLOYMENT GATES':56}{'verdict':>8}")
    for label, (ok, detail) in report.gates.items():
        print(f"  {label:56}{'PASS' if ok else 'FAIL':>8}")
        print(f"      {detail}")
    print(f"\n  OVERALL: {'PASS' if report.passes_gate else 'FAIL'}\n")


def write_markdown_report(report: AssetClassReport, path: Path) -> None:
    """Write one asset class's result as a standalone markdown report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    name = report.asset_class.value.upper()
    lines: list[str] = [f"# {name} validation", ""]

    if report.skipped:
        lines += [f"**Skipped:** {report.skipped}", ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        return

    lines += [
        f"Bar interval **{report.timeframe}**, Sharpe annualised at "
        f"{report.periods_per_year}/yr. Chronological 70/30 split; no decision is "
        f"ever made on a bar that has already been scored.",
        "",
        "## Data",
        "",
        "| symbol | bars | span | quality |",
        "|---|---|---|---|",
    ]
    for symbol, summary in sorted(report.loaded.items()):
        # "SYM: 2500 bars 2017-01-30 to 2026-08-28, quality 1.00 (OK)"
        body = summary.split(": ", 1)[1] if ": " in summary else summary
        bars = body.split(" bars")[0]
        span = body.split(" bars ")[1].split(", quality")[0] if " bars " in body else ""
        quality = body.split("quality ", 1)[1] if "quality " in body else ""
        lines.append(f"| {symbol} | {bars} | {span} | {quality} |")

    if report.volatility_profile:
        lines += ["", "Realised volatility (annualised): " + ", ".join(
            f"{s} {v:.0%}" for s, v in sorted(report.volatility_profile.items())
        )]

    lines += [
        "",
        "## Segments",
        "",
        "| segment | trades | win rate | profit factor | Sharpe | max DD | return |",
        "|---|---|---|---|---|---|---|",
    ]
    for segment in (report.in_sample, report.out_of_sample):
        if segment is None:
            continue
        if not segment.ok:
            lines.append(f"| {segment.name} | ERROR | {segment.error} | | | | |")
            continue
        m = segment.metrics
        lines.append(
            f"| {segment.name} | {m.trade_count} | {m.win_rate:.1%} | "
            f"{_fmt(m.profit_factor)} | {m.sharpe:.2f} | {m.max_drawdown:.2%} | "
            f"{m.return_pct:.2%} |"
        )

    if report.walk_forward:
        lines += [
            "",
            "## Walk-forward",
            "",
            "Rolling folds, anchored to the end of history. Each fold's train window "
            "is the backtester's warm-up, so only the test window is scored and test "
            "windows never overlap.",
            "",
            "| fold | train bars | test bars | trades | win rate | PF | Sharpe |",
            "|---|---|---|---|---|---|---|",
        ]
        for fold, m in zip(report.folds, report.walk_forward):
            lines.append(
                f"| wf_{fold.index} | {fold.train_bars} | {fold.test_bars} | "
                f"{m.trade_count} | {m.win_rate:.1%} | {_fmt(m.profit_factor)} | "
                f"{m.sharpe:.2f} |"
            )
    else:
        lines += ["", "## Walk-forward", "",
                  f"No folds ran: {report.fold_note or 'unknown reason'}."]

    if report.signals_generated:
        lines += [
            "", "## Where signals went", "",
            f"{report.signals_generated} signals generated, "
            f"{report.signals_rejected} rejected by the risk engine.",
        ]
        if report.rejection_reasons:
            lines += ["", "| rejection rule | count |", "|---|---|"]
            for rule, count in sorted(report.rejection_reasons.items(), key=lambda kv: -kv[1]):
                lines.append(f"| `{rule}` | {count} |")

    if report.strategy_breakdown:
        lines += [
            "", "## Per strategy", "",
            "| strategy | trades | win rate | PF | avg R |",
            "|---|---|---|---|---|",
        ]
        for strategy_id, stats in report.strategy_breakdown.items():
            lines.append(
                f"| {strategy_id} | {int(stats['trades'])} | {stats['win_rate']:.1%} | "
                f"{_fmt(stats['profit_factor'])} | {stats['avg_r']:.2f} |"
            )

    lines += ["", "## Deployment gates", "", "| gate | verdict | detail |", "|---|---|---|"]
    for label, (ok, detail) in report.gates.items():
        lines.append(f"| {label} | {'PASS' if ok else '**FAIL**'} | {detail} |")
    lines += ["", f"**Overall: {'PASS' if report.passes_gate else 'FAIL'}**", ""]

    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate on real market history")
    parser.add_argument("--data-dir", default="data/real")
    parser.add_argument("--asset", choices=sorted(ASSET_DIRS), action="append")
    parser.add_argument("--equity", type=float, default=100_000.0)
    parser.add_argument("--warmup", type=int, default=300)
    parser.add_argument("--in-sample", type=float, default=0.70, dest="in_sample")
    parser.add_argument("--wf-windows", type=int, default=WF_FOLDS, dest="wf_windows")
    parser.add_argument("--json", action="store_true", help="emit machine-readable summary")
    parser.add_argument(
        "--report-dir",
        default="validation",
        help="write one markdown report per asset class here (empty to skip)",
    )
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
        if args.report_dir:
            destination = Path(args.report_dir) / f"{label}_report.md"
            write_markdown_report(report, destination)
            print(f"  written: {destination}\n")

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
