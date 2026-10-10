"""One Python version for CI and local dev, pinned in a single file.

Ingester copy of jennifer-mckinney/terms-analysis#215. CI prints whatever
``python3`` the runner happens to have (``Set up Python`` runs
``python3 --version``) and no workflow pins an interpreter, so the version CI
tests is an accident of the machine. The contract below: the repo root holds
one ``.python-version`` file, every ``actions/setup-python`` step in every
workflow reads it through ``python-version-file``, every job that runs
``python`` sets it up that way before the first such step, no workflow
restates a version anywhere, the pinned version satisfies every Python floor
the repo declares, and the one test known to behave differently on 3.14 still
runs (not skipped, not xfailed).

Static checks over YAML by necessity: setup-python only runs on a GitHub
runner. The checkers are pinned by the vectors tables below so a weakened
checker goes red here, not silently green.
"""

from __future__ import annotations

import configparser
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
VERSION_FILE_NAME = ".python-version"
VERSION_FILE = REPO_ROOT / VERSION_FILE_NAME

# Acceptance value: the interpreter CI and local dev share; setup-python
# installs it on ubuntu-latest from the pin. This is the expectation the pin
# is checked against, deliberately not read back from the pin itself (a test
# that reads its answer from the file under test cannot fail).
TARGET_VERSION = (3, 14)

SETUP_PYTHON = "actions/setup-python"
VERSION_FILE_INPUT = "python-version-file"
HARD_CODED_INPUT = "python-version"

# A python interpreter as a shell word: bare, by path, or versioned
# (python, python3, /usr/bin/python3, python3.14). Not python-dotenv,
# python3-venv, pythonista, mypython or check_verdict.py.
_PYTHON_WORD = re.compile(r"(?<![\w.-])python(?:3(?:\.[0-9]+)?)?(?![\w.-])", re.ASCII)
_ECHO_WORDS = frozenset({"echo", "printf"})

# Exactly "3.<minor>", ASCII digits only, no leading zero, at most 3 digits
# (bounds int() on a hostile multi-megabyte digit run). One optional "\n".
_VERSION_LINE = re.compile(r"3\.(0|[1-9][0-9]{0,2})", re.ASCII)
_RUFF_TARGET = re.compile(r"py3([0-9]{1,3})", re.ASCII)

# The test whose outcome differs on 3.14 (terms-analysis#191, #265): before
# 3.14 the deep input raises RecursionError, on 3.14 it parses. Node id
# relative to the repo root, the directory CI runs pytest in.
DEEPLY_NESTED_NODE = (
    "tests/unit/test_p9_review_workflow.py::test_gate_fails_closed_on_deeply_nested_json"
)


# --- checkers ----------------------------------------------------------------


def _parse_version_file(raw: bytes) -> tuple[int, int]:
    """Return (major, minor) from `.python-version` bytes, or raise ValueError."""
    text = raw.decode("utf-8")  # UnicodeDecodeError is a ValueError
    if text.endswith("\n"):
        text = text[:-1]
    match = _VERSION_LINE.fullmatch(text)
    if match is None:
        raise ValueError("not exactly one '3.<minor>' line")
    return (3, int(match.group(1)))


def _workflow_files() -> list[Path]:
    return sorted(p for p in WORKFLOW_DIR.iterdir() if p.suffix in {".yml", ".yaml"})


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _is_setup_python(uses: object) -> bool:
    # Action owner/repo names are case-insensitive on GitHub.
    if not isinstance(uses, str):
        return False
    return uses.strip().lower().split("@", 1)[0] == SETUP_PYTHON


def _setup_python_steps(name: str, doc: Any) -> list[tuple[str, str, Any]]:
    """(job id, step label, step) for every setup-python step in one workflow."""
    found: list[tuple[str, str, Any]] = []
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    for job_id, job in (jobs or {}).items():
        steps = job.get("steps") if isinstance(job, dict) else None
        for index, step in enumerate(steps or []):
            if isinstance(step, dict) and _is_setup_python(step.get("uses")):
                label = f"step {index} ({step.get('name') or step.get('uses')})"
                found.append((job_id, label, step))
    return found


