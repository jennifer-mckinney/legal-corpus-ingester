# P9 pre-push gate

<!-- p9-shared:begin -->
<!-- Everything from here to p9-shared:end is byte-identical in
terms-analysis and legal-corpus-ingester, pinned by CANONICAL_P9_DOC_SHA256
in each repo's test_p9_gate_fix_r2.py. Edit both repos together and re-pin.
Repo-specific notes go below p9-shared:end. -->

## Purpose

Enforce LIB-PRINCIPLES P9 as automation. Before a push leaves the repo,
a security-engineer review and a grumpy-developer review must be on
record for the commit at the tip of every pushed ref, and that review
must cover every commit the push publishes. Both reviews are zero
tolerance: a finding of any severity, from either reviewer, blocks the
push (owner directive 2026-07-04, recorded in each repo's
`.claude/CLAUDE.md` P9 entry).

## Enforced contract

The hook header carries this exact text. A test in each repo fails if the
two drift apart.

```text
  - Each pushed ref tip needs <git-common-dir>/reviews/<sha>.signoff.json
    whose head_sha is that commit, with security_engineer and
    grumpy_developer both verdict PASS and, for each, an explicit findings == []
    (key required; zero tolerance for both roles, any severity).
  - Every commit that is new to the remote must lie inside the reviewed
    range: range.base (a commit the remote already has) .. tip. Without
    range.base the reviewed range is the tip alone, so a push that carries
    an unreviewed intermediate commit along is refused.
  - What the remote already has is read from its live advertisement for
    the push URL (git ls-remote), never from local remote-tracking refs.
    If the advertisement cannot be read or parsed, the push is refused.
  - A complete override (used: true + reason + authorized_by) replaces the
    verdict, findings and range checks, and is announced on stderr.
  - Deletions need no signoff. A push with nothing to send exits 0.
```

## Trigger

`git push` (any remote, any ref). The hook runs locally before the
transport step and checks every ref git is about to push. It is a local
guard against honest mistakes, not a security boundary: see "Limits and
server-side enforcement" below.

## How the gate works

Git hands the hook one line per pushed ref on stdin:
`<local_ref> <local_sha> <remote_ref> <remote_sha>`. The hook checks the
pushed shas, never `HEAD` (terms-analysis#175: a signoff for `HEAD` used to
let any other branch, sha or `--all` push through).

1. The hook resolves the git common dir (`git rev-parse --git-common-dir`,
   made absolute with `CDPATH` unset) and reads every stdin line.
2. No lines at all means every ref is already up to date and nothing can
   leave the machine. The hook prints `P9 pre-push gate: nothing to push
   to <remote> (no refs received); nothing to review` and exits 0, so
   `git push && ...` chains keep working.
3. A deletion (`local_sha` all zeros, 40 or 64 of them) pushes no commits
   and needs no signoff.
4. Every other `local_sha` must be a 40- or 64-hex object id that resolves
   to a commit. Annotated tags are peeled to their commit. A blob, a tree
   or an unknown object is refused ("does not resolve to a commit").
5. The hook requires `<common-dir>/reviews/<commit-sha>.signoff.json`. The
   "Expected signoff" line in the refusal names that absolute path.
6. The signoff is validated by `python3 -I` (so a `json.py` in the checkout
   cannot replace the parser). The validator must print an explicit
   `OK ...` line; a missing `python3`, a crash or a silent exit refuses.
   It requires:
   - the file is a JSON object and `head_sha` equals the pushed commit;
   - either a complete override (see "Override path"), or
     `security_engineer.verdict == "PASS"` and
     `grumpy_developer.verdict == "PASS"`, where each role's `findings` key
     is present and exactly `[]` (a missing key, a non-empty list, `{}`,
     `""`, `null` or any other value is refused);
   - `range`, when present, is an object, and `range.base`, when present,
     is a 40- or 64-hex commit sha (a ref name such as `origin/main` is
     refused: refs move, shas do not).
7. Unless an override is active, the hook checks the reviewed range. It
   reads what the destination has from its live advertisement,
   `git ls-remote -- <push-url>` (the URL git passes as the hook's second
   argument), once per push. The commits new to the remote are
   `git rev-list <tip> --not <advertised commits>`. Local
   `refs/remotes/*` are never consulted: they go stale after a
   server-side delete or a `git remote set-url`, and would vouch for
   history the destination does not hold. The remote's name is only
   printed, so a name with `/` or glob characters, or a push straight to
   a URL, is judged the same way.
   - If `ls-remote` fails, prints a line that is not `<id><TAB><ref>`,
     or the advertised ids cannot be resolved, the ref is refused with
     `cannot establish which commits are new to <remote>`.
   - An advertised id that is not a commit in this clone (never fetched,
     or a ref to a blob or tree) is left out, and nothing is fetched.
     That can only make more commits count as new, which refuses; it
     never lets an unreviewed commit through. Run `git fetch` and push
     again if the refusal names commits the remote already has.
   - Without `range.base`, the tip must be the only new commit (or there
     are none). Otherwise the push is refused with
     `<n> commits are new to <remote>, but the signoff ... has no
     range.base`.
   - With `range.base`, it must resolve to a commit, be an ancestor of the
     tip, and already be on the remote. Then every new commit lies in
     `base..tip`, the range the reviewers read.
