# Governance Scripts

Three scripts maintain the governance file integrity chain described in `.claude/CLAUDE.md` §governance-monitoring.

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
