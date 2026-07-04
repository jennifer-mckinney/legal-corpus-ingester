from __future__ import annotations
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="INGESTER_",
        env_file_encoding="utf-8",
    )

    out_dir: Path = Path("./out")
    state_dir: Path = Path("./state")
    config_dir: Path = Path("./config")
    corpus_dir: Path = Path("./out/current/corpus")
    localai_url: str = "http://localhost:8080/v1"
    localai_model: str = "apertus-8b-instruct"
    log_level: str = "INFO"
