"""CI runs on GitHub-hosted ubuntu-latest runners, not the laptop runner.

Ingester #19, ADR-016 (GitHub-hosted runners; amends HR4 and PRINCIPLES C4).
Owner ruling 2026-10-10: "goal is to get onto github hosted runners". The
self-hosted option could not work: actions/setup-python hardcodes the macOS
hosted tool-cache path, so the pin in ``.python-version`` never resolved on
the laptop runner.

The contract below:

1. every job in the six workflows runs on exactly the string ``ubuntu-latest``
   (not a list, not a mapping, not an expression, not a look-alike);
2. no file under ``.github/`` names ``self-hosted`` or the old
   ``legal-corpus-ingester`` runner label in a ``runs-on`` context, and the
   actionlint config declares no custom self-hosted runner labels;
3. the five jobs that moved still install Python from ``.python-version``
   (regression guard, green before and after the move);
4. ci.yml keeps a real linux_amd64 actionlint checksum pin and selects it on
   linux x86_64, the platform CI now runs on (the install step itself is run
   behaviourally in test_workflow_validation.py).

Static checks over YAML by necessity: ``runs-on`` is resolved by GitHub, not
by anything that runs locally. Each checker is pinned by a vectors table with
a contract test that the table keeps both outcomes, so a weakened checker goes
red here instead of silently green.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
GITHUB_DIR = REPO_ROOT / ".github"
WORKFLOW_DIR = GITHUB_DIR / "workflows"
ACTIONLINT_CONFIG = GITHUB_DIR / "actionlint.yaml"
CI_WORKFLOW = WORKFLOW_DIR / "ci.yml"

# Acceptance values from the card (#19, ADR-016). Deliberately not read back
# from the workflows under test: a test that reads its answer from the file it
# checks cannot fail.
HOSTED_LABEL = "ubuntu-latest"
WORKFLOWS_ON_HOSTED = (
    "ci.yml",
    "health.yml",
    "refresh.yml",
    "vcr-drift.yml",
    "approval-expiry.yml",
    "p9-review.yml",
)
# (workflow, job id) for the five jobs that ran on the laptop runner.
MOVED_JOBS = (
    ("ci.yml", "test"),
    ("health.yml", "health"),
    ("refresh.yml", "refresh"),
    ("vcr-drift.yml", "drift"),
    ("approval-expiry.yml", "approval-expiry"),
)
# Runner labels that must not appear in any runs-on context. GitHub matches
# runner labels case-insensitively, so the scan is case-insensitive too.
FORBIDDEN_LABELS = ("self-hosted", "legal-corpus-ingester")

SETUP_PYTHON = "actions/setup-python"
VERSION_FILE_INPUT = "python-version-file"
VERSION_FILE_NAME = ".python-version"
HARD_CODED_INPUT = "python-version"

LINUX_PIN_VAR = "ACTIONLINT_SHA256_LINUX_AMD64"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}", re.ASCII)
_RUNS_ON_KEY = re.compile(r"^(?P<indent>[ \t]*)(?:-[ \t]+)?[\"']?runs-on[\"']?[ \t]*:")


# --- checkers ----------------------------------------------------------------


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _runs_on_violations(name: str, doc: Any) -> list[str]:
    """Every job whose runs-on is not exactly the string HOSTED_LABEL.

    A workflow with no jobs is itself a violation: "checked nothing" must not
    read as "all jobs hosted" (DEV-FUNDAMENTALS F5).
    """
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict) or not jobs:
        return [f"{name}: no jobs mapping"]
    problems: list[str] = []
    for job_id, job in jobs.items():
        where = f"{name}/{job_id}"
        if not isinstance(job, dict) or "runs-on" not in job:
            problems.append(f"{where}: no runs-on")
            continue
        value = job["runs-on"]
        # Exact str type and exact value: no list, mapping, expression,
        # surrounding whitespace, case variant or Unicode look-alike.
        if type(value) is not str or value != HOSTED_LABEL:
            problems.append(f"{where}: runs-on is {value!r}, not {HOSTED_LABEL!r}")
    return problems


def _normalise(text: str) -> str:
    """NFKC, drop format (Cf) characters, casefold: what a label match sees."""
    folded = unicodedata.normalize("NFKC", text)
    folded = "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")
    return folded.casefold()


def _runs_on_label_hits(name: str, text: str) -> list[str]:
    """Lines in a runs-on context that name a forbidden runner label.

    A runs-on context is the `runs-on:` line plus every following line that is
    blank or indented deeper than the key (block lists and flow lists split
    over lines). Works on any text file, so markdown examples count too.
    """
    hits: list[str] = []
    lines = text.splitlines()  # splits on every Unicode line break, not just \n
    index = 0
    while index < len(lines):
        match = _RUNS_ON_KEY.match(lines[index])
        if match is None:
            index += 1
            continue
        key_indent = len(match.group("indent").expandtabs())
        block = [(index, lines[index])]
        index += 1
        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            if stripped and len(line) - len(line.lstrip()) <= key_indent:
                break
            block.append((index, line))
            index += 1
        for number, line in block:
            normal = _normalise(line)
            for label in FORBIDDEN_LABELS:
                if label in normal:
                    hits.append(f"{name}:{number + 1}: runs-on names {label!r}")
    return hits


def _custom_runner_labels(doc: Any) -> list[str]:
    """Custom labels the actionlint config declares (empty means none)."""
    if doc is None:
        return []
    if not isinstance(doc, dict):
        raise ValueError("actionlint config is not a mapping")
    runner = doc.get("self-hosted-runner")
    if runner is None:
        return []
    if not isinstance(runner, dict):
        raise ValueError("self-hosted-runner is not a mapping")
    labels = runner.get("labels")
    if labels is None:
        return []
    if not isinstance(labels, list):
        raise ValueError("self-hosted-runner.labels is not a list")
    return [str(label) for label in labels]


def _setup_python_problems(doc: Any, job_id: str) -> list[str]:
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    job = jobs.get(job_id) if isinstance(jobs, dict) else None
    if not isinstance(job, dict):
        return [f"{job_id}: job missing"]
    steps = [
        s
        for s in job.get("steps") or []
        if isinstance(s, dict)
        and isinstance(s.get("uses"), str)
        and s["uses"].strip().lower().split("@", 1)[0] == SETUP_PYTHON
    ]
    if len(steps) != 1:
        return [f"{job_id}: {len(steps)} setup-python steps, want 1"]
    inputs = steps[0].get("with")
    if not isinstance(inputs, dict):
        return [f"{job_id}: setup-python has no 'with' mapping"]
    problems = []
    if HARD_CODED_INPUT in inputs:
        problems.append(f"{job_id}: hard-coded {HARD_CODED_INPUT}")
    if inputs.get(VERSION_FILE_INPUT) != VERSION_FILE_NAME:
        problems.append(f"{job_id}: {VERSION_FILE_INPUT} is not {VERSION_FILE_NAME}")
    return problems


def _github_text_files() -> list[Path]:
    files = [p for p in sorted(GITHUB_DIR.rglob("*")) if p.is_file() and not p.is_symlink()]
    return [p for p in files if p.suffix in {".yml", ".yaml", ".md", ".py", ".json", ".sh"}]


def _install_actionlint_env() -> dict[str, Any]:
    doc = _load(CI_WORKFLOW)
    for step in doc["jobs"]["test"]["steps"]:
        if isinstance(step, dict) and step.get("name") == "Install actionlint":
            env = step.get("env")
            assert isinstance(env, dict), "Install actionlint step has no env mapping"
            return {"env": env, "run": step.get("run", "")}
    raise AssertionError("ci.yml/test has no 'Install actionlint' step")


# --- acceptance 1: every job runs on ubuntu-latest ----------------------------


@pytest.mark.parametrize("name", WORKFLOWS_ON_HOSTED)
def test_every_job_runs_on_ubuntu_latest(name: str) -> None:
    path = WORKFLOW_DIR / name
    assert path.is_file(), f"{name} is missing; a missing workflow is not 'hosted'"
    assert _runs_on_violations(name, _load(path)) == []


def test_every_workflow_on_disk_is_in_the_hosted_set() -> None:
    # A new workflow must join the contract, not slip past it.
    on_disk = sorted(p.name for p in WORKFLOW_DIR.iterdir() if p.suffix in {".yml", ".yaml"})
    assert on_disk == sorted(WORKFLOWS_ON_HOSTED)


_RUNS_ON_VECTORS: list[tuple[str, Any, list[str]]] = [
    ("hosted", {"jobs": {"a": {"runs-on": "ubuntu-latest"}}}, []),
    (
        "two-hosted",
        {"jobs": {"a": {"runs-on": "ubuntu-latest"}, "b": {"runs-on": "ubuntu-latest"}}},
        [],
    ),
    (
        "self-hosted-list",
        {"jobs": {"a": {"runs-on": ["self-hosted", "legal-corpus-ingester"]}}},
        ["w.yml/a: runs-on is ['self-hosted', 'legal-corpus-ingester'], not 'ubuntu-latest'"],
    ),
    (
        "hosted-in-list",
        {"jobs": {"a": {"runs-on": ["ubuntu-latest"]}}},
        ["w.yml/a: runs-on is ['ubuntu-latest'], not 'ubuntu-latest'"],
    ),
    (
        "group-mapping",
        {"jobs": {"a": {"runs-on": {"group": "g", "labels": "ubuntu-latest"}}}},
        ["w.yml/a: runs-on is {'group': 'g', 'labels': 'ubuntu-latest'}, not 'ubuntu-latest'"],
    ),
    (
        "expression",
        {"jobs": {"a": {"runs-on": "${{ matrix.os }}"}}},
        ["w.yml/a: runs-on is '${{ matrix.os }}', not 'ubuntu-latest'"],
    ),
    (
        "other-image",
        {"jobs": {"a": {"runs-on": "macos-latest"}}},
        ["w.yml/a: runs-on is 'macos-latest', not 'ubuntu-latest'"],
    ),
    (
        "pinned-image",
        {"jobs": {"a": {"runs-on": "ubuntu-24.04"}}},
        ["w.yml/a: runs-on is 'ubuntu-24.04', not 'ubuntu-latest'"],
    ),
    (
        "case-variant",
        {"jobs": {"a": {"runs-on": "Ubuntu-Latest"}}},
        ["w.yml/a: runs-on is 'Ubuntu-Latest', not 'ubuntu-latest'"],
    ),
    (
        "trailing-space",
        {"jobs": {"a": {"runs-on": "ubuntu-latest "}}},
        ["w.yml/a: runs-on is 'ubuntu-latest ', not 'ubuntu-latest'"],
    ),
    (
        "trailing-newline",
        {"jobs": {"a": {"runs-on": "ubuntu-latest\n"}}},
        ["w.yml/a: runs-on is 'ubuntu-latest\\n', not 'ubuntu-latest'"],
    ),
    (
        "hyphen-lookalike-u2010",
        {"jobs": {"a": {"runs-on": "ubuntu‐latest"}}},
        ["w.yml/a: runs-on is 'ubuntu‐latest', not 'ubuntu-latest'"],
    ),
    (
        "zero-width-cf",
        {"jobs": {"a": {"runs-on": "ubuntu-​latest"}}},
        ["w.yml/a: runs-on is 'ubuntu-\\u200blatest', not 'ubuntu-latest'"],
    ),
    (
        "nul",
        {"jobs": {"a": {"runs-on": "ubuntu-latest\x00"}}},
        ["w.yml/a: runs-on is 'ubuntu-latest\\x00', not 'ubuntu-latest'"],
    ),
    (
        "missing-runs-on",
        {"jobs": {"a": {"steps": []}}},
        ["w.yml/a: no runs-on"],
    ),
    (
        "reusable-workflow-job",
        {"jobs": {"a": {"uses": "o/r/.github/workflows/x.yml@v1"}}},
        ["w.yml/a: no runs-on"],
    ),
    (
        "one-bad-of-two",
        {"jobs": {"a": {"runs-on": "ubuntu-latest"}, "b": {"runs-on": "self-hosted"}}},
        ["w.yml/b: runs-on is 'self-hosted', not 'ubuntu-latest'"],
    ),
    ("no-jobs", {"on": "push"}, ["w.yml: no jobs mapping"]),
    ("empty-jobs", {"jobs": {}}, ["w.yml: no jobs mapping"]),
    ("empty-document", None, ["w.yml: no jobs mapping"]),
    ("not-a-mapping", ["jobs"], ["w.yml: no jobs mapping"]),
]


@pytest.mark.parametrize(
    "doc,expected",
    [(d, e) for _, d, e in _RUNS_ON_VECTORS],
    ids=[i for i, _, _ in _RUNS_ON_VECTORS],
)
def test_runs_on_checker_vectors(doc: Any, expected: list[str]) -> None:
    assert _runs_on_violations("w.yml", doc) == expected


def test_runs_on_vectors_have_both_outcomes() -> None:
    assert {bool(e) for _, _, e in _RUNS_ON_VECTORS} == {True, False}


# --- acceptance 2: no self-hosted label anywhere under .github/ ---------------


def test_github_dir_scan_covers_the_known_files() -> None:
    # Did-nothing guard: the scan must actually see the workflows, the
    # actionlint config and the P9 prompts.
    names = {p.relative_to(GITHUB_DIR).as_posix() for p in _github_text_files()}
    expected = {f"workflows/{n}" for n in WORKFLOWS_ON_HOSTED} | {
        "actionlint.yaml",
        "p9/security-engineer.md",
        "p9/grumpy-developer.md",
    }
    assert expected <= names


def test_no_runs_on_context_under_github_names_a_self_hosted_label() -> None:
    hits: list[str] = []
    for path in _github_text_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        hits.extend(_runs_on_label_hits(rel, path.read_text(encoding="utf-8")))
    assert hits == []


def test_actionlint_config_declares_no_custom_runner_labels() -> None:
    # Absent file is acceptable to this contract (test_workflow_validation.py
    # owns whether the file must exist); present means no custom labels.
    doc = _load(ACTIONLINT_CONFIG) if ACTIONLINT_CONFIG.exists() else None
    assert _custom_runner_labels(doc) == []


_LABEL_SCAN_VECTORS: list[tuple[str, str, list[str]]] = [
    ("hosted", "    runs-on: ubuntu-latest\n", []),
    ("prose-mention-is-not-runs-on", "# not the self-hosted runner\nx: 1\n", []),
    ("key-name-only", "self-hosted-runner:\n  labels: []\n", []),
    (
        "flow-list",
        "    runs-on: [self-hosted, legal-corpus-ingester]\n",
        ["f:1: runs-on names 'self-hosted'", "f:1: runs-on names 'legal-corpus-ingester'"],
    ),
    (
        "block-list",
        "    runs-on:\n      - self-hosted\n      - legal-corpus-ingester\n    steps: []\n",
        ["f:2: runs-on names 'self-hosted'", "f:3: runs-on names 'legal-corpus-ingester'"],
    ),
    (
        "block-list-ends-at-sibling-key",
        "    runs-on:\n      - ubuntu-latest\n    env:\n      X: self-hosted\n",
        [],
    ),
    (
        "flow-list-over-lines",
        "    runs-on: [\n      self-hosted,\n    ]\n",
        ["f:2: runs-on names 'self-hosted'"],
    ),
    (
        "group-mapping-labels",
        "    runs-on:\n      group: g\n      labels: [legal-corpus-ingester]\n",
        ["f:3: runs-on names 'legal-corpus-ingester'"],
    ),
    ("bare-label-string", "    runs-on: self-hosted\n", ["f:1: runs-on names 'self-hosted'"]),
    ("case-variant", "    runs-on: [Self-Hosted]\n", ["f:1: runs-on names 'self-hosted'"]),
    ("quoted-key", '    "runs-on": [self-hosted]\n', ["f:1: runs-on names 'self-hosted'"]),
    ("list-item-key", "  - runs-on: self-hosted\n", ["f:1: runs-on names 'self-hosted'"]),
    (
        "zero-width-inside-label",
        "    runs-on: [self-​hosted]\n",
        ["f:1: runs-on names 'self-hosted'"],
    ),
    (
        "fullwidth-lookalike-nfkc",
        "    runs-on: [ｓelf-hosted]\n",
        ["f:1: runs-on names 'self-hosted'"],
    ),
    (
        "markdown-example",
        "Example:\n\n```yaml\nruns-on: [self-hosted, legal-corpus-ingester]\n```\n",
        ["f:4: runs-on names 'self-hosted'", "f:4: runs-on names 'legal-corpus-ingester'"],
    ),
    (
        "duplicate-key-first-wins-in-text",
        "    runs-on: [self-hosted]\n    runs-on: ubuntu-latest\n",
        ["f:1: runs-on names 'self-hosted'"],
    ),
    (
        "unicode-line-separator-split",
        "    x: 1     runs-on: self-hosted\n",
        ["f:2: runs-on names 'self-hosted'"],
    ),
    ("empty-file", "", []),
]


@pytest.mark.parametrize(
    "text,expected",
    [(t, e) for _, t, e in _LABEL_SCAN_VECTORS],
    ids=[i for i, _, _ in _LABEL_SCAN_VECTORS],
)
def test_runs_on_label_scan_vectors(text: str, expected: list[str]) -> None:
    assert _runs_on_label_hits("f", text) == expected


def test_runs_on_label_scan_vectors_have_both_outcomes() -> None:
    assert {bool(e) for _, _, e in _LABEL_SCAN_VECTORS} == {True, False}


_ACTIONLINT_VECTORS: list[tuple[str, Any, list[str] | None]] = [
    ("empty-file", None, []),
    ("no-runner-section", {"config-variables": None}, []),
    ("runner-section-null", {"self-hosted-runner": None}, []),
    ("labels-absent", {"self-hosted-runner": {}}, []),
    ("labels-null", {"self-hosted-runner": {"labels": None}}, []),
    ("labels-empty", {"self-hosted-runner": {"labels": []}}, []),
    (
        "old-label",
        {"self-hosted-runner": {"labels": ["legal-corpus-ingester"]}},
        ["legal-corpus-ingester"],
    ),
    ("labels-string", {"self-hosted-runner": {"labels": "x"}}, None),
    ("runner-section-list", {"self-hosted-runner": ["x"]}, None),
    ("not-a-mapping", ["self-hosted-runner"], None),
]


@pytest.mark.parametrize(
    "doc,expected",
    [(d, e) for _, d, e in _ACTIONLINT_VECTORS],
    ids=[i for i, _, _ in _ACTIONLINT_VECTORS],
)
def test_actionlint_label_reader_vectors(doc: Any, expected: list[str] | None) -> None:
    # Malformed config fails closed (raises) rather than reading as "no labels".
    if expected is None:
        with pytest.raises(ValueError):
            _custom_runner_labels(doc)
    else:
        assert _custom_runner_labels(doc) == expected


def test_actionlint_vectors_have_all_outcomes() -> None:
    kinds = {
        "raise" if e is None else ("labels" if e else "none") for _, _, e in _ACTIONLINT_VECTORS
    }
    assert kinds == {"raise", "labels", "none"}


# --- acceptance 3: the moved jobs still install Python from the pin ----------


@pytest.mark.parametrize("name,job_id", MOVED_JOBS, ids=[f"{n}:{j}" for n, j in MOVED_JOBS])
def test_moved_job_sets_up_python_from_the_version_file(name: str, job_id: str) -> None:
    assert _setup_python_problems(_load(WORKFLOW_DIR / name), job_id) == []


_SETUP_VECTORS: list[tuple[str, Any, list[str]]] = [
    (
        "pinned",
        {
            "jobs": {
                "j": {
                    "steps": [
                        {
                            "uses": "actions/setup-python@v5",
                            "with": {VERSION_FILE_INPUT: VERSION_FILE_NAME},
                        }
                    ]
                }
            }
        },
        [],
    ),
    ("job-missing", {"jobs": {}}, ["j: job missing"]),
    (
        "no-setup-step",
        {"jobs": {"j": {"steps": [{"run": "python3 -V"}]}}},
        ["j: 0 setup-python steps, want 1"],
    ),
    (
        "hard-coded",
        {
            "jobs": {
                "j": {
                    "steps": [
                        {"uses": "actions/setup-python@v5", "with": {HARD_CODED_INPUT: "3.14"}}
                    ]
                }
            }
        },
        ["j: hard-coded python-version", "j: python-version-file is not .python-version"],
    ),
    (
        "no-with",
        {"jobs": {"j": {"steps": [{"uses": "actions/setup-python@v5"}]}}},
        ["j: setup-python has no 'with' mapping"],
    ),
]


@pytest.mark.parametrize(
    "doc,expected", [(d, e) for _, d, e in _SETUP_VECTORS], ids=[i for i, _, _ in _SETUP_VECTORS]
)
def test_setup_python_checker_vectors(doc: Any, expected: list[str]) -> None:
    assert _setup_python_problems(doc, "j") == expected


def test_setup_python_vectors_have_both_outcomes() -> None:
    assert {bool(e) for _, _, e in _SETUP_VECTORS} == {True, False}


# --- acceptance 4: actionlint pin for the platform CI now runs on -------------


def test_ci_keeps_a_sha256_pin_for_linux_amd64_actionlint() -> None:
    env = _install_actionlint_env()["env"]
    pin = env.get(LINUX_PIN_VAR)
    assert isinstance(pin, str), f"{LINUX_PIN_VAR} missing from the Install actionlint step"
    assert _SHA256_HEX.fullmatch(pin), f"{LINUX_PIN_VAR} is not 64 lowercase hex"


def test_ci_selects_the_linux_pin_on_linux_x86_64() -> None:
    # Pins exact identifiers: the uname pair hosted ubuntu-latest reports, the
    # release asset platform, and the env var holding its checksum.
    run = _install_actionlint_env()["run"]
    arm = re.search(r"^\s*linux_x86_64\)\s*(?P<body>[^;\n]*;[^;\n]*);;", run, re.MULTILINE)
    assert arm is not None, "no linux_x86_64 case arm in the Install actionlint step"
    body = arm.group("body")
    assert "platform=linux_amd64" in body
    assert f'want="${LINUX_PIN_VAR}"' in body
