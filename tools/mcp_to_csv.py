"""Convert an Alpha Vantage MCP tool-result file into a clean OHLCV CSV.

Large MCP responses are offloaded by the client to a JSON file rather than
returned inline. This reads that file, extracts the CSV payload, normalises it
and writes it where the validation harness expects it.

Processing the file programmatically rather than through the model's context
keeps the data exact: 5,000-row histories are transcribed byte-for-byte
instead of being re-typed.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


def extract_csv(path: Path) -> str:
    """Pull the CSV payload out of a tool-result file.

    Two shapes occur: ``{"result": "<csv>"}`` for a full response, and a
    preview envelope carrying only ``sample_data``.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        if payload.get("result"):
            return str(payload["result"])
        if payload.get("sample_data"):
            if payload.get("preview"):
                print(
                    f"  WARNING: {path.name} is a PREVIEW containing "
                    f"{payload.get('sample_lines')} of {payload.get('total_lines')} rows",
                    file=sys.stderr,
                )
            return str(payload["sample_data"])
    raise SystemExit(f"{path}: no CSV payload found (keys: {list(payload)[:8]})")


def convert(
    source: Path,
    destination: Path,
    *,
    max_rows: int | None = None,
    require_volume: bool = False,
) -> int:
    """Write a normalised CSV. Returns the row count written."""
    text = extract_csv(source).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line for line in text.split("\n") if line.strip()]
    if len(lines) < 2:
        raise SystemExit(f"{source}: fewer than two lines of CSV")

    reader = csv.DictReader(lines)
    fields = [f.strip().lower() for f in (reader.fieldnames or [])]
    if "timestamp" not in fields:
        raise SystemExit(f"{source}: no timestamp column (headers: {reader.fieldnames})")

    rows: list[dict[str, str]] = []
    for row in reader:
        clean = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        if not clean.get("timestamp"):
            continue
        rows.append(clean)

    # Alpha Vantage returns newest-first; the pipeline wants oldest-first.
    rows.sort(key=lambda r: r["timestamp"])
    if max_rows is not None and len(rows) > max_rows:
        rows = rows[-max_rows:]

    has_volume = any(r.get("volume") for r in rows)
    if require_volume and not has_volume:
        raise SystemExit(f"{source}: volume required but absent")

    # Preserve the adjusted close when the vendor supplies one. Dropping it
    # silently hands the pipeline raw prices, whose split gaps then read as
    # genuine 50% crashes and get the whole series rejected.
    has_adjusted = any(r.get("adjusted close") or r.get("adjusted_close") for r in rows)

    header = ["timestamp", "open", "high", "low", "close"]
    if has_adjusted:
        header.append("adjusted close")
    if has_volume:
        header.append("volume")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            writer.writerow(
                [
                    row.get(field) or row.get(field.replace(" ", "_"), "")
                    for field in header
                ]
            )
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert MCP tool results to OHLCV CSV")
    parser.add_argument("source", help="tool-result .txt file")
    parser.add_argument("destination", help="output CSV path")
    parser.add_argument("--max-rows", type=int, default=None)
    args = parser.parse_args()

    count = convert(Path(args.source), Path(args.destination), max_rows=args.max_rows)
    print(f"  wrote {count} rows -> {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
