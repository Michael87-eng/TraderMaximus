"""Market data providers and the priority chain (spec section 3.4).

The chain is ``primary -> secondary -> tertiary -> cache -> HALT``. Nothing in
this module invents a price: if every provider is exhausted the chain raises
:class:`DataQualityError` and the caller must stop. Fabricating data would be a
direct violation of the final mandate.

Two concrete providers ship here:

* :class:`StaticProvider` — serves pre-loaded history, used by the backtester
  and by tests.
* :class:`SyntheticProvider` — a seeded random-walk generator for paper-mode
  smoke tests. It is explicitly labelled synthetic so it can never be mistaken
  for real market data.

Live providers (Polygon, TradingView, Alpha Vantage, Finnhub) are reached
through the MCP registry; see :mod:`tradermaximus.mcp.registry`.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

from ..core.errors import DataQualityError, ToolUnavailableError
from ..core.types import AssetClass, Candle, Instrument, Quote, Timeframe, utc_now
from ..observability.journal import Journal, NULL_JOURNAL, Severity
from .series import CandleSeries


class MarketDataProvider(ABC):
    """Interface every data source must implement."""

    name: str = "provider"
    is_synthetic: bool = False

    @abstractmethod
    def fetch(self, instrument: Instrument, timeframe: Timeframe, lookback: int) -> CandleSeries:
        """Return the most recent ``lookback`` bars, oldest first."""

    def fetch_quote(self, instrument: Instrument) -> Quote | None:
        """Top-of-book snapshot. ``None`` when the source has no quote feed."""
        return None

    def healthy(self) -> bool:
        """Whether the provider is connected, authenticated and responsive."""
        return True


# ---------------------------------------------------------------------------
# Concrete providers
# ---------------------------------------------------------------------------


@dataclass
class StaticProvider(MarketDataProvider):
    """Serves pre-loaded history. The backtester's data source."""

    data: dict[tuple[str, str], list[Candle]] = field(default_factory=dict)
    name: str = "static"
    quotes: dict[str, Quote] = field(default_factory=dict)
    _healthy: bool = True

    def load(self, instrument: Instrument, timeframe: Timeframe, candles: Sequence[Candle]) -> None:
        self.data[(instrument.symbol, timeframe.value)] = sorted(candles, key=lambda c: c.timestamp)

    def fetch(self, instrument: Instrument, timeframe: Timeframe, lookback: int) -> CandleSeries:
        key = (instrument.symbol, timeframe.value)
        if key not in self.data:
            raise ToolUnavailableError(f"{self.name}: no data loaded for {key}")
        candles = self.data[key]
        return CandleSeries.build(instrument, timeframe, candles[-lookback:] if lookback > 0 else candles)

    def fetch_quote(self, instrument: Instrument) -> Quote | None:
        return self.quotes.get(instrument.symbol)

    def healthy(self) -> bool:
        return self._healthy


