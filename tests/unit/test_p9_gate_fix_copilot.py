"""Fix-coder unit tests for the Copilot round on the P9 pre-push gate (terms-analysis#175).

Covers what the acceptance suite (test_p9_gate_copilot.py) does not pin:

- C1 error paths of the live-advertisement read: no push URL, unparseable
  `git ls-remote` output, `git cat-file` failing or answering oddly; each one
  refuses with "cannot establish which commits are new to <remote>".
- C1 unknown objects: an advertised id this clone never fetched is left out
  and nothing is fetched. Beside known history that is harmless; when it is
  the only proof that a commit is on the remote, the push is refused
  (fail closed). Annotated tags count, blob refs are ignored.
- The advertisement is read once per push and never for deletions or
  complete overrides.
- C3 CI wiring: scripts/ci/p9-sibling-parity.sh (resolve-ref, check) and
  the workflow steps that call it, run with fake git / python.

Sandbox commands reuse the acceptance harness: throwaway main checkout and
local bare remotes under tmp_path, isolated HOME and git config. The CI
steps run in a tmp_path copy of the layout; nothing contacts GitHub.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.unit.test_p9_gate_copilot import SIBLING_REPO
from tests.unit.test_p9_gate_fix_r1 import _pass, _signoff
from tests.unit.test_p9_prepush_gate import (  # noqa: F401
    REPO_ROOT,
    Sandbox,
    _git,
    _run,
    pytestmark,
)

# Repo-specific wiring (the only lines that differ from the sibling's copy).
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SUITE_STEP = "Unit tests"
PARITY_NODE = "tests/unit/test_p9_gate_r5.py::test_p9_shared_files_match_the_sibling_at_the_resolved_sha"

PARITY_SCRIPT = REPO_ROOT / "scripts" / "ci" / "p9-sibling-parity.sh"
RESOLVE_STEP = "Resolve sibling ref for P9 hook parity"
ZERO_SHA = "0" * 40
NOT_ESTABLISHED = "cannot establish which commits are new to origin"


@pytest.fixture
def installed(tmp_path: Path) -> Sandbox:
    sb = Sandbox(tmp_path)
    proc = sb.install("main")
    assert proc.returncode == 0, proc.stderr
    return sb


def _hook(
    sb: Sandbox, stdin: str, *args: str, path: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the installed hook as git would, with chosen arguments and PATH."""
    env = dict(sb.env)
    if path is not None:
        env["PATH"] = path
    return _run_in(sb, [str(sb.main / ".githooks" / "pre-push"), *args], env, stdin)


def _run_in(
    sb: Sandbox, argv: list[str], env: dict[str, str], stdin: str
) -> subprocess.CompletedProcess[str]:
    root = Path(sb.env["P9_SANDBOX_ROOT"])
    assert sb.main.resolve() == root or root in sb.main.resolve().parents
    return subprocess.run(
        argv,
        cwd=sb.main,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _wrapped_git(tmp_path: Path, script: str) -> str:
    """A PATH whose `git` runs `script` (sh) first, then the real git.

    The script sees the git arguments as "$@" and may exit early to fake a
    subcommand. The real git is in $REAL_GIT.
    """
    real = shutil.which("git")
    assert real
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "git"
    fake.write_text(f'#!/bin/sh\nREAL_GIT="{real}"\n{script}\nexec "$REAL_GIT" "$@"\n')
    fake.chmod(0o755)
    return f"{bindir}{os.pathsep}{os.environ['PATH']}"


def _one_new_commit(sb: Sandbox) -> tuple[str, str]:
    seed = sb.remote_sha("main")
    assert seed is not None
    tip = sb.commit(sb.main, "one-new.txt")
    _signoff(sb, sb.main, tip, _pass(tip))
    return seed, tip


def _line(tip: str, ref: str = "refs/heads/main") -> str:
    return f"{ref} {tip} {ref} {ZERO_SHA}\n"


def _other_clone_commit(sb: Sandbox, branch: str) -> str:
    """Push a commit to the bare remote from a second clone, so the main
    checkout never has the object."""
    other = sb.root / f"other-{branch}"
    _git(sb.root, sb.env, "clone", "-q", str(sb.remote), str(other))
    (other / f"{branch}.txt").write_text(branch)
    _git(other, sb.env, "add", f"{branch}.txt")
    _git(other, sb.env, "commit", "-q", "-m", branch)
    sha = _git(other, sb.env, "rev-parse", "HEAD")
    _git(other, sb.env, "push", "-q", "--no-verify", "origin", f"HEAD:refs/heads/{branch}")
    return sha


def _local_has(sb: Sandbox, sha: str) -> bool:
    proc = _run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], sb.main, sb.env)
    return proc.returncode == 0


