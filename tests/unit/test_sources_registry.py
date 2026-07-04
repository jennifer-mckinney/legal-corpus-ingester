from __future__ import annotations
import pytest
from pathlib import Path


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
    import tempfile
    import yaml
    from pathlib import Path as P
    with tempfile.TemporaryDirectory() as tmp:
        bad = P(tmp) / "bad.yaml"
        bad.write_text(yaml.dump({"name": "bad", "jurisdiction": "XX-INVALID"}))
        with pytest.raises(RegistryLoadError) as exc_info:
            load_all(P(tmp))
        assert "bad.yaml" in str(exc_info.value)
