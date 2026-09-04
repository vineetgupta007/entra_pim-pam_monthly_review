"""Preflight - prove Graph access works before trusting an unattended export.

  python check_auth.py                        # uses config.json's auth.mode
  python check_auth.py --auth-mode interactive
  python check_auth.py --auth-mode secret_env
  python check_auth.py --auth-mode token_passthrough   # after running Get-GraphToken.ps1
                                                        # in the SAME shell

Run it once interactively, then once in secret_env mode. Only schedule the export after
BOTH come back green - that is the whole point of this script.

Checks, in order, stopping at the first failure:
  1. config.json present and filled in
  2. network reachability to login.microsoftonline.com and graph.microsoft.com
  3. authentication (browser sign-in, or client credentials)
  4. who we authenticated as, and in delegated mode which directory roles that user holds
  5. a real audit-log read: one row from auditLogs/directoryAudits
  6. a PIM-filtered read, since that is what the export actually issues

Nothing is written and nothing is exported.
"""

from __future__ import annotations

import argparse
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import graph_time, load_config
from graph_client import (AUDIT_CAPABLE_ROLES, DELEGATED_SCOPES, GRAPH_ROOT,
                          REQUIRED_APP_PERMISSIONS, GraphAuthError, GraphClient,
                          GraphPermissionError, default_token_cache_path)

PASS, FAIL, WARN, INFO = "PASS", "FAIL", "WARN", "INFO"


