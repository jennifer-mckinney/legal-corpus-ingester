# Evidence: self-hosted runner tool-cache seed for setup-python (2026-10-10)

Runbook executed: `docs/runbooks/self-hosted-runner-python.md`
Executor: Claude Code agent, as user `jennifermckinney` (the runner service user). No sudo, nothing under `/opt/homebrew` modified, runner service not restarted, nothing committed or pushed.

## Result summary

| Item | Value |
|---|---|
| VER | 3.14.3 (Homebrew python@3.14) |
| Local seed (steps 1-8) | DONE |
| Local verify (step 9) | PASS: `sys.version` 3.14.3, ssl + sqlite3 import, `python3 -m venv` works |
| CI run | 38071582790, `health.yml`, branch `chore/python-version-pin` |
| CI conclusion | **failure** at `Set up Python` |
| headSha vs worktree HEAD | MATCH (`66b56c3eecd99fea3fadb16caea8bb2a206befe8`) |
| Root cause | setup-python forces `RUNNER_TOOL_CACHE=/Users/runner/hostedtoolcache` on macOS; `_work/_tool` is never consulted. Runbook premise does not hold on macOS. |

## Local steps 1-10 (full output)

```
== Step 1-2: SRC / VER ==
SRC=/opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/3.14
VER=3.14.3

== Step 3-4: DEST / mkdir ==
DEST=/Users/jennifermckinney/actions-runner-legal-corpus/_work/_tool/Python/3.14.3/arm64
mkdir ok

== Step 5: lib/include symlinks ==
lib/include linked

== Step 6: python symlinks ==
linked bin/python
linked bin/python3
linked bin/python3.14

== Step 7: pip symlinks ==
linked bin/pip
linked bin/pip3
linked bin/pip3.14

== Step 8: marker ==
marker touched

== Step 9: verify ==
3.14.3 (main, Feb  3 2026, 15:32:20) [Clang 17.0.0 (clang-1700.6.3.2)]
Python 3.14.3
venv check ok, cleaned up

== Step 10: listings ==
--- ls -la Python/3.14.3
total 0
drwxr-xr-x  4 jennifermckinney  staff  128 Oct 10 10:23 .
drwxr-xr-x  3 jennifermckinney  staff   96 Oct 10 10:23 ..
drwxr-xr-x  5 jennifermckinney  staff  160 Oct 10 10:23 arm64
-rw-r--r--  1 jennifermckinney  staff    0 Oct 10 10:23 arm64.complete
--- ls -la DEST DEST/bin
/Users/jennifermckinney/actions-runner-legal-corpus/_work/_tool/Python/3.14.3/arm64:
total 0
drwxr-xr-x  5 jennifermckinney  staff  160 Oct 10 10:23 .
drwxr-xr-x  4 jennifermckinney  staff  128 Oct 10 10:23 ..
drwxr-xr-x  8 jennifermckinney  staff  256 Oct 10 10:23 bin
lrwxr-xr-x  1 jennifermckinney  staff   79 Oct 10 10:23 include -> /opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/3.14/include
lrwxr-xr-x  1 jennifermckinney  staff   75 Oct 10 10:23 lib -> /opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/3.14/lib

/Users/jennifermckinney/actions-runner-legal-corpus/_work/_tool/Python/3.14.3/arm64/bin:
total 0
drwxr-xr-x  8 jennifermckinney  staff  256 Oct 10 10:23 .
drwxr-xr-x  5 jennifermckinney  staff  160 Oct 10 10:23 ..
lrwxr-xr-x  1 jennifermckinney  staff   83 Oct 10 10:23 pip -> /opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/3.14/bin/pip3.14
lrwxr-xr-x  1 jennifermckinney  staff   83 Oct 10 10:23 pip3 -> /opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/3.14/bin/pip3.14
lrwxr-xr-x  1 jennifermckinney  staff   83 Oct 10 10:23 pip3.14 -> /opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/3.14/bin/pip3.14
lrwxr-xr-x  1 jennifermckinney  staff   86 Oct 10 10:23 python -> /opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/3.14/bin/python3.14
lrwxr-xr-x  1 jennifermckinney  staff   86 Oct 10 10:23 python3 -> /opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/3.14/bin/python3.14
lrwxr-xr-x  1 jennifermckinney  staff   86 Oct 10 10:23 python3.14 -> /opt/homebrew/opt/python@3.14/Frameworks/Python.framework/Versions/3.14/bin/python3.14

LOCAL STEPS COMPLETE
```

## CI steps 11-12 (full output)

```
== Step 11: dispatch ==
worktree HEAD: 66b56c3eecd99fea3fadb16caea8bb2a206befe8
branch: chore/python-version-pin
dispatched at 2026-10-10T17:24:09Z

== Step 12: find run (after ~20s) ==
[{"conclusion":"","databaseId":38071582790,"headSha":"66b56c3eecd99fea3fadb16caea8bb2a206befe8","status":"in_progress"}]
```