def _setup_python_violations(name: str, doc: Any) -> list[str]:
    """Every setup-python step that does not read the single version file."""
    problems: list[str] = []
    for job_id, label, step in _setup_python_steps(name, doc):
        where = f"{name}/{job_id}/{label}"
        inputs = step.get("with")
        if not isinstance(inputs, dict):
            problems.append(f"{where}: no 'with' mapping")
            continue
        if HARD_CODED_INPUT in inputs:
            problems.append(f"{where}: hard-coded {HARD_CODED_INPUT}")
        if inputs.get(VERSION_FILE_INPUT) != VERSION_FILE_NAME:
            problems.append(f"{where}: {VERSION_FILE_INPUT} is not {VERSION_FILE_NAME}")
    return problems


def _hard_coded_version_keys(name: str, doc: Any) -> list[str]:
    """Paths of every `python-version` key anywhere in a workflow (matrix too)."""
    hits: list[str] = []
    stack: list[tuple[str, Any]] = [(name, doc)]
    while stack:  # iterative: a hostile deep document cannot blow the stack
        path, node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key == HARD_CODED_INPUT:
                    hits.append(f"{path}/{key}")
                stack.append((f"{path}/{key}", value))
        elif isinstance(node, list):
            stack.extend((f"{path}[{i}]", item) for i, item in enumerate(node))
    return sorted(hits)


def _ruff_floor(target: object) -> tuple[int, int]:
    if not isinstance(target, str):
        raise ValueError("ruff target-version is not a string")
    match = _RUFF_TARGET.fullmatch(target)
    if match is None:
        raise ValueError(f"unrecognised ruff target-version {target!r}")
    return (3, int(match.group(1)))


def _mypy_floor(target: object) -> tuple[int, int]:
    # [tool.mypy] python_version is "3.<minor>" (TOML string; a float such as
    # 3.10 would read as 3.1, so it is refused, not coerced).
    if not isinstance(target, str):
        raise ValueError("mypy python_version is not a string")
    match = _VERSION_LINE.fullmatch(target)
    if match is None:
        raise ValueError(f"unrecognised mypy python_version {target!r}")
    return (3, int(match.group(1)))


def _declared_python_floors() -> list[tuple[str, str]]:
    """(source, constraint) for every Python floor the tracked repo declares.

    Each constraint is a PEP 440 specifier string. Sources: requires-python in
    any pyproject.toml, python_requires in any setup.cfg, ruff's target-version
    (ruff.toml, .ruff.toml, or [tool.ruff] in pyproject.toml) and mypy's
    python_version ([tool.mypy] in pyproject.toml).
    """
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout.decode("utf-8").split("\0")
    floors: list[tuple[str, str]] = []
    for rel in filter(None, tracked):
        path = REPO_ROOT / rel
        base = path.name
        if base == "pyproject.toml":
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            requires = data.get("project", {}).get("requires-python")
            if requires is not None:
                floors.append((f"{rel} requires-python", requires))
            tool = data.get("tool", {})
            ruff = tool.get("ruff", {}).get("target-version")
            if ruff is not None:
                major, minor = _ruff_floor(ruff)
                floors.append((f"{rel} [tool.ruff] target-version", f">={major}.{minor}"))
            mypy = tool.get("mypy", {}).get("python_version")
            if mypy is not None:
                major, minor = _mypy_floor(mypy)
                floors.append((f"{rel} [tool.mypy] python_version", f">={major}.{minor}"))
        elif base == "setup.cfg":
            cfg = configparser.ConfigParser()
            cfg.read_string(path.read_text(encoding="utf-8"))
            requires = cfg.get("options", "python_requires", fallback=None)
            if requires is not None:
                floors.append((f"{rel} python_requires", requires))
        elif base in {"ruff.toml", ".ruff.toml"}:
            ruff = tomllib.loads(path.read_text(encoding="utf-8")).get("target-version")
            if ruff is not None:
                major, minor = _ruff_floor(ruff)
                floors.append((f"{rel} target-version", f">={major}.{minor}"))
    return floors


# --- acceptance: the single pin ----------------------------------------------


def test_python_version_file_pins_the_target() -> None:
    # Red today: the file does not exist.
    assert VERSION_FILE.exists(), f"{VERSION_FILE_NAME} missing at the repo root"
    # F7: a symlink would let the pin silently follow some other file.
    assert VERSION_FILE.is_file() and not VERSION_FILE.is_symlink()
    assert _parse_version_file(VERSION_FILE.read_bytes()) == TARGET_VERSION


