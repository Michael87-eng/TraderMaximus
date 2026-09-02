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

---

# Round 3 — making the FOREX strategy tradeable

Round 2 left the system generating signals on synthetic equities but taking
**zero trades on real market data**, across all three asset classes. This round
addressed that on FOREX, where the diagnosis was cleanest. Every change below is
in the signal-generation path; no section 8.1 hard limit was touched, and
`TestHardLimitsUntouched` still asserts all seventeen of them verbatim.

## What was actually blocking

Measured on 2,500 daily bars each of real EURUSD and GBPUSD (2017-01 to
2026-08), sampling every bar past warm-up:

| gate | pass rate |
|---|---|
| `rsi_14 < 35` or `> 65` | 4–5% of bars |
| `bb_position < 0.05` or `> 0.95` | 11–14% of bars |
| a reversal candle pattern at support/resistance | 7–8% of bars |
| all three together | **18 bar-samples in nine years** |

Of those 18, exactly **1** cleared its score threshold. FX is range-bound: its
RSI rarely travels to 30/70, so the RSI gate alone was doing almost all the
excluding, and it was excluding almost everything.

## The fallback ladder

The conservative-fallback protocol was run as a measured ladder rather than a
guess, on the same 2,180 bar-samples:

| stage | entry conditions | threshold | triggers | qualifying | long | short |
|---|---|---|---|---|---|---|
| A | bb + rsi + pattern | 60 | 18 | 1 | 0 | 1 |
| B | bb + pattern | 50 | 63 | **30** | 7 | 23 |
| C | bb only | 50 | 919 | 532 | 234 | 298 |

Stage B was taken. Stage C removes the only remaining confirmation and turns
the strategy into "trade every band touch", which is not a strategy.

## Changes made

1. **`stretched_from_mean` removed** from the entry conjunction. It measured
   displacement from the 20-period mean in ATRs; `bb_position` at a band
   extreme measures the same displacement in standard deviations, and on FX
   those are close to the same number. ATR displacement now selects the
   *direction* instead of acting as a second gate.
2. **`bb_position` widened** from 0.05 to 0.20. §6.2.10 says "near the lower
   band"; 0.05 demanded price at or through it.
3. **The short side was implemented.** §6.2.10 specifies both directions
   ("RSI > 70 (short) or < 30 (long)") and only the long half existed, so the
   library could not trade short at all. `_bearish_reversal_at_resistance` is
   the exact mirror of `_bullish_reversal_at_support`; STRATEGY_011 is a
   separate spec because it is scored separately.
4. **The RSI gate was dropped** from both mean-reversion specs. This is a
   deliberate departure from §6.2.10's stated 30/70, recorded here so it is
   visible rather than buried: the entry is now "a reversal candle at a band
   extreme".
5. **Family-specific score thresholds** (`FAMILY_MIN_SCORE`) replace the
   regime-derived bar's *height*. Momentum, breakout, gap and short stay at 75
   untouched; CAN SLIM, institutional and pullback at 65; reversal and
   oscillator at 60; mean reversion at 50 under the fallback. A regime that
   permits no new positions still vetoes outright.
6. **Mean reversion is no longer vetoed by trend regimes.** It is permitted
   wherever trading is permitted and sized at **0.5x** in HIGH_VOLATILITY and
   VOLATILITY_EXPANSION (`REGIME_FAMILY_SIZE_MULTIPLIER`) instead.

## Two defects the changes exposed

Both were pre-existing and both were invisible until the system traded short.

**The strategy engine and the risk engine disagreed about the bar.** Family
thresholds were added to the strategy engine only. The risk engine went on
re-judging the survivor against the *regime* bar, so every mean-reversion
signal passed at 50 was rejected one layer later at 65–75 and the trade count
stayed at zero while the signal count rose. `effective_minimum_score` now
serves both layers. Pinned by `TestThresholdAgreement`.

**Opening a short inflated equity by its full notional.** Opening a position
credits or debits cash by `-quantity * price * side.sign`, so a short's
proceeds are already in cash; the equity property then added the short's entry
notional *again*. While the library was long-only this term never fired. With
39 of 48 FOREX trades short, every risk quantity computed as a fraction of
equity — the risk budget, the per-trade exposure cap, drawdown, and the return
series behind VaR and ES — was measured against equity that did not exist. The
visible symptom was one position sized at 5.22% of equity against a 5.0% cap,
and a reported max drawdown of 8.9% that fell to 0.4% once corrected. Pinned by
`TestShortEquityAccounting`.

## Result on real FOREX history

EURUSD + GBPUSD, 2017-01 to 2026-08, 2,500 daily bars each, 300-bar warm-up,
$100k starting equity, chronological 70/30 split.

| | in-sample | out-of-sample | full span |
|---|---|---|---|
| trades | 28 | 20 | 48 |
| win rate | 42.9% | 35.0% | 39.6% |
| profit factor | 1.47 | **1.00** | 1.24 |
| Sharpe (252/yr) | 0.35 | **-0.01** | 0.21 |
| max drawdown | 0.4% | 0.4% | 0.5% |

| | trades | win rate | PF | avg R |
|---|---|---|---|---|
| STRATEGY_010 (long) | 9 | 33.3% | 1.29 | +0.18 |
| STRATEGY_011 (short) | 39 | 41.0% | 1.23 | +0.14 |

Walk-forward 3/3 windows profitable, mean PF 1.18. Monte Carlo (bootstrap)
75.9% of resamples profitable, against a 95% acceptance bar. Average win
$133.23 against average loss $70.18, a 1.90 ratio.

Risk compliance across all 48 trades: worst committed risk 0.168% against the
0.5% cap, worst exposure 4.94% against the 5.0% cap, worst loss -1.00R, **zero
violations**.

## Verdict

The system trades, in both directions, within every limit. It does not clear
the deployment gate: out-of-sample profit factor is 1.00 against a required
1.3, and out-of-sample Sharpe is -0.01 against a required 0.7. The
out-of-sample edge is indistinguishable from zero, and the drop from an
in-sample PF of 1.47 to 1.00 is the signature of a specification fitted to the
period it was written in. **Do not deploy.**
