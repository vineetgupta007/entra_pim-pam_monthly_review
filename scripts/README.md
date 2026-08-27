# Monthly PIM ↔ Audit correlation workflow

Answers one question each month: **what did each admin actually do with the privilege they
activated?**

## Folder layout

```
August/                     one folder per cycle; August-2 for a re-run of the same month
├── input/                  raw exports, never modified after landing
│   ├── entraid-pim-activity-2026-08.csv
│   ├── pim-actors-2026-08.json      UPN → objectId, scopes step 2
│   ├── AuditCsv-2026-08.csv
│   └── export-manifest.json         provenance: endpoint, filter, row count, sha256
└── output/
    ├── entra-pim-correlation-2026-08.xlsx    8 sheets
    ├── entra-pim-review-summary-2026-08.docx one page
    ├── activations-2026-08.csv
    ├── correlated-actions-2026-08.csv
    ├── uncovered-actions-2026-08.csv
    ├── exceptions-2026-08.csv
    └── correlation-stats-2026-08.json
```

## What is and is not in source control

Committed: the scripts, `config.example.json`, and the docs. That is everything needed to
run a cycle anywhere.

**Never committed** — `.gitignore` excludes all of it:

| Excluded | Why |
|---|---|
| `*/input/`, `*/output/` (every month folder) | Contains privileged account UPNs, object IDs, and justification text — an inventory of who holds admin rights in the tenant |
| `scripts/config.json` | Tenant and client IDs |
| `*.pem`, `*.pfx`, `*.key`, `*.cer`, `.env` | Credentials |
| `*.xlsx`, `*.docx` | Deliverables, which carry the same account data |

Month folders are created on demand, so a fresh clone needs none of them. The client secret
lives only in an environment variable and is never written to disk by these scripts.

Because cycle data is excluded, a fresh clone has **no test fixture**. To confirm a checkout
works, drop any real export into `<Month>/input/` and run with `--skip-export`.

## Two phases, and why

A scheduled run is unattended, so nothing inside it can wait for an approval. The workflow
splits accordingly:

| | Phase A — export | Phase B — review |
|---|---|---|
| Command | `run_month.py --month YYYY-MM --export-only` | `run_month.py --month YYYY-MM --skip-export` |
| Steps | 1–2 | 3–4 |
| Needs | Graph access + credentials | only the files in `input/` |
| Runs as | you, interactively — or Task Scheduler once trusted | you, with Claude driving the skill |
| Writes | `<Month>/input/` | `<Month>/output/` |

Phase B is where judgment happens: triage, month-over-month comparison, and the approval
gate. That is what the `entra-pim-monthly-review` skill drives — see `../skills/`.

## One-time setup

```bash
git clone <repo> && cd entra_pim-pam_monthly_review
python -m venv .venv && source .venv/Scripts/activate     # Windows; source .venv/bin/activate elsewhere
pip install -r requirements.txt                    # Python 3.9+
cp scripts/config.example.json scripts/config.json # then fill in tenant_id and client_id
python scripts/check_auth.py                       # prove access before exporting anything
```

Then install the skill — one copy step, see `../skills/install_skill.md`.

## Authentication

Three modes. Set `auth.mode` in `config.json`, or override for a single run with
`--auth-mode`. Default is `interactive`, so a fresh clone works with no secret to move
between machines.

| Mode | Permission type | Credential | Use for |
|---|---|---|---|
| `interactive` | **Delegated** | none — browser sign-in | day-to-day, fresh workstations |
| `secret_env` | **Application** | secret in `$ENTRA_CLIENT_SECRET` | scheduled runs |
| `certificate` | **Application** | PEM on disk | scheduled runs, preferred over a secret |

### The two permission types are not interchangeable

This is the most common way the setup fails. Register **both** sets on the same app, each
admin-consented:

| | Delegated (interactive) | Application (unattended) |
|---|---|---|
| `AuditLog.Read.All` | ✔ | ✔ |
| `Directory.Read.All` | ✔ | ✔ |
| `RoleManagement.Read.Directory` | ✔ | ✔ |
| Platform config | `http://localhost` redirect URI under **Mobile and desktop applications** | none |
| Who must be authorised | **the signed-in user** | the app itself |