## CI steps 13-14 (full output)

```
== Step 13: watch run 38071582790 ==
Refreshing run status every 3 seconds. Press Ctrl+C to quit.

* chore/python-version-pin Nightly Health Check jennifer-mckinney/legal-corpus-ingester#69 · 38071582790
Triggered via workflow_dispatch less than a minute ago

JOBS
* health (ID 114269903291)
  ✓ Set up job
  ✓ Run actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
  * Set up Python
  * Install
  * Run ingester status
  * Run health check
  * Upload health report
  * Post Set up Python
  * Post Run actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
Refreshing run status every 3 seconds. Press Ctrl+C to quit.

* chore/python-version-pin Nightly Health Check jennifer-mckinney/legal-corpus-ingester#69 · 38071582790
Triggered via workflow_dispatch less than a minute ago

JOBS
* health (ID 114269903291)
  ✓ Set up job
  ✓ Run actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
  X Set up Python
  - Install
  - Run ingester status
  - Run health check
  ✓ Upload health report
  - Post Set up Python
  ✓ Post Run actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
  ✓ Complete job
X chore/python-version-pin Nightly Health Check jennifer-mckinney/legal-corpus-ingester#69 · 38071582790
Triggered via workflow_dispatch less than a minute ago

JOBS
X health in 29s (ID 114269903291)
  ✓ Set up job
  ✓ Run actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
  X Set up Python
  - Install
  - Run ingester status
  - Run health check
  ✓ Upload health report
  - Post Set up Python
  ✓ Post Run actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
  ✓ Complete job

ANNOTATIONS
! Node.js 20 is deprecated. The following actions target Node.js 20 but are being forced to run on Node.js 24: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683, actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065, actions/upload-artifact@4cec3d8aa04e39d1a68397de0c4cd6fb9dce8ec1. For more information see: https://github.blog/changelog/2025-09-19-deprecation-of-node-20-on-github-actions-runners/
health: .github#2

! No files were found with the provided path: out/health/. No artifacts will be uploaded.
health: .github#12

X The process '/bin/bash' failed with exit code 1
health: .github#23

X mkdir: /Users/runner: Permission denied
health: .github#22

watch exit code: 1

== Step 13b: final run state ==
{"conclusion":"failure","databaseId":38071582790,"headSha":"66b56c3eecd99fea3fadb16caea8bb2a206befe8","status":"completed","url":"https://github.com/jennifer-mckinney/legal-corpus-ingester/actions/runs/38071582790"}

== Step 13c: Set up Python log lines ==
health	Run actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683	2026-10-10T17:24:31.1106680Z [command]/opt/homebrew/bin/git -c protocol.version=2 fetch --no-tags --prune --no-recurse-submodules --depth=1 origin +66b56c3eecd99fea3fadb16caea8bb2a206befe8:refs/remotes/origin/chore/python-version-pin
health	Run actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683	2026-10-10T17:24:31.8785800Z  * [new ref]         66b56c3eecd99fea3fadb16caea8bb2a206befe8 -> origin/chore/python-version-pin
health	Run actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683	2026-10-10T17:24:31.9523970Z [command]/opt/homebrew/bin/git checkout --progress --force -B chore/python-version-pin refs/remotes/origin/chore/python-version-pin
health	Run actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683	2026-10-10T17:24:32.0778560Z Switched to a new branch 'chore/python-version-pin'
health	Run actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683	2026-10-10T17:24:32.0818440Z branch 'chore/python-version-pin' set up to track 'origin/chore/python-version-pin'.
health	Set up Python	2026-10-10T17:24:32.3777890Z   python-version-file: .python-version
health	Set up Python	2026-10-10T17:24:32.7705370Z Resolved .python-version as 3.14
health	Set up Python	2026-10-10T17:24:32.7712780Z Version 3.14 was not found in the local cache
health	Set up Python	2026-10-10T17:24:33.8168440Z Download from "https://github.com/actions/python-versions/releases/download/3.14.8-36806082737/python-3.14.8-darwin-arm64.tar.gz"
health	Set up Python	2026-10-10T17:24:40.6192940Z ##[error]mkdir: /Users/runner: Permission denied
health	Set up Python	2026-10-10T17:24:40.6196970Z ##[error]The process '/bin/bash' failed with exit code 1

== Step 14: headSha vs worktree HEAD ==
run headSha:   66b56c3eecd99fea3fadb16caea8bb2a206befe8
worktree HEAD: 66b56c3eecd99fea3fadb16caea8bb2a206befe8
MATCH
```

Run URL: https://github.com/jennifer-mckinney/legal-corpus-ingester/actions/runs/38071582790

## Key `Set up Python` log lines

