# Predeclared validation protocol — crypto

**This file is committed BEFORE any validation run.** That ordering is the
point: a gate chosen after seeing the result is not a gate. Git history is the
evidence that these numbers preceded the measurement.

Commit this file, then run. If a result later argues for different gates, the
gates change *for the next untouched window*, never retroactively for this one.

---

## 1. Frozen inputs

| item | value |
|---|---|
| Asset class | crypto only |
| Instruments | BTCUSDT, ETHUSDT (Binance spot) |
| Price source | `data/venue/binance/` — Binance's own public archive, all 600 objects verified against published `.zip.CHECKSUM`, 0 mismatches |
| Reference source (NOT used for fills) | `data/real/crypto/` — Alpha Vantage composite, retained for cross-checking only |
| Daily bars | 3,371 per symbol, 2017-08-17 → 2026-11-08, 0 gaps |
| Hourly bars | 80,773 per symbol, 29 gaps / 126 missing bars |
| Calendar | UTC, 24/7, no session boundaries |
| Provenance | `data/venue/MANIFEST.json` (per-file sha256), `docs/CRYPTO_VENUE_DATA.md` |
| Code | commit recorded in the run output |

### Known data defects accepted into this run

Full list in `docs/CRYPTO_VENUE_DATA.md`. The ones that bear on validity:

- **Timestamp units switch ms → microseconds at 2025-01-01.** Handled during
  ingestion; both units appear in the source archive.
- **43 off-grid hourly bars** (Feb 2018) offset 28m14.789s from the hour grid.
- **15 hourly bars per symbol have a duration ≠ 1 h**, one of them *negative*.
  Shipped CSVs carry open time only, so this is invisible downstream — it means
  `closeTime` is not trustworthy on this feed.
- **`exchangeInfo` unobtainable (HTTP 451 geo-block).** `tickSize`, `stepSize`
  and `minNotional` are **INSUFFICIENT EVIDENCE**. Consequence: lot and tick
  rounding and minimum-notional feasibility are **not validated**. Any result
  here is therefore provisional on instrument filters nobody has seen.

---

## 2. Cost model — explicit, conservative, and flagged

| component | value | basis |
|---|---|---|
| Fee per side | **10.0 bps** | Binance spot VIP-0 taker, 0.10%. **ASSUMPTION** — the fee tier could not be verified because `exchangeInfo` is geo-blocked. |
| Quoted spread | **2.0 bps** | BTC/ETH spot top-of-book is typically ~1 bp; 2 is deliberately pessimistic |
| Random slippage | **5.0 bps** (σ) | conservative for retail size on a liquid pair |
| Market impact | `120 bps × √participation` | existing square-root model, unchanged |
| **Round-trip floor** | **≈ 34 bps** | 2 × (10 fee + 2 spread + 5 slippage) |

**The previous default was `commission_bps = 1.0`** — 1 bp per side, which is
the right order of magnitude for equities and **10× too low for crypto spot**.
Every earlier crypto backtest in this repository understated transaction costs
by roughly 18 bps per round trip. For a strategy turning over 30 times a year
that is ~5.4% of annual return that was never charged.

**Break-even requirement, stated up front:** a strategy must clear ~34 bps per
round trip before it earns anything. A 40-bps average gross edge is a
6-bps net edge, and that is inside the slippage assumption's own error bar.

---

## 3. Fold geometry

Chronological only. Train windows are warm-up, never scored; test windows do
not overlap; folds are anchored to the end of history.

| horizon | signal bars | train | test | folds | embargo |
|---|---|---|---|---|---|
| Short | 1h | 8,760 (1 yr) | 2,190 (3 mo) | 5 | 72 bars (3 days, > max hold) |
| Medium | 1d | 730 (2 yr) | 365 (1 yr) | 5 | 42 bars (> 6-week max hold) |
| Long | 1d | 1,095 (3 yr) | 365 (1 yr) | 5 | 120 bars (> max hold) |

Medium needs 730 + 5×365 = 2,555 ≤ 3,371 ✓. Long needs 1,095 + 1,825 = 2,920 ≤ 3,371 ✓.

**Final untouched holdout:** the most recent **365 daily bars**
(2025-11-09 → 2026-11-08) are reserved and evaluated **exactly once**, after
selection is final. Walk-forward folds are drawn from the data *before* that
holdout. If the holdout fails, that is the verdict — it is not re-run with
different settings.

---

## 4. Benchmarks

