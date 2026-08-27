"""Step 3 - correlate PIM activations against directory audit events.

Answers: what did each admin actually do with the privilege they activated?

Method
  1. Load both CSVs. Casefold every UPN (the source mixes 'AdminAB@CONTOSO.COM' and
     'adminab@contoso.com'; a case-sensitive join silently drops matches). All time in UTC.
  2. Drop exact duplicate rows, recording how many. 'requested' and 'completed' events are
     NOT duplicates of each other and are kept distinct.
  2b. Reassemble Graph's chunked audit events. An event whose additionalDetails payload is
     too large for one row is emitted as several rows sharing an 'id' with 'seq' 1..N.
     They are fragments of ONE event; exact-duplicate dropping cannot see them because the
     payload slice and the sub-second Date differ, so left alone they inflate every action
     count downstream.
  3. Anchors = successful 'add-member-to-role-completed-(pim-activation)' events.
     Eligibility grants are a different thing and are not used as anchors.
  4. Window = [activation, activation + activation_window_hours).
  5. Join audit events on actor + window. PIM's own events are excluded from the action
     set so an activation never correlates with itself.
  6. One event can fall inside two overlapping activations by the same admin. Such rows are
     attributed to every overlapping activation and flagged ambiguous_attribution=TRUE; a
     de-duplicated per-actor view keeps the headline totals honest.

Integrity rule: if no audit file is present, activations are reported as
"unknown - no audit data" and never as "no actions taken". Absence of evidence is not
evidence of absence, and nothing here is inferred that the source files do not show.

Outputs (month output/ folder): activations, correlated-actions, uncovered-actions,
exceptions CSVs plus correlation-stats JSON consumed by build_reports.py.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (from_epoch_ms, load_analysis_config, month_bounds, month_folder_name,
                   month_paths, norm_upn, read_manifest)

ACTIVATION_COMPLETED = "add-member-to-role-completed-(pim-activation)"
ACTIVATION_REQUESTED = "add-member-to-role-requested-(pim-activation)"
ELIGIBILITY_COMPLETED = "add-eligible-member-to-role-in-pim-completed-(timebound)"
ELIGIBILITY_REQUESTED = "add-eligible-member-to-role-in-pim-requested-(timebound)"
TIMEBOUND_REQUESTED = "add-member-to-role-in-pim-requested-(timebound)"
DEACTIVATION = "remove-member-from-role-completed-(pim-activation)"
DEACTIVATION_REQUESTED = "remove-member-from-role-requested-(pim-activation)"

# Permanent variants. PIM's whole purpose is time-bound privilege, so an assignment or
# eligibility granted with no expiry is a finding rather than routine bookkeeping.
ELIGIBILITY_COMPLETED_PERMANENT = "add-eligible-member-to-role-in-pim-completed-(permanent)"
ELIGIBILITY_REQUESTED_PERMANENT = "add-eligible-member-to-role-in-pim-requested-(permanent)"
ASSIGNMENT_OUTSIDE_PIM_PERMANENT = "add-member-to-role-outside-of-pim-(permanent)"

# Routine expiry of an activation. Benign, but must be named so it does not read as an
# unrecognised event type.
ACTIVATION_EXPIRED = "remove-member-from-role-(pim-activation-expired)"

# Every action name the analysis understands. Anything outside this set is reported as
# unmapped, because an event type nobody has mapped generates no exceptions and would
# otherwise be silently absent from the findings.
KNOWN_ACTIONS = {
    ACTIVATION_COMPLETED, ACTIVATION_REQUESTED,
    ELIGIBILITY_COMPLETED, ELIGIBILITY_REQUESTED,
    ELIGIBILITY_COMPLETED_PERMANENT, ELIGIBILITY_REQUESTED_PERMANENT,
    ASSIGNMENT_OUTSIDE_PIM_PERMANENT, TIMEBOUND_REQUESTED,
    DEACTIVATION, DEACTIVATION_REQUESTED, ACTIVATION_EXPIRED,
}

SEVERITY = {
    "uncovered_privileged_action": "High",
    "failed_activation": "High",
    "failed_timebound_assignment_request": "High",
    "permanent_assignment_outside_pim": "High",
    "permanent_eligibility_grant": "High",
    "activation_on_behalf_of_other": "Medium",
    "activation_request_not_completed": "Medium",
    "activation_no_actions": "Medium",
    "missing_justification": "Medium",
    "weak_justification": "Medium",
    "global_administrator_activation": "Medium",
    "eligibility_grant_for_review": "Medium",
    "off_hours_activation": "Low",
    "break_glass_activity": "Informational",
}


# --------------------------------------------------------------------------- loading

def load_pim(path: Path) -> tuple[pd.DataFrame, dict]:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    stats = {"pim_file": path.name, "pim_rows_raw": len(df)}

    before = len(df)
    df = df.drop_duplicates().reset_index(drop=True)
    stats["pim_exact_duplicates_dropped"] = before - len(df)
    stats["pim_rows_deduped"] = len(df)

    df["ts"] = pd.to_datetime(pd.to_numeric(df["@timestamp"], errors="coerce"),
                              unit="ms", utc=True)
    stats["pim_rows_unparseable_timestamp"] = int(df["ts"].isna().sum())
    df = df[df["ts"].notna()].reset_index(drop=True)

    df["actor"] = df["Source User"].map(norm_upn)
    df["dest"] = df["Destination User"].map(norm_upn)
    df["action"] = df["Source User Action"].str.strip().str.lower()
    df["outcome"] = df["#event.outcome"].str.strip().str.lower()
    df["role"] = df["Entra Role"].str.strip()
    df["justification"] = df["Justification"].fillna("").str.strip()

    stats["pim_period_observed_start"] = df["ts"].min().isoformat() if len(df) else None
    stats["pim_period_observed_end"] = df["ts"].max().isoformat() if len(df) else None
    stats["pim_distinct_actors"] = int(df["actor"].nunique())
    return df, stats


# Graph splits an audit event whose additionalDetails payload will not fit in one row
# across several rows. Each fragment carries the same {"key":"id"} plus an incrementing
# {"key":"seq"}, with its slice of the payload under {"key":"b"}.
_AD_ID_RE = re.compile(r'\{"key":"id","value":"([^"]*)"\}')
_AD_SEQ_RE = re.compile(r'\{"key":"seq","value":"(\d+)"\}')
_AD_B_RE = re.compile(r'\{"key":"b","value":"(.*)"\}', re.DOTALL)


def _ad_field(text: str, pattern: re.Pattern) -> str:
    m = pattern.search(text or "")
    return m.group(1) if m else ""


def reassemble_chunked_events(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Collapse Graph's multi-row additionalDetails fragments into one row per event.

    A fragmented event appears as N rows sharing an additionalDetails 'id' with 'seq'
    1..N, otherwise identical apart from the payload slice and a sub-second difference in
    Date. Exact-duplicate dropping cannot collapse them for exactly that reason, so
    without this they survive into correlated-actions as apparent repeats of one action
    and inflate every action count that follows.

    Grouping is on id + actor + activity + correlationId rather than id alone, so an id
    reused across genuinely different events can never merge two of them. The payload is
    rebuilt in seq order rather than discarded, and the event is anchored at the earliest
    timestamp in its group.
    """
    stats = {"audit_chunk_fragment_rows": 0, "audit_chunked_events": 0,
             "audit_chunk_rows_collapsed": 0}
    if df.empty or "AdditionalDetails" not in df.columns:
        return df, stats

    ad = df["AdditionalDetails"].fillna("").astype(str)
    frag_id = ad.map(lambda s: _ad_field(s, _AD_ID_RE))
    frag_seq = ad.map(lambda s: _ad_field(s, _AD_SEQ_RE))
    # A row needs both an id and a seq to be a fragment; an id alone is just an event id.
    is_frag = (frag_id != "") & (frag_seq != "")
    if not is_frag.any():
        return df, stats

    def col(name: str) -> pd.Series:
        return (df[name].fillna("").astype(str) if name in df.columns
                else pd.Series([""] * len(df), index=df.index))

    work = df.copy()
    work["_frag_id"] = frag_id
    work["_frag_seq"] = pd.to_numeric(frag_seq, errors="coerce")
    work["_grp"] = list(zip(frag_id, col("Actor").str.casefold(),
                            col("Activity"), col("CorrelationId")))

    keep, fragment_rows, chunked, collapsed = [], 0, 0, 0
    for _, g in work[is_frag].groupby("_grp", sort=False):
        fragment_rows += len(g)
        if len(g) == 1:
            keep.append(g.index[0])
            continue
        chunked += 1
        collapsed += len(g) - 1
        g = g.sort_values("_frag_seq")
        rep = g.index[0]
        payload = "".join(_ad_field(s, _AD_B_RE)
                          for s in g["AdditionalDetails"].fillna("").astype(str))
        if payload:
            work.at[rep, "AdditionalDetails"] = json.dumps([
                {"key": "id", "value": g["_frag_id"].iloc[0]},
                {"key": "reassembled_from_fragments", "value": str(len(g))},
                {"key": "b", "value": payload},
            ])
        if "ts" in work.columns:
            work.at[rep, "ts"] = g["ts"].min()
        keep.append(rep)

    out = pd.concat([work[~is_frag], work.loc[pd.Index(keep)]])
    out = out.drop(columns=["_frag_id", "_frag_seq", "_grp"])
    if "ts" in out.columns:
        out = out.sort_values("ts")
    out = out.reset_index(drop=True)

    stats.update({"audit_chunk_fragment_rows": fragment_rows,
                  "audit_chunked_events": chunked,
                  "audit_chunk_rows_collapsed": collapsed})
    return out, stats


