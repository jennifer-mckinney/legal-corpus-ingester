# Architecture Decision Records

ADRs document architectural decisions that are locked in `PRINCIPLES.md`.

Each ADR follows the format: `NNN-<slug>.md`

| ADR | Title | Status |
|-----|-------|--------|
| ADR-001 | Repository-pattern pipeline | Accepted |
| ADR-002 | Protocol-based stage interfaces | Accepted |
| ADR-003 | Config-driven per-source YAML | Accepted |
| ADR-004 | LocalAI Apertus-8B for embeddings | Accepted |
| ADR-005 | Atomic symlink-flip publish mechanism | Accepted |
| ADR-006 | VCR cassettes for HTTP replay in tests | Accepted |
| ADR-007 | Self-hosted GitHub Actions runner | Accepted |
| ADR-008 | numpy exhaustive search (no FAISS) | Accepted |
| ADR-009 | Versioned corpus bundles | Accepted |
| ADR-010 | Provenance tracking per chunk | Accepted |
| ADR-011 | SIGHUP for zero-downtime reload | Accepted |
| ADR-012 | Weekly drift canary workflow | Accepted |
| ADR-013 | pdfminer.six + trafilatura extraction | Accepted |
| ADR-014 | Approval YAML gate for license changes | Accepted |
| ADR-015 | Dependency rules C3/C7 cover the product, not CI review tooling | Accepted |

To propose a change to any accepted ADR: open a PR with a new ADR file that supersedes the prior one, update the table above, and update the relevant constraint in `PRINCIPLES.md`.