def test_python_version_file_is_tracked_and_not_git_ignored() -> None:
    # A git-ignored or untracked pin exists locally and is absent from the CI
    # checkout, where setup-python would then fail (or worse, be skipped).
    ignored = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", "--no-index", VERSION_FILE_NAME],
        capture_output=True,
        timeout=30,
    )
    assert ignored.returncode == 1, "git ignores .python-version (exit 0) or git failed (128)"
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "--error-unmatch", VERSION_FILE_NAME],
        capture_output=True,
        timeout=30,
    )
    assert tracked.returncode == 0, f"{VERSION_FILE_NAME} is not tracked by git"


_VERSION_VECTORS: list[tuple[str, bytes, tuple[int, int] | None]] = [
    ("plain", b"3.14\n", (3, 14)),
    ("no-trailing-newline", b"3.14", (3, 14)),
    ("single-digit-minor", b"3.9\n", (3, 9)),
    ("minor-zero", b"3.0\n", (3, 0)),
    ("empty", b"", None),
    ("only-newline", b"\n", None),
    ("two-lines", b"3.14\n3.13\n", None),
    ("blank-second-line", b"3.14\n\n", None),
    ("crlf", b"3.14\r\n", None),
    ("lone-cr", b"3.14\r", None),
    ("u2028-line-separator", "3.14 ".encode(), None),
    ("u2029-paragraph-separator", "3.14 ".encode(), None),
    ("u0085-next-line", "3.14\u0085".encode(), None),
    ("vertical-tab", b"3.14\x0b", None),
    ("form-feed", b"3.14\x0c", None),
    ("utf8-bom", b"\xef\xbb\xbf3.14\n", None),
    ("zero-width-space", "3.1​4\n".encode(), None),
    ("rtl-override", "‮3.14\n".encode(), None),
    ("fullwidth-digits", "３.１４\n".encode(), None),
    ("arabic-indic-digit", "3.١٤\n".encode(), None),
    ("nul", b"3.14\x00", None),
    ("invalid-utf8", b"\xff\xfe3.14", None),
    ("lone-surrogate-cesu", b"3.14\xed\xa0\x80", None),
    ("patch-version", b"3.14.0\n", None),
    ("free-threaded", b"3.14t\n", None),
    ("leading-space", b" 3.14\n", None),
    ("trailing-space", b"3.14 \n", None),
    ("tab", b"3.14\t\n", None),
    ("comment", b"3.14 # pin\n", None),
    ("leading-zero", b"3.014\n", None),
    ("python2", b"2.7\n", None),
    ("major-only", b"3\n", None),
    ("pypy", b"pypy3.10\n", None),
    ("prefixed", b"python-3.14\n", None),
    ("huge-minor", b"3." + b"1" * 2_000_000, None),
]


@pytest.mark.parametrize(
    "raw,expected",
    [(r, e) for _, r, e in _VERSION_VECTORS],
    ids=[i for i, _, _ in _VERSION_VECTORS],
)
def test_version_file_parser_vectors(raw: bytes, expected: tuple[int, int] | None) -> None:
    if expected is None:
        with pytest.raises(ValueError):
            _parse_version_file(raw)
    else:
        assert _parse_version_file(raw) == expected


def test_version_file_vectors_have_both_outcomes() -> None:
    # Contract: the table must keep an accept row and a reject row, so the
    # parser can be neither "accept all" nor "reject all".
    outcomes = {expected is None for _, _, expected in _VERSION_VECTORS}
    assert outcomes == {True, False}


# --- acceptance: every setup-python step reads the pin ------------------------


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_setup_python_steps_read_the_version_file(path: Path) -> None:
    assert _setup_python_violations(path.name, _load(path)) == []


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_no_workflow_hard_codes_a_python_version(path: Path) -> None:
    # Anywhere, not only setup-python's `with`: a matrix or env key would
    # bring the drift back.
    assert _hard_coded_version_keys(path.name, _load(path)) == []


def _wf(*steps: Any) -> dict[str, Any]:
    return {"jobs": {"j": {"steps": list(steps)}}}


_PINNED = {"uses": "actions/setup-python@v5", "with": {VERSION_FILE_INPUT: VERSION_FILE_NAME}}

