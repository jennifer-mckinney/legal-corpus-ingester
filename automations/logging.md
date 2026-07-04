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
redacts any `extra=` key whose **normalized** name (camelCase / PascalCase
split into snake_case, then lowercased) matches the deny-list, contains a
sensitive substring, or ends with a sensitive suffix. The value in the emitted
record becomes the literal string `"[REDACTED]"`.

### camelCase / PascalCase normalization (round-9)

Every incoming key name is first normalized to a canonical snake_case form
before the three redaction checks fire:

| Input             | Normalized form   |
|-------------------|-------------------|
| `sessionKey`      | `session_key`     |
| `MasterKey`       | `master_key`      |
| `APIKey`          | `api_key`         |
| `HTTPServer`      | `http_server`     |
| `SESSION_KEY`     | `session_key`     |
| `already_snake`   | `already_snake`   |
| `kebab-case-key`  | `kebab-case-key`  |

This closes the `_key` asymmetry gap. Pre-round-9, `endswith("_key")` required
the underscore separator, so camelCase names like `sessionKey`, `masterKey`,
`privateKey`, and `accessKey` bypassed all three checks. With normalization,
both `access_key` and `accessKey` redact via the same code path.

The `_token` / `_secret` / `_password` families were already caught by the
substring rule (`token` / `secret` / `password` are on `_REDACT_SUBSTRINGS`),
so `refreshToken`, `clientSecret`, and `dbPassword` were never at risk; the
normalizer just makes their behavior symmetric with the `_key` family.

**Redacted (case-insensitive) exact matches:**

`password`, `passwd`, `secret`, `token`, `authorization`, `api_key`, `apikey`,
`access_key`, `private_key`, `cookie`, `set-cookie`, `session`, `bearer`,
`email`, `auth`, `credentials`, `jwt`, `pat`, `personal_access_token`,
`access_token`, `refresh_token`, `private_token`, `x-api-key`, `x_api_key`,
`signing_key`, `signing-key`, `hmac_secret`, `client_secret`, `oauth_token`,
`id_token`, `session_key`, `sessionid`, `session-id`, `session_id`,
`master_key`, `master-key`, `encryption_key`, `signing_secret`, `root_key`

**Redacted (case-insensitive) substrings anywhere in the key:**

`password`, `secret`, `token`. That means `user_password`, `auth_token`, and
`refresh_secret` are all redacted. The bare `key` substring was intentionally
removed because it over-matched debuggability-critical extras like `keyword`,
`cache_key_prefix`, and `stakeholders`; explicit `*_key` secret names
(`api_key`, `private_key`, `access_key`, `signing_key`, `signing-key`,
`x-api-key`, `x_api_key`) stay covered via the exact-match set above, and the
suffix rule below now catches every other `<thing>_key` variant.

**Redacted (case-insensitive) suffix pattern (round-7 structural safety net):**

Any key whose lower-cased name ENDS WITH one of the following suffixes is
redacted, regardless of whether the exact name has ever been reviewed:

`_key`, `-key`, `_secret`, `-secret`, `_token`, `-token`, `_password`,
`-password`, `_credential`, `-credential`, `_credentials`, `-credentials`

This exists to break the whack-a-mole loop where each successive review round
would surface a NEW secret name following the standard `<thing>_<credential>`
naming convention (`session_key`, `master_key`, `signing_secret`,
`refresh_token`, `db_password`). The suffix check auto-covers any future name
that follows the convention, so reviewers no longer have to catch each new
variant by hand.

**Trade-off:** `endswith` may over-redact if an operational metric ever
terminates in one of the above suffixes. In practice, operational metrics end
in `_count` / `_size` / `_ms` / `_bytes`, none of which appear above. Note
that `foreign_key` and `db_key` are now redacted; that is accepted --
callers who need those debuggability fields should rename them (e.g.
`foreign_ref`, `db_partition`). With round-9 camelCase normalization,
`foreignKey` (camelCase) also redacts -- same accepted trade-off. No
allow-list is added preemptively (YAGNI); if a real collision surfaces later,
revisit at that time.

Fields listed in the "Consistent LogRecord fields" table above are safe: none
of them match the deny-list or suffix rule. If a new operational field is a
real credential and does NOT already match the suffix pattern or an existing
exact-match / substring entry, add it to `_REDACT_KEYS` in
`src/legal_corpus_ingester/utils/logging.py` and cover it with a redaction
test.

## Rotation

Log rotation is intentionally NOT handled inside the application. Delegate to
the host:

- macOS: launchd log rotation via `StandardOutPath` / `StandardErrorPath` and
  a companion `newsyslog.conf` entry
- Linux: `logrotate` with a config under `/etc/logrotate.d/`

This keeps the ingester focused on emitting records and leaves retention policy
to the operator.
