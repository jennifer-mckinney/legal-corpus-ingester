# LIB-STACK — dependencies, versions, IRP grades
loads: on-trigger
scope: project
xref: [[LIB-ARCH]] [[LIB-PRINCIPLES]] [[.claude/CLAUDE.md]]

## planned-dependencies

No `pyproject.toml` yet — lands at Phase 0.1 Task 2. Grades are provisional until that task ships and
`.claude/skills/dependency-audit` validates each package against HR1–HR3.

| Package | Version | License | Funding origin | IRP grade | Rationale |
|---------|---------|---------|----------------|-----------|-----------|
| Python | ≥3.10 | PSF | nonprofit | A | Runtime |
| typer | ≥0.12 | MIT | open source | A | CLI entrypoint (ADR-001) |
| pydantic | v2.x | MIT | open source | A | Config validation + data models (ADR-002) |
| httpx | ≥0.27 | BSD-3 | open source | A | Async HTTP fetch; replaces sync `requests` |
| beautifulsoup4 | ≥4.12 | MIT | open source | A | HTML normalization (secondary to trafilatura) |
| trafilatura | ≥1.9 | Apache-2.0 | open source | A | Main-content extraction from HTML |
| pdfminer.six | ≥20231228 | MIT | open source | A | PDF text extraction |
| lxml | ≥5.2 | BSD | open source | A | XML/HTML parsing |
| PyYAML | ≥6.0 | MIT | open source | A | Config + MANIFEST read/write |
| numpy | ≥1.26 | BSD-3 | NumFOCUS | A | Embedding vector ops (cosine similarity, no FAISS) |
| vcrpy | ≥6.0 | MIT | open source | A | VCR cassettes for integration tests (ADR-013) |
| syrupy | ≥4.0 | Apache-2.0 | open source | A | Snapshot tests for chunker parity (ADR-003) |
| pytest | ≥8.0 | MIT | open source | A | Test framework |
| pytest-cov | ≥5.0 | MIT | open source | A | Coverage gate enforcement |
| coverage | ≥7.0 | Apache-2.0 | open source | A | Coverage measurement |
| ruff | ≥0.5 | MIT | open source | A | Lint + format (replaces flake8 + black) |
| mypy | ≥1.10 | MIT | open source | A | Static type checking |

Note: All deps verified via `.claude/skills/dependency-audit` before merge. IRP grades are provisional
until pyproject.toml lands at Phase 0.1 Task 2.

## ci-dev-tooling

GitHub Actions used only by CI workflows. These are not package dependencies, never run on the
product data path, and never ship in the published bundle or installed package. Every `uses:` is
pinned to a full 40-character commit SHA; the workflow file holds the pin of record.

| Action | Version | Used in | Product dependency rules (C3/C7) |
|--------|---------|---------|----------------------------------|
| `anthropics/claude-code-action` | v1, SHA-pinned | `.github/workflows/p9-review.yml` (P9 security + grumpy review jobs) | Exempt under ADR-015 (`docs/adr/015-dependency-rules-scope-ci-review-tooling.md`) while its four conditions hold: SHA pin, read-only tool allowlist, GitHub-hosted runners with no product data beyond the PR diff, not in the artifact |
| `actions/checkout` | v4.2.2 / v6, SHA-pinned | `.github/workflows/ci.yml`, `.github/workflows/p9-review.yml` | Outside the product data path that ADR-015 scopes C3/C7 to; checks out the repository only |

## excluded-packages

| Package | Reason |
|---------|--------|
| FAISS | Meta-origin (HR2 violation) |
| faiss-cpu / faiss-gpu | Same — Meta-origin (HR2 violation) |
| OpenAI SDK (`openai`) | Cloud API dependency (HR6 violation — no external API calls) |
| Any CC-BY-NC packages | License restriction prevents commercial/derivative use (HR1 violation) |
| requests | Sync-only; `httpx` preferred for async pipeline (ADR-001) |
| sentence-transformers | Pulls PyTorch + transformers stack; LocalAI Apertus-8B used instead via thin client |
| langchain / llamaindex | Heavy framework abstraction not needed; direct Protocol implementations preferred |

## localai-integration

LocalAI is a self-hosted inference server — not a pip dependency.

| Property | Value |
|----------|-------|
| Runtime | LocalAI server (local, self-hosted; see `docker-compose.yml`) |
| Model | Apertus-8B (multilingual legal embeddings) |
| Client | `src/legal_corpus_ingester/embedders/localai_client.py` (thin httpx wrapper) |
| Model SHA | Pinned in MANIFEST `embedder_revision` field (ADR-008) |
| Mismatch behavior | Consumer (`terms-analysis legal_kb.py`) raises `CorpusVersionError` on SHA mismatch (ADR-014) |
| Test mock | `unittest.mock` patches `localai_client.LocalAIEmbedder`; never calls real LocalAI in tests (T5) |

## config-files

| File | Purpose |
|------|---------|
| `config/sources/*.yaml` | Per-source fetch config: url, auth, content_type, license_spdx, approval_ref |
| `config/pipeline.yaml` | Pipeline-level config: retention (default: keep 3 versions), timeout, batch size |
| `APPROVAL.yaml` | Explicit approval record per source (SHA tracked in MANIFEST) |

## python-version-note

Python ≥3.10 required for:
- `match` statements (used in cleaner dispatch)
- `X | Y` union syntax in type hints
- `from __future__ import annotations` pattern (PY6)
- Pydantic v2 compatibility
