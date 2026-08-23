# Gate autopsy and calibration

Record of why the system traded too rarely, what was actually wrong, and what
was deliberately left alone.

## The question

An early backtest produced 5 signals over 640 bars across 4 symbols. The
tempting response — lower the volume threshold, widen the RSI band — would
have been calibration by guesswork. So the gates were measured instead.

`tools/gate_autopsy.py` walks the pipeline in order and counts, at each gate,
how many bars are still alive and how many die there. `tools/gate_analysis.py`
complements it with *marginal* pass rates per condition.

## Finding 0: the measuring instrument was broken

Before any threshold could be judged, the generator producing the test data
had to be checked. It drew volume from `uniform(0.6, 1.8)`, which has a hard
ceiling: relative volume — today's volume over its 50-day average — could
never exceed about **1.35**.

Every volume-gated condition in the library therefore failed 100% of the time:

| condition | spec source | pass rate on the old generator |
|---|---|---|
| `relative_volume > 1.4` | §6.2.1 | 0.0% (mathematically impossible) |
| `relative_volume > 2.0` | §6.2.3 | 0.0% (mathematically impossible) |

Lowering 1.4 to 1.2 here would have "worked" — and would have been fitting a
correct, spec-mandated threshold (O'Neil's 40%-above-average) to an artefact
of the test harness, then shipping that to real markets. This is exactly the
data-snooping failure §20.3 warns about.

`tools/market_sim.py` replaces it with a generator whose properties are each
load-bearing for some strategy: lognormal volume with a fat right tail, volume
correlated with price movement, dry-up during consolidation, overnight gaps,
and regime persistence.

| property | generated | realistic target |
|---|---|---|
| relative volume > 1.4× | 18% | 15–20% |
| relative volume > 2.0× | 5.7% | 2–5% |
| relative volume < 0.7× | 35% | 15–25% |
| gap > 3% | 1.3% | 1–2% |

## Finding 1: three strategies could never fire (a bug, not strictness)

Base pivots were computed over a window that **included the current bar**:

```
flat_base:       pivot 267.62 == max(high) of last 25 bars, current high 263.55
close > pivot    reduces to    close > this bar's own high     -> impossible
```

| detector | close > pivot, before | after |
|---|---|---|
| `flat_base` | **0.0%** | 5.9% |
| `cup_with_handle` | **0.0%** | 12.5% |
| `double_bottom` | 28.7% | 28.7% (already correct) |

`double_bottom` was unaffected because its pivot comes from a swing point
strictly before the current bar — which is what confirmed the diagnosis.

STRATEGY_005 (CAN SLIM), STRATEGY_007 (Flat Base) and STRATEGY_008 (High Tight
Flag) were incapable of producing a single signal. Every unit test passed,
because each component behaved exactly as written; only a frequency
measurement could surface it.

Fixed by `PIVOT_EXCLUDE_RECENT` in `indicators/structure.py`: the pivot is
established by the base's own formation and fixed before the breakout bar
prints.

## Finding 2: the breakout comparison was mismatched

`is_breakout` compared today's **close** against the prior 20 **intraday
highs**. §6.2.1 says "breakout above 20-day high" and "Entry: on breakout
candle close" — a closing-basis breakout.

| basis | fires on |
|---|---|
| `close > max(prior 20 highs)` | 6.02% of bars |
| `close > max(prior 20 closes)` | 13.30% of bars |

Comparing a close against intraday highs made the condition roughly twice as
strict as intended, for no reason the spec states. The default is now
`basis="close"`; the stricter form remains available.

## The autopsy, after both fixes

STRATEGY_001 over 10,240 bar-observations (16 runs × 640 bars, four market
characters):

| gate | killed | survive | kill % |
|---|---|---|---|
| bars examined | | 10,240 | |
| regime tradeable | 480 | 9,760 | 4.7% |
| strategy type eligible (momentum) | 7,084 | 2,676 | **72.6%** |
| breakout above 20-day high | 2,334 | 342 | 87.2% |
| relative volume > 1.4 | 251 | 91 | 73.4% |
| RSI 55–80 | 11 | 80 | 12.1% |
| MACD histogram > 0 | 6 | 74 | 7.5% |
| within 5% of 52-week high | 35 | 39 | 47.3% |
| established uptrend | 2 | 37 | 5.1% |
| signal score ≥ regime minimum | 0 | 37 | 0.0% |

Answering the four questions directly:

1. **Regime**: 480 bars (4.7%) vetoed outright, then 7,084 (72.6%) because
   momentum is not eligible in the prevailing regime. Combined: **73.9%**.
2. **Relative volume > 1.4**: killed 251 bars — 73.4% of those reaching it,
   but only 2.5% of all bars, because the regime gate got there first.
3. **RSI and MACD**: 11 and 6 bars — **negligible**. By the time a breakout
   has volume behind it, momentum indicators are already aligned. Widening
   the RSI band would have bought almost nothing.
4. **52-week-high proximity**: 35 bars, 47.3% of survivors. Meaningful
   conditionally, small absolutely.

The score gate killed **nothing**: setups that clear the conditions score a
median 75.4 against a threshold of 60. Scoring is not the constraint.

## What was deliberately not changed

**The §5.4 regime eligibility matrix**, despite being the largest single
killer. It is what stops momentum being traded in a bear market. It is not an
§8.1 hard limit, but it is a risk control, and loosening the biggest number in
the table simply because it is the biggest number is how a risk-first system
stops being one.

**`relative_volume > 1.4`, `RSI 55–80`, `within 5% of 52-week high`.** All are
stated numerically in §6.2.1. The volume threshold in particular *looked* like
the culprit until the generator was fixed. `tests/test_signal_frequency.py`
asserts these three remain as written.

**Every §8.1 hard limit.** 0.5% risk per trade, 7% maximum stop, ES₉₅, and the
rest are unchanged and asserted by `TestHardLimitsUntouched`.

## Result

Portfolio-level, 8 symbols over 640 bars:

| | before | after |
|---|---|---|
| signals | ~3 | **10** |
| trades | ~2 | **9** |
| strategies participating | 1 | 2 (plus CAN SLIM now reachable) |

Inside the 10–15 target, achieved entirely by fixing two defects rather than
by loosening a single threshold.

## Finding 3: R-multiples were measured against the wrong risk

Surfaced by the new frequency tests: a trade closed **+268.34 and reported
R = 0.00**. R was computed against the *current* stop, so any winner whose
stop had trailed to breakeven showed zero risk and therefore zero R.

`avg_r_multiple` feeds both drift detection (§21) and strategy validation
(§20.2), so the defect flattened both toward zero — a strategy could decay
badly while its average R stayed reassuringly near zero.

`Position` now freezes `initial_risk_per_unit` and `initial_quantity` at
entry, and R is measured against the risk actually taken.

## Caveat

All of this is measured on synthetic data. It establishes that the pipeline is
appropriately *selective* — not that any strategy has an edge. The backtest
above loses money (PF 0.20, 22% win rate), which is the expected result of
trading a near-random series after costs. Signal frequency and profitability
are separate questions, and only real market history answers the second.
