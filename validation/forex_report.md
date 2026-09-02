# FOREX validation

Bar interval **1d**, Sharpe annualised at 252/yr. Chronological 70/30 split; no decision is ever made on a bar that has already been scored.

## Data

| symbol | bars | span | quality |
|---|---|---|---|
| EURUSD | 2500 | 2017-01-30 to 2026-08-28 | 1.00 (OK) |
| GBPUSD | 2500 | 2017-01-30 to 2026-08-28 | 1.00 (OK) |

Realised volatility (annualised): EURUSD 5%, GBPUSD 4%

## Segments

| segment | trades | win rate | profit factor | Sharpe | max DD | return |
|---|---|---|---|---|---|---|
| in_sample | 43 | 39.5% | 1.08 | 0.07 | 0.59% | 0.13% |
| out_of_sample | 24 | 33.3% | 0.75 | -0.32 | 0.67% | -0.27% |

## Walk-forward

Rolling folds, anchored to the end of history. Each fold's train window is the backtester's warm-up, so only the test window is scored and test windows never overlap.

| fold | train bars | test bars | trades | win rate | PF | Sharpe |
|---|---|---|---|---|---|---|
| wf_0 | 756 | 252 | 4 | 50.0% | 4.21 | 1.84 |
| wf_1 | 756 | 252 | 12 | 33.3% | 0.65 | -0.50 |
| wf_2 | 756 | 252 | 7 | 14.3% | 0.17 | -1.19 |
| wf_3 | 756 | 252 | 10 | 10.0% | 0.31 | -1.56 |
| wf_4 | 756 | 252 | 8 | 62.5% | 1.91 | 0.63 |

## Where signals went

67 signals generated, 0 rejected by the risk engine.

## Per strategy

| strategy | trades | win rate | PF | avg R |
|---|---|---|---|---|
| STRATEGY_004 | 2 | 0.0% | 0.00 | -1.00 |
| STRATEGY_010 | 16 | 31.2% | 0.82 | -0.22 |
| STRATEGY_011 | 49 | 40.8% | 1.08 | 0.01 |

## Deployment gates

| gate | verdict | detail |
|---|---|---|
| OOS PF > 1.3 and Sharpe > 0.7 | **FAIL** | OOS PF 0.75 (need >1.3), Sharpe -0.32 (need >0.7), 24 trades |
| walk-forward: 5 folds, each PF > 1.3 | **FAIL** | 2/5 windows above PF 1.3 [4.21, 0.65, 0.17, 0.31, 1.91] |
| Monte Carlo: >=90% profitable | **FAIL** | 42.7% of 5000 simulations profitable (need >=90%) |
| max drawdown < 5% | PASS | worst segment drawdown 0.67% (need <5%) |
| OOS Sharpe >= 80% of IS | **FAIL** | OOS Sharpe -0.32 is -438% of in-sample 0.07 (need >=80%) |

**Overall: FAIL**
