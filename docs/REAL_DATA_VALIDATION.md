# Real-market validation: results

First run against genuine market history. **All three asset classes fail the
deployment gate**, and the reason is not that the strategies lose money — it is
that they never trade at all.

## Data

Fetched via the Alpha Vantage MCP server. Every series passes the section 3
quality checks.

| Asset class | Symbols | Bars | Span | Timeframe |
|---|---|---|---|---|
| Equities | SPY, AAPL, MSFT | 1,399 each | 1999-11 to 2026-08 | **weekly** |
| Crypto | BTC-USD, ETH-USD | 2,500 each | 2019-10 to 2026-08 | daily |
| FOREX | EURUSD, GBPUSD | 2,500 each | 2017-01 to 2026-08 | daily |

Equities are **weekly** because Alpha Vantage's free tier gates daily history
behind a premium plan — `outputsize=full` on `TIME_SERIES_DAILY` is refused,
leaving only 100 bars, and the pipeline needs 300 for warm-up alone. Weekly
adjusted data is free and gives 27 years. Sharpe is annualised at 52/yr for
equities, 365/yr for crypto and 252/yr for FX; using 252 throughout would have
inflated the weekly Sharpe by ~2.2x and could have carried a strategy through
a gate it never earned.

## Result

| Asset class | OOS trades | OOS PF | OOS Sharpe | OOS win rate | Gate |
|---|---|---|---|---|---|
| Equities | 0 | — | — | — | **FAIL** |
| Crypto | 0 | — | — | — | **FAIL** |
| FOREX | 0 | — | — | — | **FAIL** |

Profit factor, Sharpe and win rate are undefined with zero trades. Reporting
them as 0.00 would imply the strategies lost money; they did not trade.

## Why: where signals die

Sampling every 4th bar of the usable history and recording why no signal
emerged:

| | FOREX (EURUSD) | Crypto (ETH-USD) | Equities (SPY) |
|---|---|---|---|
| no entry conditions met | 509 | 437 | 232 |
| regime permits nothing | 24 | 100 | 38 |
| **met all conditions, score too low** | **12** | **8** | 0 |
| **signals generated** | **0** | **0** | **0** |

Two distinct causes, and they need different answers.

### Cause 1: entry conjunctions almost never hold (dominant)

The strategies are conjunctions of six or seven independently uncommon
conditions. On real data they essentially never all hold at once.

For the FOREX ATR mean-reversion spec (STRATEGY_010) over 2,500 bars of real
EURUSD, condition-level failure counts:

| condition | failures |
|---|---|
| `rsi_oversold` (RSI < 30) | 643 |
| `price_at_lower_bollinger_band` (bb_position < 0.05) | 596 |
| `bullish_reversal_at_support` | 565 |
| `stretched_from_mean` (> 2 ATR) | 556 |

Note that `price_at_lower_bollinger_band` and `stretched_from_mean` measure
nearly the same thing — price far below its mean — so the conjunction
multiplies rarity without adding independent confirmation. And
`bb_position < 0.05` is a very literal reading of §6.2.10's *"price near lower
Bollinger Band"*: it demands price at or through the band, not near it.

**Answer to "does ATR mean reversion work on FOREX": it never fired once in
nine years of real EURUSD data.** Its performance is untested, not bad.

### Cause 2: counter-trend setups cannot clear their own threshold

When conditions *did* all hold, the score never came close:

| | setups | score range | threshold |
|---|---|---|---|
| FOREX | 12 | 40.5 – 49.6 | 75 |
| Crypto | 8 | 32.3 – 59.6 | 75 |

This is the §5.4 / §7.1 / §7.2 contradiction documented in
[DECISIONS.md](DECISIONS.md), now **confirmed on real data rather than
synthetic**: §5.4 routes counter-trend strategies into RANGE and
HIGH_VOLATILITY, §7.2 gives those regimes the highest thresholds (70 and 75),
and §7.1 scores counter-trend setups lowest there. Making the rubric
family-aware raised the scores but not past 75.

Every one of the 20 qualifying setups across both classes was a counter-trend
strategy in a high-volatility regime. Not one could ever have traded.

## What this does and does not show

**Shows:** the data path, quality engine, regime engine, strategy evaluation,
scoring and validation harness all work on genuine market history across three
asset classes and two timeframes.

**Does not show:** anything about whether these strategies have an edge. A
strategy that never trades has no measurable expectancy. The gate result is
"untested", reported as FAIL because it cannot be shown to pass.

**Volatility scaling on crypto** could not be assessed either: it correctly
selected the 0.50% risk band for ETH at 79% realised volatility and BTC at
50%, but with no positions taken, its protective effect is unmeasured.

## Bugs this run exposed

All three were in the data path and all are fixed:

1. **The MCP-to-CSV converter dropped the `adjusted close` column.** The
   pipeline then saw raw prices, whose split gaps read as 50% crashes. AAPL and
   MSFT were rejected outright.
2. **A real crash was misfiled as a stock split.** AAPL fell 50.7% on
   2000-09-29 (the profit warning) on 4.5x normal volume. Because −50.7% is
   close to a 2:1 ratio, the corporate-action heuristic claimed it. Fixed: when
   the feed supplies adjusted prices, splits are already removed, so that
   heuristic is skipped entirely.
3. **A real crash was misfiled as a bad tick.** Bitcoin fell 40% on 2020-03-12
   (COVID "Black Thursday") and stayed down. A 12-sigma filter rejected the
   whole series. Fixed by testing whether the move *reverts on the next bar* —
   a corrupt print snaps back, a genuine crash persists. Rejecting that stretch
   would have discarded the single most informative period in crypto history,
   which is exactly the period a risk system most needs.
4. **Adjustment scaling broke OHLC ordering** by a single ULP on bars where
   `low == close`, failing SPY. Extremes are now re-derived after scaling.

## The decision this leaves open

Making the system trade on real data requires relaxing something in the
signal-generation path. Neither option should be taken without a deliberate
choice, and neither touches a §8.1 hard limit:

1. **Reduce the redundancy in counter-trend entry conditions.** Dropping
   `stretched_from_mean` (which duplicates `bb_position`) and reading "near the
   lower band" as `bb_position < 0.20` rather than `< 0.05` would let
   STRATEGY_010 fire. This is an interpretation change to §6.2.10, of the same
   kind as the breakout-basis correction.
2. **Resolve the §7.2 threshold contradiction.** Either lower the
   HIGH_VOLATILITY minimum from 75, or stop routing counter-trend strategies
   into a regime whose threshold they structurally cannot meet.

Both are judgement calls about the specification, not bugs. They are recorded
here rather than actioned so the choice is visible.
