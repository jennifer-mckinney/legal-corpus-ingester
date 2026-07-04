# tests/unit/test_logging.py
from __future__ import annotations
import json
import logging
import io
import os

import pytest

from legal_corpus_ingester.utils import logging as log_module
from legal_corpus_ingester.utils.logging import configure_logging


@pytest.fixture(autouse=True)
def _reset_logging_state(monkeypatch):
    # configure_logging is idempotent; reset the module flag + root handlers so
    # each test observes a clean configuration path.
    log_module._configured = False
    root = logging.getLogger()
    prior_handlers = root.handlers[:]
    prior_level = root.level
    root.handlers.clear()
    # Scrub env vars so tests are deterministic unless they set them.
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    yield
    log_module._configured = False
    root.handlers.clear()
    for h in prior_handlers:
        root.addHandler(h)
    root.setLevel(prior_level)


def test_configure_logging_emits_json():
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream, output_format="json")
    log = logging.getLogger("test.mod")
    log.info("hello", extra={"source": "eurlex", "chunk_count": 42})
    line = stream.getvalue().strip().splitlines()[-1]
    record = json.loads(line)
    assert record["level"] == "INFO"
    assert record["message"] == "hello"
    assert record["logger"] == "test.mod"
    assert record["source"] == "eurlex"
    assert record["chunk_count"] == 42
    assert "timestamp" in record


def test_configure_logging_default_text():
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream, output_format="text")
    log = logging.getLogger("test.mod")
    log.info("hello")
    line = stream.getvalue().strip().splitlines()[-1]
    assert "INFO" in line and "hello" in line


def test_configure_logging_redacts_sensitive_extras():
    # SecF1: keys resembling credentials must be redacted in emitted JSON.
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream, output_format="json")
    log = logging.getLogger("test.redact")
    log.info(
        "sensitive",
        extra={
            "api_key": "sk-abc",
            "password": "hunter2",
            "auth_token": "bearer-xyz",
            "email": "alice@example.com",
            "SECRET": "shh",
            "source": "eurlex",  # non-sensitive should pass through
        },
    )
    line = stream.getvalue().strip().splitlines()[-1]
    record = json.loads(line)
    assert record["api_key"] == "[REDACTED]"
    assert record["password"] == "[REDACTED]"
    assert record["auth_token"] == "[REDACTED]"
    assert record["email"] == "[REDACTED]"
    assert record["SECRET"] == "[REDACTED]"
    assert record["source"] == "eurlex"


def test_configure_logging_defaults_no_args_no_env():
    # SecF2: no args + no env => INFO level, json format.
    stream = io.StringIO()
    configure_logging(stream=stream)
    root = logging.getLogger()
    assert root.level == logging.INFO
    log = logging.getLogger("test.defaults")
    log.info("hi")
    line = stream.getvalue().strip().splitlines()[-1]
    # JSON default => parseable payload with a level field.
    record = json.loads(line)
    assert record["level"] == "INFO"
    assert record["message"] == "hi"


def test_configure_logging_reads_env_defaults(monkeypatch):
    # SecF2: LOG_LEVEL / LOG_FORMAT env vars supply defaults when args omitted.
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LOG_FORMAT", "text")
    stream = io.StringIO()
    configure_logging(stream=stream)
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    log = logging.getLogger("test.env")
    log.debug("verbose")
    out = stream.getvalue()
    # text formatter, not JSON — a stray '{' at line start would indicate JSON.
    assert "verbose" in out
    assert not out.strip().startswith("{")


@pytest.mark.parametrize(
    "key_name",
    [
        "JWT",
        "jwt",
        "pat",
        "PAT",
        "x-api-key",
        "X-API-KEY",
        "refresh_token",
        "REFRESH_TOKEN",
        "private_token",
        "personal_access_token",
        "access_token",
        "x_api_key",
    ],
)
def test_configure_logging_redacts_round2_secret_names(key_name):
    # SecF1' (round-2): shorthand secret key names must be redacted regardless
    # of case. Redaction path normalizes via .lower() before matching.
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream, output_format="json")
    log = logging.getLogger("test.redact.round2")
    log.info("secret-shape", extra={key_name: "should-not-leak"})
    line = stream.getvalue().strip().splitlines()[-1]
    record = json.loads(line)
    assert record[key_name] == "[REDACTED]", (
        f"expected {key_name!r} to be redacted, got {record.get(key_name)!r}"
    )


def test_configure_logging_passes_through_key_lookalikes():
    # G2' (round-2): bare "key" was dropped from _REDACT_SUBSTRINGS because it
    # over-matched debuggability-critical extras. These MUST pass through with
    # their original values.
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream, output_format="json")
    log = logging.getLogger("test.redact.negatives")
    log.info(
        "innocent-extras",
        extra={
            "keyword": "gdpr",
            "cache_key_prefix": "eurlex:v2",
            "stakeholders": "legal-team",
        },
    )
    line = stream.getvalue().strip().splitlines()[-1]
    record = json.loads(line)
    assert record["keyword"] == "gdpr"
    assert record["cache_key_prefix"] == "eurlex:v2"
    assert record["stakeholders"] == "legal-team"


def test_configure_logging_is_idempotent():
    # G1 + SecF2: repeated calls are no-ops; second call must not reset handlers.
    stream_a = io.StringIO()
    configure_logging(level="INFO", stream=stream_a, output_format="json")
    root = logging.getLogger()
    handler_after_first = root.handlers[:]

    stream_b = io.StringIO()
    configure_logging(level="DEBUG", stream=stream_b, output_format="text")
    # No handler swap; the second call should be a no-op.
    assert root.handlers == handler_after_first
    assert root.level == logging.INFO

    log = logging.getLogger("test.idem")
    log.info("still-json")
    # Output goes to the original stream, still JSON.
    assert stream_b.getvalue() == ""
    line = stream_a.getvalue().strip().splitlines()[-1]
    record = json.loads(line)
    assert record["message"] == "still-json"
