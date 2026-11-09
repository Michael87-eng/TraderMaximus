# Phase 1 recalibration — results

All work below is **EXECUTED** unless marked PROPOSED. Artifacts named.

---

## Task 1 — the three blockers

| blocker | status | evidence |
|---|---|---|
| 1a venue access / lot rounding | **INSUFFICIENT EVIDENCE** | see below |
| 1b data persistence | **RESOLVED** | `tools/verify_data.py`, 11/11 files verify |
| 1c commission default | **RESOLVED** | taker 10 bps / maker 5 bps, separated |

### 1a — INSUFFICIENT EVIDENCE. Lot rounding is NOT validated.

The specific blocker, named as instructed:

1. **Bybit, Coinbase and OKX are unreachable.** Not geo-blocked — refused at
   CONNECT by this environment's network policy, which does not list them.
   Testing them requires an environment change.
2. **Binance `api.binance.com` returns HTTP 451** (venue geo-restriction, not
   policy). `exchangeInfo` is unobtainable.
3. **Kraken is the one accessible venue** (`api.kraken.com` is allowlisted on
   the `tradermaximus-crypto` environment), and this session cannot reach it —
   a running session cannot be relocated between environments.
4. A session was dispatched to that environment to fetch Kraken
   `/0/public/AssetPairs` (`pair_decimals` → tick, `lot_decimals` → step,
   `ordermin`, `costmin` → min notional). **It failed on the account's 5-hour
   rate limit before completing** and pushed nothing. Verified: no
   `claude/crypto-venue-metadata` branch exists.

So `tick_size`, `step_size`, `min_quantity` and `min_notional` remain
**unverified against any venue**, and **no paper trade is authorised**. The
placeholders in `validate_crypto.py` are set fine enough not to bind, so they
cannot flatter a result, and that is the only claim made for them.

**PROPOSED next step:** re-dispatch that session after the rate limit resets.
It is a ~5-minute job; nothing else depends on it except paper authorisation.

### 1b — RESOLVED

`data/venue/` is tracked in git (force-added), so a container replacement can
no longer destroy the execution bars — that is the structural fix.
`tools/verify_data.py` checks every manifest-named file for existence, sha256,
row count, first/last bar, duplicate timestamps and staleness, and exits
non-zero on any failure. It never repairs and never downloads: a script that
silently re-fetched would hide the drift it exists to detect.

### 1c — RESOLVED

`commission_bps = 1.0` survived in both `BacktestConfig` and `PaperFillModel`;
only `validate_crypto.py` had overridden it. Now `taker_fee_bps = 10.0` and
`maker_fee_bps = 5.0`, reported separately, with `spread_bps = 2.0`. A
deprecated `commission_bps` property resolves to taker so a caller still
setting it is not silently ignored.

---

## Task 2 — regime matrix recalibration

### 2a — measured distributions (`data/venue/crypto_distributions.json`)

3,111 samples per pair, daily Binance bars. Pooled:

| measure | p10 | p25 | p50 | p75 | p90 |
|---|---|---|---|---|---|
| realized vol 20d, annualised | 33.5% | 44.5% | 59.8% | 77.9% | 97.8% |
| ATR(14) as % of price | 3.1% | 3.8% | **4.9%** | 6.3% | 7.8% |
| ADX(14) | 17.0 | 20.9 | 27.2 | 36.8 | 45.3 |
| drawdown from rolling peak | 11.3% | 24.9% | **43.7%** | 58.9% | 71.0% |

Phase durations: MA200 phases have a median of 4 bars but a p90 of 178 and a
max of 385 — most crossings are whipsaw and a few are real trends. ADX<20
phases run a median of 12 bars, max 48, so genuine range conditions are short.

### 2b — new thresholds and their basis

Implemented as `RegimeProfile`, so the equity path keeps its tested constants
unchanged and crypto gets crypto numbers.