Beating zero is not an edge. From the brief: of 100 RSI configurations audited,
78 were profitable and only 2 beat the base rate, mean non-beating edge −4.44pp.

| # | benchmark | why |
|---|---|---|
| B1 | Cash, 0% USD | the floor |
| B2 | Buy-and-hold BTC | the thing a buyer could do for free |
| B3 | Buy-and-hold ETH | same |
| B4 | 50/50 BTC/ETH, monthly rebalance | diversified passive |
| B5 | BTC 200-day MA trend-to-cash | horizon-matched trivial rule |

Reported alongside every strategy: total return, Sharpe, max drawdown, **and
time invested** (a strategy in cash 80% of the time is not comparable to
buy-and-hold on return alone).

---

## 5. Gates

### 5a. Existing gates, as already coded (reported unchanged for continuity)

| # | gate |
|---|---|
| E1 | OOS profit factor > 1.3 **and** OOS Sharpe > 0.7 |
| E2 | 5 walk-forward folds, **each** with PF > 1.3 |
| E3 | Monte Carlo ≥ 90% of 5,000 resamples profitable |
| E4 | Max drawdown < 5% |
| E5 | OOS Sharpe ≥ 80% of in-sample Sharpe |

**E4 is wrong for crypto and is reported but not binding.** A 5% drawdown cap
on an asset whose daily σ is ~3% means the strategy must be in cash almost
always. It was derived for equities. Replaced by P4 below.

### 5b. Proposed horizon-specific gates (binding)

| # | gate | short | medium | long |
|---|---|---|---|---|
| P1 | minimum OOS trades | 200 | 60 | 25 |
| P2 | minimum distinct calendar years in OOS | 3 | 4 | 5 |
| P3 | minimum distinct primary regimes traded | 3 | 3 | 2 |
| P4 | max drawdown | < 20% | < 25% | < 30% |
| P5 | net OOS profit factor after the §2 cost model | > 1.25 | > 1.25 | > 1.25 |
| P6 | net OOS Sharpe (annualised 365) | > 0.60 | > 0.60 | > 0.50 |
| P7 | Sharpe must exceed the **best** of B2/B4 | yes | yes | yes |
| P8 | Monte Carlo (block bootstrap) ≥ 90% profitable | yes | yes | yes |
| P9 | no single calendar year contributes > 50% of net P&L | yes | yes | yes |
| P10 | no single trade contributes > 25% of net P&L | yes | yes | yes |
| P11 | zero hard-risk violations | yes | yes | yes |
| P12 | survives fees at 1.5× the §2 assumption | yes | yes | yes |

Rationale for the numbers, since they must be justified rather than asserted:

- **P1/P2/P3** — the previous crypto "result" was 5 trades, one of them a
  winner. These floors make that arithmetically impossible to report as
  evidence. Years and regimes matter more than raw count: 200 trades inside
  one bull run is one observation of one regime.
- **P4** — set from each horizon's own holding period and crypto's realised
  volatility (BTC ~50%, ETH ~79% annualised), not from the equity 5%.
- **P5/P6** — below the existing 1.3/0.7 because those were never met by
  anything, and a gate nothing can clear tells you nothing. 1.25 and 0.60 are
  still above the level at which the cost assumption's error bar dominates.
- **P7** — the CoinPaprika lesson, made mechanical.
- **P9/P10** — concentration, which is how 5 trades produced PF 1.75.
- **P12** — the fee rate is an unverified assumption; the result must not
  depend on it being exactly right.

### 5c. Block bootstrap

Returns are serially dependent, so i.i.d. resampling overstates confidence.
Block length = **20 bars** for daily, **168 bars (1 week)** for hourly, with
sensitivity reported at 0.5× and 2× those lengths. 5,000 resamples.

Trade-order shuffling is **not** used for the profitability test: permuting a
fixed set of trades cannot change their sum, so "% profitable" from a shuffle
is 0% or 100% and carries no information. Shuffling is reported for path/
drawdown risk only.

---

## 6. Verdict vocabulary

- **PASS** — every binding gate met, artifact cited.
- **FAIL** — a binding gate missed. Recorded, not retried with new parameters.
- **INSUFFICIENT EVIDENCE** — the gates could not be *evaluated* (too few
  trades, too little history, a data defect in the way). Not a soft FAIL; a
  distinct and expected outcome.

A strategy that misses a gate narrowly is **FAIL**. It is not re-tuned.
