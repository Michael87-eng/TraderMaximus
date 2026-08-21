# Architecture

How the pieces fit, and why the seams are where they are.

## The central invariant

Every layer can only **remove** trades. None can add one.

```
universe  ──►  data quality  ──►  regime  ──►  strategy  ──►  scoring  ──►  risk  ──►  execution
   N              ≤ N              ≤ N          ≤ N           ≤ N          ≤ N          ≤ N
```

This is enforced structurally, not by convention:

- `ExecutionEngine.execute` requires a `RiskVerdict` with `approved=True` and
  returns immediately otherwise.
- `RiskEngine.authorize` is the only producer of an approving verdict.
- `StrategyEngine.select` returns at most one signal per instrument per cycle.
- Regime eligibility is checked *before* strategy conditions are evaluated, so
  an ineligible setup is never scored and cannot be argued for.

## Module dependency order

Dependencies flow one way. Nothing in `core` imports anything above it.

```
core (types, config, errors)
  └── observability (journal, metrics)
        └── data (series, quality, providers)
              └── indicators (core, structure, patterns, engine)
                    └── regime
                          └── strategy (spec, library, scoring, engine)
                                └── portfolio (book, reconciliation)
                                      └── risk (limits, stops, sizing,
                                                portfolio, engine, ...)
                                            └── execution (states, safety,
                                                           brokers, engine)
                                                  └── research (backtest,
                                                                validation, drift)
                                                        └── orchestrator ──► cli
```

`risk/engine.py` imports `core.types.Position` lazily inside `_project` to
avoid a cycle with `portfolio.book`.

## Key seams

### Data → decision

`CandleSeries.slice_until(timestamp)` returns a series truncated to bars that
had closed by that moment. The backtester passes the truncated object, so a
decision *cannot* read a future bar — future data is absent, not merely
ignored. This is what makes look-ahead bias structurally impossible rather
than a discipline someone has to maintain.

### Strategy → risk

A `Signal` carries no authorisation and no size. It carries the inputs risk
needs — entry price, ATR, structural level, score, regime — and nothing else.
A strategy cannot express an opinion about position size.

### Risk → execution

A `RiskVerdict` carries the full `StopDecision` and `SizingDecision`, so
execution never recomputes either. When the fill price differs from the signal
price (it usually does), the backtester re-derives the stop *distance* from
the actual fill, keeping realised risk equal to authorised risk.

### Internal → broker

`PositionBook` is what the system believes; `BrokerAdapter.positions()` is what
is true. `ReconciliationEngine` compares them, and the broker always wins. A
critical discrepancy engages the kill switch.

## Ordering decisions that matter

**Risk checks run system-halts first.** Kill switch, then breakers, then data
quality, then account-level losses, then execution conditions, then signal
quality, then sizing, then the post-trade portfolio projection. Two reasons:
cheap checks first, and — more importantly — when several conditions are
simultaneously true, the *recorded reason* should be the one that actually
stopped the trade. "Kill switch engaged" is the truth; "spread too wide" is a
distraction.

**Portfolio checks run against a projection.** `RiskEngine._project` builds the
snapshot as it *would* look with the candidate added. Checking current state
instead lets the portfolio be walked past a limit one compliant trade at a
time.

**Protection is attached before the position is considered established.** The
state machine refuses `FILLED → MONITORED`; the only legal path is through
`PROTECTIVE_ORDER_ATTACHED`. If the stop cannot be attached, the position is
flattened.

## Performance

The engines are pure standard library. Two optimisations keep that viable:

1. `statistics.pstdev` converts inputs to `Fraction` for exactness and once
   accounted for ~85% of indicator runtime. `indicators/core._pstdev` uses
   float arithmetic (identical to 1e-12) and `volatility_series` computes log
   returns once rather than per window. A feature snapshot on 900 bars went
   from **124ms to 19ms**.
2. The backtester passes a bounded 400-bar window to each decision, making
   runtime linear in bar count rather than quadratic. 400 bars exceeds the
   slowest warm-up (MA200 plus the 250-bar volatility lookback); older bars
   change no value, which `test_bounded_window_matches_full_history` asserts.

A 640-bar backtest over four symbols runs in ~20 seconds.

## Extension points

**A new data provider**: subclass `MarketDataProvider`, implement `fetch`, add
it to a `ProviderChain`. Set `is_synthetic = True` if it generates prices, so
the live-mode guard can refuse it.

**A new venue**: subclass `BrokerAdapter`. Never invent a fill — raise
`ExecutionError` when the venue is unreachable so the retry and recovery paths
engage.

**A new strategy**: build a `StrategySpec` from `Condition` predicates. Give
it a `strategy_type` that appears in the regime eligibility matrix, or it will
be blocked in every regime. Fail closed on missing inputs.

**A new MCP tool**: add a `ToolSpec` to `mcp/registry.CATALOGUE` with its
fallback chain, then `registry.register(name, handler)`.

## Testing strategy

179 tests, no third-party runner required (`unittest`, `pytest`-compatible).

- `test_risk_math.py` — the two V4 corrections, pinned to the spec's worked
  examples, plus invariant sweeps (`ES >= VaR` over 300 windows; the stop never
  placed inside structure over 120 structural levels).
- `test_indicators.py` — known values, alignment, warm-up, determinism.
- `test_risk_engine.py` — every rejection rule, breaker latching, governance.
- `test_data_and_regime.py` — quality checks, provider fallback, regime vetoes.
- `test_execution.py` — lifecycle legality, fill model, failure recovery,
  reconciliation.
- `test_strategy_and_research.py` — library integrity, scoring, backtest,
  validation gates, drift escalation.
- `test_orchestrator.py` — readiness gating, workflow, event postures.

Several tests are explicit regression guards for bugs found during
construction and are labelled as such in their docstrings.