# C1 error paths ------------------------------------------------------------


def test_missing_push_url_refuses(installed: Sandbox) -> None:
    _seed, tip = _one_new_commit(installed)

    proc = _hook(installed, _line(tip), "origin")

    assert proc.returncode == 1
    assert "git gave the hook no push URL" in proc.stderr, proc.stderr
    assert NOT_ESTABLISHED in proc.stderr
    assert "signoff OK" not in proc.stdout


@pytest.mark.parametrize(
    "listing",
    [
        pytest.param("garbage", id="not-a-ref-line"),
        pytest.param("{sha} refs/heads/main", id="space-not-tab"),
        pytest.param("{short}\trefs/heads/main", id="short-id"),
        pytest.param("{upper}\trefs/heads/main", id="uppercase-id"),
        pytest.param("{sha}\trefs/heads/main\n\n{sha}\trefs/heads/x", id="blank-line"),
        pytest.param("{sha}\t", id="no-ref-name"),
        pytest.param("{sha}\trefs/heads/a b", id="ref-with-space"),
    ],
)
def test_unparseable_advertisement_refuses(
    installed: Sandbox, tmp_path: Path, listing: str
) -> None:
    seed, tip = _one_new_commit(installed)
    text = listing.format(sha=seed, short=seed[:12], upper=seed.upper())
    (tmp_path / "listing.txt").write_text(text + "\n")
    path = _wrapped_git(
        tmp_path,
        f'[ "$1" = ls-remote ] && [ "$2" != --get-url ] && {{ cat "{tmp_path / "listing.txt"}"; exit 0; }}',
    )

    proc = _hook(installed, _line(tip), "origin", str(installed.remote), path=path)

    assert proc.returncode == 1
    assert "cannot parse the refs origin advertises" in proc.stderr, proc.stderr
    assert NOT_ESTABLISHED in proc.stderr


@pytest.mark.parametrize(
    ("fake", "needle", "git_said"),
    [
        pytest.param(
            '[ "$2" = --get-url ] && exit 128',
            "git cannot resolve the URL it would read from",
            False,
            id="get-url-fails",
        ),
        pytest.param('[ "$2" = -- ] && exit 128', "(git ls-remote failed)", False, id="fails-silently"),
        pytest.param(
            '[ "$2" = -- ] && { printf "warning: x\\nfatal: \\033]0;x\\007 boom\\n" >&2; exit 128; }',
            "git said: fatal: \\x1b]0;x\\x07 boom",
            True,
            id="fails-with-hostile-stderr",
        ),
        pytest.param(
            '[ "$2" = -- ] && { exec >&- 2>&-; sleep 10; }',
            "git ls-remote timed out after 2s",
            False,
            id="closes-its-output-then-hangs",
        ),
    ],
)
def test_advertisement_read_failures_refuse(
    installed: Sandbox, tmp_path: Path, fake: str, needle: str, git_said: bool
) -> None:
    """r5: each way the read can fail refuses, says why, and shows git's own
    last stderr line only escaped."""
    _seed, tip = _one_new_commit(installed)
    installed.env["P9_ADVERT_TIMEOUT_SECONDS"] = "2"
    path = _wrapped_git(tmp_path, f'[ "$1" = ls-remote ] && {{ {fake}; }}')

    proc = _hook(installed, _line(tip), "origin", str(installed.remote), path=path)

    assert proc.returncode == 1
    assert needle in proc.stderr, proc.stderr
    assert NOT_ESTABLISHED in proc.stderr
    assert ("git said: " in proc.stderr) is git_said, proc.stderr
    assert "\x1b" not in proc.stderr and "\x07" not in proc.stderr


