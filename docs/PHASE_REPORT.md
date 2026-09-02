# Execution report — Phases 1 to 7

What was asked for, what was built, what the data said, and which deployment
gates that leaves open. The governing instruction for this round was:

> Do NOT optimize to hit specific numbers. The goal is robust edge — not
> overfitting. If the system fails the validation gates, disable the
> underperforming strategies rather than tuning them.

So where a strategy could not clear a bar, the response below is either a
correctness fix (something was measuring the wrong thing) or a disablement
(the strategy cannot work on that asset class) — never a threshold moved to
let a result through.

---

## Phase 1 — silent failure modes

Both items were already implemented and pushed in the previous round; this
round verified them rather than repeating them.

| item | state | evidence |
|---|---|---|
| Unify the scoring threshold across strategy and risk engines | done | `effective_minimum_score` is called from `research/backtest.py:271` and `orchestrator.py:531`; the regime keeps its veto by returning `None` |
| Regime retains veto power | done | `TestThresholdAgreement.test_a_regime_that_permits_nothing_still_vetoes` |
| Fix short equity accounting | done | `PositionBook.equity` sums `notional * side.sign` and nothing else |
| `TestShortEquityAccounting` regression test | done | 4 tests in `tests/test_execution.py` |

---

## Phase 4 — hardened walk-forward and Monte Carlo

**5-fold rolling walk-forward.** `plan_walk_forward_folds` lays out folds of
"three years train, one year test" in each class's own bar interval, anchored
to the *end* of history so that when there is not room for every fold the
earliest are dropped rather than the latest — a deployment decision is about
recent regimes. Test windows abut without overlapping, so no bar is scored
twice. The train window is handed to the backtester as its warm-up, which is
what makes the test window genuinely out of sample.

One honest caveat: this system fits no parameters. Walk-forward here measures
whether the rules hold up across time periods, not whether fitted parameters
were overfitted. The overfitting risk in a rule-based system lives in the
human choice of thresholds, and no walk-forward can see that — only a
threshold set before the data was examined can.

**Monte Carlo at 5,000 simulations, 90% bar.** Bootstrap resampling, which
varies both composition and order. (Shuffling cannot answer this question: the
final equity is identical under every permutation because addition commutes,
so "% profitable" from a shuffle is always 0% or 100%.)

**Out-of-sample robustness.** A strategy whose OOS Sharpe retains less than
80% of its in-sample Sharpe fails a gate of its own, and the response is
disablement rather than tuning: tuning against the same out-of-sample window
is how that window stops being out of sample.

---

## Phase 5 — risk enhancements

**Volatility targeting.** `PositionSizingEngine(volatility_target=0.15)` sizes
every instrument toward one annualised volatility instead of its own class's
baseline. `max_volatility_scale` defaults to **1.0**, so targeting can only
shrink a position. That default is deliberate and worth stating plainly:
EURUSD runs near 4% annualised, so an uncapped 15% target would ask for 3.75x
the position and would breach section 18.3's "only ever scales down". The cap
is what keeps a volatility *target* from becoming a leverage instruction.

Note also that per-trade risk is already volatility-normalised by
construction: sizing divides the risk budget by the stop distance, and the stop
is ATR-derived, so a hotter market already buys a smaller position. Volatility
targeting therefore acts on *notional exposure*, not on risk-per-trade.

**ATR-adaptive stops.** Multipliers widened from 1.2/1.5/1.8 to
**1.5/1.75/2.0**. A wider stop is not more risk while the risk budget is the
binding cap — the sizer takes a proportionally smaller position and the money
at risk is unchanged. There is one exception, and it is pinned by a test
rather than hidden: when the *exposure* cap binds instead, quantity is fixed by
notional and stops shrinking as the stop widens, so risk does rise. It stays
under the section 8.1 per-trade cap because that is checked independently.

