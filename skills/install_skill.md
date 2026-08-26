# Installing the skill after a fresh clone

The skill lives here, in `skills/entra-pim-monthly-review/`, so it travels with the repo.

## Which client you're using matters

| Client | How the skill loads | `/entra-pim-monthly-review` |
|---|---|---|
| **Claude Code** (CLI) | Reads project skills from `.claude/skills/` — do the copy step below | works |
| **Cowork** (desktop) | Does **not** load skills from the project folder | not available |

**In Cowork**, the copy step below achieves nothing on its own. What wires the procedure in
is the instruction in `CLAUDE.md` under *Core Workflow Rules*, which tells Claude to read and
follow `skills/entra-pim-monthly-review/SKILL.md` whenever you ask for the monthly review.
`CLAUDE.md` loads automatically for this folder, so it works in every session with no setup.
Just ask in plain language — "run the monthly PIM review for August" — or point at the file
directly: "follow `skills/entra-pim-monthly-review/SKILL.md` for August."

A real `/` command in Cowork requires the skill to be saved to your Claude account, which
needs skill creation enabled for your org. If your admin enables it, ask Claude to save the
skill and it becomes a proper slash command.

## The copy step (Claude Code only)

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

## Alternative: save it to your Claude account

Saving it as an account-level skill makes it available in every project on that machine, and
gives you the `/` command in Cowork. Two trade-offs: it needs skill creation enabled for your
org, and it stops being version-controlled alongside the scripts it calls — so a change to the
pipeline can drift out of sync with the skill describing it. Prefer the repo copy for anything
the team shares.

## Editing the skill

Edit `skills/entra-pim-monthly-review/SKILL.md` — that copy is canonical, and it is the one
`CLAUDE.md` points Cowork at. Commit, then re-run the copy step so the `.claude/` copy used by
Claude Code does not fall behind. Editing under `.claude/` directly works for a quick
experiment, but the change is untracked and the next copy overwrites it.
