"""Shared helpers: config loading, month math, folder layout, provenance manifest."""

from __future__ import annotations

import calendar
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

MONTH_NAMES = list(calendar.month_name)  # index 1..12

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- config

def load_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else PROJECT_ROOT / "scripts" / "config.json"
    if not path.exists():
        raise SystemExit(
            f"Config not found: {path}\n"
            f"Copy scripts/config.example.json to scripts/config.json and fill it in."
        )
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for required in ("tenant_id", "client_id"):
        if not cfg.get(required) or str(cfg[required]).startswith("REPLACE_WITH"):
            raise SystemExit(f"Config value '{required}' is not filled in ({path}).")
    return cfg


ANALYSIS_DEFAULTS = {
    "activation_window_hours": 8,
    "reporting_timezone": "UTC",
    "business_hours": {"start_hour": 8, "end_hour": 18, "workdays": [0, 1, 2, 3, 4]},
    "break_glass_accounts": [],
    "roles_in_scope": [],
    "weak_justification_min_chars": 15,
    "weak_justification_phrases": ["test", "testing", "n/a", "na", "none", "asdf",
                                   "work", "task", "-", "."],
}


def load_analysis_config(path: str | Path | None = None) -> dict:
    """Config for correlation/reporting only - no Graph credentials required, so this
    works even before config.json exists. Falls back to config.example.json, then to
    ANALYSIS_DEFAULTS."""
    cfg = dict(ANALYSIS_DEFAULTS)
    candidates = [Path(path)] if path else [
        PROJECT_ROOT / "scripts" / "config.json",
        PROJECT_ROOT / "scripts" / "config.example.json",
    ]
    for cand in candidates:
        if cand and cand.exists():
            loaded = json.loads(cand.read_text(encoding="utf-8"))
            for key in ANALYSIS_DEFAULTS:
                if key in loaded:
                    cfg[key] = loaded[key]
            cfg["_config_source"] = cand.name
            break
    else:
        cfg["_config_source"] = "built-in defaults"
    return cfg


# ----------------------------------------------------------------------- month math

def month_bounds(month: str) -> tuple[datetime, datetime]:
    """'2026-08' -> (2026-08-01T00:00Z, 2026-09-01T00:00Z). Half-open interval."""
    try:
        year, mon = (int(p) for p in month.split("-"))
    except Exception:
        raise SystemExit(f"--month must look like YYYY-MM, got {month!r}")
    start = datetime(year, mon, 1, tzinfo=timezone.utc)
    end = datetime(year + (mon == 12), (mon % 12) + 1, 1, tzinfo=timezone.utc)
    return start, end


def month_folder_name(month: str, run: int = 1) -> str:
    """'2026-08', run 1 -> 'August'; run 2 -> 'August-2' (per CLAUDE.md convention)."""
    _, mon = (int(p) for p in month.split("-"))
    name = MONTH_NAMES[mon]
    return name if run <= 1 else f"{name}-{run}"


def month_paths(month: str, run: int = 1, root: Path | None = None) -> dict[str, Path]:
    root = root or PROJECT_ROOT
    base = root / month_folder_name(month, run)
    paths = {"base": base, "input": base / "input", "output": base / "output"}
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    return paths


def graph_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def to_epoch_ms(iso: str) -> int | None:
    """Graph ISO8601 -> epoch milliseconds UTC (matches the sample's @timestamp)."""
    if not iso:
        return None
    s = iso.replace("Z", "+00:00")
    if "." in s:  # trim fractional seconds to 6 digits for fromisoformat
        head, _, tail = s.partition(".")
        frac, sign, off = tail.partition("+") if "+" in tail else (tail, "", "")
        s = f"{head}.{frac[:6]}{sign}{off}"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms) -> datetime | None:
    if ms is None or (isinstance(ms, float) and ms != ms):
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


# ------------------------------------------------------------------------ manifest

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def record_manifest(input_dir: Path, entry: dict) -> Path:
    """Append/replace a provenance entry keyed by 'file'. Every deliverable figure
    must be traceable to one of these entries (CLAUDE.md evidence rule)."""
    mpath = input_dir / "export-manifest.json"
    data = {"entries": []}
    if mpath.exists():
        try:
            data = json.loads(mpath.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    entries = [e for e in data.get("entries", []) if e.get("file") != entry.get("file")]
    entries.append(entry)
    data["entries"] = sorted(entries, key=lambda e: e.get("file", ""))
    data["updated_utc"] = datetime.now(timezone.utc).isoformat()
    mpath.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return mpath


def manifest_entry(path: Path, *, source: str, endpoint: str, odata_filter: str,
                   period: str, row_count: int, notes: str = "") -> dict:
    return {
        "file": path.name,
        "source": source,
        "endpoint": endpoint,
        "filter": odata_filter,
        "period": period,
        "row_count": row_count,
        "sha256": sha256_file(path),
        "exported_utc": datetime.now(timezone.utc).isoformat(),
        "notes": notes,
    }


def read_manifest(input_dir: Path) -> dict:
    mpath = input_dir / "export-manifest.json"
    if not mpath.exists():
        return {"entries": []}
    return json.loads(mpath.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- misc

def norm_upn(value) -> str:
    """Casefold UPNs. The source data mixes 'AdminAB@CONTOSO.COM' and 'adminab@contoso.com';
    a case-sensitive join would silently drop matches."""
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.lower() in ("nan", "none") else s.casefold()


def slugify_activity(name: str) -> str:
    """'Add member to role completed (PIM activation)'
        -> 'add-member-to-role-completed-(pim-activation)'"""
    return (name or "").strip().lower().replace(" ", "-")