| threshold | was | now | percentile basis |
|---|---|---|---|
| `correction_drawdown` | 8.0% | **58.9%** | p75 of drawdown-from-peak. 8% sits *below crypto's 10th percentile* (11.3%). |
| `trend_requires_calm` | true (vol_pct < 0.60) | **false** | crypto trends happen in high volatility; requiring calm first requires the trend to look like an equity trend |
| `range_max_adx` | ∞ (residual) | **20.9** | p25 of ADX(14) |
| `trend_min_adx` | 0 | **27.2** | p50 of ADX(14) |
| `direction_from_observables` | n/a | **true** | see the defect below |
| high/low volatility percentile | 0.75 / 0.25 | unchanged | `volatility_percentile` is self-relative and therefore already scale-free — **one hypothesis tested and rejected**: the volatility thresholds were *not* equity-calibrated |

### Five defects found during recalibration

Each one meant the system was measuring something other than what it claimed.
None produced an error.

| # | defect | evidence | consequence |
|---|---|---|---|
| 1 | **The composite regime score cannot express a downtrend.** `TREND_DOWN` requires `score < 0.30`; the score occupies [0.365, 0.826] at p10–p99 and **0.0% of 1,017 sampled BTC bars** fall below 0.30 — across a span containing both the 2018 and 2022 bear markets. | measured | `TREND_DOWN` was **0.00% of all bars**. Downtrends were being classified RANGE. |
| 2 | `MARKET_CORRECTION` at an 8% drawdown | p10 is 11.3% | 14.6% of bars permitted no trading on a threshold the asset class is almost never above |
| 3 | `RANGE` was the residual classification, and forbids momentum/breakout/CAN SLIM | 54.6% of bars | trend-following vetoed by a *default* label |
| 4 | **The backtester hardcoded its benchmark instrument as `AssetClass.EQUITIES`** (`backtest.py:262`) | code | the regime engine picks its profile from the features' asset class, so **a crypto recalibration had literally no effect on a crypto backtest**. Caught because the trade count did not move. |
| 5 | **No exit path recorded the regime.** `reduce_position(regime="UNKNOWN")` was the default and nothing overrode it. | code | every `TradeRecord` read `UNKNOWN`, making gate **P3 (distinct regimes traded) unpassable by construction** — it always reported 1 |

Defects 4 and 5 are the important ones: without them the recalibration would
have appeared to do nothing, and a gate would have failed forever for reasons
unrelated to the market.

### 2c / 2d / 2e — RANGE, axis separation, and the before/after

**RANGE now requires positive evidence** (ADX ≤ 20.9). **UNKNOWN is the
residual** and is not traded. **Volatility is a separate axis that no longer
vetoes** — `VOLATILITY_REGIMES` are skipped in `is_strategy_type_allowed` and
instead cut size via `REGIME_FAMILY_SIZE_MULTIPLIER`, which gained
`momentum`/`breakout` at 0.5× in HIGH_VOLATILITY and VOLATILITY_EXPANSION so
the protection changed form rather than being deleted.

Regime distribution, BTCUSDT, 3,051 classified bars:

| regime | before | after | change |
|---|---|---|---|
| TREND_UP | 30.81% | 27.43% | −3.38% |
| **TREND_DOWN** | **0.00%** | **13.54%** | **+13.54%** |
| RANGE | 54.64% | 27.73% | −26.91% |
| UNKNOWN | 0.00% | 24.19% | +24.19% |
| MARKET_CORRECTION | 14.55% | 3.15% | −11.41% |
| BEAR_MARKET | 0.00% | 3.97% | +3.97% |
| permits no trading | 14.55% | 44.84% | +30.29% |

Family veto rates:

| family | veto before | veto after | change |
|---|---|---|---|
| momentum | 69.19% | 72.57% | **+3.38%** |
| breakout | 69.19% | 55.29% | −13.90% |
| mean_reversion | 14.55% | 33.73% | +19.17% |

### The veto rate did NOT fall substantially. Documented as instructed.

The brief said: *"The veto rate should fall substantially; if it does not, the
recalibration is wrong and must be documented as such."*

It did not. Momentum's veto rate rose 3.38 points. Here is the honest reading,
and it is not "the recalibration is wrong" so much as "the metric moved for a
reason the metric cannot show":

- In the **funnel**, which measures the pipeline rather than the matrix, the
  family-veto stage **halved: 36.31% → 17.19%** of all bar-evaluations.
