"""Draft one attestation request per privileged actor, for the role owner to complete.

Writes markdown files into the cycle's output/ folder. Nothing is sent anywhere - per
CLAUDE.md, anything leaving the project folder is a separate approval, and revocation
decisions belong to the role owner rather than to this workflow.

Every figure comes from exceptions-<label>.csv and correlation-stats-<label>.json, so no
count in a draft is computed by hand.

    python scripts/draft_attestations.py --month 2026-08
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_reports import ADVISORY, REVIEW_THEME, classify_action
from common import load_analysis_config, month_paths

# Themes where the owner needs to see each row, not just a count.
ITEMISE = {"Standing access outside time-binding", "Failing control - configuration"}

THEME_ORDER = ["Standing access outside time-binding",
               "Action outside any activation window",
               "Highest-privilege role usage",
               "Failing control - configuration",
               "Justification quality",
               "Activation with no observed use",
               "Timing - check reporting_timezone first",
               "Delegation path",
               "Eligibility granted",
               "Break-glass - reported, not for revocation"]


def slug(upn: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(upn).casefold()).strip("-") or "unknown-actor"


def draft(actor: str, rows: pd.DataFrame, stats: dict, label: str,
          break_glass_declared: bool) -> str:
    period = (f"{str(stats.get('pim_period_observed_start'))[:10]} to "
              f"{str(stats.get('pim_period_observed_end'))[:10]}")
    window = stats.get("activation_window_hours")

    out = [f"# Privileged access attestation request - {label}",
           "",
           f"**Account under review:** {actor}",
           f"**Review period observed in the source data:** {period}",
           f"**Exceptions raised against this account:** {len(rows)}",
           "",
           "## What is being asked",
           "",
           "For each item below, record a decision of **keep**, **modify**, or **revoke**, "
           "with your name and the date. Recommendations in this document are advisory; the "
           "decision is the role owner's. Return the completed table to the reviewer, who "
           "records it on the `Decisions` sheet of the findings workbook.",
           ""]

    by_theme = {t: g for t, g in rows.groupby("review_theme")}
    ordered = [t for t in THEME_ORDER if t in by_theme]
    ordered += [t for t in sorted(by_theme) if t not in ordered]

    out += ["## Summary of items", "", "| Theme | Items |", "|---|---|"]
    for t in ordered:
        out.append(f"| {t} | {len(by_theme[t])} |")
    out.append("")

    for t in ordered:
        g = by_theme[t]
        out += [f"## {t} ({len(g)})", ""]
        classes = sorted(g["exception_class"].unique())
        for cls in classes:
            sub = g[g["exception_class"] == cls]
            out.append(f"**`{cls}`** - {len(sub)} item(s), severity "
                       f"{'/'.join(sorted(sub['severity'].unique()))}")
            out.append("")
            advice = ADVISORY.get(cls)
            if advice:
                out += [f"> Advisory: {advice}", ""]

            if t in ITEMISE or len(sub) <= 8:
                out += ["| Exception | When (UTC) | Role | Detail |", "|---|---|---|---|"]
                for r in sub.itertuples(index=False):
                    detail = str(r.detail).replace("|", "\\|")
                    out.append(f"| {r.exception_id} | {str(r.timestamp_utc)[:19]} | "
                               f"{r.entra_role or '-'} | {detail} |")
                out.append("")
            else:
                ids = ", ".join(sub["exception_id"].tolist())
                out += [f"Exception IDs: {ids}", "",
                        "Full rows are on the `Exceptions` and `Decisions` sheets of the "
                        "findings workbook.", ""]

            if cls == "uncovered_privileged_action":
                at = sub["action_type"].value_counts().to_dict()
                out += [f"Of these, {at.get('read-only', 0)} are read or telemetry "
                        f"operations and {at.get('write or unclear', 0)} are writes or "
                        f"unclear. The writes warrant attention first; a read is weak "
                        f"evidence of standing privileged access.", ""]

    out += ["## Your decisions", "",
            "| Exception | Decision (keep/modify/revoke) | Decided by | Date | "
            "Target remediation date | Notes |", "|---|---|---|---|---|---|"]
    for r in rows.itertuples(index=False):
        out.append(f"| {r.exception_id} |  |  |  |  |  |")
    out.append("")

    out += ["## Limitations you should read these against", "",
            f"- Attribution uses a fixed {window}-hour window from each activation rather "
            f"than the real activation expiry, so an admin who worked outside that window "
            f"can look identical to one who did nothing.",
            "- Correlation is temporal, not causal: an action inside a window is not proof "
            "the activated role authorised it."]
    if stats.get("audit_content_identical_rows"):
        out.append(f"- {stats['audit_content_identical_rows']} audit rows cannot be "
                   f"distinguished from another row by any exported field, so action volume "
                   f"is an upper bound for those events.")
    if not break_glass_declared:
        out.append("- No break-glass accounts are declared in configuration, so a "
                   "break-glass account would appear here as a finding.")
    out += ["", "---", "",
            "*Draft prepared for review. Not sent. Revocation is never automated.*", ""]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Draft per-owner attestation requests.")
    ap.add_argument("--month", required=True)
    ap.add_argument("--run", type=int, default=1)
    ap.add_argument("--label", default=None)
    args = ap.parse_args()

    paths = month_paths(args.month, args.run)
    label = args.label or args.month
    out = paths["output"]

    exc_path = out / f"exceptions-{label}.csv"
    stats_path = out / f"correlation-stats-{label}.json"
    if not exc_path.exists() or not stats_path.exists():
        print(f"Need {exc_path.name} and {stats_path.name} - run correlate.py first.",
              file=sys.stderr)
        return 2

    exc = pd.read_csv(exc_path, keep_default_na=False)
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    if exc.empty:
        print("No exceptions this cycle - nothing to attest.")
        return 0

    exc["review_theme"] = exc["exception_class"].map(
        lambda c: REVIEW_THEME.get(c, "Unthemed - triage manually"))
    exc["action_type"] = [classify_action(c, d)
                          for c, d in zip(exc["exception_class"], exc["detail"])]

    # Read from config rather than stats: no stat records the declaration itself.
    bg_declared = bool(load_analysis_config().get("break_glass_accounts"))

    written = []
    for actor, rows in exc.groupby("actor"):
        name = actor or "unattributed"
        path = out / f"attestation-{slug(name)}-{label}.md"
        path.write_text(draft(name, rows, stats, label, bg_declared), encoding="utf-8")
        written.append((path.name, len(rows)))

    print(f"  wrote {len(written)} attestation draft(s) to {out}")
    for n, c in sorted(written, key=lambda x: -x[1]):
        print(f"    {n}  ({c} exception(s))")
    print("\n  Drafts only - nothing has been sent. Decisions belong to the role owner.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
