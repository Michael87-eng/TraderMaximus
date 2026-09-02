# Phase 1 — daily equity data

## The finding that changes the plan

The provider ranking in the brief was Polygon → Alpha Vantage Premium → Tiingo
→ Nasdaq Data Link. Measured from this environment, that ranking is close to
inverted, because reachability here is a property of the sandbox rather than of
the vendor.

Every commercial data host tested is refused at CONNECT by the environment's
network policy:

| host | result |
|---|---|
| `api.polygon.io` | `connect_rejected` (403 at CONNECT) |
| `api.tiingo.com` | `connect_rejected` |
| `data.nasdaq.com` | `connect_rejected` |
| `eodhd.com` | `connect_rejected` |
| `stooq.com` | `connect_rejected` |
| `www.alphavantage.co` | `connect_rejected` |

Note the last row: even Alpha Vantage's own API host is blocked over direct
HTTPS. **An API key does not make a vendor usable here.** No key changes a
CONNECT refusal.

What *does* work is MCP. The Alpha Vantage and Finnhub MCP servers are already
connected and do reach their vendors' APIs — Finnhub's `/stock/candle` returns
an application-level 403 saying "You don't have access to this resource", which
is a *plan* refusal and proves the endpoint was reached.

So there are two routes to daily equity bars, and they cost different things:

| route | what it needs | code change | network change |
|---|---|---|---|
| **Alpha Vantage Premium** | a paid plan on the key behind the existing MCP server | none | none |
| **Finnhub paid plan** | a paid plan on the key behind the existing MCP server | none | none |
| Polygon / Tiingo / Nasdaq | the host added to the environment's network allowlist, **then** a paid plan | none | yes |

### Recommendation

**Alpha Vantage Premium**, unless there is a reason to prefer another vendor.

It is the only option where nothing but the plan changes: the MCP server is
already connected, `TIME_SERIES_DAILY_ADJUSTED` is the same tool call that
currently returns a premium error, and prices arrive already adjusted for
splits and dividends. Finnhub would also work through MCP but its candles are
unadjusted, which puts the split problem back in our lap — and unadjusted
splits are the exact failure mode that has already cost this project four
separate data defects.

Polygon is a better product than either. It is ranked below them here only
because using it also requires editing the environment's network configuration
to allowlist `api.polygon.io`. If you would rather do that, it is a perfectly
good choice and the ingestion code below does not care which vendor supplies
the rows.

---

## Will daily bars actually fix equities? Measured, not assumed

Before spending anything, it is worth knowing whether daily bars solve the
problem they are being bought for. The weekly equity failure is that 51-52% of
signals are refused on stop placement, because structural support sits beyond
the 7% maximum stop.

Crypto is the only class where the same trending, high-volatility instruments
exist as a long daily series that can also be aggregated to weekly — so the bar
interval can be isolated with instrument, period, cap and code all held fixed:

| interval | median distance | 75th pct | beyond the 5% cap | median in σ |
|---|---|---|---|---|
| **daily** | 3.83% | 8.88% | **41%** | 1.25 |
| **weekly** | 9.92% | 18.24% | **73%** | 1.32 |

Structural support sits about **1.3 standard deviations** below price whatever
the interval. That is the scale-free constant, and it is what makes the
prediction possible. What changes with the interval is the denominator: a
weekly σ is √5 times a daily σ, while the stop cap stays fixed in percent, so
the same cap is 2.2× tighter in σ terms on weekly bars.

Applying that to equities, whose mean realised volatility across the five
symbols is about 33%:

```
weekly σ = 33% / √52  = 4.58%   ->  a 7% cap is 1.53 σ
daily  σ = 33% / √252 = 2.08%   ->  a 7% cap is 3.37 σ
```

Daily bars give **more than twice the headroom in σ terms**, and more headroom
than crypto daily had (1.47 σ) at a 41% rejection rate. The prediction is that
daily equity rejection falls well below 25%.

Two hypotheses were tested and rejected on the way to this one, which is why it
is stated with some confidence rather than as arithmetic:

