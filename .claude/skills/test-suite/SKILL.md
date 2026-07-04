---
name: test-suite
description: Run the pytest test suite with coverage analysis. Use when asked to run tests, check coverage, analyze test failures, or verify changes pass. Triggers on "run tests", "check coverage", "test this", or after writing new test code.
allowed-tools: Bash, Read, Grep, Glob
---

# Test Suite Runner

## Workflow

1. **Run tests with coverage**
   ```bash
   pytest --cov=src/legal_corpus_ingester --cov-report=term-missing -v 2>&1
   ```

2. **Analyze results**
   - Parse exit code: 0 = all pass, 1 = failures, 2 = errors, 5 = no tests collected
   - For failures: read the traceback, identify root cause, suggest fix
   - For errors: check imports, missing fixtures, async issues

3. **Report coverage gaps**
   - Extract `TOTAL` line from coverage report
   - Identify files with < 50% coverage
   - Cross-reference with @.claude/library/LIB-TEST.md priority list and coverage gates

4. **Output format**
   Report results as:
   ```
   ## Test Results
   | Metric | Value |
   |--------|-------|
   | Tests passed | X/Y |
   | Coverage | Z% |
   | Failures | list... |

   ## Coverage Gaps (top 5)
   | File | Coverage | Priority |
   |------|----------|----------|
   ```

## Test layers

| Layer | Location | Command |
|-------|----------|---------|
| Unit | `tests/unit/` | `pytest tests/unit/ -v` |
| Integration | `tests/integration/` | `pytest tests/integration/ -v` |
| E2E / round-trip | `tests/e2e/` | `pytest tests/e2e/ -v` |
| Snapshot (chunker) | `tests/snapshot/` | `pytest tests/snapshot/ -v` |
| CLI | `tests/cli/` | `pytest tests/cli/ -v` |

## If tests fail
- Read the failing test file and the source module it tests
- Check for common issues: missing fixtures, wrong mock paths, async without `asyncio.run()`
- Suggest a specific fix — do not just re-run the same test

## Arguments
- `$ARGUMENTS` can specify a test file or pattern: `pytest -k "$ARGUMENTS"`
- No arguments = run full suite
