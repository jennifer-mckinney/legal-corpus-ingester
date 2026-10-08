from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.unit.test_p9_gate_fix_r1 import in_ci

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync_jurisdictions.py"


def _sync_module() -> ModuleType:
    """The sync script as a module, so its paths are read, not restated."""
    spec = importlib.util.spec_from_file_location("sync_jurisdictions", SYNC_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_jurisdiction_codes_is_frozenset() -> None:
    from legal_corpus_ingester.jurisdictions import JURISDICTION_CODES
    assert isinstance(JURISDICTION_CODES, frozenset)


def test_jurisdiction_codes_nonempty() -> None:
    from legal_corpus_ingester.jurisdictions import JURISDICTION_CODES
    assert len(JURISDICTION_CODES) > 10


def test_jurisdiction_codes_contains_known_values() -> None:
    from legal_corpus_ingester.jurisdictions import JURISDICTION_CODES
    # These must always be present — they are in terms-analysis schemas.py
    for code in ("US-CA", "GDPR", "PIPEDA", "LGPD"):
        assert code in JURISDICTION_CODES, f"{code} missing from JURISDICTION_CODES"


def test_sync_script_is_idempotent() -> None:
    """Running sync_jurisdictions.py twice produces identical output.

    It needs the terms-analysis checkout (TERMS_ANALYSIS_ROOT, else the
    adjacent directory). Under CI a missing checkout is a failure, never a
    skip: CI provisions it (QUALITY-BAR D, terms-analysis#175 r5).
    """
    assert SYNC_SCRIPT.is_file(), "sync_jurisdictions.py must exist"
    sync = _sync_module()
    schemas = sync.terms_analysis_schemas()
    if not schemas.is_file():
        if in_ci(os.environ.get("CI")):
            pytest.fail(
                f"the terms-analysis checkout is missing in CI: no {schemas}; "
                f"the workflow must check it out and set {sync.ROOT_ENV}"
            )
        pytest.skip(f"requires the terms-analysis checkout ({schemas}); set {sync.ROOT_ENV}")

    subprocess.run(
        [sys.executable, str(SYNC_SCRIPT)],
        capture_output=True, text=True, check=True
    )
    content1 = sync.OUT_FILE.read_text()

    subprocess.run(
        [sys.executable, str(SYNC_SCRIPT)],
        capture_output=True, text=True, check=True
    )
    content2 = sync.OUT_FILE.read_text()

    assert content1 == content2, "sync_jurisdictions.py is not idempotent"