```
python-version-file: .python-version
Resolved .python-version as 3.14
Version 3.14 was not found in the local cache
Download from "https://github.com/actions/python-versions/releases/download/3.14.8-36806082737/python-3.14.8-darwin-arm64.tar.gz"
##[error]mkdir: /Users/runner: Permission denied
##[error]The process '/bin/bash' failed with exit code 1
```

Expected per runbook: `Resolved .python-version as 3.14` then success with no "not found in the local cache" line. Observed: the "not found" line, then the download path, then the known no-sudo failure.

## Read-only diagnosis (why the seed was not consulted)

### setup-python dist on the runner (pinned sha a26af69be951a213d495a4c3e4e4022e16d87065)

File: /Users/jennifermckinney/actions-runner-legal-corpus/_work/_actions/actions/setup-python/a26af69be951a213d495a4c3e4e4022e16d87065/dist/setup/index.js

```js
function run() {
    return __awaiter(this, void 0, void 0, function* () {
        var _a;
        if (utils_1.IS_MAC) {
            process.env['AGENT_TOOLSDIRECTORY'] = '/Users/runner/hostedtoolcache';
        }
        if ((_a = process.env.AGENT_TOOLSDIRECTORY) === null || _a === void 0 ? void 0 : _a.trim()) {
            process.env['RUNNER_TOOL_CACHE'] = process.env['AGENT_TOOLSDIRECTORY'];
        }
        core.debug(`Python is expected to be installed into ${process.env['RUNNER_TOOL_CACHE']}`);
```

grep -n -E "hostedtoolcache|AGENT_TOOLSDIRECTORY":
```
96970:            process.env['AGENT_TOOLSDIRECTORY'] = '/Users/runner/hostedtoolcache';
96972:        if ((_a = process.env.AGENT_TOOLSDIRECTORY) === null || _a === void 0 ? void 0 : _a.trim()) {
96973:            process.env['RUNNER_TOOL_CACHE'] = process.env['AGENT_TOOLSDIRECTORY'];
```

`run()` executes this BEFORE any cache lookup. On macOS (`IS_MAC`), it overwrites
`AGENT_TOOLSDIRECTORY` with `/Users/runner/hostedtoolcache` and then copies that into
`RUNNER_TOOL_CACHE`. `@actions/tool-cache` `find()` / `findAllVersions()` read
`process.env.RUNNER_TOOL_CACHE`, so on macOS they look under
`/Users/runner/hostedtoolcache/Python/<ver>/arm64`, never under
`<runner-dir>/_work/_tool`. The override is unconditional: it cannot be defeated by setting
`AGENT_TOOLSDIRECTORY` or `RUNNER_TOOL_CACHE` in the runner `.env` or in the workflow `env:`.

### Runner configuration

```
runner dir:            /Users/jennifermckinney/actions-runner-legal-corpus
.runner workFolder:    _work
.env keys:             LANG 
.env/.path override:   none for AGENT_TOOLSDIRECTORY / RUNNER_TOOL_CACHE
/Users/runner:         does not exist (ls: No such file or directory)
runner bin dirs:       bin bin.2.336.0 bin.2.337.0 
```

### Seeded entry (left in place, harmless, unused by setup-python on macOS)

```
/Users/jennifermckinney/actions-runner-legal-corpus/_work/_tool/Python/3.14.3/arm64.complete
/Users/jennifermckinney/actions-runner-legal-corpus/_work/_tool/Python/3.14.3/arm64/{bin,lib,include}
```

## Conclusion

The runbook's one-time steps completed and the entry is valid (local verify passed), but the
runbook's premise in "What setup-python v5.6.0 looks for" (tool cache root = `RUNNER_TOOL_CACHE`
= `<runner-dir>/_work/_tool`) is wrong on macOS. setup-python's `run()` hardcodes the macOS
tool cache to `/Users/runner/hostedtoolcache` before every lookup. Seeding `_work/_tool` has
no effect on this runner; the step still takes the download path and fails on
`mkdir /Users/runner`.

Paths forward (owner decision, not taken here):
1. Create `/Users/runner/hostedtoolcache` owned by the runner user (one-time `sudo mkdir -p
   /Users/runner/hostedtoolcache && sudo chown -R jennifermckinney /Users/runner`) and seed
   `Python/3.14.3/arm64` + `arm64.complete` there with the same symlink layout. Requires sudo once.
2. Drop `actions/setup-python` for the self-hosted jobs and use the Homebrew interpreter on
   `PATH` directly (`python3 -m venv .venv`), guarding with a version check against
   `.python-version`. No sudo, no tool cache.
3. Option c from ingester #19 (GitHub-hosted runners), already tracked.

The runbook needs correcting either way; its "Sources" section cites `src/setup-python.ts`
default architecture but misses the `IS_MAC` tool-cache override in the same file.
