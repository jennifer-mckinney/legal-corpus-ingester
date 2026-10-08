"""Round-3 review fixes for the VCR drift canary (terms-analysis#92)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import vcr_drift_report as vdr  # noqa: E402


def test_snippets_cut_trailer_never_exceeds_cap():
    """LOW: near-cap report + snippets_cut trailer must stay within the cap."""
    out = vdr._bounded_report(["x" * (vdr.MAX_REPORT_BYTES - 5)], snippets_cut=True)
    assert len(out.encode("utf-8")) <= vdr.MAX_REPORT_BYTES
    assert vdr.TRUNCATION_MARKER in out


def test_snippets_cut_trailer_appended_when_it_fits():
    out = vdr._bounded_report(["short"], snippets_cut=True)
    assert out.startswith("short")
    assert vdr.TRUNCATION_MARKER in out


def _recorded_cassette_names() -> set[str]:
    """Cassette stems produced by @pytest.mark.vcr tests under tests/."""
    names: set[str] = set()
    for path in (REPO / "tests").rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            marks = []
            for dec in node.decorator_list:
                text = ast.unparse(dec)
                marks.append((text, dec))
            if not any(t.startswith("pytest.mark.vcr") for t, _ in marks):
                continue
            custom = False
            for text, dec in marks:
                if text.startswith("pytest.mark.default_cassette") and isinstance(dec, ast.Call):
                    names.add(ast.literal_eval(dec.args[0]))
                    custom = True
            if not custom:
                names.add(node.name)  # pytest-recording default: test name
    return names


def test_every_committed_cassette_is_recorded_by_a_vcr_test():
    """MEDIUM: a cassette no @pytest.mark.vcr test records would be compared
    with itself by the canary. Hand-crafted fixtures belong in fixtures/static."""
    recorded = _recorded_cassette_names()
    assert recorded, "no @pytest.mark.vcr tests found"
    cassettes = list((REPO / "tests" / "fixtures" / "cassettes").rglob("*.yaml"))
    assert cassettes, "no committed cassettes found"
    orphans = [str(c.relative_to(REPO)) for c in cassettes if c.stem not in recorded]
    assert not orphans, f"cassettes not produced by any vcr test: {orphans}"


def test_workflow_deletes_cassettes_before_rerecord_and_no_tight_head_cap():
    wf = (REPO / ".github" / "workflows" / "vcr-drift.yml").read_text(encoding="utf-8")
    assert wf.index("-name '*.yaml' -delete") < wf.index("--record-mode=rewrite")
    assert "head -c 60000" not in wf
