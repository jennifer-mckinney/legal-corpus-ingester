from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Unicode categories that must never reach a URL, model name or path: control (Cc, which
# includes CR/LF/NUL), format (Cf: bidi overrides, zero-width), lone surrogates (Cs) and the
# line/paragraph separators (Zl, Zp). Any of them in a header-bound value is an injection
# vector, and in a path a silent truncation or a spoofed name (terms-analysis#93, F13).
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
# The only URL schemes the LocalAI client (httpx) can speak. An allowlist, not a deny-list: any
# other scheme (file, ftp, none at all) is refused when Settings load.
_LOCALAI_SCHEMES = frozenset({"http", "https"})


def _has_unsafe_char(value: str) -> bool:
    return any(unicodedata.category(c) in _UNSAFE_CATEGORIES for c in value)


class Settings(BaseSettings):
    """Runtime settings, read from INGESTER_* env vars and .env.

    Every value is validated when Settings load, so a bad environment fails closed before any
    command does work (terms-analysis#93). Error messages name the field, never echo the value.
    """

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

    @field_validator("out_dir", "state_dir", "config_dir", "corpus_dir", mode="before")
    @classmethod
    def _path_is_usable(cls, v: Any) -> Any:
        # mode="before": pydantic turns "" into Path("") == Path("."), which would silently
        # write into the cwd. An empty or blank value, or one with an unsafe character, is a
        # configuration mistake, not a request for the cwd.
        text = str(v) if isinstance(v, (str, Path)) else None
        if text is None:
            return v
        if not text.strip():
            raise ValueError("must not be empty")
        if _has_unsafe_char(text):
            raise ValueError("must not contain control, format or separator characters")
        return v

    @field_validator("localai_url")
    @classmethod
    def _localai_url_is_http(cls, v: str) -> str:
        if _has_unsafe_char(v) or any(c.isspace() for c in v):
            raise ValueError("must not contain whitespace, control, format or separator characters")
        try:
            parts = urlsplit(v)
            parts.port  # noqa: B018 - raises ValueError on an out-of-range or non-numeric port
        except ValueError as exc:
            raise ValueError("is not a valid URL") from exc
        if parts.scheme not in _LOCALAI_SCHEMES:
            raise ValueError("must be an http:// or https:// URL")
        if not parts.hostname:
            raise ValueError("must name a host")
        return v

    @field_validator("localai_model")
    @classmethod
    def _localai_model_is_safe(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        if _has_unsafe_char(v):
            raise ValueError("must not contain control, format or separator characters")
        return v
