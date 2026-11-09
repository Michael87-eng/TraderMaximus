# Crypto: decision, inventory, runbook, plan

Companion to `docs/CRYPTO_PHASE0_AUDIT.md` (audit),
`docs/CRYPTO_FUNNEL_FINDINGS.md` (where candidates die),
`docs/CRYPTO_PREDECLARATION.md` (gates, committed before measurement) and
`docs/CRYPTO_VENUE_DATA.md` (data provenance and defects).

**Live trading is disabled throughout and nothing here authorises it.**

---

## 1. Scorecard

Protocol: `docs/CRYPTO_PREDECLARATION.md`, committed at `a086730` **before** any
run. Command: `python tools/validate_crypto.py --horizon {short|medium|long}`.
Data: Binance venue archive, `data/venue/MANIFEST.json`. Costs: 10 bps fee/side
+ 2 bps spread + 5 bps slippage ≈ 34 bps round trip.

| horizon | OOS trades | net PF | Sharpe | folds PF>1.3 | bootstrap | net P&L | **verdict** |
|---|---:|---:|---:|---:|---:|---:|---|
| Short (1h) | 89 | **0.63** | 0.00 | **0 / 5** | **10.1%** | **−2,456.75** | **FAIL — negative expectancy demonstrated** |
| Medium (1d) | 2 | 1.94 | 0.00 | 1 / 5 | not computable | +147.10 | **INSUFFICIENT EVIDENCE** |
| Long (1d) | 2 | 1.94 | 0.00 | 1 / 5 | not computable | +147.10 | **INSUFFICIENT EVIDENCE** |

Short-horizon fold detail — the only horizon with a usable sample:

| fold | trades | win rate | net PF | Sharpe |
|---|---:|---:|---:|---:|
| wf_0 | 19 | 21.1% | 0.32 | −0.52 |
| wf_1 | 20 | 25.0% | 0.74 | −0.19 |
| wf_2 | 14 | 14.3% | 0.74 | −0.20 |
| wf_3 | 20 | 20.0% | 0.61 | −0.35 |
| wf_4 | 16 | 18.8% | 0.87 | −0.08 |

**Five independent folds, five negative.** 90% of block-bootstrap resamples
lose money. This is not an absence of evidence; it is evidence of absence.

### Why the short horizon is FAIL and not INSUFFICIENT EVIDENCE

89 trades is below the predeclared 200-trade floor. That floor exists to stop a
thin sample being presented as a *positive* edge — the 5-trade "profit factor
1.75" this audit began with. It must not also stop a negative result being
called what it is.

The bar is deliberately asymmetric: it takes more evidence to commit capital
than to decline to. A result that is directionally negative in every fold is a
FAIL below the floor, where the same sample could never support a PASS. That
rule is now in the code (`validate_crypto.py`), not applied by hand.

---

## 2. The benchmark comparison is the real finding

| benchmark | return | Sharpe | max DD | invested | round trips |
|---|---:|---:|---:|---:|---:|
| B1 cash (USD) | 0.0% | 0.00 | 0.0% | 0% | 0 |
| B2 buy-and-hold BTC | 2,281.9% | 0.91 | 83.2% | 100% | 1 |
| B3 buy-and-hold ETH | 1,023.6% | 0.78 | 94.0% | 100% | 1 |
| B4 50/50 monthly rebalance | 2,460.6% | 0.91 | 87.3% | 100% | 1 |
| **B5 BTC 200-day MA to cash** | **16,583.8%** | **1.69** | **35.7%** | **57%** | **35** |

Daily bars, 2017-08-17 → 2025-11-08, same 34 bps cost model, holdout excluded.

B5 is about fifteen lines of arithmetic. It was verified with a standalone
script using **none** of this repository's code: 166.8×, 57% invested. It beats
buy-and-hold BTC on every axis simultaneously — roughly 7× the return, double
the Sharpe, under half the drawdown, half the time exposed — at **35 round
trips in eight years**, which is implementable by hand.

TraderMaximus, across 13 strategies, 286 tests, a regime engine, a scoring
engine and a full risk engine, took **2 trades and made $147 on $100,000** at
the medium horizon, and **lost $2,457 over 89 trades** at the short one.

**This is not a claim that a 200-day moving average has edge.** It is in-sample
over one path on one asset across an extraordinary period, the window was not
selected out of sample, and it has had no walk-forward or holdout test. It is
simply *the thing a buyer could trivially do instead*, and gate P7 exists to
force the comparison. The system fails P7 at every horizon.

### One benchmark number that is an artifact, and is reported as one

At the short horizon B5 returned **1.22e+09×** (121,712,327,426%). That is not
a result. Selectively compounding ~40,000 hourly returns while skipping unheld
hours explodes, and the rule is unimplementable at that turnover whatever the
fee model charges.

The benchmark now reports its round-trip count, which settles it numerically:

| B5 at | round trips | over | verdict |
|---|---:|---|---|
| daily bars | **35** | 8 years | credible, implementable by hand |
| hourly bars | **1,279** | 9 years | artifact — read as a warning, not a number |