_STEP_VECTORS: list[tuple[str, Any, list[str]]] = [
    ("pinned", _wf(_PINNED), []),
    (
        "pinned-sha-ref-with-cache",
        _wf(
            {
                "uses": "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
                "with": {VERSION_FILE_INPUT: VERSION_FILE_NAME, "cache": "pip"},
            }
        ),
        [],
    ),
    ("other-action-ignored", _wf({"uses": "actions/checkout@v4"}), []),
    ("run-step-ignored", _wf({"run": "python -V"}), []),
    ("non-string-uses-ignored", _wf({"uses": ["actions/setup-python@v5"]}), []),
    ("no-jobs", {"on": "push"}, []),
    ("not-a-mapping", None, []),
    (
        "hard-coded",
        _wf({"uses": "actions/setup-python@v5", "with": {HARD_CODED_INPUT: "3.11"}}),
        [
            "w.yml/j/step 0 (actions/setup-python@v5): hard-coded python-version",
            "w.yml/j/step 0 (actions/setup-python@v5): python-version-file is not .python-version",
        ],
    ),
    (
        "both-inputs",
        _wf(
            {
                "name": "Py",
                "uses": "actions/setup-python@v5",
                "with": {HARD_CODED_INPUT: "3.14", VERSION_FILE_INPUT: VERSION_FILE_NAME},
            }
        ),
        ["w.yml/j/step 0 (Py): hard-coded python-version"],
    ),
    (
        "empty-hard-coded-key",
        _wf(
            {
                "uses": "actions/setup-python@v5",
                "with": {HARD_CODED_INPUT: None, VERSION_FILE_INPUT: VERSION_FILE_NAME},
            }
        ),
        ["w.yml/j/step 0 (actions/setup-python@v5): hard-coded python-version"],
    ),
    (
        "dot-slash-path",
        _wf({"uses": "actions/setup-python@v5", "with": {VERSION_FILE_INPUT: "./.python-version"}}),
        ["w.yml/j/step 0 (actions/setup-python@v5): python-version-file is not .python-version"],
    ),
    (
        "other-file",
        _wf({"uses": "actions/setup-python@v5", "with": {VERSION_FILE_INPUT: "src/.python-version"}}),
        ["w.yml/j/step 0 (actions/setup-python@v5): python-version-file is not .python-version"],
    ),
    (
        "no-with",
        _wf({"uses": "actions/setup-python@v5"}),
        ["w.yml/j/step 0 (actions/setup-python@v5): no 'with' mapping"],
    ),
    (
        "with-not-mapping",
        _wf({"uses": "actions/setup-python@v5", "with": "3.11"}),
        ["w.yml/j/step 0 (actions/setup-python@v5): no 'with' mapping"],
    ),
    (
        "mixed-case-uses",
        _wf({"uses": "Actions/Setup-Python@v5", "with": {HARD_CODED_INPUT: "3.11"}}),
        [
            "w.yml/j/step 0 (Actions/Setup-Python@v5): hard-coded python-version",
            "w.yml/j/step 0 (Actions/Setup-Python@v5): python-version-file is not .python-version",
        ],
    ),
    (
        "second-step-second-job",
        {
            "jobs": {
                "a": {"steps": [_PINNED]},
                "b": {
                    "steps": [
                        {"uses": "actions/checkout@v4"},
                        {"uses": "actions/setup-python@v5", "with": {}},
                    ]
                },
            }
        },
        ["w.yml/b/step 1 (actions/setup-python@v5): python-version-file is not .python-version"],
    ),
]


@pytest.mark.parametrize(
    "doc,expected",
    [(d, e) for _, d, e in _STEP_VECTORS],
    ids=[i for i, _, _ in _STEP_VECTORS],
)
def test_setup_python_checker_vectors(doc: Any, expected: list[str]) -> None:
    assert _setup_python_violations("w.yml", doc) == expected


def test_setup_python_vectors_have_both_outcomes() -> None:
    outcomes = {bool(expected) for _, _, expected in _STEP_VECTORS}
    assert outcomes == {True, False}


@pytest.mark.parametrize(
    "doc,expected",
    [
        pytest.param({"jobs": {"j": {"steps": [_PINNED]}}}, [], id="clean"),
        pytest.param(
            {"jobs": {"j": {"strategy": {"matrix": {HARD_CODED_INPUT: ["3.11", "3.14"]}}}}},
            ["w.yml/jobs/j/strategy/matrix/python-version"],
            id="matrix",
        ),
        pytest.param(
            {"jobs": {"j": {"steps": [{"with": {HARD_CODED_INPUT: "3.11"}}]}}},
            ["w.yml/jobs/j/steps[0]/with/python-version"],
            id="step-with",
        ),
        pytest.param({"env": {"PYTHON_VERSION": "3.11"}}, [], id="env-name-differs-not-flagged"),
    ],
)
def test_hard_coded_key_walker_vectors(doc: Any, expected: list[str]) -> None:
    assert _hard_coded_version_keys("w.yml", doc) == expected


