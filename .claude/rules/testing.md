---
paths:
  - "tests/**/*.py"
---

# testing — pytest conventions
loads: on-trigger
scope: project
xref: [[LIB-TEST]] [[LIB-STACK]] [[.claude/CLAUDE.md]]

## pytest-conventions

### T1: no pytest-asyncio
rule: do not use `@pytest.mark.asyncio`
apply_when: any async function under test
alternative: call from a regular (non-`async def`) test via `asyncio.run(...)`
because: `pytest-asyncio` is not in the dependency list; marker silently no-ops as `PytestUnknownMarkWarning`
xref: [[LIB-TEST#testing-rules]]

### T2: test location
rule: place tests in `tests/` mirroring source structure under matching subfolder
examples:
- `src/legal_corpus_ingester/fetchers/http_fetcher.py` → `tests/unit/fetchers/test_http_fetcher.py`
- `src/legal_corpus_ingester/cleaners/html_cleaner.py` → `tests/unit/cleaners/test_html_cleaner.py`
- integration tests → `tests/integration/<stage>/`
- E2E tests → `tests/e2e/`
- snapshot tests → `tests/snapshot/`
- CLI tests → `tests/cli/`

### T3: shared fixtures
rule: shared fixtures in `conftest.py` (VCR session, sample corpus, mock LocalAI client)
key fixtures:
- `mock_localai_client` — `MagicMock` returning deterministic 384-dim zero vectors
- `sample_corpus` — loads `tests/fixtures/corpus/` text files as `CleanedDocument` objects
- `vcr_config` — VCR cassette directory + record mode (dev=`new_episodes`, CI=`none`)
- `tmp_bundle_dir` — `tmp_path`-backed output directory for publisher isolation

### T4: parametrize multi-case
rule: use `@pytest.mark.parametrize` for multi-case coverage (source IDs, content types, license types, error conditions)

### T5: mock LocalAI
rule: mock `embedders/localai_client.py::LocalAIEmbedder` with `unittest.mock` / hand-written fakes; never call real LocalAI in tests
because: LocalAI is an external server dependency; tests must be hermetic

### T6: no respx
rule: do not use `respx`
alternative: patch `httpx.AsyncClient` with `monkeypatch` to inject `httpx.MockTransport`, or use `unittest.mock.patch`
because: `respx` is not in the dependency list
xref: [[LIB-STACK#planned-dependencies]]

### T7: temp bundle directory
rule: use `tmp_path` fixture for output bundle isolation; never write to `out/` in tests
because: `tmp_path` is per-test scoped and cleaned up automatically; `out/` is the production artifact directory

### T8: CLI tests via CliRunner
rule: use Typer `CliRunner` for CLI entrypoint tests
because: `CliRunner` invokes Typer apps in-process without spawning a subprocess
example:
```python
from typer.testing import CliRunner
from legal_corpus_ingester.cli import app

runner = CliRunner()
result = runner.invoke(app, ["run", "--dry-run"])
assert result.exit_code == 0
```

### T9: test naming
rule: `test_<module>_<function>_<scenario>`
examples:
- `test_fetcher_fetch_eurlex_404_halts_source`
- `test_cleaner_clean_html_strips_boilerplate`
- `test_chunker_chunk_sectioned_matches_snapshot`
- `test_embedder_embed_wrong_dimension_raises`
- `test_publisher_publish_atomic_rename_succeeds`
- `test_pipeline_run_full_produces_manifest`

## quality-gates

| metric | target | notes |
|--------|--------|-------|
| line coverage | ≥80% | Phase 0 exit floor; enforced via `pyproject.toml` fail_under |
| branch coverage | ≥75% | Phase 0 exit floor |
| new module pre-merge | ≥90% line | Reviewer checklist |
| round-trip E2E | must pass | parse→chunk→embed→serialize→deserialize→retrieve→non-empty result |

## vcr-cassette-policy

- Dev (local): `record_mode='new_episodes'` — records missing cassettes, replays existing
- CI: `record_mode='none'` — hard failure if cassette missing (no network in CI)
- Weekly drift canary: `--record-mode=rewrite` — deletes then re-records all cassettes against live upstream (CI cron `0 4 * * 0`)
- Cassette directory: `tests/fixtures/cassettes/<source_id>/`
- On schema change: test fails, diff shown, `ALERTS.md` updated; cassette diff committed as PR for review

## 3-rule-drift-policy

No Pydantic `Literal` allowlists exist in the ingester yet (pipeline config uses free-form YAML + Pydantic BaseModel).

When allowlists are added (e.g., a `Literal["eurlex", "lii", ...]` source-ID type), apply the R1-R3
pattern from terms-analysis `.claude/rules/testing.md`:
- R1: derive handler-level allowlists from `typing.get_args(TheLiteral)`, not hardcoded
- R2: cross-endpoint field parity test
- R3: runtime enumeration over Literal in parametrize (never hardcode the list)