@pytest.mark.parametrize(
    ("fake", "needle"),
    [
        pytest.param("exit 128", "git cat-file failed", id="cat-file-fails"),
        pytest.param("echo nonsense; exit 0", "unexpected answer 'nonsense'", id="odd-answer"),
        pytest.param("exit 0", "unexpected answer ''", id="no-answer"),
        pytest.param(
            'exec "$REAL_GIT" "$@" | while read -r l; do echo "$l"; echo "$l"; done',
            "answers for",  # each answer doubled: 2n answers for n refs
            id="extra-answer",
        ),
    ],
)
def test_cat_file_problems_refuse(
    installed: Sandbox, tmp_path: Path, fake: str, needle: str
) -> None:
    _seed, tip = _one_new_commit(installed)
    path = _wrapped_git(tmp_path, f'[ "$1" = cat-file ] && {{ {fake}; }}')

    proc = _hook(installed, _line(tip), "origin", str(installed.remote), path=path)

    assert proc.returncode == 1
    assert needle in proc.stderr, proc.stderr
    assert NOT_ESTABLISHED in proc.stderr


# C1 unknown objects: left out, never fetched -------------------------------


def test_unknown_advertised_commit_beside_known_history_is_ignored(installed: Sandbox) -> None:
    """The remote has main = seed (known) and `other` = x (never fetched)."""
    x = _other_clone_commit(installed, "other")
    _seed, tip = _one_new_commit(installed)
    assert not _local_has(installed, x)

    proc = _hook(installed, _line(tip), "origin", str(installed.remote))

    assert proc.returncode == 0, proc.stderr
    assert f"signoff OK for refs/heads/main at {tip[:12]}" in proc.stdout
    assert not _local_has(installed, x), "the hook fetched an advertised object"


def test_unknown_advertised_commit_as_the_only_proof_refuses(installed: Sandbox) -> None:
    """The remote's only ref is x, a child of seed this clone never fetched.
    Seed is on the remote but nothing local proves it, so seed counts as new
    and the tip-only signoff is refused: the documented fail-closed choice."""
    x = _other_clone_commit(installed, "moved")
    _seed, tip = _one_new_commit(installed)
    _git(installed.root, installed.env, "--git-dir", str(installed.remote), "update-ref", "-d", "refs/heads/main")

    proc = _hook(installed, _line(tip, "refs/heads/probe"), "origin", str(installed.remote))

    assert proc.returncode == 1
    assert "2 commits are not known to be on origin" in proc.stderr, proc.stderr
    assert "run 'git fetch origin' and push again" in proc.stderr, proc.stderr
    assert not _local_has(installed, x)


def test_annotated_tag_counts_and_blob_ref_is_ignored(installed: Sandbox) -> None:
    """The remote holds seed only through an annotated tag, plus a ref to a
    blob. The tag peels to seed, the blob is left out without an error."""
    seed, tip = _one_new_commit(installed)
    _git(installed.main, installed.env, "tag", "-a", "-m", "seed", "v0", seed)
    (installed.root / "blob.txt").write_text("not a commit\n")
    blob = _git(installed.main, installed.env, "hash-object", "-w", str(installed.root / "blob.txt"))
    _git(installed.main, installed.env, "update-ref", "refs/blobs/one", blob)
    _git(installed.main, installed.env, "push", "-q", "--no-verify", "origin", "refs/tags/v0", "refs/blobs/one")
    _git(installed.root, installed.env, "--git-dir", str(installed.remote), "update-ref", "-d", "refs/heads/main")

    proc = _hook(installed, _line(tip, "refs/heads/probe"), "origin", str(installed.remote))

    assert proc.returncode == 0, proc.stderr
    assert "signoff OK" in proc.stdout


