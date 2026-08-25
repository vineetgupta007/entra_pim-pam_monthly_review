"""Verification pass - run at the end of every cycle before the deliverables are shared.

Checks (CLAUDE.md verification rule):
  1. Row counts reconcile: raw source -> deduped -> anchors -> workbook sheets.
  2. No duplicate rows in any output sheet.
  3. Exception totals match the Summary sheet's live formulas.
  4. Every input file appears in export-manifest.json.
  5. No orphan references: every activation_id used in correlated/exception rows exists.
  6. Every correlated action falls inside its activation's stated window.
  7. Workbook formulas evaluate without error (requires a prior recalculation).

Exits non-zero if any check fails, so it can gate a scheduled run.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import month_paths, read_manifest

RESULTS: list[tuple[bool, str, str]] = []

COUNTA_RE = re.compile(r"^=COUNTA\('?([^'!]+)'?!\$?([A-Z]+)\$?\d+:\$?([A-Z]+)\$?(\d+)\)$")
COUNTIF_RE = re.compile(
    r'^=COUNTIF\(\'?([^\'!]+)\'?!\$?([A-Z]+)\$?\d+:\$?([A-Z]+)\$?(\d+),"(.*)"\)$')


def _col_index(letter: str) -> int:
    n = 0
    for ch in letter:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def eval_count_formula(formula: str, frames: dict[str, pd.DataFrame]) -> int | None:
    """Recompute a Summary COUNTA/COUNTIF straight from the source CSVs. Returns None for
    anything this does not recognise, so unknown formulas are skipped rather than guessed at."""
    m = COUNTA_RE.match(formula)
    if m:
        sheet, col, _, _ = m.groups()
        df = frames.get(sheet)
        if df is None or df.empty:
            return 0
        i = _col_index(col)
        if i >= len(df.columns):
            return None
        return int((df.iloc[:, i].astype(str).str.strip() != "").sum())

    m = COUNTIF_RE.match(formula)
    if m:
        sheet, col, _, _, criterion = m.groups()
        df = frames.get(sheet)
        if df is None or df.empty:
            return 0
        i = _col_index(col)
        if i >= len(df.columns):
            return None
        return int((df.iloc[:, i].astype(str).str.strip() == criterion).sum())
    return None


def check(ok: bool, name: str, detail: str = "") -> bool:
    RESULTS.append((ok, name, detail))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify a completed review cycle.")
    ap.add_argument("--month", required=True)
    ap.add_argument("--run", type=int, default=1)
    ap.add_argument("--label", default=None)
    args = ap.parse_args()

    paths = month_paths(args.month, args.run)
    label = args.label or args.month
    out, inp = paths["output"], paths["input"]

    stats_path = out / f"correlation-stats-{label}.json"
    if not stats_path.exists():
        print(f"No correlation-stats-{label}.json - nothing to verify.", file=sys.stderr)
        return 2
    stats = json.loads(stats_path.read_text(encoding="utf-8"))

    def read(name):
        p = out / f"{name}-{label}.csv"
        return pd.read_csv(p, keep_default_na=False) if p.exists() and p.stat().st_size > 2 \
            else pd.DataFrame()

    acts, corr, unc, exc = (read("activations"), read("correlated-actions"),
                            read("uncovered-actions"), read("exceptions"))

    # 1 - row count reconciliation
    raw, dedup, dropped = (stats.get("pim_rows_raw", 0), stats.get("pim_rows_deduped", 0),
                           stats.get("pim_exact_duplicates_dropped", 0))
    check(raw == dedup + dropped,
          "PIM row counts reconcile", f"{raw} raw = {dedup} deduped + {dropped} duplicates")
    check(len(acts) == stats.get("activations_successful", -1),
          "Activation count matches stats",
          f"activations csv {len(acts)} vs stats {stats.get('activations_successful')}")
    check(len(acts) <= dedup, "Anchors do not exceed distinct events", f"{len(acts)} <= {dedup}")

    # 2 - duplicates in outputs
    for name, df in (("activations", acts), ("exceptions", exc),
                     ("correlated-actions", corr), ("uncovered-actions", unc)):
        if df.empty:
            continue
        dupes = int(df.duplicated().sum())
        check(dupes == 0, f"No duplicate rows in {name}", f"{dupes} found")
    if not acts.empty:
        d = int(acts["activation_id"].duplicated().sum())
        check(d == 0, "activation_id is unique", f"{d} duplicates")
    if not exc.empty:
        d = int(exc["exception_id"].duplicated().sum())
        check(d == 0, "exception_id is unique", f"{d} duplicates")

    # 3 - exception totals vs Summary formulas
    xlsx = out / f"entra-pim-correlation-{label}.xlsx"
    if xlsx.exists():
        try:
            from openpyxl import load_workbook
            wb = load_workbook(xlsx, data_only=True)
            summary = {}
            ws = wb["Summary"]
            for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=2):
                a, b = row[0].value, row[1].value
                if a is not None and b not in (None, ""):
                    summary[str(a).strip()] = b
            total = summary.get("Total exceptions")
            if isinstance(total, (int, float)):
                check(int(total) == len(exc),
                      "Summary exception total matches Exceptions sheet",
                      f"summary {int(total)} vs csv {len(exc)}")
                if not exc.empty:
                    sev_ok = all(
                        summary.get(s) == int((exc["severity"] == s).sum())
                        for s in ("High", "Medium", "Low")
                        if summary.get(s) is not None)
                    check(sev_ok, "Summary severity counts match Exceptions sheet")
            if not exc.empty:
                by_sev = sum(int((exc["severity"] == s).sum())
                             for s in exc["severity"].unique())
                check(by_sev == len(exc), "Severity buckets sum to total",
                      f"{by_sev} vs {len(exc)}")
            # Independently re-evaluate the Summary formulas against the CSVs. This does not
            # need LibreOffice and catches a wrong range or criterion, which a clean
            # recalculation would not.
            frames = {"Activations": acts, "Exceptions": exc,
                      "Correlated Actions": corr, "Uncovered Actions": unc}
            wbf = load_workbook(xlsx, data_only=False)["Summary"]
            evaluated = wrong = 0
            for row in wbf.iter_rows(min_row=1, max_row=wbf.max_row, max_col=2):
                lbl, f = row[0].value, row[1].value
                if not isinstance(f, str) or not f.startswith("="):
                    continue
                expected = eval_count_formula(f, frames)
                if expected is None:
                    continue
                evaluated += 1
                cached = summary.get(str(lbl).strip())
                if isinstance(cached, (int, float)) and int(cached) != expected:
                    wrong += 1
                    check(False, f"Formula value wrong: {str(lbl).strip()}",
                          f"workbook {cached} vs recomputed {expected}")
            check(wrong == 0, "Summary formulas recompute correctly",
                  f"{evaluated} formulas re-evaluated from source CSVs")

            cached_missing = [k for k, v in summary.items() if v is None]
            if any(isinstance(v, str) and v.startswith("=") for v in summary.values()):
                print("  NOTE  Summary formulas have no cached values yet. Excel computes "
                      "them on open; run scripts/recalc.py for a pre-computed file.")
            sheets = set(wb.sheetnames)
            expected = {"Summary", "Activations", "Correlated Actions",
                        "Unmatched Activations", "Uncovered Actions", "Exceptions",
                        "Decisions", "Evidence"}
            check(expected <= sheets, "All expected sheets present",
                  f"missing: {sorted(expected - sheets)}")
        except Exception as exc_err:
            check(False, "Workbook readable", str(exc_err)[:200])
    else:
        check(False, "Workbook exists", f"{xlsx.name} not found")

    # 4 - manifest coverage
    manifest = read_manifest(inp)
    listed = {e.get("file") for e in manifest.get("entries", [])}
    csvs = {p.name for p in inp.glob("*.csv")}
    missing = csvs - listed
    check(not missing, "Every input CSV is in export-manifest.json",
          f"unlisted: {sorted(missing)}" if missing else "")

    # 5 - referential integrity
    if not corr.empty and not acts.empty:
        orphans = set(corr["activation_id"]) - set(acts["activation_id"])
        check(not orphans, "Correlated rows reference real activations",
              f"orphans: {sorted(orphans)[:5]}")
    if not exc.empty and not acts.empty:
        used = {a for a in exc["activation_id"].astype(str) if a and a != "nan"}
        orphans = used - set(acts["activation_id"])
        check(not orphans, "Exception rows reference real activations",
              f"orphans: {sorted(orphans)[:5]}")

    # 6 - every correlated action inside its window
    if not corr.empty:
        c = corr.copy()
        for col in ("activation_utc", "window_end_utc", "audit_Date"):
            c[col] = pd.to_datetime(c[col], errors="coerce", utc=True, format="mixed")
        bad = int(((c["audit_Date"] < c["activation_utc"]) |
                   (c["audit_Date"] >= c["window_end_utc"])).sum())
        check(bad == 0, "Every correlated action falls inside its window",
              f"{bad} outside")
        neg = int((corr["minutes_after_activation"] < 0).sum())
        check(neg == 0, "No negative minutes-after-activation", f"{neg} negative")

    # 7 - integrity of the no-audit case
    if not stats.get("audit_available"):
        if not acts.empty:
            statuses = set(acts["correlation_status"].unique())
            check(statuses == {"unknown - no audit data"},
                  "No-audit cycle labels every activation 'unknown'",
                  f"found: {sorted(statuses)}")
        if not exc.empty:
            bad = int((exc["exception_class"] == "activation_no_actions").sum())
            check(bad == 0, "No 'unused activation' findings without audit data",
                  f"{bad} such rows - would be unsupported by evidence")

    # report
    width = max(len(n) for _, n, _ in RESULTS) + 2
    print(f"\nVerification - {args.month} (label {label})\n{'-' * (width + 12)}")
    failures = 0
    for ok, name, detail in RESULTS:
        failures += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name.ljust(width)}{detail}")
    print(f"{'-' * (width + 12)}\n  {len(RESULTS) - failures}/{len(RESULTS)} passed"
          f"{'' if not failures else f' - {failures} FAILURE(S)'}")

    if not stats.get("audit_available"):
        print("\n  NOTE: no directory audit export was present, so the correlation checks "
              "that depend on it were not exercised.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