def test_hard_coded_key_walker_survives_deep_documents() -> None:
    # A hostile, deeply nested document must not raise RecursionError.
    deep: Any = {HARD_CODED_INPUT: "3.11"}
    for _ in range(50_000):
        deep = {"k": deep}
    assert len(_hard_coded_version_keys("w.yml", deep)) == 1


# --- acceptance: every job that runs python set it up from the pin ----------


def _strip_shell_comment(line: str) -> str:
    """Drop a shell comment: '#' outside quotes, at line start or after blank."""
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _run_invokes_python(script: str) -> bool:
    """True when a `run:` script calls a python interpreter.

    Errs towards True (fail closed): an over-match only asks for a
    setup-python step that does no harm. echo/printf arguments do not count
    unless they hold a command substitution, which does run.
    """
    logical = re.sub(r"\\\r?\n", " ", script)  # join backslash continuations
    for line in logical.splitlines():
        for command in re.split(r"&&|\|\||[;|&(]", _strip_shell_comment(line)):
            words = command.split()
            if not words:
                continue
            if words[0] in _ECHO_WORDS and "$(" not in command and "`" not in command:
                continue
            if _PYTHON_WORD.search(command):
                return True
    return False


def _shell_of(*candidates: object) -> str:
    for candidate in candidates:
        if isinstance(candidate, str):
            return candidate
    return ""


def _default_shell(node: object) -> object:
    defaults = node.get("defaults") if isinstance(node, dict) else None
    run = defaults.get("run") if isinstance(defaults, dict) else None
    return run.get("shell") if isinstance(run, dict) else None


def _step_invokes_python(step: dict[str, Any], default_shell: str) -> bool:
    script = step.get("run")
    if not isinstance(script, str):
        return False  # a `uses:` step (composite or JS action) is not judged here
    shell = _shell_of(step.get("shell"), default_shell)
    # `shell: python` (or `python3 {0}`) runs the whole script under python.
    return bool(_PYTHON_WORD.search(shell)) or _run_invokes_python(script)


def _reads_the_pin(step: dict[str, Any]) -> bool:
    inputs = step.get("with")
    return (
        isinstance(inputs, dict)
        and HARD_CODED_INPUT not in inputs
        and inputs.get(VERSION_FILE_INPUT) == VERSION_FILE_NAME
    )


def _python_run_steps(name: str, doc: Any) -> list[tuple[str, str, bool]]:
    """(job id, step label, pinned) for every run step that invokes python.

    pinned is True only when the most recent setup-python step earlier in the
    same job reads the pin; a later setup-python with a hard-coded version
    replaces the interpreter, so the last one wins.
    """
    found: list[tuple[str, str, bool]] = []
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    for job_id, job in (jobs or {}).items():
        default_shell = _shell_of(_default_shell(job), _default_shell(doc))
        steps = job.get("steps") if isinstance(job, dict) else None
        pinned = False
        for index, step in enumerate(steps or []):
            if not isinstance(step, dict):
                continue
            if _is_setup_python(step.get("uses")):
                pinned = _reads_the_pin(step)
            elif _step_invokes_python(step, default_shell):
                label = f"step {index} ({step.get('name') or 'run'})"
                found.append((job_id, label, pinned))
    return found


def _unpinned_python_runs(name: str, doc: Any) -> list[str]:
    return [
        f"{name}/{job_id}/{label}: runs python with no earlier setup-python "
        f"reading {VERSION_FILE_NAME}"
        for job_id, label, pinned in _python_run_steps(name, doc)
        if not pinned
    ]


def _python_running_jobs() -> set[tuple[str, str]]:
    """(workflow, job) for every job whose run steps invoke python, derived
    from the real workflows rather than restated by hand."""
    found: set[tuple[str, str]] = set()
    for path in _workflow_files():
        found.update((path.name, job_id) for job_id, _, _ in _python_run_steps(path.name, _load(path)))
    return found


def _mentions_python_outside_comments(text: str) -> bool:
    # Coarse, independent oracle for the detector: any python word on a line
    # that is not wholly a YAML/shell comment.
    return any(
        _PYTHON_WORD.search(line)
        for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )


def test_python_running_jobs_are_detected_in_every_workflow_that_runs_python() -> None:
    # "Did nothing" guard for the derived job set: a detector that matches
    # nothing would make the ordering and setup checks below pass vacuously.
    # Every workflow whose text names python outside a comment must yield at
    # least one python-running job.
    jobs = _python_running_jobs()
    files_with_jobs = {name for name, _ in jobs}
    expected_files = {
        p.name for p in _workflow_files() if _mentions_python_outside_comments(p.read_text("utf-8"))
    }
    assert expected_files, "no workflow mentions python; the derived job set is vacuous"
    assert expected_files <= files_with_jobs, sorted(expected_files - files_with_jobs)


def test_every_python_running_job_installs_python_from_the_pin() -> None:
    # Red today: no workflow has a single setup-python step. The required set
    # is derived from the real workflows (every job that runs python).
    required = _python_running_jobs()
    assert required, "derived python-running job set is empty"
    found: set[tuple[str, str]] = set()
    for path in _workflow_files():
        found.update((path.name, job_id) for job_id, _, _ in _setup_python_steps(path.name, _load(path)))
    assert sorted(required - found) == []


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_every_python_run_follows_a_pinned_setup_python(path: Path) -> None:
    # Every Python run must follow a setup-python step that reads the pin;
    # without one a job would run whatever python3 the ubuntu-latest image ships.
    assert _unpinned_python_runs(path.name, _load(path)) == []


def _run(script: str, **extra: Any) -> dict[str, Any]:
    return {"run": script, **extra}


_HARD_CODED_SETUP = {"uses": "actions/setup-python@v5", "with": {HARD_CODED_INPUT: "3.14"}}

# (id, run script, invokes python?) for the run-step detector. The first rows
# are lifted from the ingester's own workflows.
_RUN_VECTORS: list[tuple[str, str, bool]] = [
    ("version-probe", "python3 --version", True),
    ("venv-create", "python3 -m venv .venv\nsource .venv/bin/activate\n", True),
    ("script-run", "python scripts/check_approvals.py --approvals-dir config/approvals", True),
    ("var-assign", "py=python3\n\"$py\" scripts/x.py", True),
    ("quoted-script-arg", 'python3 "$RUNNER_TEMP/check_verdict.py" p9-verdict.json', True),
    ("python-m", "python -m pytest -v", True),
    ("absolute-path", "/usr/bin/python3 x.py", True),
    ("versioned", "python3.14 -V", True),
    ("env-prefix", "PYTHONHASHSEED=0 python3 x.py", True),
    ("after-and", "cd src && python -m pytest", True),
    ("after-semicolon", "true; python3 x.py", True),
    ("after-pipe", "cat f | python3 -m json.tool", True),
    ("subshell", "(python3 x.py)", True),
    ("second-line", "set -euo pipefail\npython3 x.py\n", True),
    ("continuation", "FOO=1 \\\n  python3 x.py", True),
    ("bash-c-single-quoted", "bash -c 'python3 x.py'", True),
    ("echo-command-substitution", 'echo "$(python3 -V)"', True),
    ("echo-backticks", "echo `python3 -V`", True),
    ("hash-inside-quotes-not-comment", 'X="a#b" python3 x.py', True),
    ("if-then", "if true; then python3 x.py; fi", True),
    ("pip-install", "pip install -e '.[dev]'", False),
    ("pytest-bare", "pytest tests/unit -v", False),
    ("ruff", "ruff check .", False),
    ("cp-py-file", 'cp .github/p9/check_verdict.py "$RUNNER_TEMP/check_verdict.py"', False),
    ("pip-python-dash-package", "pip install python-dotenv", False),
    ("apt-python3-dash-package", "sudo apt-get install -y python3-venv", False),
    ("prefix-word", "mypython3 x", False),
    ("suffix-word", "pythonista x", False),
    ("comment-line", "# python3 x.py would be wrong here\ntrue", False),
    ("trailing-comment", "true  # then python3 x.py", False),
    ("echo-string", 'echo "Using python3 from the runner"', False),
    ("printf-string", "printf '%s\\n' python3", False),
    ("empty", "", False),
]


@pytest.mark.parametrize(
    "script,expected",
    [(r, e) for _, r, e in _RUN_VECTORS],
    ids=[i for i, _, _ in _RUN_VECTORS],
)
def test_python_run_detector_vectors(script: str, expected: bool) -> None:
    assert _run_invokes_python(script) is expected


def test_python_run_detector_vectors_have_both_outcomes() -> None:
    assert {expected for _, _, expected in _RUN_VECTORS} == {True, False}


_RUN_PY = _run("python3 x.py", name="Gate")
_UNPINNED = "w.yml/j/step {i} (Gate): runs python with no earlier setup-python reading .python-version"