That last row is the trap. In interactive mode Graph checks *your* directory role as well as
the app's consent, so consent alone is not enough — you need **Global Reader, Reports Reader,
Security Reader, Security Administrator, Security Operator, or Global Administrator**. If the
role is PIM-*eligible* rather than active, **activate it first**; eligibility grants nothing.

`check_auth.py` distinguishes these cases by name rather than leaving you with a bare 403.

### Token cache

With `auth.cache_tokens: true` the refresh token is cached so re-runs are silent for days.
It lands outside the repo by default — `%LOCALAPPDATA%\entra-pim-review\msal_cache.json` on
Windows, `~/.cache/entra-pim-review/` elsewhere. **That file is a credential.** Set
`cache_tokens: false` if you would rather sign in every time.

## Before you schedule anything unattended

Run the preflight in both modes. One passing proves nothing about the other, because they
exercise different permission types:

```bash
python scripts/check_auth.py                          # delegated
python scripts/check_auth.py --auth-mode secret_env   # application
```

Only when both are green, point Task Scheduler at `scripts/run_export.cmd` (monthly, day 2,
early — retention is short). Note that **Task Scheduler does not inherit environment
variables from your shell**, so `$env:ENTRA_CLIENT_SECRET` set interactively will not be
visible to it. Use `setx` for a persistent variable, or switch to `certificate` mode, or best
of all run the export from Azure Automation with a managed identity and keep no credential on
the machine at all.

## Running a cycle

```bash
python run_month.py --month 2026-08 --export-only  # PHASE A only
python run_month.py --month 2026-08 --skip-export  # PHASE B only
python run_month.py --month 2026-08                # all four steps
python run_month.py --month 2026-08 --skip-export  # files already in input/
python run_month.py --month 2026-08 --run 2        # second pass → August-2/
```

Steps 1–2 need outbound access to `login.microsoftonline.com` and `graph.microsoft.com`, so
run them from a workstation that has it. Steps 3–4 run anywhere the input files exist.

Individual steps:

```bash
python export_pim_activity.py --month 2026-08
python export_audit_events.py --month 2026-08          # --all-actors for a full-tenant pull
python correlate.py --month 2026-08
python build_reports.py --month 2026-08
```

## How correlation works

1. Both CSVs load with every UPN casefolded — the source mixes `AdminAB@CONTOSO.COM` and
   `adminab@contoso.com`, and a case-sensitive join silently drops matches. All time in UTC.
2. Exact duplicate rows are dropped and counted. `requested` and `completed` events are not
   duplicates of each other and stay distinct.
2b. Graph splits an audit event whose `additionalDetails` payload will not fit in one row
   across several rows, each carrying the same `id` with an incrementing `seq`. These are
   fragments of **one** event, and exact-duplicate dropping cannot see them because the
   payload slice and the sub-second timestamp differ — so left alone they inflate every
   action count downstream. They are grouped on `id` + actor + activity + correlationId
   (never `id` alone, so a reused id cannot merge two real events), the payload is rebuilt
   in `seq` order rather than discarded, and the event is anchored at the earliest
   timestamp in its group. `audit_event_id` is carried into the output CSVs so an auditor
   can trace a row to a unique source event, and so verification can prove reassembly ran.
3. Anchors are successful `add-member-to-role-completed-(pim-activation)` events. Eligibility
   grants are a different thing and are never used as anchors.
4. Window = `[activation, activation + activation_window_hours)`, default 8h.
5. Audit events join on actor + window. PIM's own events are excluded so an activation can
   never correlate with itself.
6. An event inside two overlapping windows for the same admin is attributed to both and
   flagged `ambiguous_attribution`; headline totals use a de-duplicated event count.

**Integrity rule.** With no audit file present, activations read `unknown - no audit data`,
never `no actions taken`. Absence of evidence is not evidence of absence, and nothing is
inferred that the source files do not show.

## Exception classes

