# Governance Scripts

The first three scripts maintain the governance file integrity chain described in `.claude/CLAUDE.md` §governance-monitoring. `check-no-stale-worktrees.sh` guards agent worktree roots.

## Scripts

### verify-hashes.sh
Reads `.claude/_governance-manifest.json` and recomputes SHA256 for each tracked file.
- Exit 0: all hashes match
- Exit 1: drift on one or more files (specifies which)
- Exit 2: manifest missing
- Exit 3: a tracked file is missing

Run after any session where governance files may have changed:
```bash
bash scripts/governance/verify-hashes.sh
```

### regen-manifest.sh
Recomputes and writes the manifest. Requires `--yes` or interactive confirmation to prevent accidental clobbers.

Run only after an **intentional** governance file edit (new P1–P9 principle, CLAUDE.md update, etc.):
```bash
bash scripts/governance/regen-manifest.sh --yes
```

Policy: only run after a reviewed PR that intentionally changed a governance file.

### sync-lib-principles.sh
Diffs `.claude/library/LIB-PRINCIPLES.md` against the authoritative copy in the sibling `terms-analysis` project.

Run when terms-analysis amends P1–P9 to check if this project should mirror the change:
```bash
bash scripts/governance/sync-lib-principles.sh
```

Exit 0 = in sync. Exit 1 = drift (see diff output + instructions). Exit 3 = sibling path not found.

### check-no-stale-worktrees.sh
Worktree hygiene (terms-analysis#123). Agent worktrees live under the roots listed in `.claude/governance/worktree-roots.txt`, the only place roots are defined. For every root the script checks that:
1. the root is ignored by a **tracked** ignore file (`.gitignore`). `.git/info/exclude` and a global excludes file do not count, because a fresh CI checkout does not have them; and
2. nothing under the root is tracked (file, symlink or gitlink).

Config format: one repo-relative root per line. `#` comment lines and empty lines are ignored, and one trailing `/` is optional. A root uses only ASCII letters, digits, `.`, `_`, `-` and `/`. It has no empty, `.` or `..` segment and no segment starting with `-`, and it is at most `getconf PATH_MAX .` bytes long. Any other line makes the whole config invalid.

- Exit 0: clean. stdout carries the sentinel `CLEAN <n>`, where n is the number of roots checked
- Exit 1: violation. A root has tracked paths under it (fix: `git rm --cached -r -- <root>`), or it is not ignored by a tracked `.gitignore` (fix: add `/<root>/` to `.gitignore`)
- Exit 2: config problem. The config is missing, unreadable, empty, a symlink (or under a symlinked directory), not a regular file, or has an invalid line
- Exit 3: git or environment failure. git is unavailable, this is not a work tree, a git query failed or returned output that could not be trusted, or no temporary directory could be created. Never reported as clean

The script prints only the configured roots and counts, never tracked path names or git's own error text. It makes three git calls whatever the number of roots. CI runs it on every pull request in `.github/workflows/ci.yml` (step "Worktree hygiene"):
```bash
bash scripts/governance/check-no-stale-worktrees.sh
```