Flagged rather than deleted, because deleting it would hide the failure mode.
The short-horizon P7 comparison against a Sharpe of 5.29 is therefore not
meaningful; the short horizon fails on its own numbers (PF 0.63, 0/5 folds,
10.1% bootstrap) without needing P7.

---

## 3. DefiLlama: **SKIP**

Not OPTIONAL. SKIP, for acceptance testing, on measured grounds.

All five free endpoints are reachable and returned HTTP 200, with adequate
history (daily, 2017→present). The data exists. The problem is that it cannot
be made point-in-time correct:

| finding | evidence |
|---|---|
| **The newest daily point is revised while its day is still running** | the point dated 2026-11-09 moved from 79,003,625,907 to 79,021,985,782 — 2.32 bps — between two reads 105 s apart. Ethereum's moved 4.62 bps. The date never changed, the point count never changed, nothing marked it revised. |
| **No as-of, published-at, revision or finality field on any endpoint** | the only time-related headers are `Date` and `Age`; `Age` is CDN cache age (236–263 s observed), which says nothing about the underlying data |
| **Publication lag: INSUFFICIENT EVIDENCE** | for all five endpoints |
| Schema drift | `date` is an int in `historicalChainTvl` and a **string** in `stablecoincharts/all`; the peg-key set grows from 1 to 30 keys along the series; `totalMintedUSD` appears in exactly one record |
| `excludeTotalDataChart=true` does not shrink the response | `totalDataChartBreakdown` still returns in full: 20.3 MiB |

The brief's own rule settles it: *"Treat backfilled series with unknown
publication or revision timing as exploratory, or collect them forward before
using them in an acceptance test."* A series whose newest point silently
changes within two minutes, with no revision marker, cannot enter a gate — and
a backtest reading it today and re-run tomorrow would give different answers
over the same date range for reasons invisible in the code.

The incremental-value test was **not run**, and deliberately so: running it on a
backfilled series with unknown revision timing would produce a number that
looks like evidence and is not.

**Revisit only if** DefiLlama is collected forward with snapshots stored at
observation time, for long enough to cover the holding period of whatever
strategy would use it. Until then it is market colour, not an input to a
decision. No paid tier is justified: the free endpoints are not the constraint.

---

## 4. Verified data and tool inventory

| source | URL | checked | access | coverage | resolution | verdict |
|---|---|---|---|---|---|---|
| Binance public archive | `data.binance.vision` | 2026-11-09 | free, no key | BTCUSDT/ETHUSDT 2017-08-17→ | 1h, 1d | **ADOPT** — 600 objects, all `.zip.CHECKSUM` verified, 0 mismatches |
| Binance REST | `api.binance.com` | 2026-11-09 | **HTTP 451 geo-blocked** | — | — | **BLOCKED** — `exchangeInfo` unobtainable, so tick/step/minNotional are INSUFFICIENT EVIDENCE |
| Kraken OHLC | `api.kraken.com/0/public/OHLC` | 2026-11-09 | free, no key | ~720 trailing bars | 1d | **ADOPT for cross-check only** — too short to validate on |
| Alpha Vantage crypto | via MCP | 2026-11-09 | free tier | BTC 2013-12-27→, ETH 2015-08-08→ | 1d | **REFERENCE ONLY** — aggregated composite, basis risk, not execution-grade |
| DefiLlama | `api.llama.fi`, `stablecoins.llama.fi`, `coins.llama.fi` | 2026-11-09 | free, no key | 2017→ | 1d | **SKIP** — §3 |

Cross-venue basis, Binance vs Kraken daily, 720 overlapping days:

| comparison | median | p95 | max |
|---|---:|---:|---:|
| as specified (USDT vs USD) | 4.27 bps | 14.68 bps | 24.66 bps |
| **USDT-adjusted** | **1.29 bps** | **6.06 bps** | **10.38 bps** |

About three quarters of the headline basis is the stablecoin leg, not the two
venues disagreeing about the price of BTC. Sizing a cross-venue allowance off
the headline figure prices in USDT redemption risk without naming it.

**Framework decision: keep the existing engines.** CCXT and Freqtrade were not
adopted. The measured gap is not in the backtester's mechanics — it is that the
regime matrix vetoes 68% of bars and the strategies have no demonstrated edge.
A new framework would not close that, and would discard the point-in-time
truncation, next-bar fills and risk engine that are already tested. Revisit if
and only if a strategy clears the gates and fill quality becomes the binding
question.

---

## 5. Paper-trading runbook

**No horizon currently qualifies to enter paper trading.** The runbook is
specified so that the entry criteria are fixed in advance rather than decided
when something looks promising.

### Entry criteria (all required, per horizon)

1. PASS on every binding gate P1–P12 over the walk-forward folds.
2. PASS on the untouched 365-bar holdout, evaluated **once**.
3. Sharpe above the best of B2/B4 — and above B5 at the same horizon.
4. Zero hard-risk violations.
5. Zero open data or parity defects.

