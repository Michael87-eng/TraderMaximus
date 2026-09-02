# CRYPTO validation

Bar interval **1d**, Sharpe annualised at 365/yr. Chronological 70/30 split; no decision is ever made on a bar that has already been scored.

## Data

| symbol | bars | span | quality |
|---|---|---|---|
| BTC-USD | 2500 | 2019-10-26 to 2026-08-29 | 1.00 (OK) |
| ETH-USD | 2500 | 2019-10-26 to 2026-08-29 | 1.00 (OK) |
| SOL-USD | 2338 | 2020-04-09 to 2026-09-02 | 1.00 (OK) |

Realised volatility (annualised): BTC-USD 50%, ETH-USD 79%, SOL-USD 71%

## Segments

| segment | trades | win rate | profit factor | Sharpe | max DD | return |
|---|---|---|---|---|---|---|
| in_sample | 1 | 100.0% | inf | 0.49 | 0.00% | 0.36% |
| out_of_sample | 5 | 20.0% | 1.75 | 0.20 | 0.66% | 0.21% |

## Walk-forward

Rolling folds, anchored to the end of history. Each fold's train window is the backtester's warm-up, so only the test window is scored and test windows never overlap.

| fold | train bars | test bars | trades | win rate | PF | Sharpe |
|---|---|---|---|---|---|---|
| wf_0 | 1095 | 365 | 0 | 0.0% | 0.00 | 0.00 |
| wf_1 | 1095 | 365 | 2 | 0.0% | 0.00 | -0.39 |
| wf_2 | 1095 | 365 | 1 | 0.0% | 0.00 | 0.00 |

## Where signals went

6 signals generated, 0 rejected by the risk engine.

## Per strategy

| strategy | trades | win rate | PF | avg R |
|---|---|---|---|---|
| STRATEGY_004 | 6 | 33.3% | 3.01 | 0.33 |

## Deployment gates

| gate | verdict | detail |
|---|---|---|
| OOS PF > 1.3 and Sharpe > 0.7 | **FAIL** | OOS PF 1.75 (need >1.3), Sharpe 0.20 (need >0.7), 5 trades |
| walk-forward: 5 folds, each PF > 1.3 | **FAIL** | 3 of 5 folds ran |
| Monte Carlo: >=90% profitable | **FAIL** | too few trades to resample (fewer than 10) |
| max drawdown < 5% | PASS | worst segment drawdown 0.66% (need <5%) |
| OOS Sharpe >= 80% of IS | **FAIL** | OOS Sharpe 0.20 is 41% of in-sample 0.49 (need >=80%) |

**Overall: FAIL**