**Scale-out ladder.** `SCALE_OUT` implements 50% at +5%, 30% at +10%, trailing
the last 20% at 2x ATR, exactly as specified. Measured against real data, that
ladder is not asset-class-neutral: EURUSD's realised volatility is about 4%
annualised, so a +5% rung is roughly a year of travel and is unreachable inside
a five-day holding limit, while on crypto the same rung is a routine day. So
`TakeProfitEngine.atr_scale_out` provides the same 50/30/20 shape denominated
in ATR multiples (1.5 ATR / 3.0 ATR), which puts the first rung at the same
*opportunity* distance on EURUSD as on SOL-USD.

---

## Phase 3 — strategy selection

**Regime-specific scoring weights — built, measured, and switched off.**
`FAVOURED_FAMILY_WEIGHT` gives the favoured family a 1.5x weight on the
`market_regime_alignment` component: TREND_UP favours momentum/breakout/CAN
SLIM/pullback, RANGE favours mean reversion, HIGH_VOLATILITY favours reversal.
`REGIME_FAMILY_WEIGHT` — the table actually in force — is **empty**.

It was applied and measured first. On the only asset class with enough trades
to measure anything, nine years of real EURUSD and GBPUSD:

| | trades | IS PF | OOS PF |
|---|---|---|---|
| without the weighting | 48 | **1.47** | 1.00 |
| with the weighting | 72 | **1.04** | 1.05 |

The 24 additional trades were, in aggregate, losers.

The reason is worth stating plainly, because the design note first written for
this feature was wrong. Clamping the weighted value to the component's own
maximum does keep the score inside 0-100, and it was argued from that that "the
bar a signal must clear stays the bar it was set at". That is true and beside
the point: the *score* rises, so setups that previously fell short of their
family bar now clear it. Where the component is not already at its maximum —
the normal case — a 1.5x weight is a threshold loosening wearing the clothes of
a preference. Mean reversion already trades on a lowered bar of 50; a score
bonus on top double-counts the same preference.

The mechanism is implemented, tested and one assignment away from being
enabled. It is off because the data said so.

**Correlation-based sizing.** `correlation_size_multiplier` measures 20-day
rolling Pearson correlation between a candidate and every open position, and
cuts size by 30% above 0.7. Two positions correlated at 0.9 are not two bets;
they are one bet at double size, and the per-trade risk cap cannot see that
because it looks at each trade alone. An unmeasurable correlation is treated as
neither independence nor grounds for a cut.

**Concurrent positions.** The brief asks for 2 per asset class in Phase 3 and
for 3 (crypto) / 10 (equities) in Phase 6. These conflict; the Phase 6 numbers
are per-class and more specific, so those are what is implemented — see below.

---

## Phase 6 — per-asset-class configuration

| setting | asked | implemented | note |
|---|---|---|---|
| Crypto risk/trade | 0.25-0.75% | 0.75 / 0.50 / 0.25% by volatility band | as asked |
| Crypto max exposure/position | 5% | 5% | as asked |
| Crypto max positions | 3 | 3 | new `AssetLimits.max_positions` |
| Equities risk/trade | 0.5-1.5% | bands 1.5 / 1.0 / 0.5% | **capped at 0.5%** — see below |
| Equities max exposure/position | 10% | 10% | as asked |
| Equities max positions | 10 | 10 | new field |
| Portfolio max total exposure | 50% | 50% | already in section 8.1 |
| Portfolio max correlated exposure | 20% | 20% | already in section 8.1 |

**The equities risk band does not do what it says.** Section 8.1 caps
`max_risk_per_trade` at 0.5%, and `PositionSizingEngine` takes the *minimum* of
the asset band and that cap. So the equities 1.5% and 1.0% bands are
unreachable and effective equities risk is a flat 0.5% however quiet the
market. The hard cap was not raised to meet the band — that limit is the one
thing this system is built not to move — and the band was not lowered to hide
the gap. It is recorded here and pinned by
`test_the_hard_cap_still_overrides_every_asset_band`.

