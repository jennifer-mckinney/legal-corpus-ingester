format: agent-optimized (2026-07-04)
# legal-corpus-ingester — project identity, hard requirements, library index
loads: auto
scope: project
xref: [[LIB-PRINCIPLES]] [[LIB-ARCH]] [[LIB-STACK]] [[LIB-TEST]]
      [[docs/plans/2026-07-04-legal-corpus-ingester.md]]
      [[~/.claude/plans/delightful-humming-tome.md]]
      [[_AUTOMATION/CLAUDE.md]]
      [[../terms-analysis/.claude/CLAUDE.md]]

## identity

| Key | Value |
|-----|-------|
| Purpose | Fetch + clean + chunk + embed + publish legal corpus bundles for terms-analysis |
| Stack | Python 3.10+, Typer, Pydantic v2, httpx, BeautifulSoup4, pdfminer.six, lxml, LocalAI (Apertus-8B), numpy, PyYAML, pytest, vcrpy |
| Consumer | terms-analysis (sibling project) — reads bundles produced here |
| Status | Phase 0.0 complete (P1-P10); Phase 0.1 next |

## hard-requirements

### HR1: open-source-only
rule: all dependencies MUST be open source (Apache 2.0, MIT, BSD, MPL 2.0, public domain preferred)

### HR2: no-investor-lawsuit-vendors
rule: no tools/services from companies facing investor lawsuits; no Meta-origin packages (FAISS excluded); no VC-funded LLM houses per _AUTOMATION funding bar

### HR3: IRP-grade-A-or-higher
rule: every dependency added passes .claude/skills/dependency-audit before merge

### HR4: local-only-data
rule: all corpus data stays local; self-hosted GitHub Actions runner (never GitHub-hosted)

### HR5: fail-loud-on-source-drift
rule: 404 / schema change / license SPDX drift halts the affected source and emits ALERTS.md; never silent shorter corpus

### HR6: no-openai-local-LLM-only
rule: embedding via LocalAI + Apertus-8B; no OpenAI, no cloud embedding APIs

### HR7: manifest-pinning-hard-fail
rule: consumer verifies MANIFEST embedder_model + embedder_revision on startup; mismatch → HTTP 503 with X-Corpus-Mismatch header

### HR8: license-drift-zero-tolerance
rule: SPDX change on any tracked source blocks publish until human review

### HR9: legal-review-gate-required
rule: sources with license risk (e.g. Singapore SSO) require APPROVAL.yaml with signed_artifact_sha256 verification before ingest

## project-map

| Path | Purpose |
|------|---------|
| `src/legal_corpus_ingester/` | Python package |
| `src/legal_corpus_ingester/fetchers/` | Per-source fetch modules |
| `src/legal_corpus_ingester/cleaners/` | HTML/PDF/XML/plaintext normalization |
| `src/legal_corpus_ingester/chunkers/` | Section-aware + plain chunkers (vendored from terms-analysis) |
| `src/legal_corpus_ingester/embedders/` | LocalAI Apertus-8B client |
| `src/legal_corpus_ingester/publishers/` | filesystem / terms-analysis / tarball |
| `src/legal_corpus_ingester/pipeline/` | orchestrator + state + manifest + retention |
| `src/legal_corpus_ingester/provenance/` | tracker + license_audit + manifest writer |
| `config/sources/` | per-source YAML |
| `state/` | manifest.json, checkpoints, license-hashes.json (git-tracked) |
| `out/YYYY.MM.PATCH/` | versioned bundles |
| `docs/adr/` | 14 ADRs |
| `automations/` | one .md per automation |
| `tests/` | unit + integration + e2e + snapshot + cli |

## commands

| Task | Command |
|------|---------|
| Install | `python -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'` |
| Install hooks | `bash scripts/install-hooks.sh` |
| Run tests | `pytest` |
| Fetch one source | `ingester fetch eurlex` |
| Full refresh | `ingester refresh` |
| Status | `ingester status` |
| Validate round-trip | `ingester validate-round-trip out/current` |

## reference-library

| Key | File | Use When |
|-----|------|----------|
| **LIB-PRINCIPLES** | `.claude/library/LIB-PRINCIPLES.md` | P1–P9 governance (mirrors terms-analysis) |
| **LIB-ARCH** | `.claude/library/LIB-ARCH.md` | Module structure, interface contracts, data flow |
| **LIB-STACK** | `.claude/library/LIB-STACK.md` | Dependency list, versions, IRP grades |
| **LIB-TEST** | `.claude/library/LIB-TEST.md` | Test layers, coverage gates, VCR cassette workflow |

## governance-monitoring

### G1: content-consistency
manifest: `.claude/_governance-manifest.json`
tracks: SHA256 of `.claude/CLAUDE.md`, `.claude/library/LIB-PRINCIPLES.md`, `$HOME/.claude/CLAUDE.md`, `$HOME/.claude/library/PEAS.md`
verify: `scripts/governance/verify-hashes.sh`
regen: `scripts/governance/regen-manifest.sh --yes`

### G2: pre-push-independent-review
rule: LIB-PRINCIPLES P9 — grumpy-developer + security-engineer dispatched before every push; zero-tolerance security gate; zero-tolerance grumpy (2026-07-04 directive)

## automations

| Automation | Trigger | Docs |
|-----------|---------|------|
| pre-commit hook | `git commit` | `automations/pre-commit.md` |
| CI on PR | GitHub PR | `automations/ci-pr.md` |
| P9 pre-push gate | `git push` | `automations/p9-pre-push.md` |
| Structured logging contract | all modules | `automations/logging.md` |
| Docker skeleton | `docker compose up` | `automations/docker.md` |
| Secrets management | `.env` loading | `automations/secrets.md` |
| Self-hosted runner | launchd service | `automations/self-hosted-runner.md` |

## skills

| Skill | Trigger | Purpose |
|-------|---------|---------|
| `/dependency-audit` | "audit dependency", "check license" | IRP-score a dep against HR1-HR3 |
| `/test-suite` | "run tests", "check coverage" | Run pytest + coverage, analyze failures |
| `/write-tests` | "write tests for X" | Guided: read source → plan → write → verify |
| `/review` | "review this" | Code quality review against project conventions |
| `/ralph-loop` | "iterate on X", "loop until done" | Self-referential dev loop to completion |
| `/corpus-fetch` | "fetch corpus", "ingest GDPR" | Guided: license verify → fetch → provenance → cassette |
| `/corpus-publish` | "publish corpus", "cut a bundle" | Guided: validate → publish → symlink flip → verify consumer |
