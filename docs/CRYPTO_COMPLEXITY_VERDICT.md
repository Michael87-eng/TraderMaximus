# Task 5 — the complexity question, answered plainly

> If the 200DMA strategy clears gates and the multi-strategy regime
> architecture does not, what is the justification for continuing to build the
> complex system?

## The answer

**There is none, on the current evidence. Consolidate to the simpler system.**

Neither clears the gates as predeclared. But they fail in opposite directions,
and the difference is the whole answer:

- **The 200DMA fails on evidence volume.** 31 pooled out-of-sample trades
  against a 60-trade floor, 3 of 5 folds above PF 1.3, a 32.7% drawdown
  against a 25% cap. Its *performance* passes — pooled OOS profit factor 3.29,
  holdout +15.5% at Sharpe 0.81 — and it fails because a rule making 35 round
  trips in eight years cannot generate the sample size the gates demand.
- **The complex system fails on performance and on evidence.** 5 authorisations
  in 30,710 bar-evaluations. 1 trade in eight years at the medium horizon. Net
  −$705 over 54 trades at the short horizon.

One has an edge it cannot prove to the standard set. The other has no edge and
no sample.

## The specific test the brief set

> "The system hedges regimes the 200DMA cannot" is a justification only if the
> system demonstrably trades in those regimes and beats the 200DMA there. If
> the complex system produces no trade that the 200DMA would not have
> produced, say so.

Measured. The system's five authorisations across both pairs:

| strategy | authorisations | regime |
|---|---:|---|
| STRATEGY_004 reversal | 3 | RANGE |
| STRATEGY_012 trend | 1 | TREND_UP |
| STRATEGY_013 Donchian | 1 | TREND_UP |
| STRATEGY_002, 009 | 0 | — |

Three of the five are in RANGE, which is a regime the 200DMA genuinely does
not trade — it is in cash there. So the hedging claim is **not strictly empty**:
the architecture did produce three trades the 200DMA would not have taken.

But three trades over 30,710 bar-evaluations is not a hedge. It is not a
sample, it is not a strategy, and it cannot be gated. The 200DMA was in cash
43% of the time and still returned 166× on BTC; the architecture's
contribution to those idle periods is three reversal trades across nine years.

**So: the complex system produces almost no trade the 200DMA would not have
produced, and the handful it does produce cannot be evaluated.** The hedging
justification fails the test the brief set for it.

## What the architecture actively costs

This is the part that moves the recommendation from "unproven" to "harmful".

The system does not merely fail to add value to the 200DMA. **It rejects it.**
Run `STRATEGY_014` through the pipeline and the entry condition fires on 100%
of eligible bars — then every single one of the 837 survivors is rejected by
the signal score at 48.0 against a 75 bar.

Two of the eight scoring components are **structurally zero** for this
strategy, before the market is consulted:

- `relative_strength` = 0/11.11 — the rule's relative strength is measured
  against the benchmark, and BTC's benchmark is BTC. Excess return is
  identically zero.
- `risk_reward` = 0/5.56 — the rule has no ATR stop by design; the moving
  average is the exit, so there is no ratio to score.

The remaining rubric rewards *breadth of confirmation*. A one-condition rule
cannot accumulate what a six-condition rule accumulates, regardless of which
makes money. **The scoring engine systematically prefers complexity over
performance**, and the best-performing rule in the entire study is the one it
rejects hardest.

That is not a tuning problem. It is what a nine-component weighted score *is*.

## What is worth keeping

Consolidating is not deleting. The architecture contains several things that
are correct, tested, and independently valuable — and the simple strategy needs
most of them:

| keep | why |
|---|---|
| Risk engine and hard limits | across every run: zero sizing, stop or authorisation rejections, zero limit violations. The risk engine was never the constraint. |
| Point-in-time truncation, next-bar fills | the structural defence against look-ahead; the 200DMA result depends on it being right |
| Cost model (taker/maker/spread/impact) | the thing that makes a result admissible |
| Data quality engine | caught the zero-volume BTC prefix, the SOL FTX bar, the hourly China-ban bar, all unprompted |
| Walk-forward, holdout, block bootstrap | the machinery that produced this verdict |
| Drift detection, kill switch, dashboard | operational necessities whatever trades |
| The funnel | the only reason any of this was diagnosable |

| retire or bypass | why |
|---|---|
| The nine-component signal score | provably rejects the best strategy; two components are structurally unscoreable for simple rules |
| Family minimum scores (75/65/50) | a threshold on a metric that does not measure what it claims |
| STRATEGY_002, 009 | zero authorisations in nine years |
| The multi-strategy regime-routing premise | three trades of unique contribution in 30,710 evaluations |

**The recommendation is a simple strategy running on the existing risk,
execution and measurement infrastructure, with the scoring layer removed from
the authorisation path.** That is a smaller system than the one that exists and
a larger one than a 15-line script.

## What would change this verdict

Stated so it is falsifiable rather than rhetorical:

1. **A revised gate set, predeclared against an untouched window**, under which
   the 200DMA either passes or fails on its merits rather than on trade count.
   If it passes, it becomes the baseline. If it fails, the 166× was in-sample
   luck and the comparison collapses — which would be equally informative.
2. **A strategy that authorises enough trades to be measured and beats the
   200DMA in the regimes the 200DMA sits out.** RANGE is 27.73% of bars and the
   200DMA is in cash for all of it. That is a real, large opportunity. Three
   reversal trades is not a serious attempt at it; a mean-reversion strategy
   specified for crypto range conditions and actually permitted to trade would
   be.
3. **Evidence that the scoring engine's rejections are informative.** If the
   setups it rejects at 48/100 underperform those it accepts at 75+, the rubric
   is doing real work and the problem is elsewhere. That is directly testable
   and has never been tested. **It is the single most valuable next
   measurement**, because it decides whether the scoring layer is a filter or a
   confound.

## One sentence

The complex system is excellent at not losing money and has not demonstrated
that it can make any; the simple rule has made a great deal and cannot prove it
to the standard we set; and the system's scoring layer is the specific
component that prevents the two from being combined.
