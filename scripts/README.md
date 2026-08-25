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

Checks that row counts reconcile from raw source through to the workbook, that no output
sheet has duplicate rows, that exception totals match the Summary sheet, and that every
input file appears in `export-manifest.json`.
