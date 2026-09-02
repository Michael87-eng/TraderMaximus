# Real market data

Place historical OHLCV CSVs here, one file per symbol, named by ticker:

```
data/real/equities/SPY.csv
data/real/equities/AAPL.csv
data/real/equities/MSFT.csv
data/real/crypto/BTC-USD.csv
data/real/crypto/ETH-USD.csv
data/real/forex/EURUSD.csv
data/real/forex/GBPUSD.csv
```

Then run:

```bash
python tools/validate_real_data.py                  # all three asset classes
python tools/validate_real_data.py --asset forex    # one class
```

## Required columns

`timestamp`, `open`, `high`, `low`, `close`. Volume is strongly preferred —
several strategies gate on relative volume and cannot trigger without it.

Header names are matched against aliases, so vendor exports load unchanged:

| field | accepted headers |
|---|---|
| timestamp | `Date`, `time`, `datetime`, `open_time`, `t` (ISO-8601 or epoch s/ms) |
| open/high/low/close | `Open`/`o`/`mid_open`, and the same pattern for the rest |
| volume | `Volume`, `v`, `vol`, `base_volume` |
| adjusted close | `Adj Close`, `adjusted_close` — used in preference when present |

## Use split- and dividend-adjusted prices for equities

Unadjusted equity history contains artificial gaps at every split. The quality
engine correctly flags these as `unadjusted_corporate_action` and rejects the
series — but if a series slipped through, those gaps would read as genuine
breakouts and manufacture signals that never existed. When an `Adj Close`
column is present the loader scales the whole bar by the adjustment factor, so
OHLC stays internally consistent.

## How much history

At least **600 bars** per symbol; 1,500+ (roughly six years of daily bars) is
better. The pipeline needs 300 bars of warm-up before it can make a decision,
and the validation split then divides what remains 70/30 between in-sample and
out-of-sample. Below ~600 bars the out-of-sample segment is too small for its
result to mean anything.

## A note on FOREX volume

Spot FX is decentralised and has no consolidated tape; most feeds (Oanda
included) ship no volume column. That is handled: for FOREX only, an absent
volume column is read as "not reported" rather than "no liquidity". Equities
and crypto both have consolidated volume, so a missing column there is still
treated as a defective feed.

Volume-gated strategies cannot trigger on a volumeless FX feed, which is
correct — a condition that cannot be measured must not be assumed true. The
ATR mean-reversion spec (STRATEGY_010) uses no volume condition and runs
regardless.

## A note on crypto and corporate actions

The `unadjusted_corporate_action` heuristic applies to **equities only**.
Crypto and FX have no splits or dividends to be unadjusted for, so a move that
happens to land near a 1:2 ratio on those classes is always a real one.
Applying the heuristic anyway rejected SOL-USD outright on its -48.5% bar of
2022-11-08 — the FTX collapse, and the single most informative day in that
series.

## Symbols currently used

| class | symbols | interval | note |
|---|---|---|---|
| equities | SPY, AAPL, MSFT, NVDA, TSLA | **weekly** | daily history is premium-gated on every feed reachable from here |
| crypto | BTC-USD, ETH-USD, SOL-USD | daily | real exchange history, not testnet — testnet has no genuine price series to backtest against |
| forex | EURUSD, GBPUSD | daily | no volume column, as above |

The weekly equity interval is not a preference. It has a direct cost: on weekly
bars the median structural stop distance is 7.20% against a 7% cap, so half of
all equity signals are correctly refused on stop placement. See
`docs/PHASE_REPORT.md`.

## Nothing here is committed

`data/real/*.csv` is git-ignored. Market data is usually licensed, and
committing a vendor's history to a public repository is a licence breach.
