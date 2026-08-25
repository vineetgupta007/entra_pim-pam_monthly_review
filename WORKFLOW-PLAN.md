# Workflow Plan — PIM Activity ↔ Directory Audit Correlation

**Status:** DRAFT — awaiting approval
**Author:** Claude, for Vineet Gupta (vgupta@caqh.org)
**Drafted:** 2026-08-25

Purpose: each month, export Entra ID PIM activity and directory audit events for the same
period, then correlate them to answer one question — **what did each admin actually do with
the privilege they activated?**

Decisions confirmed with the owner: exports via Graph API app registration; fixed 8-hour
correlation window; calendar-month periods; outputs = xlsx workbook + docx summary +
machine-readable CSVs + reusable script.

---

## 1. Folder layout

Month folders use the month name, per `CLAUDE.md` (`August`, and `August-2` for a re-run
of the same month).

```
entra_pim-pam_monthly_review/
├── CLAUDE.md
├── WORKFLOW-PLAN.md              ← this file
├── scripts/
│   ├── config.example.json       committed; real config is gitignored
│   ├── graph_client.py           auth + paging
│   ├── export_pim_activity.py    step 1
│   ├── export_audit_events.py    step 2
│   ├── correlate.py              step 3
│   ├── build_reports.py          xlsx + docx
│   ├── run_month.py              orchestrator
│   └── README.md
└── August/
    ├── input/
    │   ├── entraid-pim-activity-2026-08.csv
    │   ├── AuditCsv-2026-08.csv
    │   └── export-manifest.json  provenance: endpoint, filter, row count, sha256, export time
    └── output/
        ├── entra-pim-correlation-2026-08.xlsx
        ├── entra-pim-review-summary-2026-08.docx
        ├── correlated-actions-2026-08.csv
        ├── activations-2026-08.csv
        └── exceptions-2026-08.csv
```

`run_month.py --month 2026-08` creates the folders and runs all three steps.
`--skip-export` runs correlation only, against files you dropped in `input/` by hand.

---

## 2. Step 1 — Export PIM activity

**Source:** `GET /v1.0/auditLogs/directoryAudits`
**Filter:** `activityDateTime ge {month_start}Z and activityDateTime lt {next_month_start}Z and loggedByService eq 'PIM'`
Paged via `@odata.nextLink` until exhausted.

The uploaded sample is this same data reshaped by a log platform (`@timestamp` in epoch ms,
`#event.outcome`). The exporter reproduces that exact schema so the format stays stable:

| Output column | Graph source |
|---|---|
| `@timestamp` | `activityDateTime` → epoch milliseconds (UTC) |
| `Source User` | `initiatedBy.user.userPrincipalName` |
| `Source User Action` | `activityDisplayName`, lowercased, spaces → hyphens |
| `Destination User` | `targetResources[type=User].userPrincipalName` |
| `Entra Role` | `targetResources[type=Role].displayName` |
| `User Action` | `operationType` (Create / Update) |
| `Justification` | `resultReason` / `additionalDetails` justification field |
| `#event.outcome` | `result` (success / failure) |

Verified against the sample: all five distinct `Source User Action` values map cleanly to
Graph `activityDisplayName` values, which confirms the mapping.

**Validation before the file is accepted:** row count > 0; period fully covered (earliest and
latest timestamp inside the month); no unmapped `activityDisplayName`; exact-duplicate count
recorded (see Risk 3).

## 3. Step 2 — Export directory audit events

**Source:** the same `directoryAudits` endpoint, without the `loggedByService` filter — all
directory activity for the period.

Volume control: step 1 runs first and yields the distinct set of actors who activated a role.
Step 2 then queries per actor using `initiatedBy/user/id eq '{objectId}'`, which keeps the pull
proportional to the number of privileged admins rather than to whole-tenant activity. If that
filter is rejected by the tenant, it falls back to a full period pull filtered locally, and
notes the fallback in the manifest.

Written in portal `AuditCsv` column order so it stays diff-able against a manual download:
`Date, Service, Category, Activity, Result, ResultReason, Actor, ActorType, ActorIpAddress,
Target(s), ObjectId(s), CorrelationId, AdditionalDetails`.

## 4. Step 3 — Correlate

1. **Load and normalise.** Parse both files. Casefold every UPN — the sample mixes
   `AdminAB@CONTOSO.COM` and `adminab@contoso.com`, so a case-sensitive join would silently fail.
   All timestamps handled in UTC.
2. **Deduplicate.** Drop exact duplicate rows, recording how many were dropped. Request and
   completed events are kept distinct — they are not duplicates.
3. **Pick activation anchors.** Successful `add-member-to-role-completed-(pim-activation)`
   events. `add-eligible-member-to-role-in-pim-completed-(timebound)` is an admin granting
   *eligibility*, not an activation — routed to its own exception class, not used as an anchor.
