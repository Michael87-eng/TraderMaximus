"""Walk-forward and holdout test of the 200DMA rule itself (Task 3).

The system's own gates reject this rule on ~100% of the bars where it fires —
it scores 48/100 against a 75 bar, and two of its components are zero for
structural reasons (BTC measured against BTC has no relative strength, and a
strategy with no ATR stop has no risk/reward ratio). So running it through the
backtester measures the architecture, not the rule.

This measures the rule. Same bars, same cost model, same fold geometry, same
reserved holdout as `validate_crypto.py`. The question it answers is the one
Task 3 poses: does the in-sample benchmark figure survive out of sample?

Run:  python tools/validate_200dma.py
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import statistics
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradermaximus.research.validation import plan_walk_forward_folds

FEE_BPS, SPREAD_BPS = 10.0, 2.0
PPY = 365
HOLDOUT = 365
TRAIN, TEST, FOLDS = 730, 365, 5
VENUE = Path("data/venue/binance")


def closes(symbol: str) -> tuple[list[str], list[float]]:
    rows = list(csv.DictReader((VENUE / f"{symbol}_1d.csv").read_text().splitlines()))
    return [r["timestamp"] for r in rows], [float(r["close"]) for r in rows]


def simulate(px: list[float], window: int, *, warm: int) -> dict:
    """Long above the MA, cash below. Each switch pays fee + spread.

    `warm` is the first index that may trade, so a fold's training bars are
    used only to form the average — never scored.
    """
    cost = (FEE_BPS + SPREAD_BPS) / 10_000.0
    equity, held, invested, switches = 1.0, False, 0, 0
    curve, trades, entry = [1.0], [], 0.0
    for i in range(max(warm, window), len(px)):
        above = px[i] > statistics.fmean(px[i - window:i])
        if above != held:
            equity *= (1.0 - cost)
            switches += 1
            if above:
                entry = px[i]
            elif entry > 0:
                trades.append(px[i] / entry - 1.0 - 2 * cost)
            held = above
        if held:
            equity *= px[i] / px[i - 1]
            invested += 1
        curve.append(equity)
    if held and entry > 0:
        trades.append(px[-1] / entry - 1.0 - 2 * cost)

    rets = [b / a - 1.0 for a, b in zip(curve, curve[1:]) if a > 0]
    peak, dd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak if peak > 0 else 0.0)
    sd = statistics.pstdev(rets) if len(rets) > 2 else 0.0
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    gp, gl = sum(wins), -sum(losses)
    span = max(1, len(px) - max(warm, window))
    return {
        "return": curve[-1] - 1.0,
        "sharpe": (statistics.fmean(rets) / sd * math.sqrt(PPY)) if sd else 0.0,
        "max_dd": dd,
        "invested": invested / span,
        "round_trips": switches / 2.0,
        "trades": len(trades),
        "win_rate": (len(wins) / len(trades)) if trades else 0.0,
        "profit_factor": (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0),
        "trade_returns": trades,
    }


def bootstrap(rets: list[float], block: int = 20, sims: int = 5_000, seed: int = 7) -> float:
    if len(rets) < block * 3:
        return float("nan")
    rng, n, pos = random.Random(seed), len(rets), 0
    for _ in range(sims):
        total = 1.0
        for _ in range(max(1, n // block)):
            st = rng.randrange(0, n - block)
            for r in rets[st:st + block]:
                total *= (1.0 + r)
        pos += total > 1.0
    return pos / sims


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=200)
    ap.add_argument("--holdout", action="store_true")
    args = ap.parse_args()
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                            text=True).stdout.strip()

    print(f"200DMA RULE — walk-forward and holdout   window={args.window}")
    print(f"commit {commit}")
    print(f"costs {FEE_BPS}bps fee + {SPREAD_BPS}bps spread per switch\n")

    for symbol in ("BTCUSDT", "ETHUSDT"):
        stamps, px = closes(symbol)
        research_px = px[:-HOLDOUT]
        print("=" * 78)
        print(f"  {symbol}  {stamps[0][:10]} .. {stamps[-1][:10]}")
        print(f"  research {len(research_px):,} bars, holdout {HOLDOUT} "
              f"({stamps[-HOLDOUT][:10]} .. {stamps[-1][:10]})")
        print("=" * 78)

        whole = simulate(research_px, args.window, warm=args.window)
        print(f"\n  IN-SAMPLE over the whole research span (the benchmark figure):")
        print(f"    return {whole['return']:>12.1%}  Sharpe {whole['sharpe']:>5.2f}  "
              f"maxDD {whole['max_dd']:>6.1%}  invested {whole['invested']:>4.0%}  "
              f"round trips {whole['round_trips']:>5.0f}")
        print(f"    trades {whole['trades']:>3}  win rate {whole['win_rate']:>5.1%}  "
              f"PF {whole['profit_factor']:>5.2f}")

        folds = plan_walk_forward_folds(len(research_px), train_bars=TRAIN,
                                        test_bars=TEST, folds=FOLDS,
                                        min_train_bars=max(TRAIN, args.window))
        print(f"\n  WALK-FORWARD — {len(folds)} folds, test window scored only:")
        print(f"    {'fold':7}{'trades':>8}{'win':>8}{'PF':>8}{'return':>10}"
              f"{'Sharpe':>8}{'maxDD':>8}{'inv':>6}")
        fold_pf, fold_ret, all_trades = [], [], []
        for f in folds:
            seg = research_px[f.train_start:f.test_end]
            m = simulate(seg, args.window, warm=f.train_bars)
            pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
            print(f"    wf_{f.index:<4}{m['trades']:>8}{m['win_rate']:>8.1%}{pf:>8}"
                  f"{m['return']:>10.1%}{m['sharpe']:>8.2f}{m['max_dd']:>8.1%}"
                  f"{m['invested']:>6.0%}")
            fold_pf.append(m["profit_factor"])
            fold_ret.append(m["return"])
            all_trades.extend(m["trade_returns"])

        clearing = sum(1 for p in fold_pf if p > 1.3)
        positive = sum(1 for r in fold_ret if r > 0)
        mc = bootstrap(all_trades, block=2)
        print(f"\n    folds with PF > 1.3 : {clearing}/{len(folds)}")
        print(f"    folds positive      : {positive}/{len(folds)}")
        print(f"    pooled OOS trades   : {len(all_trades)}")
        if all_trades:
            w = [t for t in all_trades if t > 0]
            gp, gl = sum(w), -sum(t for t in all_trades if t <= 0)
            print(f"    pooled OOS PF       : {(gp/gl) if gl>0 else float('inf'):.2f}")
            print(f"    pooled OOS win rate : {len(w)/len(all_trades):.1%}")
        print(f"    block bootstrap     : "
              f"{f'{mc:.1%}' if mc == mc else 'not computable'}")

        ho = simulate(px[-(HOLDOUT + args.window):], args.window, warm=args.window)
        print(f"\n  HOLDOUT (evaluated once, {HOLDOUT} bars):")
        print(f"    return {ho['return']:>12.1%}  Sharpe {ho['sharpe']:>5.2f}  "
              f"maxDD {ho['max_dd']:>6.1%}  invested {ho['invested']:>4.0%}  "
              f"trades {ho['trades']}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
