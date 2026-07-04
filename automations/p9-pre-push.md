# P9 pre-push gate

## Purpose

Enforce LIB-PRINCIPLES P9 as automation. Before any push leaves this
repo, both a security-engineer review and a grumpy-developer review
must be on record for the exact commit being pushed. The gate is
zero-tolerance on security findings and zero-tolerance on CRITICAL or
HIGH grumpy findings.

## Trigger

`git push` (any remote, any ref). The hook fires locally before the
transport step, so nothing leaves the machine until the gate passes.

## How the gate works

1. Hook reads current `HEAD` SHA via `git rev-parse HEAD`.
2. Hook looks for `.git/reviews/<HEAD_SHA>.signoff.json`.
3. If the file is missing, push is refused and instructions print to
   stderr.
4. If the file exists, hook validates:
   - JSON parses cleanly.
   - `head_sha` field matches the current HEAD SHA.
   - `security_engineer.verdict == "PASS"` and
     `grumpy_developer.verdict == "PASS"`,
     OR `override.used == true`.
5. If override is active, hook prints
   `P9 OVERRIDE ACTIVE: <reason> (authorized by <who>)` to stderr and
   allows the push.
6. On clean pass, hook prints
   `P9 pre-push gate: signoff OK for <sha:0:12>` and exits 0.

## Signoff location

`.git/reviews/<HEAD_SHA>.signoff.json`.

The signoff sits under `.git/`, which is inherently untracked. This
avoids a chicken-and-egg problem: if a signoff were a tracked file,
committing it would change HEAD and invalidate the signoff's own
SHA linkage. Keeping the file git-adjacent means the SHA the reviewer
signs off on is the SHA that will actually be pushed.

## Signoff schema

```json
{
  "head_sha": "<40-hex-sha>",
  "reviewed_at": "2026-07-04T00:00:00Z",
  "range": {"base": "<sha or ref>", "head": "<sha>"},
  "security_engineer": {
    "verdict": "PASS",
    "findings_count": 0,
    "findings": [],
    "summary": "STRIDE review of <range>: no findings"
  },
  "grumpy_developer": {
    "verdict": "PASS",
    "findings_count": 0,
    "findings": [],
    "summary": "code-quality review of <range>: no CRITICAL/HIGH"
  },
  "orchestrator": "Claude Sonnet 4.6",
  "override": {"used": false, "reason": "", "authorized_by": ""}
}
```

Field notes:

- `head_sha` must equal the current HEAD SHA at push time. A stale
  signoff for an older commit will not satisfy the gate.
- `range.base` should point at the last reviewed ancestor (or the
  merge-base with `origin/main`) so the review scope is auditable.
- `findings` is an array of finding objects; shape is reviewer-defined
  but should include at minimum `severity`, `title`, and `location`.
- `orchestrator` records which agent runtime performed the dispatch.

## How to satisfy the gate

Ask Claude to run the P9 review pair. Example prompt:

    run P9 review on HEAD and write signoff

The orchestrator dispatches `security-engineer` and `grumpy-developer`
subagents, waits for both verdicts, then writes the signoff file to
`.git/reviews/<HEAD_SHA>.signoff.json`. On the next `git push`, the
hook validates and allows the transport.

## Verdict rules

### Security (zero tolerance)

Per the P9 user directive dated 2026-07-03, ANY security-engineer
finding of ANY severity blocks the push. There is no "informational"
tier for security. The reviewer either returns `verdict: "PASS"` with
`findings: []` or the push is refused.

### Code quality (CRITICAL / HIGH block)

Grumpy-developer findings are graded. CRITICAL and HIGH block the
push. MEDIUM, LOW, and NIT are informational and MAY be filed as
follow-up issues without blocking. A signoff with only MEDIUM / LOW /
NIT findings still returns `verdict: "PASS"`.

## Override path

Emergency-only. To bypass the gate:

1. Write the signoff with `override.used: true`.
2. Set `override.reason` to a concrete justification. Example:
   `"emergency hotfix for outage in production ingest job"`.
3. Set `override.authorized_by` to the name of the human granting the
   override.

The hook logs the override reason and authorizer to stderr on every
push that uses an override signoff. Override signoffs are auditable
after the fact via `git log` correlated with `.git/reviews/`.

Overrides do not suppress the review; they record that the reviewer
step was consciously skipped and who owns that decision.

## Failure mode

No signoff, invalid JSON, wrong `head_sha`, or a non-PASS verdict
without override: push refused, exit 1, diagnostic printed to stderr
pointing at this document.

## Bootstrap note

The commit that first landed this hook system (message
`chore: pre-push P9 gate ...`) pushed BEFORE the hook was activated by
`scripts/install-hooks.sh`. That is the one exempt push in this
repo's history. From the moment the installer runs, every subsequent
push is gated.

## Installation

```bash
bash scripts/install-hooks.sh
```

The installer is idempotent: it sets `core.hooksPath` to `.githooks`,
marks each file under `.githooks/` executable, and ensures
`.git/reviews/` exists. Running it twice is safe.

## Verification

```bash
git config --get core.hooksPath   # expected: .githooks
ls -la .githooks/pre-push          # expected: executable
ls -d .git/reviews                 # expected: directory exists
```

To smoke-test the refuse path without pushing:

```bash
git push --dry-run
```

The hook runs against the dry-run transaction. With no signoff present
the hook prints the "signoff not found" diagnostic and exits 1;
nothing leaves the machine.
