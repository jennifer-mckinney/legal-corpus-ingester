# LIB-TEST — test architecture, coverage gates, fixture strategy
loads: on-trigger
scope: project
xref: [[LIB-STACK]] [[LIB-ARCH]] [[.claude/rules/testing.md]] [[.claude/CLAUDE.md]]

## test-layers

| Layer | Framework | Location | When run |
|-------|-----------|----------|----------|
| Unit | pytest | `tests/unit/` | Every task |
| Integration (mocked upstream) | pytest + VCR cassettes | `tests/integration/` | Per stage complete |
| Contract (round-trip with terms-analysis) | pytest + dynamic import | `tests/e2e/` | Per phase complete |
| Snapshot (chunker parity) | syrupy | `tests/snapshot/` | On chunker change |
| CLI smoke | Typer `CliRunner` | `tests/cli/` | Per phase complete |
| Weekly drift canary | pytest + VCR record=all | CI schedule | Weekly on self-hosted runner |

## coverage-gates

| Phase | Line | Branch | Enforcement |
|-------|------|--------|-------------|
| Phase 0 exit | ≥80% | ≥75% | `pyproject.toml` `[tool.coverage.report]` fail_under |
| Each new module (pre-merge) | ≥90% | — | Reviewer checklist + P9 loop |

Round-trip test MUST cover the full pipeline end-to-end:
```
parse → chunk → embed → serialize → deserialize → terms-analysis retrieve → assert non-empty result
```

## fixture-strategy

### VCR cassettes
- Location: `tests/fixtures/cassettes/<source_id>/` (e.g., `cassettes/eurlex/`, `cassettes/lii/`)
- Recorded once in development with `record_mode='new_episodes'`
- CI uses `record_mode='none'` — hard failure if cassette missing
- Weekly drift canary re-records with `--vcr-record=all` against live upstream (see ADR-013)

### Sample corpus
- Location: `tests/fixtures/corpus/`
- One `.txt` file per parseable variant:
  - `plain_body.txt` — body text, no section markers
  - `sectioned_body.txt` — statute with section/article headers
  - `placeholder_header.txt` — document with PLACEHOLDER header (cleaner must not emit placeholder text)
  - `multi_metadata.txt` — document with multiple metadata fields (jurisdiction, instrument, revision_date)
- Used by unit tests for Cleaner + Chunker; does NOT require network

### Chunker snapshots
- Location: `tests/fixtures/snapshots/`
- Byte-exact expected output per sample corpus fixture
- Managed by syrupy; regenerate with `pytest --snapshot-update`
- Parity check: snapshot output must match terms-analysis chunker output for the same input (ADR-003)

### Mock LocalAI client
- `conftest.py` provides `mock_localai_client` fixture via `unittest.mock.MagicMock`
- Returns deterministic 384-dim zero vectors for embedding calls
- Never calls real LocalAI server in any test (T5)

### Reference terms-analysis
- Installed as dev dependency for E2E imports (path dependency in pyproject.toml dev group)
- E2E tests call `legal_kb.py::retrieve()` with the published corpus bundle to assert non-empty result

## test-naming-convention

Pattern: `test_<module>_<function>_<scenario>`

Examples:
- `test_fetcher_fetch_eurlex_200_returns_fetch_result`
- `test_fetcher_fetch_404_halts_source`
- `test_cleaner_clean_html_strips_boilerplate`
- `test_chunker_chunk_sectioned_matches_snapshot`
- `test_embedder_embed_wrong_dimension_raises_embed_dimension_error`
- `test_publisher_publish_atomic_rename_succeeds`
- `test_provenance_record_writes_all_fields`
- `test_pipeline_run_full_produces_manifest`

## testing-rules

See `.claude/rules/testing.md` for the full T1-T9 rule set. Summary:

- **T1**: No `@pytest.mark.asyncio` — use `asyncio.run(...)` inside sync tests
- **T2**: Tests mirror source structure under `tests/` subfolder (unit/integration/etc.)
- **T3**: Shared fixtures in `conftest.py` (VCR session, sample corpus, mock LocalAI client)
- **T4**: `@pytest.mark.parametrize` for multi-case coverage
- **T5**: Mock `embedders/localai_client.py`; never call real LocalAI in tests
- **T6**: No `respx` — patch `httpx.AsyncClient` with `unittest.mock` or inject `httpx.MockTransport`
- **T7**: Use `tmp_path` fixture for output bundle isolation (temp directory per test)
- **T8**: VCR `record_mode='new_episodes'` in dev; `record_mode='none'` in CI

## weekly-drift-canary

ADR-013.

```yaml
# CI schedule
cron: "0 4 * * 0"   # Sunday 4am
```

Run: `pytest tests/integration/ --vcr-record=all`

Behavior:
- Re-records all VCR cassettes against live upstream sources
- On schema change: test fails, diff is shown in CI output, `ALERTS.md` updated
- On HTTP non-200: test fails with source ID + status in error message
- Cassette diff committed as a PR for human review before merging

Rationale: Upstream legal corpora (EUR-Lex, LII) may change HTML structure or endpoints without notice.
Canary surfaces this within one week rather than at next manual ingest run.

## phase-0-test-checklist

Tasks that must have tests before Phase 0 exit:

| Task | Test scope |
|------|-----------|
| Fetchers (HTTP + PDF + local) | Unit + VCR integration |
| Cleaners (HTML + PDF + XML + plain) | Unit with sample corpus fixtures |
| Chunkers (section-aware + plain) | Unit + syrupy snapshot |
| Embedder (LocalAI client) | Unit with mock LocalAI |
| Publisher (filesystem) | Unit with `tmp_path` |
| ProvenanceTracker | Unit |
| Pipeline orchestrator | Integration (mocked all stages) |
| CLI entrypoint | CLI smoke via Typer `CliRunner` |
| Round-trip E2E | E2E once terms-analysis integration lands |
