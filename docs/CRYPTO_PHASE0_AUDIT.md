# Phase 0 — crypto baseline audit

Commit audited: `9a42e3c` (HEAD at time of audit). Date: 2026-11-09.

Every claim below is tagged **EXECUTED** (performed in this session, artifact
named) or **PROPOSED** (not performed). Nothing here is reported as a result
without an artifact behind it.

---

## 0.0 BLOCKING FINDING — the prior evidence base no longer exists

**EXECUTED.** The container was replaced between sessions. `data/real/**/*.csv`
is git-ignored (`.gitignore:25`), so every raw input to every previously
reported number is gone, along with the scratchpad holding the run logs and
trade ledgers.

What survives: three committed summary reports under `validation/`. Those are
*summaries*, not ledgers — no per-trade rows, no dataset hash, no command line.

Consequence, stated plainly: **no previously reported crypto number can be
reproduced.** They are not wrong; they are unverifiable. Any of them cited as
evidence from here on is cited as hearsay.

Remediation applied: a coverage manifest with SHA-256 per file now ships at
`data/real/crypto/MANIFEST.json`, and it is *not* git-ignored, so provenance
survives the next container replacement even though the bars do not.

---

## 0.1 "Crypto OOS profit factor 1.75, Sharpe 0.20, five trades"

**Verdict: INSUFFICIENT EVIDENCE.** Inputs do not exist (§0.0), so this was not
rerun. No dataset hash, dates, command, config or trade ledger can be produced
for it. Per instruction, nothing is inferred about its correctness.

One thing *can* be read off the surviving artifact
(`validation/crypto_report.md`), and it matters more than the figure itself:

| field | value |
|---|---|
| OOS trades | 5 |
| OOS win rate | 20.0% |
| OOS profit factor | 1.75 |

20% of 5 trades is **one winner**. A profit factor of 1.75 from one winning
trade is not a weak edge — it is a single observation. The same artifact shows
the in-sample segment at **1 trade**, and the whole crypto run at **6 signals
across 3 strategies over ~7 years**.

The reported figure should never have been presented as a performance
measurement, independent of whether the backtester was correct.

---

## 0.2 Does the scoring deficit affect crypto? — YES, and it is only partly fixed

**EXECUTED**, analytically, from `tradermaximus/strategy/scoring.py`. This needs
no market data: it is arithmetic over the component table.

Crypto takes the non-fundamentals branch (`scoring.py:94`), so its 8 components
are rescaled by `100/90 = 1.1111`.

| item | raw pts | crypto-scaled pts |
|---|---|---|
| `risk_reward` — returned **0.0**, not neutral, without a ratio | 5.00 | 5.56 |
| `relative_strength` — rating sub-component (50% of 10) | 5.00 | 5.56 |
| **Closed by `derived_scoring_inputs` (commit `e7e670b`)** | **10.00** | **11.11** |
| `relative_strength` — `sector_strength` sub-component (25% of 10) | 2.50 | 2.78 |
| **Original total deficit** | **12.50** | **13.89** |

So the defect was **identical in kind on crypto**, the fix closed **11.11 of
13.89 points**, and **2.78 points remain unrecovered** because
`sector_strength` is never supplied on real data.

Two corrections to the record, both against my own earlier statements:

1. I previously described the deficit as "about 14 points." That is the *total*
   (13.89); the fix closed 11.11. I did not previously separate the two.
2. **`sector_strength` is meaningless for crypto.** There is no sector. Leaving
   a 2.78-point hole that can never be filled makes every crypto score
   permanently capped at 97.2, which silently stiffens every threshold for
   crypto relative to equities. This is a live, unfixed defect.

**PROPOSED fix:** drop `sector_strength` from the crypto denominator rather than
scoring it zero — i.e. rescale over the components that can actually be
measured for the asset class. Golden fixtures must pin the denominator per
asset class so this cannot drift again.

---

## 0.3 Attribution of "42.7% profitable resamples" and the failed gates

**EXECUTED.** Artifact: `validation/forex_report.md:51`.

- **42.7% is FOREX, not crypto.** Crypto's Monte Carlo gate reads *"too few
  trades to resample (fewer than 10)"* (`validation/crypto_report.md:48`).
  Equities reads the same. Only FOREX had ≥10 trades to resample at all.