# (id, workflow, expected problems) for the job-level ordering checker.
_ORDER_VECTORS: list[tuple[str, Any, list[str]]] = [
    ("setup-then-run", _wf(_PINNED, _RUN_PY), []),
    ("checkout-setup-run", _wf({"uses": "actions/checkout@v4"}, _PINNED, _RUN_PY), []),
    ("no-python-no-setup", _wf({"uses": "actions/checkout@v4"}, _run("ruff check .")), []),
    ("comment-only", _wf(_run("# python3 x.py\ntrue")), []),
    ("echo-only", _wf(_run('echo "python3 is next"')), []),
    ("composite-uses-ignored", _wf({"uses": "./.github/actions/py-thing"}), []),
    ("remote-uses-ignored", _wf({"uses": "owner/python-action@v1", "with": {"x": 1}}), []),
    ("non-string-run-ignored", _wf({"run": ["python3", "x.py"]}), []),
    ("non-mapping-step-ignored", _wf("python3 x.py"), []),
    ("reusable-workflow-job", {"jobs": {"j": {"uses": "./.github/workflows/x.yml"}}}, []),
    ("not-a-mapping", None, []),
    ("no-setup", _wf(_RUN_PY), [_UNPINNED.format(i=0)]),
    (
        "version-probe-is-not-a-setup",
        _wf(_run("python3 --version", name="Gate"), _RUN_PY),
        [_UNPINNED.format(i=0), _UNPINNED.format(i=1)],
    ),
    ("run-before-setup", _wf(_RUN_PY, _PINNED), [_UNPINNED.format(i=0)]),
    (
        "setup-in-other-job",
        {"jobs": {"a": {"steps": [_PINNED]}, "j": {"steps": [_RUN_PY]}}},
        [_UNPINNED.format(i=0)],
    ),
    ("hard-coded-version", _wf(_HARD_CODED_SETUP, _RUN_PY), [_UNPINNED.format(i=1)]),
    (
        "both-inputs",
        _wf(
            {
                "uses": "actions/setup-python@v5",
                "with": {HARD_CODED_INPUT: "3.14", VERSION_FILE_INPUT: VERSION_FILE_NAME},
            },
            _RUN_PY,
        ),
        [_UNPINNED.format(i=1)],
    ),
    (
        "setup-without-with",
        _wf({"uses": "actions/setup-python@v5"}, _RUN_PY),
        [_UNPINNED.format(i=1)],
    ),
    (
        "later-hard-coded-overrides",
        _wf(_PINNED, _HARD_CODED_SETUP, _RUN_PY),
        [_UNPINNED.format(i=2)],
    ),
    (
        "each-unpinned-run-listed",
        _wf(_RUN_PY, _PINNED, _RUN_PY, _HARD_CODED_SETUP, _RUN_PY),
        [_UNPINNED.format(i=0), _UNPINNED.format(i=4)],
    ),
    (
        "step-shell-python",
        _wf(_run("print(1)", name="Gate", shell="python")),
        [_UNPINNED.format(i=0)],
    ),
    (
        "job-default-shell-python",
        {
            "jobs": {
                "j": {
                    "defaults": {"run": {"shell": "python3 {0}"}},
                    "steps": [_run("print(1)", name="Gate")],
                }
            }
        },
        [_UNPINNED.format(i=0)],
    ),
    (
        "workflow-default-shell-python",
        {
            "defaults": {"run": {"shell": "python"}},
            "jobs": {"j": {"steps": [_run("print(1)", name="Gate")]}},
        },
        [_UNPINNED.format(i=0)],
    ),
    (
        "workflow-default-bash-pipefail-not-python",
        {
            "defaults": {"run": {"shell": "bash -eo pipefail {0}"}},
            "jobs": {"j": {"steps": [_run("true", name="Gate")]}},
        },
        [],
    ),
    (
        "step-shell-bash-overrides-python-default",
        {
            "defaults": {"run": {"shell": "python"}},
            "jobs": {"j": {"steps": [_run("true", name="Gate", shell="bash")]}},
        },
        [],
    ),
    (
        "unnamed-step-label",
        _wf(_run("python3 x.py")),
        ["w.yml/j/step 0 (run): runs python with no earlier setup-python reading .python-version"],
    ),
]


@pytest.mark.parametrize(
    "doc,expected",
    [(d, e) for _, d, e in _ORDER_VECTORS],
    ids=[i for i, _, _ in _ORDER_VECTORS],
)
def test_python_run_order_checker_vectors(doc: Any, expected: list[str]) -> None:
    assert _unpinned_python_runs("w.yml", doc) == expected