FOREX is capped at 4 concurrent positions: major pairs share the dollar leg, so
they are near-duplicates of one another far more often than equities are.

---

## Phase 7 — drift detection, reports, monitoring

**Drift detection** already existed with exactly the requested thresholds
(Sharpe change 30%, win rate change 20%, 30-trade window) but was **not wired
into the live path** — `StrategyMonitor` could disable a strategy and the
orchestrator would go on trading it. It is now wired: `StrategyEngine` takes an
`is_enabled` callback and a disabled strategy is skipped entirely rather than
sized down. A smaller position in a strategy whose edge has gone is still a
position in a strategy whose edge has gone.

**Reports.** `tools/validate_real_data.py --report-dir validation` writes one
markdown report per asset class: data provenance and quality, both segments,
every walk-forward fold, where signals were lost, per-strategy performance, and
each deployment gate with its verdict and the number behind it.

**Dashboard.** `tradermaximus/observability/dashboard.py` renders open
positions with distance-to-stop, the risk budget as a proportion of each
section 8.1 limit, daily P&L, and per-strategy health with disabled strategies
called out. It reads state and never changes it: a monitoring surface that can
also act is a second, untested trading path.

---

## Phase 2 — validation across all three asset classes

Data is real market history, fetched via the Alpha Vantage MCP server and
converted byte-for-byte by `tools/mcp_to_csv.py`. All eight symbols pass data
quality at 1.00.

| class | symbols | bars | interval | span |
|---|---|---|---|---|
| equities | SPY, AAPL, MSFT, NVDA, TSLA | 844–1,400 each | weekly | 1999-11 → 2026-09 |
| crypto | BTC-USD, ETH-USD, SOL-USD | 2,338–2,500 each | daily | 2019-10 → 2026-09 |
| forex | EURUSD, GBPUSD | 2,500 each | daily | 2017-01 → 2026-08 |

Two deviations from the brief, both forced and both stated rather than worked
around:

**Equities are weekly, not daily.** Alpha Vantage gates daily equity history
behind a premium plan (`TIME_SERIES_DAILY_ADJUSTED` and `outputsize=full` both
return a premium-endpoint error), and the Finnhub candle endpoint returns 403
on this key. Weekly is the longest real history obtainable. This matters more
than it sounds — see the equities finding below.

**Crypto uses real exchange history, not Binance testnet.** Testnet is an
order-execution sandbox with a synthetic order book; it has no genuine price
history, so a backtest against it would measure nothing about edge. Real
historical prices are the only thing that can answer "is there an edge here",
and no live order is placed either way — the paper broker fills every order in
this system.

### Results

| class | segment | trades | win rate | PF | Sharpe | max DD |
|---|---|---|---|---|---|---|
| equities | in-sample | 3 | 100.0% | inf | 0.39 | 0.2% |
| equities | **out-of-sample** | **2** | 50.0% | 2.48 | 0.16 | 0.1% |
| crypto | in-sample | 1 | 100.0% | inf | 0.49 | 0.0% |
| crypto | **out-of-sample** | **7** | 28.6% | 1.99 | 0.24 | 0.7% |
| forex | in-sample | 48 | 37.5% | 1.04 | 0.04 | 0.6% |
| forex | **out-of-sample** | **24** | 37.5% | 1.05 | 0.04 | 0.4% |

Equities and crypto produce too few trades for any of these numbers to mean
anything. A profit factor of 2.48 computed from two trades is not evidence of
an edge; it is one winner and one loser.

---

## Why equities and crypto barely trade

### Equities: structural stops do not fit weekly bars

15 signals over the full span. **10 of the 15 were rejected on stop
placement** — the V4 rule that a trade is refused, rather than its stop
tightened, when structural support sits beyond the maximum stop distance.

Measured directly, structural support distance against each class's own cap:

