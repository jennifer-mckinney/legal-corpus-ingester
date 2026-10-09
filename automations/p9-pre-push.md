# P9 review in CI

P9 (`.claude/library/LIB-PRINCIPLES.md`) requires a security-engineer review
and a grumpy-developer review of every change, both at zero findings, before
it reaches `main`. Since 2026-10-09 (terms-analysis#191, ported to this repo)
that review runs as two standard GitHub Actions jobs on the pull request. The earlier local
pre-push signoff hook is retired. The file keeps its old name so existing
links still resolve.

Companion change: this pairs with jennifer-mckinney/terms-analysis#214; the PRs merge
back-to-back, #214 first, then this one.

## How it runs

Workflow: `.github/workflows/p9-review.yml`.

- **Trigger:** `pull_request` to `main` (opened, synchronize, reopened,
  ready_for_review). Not `pull_request_target`, so the PR's code never runs
  with the base repository's secrets.
- **Jobs:** `security-review` and `grumpy-review`, on GitHub-hosted
  `ubuntu-latest` (not this repo's self-hosted runner). Each job:
  1. checks out the PR with full history (`actions/checkout`, pinned by
     commit SHA, `fetch-depth: 0`);
  2. deletes any `p9-verdict.json` committed in the PR and copies
     `.github/p9/check_verdict.py` to the runner's temp directory, before the
     reviewer can touch the tree;
  3. writes `git diff origin/<base>...HEAD` to `$RUNNER_TEMP/p9/pr.diff`,
     the changed-file list to `$RUNNER_TEMP/p9/changed-files.txt` and
     `git log --oneline origin/<base>..<PR head sha>` to
     `$RUNNER_TEMP/p9/commits.txt` (the checkout is GitHub's synthetic merge
     commit, so the list ends at `github.event.pull_request.head.sha`, not
     `HEAD`; the base branch and head sha pass through `env`, not inline);
  4. runs `anthropics/claude-code-action` (pinned by commit SHA) with a prompt
     that has it read the changed-file list and the diff first, then its brief,
     `.github/p9/security-engineer.md` or `.github/p9/grumpy-developer.md`;
  5. runs the copied gate on `p9-verdict.json`.

  The review unit is the PR's net diff, not its individual commits;
  `commits.txt` is context only (commit-history shape is an accepted item
  under the no-rebase policy).
- **Bounds:** `timeout-minutes` per job, `--max-turns` for the reviewer, and a
  `concurrency` group per PR that cancels the run for a superseded commit.
- **Tools:** an exact `--allowedTools` allowlist: `Read`, `Grep`, `Glob`,
  writes to `p9-verdict.json` only, and the inline PR-comment MCP tool
  (`mcp__github_inline_comment__create_inline_comment`). `Bash`, `WebFetch`
  and `WebSearch` are disallowed. The write scope is the rule
  `Edit(./p9-verdict.json)`: Claude Code checks every file-writing tool,
  `Write` included, against `Edit(path)` rules and ignores `Write(path)`
  rules (Claude Code permissions docs, "Read and Edit").
- **Read deny rules:** the action's `settings` input turns on
  `permissions.blockReadsOutsideWorkingDirectories` (working directories: the
  checkout and `$RUNNER_TEMP/p9`) and sets exactly these `Read` deny rules.
  Path patterns follow the Claude Code permissions docs
  (code.claude.com/docs/en/permissions): `//path` is absolute, `~/path` is
  home, `./path` is the working directory, and a single `/path` is relative
  to the settings file, so no rule uses a single leading slash.
  - `Read(//proc/**)` and `Read(//sys/**)`: the process
    environment (`ANTHROPIC_API_KEY`, the job token) cannot be read back;
  - `Read(~/.git-credentials)`, `Read(~/.config/gh/**)` and
    `Read(~/.claude/.credentials.json)`: runner credential files;
  - `Read(/${{ runner.temp }}/_runner_file_commands/**)`: the runner's
    step-command files, derived from `runner.temp` rather than a hard-coded
    hosted-runner path (`runner.temp` is absolute, so the rule renders as
    `//...`);
  - `Read(./.git/**)`: the checkout keeps no credential
    (`persist-credentials: false`), but claude-code-action itself writes the
    job token into the `origin` URL in `.git/config`, so the reviewer may not
    read the checkout's git metadata.

  `Read` deny rules also cover Grep and Glob.
  `--setting-sources user` ignores any `.claude/settings.json` the PR adds.
- **Permissions:** the workflow grants nothing by default; each job gets
  `contents: read` and `pull-requests: write` (for the inline comments) and
  uses the job's own `github.token`.
- **Secret:** `ANTHROPIC_API_KEY`, used only as the action's
  `anthropic_api_key` input, so it is set in that step only; there is no
  workflow- or job-level `env`. Fork PRs receive no secrets, so the review step
  fails and the job fails closed.

## Verdict contract

Each reviewer comments inline on each finding and writes `p9-verdict.json`
in the working directory:

```json
{"verdict": "PASS", "findings": []}
```

```json
{"verdict": "FAIL", "findings": [
  {"severity": "HIGH", "title": "Short title", "file": "path/to/file.py", "line": 42}
]}
```

`.github/p9/check_verdict.py` decides the job result:

| Exit | Meaning |
|---|---|
| 0 | verdict `PASS` and `findings` is `[]`; prints `P9 verdict: PASS, 0 findings` |
| 1 | verdict `FAIL`, or any finding listed (each printed on one sanitised line) |
| 2 | file missing, unreadable, not JSON, duplicate keys, or not the contract shape |

Only exit 0 passes the job. A reviewer that crashes, runs out of turns or
writes nothing fails the job.

## Merging

Branch protection is not enabled yet (pending). After the first green run,
the owner adds `security-review` and `grumpy-review` (source: GitHub Actions)
as required status checks on `main` (see Owner setup). From then on a PR
merges only when both pass on its head commit. A new push re-runs both. Only
the owner can waive a finding, at merge time.

## Owner setup

1. Add the `ANTHROPIC_API_KEY` repository secret.
2. After the first green run, add `security-review` and `grumpy-review` as
   required status checks on `main`.

## Limits

- The workflow and briefs are read from the PR's own merge commit, as with
  any `pull_request` workflow. A PR that edits them changes its own review;
  that edit is visible in the diff both reviewers read and the owner merges.
- The reviewer reads untrusted PR content. The gate copy, the deleted
  committed verdict, the tool allowlist and the read deny rules limit what
  a prompt injection can change; the owner's merge decision remains the final
  control. Workflow self-modification (terms-analysis#216) is an accepted, tracked risk; the
  briefs list it under "Accepted / tracked items".

## Local hooks

`scripts/install-hooks.sh` sets `core.hooksPath=.githooks` so the tracked
`.githooks/pre-commit` (ruff, licence audit, mypy, unit tests; see
`automations/pre-commit.md`) runs on `git commit`. It no longer creates a
`reviews/` directory. There is no local pre-push gate any more: the hook, its
`.sha256` pin, the `.git/reviews/<sha>.signoff.json` signoffs and the
cross-repo parity script (`scripts/ci/p9-sibling-parity.sh`) are retired.
