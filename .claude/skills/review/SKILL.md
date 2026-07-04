---
name: review
description: Review code changes for quality, conventions, and correctness. Use when asked to "review this", "check my changes", "review PR", or before committing to verify code quality. Also use proactively after writing significant code.
allowed-tools: Read, Bash, Grep, Glob
---

# Code Review

## Workflow

1. **Identify changes**
   ```bash
   git diff --stat
   git diff --name-only
   ```

2. **Read each changed file** and check against:

### Python Pipeline Checklist

| Check | Rule |
|-------|------|
| Type hints | All function signatures must have type hints (PY2) |
| `from __future__ import annotations` | Required in every module (PY6) |
| Import order | `__future__` → stdlib → third-party → local (PY4) |
| Async I/O | Any HTTP or LocalAI embed call must be async (PY5) |
| Pydantic models | Config and pipeline request/response shapes use Pydantic v2 (PY3) |
| ruff-compliant | Code passes `ruff check`; no suppression without inline comment (PY7) |
| mypy-strict | No `Any` in public interfaces; public functions annotated (PY8) |
| Error handling | 404 / schema change / SPDX drift halts source and emits ALERTS.md (HR5) |
| No external calls | All data stays local; no OpenAI, no cloud embedding APIs (HR4 / HR6) |
| Typer CLI patterns | Commands use Typer decorators; no argparse; options via `typer.Option()` |

### Test Code Checklist

| Check | Rule |
|-------|------|
| No real services | LocalAI, httpx, filesystem writes are mocked (T5) |
| Async tests | Use `asyncio.run(...)` from a regular (non-`async def`) test function; do NOT use `@pytest.mark.asyncio` (T1) |
| Descriptive names | `test_<module>_<function>_<scenario>` pattern (T9) |
| Assertions | Clear, specific assertions — not just `assert result` |
| Edge cases | Empty input, boundary values, error paths covered |
| Fixtures | Shared fixtures in `conftest.py`, not duplicated (T3) |
| CLI tests | Use Typer `CliRunner` — no subprocess spawning (T8) |
| VCR cassettes | HTTP tests use cassettes; CI `record_mode='none'` enforced (T6 + vcr-cassette-policy) |
| No writes to `out/` | Tests use `tmp_path`-backed `tmp_bundle_dir` fixture (T7) |

### Security Checklist

| Check | Rule |
|-------|------|
| No hardcoded secrets | No API keys, tokens, or credentials in source |
| Path traversal | Validate all user-supplied paths; use `pathlib.Path.resolve()` |
| Command injection | No `subprocess` with user-controlled strings |
| SPDX drift handling | SPDX change on any source blocks publish — check HR8 code path |
| Temp file cleanup | `tmp_path` fixtures clean up; no orphan files in `out/` |
| Redirect following | HTTP fetchers must not follow redirects to unexpected domains |

3. **Report findings** as:
   ```
   ## Review Summary
   | File | Issues | Severity |
   |------|--------|----------|

   ## Details
   ### file.py:line — Issue title
   Description and suggested fix
   ```

4. **Verdict**: APPROVE (no issues), APPROVE WITH COMMENTS (minor), or REQUEST CHANGES (blocking issues)

## References
- Code style rules: @.claude/rules/code-style.md (PY1-PY8, CM1-CM3)
- Test conventions: @.claude/rules/testing.md (T1-T9)

## Arguments
- `$ARGUMENTS`: optional file path or "staged" (review staged changes only)
- No arguments = review all uncommitted changes
