# Crypto funnel: where every candidate dies

**EXECUTED.** `tools/crypto_funnel.py`, run on BTC-USD (4,701 bars, 2013-12-27 →
2026-11-09) and ETH-USD (4,112 bars, 2015-08-08 → 2026-11-09), daily, 300-bar
warm-up, 5 crypto-eligible strategies. Data provenance and SHA-256:
`data/real/crypto/MANIFEST.json`. Feed is an **aggregated reference composite**,
not venue data — see the manifest's `basis_risk` field.

## The measurement bug found inside the measurement tool

Worth leading with, because it is the same class of error as the 1.47 false
positive and it was committed *by the tool written to prevent it*.

The first revision of the funnel called `features.nearest_support()` directly
instead of the spec's own `spec.structure_level()` accessor, which honours
`StopSpec.use_structure`. It therefore handed a structural stop level to
strategies that had explicitly declared `use_structure=False`, and invented
stop rejections the real pipeline would never produce.

| | authorised | stop rejections |
|---|---|---|
| funnel with the parity bug | **5** | 38 |
| funnel using the spec's accessor | **43** | 0 |

**An 8.6× difference in the headline number, from a tool reimplementing one
line of the rules it was measuring.** Nothing errored. Both runs looked
plausible. The fix is in the file with the reasoning attached, so the next
reader sees why the accessor is mandatory rather than stylistic.

## Combined funnel — 41,065 bar-evaluations

Ordered stages; a bar is attributed to the **first** one it fails, so the column
sums to the bars examined.

| stage | count | % of bars |
|---|---|---|
| bar not eligible (warm-up) | 0 | 0.00% |
| regime permits no new positions | 6,115 | 14.89% |
| **strategy family vetoed by regime** | **14,910** | **36.31%** |
| entry conditions not met | 18,038 | 43.93% |
| score below family minimum | 1,959 | 4.77% |
| stop rejected | 0 | 0.00% |
| sizing rejected | 0 | 0.00% |
| risk engine rejected | 0 | 0.00% |
| **AUTHORISED** | **43** | **0.10%** |

**The risk engine is not the constraint.** Zero sizing rejections, zero
authorisation rejections, zero stop rejections. Every candidate that reaches
risk is approved. 51.2% of all bars die before the strategy is even evaluated.

## Regime distribution on BTC-USD (4,381 classified bars)

| primary regime | bars | share |
|---|---|---|
| RANGE | 1,632 | 37.25% |
| TREND_UP | 1,417 | 32.34% |
| HIGH_VOLATILITY | 676 | 15.43% |
| MARKET_CORRECTION | 656 | **14.97% — permits nothing** |

### Family veto rates — the headline structural defect

| family | vetoed on | where |
|---|---|---|
| **momentum** | **67.66%** | RANGE, HIGH_VOLATILITY, MARKET_CORRECTION |
| **breakout** | **67.66%** | RANGE, HIGH_VOLATILITY, MARKET_CORRECTION |
| reversal | 47.32% | TREND_UP, MARKET_CORRECTION |
| pullback | 36.54% | RANGE, MARKET_CORRECTION, HIGH_VOLATILITY |
| institutional | 36.54% | RANGE, MARKET_CORRECTION, HIGH_VOLATILITY |
| mean_reversion | 14.97% | MARKET_CORRECTION only |

**Trend-following is forbidden on two-thirds of all crypto bars.** Time-series
momentum and Donchian breakout are the two most-documented crypto effects in
the public literature, and this system's regime matrix — inherited unchanged
from the equity specification — vetoes them on 67.66% of bars before any
condition is tested.

The two crypto-native strategies added this session (`STRATEGY_012` trend,
`STRATEGY_013` Donchian) authorised **zero** trades across 12.8 years of BTC.
`STRATEGY_012`'s own funnel: 52.81% vetoed by regime (`momentum is disallowed
in RANGE`), then 19.52% of survivors scored below the 75 momentum bar
(a representative failure scored 53.9).

Two mechanisms, both equity-calibrated:

1. **RANGE is the fall-through classification.** `TREND_UP` requires
   `score > 0.70` *and* `vol_pct < 0.60` (`regime/engine.py:660`). Crypto
   rarely satisfies both at once, so it lands in RANGE — where momentum,
   breakout and CAN SLIM are explicitly disallowed.
2. **`MARKET_CORRECTION` fires on an 8% drawdown** (`engine.py:650`:
   `drawdown > 0.08 and score < 0.4`) and permits no new positions. In equities
   an 8% drawdown is a correction; in crypto it is routine. This alone removes
   14.97% of all bars.

One hypothesis tested and **rejected**: that the volatility thresholds were
equity-calibrated. They are not — `volatility_percentile` is measured against
the instrument's own 250-bar history, so it is scale-free and correct as
written. The defect is in the *eligibility matrix* and the *drawdown
threshold*, not in volatility measurement.

## Three further dead-specification defects found

All three meant the backtester simulated a different strategy than the one
specified. None produced an error.

| defect | evidence | consequence |
|---|---|---|
| `invalidation_conditions` declared on every spec, `check_invalidation()` implemented, **never called** by the backtester or orchestrator | no call site existed | a strategy's own stated exit rule was ignored; exits came only from stop, target, trailing and time |
| Time stop **hardcoded to 60 days**, ignoring `spec.max_holding_days` | `backtest.py`, inline `if holding_days > 60` | the FOREX mean-reversion specs declare a **5-day** limit and were held up to **60** — twelve times their stated horizon. Every previous conclusion about their holding behaviour described a strategy that was never specified. |
| `StopSpec.use_structure` **is** honoured via `spec.structure_level()` | `spec.py:168` | not a defect — but it is honoured only if callers use the accessor, which is how the funnel bug above happened |

The first two are fixed in this commit with the reasoning recorded inline.

## What this does and does not establish

**Establishes:** the crypto path's binding constraint is the regime eligibility
matrix, not risk, not sizing, not stops, not data quality. 51.2% of bars are
rejected before a strategy is evaluated, and trend-following specifically is
vetoed on 67.66%.

**Does not establish:** that any crypto strategy has an edge. 43
authorisations across 41,065 bar-evaluations is not a sample. No expectancy,
profit factor or Sharpe is reported here, because none would mean anything.

## Why the veto matrix was NOT changed in this commit

Relaxing the matrix would immediately multiply the signal count, and that is
indistinguishable — from the outside and from the equity curve — from loosening
a gate to manufacture sample size. The brief forbids exactly that, and the 1.47
false positive is what it looks like when the rule is broken.

The disciplined sequence is: derive a crypto regime matrix from economic
reasoning, **predeclare it in writing with the gates**, freeze it, then measure
once. That is Phase 4 work and it has not been done. Until it is, every number
above describes the current system, and the current system authorises 0.10% of
bars.