def load_audit(path: Path | None) -> tuple[pd.DataFrame | None, dict]:
    if path is None or not path.exists():
        return None, {"audit_file": None, "audit_available": False, "audit_rows_raw": 0}

    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    stats = {"audit_file": path.name, "audit_available": True, "audit_rows_raw": len(df)}
    source_columns = list(df.columns)

    before = len(df)
    df = df.drop_duplicates().reset_index(drop=True)
    stats["audit_exact_duplicates_dropped"] = before - len(df)

    df["ts"] = pd.to_datetime(df["Date"], errors="coerce", utc=True, format="mixed")
    stats["audit_rows_unparseable_timestamp"] = int(df["ts"].isna().sum())
    df = df[df["ts"].notna()].reset_index(drop=True)

    # One logical event split across rows is not several events. Must run before the
    # correlation join, or each fragment is counted as a separate action taken.
    df, chunk_stats = reassemble_chunked_events(df)
    stats.update(chunk_stats)
    stats["audit_rows_after_reassembly"] = len(df)

    # Rows that no exported field can tell apart - identical in every column but the
    # sub-second timestamp. Counted and reported, NOT dropped: the field that would
    # distinguish them (modifiedProperties, e.g. which app role was granted) is absent
    # from this export's column set, so whether they are repeated events or one event
    # emitted several times cannot be decided from this file. Dropping them could erase
    # real privileged actions; keeping them may overstate volume. The gap is reported.
    content_cols = [c for c in source_columns if c != "Date" and c in df.columns]
    stats["audit_content_identical_rows"] = (
        int(df.duplicated(subset=content_cols).sum()) if content_cols else 0)

    # Graph's per-event id, where the export carries one. Lets a deliverable row be traced
    # to a unique source event, and lets verification prove reassembly actually happened.
    df["event_id"] = (df["AdditionalDetails"].fillna("").astype(str)
                      .map(lambda s: _ad_field(s, _AD_ID_RE))
                      if "AdditionalDetails" in df.columns
                      else pd.Series([""] * len(df), index=df.index))

    df["actor"] = df["Actor"].map(norm_upn)
    # Safe attribute names: itertuples renames columns like 'Target(s)' positionally.
    for safe, original in (("date_raw", "Date"), ("activity", "Activity"),
                           ("category", "Category"), ("service", "Service"),
                           ("result", "Result"), ("target_s", "Target(s)"),
                           ("correlation_id", "CorrelationId")):
        df[safe] = (df[original] if original in df.columns
                    else pd.Series([""] * len(df), index=df.index)).fillna("").astype(str).str.strip()

    # PIM's own bookkeeping is not an "action taken with the privilege".
    is_pim = df["service"].str.casefold().str.contains("privileged identity|^pim$", regex=True)
    stats["audit_pim_service_rows_excluded"] = int(is_pim.sum())
    df = df[~is_pim].reset_index(drop=True)
    stats["audit_rows_eligible_for_correlation"] = len(df)
    return df, stats


