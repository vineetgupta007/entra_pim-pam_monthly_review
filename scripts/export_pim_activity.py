"""Step 1 - export PIM activity for a calendar month.

Source:  GET /v1.0/auditLogs/directoryAudits
Filter:  activityDateTime ge <month start> and activityDateTime lt <next month> and
         loggedByService eq 'PIM'

Writes entraid-pim-activity-YYYY-MM.csv into the month's input/ folder using the same
column schema as the reference sample, plus pim-actors-YYYY-MM.json (UPN -> objectId)
which step 2 uses to scope its per-actor audit queries.

Run on a machine with outbound access to login.microsoftonline.com and graph.microsoft.com.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (clamp_to_retention, load_config, manifest_entry, month_bounds,
                   month_paths, record_manifest, slugify_activity, to_epoch_ms)
from graph_client import GraphAuthError, GraphClient, directory_audit_params

COLUMNS = ["@timestamp", "Source User", "Source User Action", "Destination User",
           "Entra Role", "User Action", "Justification", "#event.outcome"]

# Activity display names that are PIM role activations or eligibility grants.
# Keep in step with KNOWN_ACTIONS in correlate.py: a name listed here but unhandled there
# would stop being reported as unmapped while still generating no findings.
KNOWN_ACTIVITIES = {
    "add-member-to-role-requested-(pim-activation)",
    "add-member-to-role-completed-(pim-activation)",
    "add-member-to-role-in-pim-requested-(timebound)",
    "add-eligible-member-to-role-in-pim-requested-(timebound)",
    "add-eligible-member-to-role-in-pim-completed-(timebound)",
    "remove-member-from-role-requested-(pim-activation)",
    "remove-member-from-role-completed-(pim-activation)",
    # Permanent variants - no expiry, so each is an exception rather than routine.
    "add-eligible-member-to-role-in-pim-requested-(permanent)",
    "add-eligible-member-to-role-in-pim-completed-(permanent)",
    "add-member-to-role-outside-of-pim-(permanent)",
    # Routine expiry of an activation.
    "remove-member-from-role-(pim-activation-expired)",
}


def _initiator(event: dict) -> tuple[str, str]:
    """Return (upn, object_id) of whoever initiated the event."""
    by = event.get("initiatedBy") or {}
    user = by.get("user") or {}
    if user:
        return (user.get("userPrincipalName") or "", user.get("id") or "")
    app = by.get("app") or {}
    if app:
        return (app.get("displayName") or app.get("servicePrincipalName") or "",
                app.get("servicePrincipalId") or "")
    return ("", "")


def _targets(event: dict) -> tuple[str, str]:
    """Return (destination_user_upn, entra_role_name) from targetResources."""
    dest, role = "", ""
    for t in event.get("targetResources") or []:
        ttype = (t.get("type") or "").lower()
        if ttype == "user" and not dest:
            dest = t.get("userPrincipalName") or t.get("displayName") or ""
        elif ttype in ("role", "directoryrole", "serviceprincipal") and not role:
            if ttype != "serviceprincipal":
                role = t.get("displayName") or ""
        # PIM writes the role name into modifiedProperties on some event shapes
        if not role:
            for prop in t.get("modifiedProperties") or []:
                if (prop.get("displayName") or "").lower() in ("role.displayname", "roledisplayname",
                                                               "role.objectid"):
                    val = (prop.get("newValue") or "").strip('"')
                    if val and not val.count("-") == 4:
                        role = val
                        break
    return dest, role


def _justification(event: dict) -> str:
    """PIM stores the requester's justification in one of several places."""
    for detail in event.get("additionalDetails") or []:
        if (detail.get("key") or "").lower() in ("justification", "reason", "requestorreason"):
            if detail.get("value"):
                return detail["value"]
    for t in event.get("targetResources") or []:
        for prop in t.get("modifiedProperties") or []:
            if (prop.get("displayName") or "").lower() in ("justification", "reason"):
                val = (prop.get("newValue") or "").strip('"')
                if val:
                    return val
    return event.get("resultReason") or ""


def map_event(event: dict) -> dict:
    upn, _ = _initiator(event)
    dest, role = _targets(event)
    return {
        "@timestamp": to_epoch_ms(event.get("activityDateTime")),
        "Source User": upn,
        "Source User Action": slugify_activity(event.get("activityDisplayName")),
        "Destination User": dest,
        "Entra Role": role,
        "User Action": event.get("operationType") or "",
        "Justification": _justification(event),
        "#event.outcome": event.get("result") or "",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Export Entra PIM activity for a month.")
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--run", type=int, default=1, help="re-run number (August-2 etc.)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--auth-mode", choices=["interactive", "secret_env", "certificate"],
                    default=None, help="override config.json auth.mode for this run")
    args = ap.parse_args()

    cfg = load_config(args.config)
    start, end = month_bounds(args.month)
    start, end, retention_warning = clamp_to_retention(start, end)
    paths = month_paths(args.month, args.run)

    print(f"PIM activity export for {args.month}  [{start:%Y-%m-%d} .. {end:%Y-%m-%d})")
    if retention_warning:
        print(f"  WARNING: {retention_warning}")

    client = GraphClient(cfg, auth_mode=args.auth_mode)
    flt, params = directory_audit_params(start, end, "loggedByService eq 'PIM'")
    try:
        events = list(client.get_paged("/auditLogs/directoryAudits", params, "(PIM)"))
    except GraphAuthError as exc:
        print(f"\nAUTHENTICATION FAILED\n{exc}", file=sys.stderr)
        return 2

    rows, actors, unmapped = [], {}, {}
    for ev in events:
        row = map_event(ev)
        rows.append(row)
        upn, oid = _initiator(ev)
        if upn and oid:
            actors[upn.casefold()] = {"userPrincipalName": upn, "id": oid}
        act = row["Source User Action"]
        if act not in KNOWN_ACTIVITIES:
            unmapped[act] = unmapped.get(act, 0) + 1

    rows.sort(key=lambda r: (r["@timestamp"] or 0))

    out = paths["input"] / f"entraid-pim-activity-{args.month}.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    actors_path = paths["input"] / f"pim-actors-{args.month}.json"
    actors_path.write_text(json.dumps(sorted(actors.values(), key=lambda a: a["userPrincipalName"]),
                                      indent=2), encoding="utf-8")

    # Coverage check - flag a period the export does not actually span.
    notes = []
    if rows:
        first, last = rows[0]["@timestamp"], rows[-1]["@timestamp"]
        from common import from_epoch_ms
        notes.append(f"observed range {from_epoch_ms(first):%Y-%m-%d %H:%M}Z"
                     f" .. {from_epoch_ms(last):%Y-%m-%d %H:%M}Z")
    else:
        notes.append("NO ROWS RETURNED - check retention window and permissions")
    if retention_warning:
        notes.append(retention_warning)
    if unmapped:
        notes.append("unmapped activityDisplayName: "
                     + ", ".join(f"{k}={v}" for k, v in sorted(unmapped.items())))

    record_manifest(paths["input"], manifest_entry(
        out, source="Microsoft Graph", endpoint="/v1.0/auditLogs/directoryAudits",
        odata_filter=flt, period=args.month, row_count=len(rows), notes="; ".join(notes)))

    print(f"\n  wrote {out.name}: {len(rows)} rows, {len(actors)} distinct actors")
    for n in notes:
        print(f"  note: {n}")
    if not rows:
        print("\nWARNING: zero rows. Entra audit log retention is short (commonly 7 days on "
              "Free, 30 on P1/P2) - data for this period may already be gone.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