### Minimum forward duration

| horizon | minimum duration | minimum independent trades | minimum distinct months |
|---|---|---:|---:|
| Short | 90 days | 150 | 3 |
| Medium | 180 days | 40 | 6 |
| Long | 365 days | 15 | 12 |

Paper days alone are never sufficient — a quiet quarter produces time and no
evidence. Both the trade count and the month count must be met.

### Configuration freeze

The configuration is **frozen** for the whole forward window: strategy list,
parameters, cost model, risk limits, universe. Any material change **ends the
evaluation and starts a new one** from zero. Non-material changes (logging,
reporting, a crash fix that cannot alter a decision) are recorded in the run
log with a rationale and do not reset the clock.

### Daily checks

- Equity, open positions, distance-to-stop per position
  (`tradermaximus/observability/dashboard.py`).
- Drawdown against every §8.1 limit, as a proportion of each.
- Feed freshness: a stale, gapped or inconsistent feed **blocks signal
  generation**. It does not degrade quietly.
- Decision/fill quality against backtest assumptions: realised slippage versus
  the 5 bps assumption, realised fee versus 10 bps, fill price versus the
  next-bar-open model.

### Weekly checks

- Trade count and win rate against the forward plan.
- Drift detection: Sharpe change > 30% or win-rate change > 20% over 30 trades
  auto-disables the strategy (`research/drift.py`, wired at
  `orchestrator.py` via `StrategyEngine(is_enabled=...)`).
- Funnel rerun: has the rejection profile moved? A change in *where* candidates
  die is a change in the system even when the P&L looks the same.

### Kill and rollback

- Any hard-risk violation → kill switch, investigate before resuming.
- Realised cost per round trip exceeding 1.5× the 34 bps assumption → stop; the
  cost model is wrong and every result derived from it is suspect.
- Drift auto-disable → the strategy stops. Reinstatement is a human decision
  (§21.2); the monitor never re-enables on its own.
- Rollback is `git revert` to the last tagged configuration plus a flat book.
  There is no partial rollback: a half-reverted configuration is a third,
  untested one.

### Go / no-go review

Separate from the operators and from this analysis, after the minimum duration,
against the same gates. Paper results do not lower the bar.

---

## 6. Plan

### Next 48 hours

| # | action | exit criterion |
|---|---|---|
| 1 | Investigate why every fold reports regime `UNKNOWN` (P3 failed 1/3 on all horizons). The regime engine needs benchmark history the fold slices may not give it. | regimes correctly classified inside folds, or the cause documented |
| 2 | Derive a **crypto** regime eligibility matrix from economic reasoning, predeclare it, freeze it | written and committed before any run |
| 3 | Re-run all three horizons against the new matrix, once | scorecard published against both matrices |
| 4 | Obtain `tickSize`/`stepSize`/`minNotional` by a route that is not geo-blocked | lot/tick rounding and minimum notional validated |

**Next actionable command:**
`python tools/crypto_funnel.py --by-regime --strategy STRATEGY_012`

### 2–6 weeks

- Resolve whether a trend strategy can clear the gates once the regime matrix
  is correct. This is the single open question: the funnel shows trend-following
  is vetoed on 67.66% of bars, so it has never actually been measured.
- If it clears, run the holdout **once**, then enter paper per §5.
- If it does not clear, shelve the medium and long horizons and say so.
- Do **not** revisit the short horizon. It has a real sample and a demonstrated
  negative edge across five independent folds.

### 3–12 months

- Only if a strategy passes paper per §5: consider Phase 2 (derivatives).
- Otherwise: the honest conclusion is that this system executes and manages
  risk well and has no demonstrated edge, and that a 200-day moving average on
  BTC — 35 round trips in eight years — is a better use of the same capital
  than anything it has produced.

### Horizon verdicts

| horizon | verdict | what would change it |
|---|---|---|
| **Short (15m–1h)** | **SHELVE** | Nothing short of a different strategy. 89 trades, 5/5 folds negative, 90% of bootstraps losing. Re-measuring the same rules on the same bars is not a path. |
| **Medium (4h–1d)** | **RESEARCH** | A correct crypto regime matrix. Trend-following is vetoed on 67.66% of bars, so its edge is unmeasured rather than absent. |
| **Long (daily–weekly)** | **RESEARCH** | Same, plus a longer sample. Even unvetoed, this horizon may never reach the 25-trade / 5-year floor on 9 years of data. |

---

## 7. Phase 2 (derivatives) is NOT opened

The brief: *"Do not begin this phase unless at least one Phase 1 strategy
clears all acceptance gates with sufficient effective sample. If Phase 1
produces no accepted strategy, stop and report that result; do not open Phase 2
to create the appearance of progress."*

**No Phase 1 strategy cleared any gate at any horizon.** Phase 2 is not opened.
Funding-rate carry is not evaluated, is not configured, and is not a substitute
for a directional edge it would be papering over.
