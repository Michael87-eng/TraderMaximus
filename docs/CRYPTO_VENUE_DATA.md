# Crypto execution-venue data: what was fetched, and what is wrong with it

Collected 2026-11-09T17:22:00Z on branch `claude/crypto-venue-data`. Read-only public endpoints only; no orders were placed and no credentials were used or requested.

Machine-readable companion: `data/venue/MANIFEST.json` (per-file sha256, row counts, spans, full per-archive provenance, and a `caveats` array) and `data/venue/defillama/PROBE.json`.

## Headline: the live Binance API is not reachable from here

`api.binance.com` answers **HTTP 451** — *"Service unavailable from a restricted location"* — to every request from this container, `/api/v3/ping` included. This is Binance's own geo-restriction, not the environment's network policy (the host is on the allowlist and the TLS connection succeeds).

Consequences:

- **Klines were taken from `data.binance.vision` instead**, Binance's official public archive, which is also on the allowlist. Same exchange, same spot klines, same column layout — but published as monthly/daily ZIP objects. 600 archive objects were used and **every one was verified against its published `.zip.CHECKSUM` sha256 (0 mismatches)**.
- **`exchangeInfo` could not be obtained at all.** `tickSize`, `stepSize`, `minNotional`, `status` and permitted order types are **INSUFFICIENT EVIDENCE** — not estimated, not defaulted, not guessed. `data/venue/binance/exchangeinfo_{BTCUSDT,ETHUSDT}.UNAVAILABLE.json` are *attempt records*, not instrument data; three attempts per symbol, HTTP 451 each time.
- **The archive lags the live API.** It holds no bars for the current UTC day: the 2026-11-09 daily object was a 404 while 2026-11-08 was present. No still-forming bar ever needed dropping, because the archive never contained one.

## Task A — Binance spot klines

| File | Rows | First bar (UTC) | Last bar (UTC) | Gaps | Missing bars | Dup ts |
|---|---:|---|---|---:|---:|---:|
| `data/venue/binance/BTCUSDT_1d.csv` | 3,371 | 2017-08-17T00:00:00Z | 2026-11-08T00:00:00Z | 0 | 0 | 0 |
| `data/venue/binance/BTCUSDT_1h.csv` | 80,773 | 2017-08-17T04:00:00Z | 2026-11-08T23:00:00Z | 29 | 126 | 0 |
| `data/venue/binance/ETHUSDT_1d.csv` | 3,371 | 2017-08-17T00:00:00Z | 2026-11-08T00:00:00Z | 0 | 0 | 0 |
| `data/venue/binance/ETHUSDT_1h.csv` | 80,773 | 2017-08-17T04:00:00Z | 2026-11-08T23:00:00Z | 29 | 126 | 0 |

Header is exactly `timestamp,open,high,low,close,volume`. Timestamps are the bar's **open** time, UTC ISO-8601 at second precision, sorted oldest-first, de-duplicated. `volume` is **base-asset** volume (BTC or ETH), not quote volume.

Span is identical across both symbols and both intervals: the first bar of BTCUSDT and ETHUSDT spot trading, 2017-08-17T00:00:00Z, through 2026-11-08T23:00:00Z.

## Task B — Kraken cross-check

| File | Raw bars returned | Rows written | First bar (UTC) | Last bar (UTC) | Gaps |
|---|---:|---:|---|---|---:|
| `data/venue/kraken/XBTUSD_1d.csv` | 721 | 720 | 2024-11-19T00:00:00Z | 2026-11-08T00:00:00Z | 0 |
| `data/venue/kraken/ETHUSD_1d.csv` | 721 | 720 | 2024-11-19T00:00:00Z | 2026-11-08T00:00:00Z | 0 |
| `data/venue/kraken/USDTUSD_1d.csv` *(diagnostic, see below)* | 721 | 720 | 2024-11-19T00:00:00Z | 2026-11-08T00:00:00Z | 0 |

Kraken returned 721 bars per pair — the ~720-bar endpoint window, as expected and not worked around. The final bar (2026-11-09T00:00:00Z) was a partial day and was dropped.

### Cross-venue basis, as specified

`|Binance close − Kraken close| / Kraken close`, in basis points, over daily bars matched on identical UTC timestamp. Percentiles use linear interpolation between order statistics (the numpy default).

