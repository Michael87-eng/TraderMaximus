# EQUITIES validation

Bar interval **1w**, Sharpe annualised at 52/yr. Chronological 70/30 split; no decision is ever made on a bar that has already been scored.

## Data

| symbol | bars | span | quality |
|---|---|---|---|
| AAPL | 1399 | 1999-11-12 to 2026-08-28 | 1.00 (OK) |
| MSFT | 1399 | 1999-11-12 to 2026-08-28 | 1.00 (OK) |
| NVDA | 1400 | 1999-11-12 to 2026-09-01 | 1.00 (OK) |
| SPY | 1399 | 1999-11-12 to 2026-08-28 | 1.00 (OK) |
| TSLA | 844 | 2010-07-09 to 2026-09-01 | 1.00 (OK) |

Realised volatility (annualised): AAPL 27%, MSFT 46%, NVDA 35%, SPY 12%, TSLA 47%

## Segments

| segment | trades | win rate | profit factor | Sharpe | max DD | return |
|---|---|---|---|---|---|---|
| in_sample | 2 | 100.0% | inf | 0.31 | 0.17% | 0.40% |
| out_of_sample | 1 | 0.0% | 0.00 | -0.33 | 0.05% | -0.04% |

## Walk-forward

Rolling folds, anchored to the end of history. Each fold's train window is the backtester's warm-up, so only the test window is scored and test windows never overlap.

| fold | train bars | test bars | trades | win rate | PF | Sharpe |
|---|---|---|---|---|---|---|
| wf_0 | 300 | 52 | 0 | 0.0% | 0.00 | 0.00 |
| wf_1 | 300 | 52 | 0 | 0.0% | 0.00 | 0.00 |
| wf_2 | 300 | 52 | 0 | 0.0% | 0.00 | 0.00 |
| wf_3 | 300 | 52 | 0 | 0.0% | 0.00 | 0.00 |
| wf_4 | 300 | 52 | 0 | 0.0% | 0.00 | 0.00 |

## Where signals went

9 signals generated, 6 rejected by the risk engine.

| rejection rule | count |
|---|---|
| `R00_STOP_PLACEMENT_REJECTED` | 6 |

## Per strategy

| strategy | trades | win rate | PF | avg R |
|---|---|---|---|---|
| STRATEGY_004 | 3 | 66.7% | 10.73 | 0.35 |

## Deployment gates

| gate | verdict | detail |
|---|---|---|
| OOS PF > 1.3 and Sharpe > 0.7 | **FAIL** | OOS PF 0.00 (need >1.3), Sharpe -0.33 (need >0.7), 1 trades |
| walk-forward: 5 folds, each PF > 1.3 | **FAIL** | 0/5 windows above PF 1.3 [0.00, 0.00, 0.00, 0.00, 0.00] |
| Monte Carlo: >=90% profitable | **FAIL** | too few trades to resample (fewer than 10) |
| max drawdown < 5% | PASS | worst segment drawdown 0.17% (need <5%) |
| OOS Sharpe >= 80% of IS | **FAIL** | OOS Sharpe -0.33 is -107% of in-sample 0.31 (need >=80%) |

**Overall: FAIL**