* *Maybe the decision window is the problem* — `tail(400)` spans 7.7 years on
  weekly bars, so "nearest support" could be a price from a different era.
  Measured across windows of 400/260/160/120/80 weekly bars, rejection went
  **52% → 54% → 57% → 58% → 61%**. Shorter windows are worse, not better. The
  hypothesis is wrong.
* *Maybe weekly/daily scaling is mild* — the FX resample gave a weekly/daily
  ratio of only 1.10×, implying daily equities at 6.55% and barely any
  improvement. But FX is range-bound and its structure distances are ~1%, an
  entirely different regime; the crypto test above, on trending instruments, is
  the right analog and gives 2.6×.

---

## What has been built, ready for the key

### `tradermaximus/data/sources.py`

A vendor-agnostic ingestion layer, separate from `providers.py` because the
live path and the research path have different failure modes. A live fetch
returning 900 of 1,000 bars is a degraded quote; a history download that
silently truncates is a backtest run on the wrong data and reported as if it
were right.

* `AVAILABILITY` — the reachability table above, as data, so vendor choice is
  made on measurement rather than on a pricing page.
* `normalise_rows` — maps every vendor's column naming onto ours (Polygon's
  `t/o/h/l/c/v`, Alpha Vantage's `1. open`, Tiingo's `adjClose`, …). A missing
  required column **raises** rather than being inferred: a file that loads,
  validates, backtests and is wrong is the worst available outcome.
* `rows_to_csv` — sorts oldest-first, de-duplicates on timestamp (vendors
  differ on ordering and some repeat the latest bar), and refuses to write an
  empty file.
* `load_json_payload` — handles a bare list, a `results`/`data`/`candles`
  wrapper, and the MCP `{"result": "<csv>"}` envelope that large tool results
  are offloaded into.

### `tools/ingest_daily_equities.py`

Normalises payloads into `data/real/equities_daily/`, then runs the same
data-quality engine the validator runs, so a bad download is caught at
ingestion rather than as a strange backtest result. `--options` prints the
vendor table.

### `tools/preflight_daily_equities.py`

**Run this before re-running validation.** It measures structural stop distance
on the new daily bars and compares it against the 52% weekly baseline, in under
a minute rather than after a twenty-minute validation run that produces two
trades again.

The acceptance bar is **under 25% beyond the cap**. That threshold was set from
the prediction above, before the data exists — which is what makes it a test
rather than a rationalisation. If it fails, daily data was not the blocker, and
the equity strategies need a different diagnosis rather than another validation
cycle spent discovering the same thing.

Verified to fail correctly on the current weekly data.

---

## Execution order

1. **You**: upgrade the Alpha Vantage key behind the MCP server to a plan that
   includes `TIME_SERIES_DAILY_ADJUSTED` with `outputsize=full`. (Or allowlist
   `api.polygon.io` in the environment configuration and get a Polygon key.)
2. **Me**: fetch SPY, AAPL, MSFT, NVDA, TSLA daily-adjusted, 5+ years, and
   ingest them. The same tool call that errors today will simply work.
3. **Me**: `python tools/preflight_daily_equities.py` — one minute, and it
   decides whether step 4 is worth running.
4. **Me**: if pre-flight passes, re-run equity validation on daily bars with all
   eight equity strategies, 300-bar warm-up, chronological 70/30 split.
5. **Both**: read the result honestly. If the equity gates still fail with a
   real trade count behind them, that is a finding about the strategies, and
   Phase 3's recalibration is the next move — not more data.

One caveat worth stating now, so it is not a surprise at step 5: three of the
eight equity strategies are CAN SLIM (STRATEGY_005–008) and gate on fundamental
data — EPS rating, RS rating, industry rank, earnings growth, ROE,
accumulation/distribution. None of that is loaded. `_can_slim_fundamentals`
fails closed when the data is absent, deliberately, because a CAN SLIM breakout
without the fundamentals is a different and unvalidated strategy wearing the
same name. So daily price data unblocks **five** of the eight equity
strategies, not eight. Fundamentals are a separate feed and a separate
decision.