def test_python_run_order_vectors_have_both_outcomes() -> None:
    assert {bool(expected) for _, _, expected in _ORDER_VECTORS} == {True, False}


# --- acceptance: the pin agrees with every declared floor ---------------------


def test_pinned_version_satisfies_every_declared_floor() -> None:
    floors = _declared_python_floors()
    # "Did nothing" guard: today pyproject.toml declares three (requires-python,
    # [tool.ruff] target-version, [tool.mypy] python_version).
    assert floors, "no Python floor declared anywhere; the agreement check is vacuous"
    assert VERSION_FILE.is_file(), f"{VERSION_FILE_NAME} missing at the repo root"
    pinned = _parse_version_file(VERSION_FILE.read_bytes())
    version = f"{pinned[0]}.{pinned[1]}"
    violated = [
        f"{source}: {spec!r} excludes {version}"
        for source, spec in floors
        if not SpecifierSet(spec).contains(version)
    ]
    assert violated == []


def test_every_floor_source_in_pyproject_is_read() -> None:
    # Guards the floor reader against silently dropping a source: each of the
    # three keys pyproject.toml sets today must surface as a floor.
    sources = {source for source, _ in _declared_python_floors()}
    assert {
        "pyproject.toml requires-python",
        "pyproject.toml [tool.ruff] target-version",
        "pyproject.toml [tool.mypy] python_version",
    } <= sources, sorted(sources)


@pytest.mark.parametrize(
    "target,expected",
    [
        pytest.param("py310", (3, 10), id="py310"),
        pytest.param("py314", (3, 14), id="py314"),
        pytest.param("py3", None, id="no-minor"),
        pytest.param("3.10", None, id="dotted"),
        pytest.param("py310\n", None, id="newline"),
        pytest.param("py3１０", None, id="fullwidth"),
        pytest.param(310, None, id="not-a-string"),
    ],
)
def test_ruff_floor_vectors(target: object, expected: tuple[int, int] | None) -> None:
    if expected is None:
        with pytest.raises(ValueError):
            _ruff_floor(target)
    else:
        assert _ruff_floor(target) == expected


@pytest.mark.parametrize(
    "target,expected",
    [
        pytest.param("3.10", (3, 10), id="3.10"),
        pytest.param("3.14", (3, 14), id="3.14"),
        pytest.param(3.10, None, id="toml-float-reads-as-3.1"),
        pytest.param("3", None, id="major-only"),
        pytest.param("3.10\n", None, id="newline"),
        pytest.param("3.1​0", None, id="zero-width"),
        pytest.param("py310", None, id="ruff-spelling"),
        pytest.param(None, None, id="null"),
    ],
)
def test_mypy_floor_vectors(target: object, expected: tuple[int, int] | None) -> None:
    if expected is None:
        with pytest.raises(ValueError):
            _mypy_floor(target)
    else:
        assert _mypy_floor(target) == expected


def test_floor_specifiers_are_strict_pep440() -> None:
    # A malformed requires-python must fail the agreement check, not be
    # skipped: SpecifierSet raises on it.
    with pytest.raises(InvalidSpecifier):
        SpecifierSet(">=3.10; garbage")


# --- acceptance: the 3.14-sensitive test still runs ---------------------------


def test_deeply_nested_gate_case_runs_and_passes_on_this_interpreter() -> None:
    # The pin must not drop this case through a skip or xfail. Runs only that
    # node, in a child pytest from the repo root, with the project's addopts
    # (coverage) cleared so the child's summary line is the last line.
    env = {k: v for k, v in os.environ.items() if not k.startswith("COV_CORE")}
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", DEEPLY_NESTED_NODE,
            "-o", "addopts=", "-p", "no:cacheprovider", "-q", "-rA", "--no-header", "--color=no",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    out = proc.stdout
    assert proc.returncode == 0, out[-2000:]
    assert f"PASSED {DEEPLY_NESTED_NODE}" in out, out[-2000:]
    # Exact outcome counts: warnings are allowed, any skip/xfail/xpass is not.
    summary = out.strip().splitlines()[-1]
    counts = {kind: int(n) for n, kind in re.findall(r"([0-9]+) ([a-z]+)", summary)}
    counts.pop("warning", None)
    counts.pop("warnings", None)
    assert counts == {"passed": 1}, summary
