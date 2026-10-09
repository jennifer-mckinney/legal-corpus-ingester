"""Unit tests for scripts/health_check.py."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from legal_corpus_ingester import cli

# scripts/ is not a package; inject it into the path.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

import health_check  # noqa: E402
from health_check import (  # noqa: E402
    EXIT_CONFIG_MISSING,
    EXIT_SOURCE_PROBLEM,
    YamlDirError,
    _status_label,
    build_report,
    health_verdict,
    main,
)


class TestStatusLabel:
    def test_fresh(self):
        assert _status_label(10.0, stale_days=8) == "fresh"

    def test_stale(self):
        # 9 days = 216 hours > 8 * 24
        assert _status_label(216.0, stale_days=8) == "stale"

    def test_never_run(self):
        assert _status_label(None, stale_days=8) == "never-run"

    def test_exactly_stale(self):
        # lag == stale_days exactly → stale
        assert _status_label(192.0, stale_days=8) == "stale"


class TestBuildReport:
    @pytest.mark.parametrize("make_dir", [True, False], ids=["empty-dir", "missing-dir"])
    def test_no_sources_is_not_success(self, tmp_path, make_dir):
        # Zero sources, or no dir at all, must never be a clean report (terms-analysis#173, grumpy 4).
        config_dir = tmp_path / "config"
        if make_dir:
            config_dir.mkdir()
        # Grumpy r2-1: one contract only. build_report raises; it never returns a report.
        with pytest.raises(YamlDirError) as raised:
            build_report(
                config_dir=config_dir,
                state_dir=tmp_path / "state",
                out_dir=tmp_path / "out",
                stale_days=8,
                today="2026-07-04",
                refresh_wired=True,
            )
        if make_dir:
            assert str(raised.value) == health_check._empty_dir_problem(config_dir, "config dir")
        else:
            assert "does not exist" in str(raised.value)

    def test_all_fresh(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "eurlex.yaml").write_text("name: eurlex\n")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # Write a checkpoint so the source is "fresh" (recent mtime)
        cp = state_dir / "eurlex.checkpoint.json"
        cp.write_text(json.dumps({"stage": "publish"}))
        # Patch _checkpoint_info to control lag — instead use real file with recent mtime
        report, verdict = build_report(
            config_dir=config_dir,
            state_dir=state_dir,
            out_dir=tmp_path / "out",
            stale_days=8,
            today="2026-07-04",
            refresh_wired=True,
        )
        # Checkpoint was just written — lag is seconds, far below 8 days
        assert "fresh" in report
        assert verdict == 0

    def test_one_never_run(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "eurlex.yaml").write_text("name: eurlex\n")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # No checkpoint file → never-run
        report, verdict = build_report(
            config_dir=config_dir,
            state_dir=state_dir,
            out_dir=tmp_path / "out",
            stale_days=8,
            today="2026-07-04",
            refresh_wired=True,
        )
        assert "never-run" in report
        assert verdict == EXIT_SOURCE_PROBLEM


class TestMainExitCodes:
    """main() must not report success when it could not check anything (terms-analysis#90)."""

    def test_missing_config_dir_exits_config_missing(self, tmp_path, capsys):
        missing = tmp_path / "no-such-config"
        rc = main(["--config-dir", str(missing), "--state-dir", str(tmp_path / "state"),
                   "--out-dir", str(tmp_path / "out")])
        assert rc == EXIT_CONFIG_MISSING
        assert EXIT_CONFIG_MISSING not in (0, 1)
        captured = capsys.readouterr()
        assert "does not exist" in captured.err
        assert captured.out == ""

    def test_never_run_source_exits_one_once_refresh_is_wired(self, tmp_path, monkeypatch):
        monkeypatch.setattr(health_check, "REFRESH_WIRED", True)
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "eurlex.yaml").write_text("name: eurlex\n")
        rc = main(["--config-dir", str(config_dir), "--state-dir", str(tmp_path / "state"),
                   "--out-dir", str(tmp_path / "out")])
        assert rc == EXIT_SOURCE_PROBLEM == 1

    def test_never_run_while_unwired_exits_not_wired_and_says_why(self, tmp_path, capsys):
        # Today's real state: refresh is unwired, so no checkpoint can exist. That is the
        # known state (cli.EXIT_NOT_WIRED), not success and not a broken pipeline.
        assert health_check.REFRESH_WIRED is cli.REFRESH_WIRED is False
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "eurlex.yaml").write_text("name: eurlex\n")
        rc = main(["--config-dir", str(config_dir), "--state-dir", str(tmp_path / "state"),
                   "--out-dir", str(tmp_path / "out")])
        assert rc == cli.EXIT_NOT_WIRED
        out = capsys.readouterr().out
        assert "| eurlex | never | - | - | never-run |" in out
        assert "Refresh is not wired yet" in out
        written = next((tmp_path / "out" / "health").glob("*.md")).read_text()
        assert written == out

    def test_stale_source_while_unwired_still_exits_one(self, tmp_path):
        # Unwired only excuses never-run. A checkpoint that exists and is old is real staleness.
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        for name in ("eurlex", "cfpb"):
            (config_dir / f"{name}.yaml").write_text(f"name: {name}\n")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        cp = state_dir / "eurlex.checkpoint.json"
        cp.write_text(json.dumps({"stage": "done"}))
        old = time.time() - 9 * 86400
        os.utime(cp, (old, old))
        rc = main(["--config-dir", str(config_dir), "--state-dir", str(state_dir),
                   "--out-dir", str(tmp_path / "out")])
        assert rc == EXIT_SOURCE_PROBLEM


class TestHealthVerdict:
    """Truth table for the one function that decides the health exit code."""

    @pytest.mark.parametrize(
        ("statuses", "wired", "expected"),
        [
            ([], False, 0),
            ([], True, 0),
            (["fresh"], False, 0),
            (["fresh", "fresh"], True, 0),
            (["never-run"], False, cli.EXIT_NOT_WIRED),
            (["fresh", "never-run"], False, cli.EXIT_NOT_WIRED),
            (["never-run"], True, EXIT_SOURCE_PROBLEM),
            (["fresh", "never-run"], True, EXIT_SOURCE_PROBLEM),
            (["stale"], False, EXIT_SOURCE_PROBLEM),
            (["stale"], True, EXIT_SOURCE_PROBLEM),
            (["never-run", "stale"], False, EXIT_SOURCE_PROBLEM),
            # Fail closed: a status nobody planned for is a problem, never excused.
            (["unknown"], False, EXIT_SOURCE_PROBLEM),
            (["never-run", ""], False, EXIT_SOURCE_PROBLEM),
        ],
    )
    def test_truth_table(self, statuses, wired, expected):
        assert health_verdict(statuses, refresh_wired=wired) == expected

    def test_codes_are_distinct(self):
        codes = (0, EXIT_SOURCE_PROBLEM, EXIT_CONFIG_MISSING, cli.EXIT_NOT_WIRED)
        assert len(set(codes)) == len(codes)
