from __future__ import annotations
import os
from pathlib import Path


def test_settings_loads_with_defaults() -> None:
    # Remove any INGESTER_ env vars that might bleed in from environment
    for key in list(os.environ.keys()):
        if key.startswith("INGESTER_"):
            del os.environ[key]
    from legal_corpus_ingester.config import Settings
    s = Settings()
    assert isinstance(s.out_dir, Path)
    assert isinstance(s.state_dir, Path)
    assert isinstance(s.localai_url, str)


def test_settings_env_var_override() -> None:
    os.environ["INGESTER_OUT_DIR"] = "/tmp/test-out"
    os.environ["INGESTER_LOG_LEVEL"] = "DEBUG"
    try:
        # Re-import to pick up new env (or instantiate fresh Settings)
        from legal_corpus_ingester.config import Settings
        s = Settings()
        assert str(s.out_dir) == "/tmp/test-out"
        assert s.log_level == "DEBUG"
    finally:
        del os.environ["INGESTER_OUT_DIR"]
        del os.environ["INGESTER_LOG_LEVEL"]


def test_settings_path_fields_are_paths() -> None:
    from legal_corpus_ingester.config import Settings
    s = Settings()
    for field_name in ("out_dir", "state_dir", "config_dir", "corpus_dir"):
        val = getattr(s, field_name)
        assert isinstance(val, Path), f"{field_name} should be Path, got {type(val)}"