| Comparison | Days | Median | p95 | Max |
|---|---:|---:|---:|---:|
| BTCUSDT vs XBTUSD | 720 | **4.27 bps** | **14.68 bps** | **24.66 bps** |
| ETHUSDT vs ETHUSD | 720 | **4.32 bps** | **15.02 bps** | **25.29 bps** |
| Both pooled | 1440 | 4.32 bps | 14.99 bps | 25.29 bps |

Overlap window: 2024-11-19T00:00:00Z to 2026-11-08T00:00:00Z. 2,651 Binance days have no Kraken counterpart and are unmeasured.

### That number is not what it looks like

Binance quotes **USDT**; Kraken quotes **USD**. The metric as specified measures venue dislocation and USDT/USD deviation added together. Measuring the stablecoin leg separately, over the same 720 days (Kraken `USDTZUSD`):

- `|USDT/USD − 1|`: median **3.30 bps**, max **23.50 bps**.

Converting the Binance close at Kraken's USDT/USD daily close before comparing:

| Comparison | Median | p95 | Max |
|---|---:|---:|---:|
| BTCUSDT vs XBTUSD, USDT-adjusted | 1.29 bps | 6.06 bps | 10.38 bps |
| ETHUSDT vs ETHUSD, USDT-adjusted | 1.32 bps | 6.04 bps | 15.35 bps |

So roughly three quarters of the headline basis is the stablecoin, not the two venues disagreeing about the price of BTC. Sizing a cross-venue allowance off the headline figure prices in USDT redemption risk without naming it. `USDTUSD_1d.csv` is included for that reason and is flagged `diagnostic_only` in the manifest — it was not part of the requested fetch.

## Task C — DefiLlama endpoint probe

All five endpoints returned **HTTP 200**. No evaluation of trading value was made, per scope.

| Endpoint | Status | Size | Series span (UTC) | Points | Granularity |
|---|---:|---:|---|---:|---|
| `api.llama.fi/v2/historicalChainTvl` | 200 | 123,692 B | 2017-09-27T00:00:00Z → 2026-11-09T00:00:00Z | 3,331 | daily (86400 s), 1 distinct step |
| `api.llama.fi/v2/historicalChainTvl/Ethereum` | 200 | 123,004 B | 2017-09-27T00:00:00Z → 2026-11-09T00:00:00Z | 3,331 | daily (86400 s), 1 distinct step |
| `api.llama.fi/overview/dexs?excludeTotalDataChart=true` | 200 | 20.3 MiB | 2014-02-17T00:00:00Z → 2026-11-09T00:00:00Z *(in `totalDataChartBreakdown`)* | 4,649 | daily (86400 s) |
| `stablecoins.llama.fi/stablecoincharts/all` | 200 | 1.3 MiB | 2017-11-29T00:00:00Z → 2026-11-09T00:00:00Z | 3,268 | daily (86400 s), 1 distinct step |
| `coins.llama.fi/prices/current/coingecko:bitcoin` | 200 | 112 B | point-in-time snapshot | 1 | n/a |

### Fields and units

**`/v2/historicalChainTvl`** and **`/v2/historicalChainTvl/Ethereum`** — array of `{date, tvl}`. `date` is a JSON **int** of unix seconds, every value aligned to `00:00:00Z`. `tvl` is USD in absolute dollars. Both series carry 138 leading days of `tvl: 0` (first non-zero 2018-02-12T00:00:00Z); those are the only zeros present. All-chain latest value 79,003,625,907; Ethereum 41,953,070,615 - both as served at 17:24:25Z, and both revised within two minutes (see the revision test below).

**`/overview/dexs?excludeTotalDataChart=true`** — `excludeTotalDataChart=true` empties `totalDataChart` but **`totalDataChartBreakdown` is still returned in full**, which is why the response is 21,259,901 bytes (20.3 MiB). Breakdown entries are `[unix_seconds, {protocol_name: usd_volume}]` — keyed by protocol, *not* chain-nested despite the name; 1,107 protocols in the newest entry, 1,587 protocol records of 35 fields each, 298 chain names in `allChains`. Values mix int and float across entries (384 all-float, 3,458 all-int, 807 mixed). The `total24h`/`total7d`/`total30d`/`total1y`/`totalAllTime` family is **USD volume**, and `change_*` is **percent** — these are rolling windows, not a series. Earliest breakdown point is 2014-02-17 at `{'Counterparty DEX': 6.23}`.

