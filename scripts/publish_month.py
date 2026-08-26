"""Publish a completed review cycle's deliverables to a SharePoint document library.

PHASE C. This is deliberately NOT part of run_month.py. Per CLAUDE.md, anything that
leaves the project folder is a separate approval, and findings are not final if
verification fails. Automatically uploading at the end of Phase B would break both:
a cycle that failed verify_cycle.py could reach auditors before anyone read the failure.

What is published: the two deliverables only.

    entra-pim-correlation-<label>.xlsx
    entra-pim-review-summary-<label>.docx

Raw input exports and intermediate CSVs stay in the project folder. The workbook's
Evidence sheet already reproduces export-manifest.json in full - file name, source,
endpoint, filter, period, row count, sha256, export timestamp - so an auditor can read
the whole provenance chain from the published workbook and request the raw export
separately, verifying it against the recorded hash.

Preconditions, all of which must hold or nothing uploads:

  1. verify_cycle.py exits zero for this cycle
  2. an approver is named on the command line (--approved-by)
  3. both deliverables exist and carry a real period label, never -SAMPLE
  4. the destination folder does not already hold a published file (unless --force)

Permissions. Unattended modes use the APPLICATION permission Sites.Selected plus an
administrator granting this app 'write' on the one target site - a tenant-wide
Sites.ReadWrite.All grant is not needed and should not be requested. Interactive mode
uses DELEGATED Sites.ReadWrite.All, and the signed-in user's own library permissions
apply on top, which is a useful second gate.

Usage:

    python scripts/publish_month.py --month 2026-05 --check
    python scripts/publish_month.py --month 2026-05 --dry-run
    python scripts/publish_month.py --month 2026-05 --approved-by "Vineet Gupta"
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from common import PROJECT_ROOT, load_config, month_folder_name, month_paths, sha256_file
from graph_client import GRAPH_ROOT, GraphAuthError, GraphClient, GraphPermissionError

SITES_DELEGATED_SCOPE = "https://graph.microsoft.com/Sites.ReadWrite.All"
SAMPLE_MARKERS = ("SAMPLE", "sample", "TEST", "FIXTURE")
LARGE_FILE_BYTES = 4 * 1024 * 1024  # above this Graph needs an upload session


class PublishError(RuntimeError):
    """A precondition failed. Nothing has been uploaded."""


# ------------------------------------------------------------------ preconditions

def deliverables(outdir: Path, label: str) -> list[Path]:
    return [
        outdir / f"entra-pim-correlation-{label}.xlsx",
        outdir / f"entra-pim-review-summary-{label}.docx",
    ]


def check_label(label: str) -> None:
    """A fixture must never reach an audit library."""
    if any(m in label for m in SAMPLE_MARKERS):
        raise PublishError(
            f"Refusing to publish: label {label!r} is a fixture or test run.\n"
            f"  Per CLAUDE.md a -SAMPLE run 'can never be mistaken for a real cycle',\n"
            f"  which means it must not reach an audit library either.\n"
            f"  Publish a real cycle whose label is the period, e.g. 2026-05."
        )


def check_files(outdir: Path, label: str) -> list[Path]:
    files = deliverables(outdir, label)
    missing = [f.name for f in files if not f.exists()]
    if missing:
        raise PublishError(
            f"Refusing to publish: deliverable(s) missing from {outdir}\n"
            f"  missing: {', '.join(missing)}\n"
            f"  Run Phase B first: python scripts/run_month.py --month <YYYY-MM> --skip-export"
        )
    return files


def check_verification(month: str, run: int, label: str) -> str:
    """Re-run the verification gate. Its exit code is the authority, not a cached result."""
    cmd = [sys.executable, str(PROJECT_ROOT / "scripts" / "verify_cycle.py"),
           "--month", month, "--run", str(run), "--label", label]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise PublishError(
            f"Refusing to publish: verify_cycle.py exited {proc.returncode}.\n"
            f"  Findings are not final when verification fails, so they must not be\n"
            f"  published. Fix the failing check and re-verify.\n\n"
            f"{proc.stdout.strip()[-2000:]}"
        )
    return proc.stdout.strip()


def check_approver(approved_by: str | None, sp_cfg: dict) -> str:
    if not sp_cfg.get("require_approver", True):
        return approved_by or "(approval not required by config)"
    if not approved_by or not approved_by.strip():
        raise PublishError(
            "Refusing to publish: no approver named.\n"
            "  Approval of the analysis does not imply approval to publish - it is a\n"
            "  separate decision, and the receipt has to record who made it.\n"
            "  Re-run with:  --approved-by \"Full Name\""
        )
    return approved_by.strip()


def check_config(cfg: dict) -> dict:
    sp = cfg.get("sharepoint") or {}
    if not sp.get("enabled"):
        raise PublishError(
            "Publication is not provisioned yet: sharepoint.enabled is false in config.\n"
            "  Before enabling it, the following must exist:\n"
            "    1. the target site and document library\n"
            "    2. Sites.Selected consent, with this app granted 'write' on that site\n"
            "       (or delegated Sites.ReadWrite.All for interactive runs)\n"
            "    3. a retention label on the library, so published evidence is immutable\n"
            "  See docs/entra-pim-sharepoint-publication-design.docx."
        )
    unset = [k for k in ("site_hostname", "site_path", "library")
             if not sp.get(k) or str(sp[k]).startswith("REPLACE_WITH")]
    if unset:
        raise PublishError(
            f"Publication config incomplete: {', '.join(unset)} not filled in "
            f"under sharepoint in config.json."
        )
    return sp


# ------------------------------------------------------------------ graph helpers

def _headers(client: GraphClient, extra: dict | None = None) -> dict:
    h = {"Authorization": f"Bearer {client.token()}"}
    if extra:
        h.update(extra)
    return h


def _explain_site_403(client: GraphClient, detail: str) -> str:
    if client.is_delegated:
        who = client.signed_in_as or "the signed-in user"
        return (
            f"SharePoint returned 403 Forbidden.\n"
            f"  Graph message: {detail}\n\n"
            f"  In interactive mode two things are checked:\n"
            f"    1. delegated Sites.ReadWrite.All is consented on this app\n"
            f"    2. {who} has write access to the target library in SharePoint itself\n"
            f"  Consent alone is not enough - the user needs library permissions too."
        )
    return (
        f"SharePoint returned 403 Forbidden.\n"
        f"  Graph message: {detail}\n\n"
        f"  In {client.mode} mode this is the app's own authorisation. Sites.Selected\n"
        f"  grants nothing until an administrator authorises this app against the target\n"
        f"  site with the 'write' role. Confirm that grant exists for app "
        f"{client.client_id}."
    )


def graph_get(client: GraphClient, url: str) -> dict:
    resp = client.session.get(url, headers=_headers(client), timeout=60)
    if resp.status_code == 403:
        try:
            detail = (resp.json().get("error") or {}).get("message", "")
        except Exception:
            detail = resp.text[:300]
        raise GraphPermissionError(_explain_site_403(client, detail))
    if resp.status_code == 404:
        raise PublishError(f"Not found: {url}\n  {resp.text[:400]}")
    if resp.status_code != 200:
        raise RuntimeError(f"Graph GET failed (HTTP {resp.status_code}): {resp.text[:600]}")
    return resp.json()


def resolve_drive(client: GraphClient, sp: dict) -> tuple[dict, dict]:
    """Resolve the site, then the named document library (drive) inside it."""
    host, path = sp["site_hostname"], sp["site_path"].rstrip("/")
    site = graph_get(client, f"{GRAPH_ROOT}/sites/{host}:{path}")
    drives = graph_get(client, f"{GRAPH_ROOT}/sites/{site['id']}/drives").get("value", [])
    wanted = sp["library"].strip().casefold()
    for d in drives:
        if (d.get("name") or "").strip().casefold() == wanted:
            return site, d
    names = ", ".join(repr(d.get("name")) for d in drives) or "(none visible)"
    raise PublishError(
        f"Document library {sp['library']!r} not found on {host}{path}.\n"
        f"  Libraries visible to this identity: {names}"
    )


def ensure_folder(client: GraphClient, drive_id: str, segments: list[str]) -> dict:
    """Create each folder segment if absent; return the leaf item."""
    parent = "root"
    item = graph_get(client, f"{GRAPH_ROOT}/drives/{drive_id}/root")
    for seg in segments:
        url = f"{GRAPH_ROOT}/drives/{drive_id}/items/{item['id']}/children"
        resp = client.session.post(
            url,
            headers=_headers(client, {"Content-Type": "application/json"}),
            json={"name": seg, "folder": {},
                  "@microsoft.graph.conflictBehavior": "fail"},
            timeout=60,
        )
        if resp.status_code in (200, 201):
            item = resp.json()
        elif resp.status_code == 409:  # already there
            item = graph_get(
                client,
                f"{GRAPH_ROOT}/drives/{drive_id}/items/{item['id']}:/{seg}:")
        elif resp.status_code == 403:
            try:
                detail = (resp.json().get("error") or {}).get("message", "")
            except Exception:
                detail = resp.text[:300]
            raise GraphPermissionError(_explain_site_403(client, detail))
        else:
            raise RuntimeError(
                f"Could not create folder {seg!r} (HTTP {resp.status_code}): "
                f"{resp.text[:400]}")
        parent = item["id"]
    return item


def folder_children(client: GraphClient, drive_id: str, item_id: str) -> list[dict]:
    return graph_get(
        client, f"{GRAPH_ROOT}/drives/{drive_id}/items/{item_id}/children").get("value", [])


def upload(client: GraphClient, drive_id: str, folder_id: str, path: Path) -> dict:
    """Simple PUT for small files; upload session for anything over 4 MB."""
    size = path.stat().st_size
    if size <= LARGE_FILE_BYTES:
        url = (f"{GRAPH_ROOT}/drives/{drive_id}/items/{folder_id}:/{path.name}:"
               f"/content?@microsoft.graph.conflictBehavior=fail")
        resp = client.session.put(
            url,
            headers=_headers(client, {"Content-Type": "application/octet-stream"}),
            data=path.read_bytes(),
            timeout=300,
        )
        if resp.status_code in (200, 201):
            return resp.json()
        if resp.status_code == 409:
            raise PublishError(
                f"{path.name} already exists in the destination folder.\n"
                f"  Not overwriting. Published evidence should be immutable; publish a\n"
                f"  corrected cycle under the re-run suffix instead (e.g. --run 2)."
            )
        if resp.status_code == 403:
            try:
                detail = (resp.json().get("error") or {}).get("message", "")
            except Exception:
                detail = resp.text[:300]
            raise GraphPermissionError(_explain_site_403(client, detail))
        raise RuntimeError(f"Upload of {path.name} failed "
                           f"(HTTP {resp.status_code}): {resp.text[:400]}")

    # Large file: create a session and send in chunks.
    url = (f"{GRAPH_ROOT}/drives/{drive_id}/items/{folder_id}:/{path.name}:"
           f"/createUploadSession")
    resp = client.session.post(
        url,
        headers=_headers(client, {"Content-Type": "application/json"}),
        json={"item": {"@microsoft.graph.conflictBehavior": "fail"}},
        timeout=60,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Could not open upload session for {path.name} "
                           f"(HTTP {resp.status_code}): {resp.text[:400]}")
    session_url = resp.json()["uploadUrl"]
    chunk = 5 * 1024 * 1024
    with path.open("rb") as fh:
        start = 0
        while start < size:
            block = fh.read(chunk)
            end = start + len(block) - 1
            put = requests.put(
                session_url,
                headers={"Content-Length": str(len(block)),
                         "Content-Range": f"bytes {start}-{end}/{size}"},
                data=block,
                timeout=300,
            )
            if put.status_code not in (200, 201, 202):
                raise RuntimeError(f"Chunk {start}-{end} of {path.name} failed "
                                   f"(HTTP {put.status_code}): {put.text[:300]}")
            if put.status_code in (200, 201):
                return put.json()
            start = end + 1
    raise RuntimeError(f"Upload session for {path.name} ended without a completed item.")


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(
        description="PHASE C: publish an approved cycle's deliverables to SharePoint.")
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--run", type=int, default=1, help="re-run number (2 -> August-2)")
    ap.add_argument("--label", default=None, help="output filename suffix (default: month)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--auth-mode", choices=("interactive", "secret_env", "certificate"),
                    default=None, help="override config.json auth.mode for this run")
    ap.add_argument("--approved-by", default=None,
                    help="name of the person approving publication (recorded in the receipt)")
    ap.add_argument("--dry-run", action="store_true",
                    help="run every precondition and show the destination, upload nothing")
    ap.add_argument("--check", action="store_true",
                    help="test SharePoint access only; no preconditions, no upload")
    ap.add_argument("--force", action="store_true",
                    help="proceed even if the destination folder is non-empty (still never overwrites)")
    args = ap.parse_args()

    label = args.label or args.month
    paths = month_paths(args.month, args.run)
    outdir = paths["output"]
    folder = month_folder_name(args.month, args.run)

    cfg = load_config(args.config)
    print(f"Publish {args.month} ({folder}, label {label})  ->  SharePoint   PHASE C")

    try:
        sp = check_config(cfg)
    except PublishError as exc:
        print(f"\n{exc}")
        return 2

    client = GraphClient(cfg, auth_mode=args.auth_mode)
    if client.is_delegated and SITES_DELEGATED_SCOPE not in client.delegated_scopes:
        client.delegated_scopes.append(SITES_DELEGATED_SCOPE)

    # --check: prove access and stop. Useful before asking an admin for anything.
    if args.check:
        try:
            site, drive = resolve_drive(client, sp)
        except (GraphAuthError, GraphPermissionError, PublishError) as exc:
            print(f"\n{exc}")
            return 2
        print(f"  site:    {site.get('displayName')}  ({site.get('webUrl')})")
        print(f"  library: {drive.get('name')}  (drive {drive['id'][:20]}...)")
        print("\n  Access OK. This proves read access; the first real upload proves write.")
        return 0

    # Preconditions. Any failure means nothing was uploaded.
    try:
        check_label(label)
        files = check_files(outdir, label)
        approver = check_approver(args.approved_by, sp)
        print("\n  Re-running the verification gate...")
        verify_out = check_verification(args.month, args.run, label)
        passed = [ln for ln in verify_out.splitlines() if "passed" in ln]
        print(f"    {passed[-1].strip() if passed else 'verification passed'}")
    except PublishError as exc:
        print(f"\n{exc}")
        return 2

    segments = [s for s in [sp.get("folder_prefix"), args.month if args.run <= 1
                            else f"{args.month}-{args.run}"] if s]
    print(f"\n  approver: {approver}")
    print(f"  files:")
    for f in files:
        print(f"    {f.name}  ({f.stat().st_size:,} bytes, sha256 {sha256_file(f)[:16]}...)")
    print(f"  destination: {sp['site_hostname']}{sp['site_path']} / "
          f"{sp['library']} / {'/'.join(segments)}")

    if args.dry_run:
        print("\n  DRY RUN - preconditions passed, nothing uploaded.")
        return 0

    try:
        site, drive = resolve_drive(client, sp)
        target = ensure_folder(client, drive["id"], segments)
        existing = folder_children(client, drive["id"], target["id"])
        if existing and not args.force:
            names = ", ".join(c.get("name", "?") for c in existing)
            print(f"\nRefusing to publish: destination folder already contains: {names}\n"
                  f"  Published evidence should be immutable. If this cycle was corrected,\n"
                  f"  publish it as a re-run (--run 2) so the original stays intact.\n"
                  f"  Use --force only if you are certain the folder is safe to add to.")
            return 2

        uploaded = []
        for f in files:
            item = upload(client, drive["id"], target["id"], f)
            uploaded.append({
                "file": f.name,
                "sha256_local": sha256_file(f),
                "size_bytes": f.stat().st_size,
                "item_id": item.get("id"),
                "web_url": item.get("webUrl"),
                "etag": item.get("eTag"),
            })
            print(f"    uploaded {f.name}")
    except (GraphAuthError, GraphPermissionError, PublishError) as exc:
        print(f"\n{exc}")
        return 2

    receipt = {
        "month": args.month,
        "run": args.run,
        "label": label,
        "approved_by": approver,
        "published_utc": datetime.now(timezone.utc).isoformat(),
        "auth_mode": client.mode,
        "published_as": client.signed_in_as or f"application {client.client_id}",
        "site_web_url": site.get("webUrl"),
        "library": drive.get("name"),
        "folder": "/".join(segments),
        "folder_web_url": target.get("webUrl"),
        "files": uploaded,
        "note": ("Deliverables only. Provenance for every figure is inside the workbook's "
                 "Evidence sheet, which reproduces export-manifest.json."),
    }
    receipt_path = outdir / f"publication-receipt-{label}.json"
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")

    print(f"\n  receipt: {receipt_path.name}")
    print(f"  folder:  {target.get('webUrl')}")
    print(f"\nPublished {len(uploaded)} file(s).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Nothing further was uploaded.")
        sys.exit(130)