8. If any ref fails, the whole push is refused, each failure is printed,
   and the hook exits 1.
9. An active override prints
   `P9 OVERRIDE ACTIVE: <reason> (authorized by <who>)` to stderr. Each
   accepted ref prints `P9 pre-push gate: signoff OK for <ref> at <sha:0:12>`.

The hook is byte-identical in terms-analysis and legal-corpus-ingester,
pinned by `.githooks/pre-push.sha256` and by the shared constants in each
repo's `test_p9_gate_fix_r1.py`. Change both repos together and re-pin.

## Signoff location

`<common-dir>/reviews/<sha>.signoff.json`, where `<common-dir>` is
`git rev-parse --git-common-dir`. That is `<main checkout>/.git/` from
every checkout.

### Worktrees

In a linked `git worktree`, `.git` is a file, not a directory, so a
per-checkout `.git/reviews/` cannot exist. The hook and the installer
use the common dir, which every worktree shares with the main checkout:
a signoff written from any checkout satisfies a push of that sha from any
checkout, and the main checkout and worktrees behave identically
(terms-analysis#175). The per-worktree git dir (`.git/worktrees/<name>/`)
is NOT consulted; a signoff placed there is ignored.

To find the directory from any checkout or subdirectory (git 2.31 or
later):

```bash
echo "$(git rev-parse --path-format=absolute --git-common-dir)/reviews"
```

The signoff sits under `.git/`, which is inherently untracked. If a
signoff were a tracked file, committing it would change the commit and
invalidate the signoff's own sha. Keeping the file git-adjacent means the
sha the reviewers sign off on is the sha that is pushed.

## Signoff schema

```json
{
  "head_sha": "<40- or 64-hex sha of the tip>",
  "reviewed_at": "2026-07-04T00:00:00Z",
  "range": {"base": "<40- or 64-hex sha already on the remote>", "head": "<tip sha>"},
  "security_engineer": {
    "verdict": "PASS",
    "findings_count": 0,
    "findings": [],
    "summary": "STRIDE review of every commit in <range>: no findings"
  },
  "grumpy_developer": {
    "verdict": "PASS",
    "findings_count": 0,
    "findings": [],
    "summary": "code-quality review of every commit in <range>: no findings"
  },
  "orchestrator": "<agent runtime>",
  "override": {"used": false, "reason": "", "authorized_by": ""}
}
```

Field notes:

- `head_sha` must equal the pushed commit sha (the file name's sha). A
  stale signoff for an older commit, or a copy renamed to another sha,
  does not satisfy the gate. Each pushed ref needs its own signoff.
- `range.base` is the commit the review started from: one the remote
  already has, normally the merge-base with `origin/main`. It is required
  whenever the push publishes more than the tip commit.
- `range.head` and the other fields are recorded for audit; the hook does
  not read them.
- `findings` is an array of finding objects (at minimum `severity`,
  `title` and `location`). A PASS must carry `[]`.

## How to satisfy the gate

Ask Claude to run the P9 review pair. Example prompt:

    run P9 review on HEAD and write signoff

The orchestrator dispatches `security-engineer` and `grumpy-developer`
on the range `base..HEAD`, waits for both verdicts, then writes the
signoff to `<common-dir>/reviews/<sha>.signoff.json` with `range.base`
set. The reviewers read every commit in the range (`git log -p
base..HEAD`), not only the net diff: a secret added in one commit and
deleted in the next is absent from the net diff but is still published.

## Verdict rules

Both reviewers are zero tolerance (owner directive 2026-07-04; global
rule "fix every review finding at every severity"). A finding of any
severity, CRITICAL to NIT, from either reviewer blocks the push until it
is fixed and re-reviewed. A reviewer returns `verdict: "PASS"` with
`findings: []`, or the push is refused. There is no informational tier;
only the owner can waive a finding, and that is recorded as an override.

## Override path

Emergency only. To bypass the review for one commit:

1. Write the signoff with `override.used: true` (a JSON boolean).
2. Set `override.reason` to a concrete justification. Example:
   `"emergency hotfix for outage in production ingest job"`.
3. Set `override.authorized_by` to the name of the human granting the
   override.

The hook refuses an override whose `reason` or `authorized_by` is
missing, blank or not a string, and an `override.used` that is not a
boolean (`"false"` and `1` are refused). A complete override replaces the
verdict, findings and range checks. The hook logs the reason and the
authorizer to stderr on every push that uses it. Overrides are auditable
after the fact via `git log` correlated with `<common-dir>/reviews/`.

Overrides do not suppress the review; they record that the review step
was consciously skipped and who owns that decision.

## Failure mode

Any pushed ref with no signoff, invalid JSON, a `head_sha` that differs
from the pushed sha, a non-PASS verdict, a PASS with findings, a malformed
`range`, an unreviewed new commit, a `range.base` that is not an ancestor
or not on the remote, an object that is not a commit, a missing
`python3`, a remote advertisement that cannot be read or parsed, or an
incomplete override: the push is refused, the hook exits
1, and a diagnostic naming the ref and the signoff path goes to stderr.

## Limits and server-side enforcement

The pre-push hook runs on the developer's machine, so it catches honest
mistakes, not a determined author. It is bypassed by:

- `git push --no-verify`;
- `git -c core.hooksPath=<elsewhere> push`, or a per-worktree
  `core.hooksPath` set after `install-hooks.sh` ran;
- a commit that edits `.githooks/pre-push` itself, because each checkout
  runs its own tracked hook;
- a hand-written signoff, since signoffs are unsigned local JSON files.

"Already on the remote" is judged from the destination's live
advertisement at push time. A branch deleted on the server, or a remote
whose URL was changed, no longer vouches for its old history, so that
history must be covered by the review range again. The hook contacts
the push URL a second time for this; a remote that needs credentials may
ask for them again.

The enforcing control is server-side. `main` is branch-protected on
GitHub in both terms-analysis and legal-corpus-ingester (PR required,
required status checks, `enforce_admins` on), so a direct push to `main`
is refused by GitHub whatever the local hook does. A required CI status
check that verifies a committed signoff artifact for the PR head is not
in place yet; until it is, the reviewer signoff is enforced only by the
local hook and the PR-body check.

## Installation

```bash
bash scripts/install-hooks.sh
```

Run it once per clone, from the main checkout or any worktree (the
setting is shared). The installer is idempotent. It:

- sets `core.hooksPath` to the relative value `.githooks`, so each
  checkout runs its own tracked `.githooks/`;
- replaces any other existing value (for example an absolute
  `<repo>/.git/hooks`, which has no `pre-push` and silently disables the
  gate), prints `install-hooks: replacing core.hooksPath=<old> with
  .githooks ...` to stderr, and exits 0;
- re-reads the effective value and exits 1 if a higher-precedence scope
  (such as a per-worktree `config.worktree`) still shadows it;
- marks each hook script under `.githooks/` executable (not the
  `.sha256` pin files, which stay data);
- ensures `<common-dir>/reviews/` exists.

Running it twice is safe. It exits 1 if run outside a checkout that
has `.githooks/`.

## Verification

```bash
git config --get core.hooksPath   # expected: .githooks
test -x .githooks/pre-push && echo "hook executable"
ls -d "$(git rev-parse --path-format=absolute --git-common-dir)/reviews"   # expected: directory exists
```

To smoke-test the refuse path without pushing:

```bash
git push --dry-run
```

The hook runs against the dry-run transaction. With no signoff present
it prints the "signoff not found" diagnostic and exits 1; nothing leaves
the machine. If the remote is already up to date, git sends the hook no
refs; it prints "nothing to push" and exits 0.

## Cross-repo parity in CI

The hook must stay byte-identical in terms-analysis and
legal-corpus-ingester. Each repo's CI runs
`test_pre_push_hook_matches_the_sibling_repos_published_hook`, which
fetches the sibling's `.githooks/pre-push` from GitHub and compares
sha256 digests. It fails closed when the fetch fails, and CI ignores the
local opt-out `P9_SKIP_SIBLING_PARITY=1`. A dedicated CI step runs it
alone and fails unless the summary is exactly `1 passed`, so a skip is
red.

The sibling ref is `P9_SIBLING_REF`. CI sets it to the PR's head branch
(`github.head_ref`) when the sibling repo has a branch of that name, and
to `main` otherwise (pushes to `main`, and PRs that do not touch the
hook). If CI cannot ask GitHub whether that branch exists, the step
fails.

Bootstrap order for a hook change: push the same hook on a branch with
the same name in both repos, then open both PRs. Each PR's parity step is
red until the sibling branch carries the identical hook, and green once
both do. Merge the two PRs back to back. Between the two merges, the
first-merged repo's `main` build compares against the sibling's `main`,
which still has the old hook, and is red; re-run it after the second
merge.

<!-- p9-shared:end -->

## Repo-specific notes (legal-corpus-ingester)

### Bootstrap note

The commit that first landed this hook system (message
`chore: pre-push P9 gate ...`) pushed BEFORE the hook was activated by
`scripts/install-hooks.sh`. That is the one exempt push in this
repo's history. From the moment the installer runs, every subsequent
push is gated.