| Class | Severity | Meaning |
|---|---|---|
| `uncovered_privileged_action` | High | Action outside every activation window — possible standing access or PIM bypass |
| `failed_activation` | High | Activation attempt failed |
| `failed_timebound_assignment_request` | High | Time-bound assignment request failed |
| `permanent_assignment_outside_pim` | High | Role granted permanently and outside PIM entirely — standing access with no activation, approval or expiry |
| `permanent_eligibility_grant` | High | Eligibility granted with **no expiry**, which defeats time-binding. Raised at both request and completion stage, matching the time-bound treatment, so a request that was never granted is visible |
| `activation_no_actions` | Medium | Privilege activated, nothing done with it — candidate for removal |
| `activation_on_behalf_of_other` | Medium | One account activated for another |
| `activation_request_not_completed` | Medium | Requested, no completion within an hour |
| `missing_justification` / `weak_justification` | Medium | Blank or filler justification |
| `global_administrator_activation` | Medium | Highest-privilege role used |
| `eligibility_grant_for_review` | Medium | Someone was granted eligibility |
| `off_hours_activation` | Low | Outside configured business hours |
| `break_glass_activity` | Informational | Declared break-glass account — reported, not for revocation |

Self-activation is normal PIM behaviour, so it is a summary statistic rather than an
exception. The reviewable anomaly is activating *for another account*.

## Config keys

| Key | Default | Notes |
|---|---|---|
| `activation_window_hours` | 8 | Attribution window length |
| `reporting_timezone` | `UTC` | Used for the off-hours test and report display |
| `business_hours` | 08:00–18:00, Mon–Fri | `workdays` is 0=Monday |
| `break_glass_accounts` | `[]` | **Populate this.** Empty means break-glass accounts surface as uncovered-action findings |
| `weak_justification_min_chars` | 15 | Below this counts as thin |
| `roles_in_scope` | `[]` | Reserved for scoping a future cycle |

## Known limitations

- **The 8-hour window is an approximation.** Real activation start and end times come from
  `roleManagement/directory/roleAssignmentScheduleRequests`
  (`scheduleInfo.expiration`). Adopting that removes the guesswork; the fixed window stays
  as a fallback. This is the highest-value improvement available.
- **Correlation is temporal, not causal.** An action inside a window is not proof the
  activated role authorised it.
- **Some audit rows cannot be told apart.** A row identical to another in every exported
  column except the sub-second timestamp may be a genuinely repeated action or one event
  emitted more than once — the field that would separate them (`modifiedProperties`, e.g.
  which app role was granted) is not in the export's column set. They are **kept, not
  deduplicated**: dropping them could erase real privileged actions, which is the worse
  error. `audit_content_identical_rows` counts them and `verify_cycle.py` reports the
  count, so action volume for those events reads as an upper bound. Exporting
  `modifiedProperties` would resolve this.
- **An unmapped event type generates no findings.** `KNOWN_ACTIONS` in `correlate.py` lists
  every PIM action the analysis handles; anything else is counted into
  `pim_unmapped_actions`, printed as a warning, and carried into the report's limitations.
  Keep it in step with `KNOWN_ACTIVITIES` in `export_pim_activity.py`, which only controls
  the manifest note. The analysis-side check is the one that matters, because a file placed
  by hand never passes through the exporter.
- **Audit retention is short** — commonly 7 days on Entra ID Free, 30 days on P1/P2. Confirm
  your tenant. If it is 30 days, run the export within the first few business days of the
  following month or the period is gone permanently. A diagnostic-settings feed into Log
  Analytics or a SIEM is the durable fix.
- **Recommendations are advisory.** Keep / modify / revoke decisions belong to the role owner
  and are recorded on the workbook's Decisions sheet.

## Verifying a cycle

```bash
python verify_cycle.py --month 2026-08
```