- Momentum's *permitted* set is now 27.43% of bars — exactly the share in a
  confirmed uptrend. That is what momentum's opportunity set *should* be.
- The old 69% veto was **coincidentally similar and wrongly composed**: it
  included 54.6% RANGE, of which 13.5 points were actually downtrends that the
  score could not express (defect 1). The system was permitting trades in bars
  that were downtrends mislabelled RANGE.
- `permits no trading` tripled because `TREND_DOWN` and `UNKNOWN` became real
  classifications that correctly block a long-only system.

So the veto rate is roughly unchanged and is now **correct** rather than
accidental. That is a real improvement that the headline number hides, and the
brief's predicted direction was not met.

---

## Task 3 — the 200DMA as a gated candidate (`STRATEGY_014`)

### Through the system's own gates: rejected on ~100% of bars

BTCUSDT, 3,071 bars, `tools/crypto_funnel.py --strategy STRATEGY_014`:

| stage | count | % |
|---|---|---|
| regime permits no new positions | 1,388 | 45.20% |
| family vetoed by regime (momentum in RANGE) | 846 | 27.55% |
| **entry conditions not met** | **0** | **0.00%** |
| **score below family minimum** | **837** | **27.25%** |
| **AUTHORISED** | **0** | **0.00%** |

The entry condition fires perfectly. Then **every one of the 837 surviving
bars is rejected by the signal score**: 48.0 against momentum's 75.

Score decomposition on a representative TREND_UP bar (2019-04-05, confidence 0.91):

| component | scored | max | % |
|---|---|---|---|
| market_regime_alignment | 14.15 | 22.22 | 64% |
| setup_quality | 13.33 | 16.67 | 80% |
| trend_alignment | 7.75 | 16.67 | 47% |
| volume_confirmation | 5.45 | 11.11 | 49% |
| volatility_conditions | 5.08 | 11.11 | 46% |
| liquidity | 2.22 | 5.56 | 40% |
| **relative_strength** | **0.00** | 11.11 | **0%** |
| **risk_reward** | **0.00** | 5.56 | **0%** |
| **total** | **48.0** | 100 | shortfall **27.0** |

Two of those zeros are **structural, not performance**:

- `relative_strength` is 0 because the strategy's relative strength is computed
  against the benchmark, and **BTC's benchmark is BTC**. Excess return is
  identically zero, so the rating is exactly neutral and scores nothing.
- `risk_reward` is 0 because the strategy deliberately has **no ATR stop** —
  the moving average is the exit. No stop distance, no ratio.

16.67 of 100 points are unreachable for this strategy before the market is
consulted. The remainder is a rubric that rewards *breadth of confirmation*: a
one-condition rule cannot accumulate what a six-condition rule accumulates,
however well it performs.

### Standalone: walk-forward and holdout (`tools/validate_200dma.py`)

The system provably rejects this rule, so running it through the backtester
measures the architecture. Measured directly, same bars, same costs, same folds,
same reserved holdout:

| | BTCUSDT | ETHUSDT |
|---|---|---|
| in-sample return (research span) | 16,583.8% | 23,629.8% |
| in-sample Sharpe / maxDD / invested | 1.69 / 35.7% / 57% | 1.48 / 55.0% / 55% |
| folds with PF > 1.3 | **3/5** | **4/5** |
| folds positive | 4/5 | 4/5 |
| pooled OOS trades | 31 | 18 |
| pooled OOS profit factor | **3.29** | **9.33** |
| pooled OOS win rate | 29.0% | 38.9% |
| block bootstrap | 88.8% | 97.6% |
| **holdout (once, 365 bars)** | **+15.5%, Sharpe 0.81, maxDD 13.6%** | **+21.9%, Sharpe 0.83, maxDD 19.4%** |

The one losing fold on each pair is wf_1 — a bear-market window where the rule
correctly sat in cash 12–16% of the time and still lost 18–27%.

### Verdict: FAIL against the predeclared gates

