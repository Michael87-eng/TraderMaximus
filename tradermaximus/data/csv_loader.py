"""Load real market history from CSV files.

Real validation needs real data, and the data will arrive in whatever shape
its vendor emits. Polygon, Alpaca, Binance, Oanda, Yahoo and Stooq all use
different column names, date formats and orderings — so this loader maps
aliases rather than demanding one canonical layout.

Everything loaded here passes through the same
:class:`~tradermaximus.data.quality.DataQualityEngine` the live system uses.
A vendor file with an unadjusted split, a duplicated bar or a gap is rejected
with a named reason rather than quietly producing a backtest built on bad
prices — which would look exactly like a real result.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from ..core.errors import DataQualityError
from ..core.types import AssetClass, Candle, Instrument, Timeframe
from .quality import DataQualityEngine, QualityReport
from .series import CandleSeries

#: Column aliases seen across common vendors, lower-cased and stripped.
COLUMN_ALIASES: Mapping[str, tuple[str, ...]] = {
    "timestamp": (
        "timestamp", "date", "time", "datetime", "date_time", "t",
        "open_time", "opentime", "bar_start", "period",
    ),
    "open": ("open", "o", "openprice", "open_price", "adj open", "mid_open"),
    "high": ("high", "h", "highprice", "high_price", "adj high", "mid_high"),
    "low": ("low", "l", "lowprice", "low_price", "adj low", "mid_low"),
    "close": ("close", "c", "closeprice", "close_price", "mid_close", "last"),
    "volume": ("volume", "v", "vol", "quantity", "qty", "base_volume", "basevolume"),
    # Preferred when present: already adjusted for splits and dividends.
    "adj_close": ("adj close", "adj_close", "adjclose", "adjusted_close", "adjusted close"),
}

#: Date formats tried in order. ISO-8601 is attempted first via fromisoformat.
DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y%m%d",
    "%d-%b-%Y",
    "%d %b %Y",
)


@dataclass(frozen=True, slots=True)
class LoadResult:
    """A loaded series plus the quality verdict on it."""

    series: CandleSeries
    report: QualityReport
    source: str
    rows_read: int
    rows_skipped: int

    @property
    def usable(self) -> bool:
        return self.report.passed and len(self.series) > 0

    def summary(self) -> str:
        span = ""
        if len(self.series):
            span = (
                f" {self.series[0].timestamp.date()} to {self.series.last.timestamp.date()}"
            )
        return (
            f"{self.series.symbol}: {len(self.series)} bars{span}, "
            f"quality {self.report.score:.2f} "
            f"({'OK' if self.report.passed else self.report.reason()})"
        )


def _normalise(header: str) -> str:
    return header.strip().lower().replace("﻿", "")


def _resolve_columns(fieldnames: Sequence[str]) -> dict[str, str]:
    """Map canonical field names onto this file's actual headers."""
    normalised = {_normalise(name): name for name in fieldnames}
    resolved: dict[str, str] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalised:
                resolved[canonical] = normalised[alias]
                break
    return resolved


