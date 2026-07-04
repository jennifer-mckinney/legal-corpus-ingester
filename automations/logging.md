# Structured Logging

The ingester emits structured logs so pipeline runs can be queried later without
having to parse ad-hoc strings per module. Defaults lean toward machine-readable
output; a human-readable mode is available when tailing a terminal.

## Defaults

- Structured JSON to `stderr` by default
- Set `LOG_FORMAT=text` when a human is watching (single line per record, no JSON)
- `LOG_LEVEL` env var controls verbosity: `DEBUG` / `INFO` / `WARNING` / `ERROR`
- Every module should acquire its logger via `logging.getLogger(__name__)` so
  records carry a module-scoped `logger` name

## Environment variables

`configure_logging` reads defaults from the environment when arguments are
omitted:

| Var          | Purpose                        | Default |
|--------------|--------------------------------|---------|
| `LOG_LEVEL`  | Root logger verbosity          | `INFO`  |
| `LOG_FORMAT` | `json` (structured) or `text`  | `json`  |

Explicit arguments to `configure_logging` always win over environment values.

## Idempotency

`configure_logging` is idempotent: the first call installs the root handler and
sets the level; subsequent calls are no-ops. This is safe to invoke from any
entrypoint (CLI, job runner, test fixture) without worrying about handler churn
or double formatting.

## Usage

```python
import logging
from legal_corpus_ingester.utils.logging import configure_logging

configure_logging(level="INFO", output_format="json")
log = logging.getLogger(__name__)

log.info(
    "chunked source",
    extra={"source": "eurlex", "stage": "chunk", "chunk_count": 128},
)
```

`configure_logging` should be called once near process start (CLI entrypoint,
job runner, or test fixture). A second call in the same process is a no-op;
reset the module `_configured` flag only in tests where reconfiguration is the
subject under test.

## Consistent LogRecord fields

To keep queries stable across sources and stages, attach these fields via
`extra=` whenever they apply:

| Field | Meaning |
|-------|---------|
| `source` | Source name (e.g. `eurlex`, `ccpa`, `pipeda`) |
| `stage` | Pipeline phase: `fetch` / `clean` / `chunk` / `embed` / `publish` |
| `chunk_count` | Number of chunks produced or processed in this record |
| `bytes_fetched` | Byte count of the payload retrieved |
| `run_id` | Correlation id for a single ingester run |
| `elapsed_ms` | Wall-clock duration of the operation being logged |

Adding a new field is fine; the JSON formatter serializes every non-standard
attribute on the record. Reusing the field names above lets downstream queries
work without a schema-per-module.

## Secret redaction (extras deny-list)

To prevent credentials from leaking into aggregated logs, the JSON formatter
redacts any `extra=` key whose lower-cased name matches the deny-list or
contains a sensitive substring. The value in the emitted record becomes the
literal string `"[REDACTED]"`.

**Redacted (case-insensitive) exact matches:**

`password`, `passwd`, `secret`, `token`, `authorization`, `api_key`, `apikey`,
`access_key`, `private_key`, `cookie`, `set-cookie`, `session`, `bearer`,
`email`, `auth`, `credentials`

**Redacted (case-insensitive) substrings anywhere in the key:**

`password`, `secret`, `token`, `key`. That means `user_api_key`, `auth_token`,
`refresh_secret`, and `signing_key` are all redacted.

Fields listed in the "Consistent LogRecord fields" table above are safe: none
of them match the deny-list. If a new operational field could collide with the
substring rule (for example a key literally named `key`), rename it or embed
the value inside a non-matching key like `identifier`.

## Rotation

Log rotation is intentionally NOT handled inside the application. Delegate to
the host:

- macOS: launchd log rotation via `StandardOutPath` / `StandardErrorPath` and
  a companion `newsyslog.conf` entry
- Linux: `logrotate` with a config under `/etc/logrotate.d/`

This keeps the ingester focused on emitting records and leaves retention policy
to the operator.
