# CLAUDE.md

Guidance for Claude when working in this project.

## Project

**entra-pim_pam-montly-review** — recurring review of Microsoft Entra ID Privileged Identity Management (PIM) and privileged access (PAM) assignments. Run monthly to confirm that every eligible and active privileged assignment is still justified, correctly scoped, and time-bound.

Owner: Vineet Gupta (vgupta@caqh.org), CAQH.

## Core Workflow Rules

**Always present a written plan and wait for Approval before beginning a multistep task**

**Running the monthly review.** When Vineet asks to run the monthly PIM review, process a month's PIM export, review privileged access for a period, or asks what admins did with the privilege they activated — read `skills/entra-pim-monthly-review/SKILL.md` and follow it exactly, start to finish. That file is the authoritative procedure for Phase B: preconditions, run order, the verification gate, triage, month-over-month comparison, and the approval gate before anything is finalised. Do not improvise the steps from this file alone, and do not skip the approval gate.

A copy also sits at `.claude/skills/entra-pim-monthly-review/` for Claude Code, where project skills load automatically and `/entra-pim-monthly-review` works. Cowork does not load skills from the project folder, so in Cowork this instruction is what wires the procedure in. Both copies must stay in sync — `skills/` is canonical; re-copy after editing it.

## Environment & data sources

> Fill in the specifics below; Claude should ask rather than assume when a value is still `TBD`.

- **Tenant(s) in scope:** TBD
- **Directory roles in scope:** TBD (typically Global Administrator, Privileged Role Administrator, Security Administrator, Exchange/SharePoint Administrator, Application Administrator, User Administrator)
- **Azure resource / group scopes in scope:** TBD
- **Break-glass accounts (excluded from revocation, still reported):** TBD
- **Input data:** Exported from Microsoft Graph by `scripts/export_pim_activity.py` and `scripts/export_audit_events.py`, or downloaded from the Entra portal and placed by hand. One folder per cycle named for the month (`August`); a re-run of the same month gets a suffix (`August-2`). Each month folder has an `input/` and an `output/` subfolder.

  `<Month>/input/` — raw exports, never modified after they land:
  - `entraid-pim-activity-YYYY-MM.csv` — PIM activity: activations, requests, eligibility grants. Columns: `@timestamp` (epoch ms UTC), `Source User`, `Source User Action`, `Destination User`, `Entra Role`, `User Action`, `Justification`, `#event.outcome`
  - `AuditCsv-YYYY-MM.csv` — Entra directory audit events for the same period, in portal `AuditCsv` column order
  - `pim-actors-YYYY-MM.json` — UPN → objectId map written by step 1; scopes step 2's per-actor queries
  - `export-manifest.json` — provenance for every input file: endpoint, filter, period, row count, sha256, export timestamp. Files placed by hand are registered with `scripts/register_input.py`.

  Period is the **calendar month**. Files placed manually must still be registered in the manifest — every figure in a deliverable has to trace to a source file.
- **Entra Graph API Access/Connector:** To export data from Entra ID, ask for authentication. If authentication fails, say so rather than working around it. Three auth modes, set by `auth.mode` in `scripts/config.json` (gitignored) or `--auth-mode` for one run:
  - `interactive` (default) — browser sign-in, **delegated** permissions. The signed-in user must hold Global Reader / Reports Reader / Security Reader or equivalent, *active not merely PIM-eligible*; app consent alone is not enough. Needs a `http://localhost` redirect URI under "Mobile and desktop applications".
  - `secret_env` — unattended, **application** permissions, secret from `$ENTRA_CLIENT_SECRET`.
  - `certificate` — unattended, preferred over a secret.

  Delegated and application grants of the same three permission names (`AuditLog.Read.All`, `Directory.Read.All`, `RoleManagement.Read.Directory`) are **not interchangeable** — register and consent both sets. Run `scripts/check_auth.py` in each mode before relying on it; no secret is ever written to disk.
- **Audit log retention is the binding constraint.** Entra keeps directory audit logs for a short window — commonly 7 days on Free, 30 days on P1/P2 (confirm for this tenant). Run the export within the first few business days of the following month or the period is gone permanently.
- **Connectors:** Atlassian (Jira/Confluence) and other MCP servers require OAuth authorization before use. If a connector is unauthorized, say so rather than working around it.

## Monthly review process

1. **Collect** — confirm the current month's export files are present and cover the full review period. Flag missing or stale files instead of proceeding.
2. **Reconcile** — join assignments against the approved privileged-access baseline. Identify: new assignments, removed assignments, and unchanged assignments.
3. **Analyze** — flag exceptions:
   - Permanent/active assignments that should be eligible-only
   - Assignments with no activation during the period (candidates for removal)
   - Assignments without expiration or justification
   - Accounts that are disabled, offboarded, or outside the approved owning team
   - Activations without an approval record or ticket reference
   - Standing access held by service principals or shared accounts