def emit(status: str, title: str, detail: str = "") -> None:
    print(f"  [{status}] {title}")
    for line in (detail or "").splitlines():
        if line.strip():
            print(f"         {line}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify Graph auth and permissions.")
    ap.add_argument("--config", default=None)
    ap.add_argument("--auth-mode",
                    choices=["interactive", "secret_env", "certificate", "token_passthrough"],
                    default=None, help="override config.json for this run")
    args = ap.parse_args()

    print("\nEntra Graph access preflight")
    print("=" * 72)

    # 1 - config
    try:
        cfg = load_config(args.config)
    except SystemExit as exc:
        emit(FAIL, "Configuration", str(exc))
        return 2
    mode = args.auth_mode or (cfg.get("auth") or {}).get("mode", "interactive")
    emit(PASS, "Configuration", f"tenant {cfg['tenant_id']}\nclient {cfg['client_id']}\n"
                                f"auth mode: {mode}")

    # 2 - reachability
    unreachable = []
    for host in ("login.microsoftonline.com", "graph.microsoft.com"):
        try:
            socket.create_connection((host, 443), timeout=10).close()
        except OSError as exc:
            unreachable.append(f"{host}: {exc}")
    if unreachable:
        emit(FAIL, "Network reachability",
             "\n".join(unreachable) +
             "\nA proxy or firewall is blocking Graph. The export cannot run from here.")
        return 2
    emit(PASS, "Network reachability", "login.microsoftonline.com and graph.microsoft.com "
                                       "both reachable on 443")

    # 3 - authentication
    client = GraphClient(cfg, verbose=True, auth_mode=args.auth_mode)
    if client.is_delegated:
        cache_note = ("cached at " + str(default_token_cache_path())
                      if (cfg.get("auth") or {}).get("cache_tokens")
                      else "not cached (cache_tokens is false)")
        print(f"\n  Interactive sign-in - a browser window will open. Tokens {cache_note}.")
    try:
        client.token()
    except GraphAuthError as exc:
        emit(FAIL, "Authentication", str(exc))
        return 2
    emit(PASS, "Authentication", f"token acquired in {mode} mode")

    # 4 - identity and, for delegated auth, the user's directory roles
    try:
        who = client.whoami()
    except GraphPermissionError as exc:
        emit(FAIL, "Identity", str(exc))
        return 2
    except Exception as exc:
        emit(WARN, "Identity", f"could not read identity: {exc}")
        who = {}

    if who.get("kind") == "user":
        emit(PASS, "Signed in as", f"{who.get('displayName')} <{who.get('upn')}>")
        roles = client.my_directory_roles()
        capable = [r for r in roles if r in AUDIT_CAPABLE_ROLES]
        if capable:
            emit(PASS, "Directory roles", f"holds: {', '.join(capable)}")
        elif roles:
            emit(WARN, "Directory roles",
                 f"holds: {', '.join(roles)}\n"
                 f"None of these is normally sufficient to read audit logs. Expect a 403 at "
                 f"the next step.\nOne of these is needed: {', '.join(AUDIT_CAPABLE_ROLES)}")
        else:
            emit(WARN, "Directory roles",
                 "No active directory roles found for this user.\n"
                 "If your roles are PIM-eligible rather than active, ACTIVATE one before "
                 "exporting - eligibility alone does not grant access.\n"
                 f"Sufficient roles: {', '.join(AUDIT_CAPABLE_ROLES)}")
    elif who:
        emit(PASS, "Authenticated as", f"application {who.get('client_id')} (no user context)")

    emit(INFO, "Tenant", client.tenant_name())

    # 5 - a real audit read
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=2)
    flt = (f"activityDateTime ge {graph_time(start)} and "
           f"activityDateTime lt {graph_time(end)}")
    try:
        payload = client._get(f"{GRAPH_ROOT}/auditLogs/directoryAudits",
                              {"$filter": flt, "$top": "1"})
    except GraphPermissionError as exc:
        emit(FAIL, "Audit log read", str(exc))
        return 2
    except Exception as exc:
        emit(FAIL, "Audit log read", f"{exc}")
        return 2
    rows = payload.get("value") or []
    if rows:
        emit(PASS, "Audit log read",
             f"read {len(rows)} row - '{rows[0].get('activityDisplayName')}' at "
             f"{rows[0].get('activityDateTime')}")
    else:
        emit(WARN, "Audit log read",
             "the call succeeded but returned no rows for the last 2 days.\n"
             "Permissions are fine. Either the tenant was quiet, or retention is shorter "
             "than expected - worth confirming before you rely on a monthly window.")

    # 6 - the PIM-filtered read the exporter actually issues
    try:
        payload = client._get(f"{GRAPH_ROOT}/auditLogs/directoryAudits",
                              {"$filter": f"{flt} and loggedByService eq 'PIM'", "$top": "1"})
        n = len(payload.get("value") or [])
        emit(PASS, "PIM-filtered read",
             f"loggedByService eq 'PIM' accepted ({n} row(s) in the last 2 days)")
    except Exception as exc:
        emit(FAIL, "PIM-filtered read",
             f"{exc}\nThe unfiltered read worked, so this is the filter itself. The exporter "
             f"depends on it.")
        return 2

    print("=" * 72)
    print(f"  Ready. Export with:  python run_month.py --month YYYY-MM --export-only"
          f"{'' if not args.auth_mode else f' --auth-mode {args.auth_mode}'}")
    if client.is_delegated:
        print("\n  This was a DELEGATED check. Before scheduling an unattended export, run:")
        print("      python check_auth.py --auth-mode secret_env")
        print(f"  which needs the same three names granted as APPLICATION permissions: "
              f"{', '.join(REQUIRED_APP_PERMISSIONS)}")
    elif mode == "token_passthrough":
        print("\n  Unattended access confirmed for THIS token - but it expires in under an "
              "hour and is not auto-renewed.")
        print("  run_export.cmd is hardcoded to --auth-mode secret_env and does NOT call "
              "Get-GraphToken.ps1 - scheduling it as-is will NOT use this mode.")
        print("  For unattended token_passthrough, a scheduled task must run "
              "Get-GraphToken.ps1 and run_month.py back-to-back in one job, in that order, "
              "every time - there is currently no .cmd wrapper that does this for you.")
    else:
        print("\n  Unattended access confirmed. Safe to schedule run_export.cmd.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
