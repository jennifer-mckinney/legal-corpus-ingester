# Structured Logging

The ingester emits structured logs so pipeline runs can be queried later without
having to parse ad-hoc strings per module. Defaults lean toward machine-readable
output; a human-readable mode is available when tailing a terminal.

## Defaults

- Structured JSON to `stderr` by default
- Set `LOG_FORMAT=text` when a human is watching (single line per record, no JSON)
- `LOG_LEVEL` env var controls verbosity: `DEBUG` / `INFO` / `WARNING` / `ERROR`
- Every module should acquire its logger via `get_logger(__name__)` so records
  carry a module-scoped `logger` name

## Usage

```python
from legal_corpus_ingester.utils.logging import configure_logging, get_logger

configure_logging(level="INFO", format="json")
log = get_logger(__name__)

log.info(
    "chunked source",
    extra={"source": "eurlex", "stage": "chunk", "chunk_count": 128},
)
```

`configure_logging` should be called once near process start (CLI entrypoint,
job runner, or test fixture). Subsequent calls reset root handlers, which
is intentional: a second call in the same process is a reconfigure, not an
additive change.

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

## Rotation

Log rotation is intentionally NOT handled inside the application. Delegate to
the host:

- macOS: launchd log rotation via `StandardOutPath` / `StandardErrorPath` and
  a companion `newsyslog.conf` entry
- Linux: `logrotate` with a config under `/etc/logrotate.d/`

This keeps the ingester focused on emitting records and leaves retention policy
to the operator.
