"""Historical bar sources for bulk ingestion (spec section 3.4).

:mod:`providers` serves the *live* path: give it an instrument and a lookback
and it returns the most recent bars. This module serves the *research* path:
pull a symbol's whole usable history once, write it to disk, and validate
against it. The two have different failure modes — a live fetch that returns
900 of 1,000 bars is fine, a history download that silently truncates is a
backtest quietly run on the wrong data — so they are kept apart.

Every source here declares three things a caller must be able to check before
trusting a download:

* the **transport** it uses, because in a sandboxed environment reachability is
  not a property of the vendor but of the network policy;
* the **interval** it can actually deliver, because a daily strategy validated
  on weekly bars is not the same strategy;
* the **adjustment** status of its prices, because unadjusted splits read as
  breakouts and manufacture signals that never existed.

Reachability, measured
----------------------

Direct HTTPS to every commercial vendor tested is refused by this environment's
network policy at CONNECT time — polygon.io, tiingo.com, data.nasdaq.com,
eodhd.com, stooq.com and alphavantage.co alike. That is a property of the
sandbox, not of the vendors, and it means an API key alone does not make a
vendor usable here. Two transports do work:

* **MCP** — the Alpha Vantage and Finnhub MCP servers are already connected and
  reach their vendors' APIs. A paid plan on either unlocks daily bars with no
  network change at all.
* **Direct HTTPS**, once the vendor's host is added to the environment's
  network allowlist.

``AVAILABILITY`` below records what was measured, so the choice of vendor can
be made on evidence rather than on a pricing page.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from ..core.errors import DataQualityError
from ..core.types import AssetClass, Candle, Instrument, Timeframe


class Transport(str, Enum):
    """How a source is reached, which is what decides whether it works here."""

    MCP = "mcp"                    # via a connected MCP server
    DIRECT_HTTPS = "direct_https"  # needs the host on the network allowlist
    LOCAL_FILE = "local_file"      # already on disk


class Reachability(str, Enum):
    REACHABLE = "reachable"
    NEEDS_PAID_PLAN = "needs_paid_plan"
    NEEDS_NETWORK_ALLOWLIST = "needs_network_allowlist"


@dataclass(frozen=True, slots=True)
class SourceCapability:
    """What one vendor can deliver, and whether it can be reached from here."""

    vendor: str
    transport: Transport
    reachability: Reachability
    intervals: frozenset[Timeframe]
    adjusted: bool
    max_history_years: float
    note: str = ""

    @property
    def usable_now(self) -> bool:
        return self.reachability is Reachability.REACHABLE


#: Measured on 2026-09-02 from this environment. `reachability` is an
#: observation, not a claim about the vendor: NEEDS_NETWORK_ALLOWLIST means the
#: proxy refused CONNECT, which no API key can change.
AVAILABILITY: Mapping[str, SourceCapability] = {
    "alpha_vantage_free": SourceCapability(
        vendor="Alpha Vantage (free tier, via MCP)",
        transport=Transport.MCP,
        reachability=Reachability.REACHABLE,
        intervals=frozenset({Timeframe.W1}),
        adjusted=True,
        max_history_years=27.0,
        note="TIME_SERIES_WEEKLY_ADJUSTED full history is free. "
             "TIME_SERIES_DAILY_ADJUSTED and outputsize=full both return a "
             "premium-endpoint error. This is what the current equity data is.",
    ),
    "alpha_vantage_premium": SourceCapability(
        vendor="Alpha Vantage Premium (via MCP)",
        transport=Transport.MCP,
        reachability=Reachability.NEEDS_PAID_PLAN,
        intervals=frozenset({Timeframe.D1, Timeframe.W1}),
        adjusted=True,
        max_history_years=27.0,
        note="Unlocks TIME_SERIES_DAILY_ADJUSTED with outputsize=full. The MCP "
             "server is already connected, so a plan upgrade needs no network "
             "or code change — the same tool call simply stops erroring.",
    ),
    "finnhub_paid": SourceCapability(
        vendor="Finnhub paid plan (via MCP)",
        transport=Transport.MCP,
        reachability=Reachability.NEEDS_PAID_PLAN,
        intervals=frozenset({Timeframe.D1, Timeframe.W1}),
        adjusted=False,
        max_history_years=20.0,
        note="/stock/candle returns an application-level 403 ('You don't have "
             "access to this resource'), which proves the endpoint is reached "
             "and the block is the plan. Candles are unadjusted, so splits must "
             "be handled separately.",
    ),
    "polygon": SourceCapability(
        vendor="Polygon.io",
        transport=Transport.DIRECT_HTTPS,
        reachability=Reachability.NEEDS_NETWORK_ALLOWLIST,
        intervals=frozenset({Timeframe.D1, Timeframe.W1}),
        adjusted=True,
        max_history_years=20.0,
        note="api.polygon.io refused at CONNECT by the sandbox network policy. "
             "A key alone will not work from here; the host must be allowlisted "
             "in the environment configuration first.",
    ),
    "tiingo": SourceCapability(
        vendor="Tiingo",
        transport=Transport.DIRECT_HTTPS,
        reachability=Reachability.NEEDS_NETWORK_ALLOWLIST,
        intervals=frozenset({Timeframe.D1}),
        adjusted=True,
        max_history_years=30.0,
        note="api.tiingo.com refused at CONNECT.",
    ),
    "nasdaq_data_link": SourceCapability(
        vendor="Nasdaq Data Link",
        transport=Transport.DIRECT_HTTPS,
        reachability=Reachability.NEEDS_NETWORK_ALLOWLIST,
        intervals=frozenset({Timeframe.D1}),
        adjusted=True,
        max_history_years=30.0,
        note="data.nasdaq.com refused at CONNECT.",
    ),
}


def reachable_daily_sources() -> tuple[SourceCapability, ...]:
    """Sources that can deliver daily bars from this environment right now."""
    return tuple(
        c for c in AVAILABILITY.values()
        if c.usable_now and Timeframe.D1 in c.intervals
    )


def daily_source_options() -> str:
    """A rendered summary of what it would take to get daily equity bars."""
    lines = [
        f"  {'vendor':38}{'transport':15}{'daily':>7}  what is needed",
        f"  {'-' * 38} {'-' * 14} {'-' * 6}  {'-' * 40}",
    ]
    for capability in AVAILABILITY.values():
        needed = {
            Reachability.REACHABLE: "nothing — usable now",
            Reachability.NEEDS_PAID_PLAN: "a paid plan (no code or network change)",
            Reachability.NEEDS_NETWORK_ALLOWLIST: "host allowlisted, THEN a paid plan",
        }[capability.reachability]
        daily = "yes" if Timeframe.D1 in capability.intervals else "no"
        lines.append(
            f"  {capability.vendor:38}{capability.transport.value:15}{daily:>7}  {needed}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Normalising a vendor payload into candles
# ---------------------------------------------------------------------------

#: Column aliases seen across vendors, normalised to our own names. Kept here
#: rather than in each adapter so a new vendor is a table entry, not a parser.
COLUMN_ALIASES: Mapping[str, str] = {
    "timestamp": "timestamp", "date": "timestamp", "datetime": "timestamp",
    "t": "timestamp", "time": "timestamp",
    "open": "open", "o": "open", "1. open": "open",
    "high": "high", "h": "high", "2. high": "high",
    "low": "low", "l": "low", "3. low": "low",
    "close": "close", "c": "close", "4. close": "close",
    "adjusted close": "adjusted_close", "adj close": "adjusted_close",
    "adjclose": "adjusted_close", "adjusted_close": "adjusted_close",
    "5. adjusted close": "adjusted_close",
    "volume": "volume", "v": "volume", "6. volume": "volume",
}

REQUIRED = ("timestamp", "open", "high", "low", "close")


def normalise_rows(rows: Iterable[Mapping[str, object]]) -> list[dict[str, str]]:
    """Map vendor column names onto ours, dropping columns we do not use.

    Raises rather than guessing when a required column is missing: a download
    that silently loses its high column produces a backtest that runs and is
    wrong, which is worse than one that fails.
    """
    out: list[dict[str, str]] = []
    for row in rows:
        mapped: dict[str, str] = {}
        for key, value in row.items():
            canonical = COLUMN_ALIASES.get(str(key).strip().lower())
            if canonical is not None and value is not None:
                mapped[canonical] = str(value)
        if not out:
            missing = [c for c in REQUIRED if c not in mapped]
            if missing:
                raise DataQualityError(
                    f"vendor payload is missing required column(s): {', '.join(missing)}; "
                    f"saw {sorted(row)}"
                )
        out.append(mapped)
    return out


def rows_to_csv(rows: Sequence[Mapping[str, str]], destination: Path) -> int:
    """Write normalised rows to the CSV layout the loader expects.

    Rows are sorted oldest-first and de-duplicated on timestamp — vendors
    differ on ordering and some repeat the most recent bar. Returns the row
    count written.
    """
    if not rows:
        raise DataQualityError("refusing to write an empty history file")

    columns = ["timestamp", "open", "high", "low", "close", "volume"]
    if any("adjusted_close" in r for r in rows):
        columns.append("adjusted close")

    seen: dict[str, Mapping[str, str]] = {}
    for row in rows:
        seen[row["timestamp"]] = row
    ordered = [seen[k] for k in sorted(seen)]

    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in ordered:
            writer.writerow([
                row.get("timestamp", ""),
                row.get("open", ""),
                row.get("high", ""),
                row.get("low", ""),
                row.get("close", ""),
                row.get("volume", "0"),
                *([row.get("adjusted_close", row.get("close", ""))]
                  if "adjusted close" in columns else []),
            ])
    return len(ordered)


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

#: The equity universe Phase 1 validates against.
EQUITY_UNIVERSE: tuple[str, ...] = ("SPY", "AAPL", "MSFT", "NVDA", "TSLA")

#: Minimum bars before a symbol is worth validating: 300 warm-up, then enough
#: left that the 30% out-of-sample slice is not a rounding error. Five years of
#: daily bars is about 1,260.
MIN_DAILY_BARS = 1_260


@dataclass(frozen=True, slots=True)
class IngestResult:
    symbol: str
    rows: int
    path: Path | None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.error == "" and self.rows >= MIN_DAILY_BARS

    def summary(self) -> str:
        if self.error:
            return f"{self.symbol}: FAILED — {self.error}"
        verdict = "OK" if self.rows >= MIN_DAILY_BARS else f"SHORT (want {MIN_DAILY_BARS:,})"
        return f"{self.symbol}: {self.rows:,} rows -> {self.path} [{verdict}]"


def ingest_from_payloads(
    payloads: Mapping[str, Sequence[Mapping[str, object]]],
    destination_dir: Path,
) -> list[IngestResult]:
    """Normalise and write one history file per symbol.

    Deliberately takes already-fetched payloads rather than doing the fetching:
    the MCP transport that works in this environment offloads large tool results
    to files rather than returning them inline, so the fetch step belongs to the
    caller. This function owns the part that must be identical whatever the
    transport — normalisation, ordering, de-duplication and the length check.
    """
    results: list[IngestResult] = []
    for symbol in sorted(payloads):
        try:
            rows = normalise_rows(payloads[symbol])
            path = destination_dir / f"{symbol}.csv"
            count = rows_to_csv(rows, path)
            results.append(IngestResult(symbol, count, path))
        except (DataQualityError, KeyError, ValueError) as exc:
            results.append(IngestResult(symbol, 0, None, f"{type(exc).__name__}: {exc}"))
    return results


def load_json_payload(path: Path, key: str | None = None) -> list[dict[str, object]]:
    """Read a vendor JSON payload, including an MCP offloaded tool result.

    Handles the shapes actually seen: a bare list of bar objects, a dict with
    a ``results``/``data``/``candles`` list, and the MCP ``{"result": "<csv>"}``
    envelope large responses are written into.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))

    if isinstance(payload, dict) and isinstance(payload.get("result"), str):
        text = payload["result"].strip()
        if not text:
            raise DataQualityError(f"{path}: MCP result envelope is empty")
        return list(csv.DictReader(text.splitlines()))

    if isinstance(payload, list):
        return [dict(row) for row in payload]

    if isinstance(payload, dict):
        for candidate in (key, "results", "data", "candles", "bars"):
            if candidate and isinstance(payload.get(candidate), list):
                return [dict(row) for row in payload[candidate]]

    raise DataQualityError(
        f"{path}: no bar list found (top-level keys: {sorted(payload)[:8] if isinstance(payload, dict) else type(payload).__name__})"
    )
