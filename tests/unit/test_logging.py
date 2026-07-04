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
    # text formatter, not JSON -- a stray '{' at line start would indicate JSON.
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


@pytest.mark.parametrize(
    "key_name",
    [
        "signing_key",
        "SIGNING_KEY",
        "signing-key",
        "Signing-Key",
        "hmac_secret",
        "HMAC_SECRET",
        "client_secret",
        "CLIENT_SECRET",
        "oauth_token",
        "OAUTH_TOKEN",
        "id_token",
        "ID_TOKEN",
    ],
)
def test_configure_logging_redacts_round4_secret_names(key_name):
    # SecF1'' (round-4): signing_key / signing-key were left uncovered when the
    # bare "key" substring was dropped in round-3. `hmac_secret`,
    # `client_secret`, `oauth_token`, `id_token` are explicit entries as
    # defense-in-depth against future refactors of `_REDACT_SUBSTRINGS`. All
    # must redact regardless of case.
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream, output_format="json")
    log = logging.getLogger("test.redact.round4")
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
    # Round-4 originally added `foreign_key` and `db_key` here to defend the
    # round-3 substring drop -- the round-7 suffix rule (`endswith("_key")`)
    # now redacts both; that is an accepted trade-off documented in
    # `_REDACT_SUFFIXES`. The negative cases retained here do NOT terminate in
    # a secret-word suffix, so the round-7 pattern leaves them alone. New
    # round-7 negatives (`bucket_size_ms`, `key_bindings`, `agent_name`) live
    # in a dedicated test below.
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


@pytest.mark.parametrize(
    "key_name",
    [
        "session_key",
        "SESSION_KEY",
        "sessionid",
        "SESSIONID",
        "session-id",
        "Session-Id",
        "session_id",
        "SESSION_ID",
        "master_key",
        "MASTER_KEY",
        "master-key",
        "Master-Key",
        "encryption_key",
        "ENCRYPTION_KEY",
        "signing_secret",
        "SIGNING_SECRET",
        "root_key",
        "ROOT_KEY",
    ],
)
def test_configure_logging_redacts_round6_secret_names(key_name):
    # SecF1''' (round-6): session/master key families from Django SESSION_KEY,
    # Rails master.key, HashiCorp Vault, Fernet. Explicit exact-match entries
    # for defense-in-depth alongside the round-7 suffix rule.
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream, output_format="json")
    log = logging.getLogger("test.redact.round6")
    log.info("secret-shape", extra={key_name: "should-not-leak"})
    line = stream.getvalue().strip().splitlines()[-1]
    record = json.loads(line)
    assert record[key_name] == "[REDACTED]", (
        f"expected {key_name!r} to be redacted, got {record.get(key_name)!r}"
    )


@pytest.mark.parametrize(
    "key_name",
    [
        # `_key` / `-key`
        "foo_key",
        "FOO_KEY",
        "bar-key",
        "Bar-Key",
        # `_secret` / `-secret`
        "foo_secret",
        "FOO_SECRET",
        "bar-secret",
        # `_token` / `-token`
        "baz_token",
        "BAZ_TOKEN",
        "qux-token",
        # `_password` / `-password`
        "qux_password",
        "QUX_PASSWORD",
        "svc-password",
        # `_credential(s)` / `-credential(s)`
        "foo-credential",
        "bar_credentials",
        "svc-credentials",
    ],
)
def test_configure_logging_redacts_round7_suffix_pattern(key_name):
    # SecF1'''' (round-7): structural safety net. Any key ending in
    # _key / _secret / _token / _password / _credential(s) (or the hyphenated
    # variant) redacts, even if the exact name has never been reviewed. This
    # breaks the whack-a-mole loop of adding one name per review round.
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream, output_format="json")
    log = logging.getLogger("test.redact.round7.positive")
    log.info("secret-shape", extra={key_name: "should-not-leak"})
    line = stream.getvalue().strip().splitlines()[-1]
    record = json.loads(line)
    assert record[key_name] == "[REDACTED]", (
        f"expected {key_name!r} to be redacted by suffix rule, "
        f"got {record.get(key_name)!r}"
    )


