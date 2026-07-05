from __future__ import annotations
import re
import tempfile
import yaml
import pytest
from datetime import date
from pathlib import Path

# Root of the repository — resolved relative to this test file's location.
_REPO_ROOT: Path = Path(__file__).parent.parent.parent
_PROD_SOURCES_DIR: Path = _REPO_ROOT / "config" / "sources"
_PROD_APPROVALS_DIR: Path = _REPO_ROOT / "config" / "approvals"


def test_load_all_returns_dict_keyed_by_name() -> None:
    from legal_corpus_ingester.sources.registry import load_all
    registry = load_all(Path("tests/fixtures/sources"))
    assert "eurlex" in registry
    cfg = registry["eurlex"]
    assert cfg.name == "eurlex"
    assert cfg.license.spdx == "CC-BY-4.0"


def test_load_all_fetcher_class_path_is_string() -> None:
    from legal_corpus_ingester.sources.registry import load_all
    registry = load_all(Path("tests/fixtures/sources"))
    cfg = registry["eurlex"]
    # class path stored as string, not imported yet (lazy)
    assert isinstance(cfg.pipeline.fetcher, str)
    assert "." in cfg.pipeline.fetcher  # dotted path


def test_invalid_yaml_raises_with_filename() -> None:
    from legal_corpus_ingester.sources.registry import load_all, RegistryLoadError
    with pytest.raises(RegistryLoadError) as exc_info:
        load_all(Path("tests/fixtures/sources/invalid_dir"))
    # Error must mention the directory or file
    assert "invalid_dir" in str(exc_info.value) or "not found" in str(exc_info.value).lower()


def test_invalid_schema_raises_with_filename() -> None:
    from legal_corpus_ingester.sources.registry import load_all, RegistryLoadError
    # Create a fixture dir with a bad yaml and load it
    from pathlib import Path as P
    with tempfile.TemporaryDirectory() as tmp:
        bad = P(tmp) / "bad.yaml"
        bad.write_text(yaml.dump({"name": "bad", "jurisdiction": "XX-INVALID"}))
        with pytest.raises(RegistryLoadError) as exc_info:
            load_all(P(tmp))
        assert "bad.yaml" in str(exc_info.value)


# ---------------------------------------------------------------------------
# R3: runtime enumeration — no hardcoded source names, CELEX IDs, or counts.
# Discovers whatever is present in config/sources/ at test-run time.
# ---------------------------------------------------------------------------

def test_sources_registry_prod_yaml_loads_and_validates() -> None:
    """Dynamically load all prod source YAMLs and assert structural invariants.

    No source names, CELEX IDs, or expected counts are hardcoded here.
    The only count constraint is 'at least one', so the test fails loudly
    when the directory is empty or the path is wrong.
    """
    from legal_corpus_ingester.sources.registry import load_all

    registry = load_all(_PROD_SOURCES_DIR)

    assert len(registry) >= 1, (
        f"config/sources/ contains no source YAMLs; expected at least one. "
        f"Searched: {_PROD_SOURCES_DIR}"
    )

    from legal_corpus_ingester.sources.schema import VALID_JURISDICTIONS

    for cfg in registry.values():
        assert cfg.jurisdiction in VALID_JURISDICTIONS, (
            f"Source {cfg.name!r}: jurisdiction {cfg.jurisdiction!r} is not a known jurisdiction code"
        )
        assert cfg.license.spdx and len(cfg.license.spdx) > 0, (
            f"Source {cfg.name!r}: license.spdx must be a non-empty string"
        )
        assert cfg.pipeline.celex_id is not None and len(cfg.pipeline.celex_id) > 0, (
            f"Source {cfg.name!r}: pipeline.celex_id must be a non-empty string, "
            f"got {cfg.pipeline.celex_id!r}"
        )
        assert cfg.pipeline.cleaner is not None and len(cfg.pipeline.cleaner) > 0, (
            f"Source {cfg.name!r}: pipeline.cleaner must be a non-empty string, "
            f"got {cfg.pipeline.cleaner!r}"
        )
        assert cfg.pipeline.chunker is not None and len(cfg.pipeline.chunker) > 0, (
            f"Source {cfg.name!r}: pipeline.chunker must be a non-empty string, "
            f"got {cfg.pipeline.chunker!r}"
        )
        assert registry[cfg.name] is cfg, (
            f"Registry key mismatch: registry[{cfg.name!r}] is not the same object "
            f"as cfg (name={cfg.name!r})"
        )


# ---------------------------------------------------------------------------
# Approval YAML structural validity — dynamic, no hardcoded source names.
# ---------------------------------------------------------------------------

_SHA256_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{64}$")


def test_sources_registry_prod_approvals_structurally_valid() -> None:
    """Dynamically load all approval YAMLs and assert structural invariants.

    No source names are hardcoded.  At least one approval file must exist
    so an empty directory is caught immediately.
    """
    approval_files = sorted(_PROD_APPROVALS_DIR.glob("*.yaml"))

    assert len(approval_files) >= 1, (
        f"config/approvals/ contains no YAML files; expected at least one. "
        f"Searched: {_PROD_APPROVALS_DIR}"
    )

    for path in approval_files:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))

        source_id = data.get("source_id", "")
        assert isinstance(source_id, str) and len(source_id) > 0, (
            f"{path.name}: 'source_id' must be a non-empty string, got {source_id!r}"
        )

        sha = data.get("signed_artifact_sha256", "")
        assert isinstance(sha, str) and _SHA256_RE.match(sha), (
            f"{path.name}: 'signed_artifact_sha256' must match [0-9a-f]{{64}}, "
            f"got {sha!r}"
        )

        expiry_raw = data.get("expiry", "")
        try:
            date.fromisoformat(str(expiry_raw))
        except (ValueError, TypeError) as exc:
            raise AssertionError(
                f"{path.name}: 'expiry' must be a valid ISO date string "
                f"(YYYY-MM-DD), got {expiry_raw!r}"
            ) from exc
