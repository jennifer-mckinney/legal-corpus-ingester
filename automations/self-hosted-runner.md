# Self-hosted GitHub Actions runner

## Why self-hosted

Hard requirement HR4 (all data local) shapes this choice. Every workflow
in this repo fetches legal corpus text, chunks it, embeds it, and stages
artifacts for the terms-analysis project. On a GitHub-hosted runner those
files would move through Azure infrastructure during job execution.
A self-hosted runner on this Mac keeps the fetches, the intermediate
files, and the built artifacts on-machine. GitHub receives only the
workflow logs the runner chooses to stream back.

There is a related benefit: local runs use whatever network egress this
laptop already has, so we can reuse cached downloads and avoid rebuilding
statute snapshots on every run.

## What is installed

The runner binary lives at `~/actions-runner-legal-corpus`, outside this
git repo. That directory is intentionally excluded from version control
so runner credentials never enter the tree.

Registered configuration:

- Repo: `jennifer-mckinney/legal-corpus-ingester`
- Labels: `legal-corpus-ingester`, `self-hosted`, `macos`
  (GitHub also auto-attaches `macOS` and `ARM64`)
- Service framework: launchd LaunchAgent, user scope (no sudo)
- Plist: `~/Library/LaunchAgents/actions.runner.jennifer-mckinney-legal-corpus-ingester.MacBook-Pro.plist`
- Logs: `~/Library/Logs/actions.runner.jennifer-mckinney-legal-corpus-ingester.MacBook-Pro/`

## Install

Re-runnable installer:

```bash
./scripts/setup_runner.sh
```

The script is idempotent. It exits early if `~/actions-runner-legal-corpus/.runner`
already exists, so re-running does not clobber a live registration.

Under the hood the script does what Task P2 called for:

1. Requests a short-lived registration token via `gh api`.
2. Downloads the pinned runner tarball if the binary is not already extracted.
3. Runs `config.sh --unattended` with the labels above.
4. Runs `svc.sh install` then `svc.sh start`.
5. Calls `gh api /repos/.../actions/runners` and prints the resulting
   name, status, and labels for confirmation.

## Service management

All commands run from `~/actions-runner-legal-corpus`:

```bash
./svc.sh status      # is the LaunchAgent loaded and running
./svc.sh start       # start (if stopped)
./svc.sh stop        # stop without unregistering
./svc.sh uninstall   # remove the LaunchAgent plist
```

`svc.sh status` reads the launchd label
`actions.runner.jennifer-mckinney-legal-corpus-ingester.MacBook-Pro` and
prints its PID when running.

## Tear-down

To fully remove the runner (both this machine and the GitHub-side record):

```bash
cd ~/actions-runner-legal-corpus
./svc.sh stop
./svc.sh uninstall

# fresh removal token, then unregister on GitHub
REMOVE_TOKEN=$(gh api -X POST \
  /repos/jennifer-mckinney/legal-corpus-ingester/actions/runners/remove-token \
  -q .token)
./config.sh remove --token "$REMOVE_TOKEN"

# clean up the on-disk install
cd ~ && rm -rf ~/actions-runner-legal-corpus
```

After that the repo will no longer see this runner in
`Settings -> Actions -> Runners`.

## Security notes

A self-hosted runner picks up any workflow triggered by push or
pull-request in the repo it is registered to. That has implications
worth naming.

- Only trust workflows authored inside this repo. External PRs from
  forks should not run on this runner without review; GitHub's
  default is to require approval for first-time contributors, and
  that default is worth keeping.
- The runner executes with this user account's privileges. Anything a
  workflow can do, the workflow can also do to files on this Mac.
  Treat workflow changes with the same care as local shell scripts.
- Registration tokens live for 60 minutes and are single-use for a
  given registration. Removal tokens have the same window. Neither is
  committed to the repo; both are fetched fresh via `gh api`.
- The runner never uploads corpus files back to GitHub unless a
  workflow explicitly does so (for example via `actions/upload-artifact`).
  Review new workflows for that pattern if HR4 must be preserved.