Checks that row counts reconcile from raw source through to the workbook — including that
every audit row dropped along the way is attributable to a named cause (exact duplicate,
unparseable timestamp, chunk fragment, or PIM's own bookkeeping) — that no output sheet has
duplicate rows, that chunked events were reassembled, that exception totals and severity
counts agree between `correlation-stats-<label>.json` and the CSVs, that every Summary
formula recomputed from the source CSVs agrees with stats, and that every input file
appears in `export-manifest.json`.

Two conditions are reported as notes rather than failures, because neither should block a
cycle and both would otherwise go unnoticed: audit rows no exported field can distinguish,
and PIM event types the analysis does not map.

The Summary sheet holds live formulas, and openpyxl writes formulas without computing
them - so a freshly built workbook has no cached formula values. Checks that compared
against cached values therefore used to skip silently while still reporting PASS, letting
a stale or tampered Exceptions sheet clear the gate. Verification now recomputes each
formula from the CSVs and compares it to `correlation-stats`, which is the authority for
every figure the reports use. Cached values are still checked when present, but no check
depends on them, and a run where nothing could be compared fails rather than passes.

## Drafting attestation requests

After findings are approved, and only if asked:

```bash
python draft_attestations.py --month 2026-08
```

Writes one `attestation-<actor>-<label>.md` per actor into `output/`, grouping that actor's
exceptions into review themes, carrying the advisory recommendation for each exception class,
itemising the rows an owner needs to see individually, and ending with an empty decision
table plus the cycle's limitations. Every figure comes from `exceptions-<label>.csv` and
`correlation-stats-<label>.json`, so no count in a draft is typed by hand.

The drafts are **drafts**. Nothing is sent, no decision is pre-filled, and the keep / modify
/ revoke call belongs to the role owner. Sending them is a separate approval.

## Publishing to SharePoint (Phase C)

Publication is a third phase on purpose. It is never part of `run_month.py`, because per
`CLAUDE.md` anything leaving the project folder is a separate approval, and findings are
not final if verification fails. Auto-uploading at the end of Phase B would let a cycle
that failed `verify_cycle.py` reach auditors before anyone read the failure.

```bash
python publish_month.py --month 2026-08 --check                       # prove access only
python publish_month.py --month 2026-08 --dry-run                     # preconditions, no upload
python publish_month.py --month 2026-08 --approved-by "Vineet Gupta"  # publish
```

The whole evidence set is published, mirroring the project layout:

```
<cycle folder>/output/    deliverables, machine-readable CSVs, correlation-stats
<cycle folder>/input/     raw exports and export-manifest.json
```

`input/` is the half that matters most, which is not obvious. The workbook can be
regenerated from the raw export any time by re-running Phase B; the raw export cannot be
regenerated at all, because Entra discards the source audit logs within its retention
window. After that the CSV in `input/` is the only surviving record of the period
anywhere, so keeping it on one workstation while the reproducible artefact goes to a
retention-protected library gets the risk backwards. Publishing both also lets an auditor
recompute the hashes the workbook's `Evidence` sheet cites instead of trusting them.

Input files are hash-checked against `export-manifest.json` immediately before upload. A
file whose content has drifted from its own provenance record blocks publication — note
that `verify_cycle.py` checks only that every input is *listed* in the manifest, not that
its hash still matches, so this is a genuinely additional gate.

Pass `--outputs-only`, or set `sharepoint.publish_inputs` false, to publish deliverables
alone. The receipt records which was done.

Four preconditions, all enforced. Any failure means nothing uploads:

1. `verify_cycle.py` exits zero — re-run at publish time, not trusted from earlier
2. an approver is named with `--approved-by`
3. both deliverables exist and the label is a real period, never `-SAMPLE`
4. the destination folder is empty (a corrected cycle is published as `--run 2`)

On success it writes `publication-receipt-<label>.json` into the cycle's `output/`,
recording the destination URL, each file's sha256 and item ID, the approver, and the
timestamp. The receipt's presence means the cycle has been published.

**Permissions.** Unattended modes need the application permission `Sites.Selected`, plus an
administrator granting this app the `write` role on the one target site. `Sites.Selected`
grants nothing until that second step happens, which is what makes it safer than a
tenant-wide `Sites.ReadWrite.All`. Interactive mode instead requests delegated
`Sites.ReadWrite.All`, and the signed-in user's own library permissions apply on top.

Configure the destination under `sharepoint` in `config.json`. `enabled` stays false until
the library exists, consent is granted, and a retention label is applied — see
`docs/entra-pim-sharepoint-publication-design.docx`.
