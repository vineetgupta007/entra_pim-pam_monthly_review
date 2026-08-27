---
name: entra-pim-monthly-review
description: >
  Phase B of the monthly Entra PIM/PAM privileged access review: correlate an already-exported
  month of PIM activity against directory audit events, build the findings workbook and
  executive summary, run the verification pass, triage the exceptions, and compare against
  prior months before asking the owner to approve. Use when someone says "run the monthly PIM
  review", "process the August PIM export", "review privileged access for last month", asks
  what admins did with the privilege they activated, or invokes /entra-pim-monthly-review.
  Requires the month's exports to already exist in <Month>/input/; if they do not, explain the
  Phase A export command and offer to run it rather than proceeding. Do not use this skill to
  perform the export itself, to revoke or modify any assignment, or for Entra work unrelated
  to PIM activation review.
---

# Monthly Entra PIM / PAM review — Phase B

You are running the review half of a two-phase workflow. Phase A (export) happens outside
this skill, because it needs Graph credentials. Your job starts once the files are on disk.

**The scripts own every number. You own judgment.** Never recompute a count by hand or
estimate one — `correlate.py` is the single source of truth, and `verify_cycle.py` is the
gate. If you find yourself doing arithmetic on the raw CSVs to produce a figure for the
report, stop: that figure belongs in the scripts.

## Non-negotiables

These come from `CLAUDE.md` and override any instinct to be helpful:

- **Never invent counts, names, or assignments.** Every figure traces to a source file.
  If data is missing, report the gap instead of filling it.
- **No audit file means "unknown", never "no actions taken."** Absence of evidence is not
  evidence of absence.
- **Do not present findings as final if `verify_cycle.py` exits non-zero.** Report which
  check failed and stop.
- **Recommendations are advisory.** Keep / modify / revoke decisions belong to the role
  owner. You never revoke anything, and you never write to Entra.
- **Present a written plan and wait for approval** before the finalize step.
- Treat UPNs, object IDs, and justification text as sensitive. They stay in the project
  folder and are never sent to an external service.

## Step 1 — Establish which cycle, and check the inputs

Work out the target month. "Last month" is relative to today; if it is ambiguous, ask.
Month folders use the month name (`August`), with `August-2` for a re-run.

Confirm `<Month>/input/` contains:

| File | Required |
|---|---|
| `entraid-pim-activity-YYYY-MM.csv` | yes — nothing runs without it |
| `AuditCsv-YYYY-MM.csv` | no, but its absence caps what the review can conclude |
| `export-manifest.json` with an entry per CSV | yes — the evidence chain |

**If the PIM activity file is missing**, do not proceed. Say so, and offer:

```
python scripts/check_auth.py                              # prove Graph access first
python scripts/run_month.py --month YYYY-MM --export-only # Phase A
```

Offer to run those, but flag that the export needs Graph access and credentials, which may
not exist wherever you are running. Never fabricate an export.

**If a CSV is present but unlisted in the manifest** (a hand-placed portal download),
register it before analysing, so the evidence chain stays intact:

```
python scripts/register_input.py --month YYYY-MM --file <name> --source "<where it came from>" --notes "<when>"
```

**If `AuditCsv` is missing**, you may proceed, but say plainly and early that no activation
can be reported as unused this cycle, and that this is the review's main gap.

## Step 2 — Run the pipeline

```
python scripts/run_month.py --month YYYY-MM --skip-export
python scripts/verify_cycle.py --month YYYY-MM
```

Read the console output rather than assuming success. Things worth reacting to:

- **Duplicate ratio.** The known baseline is roughly 55% exact duplicates from log-platform
  fan-out, where every copy is identical and multiplicities are even. A materially different
  ratio, or odd multiplicities, means the upstream pipeline changed and the dedup assumption
  needs rechecking before any count is trusted. Raise it.
- **Observed period vs. requested month.** A gap at either end means the export is short —
  likely retention — and the review covers less than it claims.
- **Zero rows**, or `unmapped activityDisplayName` in the manifest notes: new PIM event types
  Graph is emitting that the exporter does not map yet. Report, do not silently ignore.
- **`verify_cycle` failures.** Stop and report. Do not proceed to triage.

## Step 3 — Triage, which is the part only you can do

The workbook has the rows; the owner needs the story. Turn the exception list into a handful
of themes ordered by what deserves attention first. For each theme give the count, the
severity, and what you would do about it.

Judgment to apply, not just counting:

- **A rate is more informative than a count.** "26 failed time-bound requests" is a number;
  "26 of 26 failed, so this is configuration rather than user error" is a finding.
- **Concentration matters.** One admin holding most activations, or one role dominating,
  is a scoping question even when nothing individually breaches policy.
- **`uncovered_privileged_action` is the highest-value class** — action taken outside any
  activation window, meaning possible standing access or PIM bypass. Lead with it when
  present. Check the actor against `break_glass_accounts` in config first; if that list is
  empty, say that these may be false positives for that reason.
- **Do not pad.** If the month is quiet, say so in a sentence. A thin month honestly
  reported is worth more than manufactured concern.

Then read the previous cycles' `output/exceptions-*.csv` and separate:

- **new** this cycle,
- **repeats** from prior cycles — a repeat that was accepted last month and is unresolved
  this month is a different conversation from a first occurrence,
- **carried-forward open items** with no recorded decision, which per `CLAUDE.md` must roll
  into this cycle.

## Step 4 — Present a plan and wait

Summarise: scope and period, whether audit data was available, verification result, the
triaged themes, month-over-month movement, and what you propose to do next. State the
limitations inline — the fixed 8-hour attribution window, and that correlation is temporal
rather than causal.

Then stop and ask for approval. Do not finalize, do not draft anything outbound, and do not
fill in the Decisions sheet before the owner has responded.

## Step 5 — After approval

Only what was approved:

- Record agreed dispositions on the workbook's `Decisions` sheet — decision, decider, date.
  A bare "approved" approves the *findings*, not the dispositions: without a named decider
  and a per-theme decision, leave those three columns blank and ask. Do not infer a decision
  from the advisory recommendation.
- Draft per-owner attestation requests if asked, with `python scripts/draft_attestations.py
  --month YYYY-MM`, which writes one file per actor into `<Month>/output/`. Do not send
  anything.
- Note carried-forward items for next cycle.
- Re-run `verify_cycle.py` if the workbook changed, and report the result — a rebuild that
  leaves every approved count identical is the evidence that post-approval edits were
  presentational.

Anything that leaves the folder — email, Jira, Confluence — is a separate approval. Revocation
is never automated.

## Failure modes and the honest response

| Situation | Response |
|---|---|
| No PIM activity file | Stop. Give the Phase A command. Do not fabricate. |
| No audit file | Proceed, but report that unused activations cannot be identified. |
| `verify_cycle` fails | Report the failing check. Findings are not final. |
| Retention window blown, export short or empty | Say the period is unrecoverable. A thin report that looks complete is worse than none. |
| Graph unreachable or auth fails during an offered export | Report it. Never work around a failed auth. |
| Duplicate ratio shifted from baseline | Flag before presenting any count as reliable. |
| Manifest missing an input | Register it first, or report the break in the evidence chain. |

## Reference

- `scripts/README.md` — pipeline detail, exception classes, config keys
- `CLAUDE.md` — project conventions and standing rules
- `<Month>/output/correlation-stats-YYYY-MM.json` — every count the reports use
- `<Month>/input/export-manifest.json` — provenance for every figure