4. **Build windows.** `[activation_ts, activation_ts + 8h)`.
5. **Join.** An audit event is attributed to an activation when the actor matches and the
   event timestamp falls inside the window. PIM's own events are excluded from the action set,
   so an activation never correlates with itself.
6. **Handle overlaps.** When one admin holds two overlapping activations, a single audit event
   matches both. Such rows are attributed to every overlapping activation and flagged
   `ambiguous_attribution = TRUE`; a second de-duplicated per-actor-session view is produced
   so action counts are never double-counted in the totals.

### Findings the correlation produces

| Finding | Meaning |
|---|---|
| Activation with zero follow-on actions | Privilege taken and never used — strongest candidate for removal |
| Privileged action with no covering activation | Action taken outside any PIM window — possible standing access or PIM bypass. **Highest-value security finding.** |
| Failed activation attempts | 54 in the sample. Clusters suggest misconfiguration or unauthorised attempts |
| Weak or missing justification | Blank, or generic filler |
| Global Administrator activations | 404 of 606 sample rows (67%) — disproportionate, warrants scrutiny |
| Self-targeted activation | `Source User` == `Destination User`; assess against your approval requirement |
| Off-hours activation | Outside business hours in the reporting timezone |

### Workbook sheets

`Summary` · `Activations` · `Correlated Actions` · `Unmatched Activations` ·
`Uncovered Actions` · `Exceptions`

Header row frozen, autofilter on, no merged cells in data ranges, per `CLAUDE.md`.

## 5. Verification pass (every cycle)

- Row counts reconcile end to end: raw → deduped → anchors → correlated.
- No duplicate assignment rows in any output sheet.
- Exception totals match the `Summary` sheet.
- Every figure traces to a filename plus export date via `export-manifest.json`.
- Correlation spot-check: a sample of matched rows re-verified by hand against the raw CSVs.

---

## 6. Risks and open items

1. **Graph reachability.** My sandbox has allowlisted network access, so `graph.microsoft.com`
   may be blocked from here. If it is, the export scripts are still correct but must run on
   your workstation; correlation and reporting run fine on my side from the files in `input/`.
   I will test reachability first and tell you which way it lands rather than working around it.
2. **Audit log retention is short.** Entra keeps directory audit logs for a limited window —
   commonly 7 days on Free and 30 days on P1/P2. **Please confirm your tenant's retention.**
   If it is 30 days, a monthly cycle has almost no slack: the export must run within days of
   month end or the data is permanently gone. Recommendation: run the export by the 3rd
   business day, and separately consider a diagnostic-settings feed to Log Analytics or your
   SIEM for durable history. This is the single biggest threat to the workflow.
3. **Duplicates in the sample.** 334 of 606 rows are exact duplicates — 55%. I need to know
   whether that is log-platform fan-out or genuinely repeated events before any count derived
   from this data can be trusted.
4. **The fixed 8-hour window is an approximation.** It over-attributes when an admin
   deactivates early and under-attributes if your maximum duration exceeds 8 hours. Real
   activation start and end times are available from
   `roleManagement/directory/roleAssignmentScheduleRequests` (`scheduleInfo.expiration`).
   I recommend adopting that later as a precision upgrade; the 8-hour default stays as fallback.
5. **Secrets.** Client secret or certificate is read from environment variables, never written
   to the repo or any output. Certificate auth preferred over a secret. Nothing sensitive
   leaves the project folder.
6. **Break-glass accounts.** These bypass PIM by design, so they will surface as false-positive
   "standing access" findings until they are listed. Still `TBD` in `CLAUDE.md`.

### Needed from you

| Item | Why |
|---|---|
| Tenant ID + app registration client ID | Graph auth |
| App permissions granted: `AuditLog.Read.All`, `Directory.Read.All`, `RoleManagement.Read.Directory` (application, admin-consented) | Steps 1–2 |
| Client secret or certificate, via environment variable | Graph auth |
| Break-glass account UPNs | Suppress false positives |
| Audit log retention in your tenant | Sets the real deadline |
| Reporting timezone (UTC or US Eastern) | Off-hours flag and report display |
| Cause of the 55% duplicate rate | Count integrity |

---

## 7. Build order once approved

1. Scaffold `scripts/` and the `August/input`, `August/output` folders.
2. Test Graph reachability and authentication. Report the result before going further.
3. Build `export_pim_activity.py`; validate its output against the uploaded sample.
4. Build `export_audit_events.py`.
5. Build `correlate.py`.
6. Build `build_reports.py` (xlsx + docx).
7. Wire up `run_month.py`, write `README.md`.
8. Run a full cycle end to end on real data, then the verification pass.
9. Optionally schedule the monthly run as a recurring task.

Steps 3–8 can run against the uploaded sample as a fixture if Graph access is not ready yet,
so correlation logic gets proven before auth is sorted out.