def test_configure_logging_suffix_pattern_passes_through_lookalikes():
    # SecF1'''' (round-7) negatives: keys that CONTAIN a secret word but do
    # NOT terminate in the secret-word suffix must pass through. This is the
    # accepted-trade-off boundary for the `endswith` check: it fires only on
    # trailing suffix, not substring.
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream, output_format="json")
    log = logging.getLogger("test.redact.round7.negative")
    log.info(
        "innocent-extras",
        extra={
            # `_token` is a substring but the field ends in `_size`.
            # NOTE: `token` is on _REDACT_SUBSTRINGS so this ONE would still
            # redact via the substring rule; renamed to `bucket_size_ms` to
            # actually exercise the suffix-only negative case.
            "bucket_size_ms": 250,
            # `key_bindings` starts with `key` but ends in `_bindings`.
            "key_bindings": "cmd+k",
            # ends in `_name`, not a secret suffix.
            "agent_name": "ingester",
        },
    )
    line = stream.getvalue().strip().splitlines()[-1]
    record = json.loads(line)
    assert record["bucket_size_ms"] == 250
    assert record["key_bindings"] == "cmd+k"
    assert record["agent_name"] == "ingester"


@pytest.mark.parametrize(
    "key_name",
    [
        # camelCase `_key` family — bypassed round-7 suffix rule pre-round-9
        # because `endswith("_key")` requires the underscore separator.
        "sessionKey",
        "masterKey",
        "privateKey",
        "accessKey",
        "signingKey",
        "encryptionKey",
        "rootKey",
        "sshKey",
        "licenseKey",
        # PascalCase equivalents — same class of risk.
        "SessionKey",
        "MasterKey",
        # camelCase `_token` / `_secret` / `_password` — already caught by
        # substring rule pre-round-9 (`token` / `secret` / `password` are on
        # `_REDACT_SUBSTRINGS`), but lock the behavior via the normalizer path
        # so future refactors of the substring set do not silently regress.
        "refreshToken",
        "clientSecret",
        "dbPassword",
    ],
)
def test_configure_logging_redacts_camelcase_secret_names(key_name):
    # SecF1''''' (round-9): camelCase / PascalCase secret names must redact
    # after being normalized to snake_case. Closes the `_key` asymmetry gap
    # surfaced by the round-8 P9 security review.
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream, output_format="json")
    log = logging.getLogger("test.redact.round9")
    log.info("secret-shape", extra={key_name: "should-not-leak"})
    line = stream.getvalue().strip().splitlines()[-1]
    record = json.loads(line)
    assert record[key_name] == "[REDACTED]", (
        f"expected {key_name!r} to be redacted after camelCase normalization, "
        f"got {record.get(key_name)!r}"
    )


def test_configure_logging_camelcase_lookalikes_pass_through():
    # SecF1''''' (round-9) negatives: camelCase / PascalCase names that
    # normalize to snake_case forms which do NOT match any redaction rule
    # must pass through with their original values. Guards against the
    # normalizer over-redacting benign fields.
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream, output_format="json")
    log = logging.getLogger("test.redact.round9.negative")
    log.info(
        "innocent-extras",
        extra={
            # `keyword` -> `keyword`; no `_key` suffix, no secret substring.
            "keyword": "gdpr",
            # `Keyword` -> `keyword`; PascalCase normalizes but still safe.
            "Keyword": "gdpr",
            # `stakeholder` -> `stakeholder`; safe.
            "stakeholder": "legal-team",
            # `Stakeholders` -> `stakeholders`; safe.
            "Stakeholders": "legal-team",
        },
    )
    line = stream.getvalue().strip().splitlines()[-1]
    record = json.loads(line)
    assert record["keyword"] == "gdpr"
    assert record["Keyword"] == "gdpr"
    assert record["stakeholder"] == "legal-team"
    assert record["Stakeholders"] == "legal-team"


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
