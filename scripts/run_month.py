"""Orchestrator - runs the monthly cycle in order.

The workflow is deliberately split into two phases, because a scheduled run is unattended
and cannot wait for anyone's approval:

  PHASE A - export (steps 1-2). Needs Graph access and credentials.
      python run_month.py --month 2026-08 --export-only

  PHASE B - correlate and report (steps 3-4). Needs only the files in input/.
      python run_month.py --month 2026-08 --skip-export

  Both at once, for an interactive run:
      python run_month.py --month 2026-08

  Second pass over the same month -> August-2/:
      python run_month.py --month 2026-08 --run 2

Phase A defaults to interactive browser sign-in. Prove access first with
`python check_auth.py`, and again with `--auth-mode secret_env` before scheduling it
unattended. Any step that fails stops the run - nothing is worked around.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import month_folder_name, month_paths

HERE = Path(__file__).resolve().parent


def step(name: str, script: str, extra: list[str]) -> bool:
    print(f"\n{'=' * 72}\n{name}\n{'=' * 72}")
    proc = subprocess.run([sys.executable, str(HERE / script), *extra])
    if proc.returncode != 0:
        print(f"\n{name} FAILED (exit {proc.returncode}). Stopping.", file=sys.stderr)
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the monthly PIM review cycle.")
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--run", type=int, default=1, help="re-run number (2 -> August-2)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--auth-mode", choices=["interactive", "secret_env", "certificate"],
                    default=None, help="override config.json auth.mode for this run")
    phase = ap.add_mutually_exclusive_group()
    phase.add_argument("--skip-export", action="store_true",
                       help="PHASE B: skip steps 1-2, use files already in input/")
    phase.add_argument("--export-only", action="store_true",
                       help="PHASE A: run steps 1-2 only, then stop for review")
    ap.add_argument("--label", default=None, help="output filename suffix (default: month)")
    args = ap.parse_args()

    paths = month_paths(args.month, args.run)
    folder = month_folder_name(args.month, args.run)
    common = ["--month", args.month, "--run", str(args.run)]
    cfg = ["--config", args.config] if args.config else []
    auth = ["--auth-mode", args.auth_mode] if args.auth_mode else []
    label = ["--label", args.label] if args.label else []

    phase_name = ("PHASE A (export only)" if args.export_only else
                  "PHASE B (correlate and report)" if args.skip_export else
                  "PHASE A + B")
    print(f"Entra PIM/PAM monthly review - {args.month}  ->  {folder}/   {phase_name}")
    print(f"  input:  {paths['input']}")
    print(f"  output: {paths['output']}")

    if not args.skip_export:
        if not step("STEP 1  Export PIM activity", "export_pim_activity.py",
                    common + cfg + auth):
            return 1
        if not step("STEP 2  Export directory audit events", "export_audit_events.py",
                    common + cfg + auth):
            return 1
        if args.export_only:
            print(f"\n{'=' * 72}\nPHASE A complete. Files in {paths['input']}:")
            for f in sorted(paths["input"].glob("*")):
                print(f"  {f.name}")
            print("\nNext, PHASE B - either run:")
            print(f"  python run_month.py --month {args.month}"
                  f"{'' if args.run == 1 else f' --run {args.run}'} --skip-export")
            print("or ask Claude to run the monthly PIM review, which will correlate, "
                  "report, verify, and triage before asking you to approve.")
            return 0
    else:
        print("\nSkipping steps 1-2 (--skip-export). Using files already in input/:")
        for f in sorted(paths["input"].glob("*")):
            print(f"  {f.name}")

    if not step("STEP 3  Correlate", "correlate.py", common + cfg + label):
        return 1
    if not step("STEP 4  Build deliverables", "build_reports.py", common + label):
        return 1

    print(f"\n{'=' * 72}\nDone. Deliverables in {paths['output']}")
    for f in sorted(paths["output"].glob("*")):
        print(f"  {f.name}")
    print("\nNext: route each exception to its role owner for keep / modify / revoke, and "
          "record the decision, decider, and date on the Decisions sheet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
