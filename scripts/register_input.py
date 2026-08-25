"""Record provenance for an input file that was placed by hand rather than exported by
these scripts (the --skip-export path).

Every figure in a deliverable has to trace to a source file, so a manually placed export
still needs a manifest entry. This records what is actually known and does not guess:
unknown fields are written as "unknown - manually placed file".

  python register_input.py --month 2026-08 --file AuditCsv-2026-08.csv \
      --source "Entra portal download by vgupta" --notes "downloaded 2026-09-02"
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import manifest_entry, month_paths, record_manifest

UNKNOWN = "unknown - manually placed file"


def main() -> int:
    ap = argparse.ArgumentParser(description="Add a manifest entry for a manual input file.")
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--run", type=int, default=1)
    ap.add_argument("--file", required=True, help="filename inside the month's input/ folder")
    ap.add_argument("--source", default=UNKNOWN, help="where the file came from")
    ap.add_argument("--endpoint", default=UNKNOWN)
    ap.add_argument("--filter", dest="odata_filter", default=UNKNOWN)
    ap.add_argument("--period", default=None, help="defaults to --month")
    ap.add_argument("--notes", default="")
    args = ap.parse_args()

    paths = month_paths(args.month, args.run)
    target = paths["input"] / args.file
    if not target.exists():
        print(f"Not found: {target}", file=sys.stderr)
        return 2

    with open(target, newline="", encoding="utf-8", errors="replace") as fh:
        rows = max(0, sum(1 for _ in csv.reader(fh)) - 1)

    notes = "; ".join(filter(None, [args.notes, "provenance recorded manually via "
                                                "register_input.py"]))
    entry = manifest_entry(target, source=args.source, endpoint=args.endpoint,
                           odata_filter=args.odata_filter,
                           period=args.period or args.month, row_count=rows, notes=notes)
    path = record_manifest(paths["input"], entry)
    print(f"  recorded {target.name}: {rows} data rows")
    print(f"  sha256 {entry['sha256'][:16]}...")
    print(f"  -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