# ----------------------------------------------------------------------- correlation

def build_activations(pim: pd.DataFrame, window_hours: int) -> pd.DataFrame:
    anchors = pim[(pim["action"] == ACTIVATION_COMPLETED) &
                  (pim["outcome"] == "success")].copy()
    anchors = anchors.sort_values("ts").reset_index(drop=True)
    anchors["activation_id"] = [f"A{i + 1:04d}" for i in range(len(anchors))]
    anchors["window_start"] = anchors["ts"]
    anchors["window_end"] = anchors["ts"] + timedelta(hours=window_hours)
    return anchors


def correlate(activations: pd.DataFrame, audit: pd.DataFrame | None) -> pd.DataFrame:
    if audit is None or activations.empty or audit.empty:
        return pd.DataFrame(columns=[
            "activation_id", "actor", "Entra Role", "activation_utc", "window_end_utc",
            "minutes_after_activation", "audit_Date", "audit_Activity", "audit_Category",
            "audit_Service", "audit_Result", "audit_Target(s)", "audit_CorrelationId",
            "audit_event_id", "ambiguous_attribution"])

    by_actor = {a: g for a, g in audit.groupby("actor")}
    out = []
    for act in activations.itertuples(index=False):
        g = by_actor.get(act.actor)
        if g is None:
            continue
        hit = g[(g["ts"] >= act.window_start) & (g["ts"] < act.window_end)]
        for ev in hit.itertuples(index=False):
            out.append({
                "activation_id": act.activation_id,
                "actor": act.actor,
                "Entra Role": act.role,
                "activation_utc": act.window_start.isoformat(),
                "window_end_utc": act.window_end.isoformat(),
                "minutes_after_activation": round(
                    (ev.ts - act.window_start).total_seconds() / 60, 1),
                "audit_Date": ev.date_raw,
                "audit_Activity": ev.activity,
                "audit_Category": ev.category,
                "audit_Service": ev.service,
                "audit_Result": ev.result,
                "audit_Target(s)": ev.target_s,
                "audit_CorrelationId": ev.correlation_id,
                "audit_event_id": ev.event_id,
                "_event_key": f"{ev.actor}|{ev.date_raw}|{ev.correlation_id}|{ev.activity}",
            })
    df = pd.DataFrame(out)
    if df.empty:
        return df
    counts = df["_event_key"].value_counts()
    df["ambiguous_attribution"] = df["_event_key"].map(lambda k: counts[k] > 1)
    return df


