"""Step 2 - export Entra directory audit events for the same month.

Source: GET /v1.0/auditLogs/directoryAudits (no loggedByService filter - all activity).

Volume control: step 1 wrote pim-actors-YYYY-MM.json. By default this script queries
once per actor using `initiatedBy/user/id eq '<guid>'`, so the pull scales with the number
of privileged admins rather than with whole-tenant activity. If the tenant rejects that
filter, it falls back to a full-period pull filtered locally, and records the fallback in
the manifest.

Output column order matches the Entra portal's AuditCsv download so the file stays
diff-able against a manual export.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (clamp_to_retention, load_config, manifest_entry, month_bounds, month_paths, norm_upn,
                   record_manifest, record_manifest as _rm)
from graph_client import GraphAuthError, GraphClient, directory_audit_params

COLUMNS = ["Date", "Service", "Category", "Activity", "Result", "ResultReason",
           "Actor", "ActorType", "ActorIpAddress", "Target(s)", "ObjectId(s)",
           "CorrelationId", "AdditionalDetails"]


def _actor(event: dict) -> tuple[str, str, str]:
    by = event.get("initiatedBy") or {}
    user = by.get("user") or {}
    if user:
        return (user.get("userPrincipalName") or user.get("displayName") or "",
                "User", user.get("ipAddress") or "")
    app = by.get("app") or {}
    if app:
        return (app.get("displayName") or app.get("servicePrincipalName") or "",
                "App", "")
    return ("", "", "")


def map_event(event: dict) -> dict:
    actor, actor_type, ip = _actor(event)
    targets = event.get("targetResources") or []
    return {
        "Date": event.get("activityDateTime") or "",
        "Service": event.get("loggedByService") or "",
        "Category": event.get("category") or "",
        "Activity": event.get("activityDisplayName") or "",
        "Result": event.get("result") or "",
        "ResultReason": event.get("resultReason") or "",
        "Actor": actor,
        "ActorType": actor_type,
        "ActorIpAddress": ip,
        "Target(s)": "; ".join(
            filter(None, (t.get("userPrincipalName") or t.get("displayName") or "" for t in targets))
        ),
        "ObjectId(s)": "; ".join(filter(None, (t.get("id") or "" for t in targets))),
        "CorrelationId": event.get("correlationId") or "",
        "AdditionalDetails": json.dumps(event.get("additionalDetails") or [], separators=(",", ":")),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Export Entra directory audit events for a month.")
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--run", type=int, default=1)
    ap.add_argument("--config", default=None)
    ap.add_argument("--auth-mode", choices=["interactive", "secret_env", "certificate"],
                    default=None, help="override config.json auth.mode for this run")
    ap.add_argument("--all-actors", action="store_true",
                    help="skip per-actor scoping and pull the full period for every actor")
    args = ap.parse_args()

    cfg = load_config(args.config)
    start, end = month_bounds(args.month)
    start, end, retention_warning = clamp_to_retention(start, end)
    paths = month_paths(args.month, args.run)
    if retention_warning:
        print(f"  WARNING: {retention_warning}")

    actors_path = paths["input"] / f"pim-actors-{args.month}.json"
    actors = []
    if actors_path.exists():
        actors = json.loads(actors_path.read_text(encoding="utf-8"))
    else:
        print(f"  {actors_path.name} not found - run export_pim_activity.py first, "
              f"or pass --all-actors for a full pull.")
        if not args.all_actors:
            return 2

    print(f"Directory audit export for {args.month}  [{start:%Y-%m-%d} .. {end:%Y-%m-%d})")
    client = GraphClient(cfg, auth_mode=args.auth_mode)

    rows: list[dict] = []
    seen_ids: set[str] = set()
    strategy = "per-actor"
    fallback_reason = ""

    if actors and not args.all_actors:
        try:
            for actor in actors:
                flt, params = directory_audit_params(
                    start, end, f"initiatedBy/user/id eq '{actor['id']}'")
                print(f"  actor {actor['userPrincipalName']}")
                for ev in client.get_paged("/auditLogs/directoryAudits", params):
                    if ev.get("id") in seen_ids:
                        continue
                    seen_ids.add(ev.get("id"))
                    rows.append(map_event(ev))
            odata_filter = ("activityDateTime window AND initiatedBy/user/id eq <one query per "
                            f"actor, {len(actors)} actors>")
        except GraphAuthError as exc:
            print(f"\nAUTHENTICATION FAILED\n{exc}", file=sys.stderr)
            return 2
        except RuntimeError as exc:
            print(f"  per-actor filter rejected ({exc}); falling back to full-period pull.")
            strategy, fallback_reason = "full-pull-fallback", str(exc)[:300]
            rows, seen_ids = [], set()

    if strategy != "per-actor" or not actors or args.all_actors:
        keep = {norm_upn(a["userPrincipalName"]) for a in actors} if actors else None
        flt, params = directory_audit_params(start, end)
        odata_filter = flt
        try:
            for ev in client.get_paged("/auditLogs/directoryAudits", params, "(all)"):
                if ev.get("id") in seen_ids:
                    continue
                seen_ids.add(ev.get("id"))
                row = map_event(ev)
                if keep and norm_upn(row["Actor"]) not in keep:
                    continue
                rows.append(row)
        except GraphAuthError as exc:
            print(f"\nAUTHENTICATION FAILED\n{exc}", file=sys.stderr)
            return 2
        if not actors:
            strategy = "full-pull-no-actor-scope"

    rows.sort(key=lambda r: r["Date"])

    out = paths["input"] / f"AuditCsv-{args.month}.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    notes = [f"strategy={strategy}", f"actors_scoped={len(actors)}"]
    if fallback_reason:
        notes.append(f"fallback_reason={fallback_reason}")
    if not rows:
        notes.append("NO ROWS RETURNED - check retention window and permissions")
    if retention_warning:
        notes.append(retention_warning)

    record_manifest(paths["input"], manifest_entry(
        out, source="Microsoft Graph", endpoint="/v1.0/auditLogs/directoryAudits",
        odata_filter=odata_filter, period=args.month, row_count=len(rows),
        notes="; ".join(notes)))

    print(f"\n  wrote {out.name}: {len(rows)} rows ({strategy})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
