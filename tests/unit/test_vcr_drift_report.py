"""Unit tests for scripts/vcr_drift_report.py."""
from __future__ import annotations

import sys
from pathlib import Path

# scripts/ is not a package; inject it into the path.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

from vcr_drift_report import diff_cassette, generate_report, load_cassette  # noqa: E402


def _make_interaction(
    uri: str = "http://example.com",
    method: str = "GET",
    status: int = 200,
    body: str = "ok",
) -> dict:
    return {
        "request": {"uri": uri, "method": method},
        "response": {
            "status": {"code": status},
            "body": {"string": body},
        },
    }


def _cassette(interactions: list) -> dict:
    return {"interactions": interactions}


class TestDiffCassette:
    def test_no_drift(self):
        ix = _make_interaction()
        result = diff_cassette(_cassette([ix]), _cassette([ix]))
        assert result["changed"] is False
        assert result["uri_drift"] == 0
        assert result["body_drift"] == 0

    def test_uri_drift(self):
        b = _cassette([_make_interaction(uri="http://a.com")])
        c = _cassette([_make_interaction(uri="http://b.com")])
        result = diff_cassette(b, c)
        assert result["changed"] is True
        assert result["uri_drift"] == 1

    def test_body_drift(self):
        b = _cassette([_make_interaction(body="hello")])
        c = _cassette([_make_interaction(body="world")])
        result = diff_cassette(b, c)
        assert result["changed"] is True
        assert result["body_drift"] == 1

    def test_status_code_drift(self):
        b = _cassette([_make_interaction(status=200)])
        c = _cassette([_make_interaction(status=403)])
        result = diff_cassette(b, c)
        assert result["changed"] is True
        assert any("status code" in d for d in result["details"])

    def test_method_drift(self):
        b = _cassette([_make_interaction(method="GET")])
        c = _cassette([_make_interaction(method="POST")])
        result = diff_cassette(b, c)
        assert result["changed"] is True
        assert result["method_drift"] == 1
        assert any("method" in d for d in result["details"])

    def test_count_mismatch(self):
        ix = _make_interaction()
        b = _cassette([ix, ix])
        c = _cassette([ix])
        result = diff_cassette(b, c)
        assert result["changed"] is True
        assert result["interaction_count_baseline"] == 2
        assert result["interaction_count_current"] == 1

    def test_zero_interactions(self):
        result = diff_cassette(_cassette([]), _cassette([]))
        assert result["changed"] is False


class TestLoadCassette:
    def test_empty_yaml_returns_error(self, tmp_path):
        f = tmp_path / "empty.yaml"
        f.write_text("")
        data, err = load_cassette(f)
        assert data is None
        assert err is not None
        assert "Empty" in err

    def test_missing_file_returns_error(self, tmp_path):
        data, err = load_cassette(tmp_path / "missing.yaml")
        assert data is None
        assert err is not None

    def test_valid_cassette(self, tmp_path):
        f = tmp_path / "ok.yaml"
        f.write_text("interactions: []\n")
        data, err = load_cassette(f)
        assert err is None
        assert data == {"interactions": []}


class TestGenerateReport:
    def test_no_cassettes(self, tmp_path):
        b = tmp_path / "baseline"
        c = tmp_path / "current"
        b.mkdir()
        c.mkdir()
        report, any_drift = generate_report(b, c)
        assert any_drift is False
        assert "No cassettes" in report

    def test_no_drift(self, tmp_path):
        b = tmp_path / "baseline"
        c = tmp_path / "current"
        b.mkdir()
        c.mkdir()
        cassette_yaml = "interactions:\n  - request:\n      uri: http://x.com\n      method: GET\n    response:\n      status:\n        code: 200\n      body:\n        string: ok\n"
        (b / "a.yaml").write_text(cassette_yaml)
        (c / "a.yaml").write_text(cassette_yaml)
        report, any_drift = generate_report(b, c)
        assert any_drift is False
        assert "No drift" in report

    def test_drift_detected(self, tmp_path):
        b = tmp_path / "baseline"
        c = tmp_path / "current"
        b.mkdir()
        c.mkdir()
        (b / "a.yaml").write_text("interactions:\n  - request:\n      uri: http://x.com\n      method: GET\n    response:\n      status:\n        code: 200\n      body:\n        string: hello\n")
        (c / "a.yaml").write_text("interactions:\n  - request:\n      uri: http://x.com\n      method: GET\n    response:\n      status:\n        code: 200\n      body:\n        string: world\n")
        report, any_drift = generate_report(b, c)
        assert any_drift is True
        assert "Changed" in report

    def test_parse_error_triggers_drift(self, tmp_path):
        b = tmp_path / "baseline"
        c = tmp_path / "current"
        b.mkdir()
        c.mkdir()
        (b / "bad.yaml").write_text(": : invalid\n")
        (c / "bad.yaml").write_text(": : invalid\n")
        report, any_drift = generate_report(b, c)
        assert any_drift is True
        assert "Parse errors" in report