| class | bar interval | max stop | median distance | p75 | p90 | beyond cap |
|---|---|---|---|---|---|---|
| equities | **weekly** | 7% | **7.20%** | 14.60% | 25.12% | **51%** |
| crypto | daily | 5% | 3.72% | 9.10% | 18.42% | 41% |
| forex | daily | 5% | 1.11% | 1.72% | 2.32% | **0%** |

The median structural stop on weekly equity bars is *already past the 7% cap*.
This is not a defect in the rule — refusing a trade whose stop cannot be placed
where structure demands is exactly right, and tightening it into the noise
would be worse. It is a mismatch between a limit calibrated for daily bars and
data that is only available weekly. A week's range is roughly √5 times a day's,
and structure sits proportionally further away.

**The fix is daily equity data, not a higher stop limit.** Raising the 7% cap
to accommodate weekly bars would raise real risk on daily bars too, where the
limit is doing its job. Until a daily equity feed is available, the equity
strategies cannot be validated, and this is a data-access problem rather than a
strategy problem.

### Crypto: the gap strategy cannot work on a 24/7 market

STRATEGY_003 requires a 3% overnight gap. Measured on 2,499 daily BTC-USD bars:
median open-to-previous-close gap **0.04%**, 99th percentile 0.61%, maximum
1.59%, and **zero** bars at or above 3%. The gate autopsy agrees — 2,466 of
2,467 eligible bars died on `gap_up_3%`.

A gap is a price change across a session boundary and a 24/7 market has none.
No threshold fixes this: a gap threshold lowered until crypto trades is no
longer measuring a gap, it is measuring ordinary overnight drift under a gap's
name. **STRATEGY_003 is now equities-only** — disabled, not retuned, per the
governing instruction.

That leaves crypto with STRATEGY_002 (pullback), STRATEGY_004 (reversal) and
STRATEGY_009 (institutional). Only STRATEGY_004 produced trades.

### One defect found and fixed along the way

`SOL-USD` was rejected outright at data quality 0.55, flagged as an unadjusted
stock split on its -48.5% bar of 2022-11-08. That bar is the FTX collapse.
Crypto has no corporate actions to be unadjusted for, so the split heuristic is
now restricted to equities. SOL-USD loads at quality 1.00 and is included above.
This is the fourth defect of this family the real-data work has surfaced, after
the AAPL crash, the BTC COVID crash and the OHLC rounding.

---

## The most important finding: the backtest was validating a different system

Two of the nine scoring components read their values from `ScoringInputs`
rather than from the feature set. The backtester supplied only
`average_daily_volume`, so:

* `risk_reward` returned **0.0** — not a neutral value, zero — for want of a
  ratio it could have computed from the strategy's own stop and target ladder;
* `relative_strength` lost three-quarters of its points for want of a
  benchmark comparison and a sector reading.

Together that is roughly **14 points of the 100-point scale, deducted from
every signal in the backtest and from no signal in live trading**, where a
broker quote supplies the spread and the target ladder supplies the ratio.

The backtest was therefore validating a *stricter* system than the one that
would trade. `derived_scoring_inputs` now computes both from data the
backtester already holds. The result, ablated on nine years of real EURUSD and
GBPUSD:

| configuration | IS trades | IS PF | OOS trades | OOS PF |
|---|---|---|---|---|
| previous round (both off) | 28 | **1.47** | 20 | 1.00 |
| new ATR stops only | 28 | 1.43 | 19 | 1.06 |
| scoring inputs only | 43 | 1.11 | 25 | 0.72 |
| both (current) | 43 | **1.08** | 24 | 0.75 |

The widened ATR stops are approximately neutral — slightly better
out-of-sample, in fact. The scoring-input correction is what moved the result:
it admitted 15 additional in-sample trades which were, in aggregate, losers.

**So the profit factor of 1.47 reported in the previous round was an
artifact.** It was measured on a system that scored every signal 14 points
below what the live system would score it, and that accidental strictness was
doing real filtering work. Corrected, the FOREX result is IS PF 1.08 and OOS PF
0.75 — no edge, and the previous round's apparent edge was a measurement error
rather than a finding.

