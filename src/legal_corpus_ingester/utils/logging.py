# src/legal_corpus_ingester/utils/logging.py
from __future__ import annotations
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import IO, Optional

# SecF1: case-insensitive deny-list for sensitive extras. Any key whose lower-cased
# form appears in this set, or contains one of the substrings below, is redacted
# rather than serialized. Prevents leaking secrets passed via `extra=` into
# aggregated logs.
_REDACT_KEYS = frozenset({
    "password", "passwd", "secret", "token", "authorization", "api_key",
    "apikey", "access_key", "private_key", "cookie", "set-cookie",
    "session", "bearer", "email", "auth", "credentials",
    # SecF1' (round-2): explicit shorthand secret names that the substring
    # heuristic misses. Case-insensitive comparison happens against .lower().
    "jwt", "pat", "personal_access_token", "access_token", "refresh_token",
    "private_token", "x-api-key", "x_api_key",
    # SecF1'' (round-4): dropping the bare "key" substring in round-3 left
    # `signing_key` / `signing-key` uncovered. Add explicit entries for real-
    # world signing/OAuth/OIDC secret names. `client_secret`/`hmac_secret`/
    # `oauth_token`/`id_token` are already caught by the `secret`/`token`
    # substring safety net; listing them explicitly is defense-in-depth against
    # future refactors of `_REDACT_SUBSTRINGS`.
    "signing_key", "signing-key", "hmac_secret", "client_secret",
    "oauth_token", "id_token",
})
# G2' (round-2): bare "key" removed. It over-matched debuggability-critical
# extras like `keyword`, `foreign_key`, `cache_key_prefix`, `stakeholders`.
# Explicit variants (api_key, private_key, access_key, x-api-key, x_api_key)
# remain covered by _REDACT_KEYS above.
_REDACT_SUBSTRINGS = ("password", "secret", "token")

# Stdlib LogRecord attributes that must never appear as top-level payload fields.
# G8: `taskName` added for Python 3.12+ (asyncio task name attribute).
_STDLIB_RECORD_FIELDS = frozenset({
    "args", "msg", "levelname", "levelno", "pathname",
    "filename", "module", "exc_info", "exc_text", "stack_info",
    "lineno", "funcName", "created", "msecs", "relativeCreated",
    "thread", "threadName", "processName", "process", "name",
    "message", "taskName",
})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STDLIB_RECORD_FIELDS:
                continue
            # SecF1: redact anything that smells like a credential.
            key_lower = key.lower()
            if key_lower in _REDACT_KEYS or any(sub in key_lower for sub in _REDACT_SUBSTRINGS):
                payload[key] = "[REDACTED]"
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


_configured = False


def configure_logging(
    level: str | None = None,
    stream: Optional[IO[str]] = None,
    output_format: str | None = None,
) -> None:
    """Configure root logging.

    Idempotent: subsequent calls are no-ops. Call once near process start
    (CLI entrypoint, job runner, or test fixture).

    SecF2: When arguments are omitted, defaults come from the `LOG_LEVEL` and
    `LOG_FORMAT` environment variables, then fall back to `INFO` / `json`.

    G4: `output_format` (was `format`, which shadowed the builtin).
    """
    global _configured
    if _configured:
        return
    level = level or os.environ.get("LOG_LEVEL", "INFO")
    output_format = output_format or os.environ.get("LOG_FORMAT", "json")
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(stream or sys.stderr)
    if output_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
    root.addHandler(handler)
    root.setLevel(level)
    _configured = True