**`/stablecoincharts/all`** — array of records. `date` is a JSON **string** of unix seconds (unlike `historicalChainTvl`, where it is an int), all `00:00:00Z` aligned. `totalCirculating[peg]` is in **units of the pegged asset** (`peggedEUR` is EUR, `peggedJPY` is JPY); `totalCirculatingUSD[peg]` is **USD**. The peg-key set **grows along the series**, from 1 key in the first record to 30 in the last (30 distinct). Three distinct top-level key sets occur: 476 records lack `totalUnreleased`, and `totalMintedUSD`/`totalBridgedToUSD` appear in **exactly one record — the final one**.

**`/prices/current/coingecko:bitcoin`** — `{coins: {"coingecko:bitcoin": {price, symbol, timestamp, confidence}}}`. `price` is USD; `timestamp` is unix seconds and is an **observation time, not a bar boundary**; `confidence` is 0–1 and the response does not define what it measures.

### Publication lag: INSUFFICIENT EVIDENCE

**Publication lag cannot be determined from the response alone, for any of the five endpoints.** No body carries an as-of, published-at, revision or finality field. The only time-related HTTP headers are `Date` (origin/CDN clock) and `Age` (observed 236–263 s) — `Age` is how long the *CDN* has held that response, which says nothing about the staleness or finality of the underlying data.

Every daily series' newest point is dated `2026-11-09T00:00:00Z`, the current UTC day, 17.4 h before the probe. Whether that point is final or still accumulating is not stated anywhere in the response.

What *can* be measured is whether the newest point moves. The same point — dated `2026-11-09T00:00:00Z` — was read three times:

| Endpoint (field) | 17:24:25Z | 17:26:10Z | 17:41:11Z | Moved? |
|---|---:|---:|---:|---|
| `historicalChainTvl` (`tvl`) | 79,003,625,907 | 79,021,985,782 | 79,021,985,782 | **yes**, 18,359,875 USD (2.3239 bps) |
| `historicalChainTvl/Ethereum` (`tvl`) | 41,953,070,615 | 41,972,455,634 | 41,972,455,634 | **yes**, 19,385,019 USD (4.6206 bps) |
| `stablecoincharts/all` (`totalCirculatingUSD.peggedUSD`) | 293,008,620,022.67 | 293,008,620,022.67 | 293,008,620,022.67 | no |

**The newest daily point is provisional and is revised while its day is still running.** The point's date never changed, the point count stayed at 3,331, and nothing in the response marked the value as revised. (The first read had a CDN cache age of 243 s and the second 1 s, so the change is in the origin data, not a caching artefact.)

Two cautions on reading this: the 15-minute interval with **no** change is *not* evidence of finality — it only bounds the update cadence from below. And `stablecoincharts/all` not moving in this window does not make it final either; it was simply not observed to move.

The practical consequence is point-in-time correctness: a backtest that reads this series today and treats the last row as what was knowable on that date is using a number that did not exist in that form during the day — and re-running the same code over the same date range hours later will silently give different answers. Either drop the newest point, or snapshot what was served and store it.

### Bar durations are not uniform

Measured on the raw archive: in each 1h series **15 bars have `closeTime - openTime != 1 h`**, including one with a **negative** duration — BTCUSDT `2020-12-21T14:00:00Z` closes at `2020-12-21T13:47:20.521Z`, 12 m 39 s *before* it opens. The shortest is **14.8 seconds** (`2018-01-04T03:00:00Z`). The anomalies cluster at outage boundaries and are near-identical between the two symbols. The daily series is clean: all 3,363 monthly-archive daily bars span exactly 86,400,000 ms.

The shipped CSVs keep open time only, so this is invisible in them — the full list is in the manifest under `files[...].integrity.bar_duration_anomalies`. It also means Binance's own `closeTime` is not reliable here, and the "drop the bar if closeTime is in the future" rule leans on exactly that field.

## Data defects found

Plain list, worst first. Each has full detail under `caveats` in `data/venue/MANIFEST.json`.