- The per-class gate table has **5 gates**; crypto failed **4 of 5** (max
  drawdown passed at 0.66%). The "5 of 8 failed" figure I reported earlier
  comes from a *different*, cross-asset 8-gate table in
  `docs/PHASE_REPORT.md`. Both are correct about different tables; conflating
  them was my error.

Crypto's four failures, each with its artifact line:

| gate | detail | artifact |
|---|---|---|
| OOS PF > 1.3 **and** Sharpe > 0.7 | PF 1.75 ✓ but Sharpe 0.20 ✗, 5 trades | crypto_report.md:44 |
| 5 walk-forward folds, each PF > 1.3 | only **3 of 5 folds ran** | crypto_report.md:46 |
| Monte Carlo ≥ 90% profitable | **not computable**, <10 trades | crypto_report.md:48 |
| OOS Sharpe ≥ 80% of IS | 0.20 is 41% of 0.49 | crypto_report.md:50 |

Note the second row: the folds did not *fail*, they **did not exist**. The
dataset (2,500 bars) could not hold 5 × (1,095 train + 365 test). That is a
data-coverage failure being reported as a strategy failure.

---

## 0.4 Repository inspection

**EXECUTED.** Full read access; findings below are VERIFIED unless marked.

---

## 0.5 Specification defects

### (a) Scope and readiness

**EXECUTED.**

| finding | severity | evidence |
|---|---|---|
| `polygon-mcp` is `required=True` in the MCP catalogue — an **equities** vendor on the critical path for a crypto-only system | HIGH | `mcp/registry.py:115` |
| Readiness reports `required_mcp_tools_available: true` with **0 handlers registered** | INFO — **not a defect** | `orchestrator.py:184`; an empty registry is the documented signal that MCP is not the data path, and paper/backtest run via `ProviderChain`. Correct as written. |
| CATALOGUED vs CONNECTED vs RUNTIME-VALIDATED are not distinguished anywhere | MEDIUM | `cli.py cmd_tools` reports 26 catalogued / 0 registered and nothing about runtime validation |
| Equity logic reachable from the crypto path | **LOW** | Only 3 strategies are crypto-eligible: `STRATEGY_002` (pullback), `STRATEGY_004` (reversal), `STRATEGY_009` (institutional accumulation). No CAN SLIM, no earnings, no sector rotation — 005–008 are equities-only and 003 (gap) was already disabled for crypto. The crypto path is cleaner than the spec implies. |
| All three crypto strategies are **long-only** and were designed on an equity mental model ("institutional accumulation") | MEDIUM | `strategy/library.py` |

### (b) Score and regimes

**EXECUTED.** The 10 equity-only fundamentals points *are* handled — crypto
rescales 100/90 (`scoring.py:99-103`). The live defect is the residual 2.78-pt
`sector_strength` hole documented in §0.2, not the fundamentals block.

### (c) Risk and stop rules — all six contradictions CONFIRMED in code

**EXECUTED.** Values read directly from `tradermaximus/risk/limits.py`.

| # | spec contradiction | as coded | effective | verdict |
|---|---|---|---|---|
| 1 | 0.5% vs 0.75% crypto risk | `HardLimits.max_risk_per_trade=0.005`; crypto band top `0.0075` | **0.5%** — `sizing.py:140` takes `min()` | CONFIRMED. The 0.75% tier is **unreachable dead config**. |
| 2 | 40% asset-class vs 50% portfolio | `crypto.max_exposure_total=0.40`; `HardLimits.max_portfolio_exposure=0.50` | **50%** | CONFIRMED, and worse than stated: `max_exposure_total` is **defined and never read anywhere** in the codebase. The asset-class cap does not exist at runtime. |
| 3 | 0.0 vs 1.0 leverage | `AssetLimits.max_leverage=0.0` (crypto); `HardLimits.max_leverage=1.0` | no leverage | CONFIRMED — a **semantic collision**: 0.0 means "none permitted", 1.0 means "1× permitted". `engine.py:407` computes `max(0.0, max_leverage - 1.0)`, which yields 0.0 for *both* by coincidence. `AssetLimits.max_leverage` is also never read. Latent: any future edit to either constant changes meaning unpredictably. |
| 4 | 2% vs 10% ADV | `crypto.max_position_volume_pct=0.02`; `HardLimits.max_order_size=0.10` | **2%** | CONFIRMED but **both are enforced** as separate caps in `sizing.py:168-171`; `min()` binds at 2%. Not a conflict in effect — a naming collision between two distinct concepts. |
| 5 | $100k vs $1m liquidity | `crypto.min_liquidity=100_000`; `forex.min_liquidity=1_000_000` | $100k for crypto | **NOT a contradiction** — different asset classes, by design. |
| 6 | 30 vs 50 bps slippage | `max_slippage_bps=30`; `max_spread_bps=50` | both | **NOT a contradiction** — slippage and spread are different quantities. Spec wording conflates them. |

