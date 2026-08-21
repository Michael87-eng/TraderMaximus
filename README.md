# TraderMaximus

A risk-first algorithmic trading system for equities, ETFs, crypto and FOREX.

The organising principle, from the specification's final mandate:

> **The AI may recommend. The risk engine decides.**

Strategies *propose* trades. Nothing downstream can open a position without a
`RiskVerdict` from the risk engine, and no strategy, model or forecast can
override a hard limit. Survival ranks above profit; system integrity ranks
above both.

The system runs in **paper mode by default**. There is no `live` CLI
subcommand — going live is a deliberate configuration act, not a flag someone
can reach for at 2am.

---

## Status

The pipeline is complete and tested end to end against synthetic data: 179
tests covering the risk mathematics, indicators, regime classification,
strategy selection, execution lifecycle, reconciliation and validation gates.

**It has not been validated on real market history, and no strategy in it has
an established edge.** See [What is *not* done](#what-is-not-done).

---

## Quick start

```bash
python -m tradermaximus.cli limits      # resolved risk limits and their provenance
python -m tradermaximus.cli tools       # MCP catalogue and availability
python -m tradermaximus.cli readiness   # section 31.1 readiness checklist
python -m tradermaximus.cli paper       # a paper session: pre-market -> cycles -> post-market
python -m tradermaximus.cli backtest    # backtest plus the section 20.2 validation gates

python -m unittest discover -s tests -t .
```

No third-party packages are required to run the engines. `PyYAML` is needed
only to read YAML configuration; JSON configs work without it.

---

## The pipeline

```
market data ──► data quality ──► features ──► regime ──► strategy ──► scoring
                     │                          │                        │
                  no data,               regime decides            0-100 score
                  no trade              which strategies           vs. regime
                                         may even run                minimum
                                                                        │
                                                                        ▼
     journal ◄── reconciliation ◄── execution ◄── sizing ◄── RISK AUTHORISATION
                                                                (27 checks)
```

Each stage can only *remove* a trade, never add one. A candidate that survives
to execution has passed data validation, regime eligibility, a scoring
threshold, stop placement, sizing caps and 22 portfolio-level rejection rules.

| Layer | Module | Spec |
|---|---|---|
| Configuration governance | `core/config.py` | §29 |
| Data quality | `data/quality.py` | §3 |
| Provider chain | `data/providers.py` | §3.4 |
| Indicators | `indicators/` | §4 |
| Market regime | `regime/engine.py` | §5 |
| Strategies | `strategy/library.py` | §6.2 |
| Signal scoring | `strategy/scoring.py` | §7 |
| Risk authorisation | `risk/engine.py` | §8 |
| Position sizing | `risk/sizing.py` | §9 |
| Stops / targets / trailing | `risk/stops.py`, `targets.py`, `trailing.py` | §10–12 |
| Portfolio risk (VaR, ES) | `risk/portfolio.py` | §13 |
| Execution | `execution/` | §14, §17 |
| Kill switch / breakers | `risk/kill_switch.py`, `circuit_breakers.py` | §15–16 |
| Events | `news/events.py` | §19 |
| Backtest / validation / drift | `research/` | §20–21, §23 |
| Orchestration | `orchestrator.py` | §26, §31 |

---

## The two V4 corrections

Both are implemented and pinned by tests against the specification's own
worked examples.

### 1. Expected Shortfall is the mean of the tail, not a percentile

```
VaR_95 = the loss threshold the worst 5% of days begin at
ES_95  = mean(loss | loss >= VaR_95)
```

Computing a percentile and calling it Expected Shortfall understates tail risk
by construction. VaR says *where the tail starts*; ES says *how bad it gets
once you are in it*. The gap between them is exactly the information VaR
discards, and a portfolio can sit comfortably inside a VaR limit while its
actual tail losses are ruinous.

Worked example — 95 small gains and losses of 2%, 3%, 4%, 5%, 10%:

```
VaR_95 = 0.02      (the threshold)
ES_95  = 0.048     (mean of 0.02, 0.03, 0.04, 0.05, 0.10)
```

`ES >= VaR` holds by construction, and is asserted across 300 random windows
in `tests/test_risk_math.py`.

### 2. A structural stop is respected, or the trade is skipped

```
raw_stop      = max(atr_distance, structure_distance, min_pct × entry)
stop_distance = min(raw_stop, max_pct × entry)

if structure_distance > max_pct × entry:
    REJECT THE TRADE
```

The tempting bug is to clamp the stop to the maximum and take the trade
anyway. It *looks* conservative — loss per share is capped — while actually
destroying the edge: a stop placed inside the structural level gets taken out
by ordinary noise before the thesis can be right or wrong. The result is a
strategy that bleeds small losses and never realises the wins its backtest
promised.

When the market's structure needs more room than the risk budget allows, the
correct answer is **not to take the trade**.

Note the deliberate asymmetry: an ATR distance wider than the maximum is
*clamped*, not rejected. ATR is a volatility estimate, not a level the market
must respect, so capping it costs nothing structural.

---

## Design decisions worth knowing

Several places in the specification were ambiguous or, taken literally, would
have produced behaviour contrary to its own stated intent. Each is resolved in
code with the reasoning recorded at the decision site.

**Regime is market-wide, not per-instrument.** The regime engine is fed the
benchmark, not the candidate. Feeding it a single instrument lets one stock's
breakout bar reclassify the whole market as high-volatility.

**Trend-slope normalisation saturates at 0.002/bar, not 0.01.** Read as a
per-bar fractional change, §5.1's 0.01 means a 20-day average rising 1% *every
day* — about 250% a year. Using it as the normalisation divisor pinned the
trend component near neutral, capped the composite score below the 0.70
`TREND_UP` cutoff, and would have silently disabled the entire momentum and
CAN SLIM library. See `regime/engine.py::TREND_SLOPE_REFERENCE`.

**Regimes are concurrent, and any of them can veto.** Classification yields a
primary regime *plus* tags. A market can be `TREND_UP` and `HIGH_VOLATILITY`
at once; collapsing that to one label would discard the risk information. A
veto from any active regime blocks a strategy.

**Non-equity scores are rescaled to 100.** §7.1 allocates 10 points to
fundamentals, "equities only". Taken literally, crypto and FX could score at
most 90, making every regime threshold ~11% stricter for them — a hidden
asset-class bias. The remaining components are rescaled instead.

**Top-N concentration is measured against equity.** Against *exposure* the
metric is degenerate: with three positions, the top three are always 100% of
exposure. Against equity, "top 3 < 40%" is a real constraint, and consistent
with `max_single_position`.

**Monte Carlo bootstraps rather than shuffles.** Reordering trades leaves the
P&L sum invariant, so "% of simulations profitable" is always 0% or 100% and
tests nothing. Bootstrap resampling separates a real edge (99.9% positive)
from none (7.5%). Shuffling is kept for sequence-risk analysis only.

**Uncertainty always reduces risk.** Unknown volatility resolves to the most
conservative band. A missing regime input lowers confidence, which halves
position size. A feature that failed to compute fails its condition rather
than passing by default.

---

## Safety properties

- **No data, no trade.** A series failing validation removes the decision
  rather than degrading it. The provider chain HALTs when exhausted; it never
  fabricates a price.
- **Protection before position.** A fill that cannot get a stop attached is
  *flattened immediately*, not left open with an error logged.
- **Circuit breakers latch.** A daily loss limit stays tripped for the session
  even if the market recovers. A breaker that un-trips on a bounce is a
  suggestion, not a breaker.
- **The kill switch needs a human.** It engages automatically and can only be
  released with an operator name and a reason, both journalled.
- **The broker is authoritative.** Reconciliation disagreements are halt
  conditions; an unknown broker position triggers the kill switch.
- **Governance is directional.** The AI layer can tighten any limit and raise
  none. Rejected overrides are recorded, not silently dropped.
- **Live-on-synthetic is refused.** A live broker paired with a synthetic feed
  fails the readiness check and blocks pre-market.

---

## What is *not* done

Being explicit, because the gap between "the pipeline runs" and "this is
ready for money" is the whole distance:

1. **No real market data.** Everything has been exercised against a seeded
   synthetic generator. It proves the machinery is correct; it says nothing
   about whether any strategy has an edge.
2. **No validated strategy.** No strategy has passed §20.2 acceptance on real
   history. The gates are implemented and enforced — none has been *cleared*.
3. **No live venue adapters.** Alpaca, Binance, Oanda and IBKR are described
   in the MCP catalogue with fallback chains, but only `PaperBroker` is
   implemented. Each live adapter needs its own integration tests.
4. **No MCP transport.** The registry enforces availability, auth, retry and
   fallback; the handlers themselves are yours to register.
5. **Fundamentals must be supplied.** EPS/RS ratings and accumulation data
   cannot be derived from price. CAN SLIM strategies fail closed without them.
6. **Short selling is long-only in practice.** The engines handle `Side.SHORT`
   throughout, but every strategy in the library is a long spec.

Against the §23.1 deployment pipeline, this repository sits at the boundary of
**Research → Backtest**. Seven gates remain before live capital.

---

## Suggested next steps

1. Wire a real data provider (Polygon or Alpha Vantage) behind
   `MarketDataProvider` and re-run the backtest on 5+ years of real history,
   including delisted symbols.
2. Run `validate_backtest` → `validate_out_of_sample` → `walk_forward` →
   `monte_carlo` on the results. Expect most strategies to fail; that is the
   gates working.
3. For anything that survives, run 4–8 weeks of paper trading and compare
   realised slippage against the paper broker's model.
4. Only then consider a live adapter, at minimum size, under
   `DeploymentGate`.

---

## Philosophy

> Cut losses. Protect capital. Let genuine edges compound.
>
> A profitable backtest can still be wrong. A correct signal can still lose
> money. A strong trade can still be a bad portfolio decision.
>
> Risk controls matter more than prediction accuracy.