- **[high] binance-live-api-unreachable** — api.binance.com returned HTTP 451 ('Service unavailable from a restricted location') to every request from this container, including /api/v3/ping, /api/v3/klines and /api/v3/exchangeInfo. 3 attempts per symbol for exchangeInfo, all 451.
- **[high] exchangeinfo-not-obtained** — tickSize, stepSize, minNotional, status and permitted order types were NOT obtained for either symbol. The two exchangeinfo_*.UNAVAILABLE.json files are attempt records, not instrument data.
- **[high] archive-publication-lag** — The archive contains no bars for the current UTC day. Latest 1h bar is 2026-11-08T23:00:00Z (closing 2026-11-08T23:59:59Z); the daily archive object for 2026-11-09 returned HTTP 404 while the 2026-11-08 object was present. Snapshot taken at 2026-11-09T17:22:00Z.
- **[high] timestamp-unit-change-ms-to-us** — Binance archive open_time/close_time switched from milliseconds to MICROSECONDS exactly at 2025-01-01T00:00:00Z. Last millisecond bar: 2024-12-31T23:00:00Z. Both units appear within one series: {'us': 16992, 'ms': 64525} rows (counts include the monthly/daily overlap).
- **[high] off-grid-hourly-bars-feb-2018** — 43 consecutive 1h bars, 2018-02-09T09:28:14Z through 2018-02-11T03:28:14Z, are offset from the hour grid by 1694789 ms (28 m 14.789 s). They are hourly-spaced but not hour-aligned, and realign at 2018-02-11T04:00:00Z. Identical in BTCUSDT and ETHUSDT. Follows the 32-bar outage gap ending 2018-02-09.
- **[high] hourly-bar-durations-are-not-all-one-hour** — In each 1h series, 15 bars have closeTime - openTime != 1 h, measured on the raw archive. Durations run from 14.8 s (2018-01-04T03:00:00Z, BTCUSDT) up to 2,454,774 ms (2021-02-11T03:00:00Z), and one bar has a NEGATIVE duration: 2020-12-21T14:00:00Z closes at 2020-12-21T13:47:20.521Z, 12 m 39 s BEFORE it opens. Full list per symbol in files[...].integrity.bar_duration_anomalies. The anomalies sit at outage boundaries and are near-identical between BTCUSDT and ETHUSDT (they differ only in milliseconds).
- **[high] quote-currency-mismatch-usdt-vs-usd** — Binance pairs quote in USDT; Kraken pairs quote in USD. The requested basis metric therefore measures venue dislocation AND USDT/USD deviation together. Measured over the same 720 days, |USDT/USD - 1| had a median of 3.3 bps and a max of 23.5 bps. Re-running the comparison with the Binance close converted at Kraken's USDT/USD daily close drops the BTC median from 4.2694 to 1.2944 bps and the p95 from 14.6778 to 6.061 bps.
- **[high] defillama-publication-lag-unknown** — None of the five endpoints returns an as-of, published-at, revision or finality field. The only time-related HTTP headers are Date and Age; Age is CDN cache age (observed 236-263 s), which says nothing about the staleness or finality of the underlying data. Every daily series' last point is dated 2026-11-09T00:00:00Z, i.e. the current UTC day, 17.4 h before the probe.
- **[high] defillama-stablecoin-schema-drift** — stablecoincharts/all: 'date' is a JSON STRING of unix seconds, whereas historicalChainTvl's 'date' is an int. The peg-key set inside totalCirculating grows from 1 key in the first record to 30 in the last (30 distinct keys across the series). Three distinct top-level key sets occur: 476 records lack totalUnreleased, and totalMintedUSD/totalBridgedToUSD appear in exactly ONE record - the final one.
- **[high] defillama-newest-daily-point-is-revised-intraday** — The same newest daily point (dated 2026-11-09T00:00:00Z) was read three times from /v2/historicalChainTvl and its VALUE changed. All-chain tvl: 79,003,625,907 served 17:24:25Z (CDN cache age 243 s), then 79,021,985,782 served 17:26:10Z (cache age 1 s) - a move of 18,359,875 USD (2.3239 bps) - then unchanged at 17:41:11Z. Ethereum tvl moved 19,385,019 USD (4.6206 bps) over the same interval. Point count stayed at 3331 and the point's date never changed. stablecoincharts/all's totalCirculatingUSD.peggedUSD did NOT move across the three reads (293,008,620,022.67). prices/current moved as expected for a spot quote.
- **[medium] subsecond-precision-truncated** — 43 bars (the same Feb-2018 window) have sub-second open times, e.g. 2018-02-09T09:28:14.789Z. The required output format is second precision, so .789 is truncated.
- **[medium] hourly-gaps** — The 1h series has 29 gaps totalling 126 missing bars, all between 2017-09-06 and 2023-03-24, identical in both symbols. Largest is 32 bars ending 2018-02-09. The 1d series has 0 gaps.
- **[medium] volume-is-base-asset** — The volume column is archive field 5 = base asset volume (BTC or ETH). Quote-asset volume (field 7), trade count (field 8) and taker-buy splits (fields 9-10) exist in the source and were NOT carried into these CSVs. Kraken's volume is its OHLC field index 6, also base volume; its field index 5 (vwap) was discarded.
- **[medium] kraken-720-bar-window** — api.kraken.com /0/public/OHLC returned only 721 daily bars per pair - the endpoint's ~720-bar trailing window. After dropping the forming final bar, the shipped files span 2024-11-19T00:00:00Z to 2026-11-08T00:00:00Z. This is the documented endpoint limit and was not worked around.
- **[medium] transient-fetch-failure-silently-loses-data** — One of 636 archive requests (BTCUSDT 1d 2021-04) failed with a connection error rather than a 404 on the first pass, and succeeded on retry. Without the present/404 distinction it would have been indistinguishable from 'month does not exist'.
- **[medium] defillama-leading-zero-tvl** — historicalChainTvl returns 138 leading days with tvl exactly 0, from 2017-09-27 to 2018-02-11; first non-zero is 2018-02-12T00:00:00Z. Identical count for the all-chain and Ethereum series. Those are the only zero values in either series.
- **[medium] defillama-dexs-param-does-not-shrink-response** — excludeTotalDataChart=true empties totalDataChart (length 0) but totalDataChartBreakdown is still returned in full (4649 daily points, 2014-02-17 to 2026-11-09), alongside 1587 protocol records of 35 fields each. Observed response: 21,259,901 bytes (20.3 MiB).
- **[low] monthly-daily-archive-overlap** — October 2026 is published both as a monthly archive and as 31 per-day archives, producing 31 duplicate timestamps in the 1d source and 744 in the 1h source. Value conflicts between the two: 0 and 0.
- **[low] kraken-forming-bar-dropped** — Kraken returned a bar for 2026-11-09T00:00:00Z whose day had not closed; it was dropped from all three Kraken CSVs. Kraken's result also carries a 'last' field (XBTUSD: 1794096000 = 2026-11-08T00:00:00Z), which is a continuation cursor, not a data boundary.
- **[low] numeric-string-formats-differ** — Price and volume values are copied verbatim from each venue. Binance uses fixed 8-decimal strings ('4261.48000000'); Kraken uses variable precision ('90508.0', '92538.5').
- **[low] defillama-dex-history-predates-dexs** — totalDataChartBreakdown's earliest point is 2014-02-17 with value {'Counterparty DEX': 6.23}, years before the AMM era.
- **[low] prices-endpoint-is-a-snapshot** — coins.llama.fi/prices/current returns a single point: price (USD), symbol, timestamp (unix seconds - an observation time, not a bar boundary) and confidence (0-1, meaning not defined by the response). Observed size 112 bytes.
- **[low] clock-trust** — Bar-dropping and lag figures are computed against the container clock, which agreed with DefiLlama's returned price timestamp to within about 3 minutes and with Kraken's and the CDN's Date headers.

## What is INSUFFICIENT EVIDENCE

- **Binance instrument filters** — `tickSize`, `stepSize`, `minNotional`, `status`, permitted order types. The only host that serves them is geo-blocked. Not estimated.
- **DefiLlama publication lag and point finality** — for all five endpoints.
- **Cross-venue basis before 2024-11-19** — Kraken's endpoint returns ~720 bars, so 2,651 of the 3,371 Binance days have no counterpart. The measured window contains no dislocation event comparable to March 2020 or May 2021.
- **Binance archive publication schedule** — that the current day's object was absent at 2026-11-09T17:22:00Z while the previous day's was present bounds the lag below ~18 h on one observation. One observation is not a schedule.
- **Whether these CSVs match what `/api/v3/klines` would return** — not verifiable from here, since that endpoint is unreachable. The archive is Binance's own publication of the same bars, and its published checksums all verified, but the two were not compared.

## Reproducing

Every URL used is recorded in `data/venue/MANIFEST.json`: per-CSV `source.actual_url_template` plus a `source.files` array naming each archive object with its sha256, byte count and row count, and for Kraken the exact request URL and parameters. DefiLlama request URLs, statuses, headers and response sizes are in `data/venue/defillama/PROBE.json`.
