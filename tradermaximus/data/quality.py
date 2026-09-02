"""Data quality engine (spec section 3).

The rule the rest of the system depends on: **no data, no trade**. A series
that fails validation does not degrade the decision, it removes it. Every
check returns a structured issue so the journal records *which* rule failed
rather than a generic "bad data".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Mapping, Sequence

from ..core.types import AssetClass, Candle, Quote, utc_now
from .series import CandleSeries


class QualityIssue(str, Enum):
    """Machine-readable failure reasons (section 3.2)."""

    EMPTY_SERIES = "empty_series"
    INSUFFICIENT_HISTORY = "insufficient_history"
    STALE_DATA = "stale_data"
    MISSING_CANDLES = "missing_candles"
    DUPLICATE_TIMESTAMPS = "duplicate_timestamps"
    NON_MONOTONIC_TIMESTAMPS = "non_monotonic_timestamps"
    NAIVE_TIMESTAMP = "naive_timestamp"
    INVALID_OHLC = "invalid_ohlc"
    NEGATIVE_VOLUME = "negative_volume"
    ZERO_VOLUME_RUN = "zero_volume_run"
    PRICE_OUTLIER = "price_outlier"
    WIDE_SPREAD = "wide_spread"
    INSUFFICIENT_LIQUIDITY = "insufficient_liquidity"
    UNKNOWN_SYMBOL = "unknown_symbol"
    INACTIVE_SYMBOL = "inactive_symbol"
    UNADJUSTED_CORPORATE_ACTION = "unadjusted_corporate_action"
    PROVIDER_DISAGREEMENT = "provider_disagreement"


#: Weight each issue removes from the 0-1 quality score. Structural problems
#: (bad OHLC, stale data) are near-fatal; cosmetic ones merely erode the score.
_ISSUE_WEIGHTS: Mapping[QualityIssue, float] = {
    QualityIssue.EMPTY_SERIES: 1.00,
    QualityIssue.INSUFFICIENT_HISTORY: 1.00,
    QualityIssue.STALE_DATA: 0.60,
    QualityIssue.MISSING_CANDLES: 0.25,
    QualityIssue.DUPLICATE_TIMESTAMPS: 0.40,
    QualityIssue.NON_MONOTONIC_TIMESTAMPS: 0.40,
    QualityIssue.NAIVE_TIMESTAMP: 1.00,
    QualityIssue.INVALID_OHLC: 0.80,
    QualityIssue.NEGATIVE_VOLUME: 0.50,
    QualityIssue.ZERO_VOLUME_RUN: 0.20,
    QualityIssue.PRICE_OUTLIER: 0.35,
    QualityIssue.WIDE_SPREAD: 0.30,
    QualityIssue.INSUFFICIENT_LIQUIDITY: 0.50,
    QualityIssue.UNKNOWN_SYMBOL: 1.00,
    QualityIssue.INACTIVE_SYMBOL: 1.00,
    QualityIssue.UNADJUSTED_CORPORATE_ACTION: 0.45,
    QualityIssue.PROVIDER_DISAGREEMENT: 0.30,
}

#: Minimum average daily volume by asset class (section 3.1).
MIN_LIQUIDITY: Mapping[AssetClass, float] = {
    AssetClass.EQUITIES: 100_000.0,
    AssetClass.CRYPTO: 100_000.0,
    AssetClass.FOREX: 1_000_000.0,
}


@dataclass(frozen=True, slots=True)
class QualityFinding:
    issue: QualityIssue
    detail: str

    def __str__(self) -> str:
        return f"{self.issue.value}: {self.detail}"


@dataclass(frozen=True, slots=True)
class QualityReport:
    """Outcome of validating one series.

    ``passed`` is the only field the pipeline needs; the rest exists so a human
    reading the journal can tell what went wrong without re-running anything.
    """

    symbol: str
    score: float
    findings: tuple[QualityFinding, ...] = ()
    checked_at: datetime = field(default_factory=utc_now)
    threshold: float = 0.85

    @property
    def passed(self) -> bool:
        return self.score >= self.threshold and not self.has_fatal

    @property
    def has_fatal(self) -> bool:
        return any(_ISSUE_WEIGHTS.get(f.issue, 0.0) >= 1.0 for f in self.findings)

    @property
    def issues(self) -> tuple[str, ...]:
        return tuple(str(f) for f in self.findings)

    def reason(self) -> str:
        if self.passed:
            return "data quality acceptable"
        if not self.findings:
            return f"quality score {self.score:.3f} below threshold {self.threshold:.2f}"
        return "; ".join(self.issues)


class DataQualityEngine:
    """Validates series and quotes before anything downstream may use them."""

    def __init__(
        self,
        *,
        threshold: float = 0.85,
        max_staleness_seconds: float = 5.0,
        max_spread_bps: float = 50.0,
        max_gap_ratio: float = 0.05,
        outlier_sigma: float = 12.0,
        corporate_action_jump: float = 0.35,
    ) -> None:
        self.threshold = threshold
        self.max_staleness_seconds = max_staleness_seconds
        self.max_spread_bps = max_spread_bps
        self.max_gap_ratio = max_gap_ratio
        self.outlier_sigma = outlier_sigma
        self.corporate_action_jump = corporate_action_jump

    # -- main entry point ---------------------------------------------------

    def validate_series(
        self,
        series: CandleSeries,
        *,
        min_history: int = 50,
        now: datetime | None = None,
        enforce_staleness: bool = True,
        prices_adjusted: bool = False,
    ) -> QualityReport:
        """Run every section 3.2 check and produce a scored report.

        Args:
            min_history: bars the downstream strategy needs; fewer is fatal.
            now: injected clock, so backtests validate against bar time rather
                than wall-clock time.
            enforce_staleness: disabled during backtests where every bar is old.
            prices_adjusted: the feed already removed splits and dividends, so
                the corporate-action heuristic is skipped. Without this, a real
                50% single-session crash (AAPL on 2000-09-29, for one) is
                indistinguishable by ratio from a 2:1 split and the whole
                series gets thrown away.
        """
        findings: list[QualityFinding] = []
        instrument = series.instrument

        if not instrument.active:
            findings.append(QualityFinding(QualityIssue.INACTIVE_SYMBOL, f"{instrument.symbol} is not active"))
        if not instrument.symbol or not instrument.symbol.strip():
            findings.append(QualityFinding(QualityIssue.UNKNOWN_SYMBOL, "empty symbol"))

        if len(series) == 0:
            findings.append(QualityFinding(QualityIssue.EMPTY_SERIES, "series contains no candles"))
            return self._report(series.symbol, findings)

        if len(series) < min_history:
            findings.append(
                QualityFinding(
                    QualityIssue.INSUFFICIENT_HISTORY,
                    f"{len(series)} bars available, {min_history} required",
                )
            )

        findings.extend(self._check_timestamps(series))
        findings.extend(self._check_integrity(series))
        findings.extend(self._check_outliers(series, prices_adjusted=prices_adjusted))
        findings.extend(self._check_liquidity(series))
        if enforce_staleness:
            findings.extend(self._check_freshness(series, now or utc_now()))

        return self._report(series.symbol, findings)

    # -- individual checks --------------------------------------------------

    def _check_timestamps(self, series: CandleSeries) -> list[QualityFinding]:
        findings: list[QualityFinding] = []
        stamps = series.timestamps

        if any(ts.tzinfo is None for ts in stamps):
            findings.append(QualityFinding(QualityIssue.NAIVE_TIMESTAMP, "timestamps must be tz-aware UTC"))

        duplicates = len(stamps) - len(set(stamps))
        if duplicates:
            findings.append(
                QualityFinding(QualityIssue.DUPLICATE_TIMESTAMPS, f"{duplicates} duplicate bar timestamps")
            )

        if any(b <= a for a, b in zip(stamps, stamps[1:])):
            findings.append(
                QualityFinding(QualityIssue.NON_MONOTONIC_TIMESTAMPS, "bar timestamps are not strictly increasing")
            )

        # Gap detection uses the modal spacing rather than the nominal timeframe
        # so that weekends and exchange holidays do not read as missing data.
        gaps = self._count_gaps(stamps)
        if gaps and len(stamps) > 1:
            ratio = gaps / (len(stamps) - 1)
            if ratio > self.max_gap_ratio:
                findings.append(
                    QualityFinding(
                        QualityIssue.MISSING_CANDLES,
                        f"{gaps} suspected missing bars ({ratio:.1%} of series)",
                    )
                )
        return findings

    @staticmethod
    def _count_gaps(stamps: Sequence[datetime]) -> int:
        if len(stamps) < 3:
            return 0
        deltas = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:]) if (b - a).total_seconds() > 0]
        if not deltas:
            return 0
        ordered = sorted(deltas)
        modal = ordered[len(ordered) // 2]  # median spacing
        if modal <= 0:
            return 0
        gaps = 0
        for delta in deltas:
            # Allow up to a 4x spacing before calling it a gap: that absorbs
            # weekends on daily bars without hiding a genuinely dropped week.
            if delta > modal * 4:
                gaps += int(round(delta / modal)) - 1
        return gaps

    @staticmethod
    def _volume_is_reported(series: CandleSeries) -> bool:
        """Whether this feed reports volume at all.

        Spot FOREX is decentralised: there is no consolidated tape, and most FX
        feeds (Oanda among them) ship no volume column. An all-zero volume
        column therefore means "not reported", not "nothing traded" — and
        treating it as zero liquidity rejects every FX series outright, which
        silently made the entire asset class unvalidatable.

        Only FOREX gets this reading. An equity or crypto feed with no volume
        is genuinely defective, because both have consolidated volume.
        """
        if series.instrument.asset_class is not AssetClass.FOREX:
            return True
        return any(candle.volume > 0 for candle in series)

    def _check_integrity(self, series: CandleSeries) -> list[QualityFinding]:
        findings: list[QualityFinding] = []
        invalid = [c for c in series if not c.is_valid()]
        if invalid:
            first = invalid[0]
            findings.append(
                QualityFinding(
                    QualityIssue.INVALID_OHLC,
                    f"{len(invalid)} malformed bars, first at {first.timestamp.isoformat()}",
                )
            )
        if any(c.volume < 0 for c in series):
            findings.append(QualityFinding(QualityIssue.NEGATIVE_VOLUME, "negative volume present"))

        if self._volume_is_reported(series):
            zero_run = _longest_zero_volume_run(series.volumes)
            if zero_run >= 3:
                findings.append(
                    QualityFinding(QualityIssue.ZERO_VOLUME_RUN, f"{zero_run} consecutive zero-volume bars")
                )
        return findings

    @staticmethod
    def _reverts_immediately(moves: Sequence[float], index: int, tolerance: float = 0.35) -> bool:
        """Whether a large move is undone by the very next bar.

        This is what separates a bad tick from real market history. A corrupt
        print spikes and snaps back; a genuine crash persists. Bitcoin fell 40%
        on 2020-03-12 and stayed down — treating that as a data error would
        reject the single most informative stretch of crypto history there is,
        which is precisely the stretch a risk system most needs to be tested on.
        """
        if index + 1 >= len(moves):
            return False
        forward = moves[index + 1]
        # A true revert is equal and opposite: -40% followed by ~+67%.
        implied_recovery = (1.0 / (1.0 + moves[index])) - 1.0 if moves[index] > -1 else 0.0
        if implied_recovery == 0:
            return False
        return abs(forward - implied_recovery) / abs(implied_recovery) < tolerance

    def _check_outliers(
        self, series: CandleSeries, *, prices_adjusted: bool = False
    ) -> list[QualityFinding]:
        """Flag implausible jumps, separating data errors from real moves.

        Three cases, and conflating them is what makes a quality engine reject
        good data:

        * an unadjusted split, recognisable by its ratio;
        * a bad tick, recognisable because the next bar undoes it;
        * a genuine crash, which persists and must be kept.

        The split heuristic applies to equities only. Crypto and FX have no
        corporate actions to be unadjusted for, so on those classes a move that
        happens to land near 1:2 is *always* a real one. Applying it anyway
        rejected SOL-USD outright on its -48.5% bar of 2022-11-08, which is the
        FTX collapse — the single most informative day in that series.
        """
        findings: list[QualityFinding] = []
        closes = series.closes
        if len(closes) < 20:
            return findings

        corporate_actions_possible = (
            series.instrument.asset_class is AssetClass.EQUITIES
        )

        moves = [(b - a) / a for a, b in zip(closes, closes[1:]) if a > 0]
        if not moves:
            return findings

        mean = sum(moves) / len(moves)
        variance = sum((m - mean) ** 2 for m in moves) / len(moves)
        stdev = variance ** 0.5

        for index, move in enumerate(moves):
            if (
                corporate_actions_possible
                and not prices_adjusted
                and abs(move) >= self.corporate_action_jump
                and _looks_like_ratio(move)
            ):
                findings.append(
                    QualityFinding(
                        QualityIssue.UNADJUSTED_CORPORATE_ACTION,
                        f"{move:+.1%} jump at bar {index + 1} resembles an unadjusted split/dividend",
                    )
                )
                break
            if stdev > 0 and abs(move - mean) > self.outlier_sigma * stdev:
                if self._reverts_immediately(moves, index):
                    findings.append(
                        QualityFinding(
                            QualityIssue.PRICE_OUTLIER,
                            f"{move:+.1%} move at bar {index + 1} is undone by the next bar; "
                            "this is a bad tick, not a market move",
                        )
                    )
                    break
                # Persistent: a real move, however extreme. Not a data defect.
                continue
        return findings

    def _check_liquidity(self, series: CandleSeries) -> list[QualityFinding]:
        asset_class = series.instrument.asset_class
        minimum = MIN_LIQUIDITY.get(asset_class, 0.0)
        if minimum <= 0 or len(series) < 5:
            return []
        if not self._volume_is_reported(series):
            # No volume feed to measure. Liquidity must then be established from
            # spread at execution time, which the pre-execution checklist does.
            return []

        avg_volume = series.average_volume(50)
        # Crypto/FX minimums are expressed in notional; equities in shares.
        measure = avg_volume if asset_class is AssetClass.EQUITIES else avg_volume * series.last.close
        if asset_class is AssetClass.FOREX:
            measure = avg_volume
        if measure < minimum:
            return [
                QualityFinding(
                    QualityIssue.INSUFFICIENT_LIQUIDITY,
                    f"average daily volume {measure:,.0f} below {minimum:,.0f} minimum for {asset_class.value}",
                )
            ]
        return []

    def _check_freshness(self, series: CandleSeries, now: datetime) -> list[QualityFinding]:
        last = series.last.timestamp
        if last.tzinfo is None:
            return []
        # A bar is fresh if it closed within one timeframe plus the tolerance.
        allowance = series.timeframe.seconds + self.max_staleness_seconds
        age = (now - last).total_seconds()
        if age > allowance:
            return [
                QualityFinding(
                    QualityIssue.STALE_DATA,
                    f"last bar is {age:,.0f}s old, allowance {allowance:,.0f}s",
                )
            ]
        return []

    # -- quotes -------------------------------------------------------------

    def validate_quote(self, quote: Quote, *, now: datetime | None = None) -> QualityReport:
        """Validate a top-of-book snapshot for execution suitability."""
        findings: list[QualityFinding] = []
        now = now or utc_now()

        if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
            findings.append(
                QualityFinding(QualityIssue.INVALID_OHLC, f"crossed or non-positive quote {quote.bid}/{quote.ask}")
            )
        if quote.spread_bps > self.max_spread_bps:
            findings.append(
                QualityFinding(
                    QualityIssue.WIDE_SPREAD,
                    f"spread {quote.spread_bps:.1f}bps exceeds {self.max_spread_bps:.1f}bps",
                )
            )
        age = (now - quote.timestamp).total_seconds()
        if age > self.max_staleness_seconds:
            findings.append(
                QualityFinding(
                    QualityIssue.STALE_DATA,
                    f"quote is {age:.1f}s old, max {self.max_staleness_seconds:.1f}s",
                )
            )
        return self._report("quote", findings)

    # -- cross-provider agreement ------------------------------------------

    def compare_providers(
        self,
        primary_price: float,
        secondary_price: float,
        *,
        tolerance: float = 0.005,
        symbol: str = "unknown",
    ) -> QualityReport:
        """Section 3.4: when sources disagree, use primary but log the gap."""
        findings: list[QualityFinding] = []
        if primary_price > 0:
            deviation = abs(primary_price - secondary_price) / primary_price
            if deviation > tolerance:
                findings.append(
                    QualityFinding(
                        QualityIssue.PROVIDER_DISAGREEMENT,
                        f"{symbol}: primary {primary_price} vs secondary {secondary_price} "
                        f"({deviation:.2%} > {tolerance:.2%} tolerance)",
                    )
                )
        return self._report(symbol, findings)

    # -- scoring ------------------------------------------------------------

    def _report(self, symbol: str, findings: Sequence[QualityFinding]) -> QualityReport:
        score = 1.0
        for finding in findings:
            score -= _ISSUE_WEIGHTS.get(finding.issue, 0.1)
        score = max(0.0, min(1.0, score))
        return QualityReport(symbol=symbol, score=round(score, 4), findings=tuple(findings), threshold=self.threshold)


def _longest_zero_volume_run(volumes: Sequence[float]) -> int:
    longest = current = 0
    for volume in volumes:
        current = current + 1 if volume == 0 else 0
        longest = max(longest, current)
    return longest


def _looks_like_ratio(move: float) -> bool:
    """True when a move is close to a common split ratio (2:1, 3:1, 3:2, 4:1)."""
    factor = 1.0 + move
    if factor <= 0:
        return False
    for ratio in (2.0, 3.0, 4.0, 1.5, 5.0, 10.0):
        for candidate in (ratio, 1.0 / ratio):
            if abs(factor - candidate) / candidate < 0.03:
                return True
    return False
