"""Unit tests for scripts/_yaml_dir.py and the error paths it feeds (terms-analysis#173).

Covers what the acceptance tests do not: the sanitiser's escape and cap rules,
broken symlinks, a *.yaml directory next to a real config, unparseable or
unreadable source YAML, and the approvals message naming unverified sources.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

# scripts/ is not a package; inject it into the path.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

from _yaml_dir import YamlDirError, display_path, scan_yaml_dir
from check_approvals import main as approvals_main
from health_check import EXIT_CONFIG_MISSING
from health_check import main as health_main

_FIXTURE_SOURCE = Path(__file__).resolve().parents[1] / "fixtures" / "sources" / "eurlex.yaml"


# ---------------------------------------------------------------------------
# display_path: one renderer for untrusted paths in logs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain/dir", "plain/dir"),
        ("a\nb", "a\\x0ab"),
        ("a\rb", "a\\x0db"),
        ("esc\x1b[2J", "esc\\x1b[2J"),
        ("del\x7f", "del\\x7f"),
        ("bidi\u202eRLO", "bidi\\u202eRLO"),
        ("zw\u200bsp", "zw\\u200bsp"),
        ("line\u2028sep", "line\\u2028sep"),
        ("para\u2029sep", "para\\u2029sep"),
        ("bad\udcffbyte", "bad\\udcffbyte"),
        ("caf\u00e9 r\u00e9sum\u00e9", "caf\u00e9 r\u00e9sum\u00e9"),
    ],
    ids=[
        "plain", "lf", "cr", "ansi-escape", "del", "bidi-override", "zero-width",
        "line-separator", "paragraph-separator", "lone-surrogate", "printable-unicode-kept",
    ],
)
def test_display_path_escapes_unsafe_characters(raw: str, expected: str) -> None:
    assert display_path(raw) == expected


def test_display_path_output_is_one_encodable_line() -> None:
    shown = display_path("x\udcff\n\u2028y")
    assert len(shown.splitlines()) == 1
    shown.encode("utf-8")  # a lone surrogate would raise here if left raw


def test_display_path_caps_length_at_boundary() -> None:
    assert display_path("a" * 200) == "a" * 200
    assert display_path("a" * 201) == "a" * 200 + "...(truncated)"


# ---------------------------------------------------------------------------
# scan_yaml_dir: one listing rule, fail closed
# ---------------------------------------------------------------------------


def test_scan_lists_regular_and_hidden_yaml_files_sorted(tmp_path: Path) -> None:
    for name in ("b.yaml", "a.yaml", ".hidden.yaml", "notes.yml", "c.json"):
        (tmp_path / name).write_text("k: v\n")
    listing = scan_yaml_dir(tmp_path, "config dir")
    # Hidden *.yaml counts, matching the registry's glob("*.yaml"); *.yml does not.
    assert [p.name for p in listing.files] == [".hidden.yaml", "a.yaml", "b.yaml"]
    assert listing.non_files == []


def test_scan_follows_symlink_to_file_and_flags_broken_symlink(tmp_path: Path) -> None:
    target = tmp_path / "real.txt"
    target.write_text("k: v\n")
    d = tmp_path / "d"
    d.mkdir()
    (d / "linked.yaml").symlink_to(target)
    (d / "dangling.yaml").symlink_to(tmp_path / "gone")
    listing = scan_yaml_dir(d, "config dir")
    assert [p.name for p in listing.files] == ["linked.yaml"]
    assert [p.name for p in listing.non_files] == ["dangling.yaml"]


def test_scan_rejects_a_file_given_as_the_dir(tmp_path: Path) -> None:
    f = tmp_path / "not-a-dir"
    f.write_text("")
    with pytest.raises(YamlDirError, match="is not a directory"):
        scan_yaml_dir(f, "config dir")


def test_scan_rejects_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(YamlDirError, match="does not exist"):
        scan_yaml_dir(tmp_path / "absent", "approvals dir")


def test_scan_unreadable_dir_message_has_no_raw_errno_text(tmp_path: Path) -> None:
    d = tmp_path / "locked"
    d.mkdir()
    d.chmod(0)
    try:
        if os.access(d, os.R_OK):
            msg = "cannot make a directory unreadable here (running as root?)"
            if os.environ.get("CI"):
                pytest.fail(msg)
            pytest.skip(msg)
        with pytest.raises(YamlDirError) as info:
            scan_yaml_dir(d, "config dir")
    finally:
        d.chmod(stat.S_IRWXU)
    message = str(info.value)
    assert message.startswith("cannot read config dir ")
    assert "Errno" not in message  # strerror only, not the exception's repr


# ---------------------------------------------------------------------------
# health_check.py error paths beyond the acceptance tests
# ---------------------------------------------------------------------------


def _health(config_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    rc = health_main([
        "--config-dir", str(config_dir),
        "--state-dir", str(tmp_path / "state"),
        "--out-dir", str(tmp_path / "out"),
    ])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_health_yaml_directory_beside_real_config_is_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "eurlex.yaml").write_text(_FIXTURE_SOURCE.read_text())
    (config / "stray.yaml").mkdir()
    rc, out, err = _health(config, tmp_path, capsys)
    assert rc == EXIT_CONFIG_MISSING
    assert "stray.yaml in config dir" in err
    assert "is not a regular file" in err
    assert out == ""
    assert not (tmp_path / "out").exists(), "no report is written for a config error"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("key: [unclosed\n", "is not valid YAML (line "),
        ("- a\n- b\n", "is empty or not a YAML mapping"),
        ("just a string\n", "is empty or not a YAML mapping"),
        ("{}\n", "is empty or not a YAML mapping"),
    ],
    ids=["parse-error", "list", "scalar", "empty-mapping"],
)
def test_health_unusable_source_yaml_is_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str, expected: str
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "eurlex.yaml").write_text(_FIXTURE_SOURCE.read_text())
    (config / "broken.yaml").write_text(content)
    rc, _out, err = _health(config, tmp_path, capsys)
    assert rc == EXIT_CONFIG_MISSING
    assert "broken.yaml" in err
    assert expected in err
    assert "[unclosed" not in err  # the parser's quote of file bytes is not echoed


def test_health_non_utf8_source_yaml_is_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "latin.yaml").write_bytes(b"name: caf\xe9\n")
    rc, _out, err = _health(config, tmp_path, capsys)
    assert rc == EXIT_CONFIG_MISSING
    assert "cannot read source config latin.yaml" in err
    assert "UnicodeDecodeError" in err


def test_health_unreadable_source_file_is_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    locked = config / "locked.yaml"
    locked.write_text(_FIXTURE_SOURCE.read_text())
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            msg = "cannot make a file unreadable here (running as root?)"
            if os.environ.get("CI"):
                pytest.fail(msg)
            pytest.skip(msg)
        rc, _out, err = _health(config, tmp_path, capsys)
    finally:
        locked.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert rc == EXIT_CONFIG_MISSING
    assert "cannot read source config locked.yaml" in err


# ---------------------------------------------------------------------------
# check_approvals.py error paths beyond the acceptance tests
# ---------------------------------------------------------------------------


_VALID_APPROVAL = (
    'source_id: "eurlex"\n'
    f'signed_artifact_sha256: "{"a" * 64}"\n'
    'expiry: "2099-01-01"\n'
)


def test_approvals_empty_dir_message_counts_unverified_sources(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sources = tmp_path / "srcs"
    sources.mkdir()
    (sources / "a.yaml").write_text(_FIXTURE_SOURCE.read_text())
    (sources / "b.yaml").write_text(_FIXTURE_SOURCE.read_text())
    approvals = tmp_path / "appr"
    approvals.mkdir()
    rc = approvals_main(["--sources-dir", str(sources), "--approvals-dir", str(approvals)])
    captured = capsys.readouterr()
    assert rc == 2
    assert "2 source config(s)" in captured.err
    assert "unverified" in captured.err
    assert captured.out == ""


def test_approvals_yaml_directory_in_sources_dir_is_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sources = tmp_path / "srcs"
    sources.mkdir()
    (sources / "eurlex.yaml").write_text(_FIXTURE_SOURCE.read_text())
    (sources / "stray.yaml").mkdir()
    approvals = tmp_path / "appr"
    approvals.mkdir()
    (approvals / "eurlex.yaml").write_text(_VALID_APPROVAL)
    rc = approvals_main(["--sources-dir", str(sources), "--approvals-dir", str(approvals)])
    captured = capsys.readouterr()
    assert rc == 2
    assert "stray.yaml in sources dir" in captured.err
    assert captured.out == ""


def test_approvals_yaml_directory_beside_real_approval_is_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sources = tmp_path / "srcs"
    sources.mkdir()
    (sources / "eurlex.yaml").write_text(_FIXTURE_SOURCE.read_text())
    approvals = tmp_path / "appr"
    approvals.mkdir()
    (approvals / "eurlex.yaml").write_text(_VALID_APPROVAL)
    (approvals / "stray.yaml").mkdir()
    rc = approvals_main(["--sources-dir", str(sources), "--approvals-dir", str(approvals)])
    captured = capsys.readouterr()
    assert rc == 2
    assert "stray.yaml in approvals dir" in captured.err


def test_approvals_zero_sources_but_real_approvals_still_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Approvals present means there is work to do; an empty sources dir alone is not exit 2.
    sources = tmp_path / "srcs"
    sources.mkdir()
    approvals = tmp_path / "appr"
    approvals.mkdir()
    (approvals / "eurlex.yaml").write_text(_VALID_APPROVAL)
    rc = approvals_main(["--sources-dir", str(sources), "--approvals-dir", str(approvals)])
    assert rc == 0
    assert "| eurlex | OK |" in capsys.readouterr().out
