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

---

# Second pass: findings from the complete pre-fix baseline

The first pass measured STRATEGY_001 only. Running the autopsy across all
eight equity strategies revealed that **five of them produced literally zero
signals**, which reframed the problem: this was never mainly a calibration
question.

| strategy | pre-fix signals / 640 bars |
|---|---|
| STRATEGY_001 Momentum Breakout | 1.6 |
| STRATEGY_002 Pullback | 0.1 |
| STRATEGY_003 Gap Up | **0.0** |
| STRATEGY_004 Reversal | **0.0** |
| STRATEGY_005 CAN SLIM Cup | **0.0** |
| STRATEGY_006 Double Bottom | 0.2 |
| STRATEGY_007 Flat Base | **0.0** |
| STRATEGY_008 High Tight Flag | **0.0** |

## Finding 4: the gap strategy was eligible in no regime at all

`strategy type eligible (gap)` killed **9,760 of 9,760 bars — 100.0%**.

§5.4 gives TREND_UP's disallowed column as *"Reversal, Gap (except gap-up),
Mean Reversion"*. The parenthetical carves gap-**up** out of the prohibition,
but the allowed column does not list gap either. Since the permission check
required explicit membership in the allowed set, a long gap-up continuation
was permitted nowhere: not forbidden, but never authorised.

Fixed by adding `gap` to TREND_UP's allowed set, matching the carve-out the
specification states.

## Finding 5: permission and veto were inconsistent

The same investigation exposed a broader asymmetry. Vetoes were evaluated
across *every* active regime — correct, and what makes a `TREND_UP` market
that is also `HIGH_VOLATILITY` refuse breakouts. But permissions were read
from the **primary regime alone**, so a strategy authorised by an active
`BULL_MARKET` backdrop was still blocked when the primary `TREND_UP` label
happened not to list it.

Permission is now a union across active regimes; the veto remains universal
and still overrides. Widening permission cannot authorise anything an active
regime objects to, which `test_veto_still_overrides_any_permission` asserts.

## Finding 6: counter-trend strategies were scored on a trend rubric

REVERSAL was eligible on 72.6% of bars and still produced zero signals. The
funnel showed why: **42 of 42 valid setups scored 42–64 against a threshold of
75**. Not one was ever close.

Component breakdown of those setups:

| component | mean | % of max |
|---|---|---|
| market_regime_alignment | 6.79 / 20 | 34% |
| **trend_alignment** | **4.30 / 15** | **29%** |
| setup_quality | 12.29 / 15 | 82% |
| **volume_confirmation** | **0.91 / 10** | **9%** |
| fundamentals | 8.27 / 10 | 83% |
| liquidity | 4.85 / 5 | 97% |

§7.1 defines `trend_alignment` as MA stack, ADX strength and higher
highs/lows, and `volume_confirmation` as expanding relative volume with rising
OBV. A reversal at support has none of these *by construction* — that absence
is the setup, not a defect in it.

This produced a three-way contradiction between sections that cannot all hold:

* §5.4 routes reversal and mean-reversion into RANGE and HIGH_VOLATILITY
* §7.2 assigns those regimes the **highest** minimum scores (70 and 75)
* §7.1 scores exactly those strategies **lowest**

Resolved by making both components family-aware, as `volatility_conditions`
already was. For counter-trend families:

* `trend_alignment` measures a constructive long-term backdrop to revert into
  (price above MA200), the *absence* of a strong trend to fight (low ADX
  scores high), and stretch from the mean.
* `volume_confirmation` rewards both exhaustion (dry-up: sellers finished) and
  capitulation (a spike flushing the last holders), which are the two textbook
  reversal signatures, rather than expanding up-volume.

Effect: reversal `trend_alignment` 29% → 51%, `volume_confirmation` 9% → 55%,
median total 49.5 → 59.7. Momentum scores are unchanged, asserted by
`test_trend_strategies_are_unaffected`.

## Where this was deliberately stopped

REVERSAL still rarely clears HIGH_VOLATILITY's threshold of 75 (1 of 55 in the
sweep). That threshold comes straight from §7.2 and was left alone.

The rubric was made *correct* — measuring what quality means for the family —
not *lenient*. Continuing to tune the confidence weighting until reversal
traded would have been optimising for an outcome on synthetic data, which is
the failure mode this whole exercise exists to avoid. Whether reversal clears
75 is a question for real market history.

## Combined result

| | before | after |
|---|---|---|
| portfolio signals / 640 bars | ~3 | **11** |
| trades / 640 bars | ~2 | **9** |
| strategies structurally able to fire | 3 of 8 | **8 of 8** |
| hard limits changed | — | **none** |
| tests | 180 | **205** |