def find_uncovered(audit: pd.DataFrame | None, activations: pd.DataFrame,
                   pim_actors: set[str], break_glass: set[str]) -> pd.DataFrame:
    """Audit actions by PIM-using admins that fall inside NO activation window.
    Potential standing access or PIM bypass - the highest-value finding here."""
    if audit is None or audit.empty:
        return pd.DataFrame()

    windows: dict[str, list[tuple]] = {}
    for act in activations.itertuples(index=False):
        windows.setdefault(act.actor, []).append((act.window_start, act.window_end))

    rows = []
    for ev in audit.itertuples(index=False):
        if ev.actor not in pim_actors:
            continue  # only judge admins we know use PIM
        covered = any(s <= ev.ts < e for s, e in windows.get(ev.actor, []))
        if covered:
            continue
        rows.append({
            "actor": ev.actor,
            "audit_Date": ev.date_raw,
            "audit_Activity": ev.activity,
            "audit_Category": ev.category,
            "audit_Service": ev.service,
            "audit_Result": ev.result,
            "audit_Target(s)": ev.target_s,
            "audit_CorrelationId": ev.correlation_id,
            "audit_event_id": ev.event_id,
            "is_break_glass": ev.actor in break_glass,
        })
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------ exceptions

def build_exceptions(pim, activations, correlated, uncovered, cfg, audit_available):
    ex = []
    bg = {norm_upn(a) for a in cfg.get("break_glass_accounts", [])}
    tzname = cfg.get("reporting_timezone", "UTC")
    bh = cfg.get("business_hours", {})
    weak_min = int(cfg.get("weak_justification_min_chars", 15))
    weak_phrases = {p.casefold() for p in cfg.get("weak_justification_phrases", [])}

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tzname)
    except Exception:
        from datetime import timezone as _tz
        tz, tzname = _tz.utc, "UTC"

    def add(cls, actor, role, ts, detail, activation_id="", source=""):
        ex.append({"exception_class": cls, "severity": SEVERITY.get(cls, "Medium"),
                   "activation_id": activation_id, "actor": actor, "entra_role": role,
                   "timestamp_utc": ts, "detail": detail, "source_file": source})

    acted = set(correlated["activation_id"]) if not correlated.empty else set()

    for act in activations.itertuples(index=False):
        ts = act.window_start.isoformat()
        local = act.window_start.astimezone(tz)

        if audit_available and act.activation_id not in acted:
            add("activation_no_actions", act.actor, act.role, ts,
                "Role activated but no directory audit activity by this actor inside the "
                "correlation window - candidate for removal", act.activation_id)

        just = (act.justification or "").strip()
        if not just:
            add("missing_justification", act.actor, act.role, ts,
                "Activation recorded with no justification text", act.activation_id)
        elif len(just) < weak_min or just.casefold().strip(" .-") in weak_phrases:
            add("weak_justification", act.actor, act.role, ts,
                f"Justification is thin: {just!r}", act.activation_id)

        if act.role == "Global Administrator":
            add("global_administrator_activation", act.actor, act.role, ts,
                "Global Administrator activated - highest-privilege role, confirm necessity",
                act.activation_id)

        # Self-activation is normal PIM behaviour and is reported as a statistic, not an
        # exception. The anomaly worth reviewing is one account activating for another.
        if act.actor and act.dest and act.actor != act.dest:
            add("activation_on_behalf_of_other", act.actor, act.role, ts,
                f"Activated for a different account ({act.dest}) - confirm this is an "
                f"approved delegation path", act.activation_id)

        if local.weekday() not in bh.get("workdays", [0, 1, 2, 3, 4]) or not (
                bh.get("start_hour", 8) <= local.hour < bh.get("end_hour", 18)):
            add("off_hours_activation", act.actor, act.role, ts,
                f"Activated {local:%Y-%m-%d %H:%M} {tzname} - outside configured business hours",
                act.activation_id)

        if act.actor in bg:
            add("break_glass_activity", act.actor, act.role, ts,
                "Break-glass account activity - reported, not for revocation", act.activation_id)

    # failed activation attempts
    failed_act = pim[(pim["outcome"] == "failure") &
                     (pim["action"].isin([ACTIVATION_COMPLETED, ACTIVATION_REQUESTED]))]
    for r in failed_act.itertuples(index=False):
        add("failed_activation", r.actor, r.role, r.ts.isoformat(),
            f"Activation attempt failed: {r.action}", "")

    # failed time-bound assignment requests - a distinct failure mode from activation
    failed_tb = pim[(pim["outcome"] == "failure") & (pim["action"] == TIMEBOUND_REQUESTED)]
    tb_total = int((pim["action"] == TIMEBOUND_REQUESTED).sum())
    for r in failed_tb.itertuples(index=False):
        add("failed_timebound_assignment_request", r.actor, r.role, r.ts.isoformat(),
            f"Time-bound assignment request failed ({len(failed_tb)} of {tb_total} such "
            f"requests failed this period)", "")

    # requested with no matching completion within 1 hour
    req = pim[(pim["action"] == ACTIVATION_REQUESTED) & (pim["outcome"] == "success")]
    comp = pim[(pim["action"] == ACTIVATION_COMPLETED) & (pim["outcome"] == "success")]
    for r in req.itertuples(index=False):
        near = comp[(comp["actor"] == r.actor) & (comp["role"] == r.role) &
                    (comp["ts"] >= r.ts - timedelta(minutes=5)) &
                    (comp["ts"] <= r.ts + timedelta(hours=1))]
        if near.empty:
            add("activation_request_not_completed", r.actor, r.role, r.ts.isoformat(),
                "Activation requested but no matching completion within 1 hour", "")

    # eligibility grants - an admin handing out eligibility, not an activation
    elig = pim[pim["action"].isin([ELIGIBILITY_COMPLETED, ELIGIBILITY_REQUESTED])]
    for r in elig.itertuples(index=False):
        add("eligibility_grant_for_review", r.actor, r.role, r.ts.isoformat(),
            f"Eligibility granted to {r.dest or 'unknown'} - confirm approved and time-bound", "")

    # Permanent eligibility - eligibility with no expiry, which defeats time-binding.
    # Reported at both request and completion stage, matching the timebound treatment
    # above, so the owner can see whether a request was actually granted.
    elig_perm = pim[pim["action"].isin([ELIGIBILITY_COMPLETED_PERMANENT,
                                        ELIGIBILITY_REQUESTED_PERMANENT])]
    for r in elig_perm.itertuples(index=False):
        stage = "completed" if r.action == ELIGIBILITY_COMPLETED_PERMANENT else "requested"
        add("permanent_eligibility_grant", r.actor, r.role, r.ts.isoformat(),
            f"PERMANENT (no expiry) eligibility {stage} for {r.dest or 'unknown'} - PIM "
            f"eligibility should be time-bound; confirm approved or convert to time-bound", "")

    # Role membership granted permanently and outside PIM altogether - standing access
    # subject to no activation, approval or expiry. Exactly what this review exists to find.
    outside = pim[pim["action"] == ASSIGNMENT_OUTSIDE_PIM_PERMANENT]
    for r in outside.itertuples(index=False):
        add("permanent_assignment_outside_pim", r.actor, r.role, r.ts.isoformat(),
            f"Permanent role assignment made OUTSIDE PIM for {r.dest or 'unknown'} - "
            f"standing privileged access, not subject to activation, approval or expiry", "")

    if uncovered is not None and not uncovered.empty:
        for r in uncovered[~uncovered["is_break_glass"]].itertuples(index=False):
            add("uncovered_privileged_action", r.actor, "", r.audit_Date,
                f"Action '{r.audit_Activity}' taken outside any PIM activation window - "
                f"possible standing access or PIM bypass", "")

    df = pd.DataFrame(ex)
    if not df.empty:
        order = ["High", "Medium", "Low", "Informational"]
        df["_s"] = df["severity"].map({s: i for i, s in enumerate(order)})
        df = df.sort_values(["_s", "exception_class", "timestamp_utc"]).drop(columns="_s")
        df.insert(0, "exception_id", [f"E{i + 1:04d}" for i in range(len(df))])
    return df