class SyntheticProvider(MarketDataProvider):
    """Seeded geometric random walk for paper-mode smoke tests.

    Deterministic for a given (symbol, seed) so that runs are reproducible.
    ``is_synthetic`` is True, and the orchestrator refuses to route live orders
    off a synthetic feed.
    """

    name = "synthetic"
    is_synthetic = True

    def __init__(
        self,
        *,
        seed: int = 42,
        start_price: float = 100.0,
        annual_drift: float = 0.08,
        annual_volatility: float = 0.28,
        base_volume: float = 1_500_000.0,
    ) -> None:
        self.seed = seed
        self.start_price = start_price
        self.annual_drift = annual_drift
        self.annual_volatility = annual_volatility
        self.base_volume = base_volume

    def fetch(self, instrument: Instrument, timeframe: Timeframe, lookback: int) -> CandleSeries:
        return CandleSeries.build(
            instrument,
            timeframe,
            self.generate(instrument, timeframe, lookback),
        )

    def generate(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        count: int,
        *,
        end: datetime | None = None,
    ) -> list[Candle]:
        rng = random.Random(f"{instrument.symbol}:{self.seed}")
        end = end or utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
        step = timedelta(seconds=timeframe.seconds)

        periods_per_year = 31_536_000 / timeframe.seconds
        drift = self.annual_drift / periods_per_year
        vol = self.annual_volatility / (periods_per_year ** 0.5)

        price = self.start_price
        candles: list[Candle] = []
        for index in range(count):
            timestamp = end - step * (count - 1 - index)
            shock = rng.gauss(drift, vol)
            open_price = price
            close_price = max(0.01, open_price * (1.0 + shock))
            wick = abs(rng.gauss(0.0, vol)) * open_price
            high = max(open_price, close_price) + wick
            low = max(0.01, min(open_price, close_price) - wick)
            volume = max(1.0, rng.gauss(self.base_volume, self.base_volume * 0.25))
            candles.append(
                Candle(
                    timestamp=timestamp,
                    open=round(open_price, 4),
                    high=round(high, 4),
                    low=round(low, 4),
                    close=round(close_price, 4),
                    volume=round(volume, 2),
                )
            )
            price = close_price
        return candles

    def fetch_quote(self, instrument: Instrument) -> Quote:
        series = self.fetch(instrument, Timeframe.D1, 2)
        last = series.last.close
        half_spread = last * 0.0002
        return Quote(timestamp=utc_now(), bid=round(last - half_spread, 4), ask=round(last + half_spread, 4))


# ---------------------------------------------------------------------------
# Priority chain
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchResult:
    """A series plus the provenance of where it came from."""

    series: CandleSeries
    provider: str
    from_cache: bool = False
    degraded: bool = False
    notes: tuple[str, ...] = ()


