"""Verify the committed data against its manifest, or fail loudly (Task 1b).

The failure mode this removes: `data/real/**/*.csv` is git-ignored, a container
replacement destroyed every raw input, and the loss was discovered only when a
rerun was attempted. Every number reported before that point became
unreproducible.

Two defences:

1. **Venue data is tracked in git.** `data/venue/` was force-added, so the
   execution-venue bars survive a container replacement. That is the primary
   fix and it is already in place.
2. **This script.** Run it at session start. It checks every file named in a
   manifest for existence, sha256, row count, span, duplicate timestamps and
   staleness, and exits non-zero on any failure. It never repairs and never
   downloads — a script that silently re-fetches would hide the very drift it
   exists to detect.

Run:
    python tools/verify_data.py                 # all manifests
    python tools/verify_data.py --max-age-days 30
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

MANIFESTS = (
    Path("data/venue/MANIFEST.json"),
    Path("data/real/crypto/MANIFEST.json"),
)


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _iso(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def check_file(root: Path, name: str, spec: dict, *, max_age_days: int) -> list[str]:
    """Return a list of failures for one file. Empty means it verified."""
    failures: list[str] = []

    candidates = [root / name, root / f"{name}.csv"]
    if "path" in spec:
        candidates.insert(0, Path(spec["path"]))
    path = next((c for c in candidates if c.is_file()), None)
    if path is None:
        return [f"{name}: MISSING (looked for {', '.join(str(c) for c in candidates)})"]

    raw = path.read_bytes()
    expected = spec.get("sha256")
    if expected:
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected:
            failures.append(
                f"{name}: SHA256 MISMATCH\n"
                f"      expected {expected}\n"
                f"      actual   {actual}\n"
                f"      the file changed since the manifest was written"
            )
            # A hash mismatch invalidates every other check against this spec.
            return failures

    if not path.suffix == ".csv":
        return failures

    rows = _rows(path)
    if (want := spec.get("rows")) is not None and len(rows) != want:
        failures.append(f"{name}: ROW COUNT {len(rows)} != manifest {want}")

    if rows:
        stamps = [r.get("timestamp", "") for r in rows]
        if len(stamps) != len(set(stamps)):
            dupes = len(stamps) - len(set(stamps))
            failures.append(f"{name}: {dupes} DUPLICATE timestamps")
        if (want := spec.get("first_bar")) and stamps[0] != want:
            failures.append(f"{name}: FIRST BAR {stamps[0]} != manifest {want}")
        if (want := spec.get("last_bar")) and stamps[-1] != want:
            failures.append(f"{name}: LAST BAR {stamps[-1]} != manifest {want}")

        last = _iso(stamps[-1])
        if last is not None:
            age = (datetime.now(timezone.utc) - last).days
            if age > max_age_days:
                failures.append(
                    f"{name}: STALE — last bar {stamps[-1]} is {age} days old "
                    f"(limit {max_age_days}). Re-fetch before using this for "
                    f"anything other than historical research."
                )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-age-days", type=int, default=45)
    parser.add_argument("--manifest", type=Path, action="append")
    args = parser.parse_args()

    manifests = args.manifest or [m for m in MANIFESTS if m.is_file()]
    if not manifests:
        print("NO MANIFEST FOUND — nothing can be verified.", file=sys.stderr)
        print("Expected one of: " + ", ".join(str(m) for m in MANIFESTS), file=sys.stderr)
        return 2

    total_failures: list[str] = []
    total_checked = 0

    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        root = manifest_path.parent
        print(f"\n{manifest_path}")
        if provider := manifest.get("provider"):
            print(f"  provider: {provider}")
        if klass := manifest.get("provider_class"):
            print(f"  class:    {klass}")

        # Two manifest shapes exist: {"symbols": {...}} and {"files": {...}}.
        entries = manifest.get("files") or manifest.get("symbols") or {}
        if isinstance(entries, list):
            entries = {e.get("path", str(i)): e for i, e in enumerate(entries)}

        for name, spec in sorted(entries.items()):
            if not isinstance(spec, dict):
                continue
            total_checked += 1
            failures = check_file(root, name, spec, max_age_days=args.max_age_days)
            if failures:
                total_failures.extend(failures)
                for line in failures:
                    print(f"  FAIL  {line}")
            else:
                rows = spec.get("rows", "?")
                print(f"  ok    {name}  rows={rows}  sha256 verified")

    print(f"\n{total_checked} file(s) checked, {len(total_failures)} failure(s)")
    if total_failures:
        print("\nDATA VERIFICATION FAILED. Do not run a backtest against this tree:")
        print("a result computed on unverified data cannot be reproduced or audited.")
        return 1
    print("All data verified against manifest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