**Honest correction to the brief:** four of six are real (two of them *worse*
than described, because the limit is dead code rather than merely conflicting);
two are spec-wording artifacts, not code defects.

Stop-clamp vs structural rejection (`risk/stops.py`): **already correct.** Wide
ATR is clamped; a structure distance beyond the maximum **rejects** the trade
with `StopRejection.STRUCTURE_TOO_WIDE`. Verified by test.

### (d) Measurement

**PROPOSED.** A stage-by-stage funnel by pair, horizon, strategy and regime does
not exist. `tools/gate_autopsy.py` is the closest thing and is per-strategy,
synthetic-or-real, with no horizon or regime dimension and no cross-check
against an independent engine.

---

## 0.6 Data and venue reachability

**EXECUTED.** Measured this session; all are proxy `connect_rejected` (403 at
CONNECT), confirmed against `$HTTPS_PROXY/__agentproxy/status`.

| host | purpose | result |
|---|---|---|
| `data.binance.vision` | Binance Public Data archive | **REFUSED** |
| `api.binance.com` | Binance REST (exchangeInfo, fees) | **REFUSED** |
| `api.kraken.com` | Kraken REST + OHLC | **REFUSED** |
| `api.llama.fi` | DefiLlama TVL / DEX volumes | **REFUSED** |
| `coins.llama.fi` | DefiLlama prices | **REFUSED** |
| `stablecoins.llama.fi` | DefiLlama stablecoin circulation | **REFUSED** |
| `github.com`, `raw.githubusercontent.com` | repos, docs | reachable |

A personal environment **`tradermaximus-crypto`**
(`env_01GGTpeTiQDouhKF9qSCXHSP`) now exists allowing exactly those six hosts.
**This session could not be moved onto it** — the runtime refuses to relocate a
running session — so venue data and DefiLlama remain unreachable *here*. Work
needing them must start in a new session on that environment.

### Data re-acquired (reference feed only)

**EXECUTED.** Alpha Vantage `DIGITAL_CURRENCY_DAILY`, market=USD.

| symbol | rows | span | dupes | zero-vol | quality |
|---|---|---|---|---|---|
| BTC-USD | 5,960 | 2010-07-17 → 2026-11-09 | 0 | 1,259 | 0.80 — `zero_volume_run` |
| ETH-USD | 4,112 | 2015-08-08 → 2026-11-09 | 0 | 0 | 1.00 |

Hashes and full manifest: `data/real/crypto/MANIFEST.json`.

Two things to be explicit about:

1. **This is an aggregated cross-venue composite, not execution-venue data.**
   Fill assumptions derived from it are not venue-accurate. The manifest records
   this as `basis_risk`. Per the brief's own rule, fine-grained execution
   conclusions from this feed are **INSUFFICIENT EVIDENCE**.
2. BTC's first 1,259 bars (2010-07-17 → 2013-12-26) are flat at $0.05 with zero
   volume. The quality engine caught this unprompted and scored the series 0.80.
   Usable BTC history begins **2013-12-27** (4,701 bars) — materially more than
   the 2,500 bars the previous run had, which is why only 3 of 5 folds fitted
   before. Five folds now fit.

---

## What this audit does NOT establish

- Any crypto strategy has an edge. Nothing here measures that.
- That the re-acquired data is suitable for execution modelling. It is not.
- That the previously reported crypto numbers were wrong. They are
  **unverifiable**, which is a different and weaker claim.
