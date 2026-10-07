"""Unit tests for scripts/detect_bundle_change.py (terms-analysis#90).

The refresh workflow's old pre/post symlink diff always saw an empty "before"
value, because checkout's clean wipes out/. These tests pin the replacement:
change is decided against a durable record of the last announced bundle, and
"refresh succeeded but published nothing valid" is a loud failure, never "no change".
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# scripts/ is not a package; inject it into the path.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

import detect_bundle_change as dbc  # noqa: E402


def _bundle(out: Path, version: str, corpus_sha: str = "a" * 64, manifest_sha: str = "m" * 64) -> Path:
    """Build out/<version>/checksums.txt and point out/current at it."""
    bundle = out / version
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "checksums.txt").write_text(
        f"{corpus_sha}  index/legal_kb.npy\n{'b' * 64}  index/legal_kb_metadata.json\n"
        f"{manifest_sha}  MANIFEST.yaml\n",
        encoding="utf-8",
    )
    link = out / "current"
    if link.is_symlink():
        link.unlink()
    link.symlink_to(version)
    return link


def _detect(link: Path, state: Path, gh_out: Path | None = None) -> int:
    argv = ["detect", "--bundle-link", str(link), "--state-file", str(state)]
    if gh_out is not None:
        argv += ["--github-output", str(gh_out)]
    return dbc.main(argv)


def _record(link: Path, state: Path) -> int:
    return dbc.main(["record", "--bundle-link", str(link), "--state-file", str(state)])


def _outputs(gh_out: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in gh_out.read_text(encoding="utf-8").splitlines())


class TestDetect:
    def test_first_publish_is_a_change(self, tmp_path: Path) -> None:
        link = _bundle(tmp_path / "out", "2026.10.0")
        gh_out = tmp_path / "gh_output"
        assert _detect(link, tmp_path / "state.json", gh_out) == 0
        assert _outputs(gh_out) == {"changed": "true", "new_target": "2026.10.0"}

    def test_same_bundle_after_record_is_not_a_change(self, tmp_path: Path) -> None:
        # The bug this replaces: every run looked changed because "before" was always empty.
        out, state = tmp_path / "out", tmp_path / "state.json"
        link = _bundle(out, "2026.10.0")
        assert _record(link, state) == 0
        # Simulate checkout's clean: out/ is wiped and the refresh rebuilds the same bundle.
        for p in sorted(out.rglob("*"), reverse=True):
            p.unlink() if p.is_file() or p.is_symlink() else p.rmdir()
        link = _bundle(out, "2026.10.0")
        gh_out = tmp_path / "gh_output"
        assert _detect(link, state, gh_out) == 0
        assert _outputs(gh_out)["changed"] == "false"

    def test_manifest_only_change_is_not_a_change(self, tmp_path: Path) -> None:
        out, state = tmp_path / "out", tmp_path / "state.json"
        assert _record(_bundle(out, "2026.10.0"), state) == 0
        link = _bundle(out, "2026.10.0", manifest_sha="n" * 64)
        gh_out = tmp_path / "gh_output"
        assert _detect(link, state, gh_out) == 0
        assert _outputs(gh_out)["changed"] == "false"

    def test_new_content_under_same_version_is_a_change(self, tmp_path: Path) -> None:
        out, state = tmp_path / "out", tmp_path / "state.json"
        assert _record(_bundle(out, "2026.10.0"), state) == 0
        link = _bundle(out, "2026.10.0", corpus_sha="c" * 64)
        gh_out = tmp_path / "gh_output"
        assert _detect(link, state, gh_out) == 0
        assert _outputs(gh_out)["changed"] == "true"

    def test_new_version_is_a_change(self, tmp_path: Path) -> None:
        out, state = tmp_path / "out", tmp_path / "state.json"
        assert _record(_bundle(out, "2026.10.0"), state) == 0
        link = _bundle(out, "2026.10.1")
        gh_out = tmp_path / "gh_output"
        assert _detect(link, state, gh_out) == 0
        assert _outputs(gh_out) == {"changed": "true", "new_target": "2026.10.1"}

    def test_detect_does_not_write_the_record(self, tmp_path: Path) -> None:
        # Only `record` (run after a successful alert) may write it, so a failed alert retries.
        state = tmp_path / "state.json"
        assert _detect(_bundle(tmp_path / "out", "2026.10.0"), state) == 0
        assert not state.exists()


class TestLoudFailures:
    def test_missing_current_link_fails(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (tmp_path / "out").mkdir()
        gh_out = tmp_path / "gh_output"
        assert _detect(tmp_path / "out" / "current", tmp_path / "state.json", gh_out) == 1
        assert "published no bundle" in capsys.readouterr().err
        assert not gh_out.exists()

    def test_bad_version_name_fails_and_never_reaches_github_output(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        out.mkdir()
        (out / "current").symlink_to("2026.10.0\nchanged=true")
        gh_out = tmp_path / "gh_output"
        assert _detect(out / "current", tmp_path / "state.json", gh_out) == 1
        assert not gh_out.exists()

    def test_missing_checksums_fails(self, tmp_path: Path) -> None:
        link = _bundle(tmp_path / "out", "2026.10.0")
        (tmp_path / "out" / "2026.10.0" / "checksums.txt").unlink()
        assert _detect(link, tmp_path / "state.json") == 1

    def test_checksums_with_only_manifest_fails(self, tmp_path: Path) -> None:
        link = _bundle(tmp_path / "out", "2026.10.0")
        (tmp_path / "out" / "2026.10.0" / "checksums.txt").write_text(f"{'m' * 64}  MANIFEST.yaml\n")
        assert _detect(link, tmp_path / "state.json") == 1

    @pytest.mark.parametrize("content", ["not json", "[]", '{"version": "2026.10.0"}'])
    def test_corrupt_state_file_fails_not_no_change(self, tmp_path: Path, content: str) -> None:
        state = tmp_path / "state.json"
        state.write_text(content, encoding="utf-8")
        assert _detect(_bundle(tmp_path / "out", "2026.10.0"), state) == 1

    def test_record_fails_without_a_bundle(self, tmp_path: Path) -> None:
        state = tmp_path / "state.json"
        assert _record(tmp_path / "nope", state) == 1
        assert not state.exists()


class TestStateFile:
    def test_record_writes_version_and_fingerprint(self, tmp_path: Path) -> None:
        state = tmp_path / "nested" / "state.json"
        assert _record(_bundle(tmp_path / "out", "2026.10.0"), state) == 0
        data = json.loads(state.read_text(encoding="utf-8"))
        assert data["version"] == "2026.10.0"
        assert len(data["fingerprint"]) == 64
        assert not (state.parent / "state.json.tmp").exists()

    def test_default_state_file_is_outside_the_checkout(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert dbc.default_state_file() == tmp_path / "legal-corpus-ingester" / "last-published-bundle.json"
        monkeypatch.delenv("XDG_STATE_HOME")
        assert dbc.default_state_file() == (
            Path.home() / ".local" / "state" / "legal-corpus-ingester" / "last-published-bundle.json"
        )
        assert Path(os.getcwd()) not in dbc.default_state_file().parents
