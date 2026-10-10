# Red log: CI on GitHub-hosted ubuntu-latest runners (ingester #19, ADR-016)

Date: 2026-10-10. Base: `origin/chore/python-version-pin` at 66b56c3. Branch: `pr69/hosted-workflows`.
Module: `tests/unit/test_hosted_runner.py` (75 cases, static YAML/text checks, no network).

## Command

```
pytest tests/unit/test_hosted_runner.py -q --no-cov -p no:cacheprovider -rfE
```

## Result today

```
FAILED tests/unit/test_hosted_runner.py::test_every_job_runs_on_ubuntu_latest[ci.yml]
FAILED tests/unit/test_hosted_runner.py::test_every_job_runs_on_ubuntu_latest[health.yml]
FAILED tests/unit/test_hosted_runner.py::test_every_job_runs_on_ubuntu_latest[refresh.yml]
FAILED tests/unit/test_hosted_runner.py::test_every_job_runs_on_ubuntu_latest[vcr-drift.yml]
FAILED tests/unit/test_hosted_runner.py::test_every_job_runs_on_ubuntu_latest[approval-expiry.yml]
FAILED tests/unit/test_hosted_runner.py::test_no_runs_on_context_under_github_names_a_self_hosted_label
FAILED tests/unit/test_hosted_runner.py::test_actionlint_config_declares_no_custom_runner_labels
========================= 7 failed, 68 passed in 0.17s =========================
```

| Brief item | Test | Today | Reason |
|---|---|---|---|
| 1 runs-on exactly "ubuntu-latest" | `test_every_job_runs_on_ubuntu_latest[ci,health,refresh,vcr-drift,approval-expiry]` | RED | each job has `runs-on: ['self-hosted', 'legal-corpus-ingester']` |
| 1 | `test_every_job_runs_on_ubuntu_latest[p9-review.yml]` | green | both P9 jobs already on ubuntu-latest |
| 1 | `test_every_workflow_on_disk_is_in_the_hosted_set` | green | the six workflows are exactly the files on disk; a new one goes red |
| 2 no self-hosted label in a runs-on context under .github/ | `test_no_runs_on_context_under_github_names_a_self_hosted_label` | RED | 10 hits: the five workflows' runs-on lines, each naming both labels |
| 2 actionlint declares no custom labels | `test_actionlint_config_declares_no_custom_runner_labels` | RED | `self-hosted-runner.labels == ['legal-corpus-ingester']` |
| 2 | `test_github_dir_scan_covers_the_known_files` | green | did-nothing guard: scan sees workflows, actionlint.yaml, p9/*.md |
| 3 setup-python reads .python-version | `test_moved_job_sets_up_python_from_the_version_file[x5]` | green | regression guard, by design |
| 4 linux_amd64 actionlint pin | `test_ci_keeps_a_sha256_pin_for_linux_amd64_actionlint`, `test_ci_selects_the_linux_pin_on_linux_x86_64` | green | pin present and 64 hex; the linux_x86_64 arm selects it |
| checker vectors | `test_runs_on_checker_vectors` (21), `test_runs_on_label_scan_vectors` (18), `test_actionlint_label_reader_vectors` (10), `test_setup_python_checker_vectors` (5), plus contract tests that each table has both outcomes | green | these test the checkers, not the repo |

Hostile vectors: list, mapping, `${{ }}` expression, other or pinned image, case variant, trailing space or newline, U+2010 look-alike, U+200B (Cf), NUL, missing runs-on, reusable-workflow job, no or empty jobs, a non-mapping doc, block and flow lists over several lines, a quoted key, a list-item key, fullwidth NFKC look-alike, a markdown example, a duplicate key, a U+2028 line split, an empty file, and malformed actionlint config (it fails closed by raising).

## Full unit suite (pytest tests/unit, repo coverage flags)

7 failed (exactly the red cases above), 1192 passed, 2 skipped, 1 xfailed. The 2 skips are pre-existing: `test_workflow_validation.py:106`, actionlint is not on PATH locally. Under CI that case fails rather than skips (F9). ruff check, ruff format and mypy --strict are clean on the new module.

## Can it pass, and does it bite? (scratch copy, never committed)

The minimal fix sets runs-on to `ubuntu-latest` in the five workflows and leaves actionlint.yaml with comments only. With that fix, 75 of 75 pass. Each mutant below was applied on top of the fix:

```
== today (unfixed)
========================= 7 failed, 68 passed in 0.20s =========================
== minimal fix
============================== 75 passed in 0.13s ==============================
== mutant: health list form
========================= 1 failed, 74 passed in 0.14s =========================
== mutant: p9 pinned image (BSD sed no-op; rerun with perl below)
============================== 75 passed in 0.14s ==============================
== mutant: vcr expression
========================= 1 failed, 74 passed in 0.14s =========================
== mutant: actionlint label back
========================= 1 failed, 74 passed in 0.15s =========================
== mutant: actionlint labels malformed
========================= 1 failed, 74 passed in 0.17s =========================
== mutant: md runs-on example
========================= 1 failed, 74 passed in 0.15s =========================
== mutant: refresh hard-coded version
========================= 1 failed, 74 passed in 0.15s =========================
== mutant: linux pin truncated
========================= 1 failed, 74 passed in 0.15s =========================
== mutant: linux arm uses darwin pin
========================= 1 failed, 74 passed in 0.15s =========================
== mutant: unlisted workflow
========================= 1 failed, 74 passed in 0.15s =========================
== mutant: health.yml deleted
========================= 4 failed, 71 passed in 0.18s =========================
== restored fix
============================== 75 passed in 0.12s ==============================
== mutant: p9 pinned image (perl): 1 failed (test_every_job_runs_on_ubuntu_latest[p9-review.yml])
```

Every mutant turns red, and restoring the fix turns it green again.

## Notes for the coder and orchestrator

- Some prose under .github/ still mentions "self-hosted" outside a runs-on context and is not asserted here: the p9-review.yml:4 comment, the vcr-drift.yml:42 comment (checklist A) and p9/security-engineer.md:57 (checklist B). The `self-hosted-runner:` key in actionlint.yaml is also allowed, as long as `labels` is absent or empty.
- After the move, the darwin_arm64 pin and its case arm are dead in CI, but they are kept on purpose (checklist A: test_workflow_validation parametrizes both platforms).
- ADR-016 does not exist on this branch yet. It belongs to the docs sub-branch (checklist B).
