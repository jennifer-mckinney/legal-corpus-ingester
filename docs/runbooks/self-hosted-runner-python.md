# Runbook: seed the self-hosted runner's tool cache with Python for setup-python

Owner decision (ingester copy of terms-analysis#215, option a): keep the self-hosted
macOS arm64 runner and pre-seed its tool cache with the Homebrew Python that matches
`.python-version`. Until this runbook is run once, the five self-hosted jobs (ci `test`,
`health`, `refresh`, `vcr-drift` `drift`, `approval-expiry`) FAIL at the `Set up Python`
step. The two ubuntu-latest jobs in `p9-review.yml` are unaffected.

Moving those jobs to GitHub-hosted runners (option c) stays the decision tracked in
ingester #19. This runbook does not replace it.

## Why this is needed

- `actions/setup-python` first looks in the runner tool cache for a version matching
  the spec. If nothing matches, it downloads a build from `actions/python-versions`.
- On macOS those builds are not relocatable. They must be installed under
  `/Users/runner/hostedtoolcache`, and the install script runs `sudo installer`. This
  runner has no passwordless sudo and no `/Users/runner`, so the download path fails.
- If a matching version is already in the tool cache, setup-python uses it and
  downloads nothing. That is what this runbook sets up, without sudo.

## What setup-python v5.6.0 looks for

- Tool cache root: `RUNNER_TOOL_CACHE`. On a self-hosted runner this defaults to
  `<runner-dir>/_work/_tool`. Create `_work/_tool` if the runner has never made it.
- Python entry: `$RUNNER_TOOL_CACHE/Python/<x.y.z>/arm64/` plus an empty marker FILE
  `$RUNNER_TOOL_CACHE/Python/<x.y.z>/arm64.complete` next to it. Without the marker,
  the entry is ignored.
- `arm64` is Node's `os.arch()` on Apple silicon, the default when the workflow sets no
  `architecture:` (ours sets none).
- Version matching: `.python-version` holds `3.14`. The action reads it with whitespace
  trimmed and treats it as a semver range (`>=3.14.0 <3.15.0`, no pre-releases). Among the
  cache directories with a marker, it picks the HIGHEST `x.y.z` that satisfies the range.
  A `3.13.x` or `3.15.x` entry never matches.
- Once found, it adds `<entry>` and `<entry>/bin` to `PATH` and sets `pythonLocation`.
  The workflow's `python3 -m venv .venv` then needs `bin/python3` to work. setup-python
  reports `bin/python` as its `python-path` output, so provide that too.

## One-time steps (no sudo; run as the user the runner service runs as)

Replace `<runner-dir>` with the runner's install directory. Do not change anything else
in the runner installation.

```bash
python3 --version
```

That should print `Python 3.14.x` (Homebrew's `python@3.14`). If it prints a different minor
version, install the matching formula first (`brew install python@3.14`).

```bash
SRC="$(brew --prefix python@3.14)/Frameworks/Python.framework/Versions/3.14"
```

`brew --prefix` gives the stable `opt` path, not the versioned `Cellar` path, so a Homebrew
patch upgrade does not leave dangling links.

```bash
VER="$("$SRC/bin/python3" -c 'import platform; print(platform.python_version())')"
```

```bash
DEST="<runner-dir>/_work/_tool/Python/$VER/arm64"
```

```bash
mkdir -p "$DEST/bin"
```

```bash
ln -s "$SRC/lib" "$DEST/lib" && ln -s "$SRC/include" "$DEST/include"
```

```bash
for n in python python3 python3.14; do ln -s "$SRC/bin/python3.14" "$DEST/bin/$n"; done
```

```bash
for n in pip pip3 pip3.14; do ln -s "$SRC/bin/pip3.14" "$DEST/bin/$n"; done
```

```bash
touch "<runner-dir>/_work/_tool/Python/$VER/arm64.complete"
```

Homebrew's framework `bin/` has `python3` and `python3.14` but no plain `python`. That is
why `arm64/` is a real directory with its own `bin/` of symlinks, not a symlink to the
framework directory. The framework itself is never modified.

Check the entry locally before restarting:

```bash
"$DEST/bin/python" -c 'import sys, ssl, sqlite3; print(sys.version)'
```

Restart the runner service from the runner directory. The cache is read from disk on every
job, so a restart is a precaution, not a requirement:

```bash
cd "<runner-dir>" && ./svc.sh stop && ./svc.sh start
```

## Verify in CI

1. Dispatch a workflow that runs on the runner, such as `health.yml` (Actions tab, Run
   workflow) or `gh workflow run health.yml --ref <branch>`.
2. Open the `Set up Python` step log. Expect `Resolved .python-version as 3.14`, then a
   successful step with no "not found in the local cache" line and no download. The
   step's `python-version` output is the cache directory name, for example `3.14.3`.
3. For more detail, re-run with debug logging (set the `ACTIONS_STEP_DEBUG` secret to
   `true`). The log then shows `checking cache: .../_tool/Python/<x.y.z>/arm64` and
   `Found tool in cache Python <x.y.z> arm64`.

## When Homebrew's Python changes

- **Patch upgrade (3.14.3 to 3.14.4):** the symlinks go through the `opt` path, so they
  still resolve and jobs keep passing. The cache directory name is now out of date, though,
  so setup-python reports `3.14.3` while running 3.14.4. Re-seed: repeat the steps (new
  `VER`), then delete the old `Python/3.14.3` directory and its `arm64.complete`. If you
  keep both, the higher one wins anyway.
- **`python@3.14` uninstalled or its prefix moved:** the marker is still there, so
  setup-python reports success, but `python3 -m venv` in the `Install` step fails because
  the links are dangling. Reinstall `python@3.14` or re-seed.
- **`.python-version` bumped (for example to `3.15`):** no cache entry matches. setup-python
  tries the download and fails as described above. Seed the new minor version with these
  same steps, using `python@3.15` and `Versions/3.15`, in the same PR that bumps the pin.

## Sources

- setup-python v5.6.0 `README.md`, section "Using setup-python with a self-hosted runner",
  and `docs/advanced-usage.md`, sections "Hosted tool cache" and "macOS" (the
  `/Users/runner/hostedtoolcache` and non-relocatable requirement).
- setup-python v5.6.0 source: `src/find-python.ts` (`useCpythonVersion`,
  `pythonVersionToSemantic`, the `bin` directory and `PATH` exports), `src/utils.ts`
  (`getVersionInputFromPlainFile` trims the file), and `src/setup-python.ts` (default
  architecture `os.arch()`).
- `@actions/tool-cache` `find`, `findAllVersions` and `evaluateVersions`: the `<arch>.complete`
  marker, and the highest version that satisfies the range.
- GitHub Docs, "Setting up the tool cache on self-hosted runners without internet access":
  the default tool cache directory is `RUNNER_DIR/_work/_tool`.
- GitHub Docs, "Configuring the self-hosted runner application as a service": `svc.sh`.
