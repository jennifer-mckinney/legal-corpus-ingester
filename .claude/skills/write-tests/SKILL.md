---
name: write-tests
description: Guided workflow for writing comprehensive tests for a pipeline module. Use when asked to "write tests for X", "add test coverage for X", "test the X stage", or when improving test coverage for a specific module. Accepts a module name as argument.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Write Tests Workflow

## Phase 1: Understand the Target

1. **Identify the module** from `$ARGUMENTS` (e.g., "fetchers/http_fetcher", "cleaners/html_cleaner", "chunkers/section_chunker", "embedders/localai_client", "publishers/filesystem", "pipeline/orchestrator", "provenance/tracker")
2. **Read the source file**: `src/legal_corpus_ingester/$ARGUMENTS.py`
3. **Read existing tests**: check `tests/unit/$ARGUMENTS/test_*.py` or `tests/integration/$ARGUMENTS/` if they exist
4. **Read the coverage gap analysis**: @.claude/library/LIB-TEST.md — find the module's layer and fixture strategy

## Phase 2: Plan Test Cases

For each public function in the module, plan:

| Function | Happy Path | Edge Cases | Error Cases |
|----------|-----------|------------|-------------|
| (fill in) | (fill in) | (fill in) | (fill in) |

Use `@pytest.mark.parametrize` when a function has 3+ test scenarios (see T4 in .claude/rules/testing.md).

## Phase 3: Write Tests

### File Structure
```python
from __future__ import annotations

import pytest
# ... imports

# === Fixtures ===

# === Tests for function_name ===

class TestFunctionName:
    def test_happy_path(self):
        ...
    def test_edge_case(self):
        ...
    @pytest.mark.parametrize("input,expected", [...])
    def test_variations(self, input, expected):
        ...
```

### Rules
- **IMPORTANT**: Use `from __future__ import annotations` in every test file
- **IMPORTANT**: Mock external dependencies (LocalAI, httpx, filesystem writes) — never call real services
- **IMPORTANT**: Use `asyncio.run(...)` inside a regular (non-`async def`) test function. Do NOT use `@pytest.mark.asyncio` — see .claude/rules/testing.md T1 for why (marker silently no-ops as PytestUnknownMarkWarning).
- Use descriptive test names: `test_<module>_<function>_<scenario>` (T9)
- One assertion per test when possible
- Use fixtures from `conftest.py` for `mock_localai_client`, `sample_corpus`, `tmp_bundle_dir`
- Check `conftest.py` exists at the appropriate level — if not, create it first with shared fixtures

### Key Fixture Patterns
```python
# conftest.py essentials:
@pytest.fixture
def mock_localai_client():
    from unittest.mock import MagicMock
    client = MagicMock()
    # Returns deterministic 384-dim zero vectors
    client.embed.return_value = [0.0] * 384
    return client

@pytest.fixture
def sample_corpus(tmp_path):
    # Load from tests/fixtures/corpus/ text files as CleanedDocument objects
    ...

@pytest.fixture
def tmp_bundle_dir(tmp_path):
    # Per-test isolated output directory — never write to out/ in tests (T7)
    return tmp_path / "bundle"
```

### VCR Pattern (HTTP fetcher tests)
```python
import vcr

@vcr.use_cassette("tests/fixtures/cassettes/eurlex/fetch_gdpr.yaml")
def test_fetcher_fetch_eurlex_returns_content():
    import asyncio
    from legal_corpus_ingester.fetchers.http_fetcher import HttpFetcher
    result = asyncio.run(HttpFetcher().fetch("eurlex"))
    assert result.content != ""
```

### CLI Pattern (Typer CliRunner — no app.dependency_overrides)
```python
from typer.testing import CliRunner
from legal_corpus_ingester.cli import app

runner = CliRunner()

def test_cli_fetch_dry_run_exits_zero():
    result = runner.invoke(app, ["fetch", "eurlex", "--dry-run"])
    assert result.exit_code == 0
```

## Phase 4: Verify

1. Run the new tests: `pytest tests/unit/<module_path>/ -v`
2. If failures, fix them immediately
3. Run full suite to check for regressions: `pytest --cov=src/legal_corpus_ingester --cov-report=term-missing`
4. Report coverage delta

## Arguments
- `$ARGUMENTS`: module path relative to `src/legal_corpus_ingester/` (e.g., "fetchers/http_fetcher", "cleaners/html_cleaner")
- If no argument given, read @.claude/library/LIB-TEST.md and pick the highest-priority untested module
