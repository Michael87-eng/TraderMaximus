"""Turn fetched daily equity payloads into validated history files (Phase 1).

The fetch step is deliberately not here. The only transport that reaches a
vendor from this environment is MCP, and MCP offloads large tool results to
files rather than returning them inline — so fetching is a sequence of tool
calls, and this owns everything after: normalise, order, de-duplicate, write,
then run the same data-quality engine the validator will run, so a bad download
is caught now rather than as a strange backtest result later.

Usage, once daily payloads exist::

    # one JSON file per symbol, named <SYMBOL>.json
    python tools/ingest_daily_equities.py --payload-dir /path/to/payloads

    # or check what it would take to get daily bars at all
    python tools/ingest_daily_equities.py --options
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradermaximus.core.types import AssetClass, Timeframe
from tradermaximus.data.csv_loader import load_directory
from tradermaximus.data.sources import (
    EQUITY_UNIVERSE,
    MIN_DAILY_BARS,
    daily_source_options,
    ingest_from_payloads,
    load_json_payload,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--payload-dir", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("data/real/equities_daily"))
    parser.add_argument("--symbols", nargs="*", default=list(EQUITY_UNIVERSE))
    parser.add_argument("--options", action="store_true",
                        help="print what each vendor would require, and exit")
    args = parser.parse_args()

    if args.options or args.payload_dir is None:
        print("Daily equity bars — what each vendor would require\n")
        print(daily_source_options())
        print("\nReachability was measured from this environment, not read off a")
        print("pricing page: every direct-HTTPS vendor is refused at CONNECT by the")
        print("network policy, so a key alone does not make one usable here.")
        return 0 if args.options else 1

    payloads = {}
    for symbol in args.symbols:
        path = args.payload_dir / f"{symbol}.json"
        if not path.exists():
            print(f"  {symbol}: no payload at {path}", file=sys.stderr)
            continue
        try:
            payloads[symbol] = load_json_payload(path)
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"  {symbol}: unreadable — {exc}", file=sys.stderr)

    if not payloads:
        print("no payloads read; nothing written", file=sys.stderr)
        return 1

    print(f"Writing daily history to {args.out_dir}\n")
    results = ingest_from_payloads(payloads, args.out_dir)
    for result in results:
        print(f"  {result.summary()}")

    print("\nData-quality check (the same engine the validator runs):\n")
    loaded = load_directory(
        args.out_dir, timeframe=Timeframe.D1,
        asset_class=AssetClass.EQUITIES, min_history=300,
    )
    usable = 0
    for load in loaded.values():
        print(f"  {load.summary()}")
        usable += bool(load.usable)

    short = [r for r in results if r.rows < MIN_DAILY_BARS]
    print(f"\n  {usable}/{len(loaded)} symbols usable; "
          f"{len(short)} below the {MIN_DAILY_BARS:,}-bar floor")
    if usable == 0:
        print("\n  Nothing usable. Do not re-run validation yet.", file=sys.stderr)
        return 1
    print("\n  Next: python tools/preflight_daily_equities.py")
    print("  That checks whether daily bars actually resolved the stop-placement")
    print("  blocker before you spend a validation cycle finding out.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