def _parse_timestamp(raw: str) -> datetime | None:
    """Parse a vendor timestamp into tz-aware UTC."""
    text = raw.strip()
    if not text:
        return None

    # Epoch seconds or milliseconds (Binance and most exchange APIs).
    if text.isdigit():
        value = int(text)
        if value > 10_000_000_000:      # milliseconds
            value //= 1000
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_number(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = raw.strip().replace(",", "").replace("$", "")
    if not text or text.upper() in {"NA", "N/A", "NULL", "NAN", "-"}:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return None if math.isnan(value) or math.isinf(value) else value


def load_candles(
    path: str | Path,
    *,
    prefer_adjusted: bool = True,
    max_bars: int | None = None,
) -> tuple[list[Candle], int, int]:
    """Parse a CSV into candles. Returns ``(candles, rows_read, rows_skipped)``.

    Args:
        prefer_adjusted: use an adjusted-close column when the file has one.
            Unadjusted equity prices contain artificial gaps at splits, which
            the quality engine correctly flags and which would otherwise
            produce fictitious breakouts.
    """
    path = Path(path)
    if not path.exists():
        raise DataQualityError(f"CSV not found: {path}")

    candles: list[Candle] = []
    rows_read = rows_skipped = 0

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise DataQualityError(f"{path.name}: file has no header row")

        columns = _resolve_columns(reader.fieldnames)
        missing = [f for f in ("timestamp", "open", "high", "low", "close") if f not in columns]
        if missing:
            raise DataQualityError(
                f"{path.name}: missing required column(s) {', '.join(missing)}. "
                f"Headers found: {', '.join(reader.fieldnames)}"
            )

        use_adjusted = prefer_adjusted and "adj_close" in columns

        for row in reader:
            rows_read += 1
            timestamp = _parse_timestamp(row.get(columns["timestamp"], "") or "")
            open_ = _parse_number(row.get(columns["open"]))
            high = _parse_number(row.get(columns["high"]))
            low = _parse_number(row.get(columns["low"]))
            close = _parse_number(row.get(columns["close"]))
            volume = _parse_number(row.get(columns["volume"])) if "volume" in columns else 0.0

            if use_adjusted:
                adjusted = _parse_number(row.get(columns["adj_close"]))
                if adjusted is not None and close and close > 0:
                    # Scale the whole bar by the adjustment factor so OHLC stays
                    # internally consistent rather than mixing raw and adjusted.
                    factor = adjusted / close
                    open_ = (open_ or 0.0) * factor
                    high = (high or 0.0) * factor
                    low = (low or 0.0) * factor
                    close = adjusted

            if None in (timestamp, open_, high, low, close):
                rows_skipped += 1
                continue
            if min(open_, high, low, close) <= 0:
                rows_skipped += 1
                continue

            candles.append(
                Candle(
                    timestamp=timestamp,
                    open=float(open_),
                    high=float(high),
                    low=float(low),
                    close=float(close),
                    volume=float(volume or 0.0),
                )
            )

    candles.sort(key=lambda c: c.timestamp)
    if max_bars is not None and len(candles) > max_bars:
        candles = candles[-max_bars:]
    return candles, rows_read, rows_skipped


def load_series(
    path: str | Path,
    instrument: Instrument,
    *,
    timeframe: Timeframe = Timeframe.D1,
    quality: DataQualityEngine | None = None,
    min_history: int = 300,
    prefer_adjusted: bool = True,
    max_bars: int | None = None,
) -> LoadResult:
    """Load one instrument's history and validate it.

    Staleness is not enforced: historical files are old by definition. Every
    other section 3.2 check applies in full.
    """
    candles, rows_read, rows_skipped = load_candles(
        path, prefer_adjusted=prefer_adjusted, max_bars=max_bars
    )
    series = CandleSeries.build(instrument, timeframe, candles)
    engine = quality if quality is not None else DataQualityEngine()
    report = engine.validate_series(
        series, min_history=min_history, enforce_staleness=False
    )
    return LoadResult(
        series=series,
        report=report,
        source=str(path),
        rows_read=rows_read,
        rows_skipped=rows_skipped,
    )


#: Symbol prefixes/suffixes that identify an asset class when it is not given.
_CRYPTO_HINTS = ("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "USDT", "-USD", "USD_")
_FOREX_HINTS = ("EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD")


def infer_asset_class(symbol: str) -> AssetClass:
    """Best-effort asset-class inference from a ticker.

    Only a convenience for loading directories of files; an explicit
    :class:`Instrument` always wins.
    """
    upper = symbol.upper().replace("/", "").replace("-", "")
    if any(hint.replace("-", "") in upper for hint in _CRYPTO_HINTS):
        if len(upper) <= 8 and upper[:3] in {"EUR", "GBP", "AUD", "NZD", "USD"} and upper[3:6] in {
            "USD", "JPY", "CHF", "CAD", "GBP",
        }:
            return AssetClass.FOREX
        return AssetClass.CRYPTO
    if len(upper) == 6 and upper[:3] in _FOREX_HINTS and upper[3:] in (
        "USD", "JPY", "CHF", "CAD", "AUD", "NZD", "GBP", "EUR",
    ):
        return AssetClass.FOREX
    return AssetClass.EQUITIES


def instrument_for(symbol: str, *, asset_class: AssetClass | None = None, sector: str = "UNKNOWN") -> Instrument:
    """Build an Instrument with sensible per-asset-class tick and lot sizes."""
    resolved = asset_class or infer_asset_class(symbol)
    if resolved is AssetClass.CRYPTO:
        return Instrument(symbol, resolved, "EXCHANGE", "Crypto", 0.01, 0.0001, 0.0001)
    if resolved is AssetClass.FOREX:
        return Instrument(symbol, resolved, "FX", "FX", 0.00001, 1000.0, 1000.0)
    return Instrument(symbol, resolved, "EXCHANGE", sector, 0.01, 1.0, 1.0)


def load_directory(
    directory: str | Path,
    *,
    timeframe: Timeframe = Timeframe.D1,
    asset_class: AssetClass | None = None,
    min_history: int = 300,
    pattern: str = "*.csv",
) -> dict[Instrument, LoadResult]:
    """Load every CSV in a directory, keyed by instrument.

    The file stem is used as the ticker, so ``data/real/AAPL.csv`` becomes
    ``AAPL``.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise DataQualityError(f"not a directory: {directory}")

    results: dict[Instrument, LoadResult] = {}
    for path in sorted(directory.glob(pattern)):
        symbol = path.stem.upper().replace("_", "").replace(" ", "")
        instrument = instrument_for(symbol, asset_class=asset_class)
        results[instrument] = load_series(
            path, instrument, timeframe=timeframe, min_history=min_history
        )
    return results
