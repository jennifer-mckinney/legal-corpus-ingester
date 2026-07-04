"""Unit tests for scripts/health_check.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path

# scripts/ is not a package; inject it into the path.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

from health_check import _status_label, build_report  # noqa: E402


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
    def test_no_sources(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        report, any_problem = build_report(
            config_dir=config_dir,
            state_dir=tmp_path / "state",
            out_dir=tmp_path / "out",
            stale_days=8,
            today="2026-07-04",
        )
        assert "No sources" in report
        assert any_problem is False

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
        report, any_problem = build_report(
            config_dir=config_dir,
            state_dir=state_dir,
            out_dir=tmp_path / "out",
            stale_days=8,
            today="2026-07-04",
        )
        # Checkpoint was just written — lag is seconds, far below 8 days
        assert "fresh" in report
        assert any_problem is False

    def test_one_never_run(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "eurlex.yaml").write_text("name: eurlex\n")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # No checkpoint file → never-run
        report, any_problem = build_report(
            config_dir=config_dir,
            state_dir=state_dir,
            out_dir=tmp_path / "out",
            stale_days=8,
            today="2026-07-04",
        )
        assert "never-run" in report
        assert any_problem is True
