"""Phase 1: the daily-equity ingestion path.

These test the part that must be identical whatever transport delivers the
bars — normalisation, ordering, de-duplication, and refusing to write history
that is silently wrong. A live fetch that returns 900 of 1,000 bars is a
degraded quote; a history download that quietly truncates is a backtest run on
the wrong data and reported as if it were right.
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradermaximus.core.errors import DataQualityError
from tradermaximus.core.types import Timeframe
from tradermaximus.data.sources import (
    AVAILABILITY,
    MIN_DAILY_BARS,
    Reachability,
    Transport,
    daily_source_options,
    ingest_from_payloads,
    load_json_payload,
    normalise_rows,
    reachable_daily_sources,
    rows_to_csv,
)


class TestSourceAvailability(unittest.TestCase):
    """Reachability is a property of this sandbox, not of the vendor."""

    def test_no_daily_source_is_usable_without_a_paid_plan_or_allowlist(self) -> None:
        """The finding that decides what to buy.

        Every commercial vendor tested was refused at CONNECT by the network
        policy, alphavantage.co included. An API key cannot change that; only
        an allowlist entry can. The two MCP-backed vendors are already reached,
        so for them a plan upgrade is sufficient on its own.
        """
        self.assertEqual(reachable_daily_sources(), ())

    def test_the_mcp_vendors_need_only_a_plan(self) -> None:
        for key in ("alpha_vantage_premium", "finnhub_paid"):
            with self.subTest(source=key):
                capability = AVAILABILITY[key]
                self.assertIs(capability.transport, Transport.MCP)
                self.assertIs(capability.reachability, Reachability.NEEDS_PAID_PLAN)
                self.assertIn(Timeframe.D1, capability.intervals)

    def test_the_direct_https_vendors_need_the_network_opened_first(self) -> None:
        for key in ("polygon", "tiingo", "nasdaq_data_link"):
            with self.subTest(source=key):
                capability = AVAILABILITY[key]
                self.assertIs(capability.transport, Transport.DIRECT_HTTPS)
                self.assertIs(capability.reachability, Reachability.NEEDS_NETWORK_ALLOWLIST)

    def test_the_current_equity_feed_is_weekly_only(self) -> None:
        capability = AVAILABILITY["alpha_vantage_free"]
        self.assertTrue(capability.usable_now)
        self.assertNotIn(Timeframe.D1, capability.intervals)

    def test_the_summary_renders(self) -> None:
        rendered = daily_source_options()
        self.assertIn("Polygon.io", rendered)
        self.assertIn("usable now", rendered)


class TestNormalisation(unittest.TestCase):
    def test_vendor_column_names_map_onto_ours(self) -> None:
        rows = normalise_rows([
            {"Date": "2024-01-02", "Open": "1", "High": "3", "Low": "0.5",
             "Close": "2", "Adj Close": "2", "Volume": "100"},
        ])
        self.assertEqual(rows[0], {
            "timestamp": "2024-01-02", "open": "1", "high": "3", "low": "0.5",
            "close": "2", "adjusted_close": "2", "volume": "100",
        })

    def test_polygon_and_alpha_vantage_shapes_both_normalise(self) -> None:
        polygon = normalise_rows([{"t": "2024-01-02", "o": 1, "h": 3, "l": 0.5, "c": 2, "v": 100}])
        alpha = normalise_rows([{
            "timestamp": "2024-01-02", "1. open": 1, "2. high": 3,
            "3. low": 0.5, "4. close": 2, "6. volume": 100,
        }])
        self.assertEqual(polygon[0]["high"], "3")
        self.assertEqual(alpha[0]["high"], "3")

    def test_a_missing_required_column_raises_rather_than_guessing(self) -> None:
        """A download that loses its high column must fail loudly.

        Filling it in from the close would produce a file that loads, validates,
        backtests, and is wrong — the worst of the available outcomes.
        """
        with self.assertRaises(DataQualityError) as caught:
            normalise_rows([{"date": "2024-01-02", "open": "1", "low": "0.5", "close": "2"}])
        self.assertIn("high", str(caught.exception))

    def test_unknown_columns_are_dropped_not_carried(self) -> None:
        rows = normalise_rows([{
            "date": "2024-01-02", "open": "1", "high": "3", "low": "0.5",
            "close": "2", "dividend amount": "0.22", "split coefficient": "1.0",
        }])
        self.assertNotIn("dividend amount", rows[0])


class TestWriting(unittest.TestCase):
    ROWS = [
        {"timestamp": "2024-01-03", "open": "2", "high": "4", "low": "1", "close": "3", "volume": "9"},
        {"timestamp": "2024-01-02", "open": "1", "high": "3", "low": "0.5", "close": "2", "volume": "8"},
        {"timestamp": "2024-01-03", "open": "2", "high": "4", "low": "1", "close": "3.5", "volume": "9"},
    ]

    def test_rows_are_sorted_oldest_first_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "X.csv"
            count = rows_to_csv(self.ROWS, path)
            self.assertEqual(count, 2)
            written = list(csv.DictReader(path.read_text().splitlines()))
            self.assertEqual([r["timestamp"] for r in written], ["2024-01-02", "2024-01-03"])
            # The later duplicate wins, not the first seen.
            self.assertEqual(written[1]["close"], "3.5")

    def test_an_empty_history_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(DataQualityError):
                rows_to_csv([], Path(tmp) / "X.csv")

    def test_the_adjusted_column_is_emitted_only_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain.csv"
            rows_to_csv(self.ROWS, plain)
            self.assertNotIn("adjusted close", plain.read_text().splitlines()[0])

            adjusted = Path(tmp) / "adj.csv"
            rows_to_csv(
                [dict(r, adjusted_close="1.5") for r in self.ROWS[:2]], adjusted
            )
            self.assertIn("adjusted close", adjusted.read_text().splitlines()[0])


class TestIngest(unittest.TestCase):
    def _bars(self, n: int) -> list[dict[str, object]]:
        return [
            {"date": f"2020-{1 + i // 28:02d}-{1 + i % 28:02d}", "open": 10 + i,
             "high": 11 + i, "low": 9 + i, "close": 10.5 + i, "volume": 1000}
            for i in range(n)
        ]

    def test_a_short_history_is_written_but_not_marked_ok(self) -> None:
        """Truncation must be visible, not silent — and not silently discarded."""
        with tempfile.TemporaryDirectory() as tmp:
            results = ingest_from_payloads({"SPY": self._bars(50)}, Path(tmp))
            self.assertEqual(len(results), 1)
            self.assertFalse(results[0].ok)
            self.assertIn("SHORT", results[0].summary())
            self.assertTrue(results[0].path.exists())

    def test_one_symbol_failing_does_not_lose_the_others(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            results = ingest_from_payloads(
                {"SPY": self._bars(20), "BAD": [{"date": "2024-01-02", "open": 1}]},
                Path(tmp),
            )
            by_symbol = {r.symbol: r for r in results}
            self.assertEqual(by_symbol["SPY"].rows, 20)
            self.assertIn("high", by_symbol["BAD"].error)
            self.assertIsNone(by_symbol["BAD"].path)

    def test_the_bar_floor_is_five_years_of_daily_history(self) -> None:
        self.assertGreaterEqual(MIN_DAILY_BARS, 252 * 5)


class TestPayloadLoading(unittest.TestCase):
    def _write(self, tmp: str, name: str, payload: object) -> Path:
        path = Path(tmp) / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_it_reads_an_mcp_offloaded_csv_envelope(self) -> None:
        """The transport that works here writes large results to a file."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "r.json", {
                "result": "date,open,high,low,close,volume\n2024-01-02,1,3,0.5,2,100\n"
            })
            rows = load_json_payload(path)
            self.assertEqual(rows[0]["close"], "2")

    def test_it_reads_a_bare_list_and_a_wrapped_list(self) -> None:
        bar = {"t": "2024-01-02", "o": 1, "h": 3, "l": 0.5, "c": 2, "v": 100}
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(len(load_json_payload(self._write(tmp, "a.json", [bar]))), 1)
            self.assertEqual(
                len(load_json_payload(self._write(tmp, "b.json", {"results": [bar]}))), 1
            )

    def test_an_unrecognised_shape_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "c.json", {"status": "OK", "count": 0})
            with self.assertRaises(DataQualityError):
                load_json_payload(path)

    def test_an_empty_mcp_envelope_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "d.json", {"result": "   "})
            with self.assertRaises(DataQualityError):
                load_json_payload(path)


if __name__ == "__main__":
    unittest.main()
