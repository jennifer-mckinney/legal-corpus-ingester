format: constitutional (immutable without ADR + PR + manifest bump)
# legal-corpus-ingester — PRINCIPLES

This file locks architectural constraints and decisions for the legal-corpus-ingester.
Any change requires: (1) a new ADR in `docs/adr/`, (2) a PR review, (3) a `scripts/governance/regen-manifest.sh --yes` bump.

---

## Constraint-native design

The ingester is a local-data-only, open-source-only pipeline. Constraints are first-class.

### What this project CANNOT do

| # | Constraint | Because |
|---|-----------|---------|
| C1 | Fetch runtime permission manifests from external URLs | HR4 — all data stays local; external fetches would leak corpus topology |
| C2 | Use CC-BY-NC or commercially-restricted dependencies | HR1 — open source only (Apache 2.0 / MIT / BSD / MPL 2.0 / public domain preferred) |
| C3 | Use packages from companies facing investor lawsuits | HR2 — no Meta-origin (FAISS, etc.), no VC-funded LLM houses. Scope: product runtime and data path only; CI review tooling is exempt per [ADR-015](docs/adr/015-dependency-rules-scope-ci-review-tooling.md) |
| C4 | Dispatch fetch or embed workloads to remote (GitHub-hosted) runners | HR4 — self-hosted runner only; corpus artifacts must never leave the local machine |
| C5 | Call OpenAI or any cloud embedding API | HR6 — local-only LLM inference via LocalAI + Apertus-8B |
| C6 | Silently publish a shorter corpus on source drift or 404 | HR5 — fail-loud on drift; emit ALERTS.md; halt the affected source |
| C7 | Accept a dependency with IRP Grade below A | HR3 — every dependency audited via `.claude/skills/dependency-audit` before merge. Scope: product runtime and data path only; CI review tooling is exempt per [ADR-015](docs/adr/015-dependency-rules-scope-ci-review-tooling.md) |
| C8 | Publish a bundle without MANIFEST.yaml containing embedder_model + embedder_revision | HR7 — consumer (terms-analysis) verifies these fields on startup; missing = HTTP 503 |
| C9 | Proceed past SPDX license change without human review | HR8 — license drift zero tolerance; blocked until APPROVAL.yaml is updated |
| C10 | Ingest a license-risk source without a signed APPROVAL.yaml | HR9 — legal-review gate required |

---

## Locked ADRs

All 14 ADRs are locked decisions. Full ADR design rationale captured in the project planning session; contact maintainer for the source document. A summary will land in `docs/adr/` files at Phase 0.1.
ADR markdown files land in `docs/adr/` during Phase 0.1.

| ADR | Decision | Short title |
|-----|---------|-------------|
| ADR-001 | CLI framework = **Typer** | Pydantic-native, matches hub conventions |
| ADR-002 | Config schema = **Pydantic BaseModel from YAML** | `model_validate` for safety, no schema inference |
| ADR-003 | Chunker parity = **vendor + snapshot test** | Detect drift from terms-analysis; semver + git SHA in MANIFEST |
| ADR-004 | Publish strategy = **atomic symlink + selective copy at bundle level** | `corpus/` + `index/` + `MANIFEST.yaml` + `provenance/` + `ALERTS.md` atomically; `--target tarball` uses tar.gz (HR2) |
| ADR-005 | Jurisdiction Literal sync = **generator script** | `scripts/sync_jurisdictions.py` at sync-time, not runtime |
| ADR-006 | License drift storage = **git-tracked YAML** | `sources/registry.yaml` with pinned SPDX + `license_text_sha256`; drift visible in PRs |
| ADR-007 | Legal-review gate = **in-tree APPROVAL.yaml per source** | Verified against `signed_artifact_sha256` + `today < expiry`; env-var override only for emergencies |
| ADR-008 | MANIFEST schema = **load-bearing artifact** | `{corpus_version (calver), generated_at, chunker_version, embedder_model, embedder_revision, sources: [{id, revision_date, approval_ref, chunk_count, sha256, license_spdx, license_sha256, http_status, fetched_at, byte_count}]}` |
| ADR-009 | Citation tuple schema = **structured per chunk** | `{jurisdiction, instrument, short_label, unit, eli_uri\|us_lii_url, license, attribution}`; rendering at read time; no pre-formatted strings |
| ADR-010 | Grace-period tripwire = **pure function + YAML index** | `resolve_status(chunk, today)` pure; `state/tripwires.yaml` for observability only, never authoritative |
| ADR-011 | Retention policy = **4 weekly + 12 monthly + quarterly forever** | `--dry-run` gate; refuse to prune `out/current` symlink target |
| ADR-012 | Runtime handoff = **atomic symlink flip + SIGHUP** | `POST /reload` as fallback under uvicorn `--workers` > 1 |
| ADR-013 | External-source testing = **VCR.py cassettes** | `record_mode='none'` in CI; weekly `--vcr-record=all` schema-drift canary |
| ADR-014 | Consumer verification = **MANIFEST field assertions** | `LegalKnowledgeBase.load_from_bundle()` asserts chunker_version / embedder_model / embedder_revision; mismatch → HTTP 503 + `X-Corpus-Mismatch` header |

---

## Change process

1. Identify the ADR that governs the area being changed (or draft a new ADR-NNN).
2. Write `docs/adr/NNN-<slug>.md` — problem, options considered, decision, consequences.
3. Open a PR; get review from at least the security-engineer agent and one human approver.
4. After merge, run `scripts/governance/regen-manifest.sh --yes` to bump the governance manifest.
5. Update this file if the constraint table changes.

Constraints C1–C10 above are **absolute** — no override without explicit user directive and a permanent ADR justification.