# ----------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Correlate PIM activations with audit events.")
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--run", type=int, default=1)
    ap.add_argument("--config", default=None)
    ap.add_argument("--pim-file", default=None, help="override PIM activity CSV path")
    ap.add_argument("--audit-file", default=None, help="override AuditCsv path")
    ap.add_argument("--label", default=None, help="output filename suffix (default: month)")
    args = ap.parse_args()

    cfg = load_analysis_config(args.config)
    paths = month_paths(args.month, args.run)
    label = args.label or args.month
    window_hours = int(cfg.get("activation_window_hours", 8))

    pim_path = Path(args.pim_file) if args.pim_file else \
        paths["input"] / f"entraid-pim-activity-{args.month}.csv"
    if not pim_path.exists():
        print(f"PIM activity file not found: {pim_path}\n"
              f"Run export_pim_activity.py, or place the file in {paths['input']}", file=sys.stderr)
        return 2

    audit_path = Path(args.audit_file) if args.audit_file else \
        paths["input"] / f"AuditCsv-{args.month}.csv"

    print(f"Correlating {args.month}  (window {window_hours}h, config: {cfg['_config_source']})")

    pim, pstats = load_pim(pim_path)
    audit, astats = load_audit(audit_path if audit_path.exists() else None)
    stats = {**pstats, **astats, "month": args.month,
             "month_folder": month_folder_name(args.month, args.run),
             "activation_window_hours": window_hours,
             "reporting_timezone": cfg.get("reporting_timezone", "UTC"),
             "config_source": cfg["_config_source"]}

    # Guard on the analysis side, not just in the exporter's manifest note: a PIM event
    # type nobody has mapped produces no exceptions, so it would otherwise be absent from
    # the findings without anything saying so. Files placed by hand never pass through the
    # exporter at all, so this is the only check that sees them.
    unmapped = (pim.loc[~pim["action"].isin(KNOWN_ACTIONS), "action"]
                .value_counts().to_dict())
    stats["pim_unmapped_actions"] = unmapped
    stats["pim_unmapped_action_rows"] = int(sum(unmapped.values()))

    activations = build_activations(pim, window_hours)
    stats["activations_successful"] = len(activations)
    if len(activations):
        same = (activations["actor"] == activations["dest"]) & (activations["actor"] != "")
        stats["activations_self"] = int(same.sum())
        stats["activations_on_behalf_of_other"] = int((~same).sum())
        stats["activation_requests"] = int(((pim["action"] == ACTIVATION_REQUESTED) &
                                            (pim["outcome"] == "success")).sum())
        stats["timebound_requests_total"] = int((pim["action"] == TIMEBOUND_REQUESTED).sum())
        stats["timebound_requests_failed"] = int(((pim["action"] == TIMEBOUND_REQUESTED) &
                                                  (pim["outcome"] == "failure")).sum())

    correlated = correlate(activations, audit)
    stats["correlated_action_rows"] = len(correlated)
    stats["correlated_ambiguous_rows"] = int(correlated["ambiguous_attribution"].sum()) \
        if not correlated.empty else 0
    stats["correlated_distinct_events"] = int(correlated["_event_key"].nunique()) \
        if not correlated.empty else 0

    pim_actors = set(pim["actor"].unique())
    bg = {norm_upn(a) for a in cfg.get("break_glass_accounts", [])}
    uncovered = find_uncovered(audit, activations, pim_actors, bg)
    stats["uncovered_action_rows"] = 0 if uncovered is None or uncovered.empty else len(uncovered)

    exceptions = build_exceptions(pim, activations, correlated, uncovered, cfg,
                                  astats.get("audit_available", False))
    stats["exception_rows"] = len(exceptions)
    stats["exceptions_by_class"] = (exceptions["exception_class"].value_counts().to_dict()
                                    if not exceptions.empty else {})
    stats["exceptions_by_severity"] = (exceptions["severity"].value_counts().to_dict()
                                       if not exceptions.empty else {})

    # per-activation rollup
    acts_out = activations[["activation_id", "Source User", "actor", "Destination User",
                            "Entra Role", "justification", "outcome"]].copy()
    acts_out["activation_utc"] = activations["window_start"].map(lambda d: d.isoformat())
    acts_out["window_end_utc"] = activations["window_end"].map(lambda d: d.isoformat())
    if not correlated.empty:
        per = correlated.groupby("activation_id").agg(
            correlated_actions=("audit_Activity", "size"),
            distinct_activities=("audit_Activity", "nunique"),
            first_action_minutes=("minutes_after_activation", "min"),
            last_action_minutes=("minutes_after_activation", "max"))
        acts_out = acts_out.merge(per, on="activation_id", how="left")
    else:
        for col in ("correlated_actions", "distinct_activities",
                    "first_action_minutes", "last_action_minutes"):
            acts_out[col] = pd.NA
    acts_out["correlated_actions"] = pd.to_numeric(
        acts_out["correlated_actions"], errors="coerce").fillna(0).astype(int)
    acts_out["correlation_status"] = (
        "unknown - no audit data" if not astats.get("audit_available")
        else acts_out["correlated_actions"].map(
            lambda n: "actions observed" if n > 0 else "no actions observed"))

    # rollups
    stats["by_role"] = (activations["role"].value_counts().to_dict() if len(activations) else {})
    stats["by_actor"] = (activations["Source User"].value_counts().to_dict()
                         if len(activations) else {})
    stats["manifest"] = read_manifest(paths["input"])

    outdir = paths["output"]
    acts_out.to_csv(outdir / f"activations-{label}.csv", index=False)
    correlated.drop(columns=["_event_key"], errors="ignore").to_csv(
        outdir / f"correlated-actions-{label}.csv", index=False)
    (uncovered if uncovered is not None else pd.DataFrame()).to_csv(
        outdir / f"uncovered-actions-{label}.csv", index=False)
    exceptions.to_csv(outdir / f"exceptions-{label}.csv", index=False)
    (outdir / f"correlation-stats-{label}.json").write_text(
        json.dumps(stats, indent=2, default=str), encoding="utf-8")

    print(f"  PIM rows: {stats['pim_rows_raw']} raw -> {stats['pim_rows_deduped']} deduped "
          f"({stats['pim_exact_duplicates_dropped']} exact duplicates dropped)")
    print(f"  successful activations (anchors): {stats['activations_successful']}")
    if astats.get("audit_available"):
        if stats.get("audit_chunk_rows_collapsed"):
            print(f"  audit rows: {stats['audit_rows_raw']} raw -> "
                  f"{stats['audit_rows_after_reassembly']} after reassembling "
                  f"{stats['audit_chunk_fragment_rows']} chunk fragment rows into "
                  f"{stats['audit_chunked_events']} events "
                  f"({stats['audit_chunk_rows_collapsed']} rows collapsed)")
        print(f"  audit rows eligible: {stats.get('audit_rows_eligible_for_correlation')}")
        print(f"  correlated action rows: {stats['correlated_action_rows']} "
              f"({stats['correlated_ambiguous_rows']} ambiguous)")
        print(f"  uncovered actions: {stats['uncovered_action_rows']}")
    else:
        print("  NO AUDIT FILE - activations reported as 'unknown - no audit data'; "
              "no 'unused activation' findings can be made without it")
    print(f"  exceptions: {stats['exception_rows']}")
    if unmapped:
        print(f"  WARNING: {stats['pim_unmapped_action_rows']} row(s) carry a PIM action "
              f"this script does not map, so they generate no findings:")
        for name, n in sorted(unmapped.items(), key=lambda kv: -kv[1]):
            print(f"    {name} = {n}")
    print(f"  wrote 5 files to {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
