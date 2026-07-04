# code-style — Python style + commit prefixes
loads: on-trigger
scope: project
xref: [[.claude/rules/testing.md]] [[.claude/CLAUDE.md]]

## python

### PY1: indentation
rule: 4 spaces; match existing file

### PY2: type hints
rule: type hints on all function signatures

### PY3: schema shapes
rule: Pydantic models for all config and pipeline request/response shapes; dataclasses for internal value objects (e.g., `FetchResult`, `CleanedDocument`, `Chunk`, `CitationTuple`)

### PY4: import order
rule: `__future__`, stdlib, third-party, local

### PY5: async I/O
rule: async functions for all I/O (HTTP fetch, LocalAI embed calls)

### PY6: future annotations
rule: `from __future__ import annotations` in all modules

### PY7: ruff-compliant
rule: all code must pass `ruff check` (configured in `pyproject.toml` at Phase 0.1 Task 2); no ruff-suppressions without inline comment explaining why

### PY8: mypy-strict
rule: all public functions annotated; no `Any` in public interfaces (Protocol definitions, Pydantic models, public module APIs); internal helpers may use `Any` only with inline justification comment

## commit-messages

### CM1: prefixes
rule: use `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `style:`, `chore:`
note: `chore:` is for infrastructure commits (install-hooks, Docker, CI config, pyproject.toml scaffolding)

### CM2: subject length
rule: subject line under 72 characters

### CM3: issue refs
rule: reference issue numbers when applicable