| gate | BTC | ETH |
|---|---|---|
| P1 min OOS trades ≥ 60 | **FAIL** (31) | **FAIL** (18) |
| P4 max drawdown < 25% | **FAIL** (32.7%) | **FAIL** (55.0%) |
| P5 net OOS PF > 1.25 | PASS (3.29) | PASS (9.33) |
| P8 bootstrap ≥ 90% | **FAIL** (88.8%) | PASS (97.6%) |
| E2 every fold PF > 1.3 | **FAIL** (3/5) | **FAIL** (4/5) |
| holdout Sharpe > 0.60 | PASS (0.81) | PASS (0.83) |
| **overall** | **FAIL** | **FAIL** |

**This is now a question about the gates, not the strategy — and I am not
going to answer it by moving them.** Three of the failures are structural
incompatibilities between the gates and a low-frequency strategy:

- **P1 (60 trades)** is unreachable for a rule that makes **35 round trips in
  eight years**. Even the holdout produced 1–2 trades. A trade-count floor and
  a low-frequency strategy cannot both be satisfied.
- **P4 (25% drawdown)** is below what a crypto trend strategy produces while
  riding a bull market. I derived 25% from the p75 drawdown-from-peak and that
  was still too tight.
- **E2 (every fold PF > 1.3)** demands a strategy that never loses in any
  one-year window. wf_1 is a bear market.

Changing a gate after seeing the result it rejects is the one thing the
protocol forbids. **PROPOSED:** revise P1 toward independent *periods* rather
than trades, raise P4 for the long horizon, and replace E2 with a majority
rule — then test against a window not used here. That revision is justified by
the structural argument above, not by the result.

---

## Task 4 — the three horizons, recalibrated

| horizon | trades | net PF | folds PF>1.3 | bootstrap | net P&L | verdict |
|---|---:|---:|---:|---:|---:|---|
| Short (1h) | 54 | 0.78 | 2/5 | 11.8% | **−705.18** | INSUFFICIENT EVIDENCE |
| Medium (1d) | 1 | inf | 1/5 | n/a | +303.65 | INSUFFICIENT EVIDENCE |
| Long (1d) | 1 | inf | 1/5 | n/a | +303.65 | INSUFFICIENT EVIDENCE |

Per strategy: `STRATEGY_004` 3 authorisations, `STRATEGY_012` 1,
`STRATEGY_013` 1, `STRATEGY_014` 0–1, `STRATEGY_002`/`009` zero. Total **5
authorisations in 30,710 bar-evaluations (0.02%)**.

### Was the 89-trade short sample biased by the mis-specified matrix? YES.

| | before recalibration | after |
|---|---|---|
| OOS trades | 89 | 54 |
| net P&L | −2,456.75 | **−705.18** |
| net PF | 0.63 | **0.78** |
| folds with PF ≥ 1.0 | **0/5** | **3/5** |
| bootstrap | 10.1% | 11.8% |

The previous **FAIL (negative expectancy demonstrated)** does **not** survive a
clean regime sample. Three of five folds are now positive (1.19, 1.92, 2.97),
so the result is no longer uniformly negative and my own
`conclusively_negative` rule correctly stops firing. It is still net negative
and still fails every performance gate, but the honest label is **INSUFFICIENT
EVIDENCE, leaning negative** — not a demonstrated negative edge.

**I was wrong to call it demonstrated.** The brief hypothesised the sample was
biased; it was.

### Do medium and long produce enough trades to be measurable? NO.

1 trade in 8 years. The brief's own test applies: *"If they still produce 2
trades in 8 years, the horizon specification is wrong, not the regime matrix."*

The horizon specification is not obviously wrong — `STRATEGY_014` is a
medium/long-horizon rule and makes 35 round trips in the same window
standalone. What blocks it is the **signal score**, measured above. The
constraint has moved from the regime matrix to the scoring rubric.

### One hard-limit finding, reported and NOT changed

Crypto `max_stop_pct` is **5%** and the median ATR(14) is **4.9% of price**.
Verified against the stop engine: a 2× ATR stop is clamped from 9.8% to 5.0%
at the median and from 11.4% to 5.0% at ETH's typical ATR. **Every crypto stop
sits inside one day's typical range**, which is a coherent explanation for the
5.9%–55.6% per-fold win rates.

Widening it would not raise risk per trade — the budget divided by a wider stop
simply buys a smaller position — but `max_stop_pct` is a hard risk control and
the brief forbids relaxing one. **Reported for your decision, not changed.**
