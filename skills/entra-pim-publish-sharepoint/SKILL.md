---
name: entra-pim-publish-sharepoint
description: >
  Phase C of the monthly Entra PIM/PAM review: publish an already-completed and approved
  cycle's deliverables to the auditor SharePoint library. Use when someone says "upload
  the May review to SharePoint", "publish this cycle for the auditors", "send the findings
  to the audit library", asks where auditors can find a month's review, or invokes
  /entra-pim-publish-sharepoint. Requires the cycle's workbook and executive summary to
  already exist in <Month>/output/ and to have passed verification; if they do not, explain
  what is missing rather than proceeding. Do not use this skill to run or re-run the review
  itself, to publish a -SAMPLE fixture, to overwrite something already published, or to
  send notifications to auditors.
---

# Publish a review cycle to SharePoint — Phase C

Phase B produced findings and the owner approved them. Your job is to get exactly two
files into the auditor library, and to refuse if anything about the cycle is not sound.

**Publishing is a decision, not a step.** Approving the *analysis* does not approve
*publication* — they are separate, and the second one has to be asked for explicitly.
Never publish because a review just finished.

## Non-negotiables

These come from `CLAUDE.md` and from the review skill, and override any instinct to be
helpful:

- **Never publish a cycle that failed verification.** `verify_cycle.py` is the gate, and
  `publish_month.py` re-runs it rather than trusting an earlier result. If it fails,
  report which check failed and stop.
- **Never publish a `-SAMPLE` cycle.** A fixture in an audit library is worse than an
  empty one. The script refuses these; do not work around it by relabelling.
- **Never overwrite.** Published evidence is immutable. A corrected cycle is published
  as a re-run (`--run 2`), leaving the original intact.
- **Never widen the permission ask.** If publication fails for lack of access, report it.
  Do not suggest `Sites.ReadWrite.All` as a fix — the design is `Sites.Selected`, scoped
  to one library, and a tenant-wide write grant is not an acceptable substitute.
- **Deliverables only.** The workbook and the summary. Raw exports and intermediate CSVs
  stay in the project folder.
- **Record who approved.** The receipt names a person. "The owner approved it" is not a
  name.
- **Never notify anyone.** Emailing or messaging auditors is a further outbound action
  and needs its own approval.

## Step 1 — Establish which cycle, and confirm it is publishable

Work out the target month and run number. Month folders use the month name (`August`,
`August-2`). The label defaults to the period (`2026-05`).

Check `<Month>/output/` for both:

| File | Required |
|---|---|
| `entra-pim-correlation-<label>.xlsx` | yes |
| `entra-pim-review-summary-<label>.docx` | yes |
| `publication-receipt-<label>.json` | must **not** already exist |

**If either deliverable is missing**, the cycle has not been through Phase B. Say so and
offer the Phase B command rather than trying to publish a partial cycle:

```
python scripts/run_month.py --month YYYY-MM --skip-export
```

**If a publication receipt already exists**, this cycle has been published. Read the
receipt, tell the owner when and by whom, and stop. Do not publish again.

**If the label contains `SAMPLE`**, stop. Explain that fixtures do not go to auditors.

## Step 2 — Confirm access before promising anything

```
python scripts/publish_month.py --month YYYY-MM --check
```

This resolves the site and library and proves read access, uploading nothing. Run it
first when publication has never been exercised on this machine, or when config changed.

Common outcomes, and the honest reading of each:

- **`sharepoint.enabled` is false** — publication is not provisioned yet. The library,
  the `Sites.Selected` grant, and a retention label all have to exist first. Point at
  `docs/entra-pim-sharepoint-publication-design.docx` and stop. Do not flip the flag to
  make the error go away.
- **403 Forbidden** — authentication worked, authorisation did not. In unattended modes
  this means an administrator has not granted this app `write` on the target site;
  `Sites.Selected` grants nothing until they do. In interactive mode it may instead mean
  the signed-in user lacks write access to the library. The script's message distinguishes
  these; relay it rather than guessing.
- **Library not found** — the script lists the libraries it can see. Compare against
  `sharepoint.library` in config.

Never work around a failed auth. Report it.

## Step 3 — Dry run, and show the owner what will happen

```
python scripts/publish_month.py --month YYYY-MM --dry-run
```

This runs every precondition — label, files present, verification gate, approver — and
prints the exact destination path and the SHA-256 of each file, without uploading.

Show the owner: the two file names, their hashes, the destination site, library and
folder, and the verification result. Then **stop and ask for approval to publish**, and
ask who is approving it. Do not proceed on an implied yes.

## Step 4 — Publish

```
python scripts/publish_month.py --month YYYY-MM --approved-by "Full Name"
```

Read the output rather than assuming success. On completion the script writes
`publication-receipt-<label>.json` into `<Month>/output/`, recording the destination URL,
each file's hash and item ID, the approver, and the timestamp. That receipt is the
project's proof of what was published and when — mention where it is.

If the destination folder is non-empty the script refuses. That is correct behaviour, not
an obstacle: investigate what is already there before considering `--force`.

## Step 5 — Confirm independently, if the connector is available

The Microsoft 365 connector is read-only — it cannot upload, but `sharepoint_search` and
`sharepoint_folder_search` can confirm the files landed where the receipt says. This is a
cheap independent check and worth doing when the connector is authorised. If it is not
authorised, say so; do not treat its absence as a failure of the upload.

## Failure modes and the honest response

| Situation | Response |
|---|---|
| Deliverables missing | Stop. Offer the Phase B command. The cycle is not finished. |
| `verify_cycle` fails | Stop. Name the failing check. Unverified findings must not reach auditors. |
| Label is `-SAMPLE` | Stop. Fixtures never go to an audit library. |
| Receipt already exists | Stop. Report when and by whom it was published. |
| `sharepoint.enabled` false | Report that publication is not provisioned. Do not enable it yourself. |
| 403 on the site | Report it, and say which of app-consent or user-permission is the likely cause. |
| Destination folder occupied | Stop. Suggest publishing as a re-run rather than adding to it. |
| Asked to notify auditors | Separate approval. Not part of this skill. |

## Reference

- `docs/entra-pim-sharepoint-publication-design.docx` — permissions, retention, open decisions
- `scripts/publish_month.py` — the publisher and its preconditions
- `skills/entra-pim-monthly-review/SKILL.md` — Phase B, which must complete first
- `CLAUDE.md` — project conventions and standing rules