# When the advertisement is read --------------------------------------------


def _counting_git(tmp_path: Path) -> tuple[str, Path]:
    log = tmp_path / "ls-remote.calls"
    script = f'[ "$1" = ls-remote ] && [ "$2" != --get-url ] && echo call >> "{log}"'
    return _wrapped_git(tmp_path, script), log


def test_advertisement_is_read_once_per_push(installed: Sandbox, tmp_path: Path) -> None:
    _seed, tip = _one_new_commit(installed)
    path, log = _counting_git(tmp_path)
    stdin = _line(tip) + _line(tip, "refs/heads/second")

    proc = _hook(installed, stdin, "origin", str(installed.remote), path=path)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("signoff OK") == 2
    assert log.read_text().splitlines() == ["call"]


def test_failed_advertisement_refuses_every_ref(installed: Sandbox) -> None:
    _seed, tip = _one_new_commit(installed)
    stdin = _line(tip) + _line(tip, "refs/heads/second")

    proc = _hook(installed, stdin, "origin", str(installed.root / "missing.git"))

    assert proc.returncode == 1
    assert proc.stderr.count(NOT_ESTABLISHED) == 2, proc.stderr
    assert "signoff OK" not in proc.stdout


def test_deletion_and_override_never_contact_the_remote(
    installed: Sandbox, tmp_path: Path
) -> None:
    tip = installed.commit(installed.main, "override.txt")
    _signoff(
        installed,
        installed.main,
        tip,
        {"head_sha": tip, "override": {"used": True, "reason": "test", "authorized_by": "test"}},
    )
    path, log = _counting_git(tmp_path)
    stdin = f"(delete) {ZERO_SHA} refs/heads/gone {'a' * 40}\n" + _line(tip)

    proc = _hook(installed, stdin, "origin", str(installed.root / "missing.git"), path=path)

    assert proc.returncode == 0, proc.stderr
    assert "deleting refs/heads/gone on origin" in proc.stdout
    assert "P9 OVERRIDE ACTIVE" in proc.stderr
    assert not log.exists(), "the hook contacted the remote for a deletion / override"


# C3: scripts/ci/p9-sibling-parity.sh ---------------------------------------