This is uncomfortable and it is the right way round: better to discover that a
backtest flattered a strategy now than after funding it.

The consequence is that the family score thresholds were calibrated against an
under-scored system and are now roughly 14 points too lenient. Recalibrating
them is real work, and it needs market data this round has not examined —
choosing the offset that maximises profit factor on *this* history would be
exactly the overfitting the brief forbids.

---

## Deployment gates

| # | gate | equities | crypto | forex | why |
|---|---|---|---|---|---|
| 1 | Equities OOS PF > 1.3 and Sharpe > 0.7 | **FAIL** | — | — | 1 OOS trade. Not a measurement. |
| 2 | Crypto OOS PF > 1.3 and Sharpe > 0.7 | — | **FAIL** | — | PF 1.75, Sharpe 0.20 on 5 OOS trades. |
| 3 | FOREX OOS PF > 1.3 and Sharpe > 0.7 | — | — | **FAIL** | PF 0.75, Sharpe −0.32 on 24 OOS trades. |
| 4 | Walk-forward mean PF > 1.3 across 5 windows | **FAIL** | **FAIL** | **FAIL** | Equities 0/5 folds traded; crypto only 3 folds fit its history; FOREX 2/5 above 1.3 — folds ran 4.21, 0.65, 0.17, 0.31, 1.91. |
| 5 | Monte Carlo > 90% of simulations profitable | **FAIL** | **FAIL** | **FAIL** | Equities and crypto have under 10 trades, so no verdict is computable. FOREX: 42.7% of 5,000 bootstrap resamples profitable. |
| 6 | All 219+ tests pass | **PASS** | | | 255 tests, all passing. |
| 7 | Zero risk-limit violations across all classes | **PASS** | | | Worst segment drawdown 0.67% against a 5% gate; every class inside its section 8.1 caps. |
| 8 | Drift detection implemented and tested | **PASS** | | | Thresholds were already correct; the gap was that nothing consumed them. `StrategyEngine` now takes an `is_enabled` gate wired to `StrategyMonitor`, and a disabled strategy is skipped rather than sized down. |

**5 of 8 gates fail. Do not deploy. No pull request opened.**

Gate 5 deserves a note: for equities and crypto it fails for *lack of evidence*
rather than for negative evidence. Fewer than ten trades cannot be resampled,
and the honest verdict on an unmeasurable quantity is "unmeasured", not "pass".

---

## What to do next, in order

1. **Get daily equity history.** Everything about equities is blocked behind
   this. On weekly bars the median structural stop is already past the 7% cap
   and half of all signals are correctly refused. This is a data-access
   problem, and no amount of strategy work will move it. Alpha Vantage's daily
   adjusted endpoint needs a paid key; a Polygon, Tiingo or Nasdaq Data Link
   key would also do.
2. **Recalibrate the family score thresholds against the corrected scale.**
   They were set when every signal scored ~14 points low. Do this on data held
   back from every measurement in this round — otherwise it is curve-fitting.
   The FOREX walk-forward folds already show what an uncalibrated bar costs:
   fold PFs of 4.21, 0.65, 0.17, 0.31, 1.91 are not a strategy with a weak
   edge, they are a strategy with no edge and high variance.
3. **Give crypto more than one working strategy.** With STRATEGY_003 disabled,
   only STRATEGY_004 produces trades. STRATEGY_002's `established_uptrend`
   killed 2,120 bars and STRATEGY_009 produced nothing. Either adapt them to
   24/7 markets deliberately, or accept that crypto is a one-strategy class and
   size it accordingly.
4. **Do not re-run the FOREX tuning loop.** Three rounds have now produced
   OOS profit factors of 1.00, 1.05 and 0.75 on the same nine years. The
   variation between them is noise from changes elsewhere in the pipeline, not
   signal. Continuing to tune against this window is how it stops being out of
   sample.
