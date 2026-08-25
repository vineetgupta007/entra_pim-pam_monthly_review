# Installing the skill after a fresh clone

The skill lives here, in `skills/entra-pim-monthly-review/`, so it travels with the repo.
Claude discovers project skills from `.claude/skills/`, so a clone needs one copy step.

**Windows (PowerShell), from the repo root:**

```powershell
New-Item -ItemType Directory -Force .claude\skills | Out-Null
Copy-Item -Recurse -Force skills\entra-pim-monthly-review .claude\skills\
```

**macOS / Linux:**

```bash
mkdir -p .claude/skills
cp -r skills/entra-pim-monthly-review .claude/skills/
```

`.claude/` is gitignored, so the copy is local to each machine and the canonical version
stays under `skills/` where it is reviewable in pull requests.

To confirm it registered, ask Claude which skills are available, or invoke
`/entra-pim-monthly-review`.

## Alternative: install as a user skill

Ask Claude to save it as a user skill instead, and it becomes available in every project on
that machine rather than only this repo. The trade-off: it stops being version-controlled
alongside the scripts it calls, so a change to the pipeline can drift out of sync with the
skill describing it. Prefer the repo copy for anything the team shares.

## Editing the skill

Edit `skills/entra-pim-monthly-review/SKILL.md`, commit, then re-run the copy step. Editing
the copy under `.claude/` works for a quick experiment but the change is not tracked and the
next copy overwrites it.
