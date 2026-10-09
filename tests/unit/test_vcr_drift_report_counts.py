"""Compared-pair counting and zero-comparison exit code for vcr_drift_report (issue #92).

A drift canary that compared nothing must never report green.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from vcr_drift_report import (  # noqa: E402
    EXIT_NOTHING_COMPARED,
    generate_report,
    generate_report_with_count,
    main,
)

_CASSETTE = (
    "interactions:\n  - request:\n      uri: http://x.com\n      method: GET\n"
    "    response:\n      status:\n        code: 200\n      body:\n        string: {body}\n"
)


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    b = tmp_path / "baseline"
    c = tmp_path / "current"
    b.mkdir()
    c.mkdir()
    return b, c


def _run(b: Path, c: Path, tmp_path: Path) -> tuple[int, str]:
    out = tmp_path / "report.md"
    code = main(["--baseline", str(b), "--current", str(c), "--output", str(out)])
    return code, out.read_text(encoding="utf-8")


def test_exit_code_is_distinct_nonzero() -> None:
    assert EXIT_NOTHING_COMPARED not in (0, 1)


def test_both_dirs_empty_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    b, c = _dirs(tmp_path)
    code, report = _run(b, c, tmp_path)
    assert code == EXIT_NOTHING_COMPARED
    assert "Cassette pairs compared: 0" in report
    assert "Compared 0 cassettes" in capsys.readouterr().err


def test_dirs_with_only_non_yaml_files_exit_nonzero(tmp_path: Path) -> None:
    b, c = _dirs(tmp_path)
    (b / "README.txt").write_text("not a cassette")
    (c / "README.txt").write_text("not a cassette")
    code, _ = _run(b, c, tmp_path)
    assert code == EXIT_NOTHING_COMPARED


def test_no_overlapping_files_exits_nonzero(tmp_path: Path) -> None:
    # Mirrors the B1 bug shape: baseline tree nested one level off from current.
    b, c = _dirs(tmp_path)
    (b / "cassettes" / "eurlex").mkdir(parents=True)
    (b / "cassettes" / "eurlex" / "a.yaml").write_text(_CASSETTE.format(body="ok"))
    (c / "eurlex").mkdir()
    (c / "eurlex" / "a.yaml").write_text(_CASSETTE.format(body="ok"))
    code, report = _run(b, c, tmp_path)
    assert code == EXIT_NOTHING_COMPARED
    assert "Cassette pairs compared: 0" in report


def test_only_parse_errors_counts_zero_compared(tmp_path: Path) -> None:
    b, c = _dirs(tmp_path)
    (b / "bad.yaml").write_text(": : invalid\n")
    (c / "bad.yaml").write_text(": : invalid\n")
    _report, any_drift, compared = generate_report_with_count(b, c)
    assert any_drift is True
    assert compared == 0
    code, _ = _run(b, c, tmp_path)
    assert code != 0


def test_identical_pairs_exit_zero_and_report_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    b, c = _dirs(tmp_path)
    for name in ("a.yaml", "b.yaml"):
        (b / name).write_text(_CASSETTE.format(body="ok"))
        (c / name).write_text(_CASSETTE.format(body="ok"))
    code, report = _run(b, c, tmp_path)
    assert code == 0
    assert "Cassette pairs compared: 2" in report
    assert "No drift detected" in report
    assert "2 cassette pair(s) compared" in capsys.readouterr().out


def test_drift_exits_one_and_reports_count(tmp_path: Path) -> None:
    b, c = _dirs(tmp_path)
    (b / "a.yaml").write_text(_CASSETTE.format(body="hello"))
    (c / "a.yaml").write_text(_CASSETTE.format(body="world"))
    (b / "same.yaml").write_text(_CASSETTE.format(body="ok"))
    (c / "same.yaml").write_text(_CASSETTE.format(body="ok"))
    code, report = _run(b, c, tmp_path)
    assert code == 1
    assert "Cassette pairs compared: 2" in report


def test_nested_tree_counts_pairs(tmp_path: Path) -> None:
    b, c = _dirs(tmp_path)
    for root in (b, c):
        (root / "eurlex").mkdir()
        (root / "eurlex" / "gdpr.yaml").write_text(_CASSETTE.format(body="ok"))
    _report, any_drift, compared = generate_report_with_count(b, c)
    assert (any_drift, compared) == (False, 1)


def test_generate_report_wrapper_keeps_two_tuple(tmp_path: Path) -> None:
    b, c = _dirs(tmp_path)
    result = generate_report(b, c)
    assert len(result) == 2
    assert result[1] is False