4. **Attest** — route each exception to the role owner for keep / modify / revoke. Record the decision, decider, and date.
5. **Report** — produce the deliverables below.
6. **Track remediation** — list open revocations with target dates; carry unresolved items into next month's cycle.

Deadline: complete by the **10th business day** of the following month unless stated otherwise. Note that audit log retention (above) may force the *export* step much earlier than this.

## Correlation workflow (`scripts/`)

Steps 1–3 of the process above are automated. Full detail in `scripts/README.md`.

**Two phases**, because a scheduled run is unattended and cannot wait for an approval:

```bash
python scripts/check_auth.py                                 # prove access first
python scripts/run_month.py --month 2026-08 --export-only    # PHASE A: steps 1-2
python scripts/run_month.py --month 2026-08 --skip-export    # PHASE B: steps 3-4
python scripts/verify_cycle.py --month 2026-08               # verification pass
```

Phase A needs Graph credentials and runs as Vineet interactively, or unattended via
`scripts/run_export.cmd` in Task Scheduler once `check_auth.py` passes in `secret_env` mode.
Phase B needs only the files in `input/` and is driven by the `entra-pim-monthly-review`
skill in `skills/` — that is where triage, month-over-month comparison, and the approval
gate live. Claude should never perform the export as part of Phase B; if inputs are missing,
report the gap and offer the Phase A command.

The correlation answers: **what did each admin actually do with the privilege they activated?** Successful activations anchor a window of `activation_window_hours` (default 8); directory audit events by the same actor inside that window are attributed to it. PIM's own events are excluded so an activation never correlates with itself.

Rules the scripts enforce, which apply to any manual analysis too:

- **Casefold every UPN before joining.** The source data mixes `AdminAB@CONTOSO.COM` and `adminab@contoso.com`; a case-sensitive join silently drops matches.
- **Deduplicate, and report how many were dropped.** The log platform emits each event 2–8 times. `requested` and `completed` events are *not* duplicates of each other.
- **With no audit file, activations read `unknown - no audit data`, never `no actions taken`.** Absence of evidence is not evidence of absence.
- **Correlation is temporal, not causal.** An action inside a window is not proof the activated role authorised it.
- Self-activation is normal PIM behaviour and is a summary statistic, not an exception. The reviewable anomaly is activating *for another account*.
- The fixed window is an approximation. Real activation start/end times come from Graph `roleManagement/directory/roleAssignmentScheduleRequests` (`scheduleInfo.expiration`) — the highest-value improvement available to this workflow.

## Output formats & conventions

All deliverables go in `<Month>/output/`.

- **Findings workbook** — `.xlsx`, named `entra-pim-correlation-YYYY-MM.xlsx`. Sheets: `Summary`, `Activations`, `Correlated Actions`, `Unmatched Activations`, `Uncovered Actions`, `Exceptions`, `Decisions`, `Evidence`. Arial throughout, freeze header row, autofilter on, no merged cells in data ranges. Summary figures are live formulas over the data sheets, never Python-computed literals.
- **Executive summary** — `.docx`, named `entra-pim-review-summary-YYYY-MM.docx`. One page: scope, counts (distinct events, activations, exceptions raised, revoked, retained), notable risks, remediation status, limitations.
- **Machine-readable CSVs** — `activations-YYYY-MM.csv`, `correlated-actions-YYYY-MM.csv`, `uncovered-actions-YYYY-MM.csv`, `exceptions-YYYY-MM.csv`, plus `correlation-stats-YYYY-MM.json` which carries every count used in the reports.
- **Evidence** — raw source exports stay unmodified in `input/`; the workbook's `Evidence` sheet reproduces `export-manifest.json` so every figure cites a file name, row count, sha256, and export date.
- Dates as `YYYY-MM-DD`. Period labels as `YYYY-MM`. Month folders use the month name (`August`, `August-2`).
- A test or fixture run uses a `-SAMPLE` suffix in place of the period label so it can never be mistaken for a real cycle.

## Working rules for Claude

- **Never invent counts, names, or assignments.** Every figure in a deliverable must trace to a source file. If data is missing, report the gap.
- Treat account names, UPNs, and object IDs as sensitive: keep them inside deliverables, don't send them to external services.
- Show the reconciliation logic (which rows matched, which didn't) before presenting conclusions.
- Recommendations are advisory — revocation decisions belong to the role owner, not to Claude.
- Include a verification pass on every cycle: row counts reconcile between source and workbook, no duplicate assignment rows, exception totals match the summary.