class ProviderChain:
    """Ordered provider list with cache fallback and a hard HALT terminus.

    Args:
        providers: in priority order, primary first.
        journal: audit sink for fallbacks and disagreements.
        fail_mode: ``"HALT"`` raises when exhausted (the spec default);
            ``"DEGRADE"`` allows returning stale cache, flagged as degraded.
        disagreement_tolerance: relative gap between primary and secondary last
            close that triggers a logged discrepancy (section 3.4).
    """

    def __init__(
        self,
        providers: Sequence[MarketDataProvider],
        *,
        journal: Journal | None = None,
        fail_mode: str = "HALT",
        disagreement_tolerance: float = 0.005,
    ) -> None:
        if not providers:
            raise ValueError("ProviderChain requires at least one provider")
        self.providers = list(providers)
        self.journal = journal if journal is not None else NULL_JOURNAL
        self.fail_mode = fail_mode.upper()
        self.disagreement_tolerance = disagreement_tolerance
        self._cache: dict[tuple[str, str], CandleSeries] = {}

    @property
    def primary(self) -> MarketDataProvider:
        return self.providers[0]

    def fetch(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        lookback: int,
        *,
        cross_check: bool = True,
    ) -> FetchResult:
        """Walk the chain until a provider returns data, else fall back or halt."""
        key = (instrument.symbol, timeframe.value)
        errors: list[str] = []

        for index, provider in enumerate(self.providers):
            if not provider.healthy():
                errors.append(f"{provider.name}: unhealthy")
                continue
            try:
                series = provider.fetch(instrument, timeframe, lookback)
            except Exception as exc:  # noqa: BLE001 - any provider failure falls through
                errors.append(f"{provider.name}: {exc}")
                self.journal.record(
                    "data",
                    f"provider {provider.name} failed for {instrument.symbol}: {exc}",
                    severity=Severity.WARNING,
                    provider=provider.name,
                    symbol=instrument.symbol,
                )
                continue

            if len(series) == 0:
                errors.append(f"{provider.name}: empty series")
                continue

            self._cache[key] = series
            notes: list[str] = []
            if index > 0:
                notes.append(f"fell back to {provider.name} after {index} provider failure(s)")
                self.journal.record(
                    "data",
                    f"{instrument.symbol}: using fallback provider {provider.name}",
                    severity=Severity.WARNING,
                    failures=errors,
                )
            if cross_check and index == 0 and len(self.providers) > 1:
                notes.extend(self._cross_check(instrument, timeframe, series))

            return FetchResult(series=series, provider=provider.name, notes=tuple(notes))

        # Every provider failed: cache, then halt.
        cached = self._cache.get(key)
        if cached is not None and self.fail_mode != "HALT":
            self.journal.record(
                "data",
                f"{instrument.symbol}: serving cached data, all providers failed",
                severity=Severity.ERROR,
                failures=errors,
            )
            return FetchResult(series=cached, provider="cache", from_cache=True, degraded=True, notes=tuple(errors))

        self.journal.record(
            "data",
            f"{instrument.symbol}: all providers exhausted, halting",
            severity=Severity.CRITICAL,
            failures=errors,
        )
        raise DataQualityError(
            f"No usable data for {instrument.symbol} {timeframe.value}: " + "; ".join(errors)
        )

    def _cross_check(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        primary_series: CandleSeries,
    ) -> list[str]:
        """Compare the primary's last close against the secondary's.

        Per section 3.4 the primary always wins; the point of this check is to
        leave a record when the sources drift apart.
        """
        secondary = self.providers[1]
        if not secondary.healthy():
            return []
        try:
            other = secondary.fetch(instrument, timeframe, 2)
        except Exception:  # noqa: BLE001 - cross-check is advisory only
            return []
        if len(other) == 0:
            return []

        primary_price = primary_series.last.close
        secondary_price = other.last.close
        if primary_price <= 0:
            return []
        deviation = abs(primary_price - secondary_price) / primary_price
        if deviation > self.disagreement_tolerance:
            note = (
                f"provider disagreement {deviation:.2%}: "
                f"{self.primary.name}={primary_price} {secondary.name}={secondary_price}"
            )
            self.journal.record(
                "data",
                f"{instrument.symbol}: {note}",
                severity=Severity.WARNING,
                deviation=deviation,
            )
            return [note]
        return []

    def fetch_quote(self, instrument: Instrument) -> Quote | None:
        for provider in self.providers:
            if not provider.healthy():
                continue
            try:
                quote = provider.fetch_quote(instrument)
            except Exception:  # noqa: BLE001
                continue
            if quote is not None:
                return quote
        return None

    @property
    def uses_synthetic_data(self) -> bool:
        """True when any healthy provider in the chain fabricates prices."""
        return any(p.is_synthetic for p in self.providers if p.healthy())


DEFAULT_INSTRUMENTS: Mapping[str, Instrument] = {
    "AAPL": Instrument("AAPL", AssetClass.EQUITIES, "NASDAQ", "Technology", 0.01, 1.0, 1.0),
    "MSFT": Instrument("MSFT", AssetClass.EQUITIES, "NASDAQ", "Technology", 0.01, 1.0, 1.0),
    "NVDA": Instrument("NVDA", AssetClass.EQUITIES, "NASDAQ", "Semiconductors", 0.01, 1.0, 1.0),
    "XLE": Instrument("XLE", AssetClass.EQUITIES, "NYSE", "Energy", 0.01, 1.0, 1.0),
    "BTCUSD": Instrument("BTCUSD", AssetClass.CRYPTO, "BINANCE", "Crypto", 0.01, 0.0001, 0.0001),
    "ETHUSD": Instrument("ETHUSD", AssetClass.CRYPTO, "BINANCE", "Crypto", 0.01, 0.001, 0.001),
    "EURUSD": Instrument("EURUSD", AssetClass.FOREX, "OANDA", "FX", 0.00001, 1000.0, 1000.0),
    "GBPUSD": Instrument("GBPUSD", AssetClass.FOREX, "OANDA", "FX", 0.00001, 1000.0, 1000.0),
}