def _script(
    tmp_path: Path, *args: str, path: str | None = None, extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if path is not None:
        env["PATH"] = path
    env.update(extra or {})
    return subprocess.run(
        ["bash", str(PARITY_SCRIPT), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _fake_ls_remote(tmp_path: Path, body: str) -> tuple[str, Path]:
    """Fake `git ls-remote` printing `body`; records its arguments."""
    argv_log = tmp_path / "ls-remote.argv"
    script = (
        f'if [ "$1" = ls-remote ]; then printf "%s\\n" "$@" > "{argv_log}"; {body}; fi'
    )
    return _wrapped_git(tmp_path, script), argv_log


SIB = "jennifer-mckinney/" + SIBLING_REPO
BRANCH = "feat/g0-5-p9-gate"


def test_resolve_ref_uses_the_head_branch_when_the_sibling_has_it(tmp_path: Path) -> None:
    path, argv = _fake_ls_remote(tmp_path, f"printf '{'a' * 40}\\trefs/heads/{BRANCH}\\n'; exit 0")

    proc = _script(tmp_path, "resolve-ref", SIB, BRANCH, path=path)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"ref={BRANCH}\nsha={'a' * 40}\n"
    assert argv.read_text().splitlines() == [
        "ls-remote",
        "--heads",
        "--",
        f"https://github.com/{SIB}",
        f"refs/heads/{BRANCH}",
    ]


# A fake GitHub that has only main, at MAIN_SHA ($5 is the ref pattern).
MAIN_SHA = "b" * 40
ONLY_MAIN = f"[ \"$5\" = refs/heads/main ] && printf '{MAIN_SHA}\\trefs/heads/main\\n'; exit 0"


def test_resolve_ref_falls_back_to_main_when_the_sibling_lacks_the_branch(
    tmp_path: Path,
) -> None:
    path, _argv = _fake_ls_remote(tmp_path, ONLY_MAIN)

    proc = _script(tmp_path, "resolve-ref", SIB, "some-other-card", path=path)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"ref=main\nsha={MAIN_SHA}\n"
    assert "has no branch 'some-other-card'; comparing against main" in proc.stderr


def test_resolve_ref_without_a_head_ref_asks_only_for_main(tmp_path: Path) -> None:
    path, argv = _fake_ls_remote(tmp_path, ONLY_MAIN)

    proc = _script(tmp_path, "resolve-ref", SIB, "", path=path)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"ref=main\nsha={MAIN_SHA}\n"
    assert argv.read_text().splitlines() == [
        "ls-remote",
        "--heads",
        "--",
        f"https://github.com/{SIB}",
        "refs/heads/main",
    ]


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        pytest.param("exit 128", "cannot ask github.com/", id="ls-remote-fails"),
        pytest.param("echo junk; exit 0", "unexpected answer", id="junk"),
        pytest.param(
            f"printf '{'a' * 40}\\trefs/heads/{BRANCH}-x\\n'; exit 0",
            "unexpected answer",
            id="other-branch",
        ),
        pytest.param(
            f"printf '{'a' * 40}\\trefs/heads/{BRANCH}\\n{'b' * 40}\\trefs/heads/{BRANCH}\\n'; exit 0",
            "unexpected answer",
            id="two-lines",
        ),
    ],
)
def test_resolve_ref_fails_closed(tmp_path: Path, body: str, needle: str) -> None:
    path, _argv = _fake_ls_remote(tmp_path, body)

    proc = _script(tmp_path, "resolve-ref", SIB, BRANCH, path=path)

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert needle in proc.stderr, proc.stderr


@pytest.mark.parametrize(
    ("args", "code", "needle"),
    [
        pytest.param(["resolve-ref", "not a repo", BRANCH], 1, "is not an owner/repo name", id="bad-repo"),
        pytest.param(["resolve-ref", "a/b", "../x"], 1, "is not a valid branch name", id="bad-ref-dots"),
        pytest.param(["resolve-ref", "a/b", "-x"], 1, "is not a valid branch name", id="bad-ref-dash"),
        pytest.param([], 2, "usage:", id="no-args"),
        pytest.param(["resolve-ref"], 2, "usage:", id="resolve-no-repo"),
        pytest.param(["check"], 2, "usage:", id="check-no-node"),
        pytest.param(["frobnicate"], 2, "usage:", id="unknown-subcommand"),
    ],
)
def test_parity_script_rejects_bad_arguments(
    tmp_path: Path, args: list[str], code: int, needle: str
) -> None:
    path, argv = _fake_ls_remote(tmp_path, "exit 0")

    proc = _script(tmp_path, *args, path=path)

    assert proc.returncode == code
    assert needle in proc.stderr, proc.stderr
    assert not argv.exists()


# The summary line pytest 9.1.1 prints under `PY_COLORS=1` or `FORCE_COLOR=1` (bytes captured
# from a real run). `--color=no` overrides both variables and yields the plain line.
_COLOURED_PASS = "\x1b[32m\x1b[32m\x1b[1m1 passed\x1b[0m\x1b[32m in 0.00s\x1b[0m\x1b[0m"


def _fake_python(
    tmp_path: Path, summary: str, code: int, coloured: str | None = None
) -> tuple[Path, Path]:
    """Fake interpreter: records argv, prints `summary` verbatim, exits `code`.

    The output is read from a file, never embedded in sh source, so quotes, `$`,
    backticks, `\\` and `%` reach the script byte for byte. With `coloured`, the fake
    behaves like pytest: when the environment forces colour and `--color=no` is absent
    from argv, it prints `coloured` instead.
    """
    log = tmp_path / "python.argv"
    fake = tmp_path / "python-fake"
    payload = tmp_path / "pytest.out"
    payload.write_text("....\n" + summary + "\n")
    emit = f'cat "{payload}"'
    if coloured is not None:
        coloured_payload = tmp_path / "pytest-coloured.out"
        coloured_payload.write_text("....\n" + coloured + "\n")
        emit = (
            f'case " $* " in *" --color=no "*) {emit} ;; *)\n'
            f'  if [ "${{PY_COLORS:-}}" = 1 ] || [ -n "${{FORCE_COLOR:-}}" ]; then '
            f'cat "{coloured_payload}"; else {emit}; fi ;;\nesac'
        )
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{log}"\n{emit}\nexit {code}\n')
    fake.chmod(0o755)
    return fake, log


@pytest.mark.parametrize(
    ("summary", "code", "expected"),
    [
        pytest.param("1 passed in 0.10s", 0, 0, id="passed"),
        pytest.param("1 passed, 2 warnings in 0.10s", 0, 0, id="passed-with-warnings"),
        pytest.param("1 passed, 1 warning in 0.10s", 0, 0, id="passed-with-warning"),
        # The default banner pytest prints (no -q effect on it): the guard must read the
        # summary, not demand a bare line (CI defect on PR #11).
        pytest.param("=== 1 passed in 1.21s ===", 0, 0, id="banner-passed"),
        pytest.param("=" * 30 + " 1 passed in 1.21s " + "=" * 31, 0, 0, id="banner-passed-wide"),
        pytest.param("=== 1 passed, 1 warning in 0.5s ===", 0, 0, id="banner-passed-with-warning"),
        pytest.param("=== 1 passed in 61.02s (0:01:01) ===", 0, 0, id="banner-passed-long-run"),
        pytest.param(
            "---------- coverage: platform linux, python 3.12 ----------\n"
            "TOTAL   10   0   100%\n"
            "Required test coverage of 90% reached. Total coverage: 100.00%\n"
            "=== 1 passed in 1.21s ===",
            0, 0, id="coverage-plugin-before-summary",
        ),
        pytest.param(
            "=== 1 passed in 1.21s ===\nCoverage XML written to file coverage.xml",
            0, 0, id="coverage-plugin-after-summary",
        ),
        pytest.param("=== 1 skipped in 0.10s ===", 0, 1, id="banner-skipped"),
        pytest.param("=== 1 passed, 1 skipped in 0.10s ===", 0, 1, id="banner-passed-and-skipped"),
        pytest.param("=== 0 passed in 0.10s ===", 0, 1, id="banner-zero-passed"),
        pytest.param("=== no tests ran in 0.10s ===", 5, 1, id="banner-no-tests"),
        pytest.param("=== 1 failed in 0.10s ===", 1, 1, id="banner-failed"),
        pytest.param("=== 1 failed, 1 passed in 0.10s ===", 0, 1, id="banner-failed-and-passed"),
        pytest.param("=== 1 passed, 1 error in 0.10s ===", 0, 1, id="banner-passed-and-error"),
        pytest.param("=== 1 passed, 1 xfailed in 0.10s ===", 0, 1, id="banner-passed-and-xfailed"),
        pytest.param("=== 1 passed, 1 xpassed in 0.10s ===", 0, 1, id="banner-passed-and-xpassed"),
        pytest.param(
            "=== 1 passed, 1 warning, 1 error in 0.10s ===", 0, 1, id="banner-passed-warning-error"
        ),
        # Shell metacharacters in pytest's output reach the script verbatim (N1).
        pytest.param(
            'note: "quoted" $HOME `echo injected` C:\\temp\\new 100%\n=== 1 passed in 0.10s ===',
            0, 0, id="shell-metacharacters-in-output",
        ),
        pytest.param("=== 11 passed in 0.10s ===", 0, 1, id="banner-eleven-passed"),
        pytest.param("=== 1 passed in 0.10s ===", 1, 1, id="banner-passed-nonzero-exit"),
        # "1 passed" only in an earlier line; the real summary says otherwise.
        pytest.param("1 passed in 0.10s\n=== 2 failed in 0.10s ===", 0, 1, id="earlier-passed-final-failed"),
        pytest.param("1 passed in 0.10s\n=== 1 skipped in 0.10s ===", 0, 1, id="earlier-passed-final-skipped"),
        pytest.param("1 passed in 0.10s\n=== no tests ran in 0.10s ===", 0, 1, id="earlier-passed-final-no-tests"),
        pytest.param("=== 1 skipped in 0.10s ===\nnote: 1 passed in 0.10s", 0, 1, id="skipped-with-passed-in-prose"),
        pytest.param("1 skipped in 0.10s", 0, 1, id="skipped"),
        pytest.param("1 skipped, 2 warnings in 0.10s", 0, 1, id="skipped-with-warnings"),
        pytest.param("2 passed in 0.10s", 0, 1, id="two-passed"),
        pytest.param("1 passed, 1 skipped in 0.10s", 0, 1, id="passed-and-skipped"),
        pytest.param("1 passed, 1 deselected in 0.10s", 0, 1, id="deselected"),
        pytest.param("no tests ran in 0.10s", 5, 1, id="no-tests"),
        pytest.param("1 failed in 0.10s", 1, 1, id="failed"),
        pytest.param("1 passed in 0.10s", 1, 1, id="nonzero-exit"),
        pytest.param("", 0, 1, id="silent"),
    ],
)
def test_check_requires_exactly_one_pass(
    tmp_path: Path, summary: str, code: int, expected: int
) -> None:
    fake, log = _fake_python(tmp_path, summary, code)

    proc = _script(tmp_path, "check", PARITY_NODE, extra={"PYTHON": str(fake)})

    assert proc.returncode == expected, proc.stdout + proc.stderr
    assert summary in proc.stdout
    assert log.read_text().splitlines() == [
        "-m", "pytest", PARITY_NODE, "-q", "-p", "no:cacheprovider", "--color=no",
    ]


@pytest.mark.parametrize(
    "forcing",
    [
        pytest.param({"PY_COLORS": "1"}, id="py-colors"),
        pytest.param({"FORCE_COLOR": "1"}, id="force-color"),
    ],
)
def test_check_accepts_a_real_pass_when_the_runner_forces_colour(
    tmp_path: Path, forcing: dict[str, str]
) -> None:
    # L1: a runner that forces colour must not turn a genuine pass into a failure.
    fake, log = _fake_python(tmp_path, "=== 1 passed in 0.00s ===", 0, coloured=_COLOURED_PASS)

    proc = _script(tmp_path, "check", PARITY_NODE, extra={"PYTHON": str(fake), **forcing})

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "\x1b" not in proc.stdout
    assert "--color=no" in log.read_text().splitlines()


# C3: the workflow runs the script, with fakes ------------------------------


def _steps() -> list[dict[str, Any]]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        names = [step.get("name") for step in job["steps"]]
        if RESOLVE_STEP in names:
            return list(job["steps"])
    raise AssertionError(f"no job in {WORKFLOW.name} has a '{RESOLVE_STEP}' step")


def _step(steps: list[dict[str, Any]], name: str) -> tuple[int, dict[str, Any]]:
    found = [(n, s) for n, s in enumerate(steps) if s.get("name") == name]
    assert len(found) == 1, f"expected one '{name}' step, found {len(found)}"
    return found[0]


def _parity_step(steps: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    found = [(n, s) for n, s in enumerate(steps) if "p9-sibling-parity.sh check" in s.get("run", "")]
    assert len(found) == 1, f"expected one parity check step, found {len(found)}"
    return found[0]


def _layout(tmp_path: Path, step: dict[str, Any]) -> tuple[Path, Path]:
    """A tmp_path copy of the repo layout the step needs: scripts/ linked,
    a stub .venv/bin/activate, and the step's working directory."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "scripts").symlink_to(REPO_ROOT / "scripts")
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "activate").write_text("# stub\n")
    cwd = root / step.get("working-directory", ".")
    cwd.mkdir(parents=True, exist_ok=True)
    return root, cwd


def _run_step(
    tmp_path: Path, step: dict[str, Any], env: dict[str, str], path: str
) -> subprocess.CompletedProcess[str]:
    _root, cwd = _layout(tmp_path, step)
    full = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    full.pop("PYTHON", None)
    full.update(env)
    full["PATH"] = path
    # GitHub runs `run:` with bash -e (plus pipefail on ubuntu and here).
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        cwd=cwd,
        env=full,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize(
    ("head_ref", "listing", "expected"),
    [
        pytest.param(
            BRANCH,
            f"printf '{'a' * 40}\\trefs/heads/{BRANCH}\\n'",
            f"ref={BRANCH}\nsha={'a' * 40}",
            id="pr-shared-branch",
        ),
        pytest.param("other-card", ONLY_MAIN, f"ref=main\nsha={MAIN_SHA}", id="pr-no-sibling-branch"),
        pytest.param("", ONLY_MAIN, f"ref=main\nsha={MAIN_SHA}", id="push-to-main"),
    ],
)
def test_resolve_step_writes_the_sibling_ref(
    tmp_path: Path, head_ref: str, listing: str, expected: str
) -> None:
    steps = _steps()
    _n, step = _step(steps, RESOLVE_STEP)
    assert step["env"] == {"HEAD_REF": "${{ github.head_ref }}"}
    path, argv = _fake_ls_remote(tmp_path, f"{listing}; exit 0")
    output = tmp_path / "github_output"

    proc = _run_step(tmp_path, step, {"HEAD_REF": head_ref, "GITHUB_OUTPUT": str(output)}, path)

    assert proc.returncode == 0, proc.stderr
    assert output.read_text() == f"{expected}\n"
    assert f"https://github.com/{SIB}" in argv.read_text().splitlines()


def test_resolve_step_fails_when_github_cannot_be_asked(tmp_path: Path) -> None:
    _n, step = _step(_steps(), RESOLVE_STEP)
    path, _argv = _fake_ls_remote(tmp_path, "exit 128")
    output = tmp_path / "github_output"

    proc = _run_step(tmp_path, step, {"HEAD_REF": BRANCH, "GITHUB_OUTPUT": str(output)}, path)

    assert proc.returncode != 0
    assert not output.exists() or output.read_text() == ""


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        pytest.param("1 passed in 0.1s", 0, id="passed"),
        pytest.param("1 skipped in 0.1s", 1, id="skipped-is-red"),
    ],
)
def test_parity_step_runs_this_repos_parity_test(
    tmp_path: Path, summary: str, expected: int
) -> None:
    steps = _steps()
    resolve_at, resolve = _step(steps, RESOLVE_STEP)
    parity_at, parity = _parity_step(steps)
    suite_at, suite = _step(steps, SUITE_STEP)
    sha_expr = "${{ steps.%s.outputs.sha }}" % resolve["id"]
    assert resolve_at < parity_at and resolve_at < suite_at
    assert parity["env"] == {"P9_SIBLING_SHA": sha_expr}
    assert suite["env"]["P9_SIBLING_SHA"] == sha_expr
    assert "if" not in parity and "continue-on-error" not in parity
    fake, log = _fake_python(tmp_path, summary, 0)
    bindir = tmp_path / "pybin"
    bindir.mkdir()
    (bindir / "python").symlink_to(fake)

    proc = _run_step(tmp_path, parity, {}, f"{bindir}{os.pathsep}{os.environ['PATH']}")

    assert proc.returncode == expected, proc.stdout + proc.stderr
    assert PARITY_NODE in log.read_text().splitlines()
