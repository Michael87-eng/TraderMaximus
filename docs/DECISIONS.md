# Interpretation decisions

Where the specification was ambiguous, silent, or would — read literally —
have produced behaviour contrary to its own stated intent. Each entry records
what the spec says, what was implemented, and why.

These are judgement calls. They are collected here so they can be reviewed and
overruled, rather than buried in the code.

---

## 1. Trend-slope normalisation

**Spec** (§5.1): `trend_slope: "20-period MA slope normalized to [-1,1]"` with
`trend_slope_threshold: 0.01`. The unit of the slope is not stated.

**Implemented**: normalisation saturates at `TREND_SLOPE_REFERENCE = 0.002`
per bar. The spec's 0.01 is retained as the "is it trending at all" threshold.

**Why**: read as a per-bar fractional change on daily bars, 0.01 means the
20-day average rising 1% *every single day* — roughly 250% annualised. Using
it as the normalisation divisor pins the trend component near its neutral
midpoint (0.5) for every realistic market, which caps the composite score
below the 0.70 `TREND_UP` cutoff. Since `TREND_UP` gates the momentum,
breakout, pullback and CAN SLIM families, the literal reading silently
disables most of the strategy library. Measured: with 0.01, a realistic
trending benchmark scored 0.695 and classified as `RANGE`; with 0.002 the same
data scores 0.780 and classifies `TREND_UP`.

**Risk if wrong**: the system trades more often than intended. Mitigated by
every downstream gate remaining unchanged.

---

## 2. Correlation change is inverted

**Spec** (§5.1): weights `correlation_change` at 0.05 and adds
`correlation_change_norm × 0.05` to the score, without stating direction.

**Implemented**: `corr_norm = 1 - clamp(correlation_change)`, so *rising*
cross-asset correlation lowers the score.

**Why**: rising correlation means diversification is failing — a risk-off
condition. Adding it positively would raise the regime score exactly when the
market is becoming more dangerous.

---

## 3. Regime is a set, not a label

**Spec** (§5.2): lists thirteen regimes as if classification returns one.

**Implemented**: a *primary* regime plus concurrent *tags*. Strategy
eligibility is checked against all of them; a veto from any active regime
blocks the strategy.

**Why**: a market can genuinely be `TREND_UP` and `HIGH_VOLATILITY` at once.
Collapsing to one label discards whichever half was not chosen — and if the
discarded half is the volatility reading, the system takes breakouts into
exactly the conditions §5.4 forbids them in.

---

## 4. Non-equity signal scores are rescaled

**Spec** (§7.1): allocates 10 of 100 points to fundamentals, marked "equities
only". §7.2 gives regime thresholds as absolute scores.

**Implemented**: when fundamentals do not apply, the remaining eight
components are rescaled so the maximum stays 100.

**Why**: taken literally, a crypto or FX signal could score at most 90,
silently making every threshold ~11% stricter for those asset classes. That is
a hidden bias, not a stated policy. If a stricter bar for non-equities *is*
wanted, it belongs in §7.2 as an explicit threshold.

---

## 5. Top-N concentration is measured against equity

**Spec**: §13.1 defines `concentration_risk` as "Top 3 positions as % of total
exposure"; §13.3 limits `TOP_3_POSITIONS < 0.40`.

**Implemented**: measured against equity.

**Why**: against exposure the metric is degenerate — with three positions the
top three are always 100% of exposure, so the limit fires on every
three-position book regardless of size. Against equity the limit is meaningful
and consistent with `SINGLE_POSITION < 0.15`, which is unambiguously an equity
fraction.

---

## 6. Diversification ratio is normalised

**Spec** (§13.1): "diversification_ratio: 1 / Herfindahl-Hirschman Index",
limited to `> 0.30` in §13.3.

**Implemented**: `effective_positions = 1 / HHI` is reported, and
`diversification_ratio = effective_positions / position_count`, which lies in
(0, 1].

**Why**: `1 / HHI` is at least 1.0 for any portfolio, so a `> 0.30` threshold
could never bind. The normalised form makes 1.0 mean "perfectly equal weights"
and the threshold meaningful. The check is skipped below three positions,
where concentration is not a real choice.

---

## 7. Monte Carlo bootstraps rather than shuffles

**Spec** (§20.1): "1000+ simulations with randomized trade sequences";
§20.2 requires "95% of simulations show positive expectancy".

**Implemented**: bootstrap resampling with replacement by default. Shuffling
is available via `method="shuffle"` for sequence-risk analysis.

**Why**: reordering a fixed set of trades leaves the P&L sum invariant —
addition commutes. The "% profitable" statistic under shuffling is therefore
always exactly 0% or 100% and carries no information, making the §20.2 gate
untestable. Bootstrap varies composition as well as order: measured, it
separates a genuine edge (99.9% positive) from none (7.5%).

---

## 8. ATR-wide stops are clamped; structural ones reject

**Spec** (§10.1): `stop_distance = min(raw, max_pct)`, and "IF
structure_distance > max_pct × entry_price ... REJECT THE TRADE".

**Implemented**: exactly that, with the asymmetry made explicit — an ATR
distance exceeding the maximum is clamped, only a *structural* distance
rejects.

**Why**: the spec states the rejection clause only for structure, and the
distinction is sound. ATR is a volatility estimate, not a level the market
must respect; capping it forfeits nothing structural. A structural level is a
price where participants have actually transacted, and placing a stop inside
it invites noise stop-outs.

A relative tolerance of 1e-9 guards the boundary comparison, so a structural
stop sitting exactly at the maximum is accepted (the spec rejects only when it
*exceeds*) regardless of floating-point representation.

---

## 9. Regime is classified from the benchmark

**Spec** (§5): describes a "MARKET REGIME ENGINE" using breadth, new
highs/lows and index MA slope, but the pipeline diagram feeds it per-instrument
features.

**Implemented**: the orchestrator and backtester classify from the benchmark
series; the candidate instrument's features are used only for strategy
conditions.

**Why**: regime is a property of the market. Feeding a single instrument lets
one stock's breakout bar spike `volatility_percentile` to 1.0 and reclassify
the entire market as `HIGH_VOLATILITY`, blocking every other candidate.

---

## 10. MCP requirements apply only when MCP is the data path

**Spec** (§31.1): readiness requires "All MCP servers operational".

**Implemented**: `missing_required()` is enforced only when the registry has
at least one registration. A deployment wired directly to a `ProviderChain`
and a broker adapter is not using MCP.

**Why**: paper mode and backtests use direct providers. Demanding an unused
MCP registration would report a healthy system as unready and train operators
to ignore the readiness check — the worst possible outcome for a safety gate.

---

## 11. Unknown inputs are treated as adverse

Not a spec ambiguity so much as a policy applied consistently:

- unknown volatility → the *most conservative* risk band, not the most generous
- a missing regime input → lower confidence → halved position size
- a feature that failed to compute → its condition **fails**, never passes
- unknown data age → the freshness check fails
- unknown sector strength → neutral, because it is one supporting factor and
  failing on it would block every trade without sector data

The one exception is deliberate: a *supporting* factor that is merely absent
should not veto a trade the way a *safety* input does. The scoring engine
already withholds the points.

**Why**: §30 requires reducing risk when uncertainty rises. Any default that
resolves an unknown in the permissive direction inverts that.
