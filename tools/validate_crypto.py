"""Crypto validation against the predeclared protocol (docs/CRYPTO_PREDECLARATION.md).

Reads venue data, runs the predeclared fold geometry, applies the predeclared
cost model, compares against the predeclared benchmarks, and evaluates the
predeclared gates. Nothing in here chooses a threshold: every number it tests
against comes from GATES below, which mirrors the committed predeclaration.

Usage:
    python tools/validate_crypto.py --horizon medium
    python tools/validate_crypto.py --horizon medium --holdout
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradermaximus.core.types import AssetClass, Candle, Instrument, Timeframe, TradeRecord
from tradermaximus.data.csv_loader import load_series
from tradermaximus.observability.metrics import compute_metrics, max_drawdown
from tradermaximus.regime.engine import MarketContext
from tradermaximus.research.backtest import BacktestConfig, Backtester
from tradermaximus.research.validation import plan_walk_forward_folds

# --- the predeclared cost model (docs/CRYPTO_PREDECLARATION.md section 2) ---
FEE_BPS_PER_SIDE = 10.0
SPREAD_BPS = 2.0
SLIPPAGE_BPS = 5.0

# --- the predeclared fold geometry (section 3) -------------------------------
HORIZONS = {
    "short":  {"tf": Timeframe.H1, "train": 8_760, "test": 2_190, "ppy": 8_760, "embargo": 72},
    "medium": {"tf": Timeframe.D1, "train": 730,   "test": 365,   "ppy": 365,   "embargo": 42},
    "long":   {"tf": Timeframe.D1, "train": 1_095, "test": 365,   "ppy": 365,   "embargo": 120},
}

# --- the predeclared gates (section 5b) -------------------------------------
GATES = {
    "short":  {"P1": 200, "P2": 3, "P3": 3, "P4": 0.20, "P5": 1.25, "P6": 0.60},
    "medium": {"P1": 60,  "P2": 4, "P3": 3, "P4": 0.25, "P5": 1.25, "P6": 0.60},
    "long":   {"P1": 25,  "P2": 5, "P3": 2, "P4": 0.30, "P5": 1.25, "P6": 0.50},
}
HOLDOUT_BARS = 365
BLOCK = {"short": 168, "medium": 20, "long": 20}
SIMULATIONS = 5_000

VENUE = Path("data/venue/binance")
SYMBOLS = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}


def _instrument(symbol: str) -> Instrument:
    # tick_size / lot_size are NOT from exchangeInfo: that endpoint is
    # geo-blocked (HTTP 451). These are placeholders and the predeclaration
    # records lot/tick rounding as unvalidated. They are set fine enough that
    # they do not bind, so they cannot flatter a result.
    return Instrument(symbol, AssetClass.CRYPTO, "BINANCE", "crypto", 0.01, 1e-8, 1e-8)


def load(symbol: str, timeframe: Timeframe) -> list[Candle]:
    name = "1d" if timeframe is Timeframe.D1 else "1h"
    result = load_series(
        VENUE / f"{symbol}_{name}.csv",
        _instrument(symbol), timeframe=timeframe, min_history=100,
    )
    if not result.usable:
        raise SystemExit(f"{symbol}: {result.summary()}")
    return list(result.series.candles)


# --- benchmarks (section 4) -------------------------------------------------

def buy_and_hold(candles: list[Candle], fee_bps: float = FEE_BPS_PER_SIDE) -> dict:
    """One entry, one exit, both charged."""
    entry, exit_ = candles[0].close, candles[-1].close
    cost = (fee_bps + SPREAD_BPS) / 10_000.0
    gross = exit_ / entry
    net = gross * (1.0 - cost) ** 2
    curve = [c.close / entry for c in candles]
    rets = [b / a - 1.0 for a, b in zip(curve, curve[1:]) if a > 0]
    return {"return": net - 1.0, "max_dd": max_drawdown(curve),
            "rets": rets, "time_invested": 1.0}


def rebalanced_mix(series: dict[str, list[Candle]], every: int = 30) -> dict:
    """Equal-weight, rebalanced every `every` bars, rebalancing charged."""
    names = sorted(series)
    n = min(len(series[s]) for s in names)
    weights = {s: 1.0 / len(names) for s in names}
    units = {s: weights[s] / series[s][0].close for s in names}
    curve = [1.0]
    cost = (FEE_BPS_PER_SIDE + SPREAD_BPS) / 10_000.0
    for i in range(1, n):
        value = sum(units[s] * series[s][i].close for s in names)
        if i % every == 0 and value > 0:
            turnover = sum(
                abs(units[s] * series[s][i].close / value - weights[s]) for s in names
            ) / 2.0
            value *= (1.0 - cost * turnover * 2.0)
            units = {s: weights[s] * value / series[s][i].close for s in names}
        curve.append(value)
    rets = [b / a - 1.0 for a, b in zip(curve, curve[1:]) if a > 0]
    return {"return": curve[-1] - 1.0, "max_dd": max_drawdown(curve),
            "rets": rets, "time_invested": 1.0}


def ma_trend_to_cash(candles: list[Candle], window: int = 200) -> dict:
    """Long when close > SMA(window), else cash. Each switch charged.

    ``switches`` is returned and reported, because without it this benchmark
    can produce a number that looks like a result and is an artifact. Measured
    on hourly bars with a 200-*hour* window it returned 1.2e9 x: selectively
    compounding ~40,000 hourly returns while skipping the unheld hours
    explodes, and the rule it describes is unimplementable at that turnover
    whatever the fee model says. Judge this benchmark by its switch count and
    its implied per-round-trip edge, not by its terminal equity.
    """
    closes = [c.close for c in candles]
    cost = (FEE_BPS_PER_SIDE + SPREAD_BPS) / 10_000.0
    equity, invested, held, switches = 1.0, 0, False, 0
    curve = [1.0]
    for i in range(window, len(closes)):
        want = closes[i] > statistics.fmean(closes[i - window:i])
        if want != held:
            equity *= (1.0 - cost)
            held = want
            switches += 1
        if held:
            equity *= closes[i] / closes[i - 1]
            invested += 1
        curve.append(equity)
    rets = [b / a - 1.0 for a, b in zip(curve, curve[1:]) if a > 0]
    span = max(1, len(closes) - window)
    return {"return": curve[-1] - 1.0, "max_dd": max_drawdown(curve),
            "rets": rets, "time_invested": invested / span,
            "switches": switches, "round_trips": switches / 2.0}


def sharpe(rets: list[float], ppy: int) -> float:
    if len(rets) < 3:
        return 0.0
    sd = statistics.pstdev(rets)
    return 0.0 if sd == 0 else statistics.fmean(rets) / sd * math.sqrt(ppy)


# --- block bootstrap (section 5c) ------------------------------------------

def block_bootstrap(rets: list[float], block: int, *, sims: int = SIMULATIONS,
                    seed: int = 7) -> float:
    """Fraction of resamples with positive total return, blocks preserved."""
    if len(rets) < block * 3:
        return float("nan")
    rng = random.Random(seed)
    n, positive = len(rets), 0
    blocks_needed = max(1, n // block)
    for _ in range(sims):
        total = 1.0
        for _ in range(blocks_needed):
            start = rng.randrange(0, n - block)
            for r in rets[start:start + block]:
                total *= (1.0 + r)
        positive += total > 1.0
    return positive / sims


@dataclass
class Segment:
    name: str
    trades: list[TradeRecord] = field(default_factory=list)
    curve: list[float] = field(default_factory=list)
    error: str = ""


def run_segment(name: str, universe: dict, bench: list[Candle], *,
                timeframe: Timeframe, warmup: int, ppy: int) -> Segment:
    try:
        result = Backtester(BacktestConfig(
            starting_equity=100_000.0, warmup_bars=warmup, seed=7,
            periods_per_year=ppy, commission_bps=FEE_BPS_PER_SIDE,
            spread_bps=SPREAD_BPS, random_slippage_bps=SLIPPAGE_BPS,
            max_positions=3,
        )).run(universe, timeframe=timeframe, benchmark=bench,
               market_context=MarketContext(breadth_index=0.5, new_highs_ratio=0.55,
                                            correlation_change=0.15))
        return Segment(name, list(result.trades), result.equity_values)
    except Exception as exc:  # noqa: BLE001
        return Segment(name, error=f"{type(exc).__name__}: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", choices=sorted(HORIZONS), default="medium")
    ap.add_argument("--holdout", action="store_true",
                    help="evaluate the reserved final 365 bars (ONCE)")
    args = ap.parse_args()

    h = HORIZONS[args.horizon]
    tf, ppy = h["tf"], h["ppy"]
    gates = GATES[args.horizon]
    commit = subprocess.run(["git", "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()

    print(f"CRYPTO VALIDATION — horizon={args.horizon}  timeframe={tf.value}")
    print(f"commit {commit}")
    print(f"cost model: {FEE_BPS_PER_SIDE}bps fee/side + {SPREAD_BPS}bps spread "
          f"+ {SLIPPAGE_BPS}bps slippage  (~{2*(FEE_BPS_PER_SIDE+SPREAD_BPS+SLIPPAGE_BPS):.0f}bps round trip)")

    raw = {s: load(s, tf) for s in SYMBOLS}
    for s, c in raw.items():
        print(f"  {s}: {len(c):,} bars  {c[0].timestamp.date()} .. {c[-1].timestamp.date()}")

    # Reserve the holdout. Folds are drawn strictly before it.
    research = {s: c[:-HOLDOUT_BARS] for s, c in raw.items()}
    holdout = {s: c[-(HOLDOUT_BARS + h["train"]):] for s, c in raw.items()}
    print(f"\n  research span: {len(next(iter(research.values()))):,} bars")
    print(f"  reserved holdout: {HOLDOUT_BARS} bars "
          f"({raw['BTCUSDT'][-HOLDOUT_BARS].timestamp.date()} .. "
          f"{raw['BTCUSDT'][-1].timestamp.date()})")

    source = holdout if args.holdout else research
    label = "HOLDOUT (evaluated once)" if args.holdout else "research"
    bench = max(source.values(), key=len)

    # --- benchmarks ---------------------------------------------------------
    print(f"\n  BENCHMARKS over the {label} span")
    print(f"  {'benchmark':34}{'return':>13}{'Sharpe':>8}{'max DD':>8}{'inv':>6}{'round trips':>12}")
    marks = {
        "B2 buy-and-hold BTC": buy_and_hold(source["BTCUSDT"]),
        "B3 buy-and-hold ETH": buy_and_hold(source["ETHUSDT"]),
        "B4 50/50 monthly rebalance": rebalanced_mix(source),
        "B5 BTC 200-bar MA to cash": ma_trend_to_cash(source["BTCUSDT"]),
    }
    def fmt_return(x: float) -> str:
        return f"{x:>12.1%}" if abs(x) < 1_000 else f"{1.0 + x:>11.3g}x"

    print(f"  {'B1 cash (USD)':34}{fmt_return(0.0)}{0.0:>8.2f}{0.0:>8.1%}{0.0:>6.0%}{0:>12}")
    for name, m in marks.items():
        rt = m.get("round_trips")
        print(f"  {name:34}{fmt_return(m['return'])}{sharpe(m['rets'], ppy):>8.2f}"
              f"{m['max_dd']:>8.1%}{m['time_invested']:>6.0%}"
              f"{(f'{rt:,.0f}' if rt is not None else '1'):>12}")
    best_bench_sharpe = max(sharpe(m["rets"], ppy) for m in marks.values())

    # --- folds -------------------------------------------------------------
    folds = plan_walk_forward_folds(
        len(bench), train_bars=h["train"], test_bars=h["test"], folds=5,
        min_train_bars=h["train"],
    )
    print(f"\n  WALK-FORWARD: {len(folds)} folds of {h['train']} train / {h['test']} test")
    if not folds:
        print("    INSUFFICIENT EVIDENCE: history too short for the predeclared geometry")
        return 1

    universe = {_instrument(s): c for s, c in source.items()}
    all_trades: list[TradeRecord] = []
    fold_rows = []
    print(f"  {'fold':8}{'trades':>8}{'win':>8}{'net PF':>9}{'Sharpe':>9}{'maxDD':>8}")
    for fold in folds:
        sliced = {i: c[fold.train_start:fold.test_end] for i, c in universe.items()}
        seg = run_segment(f"wf_{fold.index}", sliced,
                          bench[fold.train_start:fold.test_end],
                          timeframe=tf, warmup=fold.train_bars, ppy=ppy)
        if seg.error:
            print(f"  wf_{fold.index:<6}ERROR  {seg.error}")
            continue
        m = compute_metrics(seg.trades, seg.curve)
        pf = "inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
        print(f"  wf_{fold.index:<6}{m.trade_count:>8}{m.win_rate:>8.1%}{pf:>9}"
              f"{m.sharpe:>9.2f}{m.max_drawdown:>8.1%}")
        fold_rows.append(m)
        all_trades.extend(seg.trades)

    # --- gate evaluation ---------------------------------------------------
    print(f"\n  AGGREGATE over {len(fold_rows)} folds")
    if not all_trades:
        print("    0 trades. INSUFFICIENT EVIDENCE — no gate is evaluable.")
        print(f"\n  VERDICT: INSUFFICIENT EVIDENCE (horizon={args.horizon})")
        return 0

    agg = compute_metrics(all_trades)
    years = {t.closed_at.year for t in all_trades if t.closed_at}
    regimes = {t.regime for t in all_trades}
    by_year: Counter = Counter()
    for t in all_trades:
        if t.closed_at:
            by_year[t.closed_at.year] += t.pnl
    net = sum(t.pnl for t in all_trades)
    worst_year_share = (max(abs(v) for v in by_year.values()) / abs(net)) if net else float("inf")
    worst_trade_share = (max(abs(t.pnl) for t in all_trades) / abs(net)) if net else float("inf")
    trade_rets = [t.pnl / 100_000.0 for t in all_trades]
    mc = block_bootstrap(trade_rets, max(2, BLOCK[args.horizon] // 10))
    worst_dd = max((m.max_drawdown for m in fold_rows), default=0.0)

    checks = [
        ("P1 min OOS trades", agg.trade_count >= gates["P1"],
         f"{agg.trade_count} vs >= {gates['P1']}"),
        ("P2 min calendar years", len(years) >= gates["P2"],
         f"{len(years)} ({sorted(years)}) vs >= {gates['P2']}"),
        ("P3 min distinct regimes", len(regimes) >= gates["P3"],
         f"{len(regimes)} ({sorted(regimes)}) vs >= {gates['P3']}"),
        ("P4 max drawdown", worst_dd < gates["P4"],
         f"{worst_dd:.1%} vs < {gates['P4']:.0%}"),
        ("P5 net OOS profit factor", agg.profit_factor > gates["P5"],
         f"{agg.profit_factor:.2f} vs > {gates['P5']}"),
        ("P6 net OOS Sharpe", agg.sharpe > gates["P6"],
         f"{agg.sharpe:.2f} vs > {gates['P6']}"),
        ("P7 beats best benchmark Sharpe", agg.sharpe > best_bench_sharpe,
         f"{agg.sharpe:.2f} vs best benchmark {best_bench_sharpe:.2f}"),
        ("P8 block bootstrap >= 90%", (mc == mc) and mc >= 0.90,
         f"{mc:.1%}" if mc == mc else "not computable (too few trades for blocks)"),
        ("P9 no year > 50% of P&L", worst_year_share <= 0.50,
         f"largest year {worst_year_share:.0%}"),
        ("P10 no trade > 25% of P&L", worst_trade_share <= 0.25,
         f"largest trade {worst_trade_share:.0%}"),
        ("E2 every fold PF > 1.3",
         bool(fold_rows) and all(m.profit_factor > 1.3 for m in fold_rows),
         f"{sum(1 for m in fold_rows if m.profit_factor > 1.3)}/{len(fold_rows)} folds"),
    ]
    print(f"  {'gate':36}{'verdict':>9}   detail")
    for name, ok, detail in checks:
        print(f"  {name:36}{'PASS' if ok else 'FAIL':>9}   {detail}")

    passed = all(ok for _, ok, _ in checks)

    # The evidence bar is deliberately ASYMMETRIC, and that is a judgement
    # worth stating rather than burying.
    #
    # P1/P2 exist to stop a thin sample being presented as a positive edge —
    # the 5-trade "profit factor 1.75" this audit began with. They must not
    # also stop a NEGATIVE result being called what it is: it takes more
    # evidence to commit capital than to decline to. So a result that is
    # consistently and directionally negative is a FAIL even below the
    # minimum-trade floor, where the same sample could never support a PASS.
    folds_all_losing = bool(fold_rows) and all(
        m.profit_factor < 1.0 for m in fold_rows
    )
    conclusively_negative = (
        agg.trade_count >= 30
        and net < 0
        and agg.profit_factor < 1.0
        and folds_all_losing
    )
    enough_for_positive = (
        agg.trade_count >= gates["P1"] and len(years) >= gates["P2"]
    )
    if passed:
        verdict = "PASS"
    elif conclusively_negative:
        verdict = "FAIL (negative expectancy demonstrated)"
    elif enough_for_positive:
        verdict = "FAIL"
    else:
        verdict = "INSUFFICIENT EVIDENCE"
    print(f"\n  VERDICT: {verdict}  (horizon={args.horizon}, {label})")
    print(f"  net P&L {net:+,.2f} on 100,000 over {agg.trade_count} trades")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
