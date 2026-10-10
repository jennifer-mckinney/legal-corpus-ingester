# ADR-016: CI runs on GitHub-hosted runners

- **Status:** Accepted
- **Date:** 2026-10-10
- **Decided by:** owner, 2026-10-10 (issue #19, option c: "goal is to get onto github hosted runners")
- **Governs:** `.claude/CLAUDE.md` HR4 (local-only data) and `PRINCIPLES.md` constraint C4
- **Supersedes:** ADR-007 (self-hosted GitHub Actions runner, as listed in `docs/adr/README.md`)

## Context

Since Phase 0.0 every workflow except the P9 review jobs ran on a self-hosted runner on the
owner's laptop: `ci.yml`, `health.yml`, `refresh.yml`, `vcr-drift.yml` and
`approval-expiry.yml`, all with `runs-on: [self-hosted, legal-corpus-ingester]`. HR4 said
"self-hosted GitHub Actions runner (never GitHub-hosted)" and C4 forbade dispatching fetch or
embed workloads to GitHub-hosted runners, on the grounds that corpus artifacts must never leave
the local machine.

PR #69 pinned the interpreter once in `.python-version` and had `actions/setup-python` read it.
On the self-hosted macOS runner that step failed: the runner has no `/Users/runner`, so the
download path ends in `mkdir: /Users/runner: Permission denied`. Issue #19 listed three ways out:

- (a) seed the runner's tool cache so setup-python finds the interpreter without downloading;
- (b) drop setup-python on the self-hosted jobs and use the Homebrew interpreter on `PATH`;
- (c) move the jobs to GitHub-hosted runners.

Option (a) was written up as `docs/runbooks/self-hosted-runner-python.md` (PR #69 commit
`66b56c3`) and executed on 2026-10-10. The local seed completed and verified, but the dispatched
`health.yml` run (38071582790, on the exact tip) still reported `Version 3.14 was not found in
the local cache` and failed on the same `mkdir`. The read-only diagnosis in
`docs/evidence/2026-10-10-runner-toolcache-seed.md` shows why: setup-python v5.6.0's `run()`
(`dist/setup/index.js`, the `if (IS_MAC)` block) sets
`AGENT_TOOLSDIRECTORY=/Users/runner/hostedtoolcache` unconditionally on macOS and copies it into
`RUNNER_TOOL_CACHE` before any cache lookup. A seed under `_work/_tool` is never consulted, and
the override cannot be defeated from the runner `.env` or the workflow `env:`. Option (a) does
not work without `sudo` on the laptop.

Other facts bearing on the decision:

- The laptop is a single point of failure for every scheduled job. When it is off, asleep or
  off-network, the nightly health check, the weekly drift canary and the daily approval watcher
  queue silently instead of running.
- The owner's standing rule (2026-10-09) prefers GitHub-hosted Actions, required checks and
  vendor-documented patterns over custom machinery. The runner service, its launchd plist,
  `scripts/setup_runner.sh` and the tool-cache seed are custom machinery.
- This repository and the terms-analysis consumer are both public. No job needs a secret beyond
  the job's own `github.token`; the one repository secret (`ANTHROPIC_API_KEY`) belongs to the P9
  review jobs, which already run on `ubuntu-latest` (ADR-015).
- The five jobs read tracked files, run tests, and in the canary's case re-record cassettes from
  public legal sources. None of them reads a published corpus bundle, an index or an embedding.

## Decision

All workflows in `.github/workflows/` run on GitHub-hosted `ubuntu-latest` runners. The
`[self-hosted, legal-corpus-ingester]` label set, `.github/actionlint.yaml`'s custom runner
label, `scripts/setup_runner.sh`, `automations/self-hosted-runner.md` and the option (a) runbook
are removed.

HR4 is amended to: "all corpus data stays local to the pipeline's configured output; CI runs on
GitHub-hosted ubuntu-latest runners (ADR-016)". The "never GitHub-hosted" clause is removed. C4
is rewritten to match. HR6 (no cloud embedding or LLM APIs, LocalAI + Apertus-8B only) is
unchanged: moving CI does not move inference.

## Consequences

- Corpus fetches that CI performs run on GitHub infrastructure. Today that is the weekly VCR
  drift canary's live re-record; when the weekly refresh is wired, its fetches and any bundle it
  publishes run there too. The HTTP requests to EUR-Lex, Congress.gov and the other sources
  originate from GitHub's runners rather than the owner's network.
- Runner filesystems are discarded after every job. `scripts/detect_bundle_change.py` keeps the
  last-announced-bundle record, and `health.yml` reads checkpoints, under
  `$XDG_STATE_HOME/legal-corpus-ingester` (default `~/.local/state`), which was chosen because
  the self-hosted runner's home directory persisted between runs. On a hosted runner it is empty
  on every run. A persistence design (Actions cache, artifact, or a state branch) is required
  before refresh is wired; it is tracked as decision card #71 and is not decided here. Until
  #71 lands, `refresh.yml` runs the detect step with `--require-state`, so a missing record is
  a red run (exit 2) rather than an announcement every week; a wired refresh cannot announce
  and then forget.
- ADR-015 condition 3 ("the jobs run on GitHub-hosted runners, never the self-hosted corpus
  runner") is still satisfied; there is no longer a corpus runner to move them to.
- `.claude/CLAUDE.md` HR4 changes, so `.claude/_governance-manifest.json` is regenerated in the
  same PR.
- Decommissioning the laptop runner is an owner step after merge, not part of the PR:
  `cd ~/actions-runner-legal-corpus && ./svc.sh stop && ./svc.sh uninstall && ./config.sh remove --token <removal-token>`,
  delete the seeded `_work/_tool/Python/3.14.3`, and remove the runner from the repository's
  settings if `config.sh remove` did not. Until then the runner stays registered and idle.
- `docs/evidence/2026-10-10-runner-toolcache-seed.md` remains as the record of the option (a)
  attempt; the runbook it executed is deleted rather than corrected.
