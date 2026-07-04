format: agent-optimized (2026-07-04 stub — full version at P8)
# legal-corpus-ingester — project identity (stub)
loads: auto
scope: project
xref: [[LIB-PRINCIPLES]] [[../terms-analysis/.claude/CLAUDE.md]] [[../terms-analysis/docs/plans/2026-07-04-legal-corpus-ingester.md]]

## identity

| Key | Value |
|-----|-------|
| Purpose | Fetch, clean, chunk, embed, publish legal corpus bundles consumed by terms-analysis |
| Stack | Python 3.10+, Typer, Pydantic v2, httpx, BeautifulSoup4, pdfminer.six, lxml, LocalAI (Apertus-8B), numpy, PyYAML, pytest, vcrpy |
| Consumer | terms-analysis sibling — reads bundles produced here |
| Status | Phase 0.0 P1-P7 shipped 2026-07-04; P8-P10 pending |

## hard-requirements (from full plan)

- HR1 open-source-only
- HR2 no-investor-lawsuit-vendors (no Meta origin, no VC-funded LLM houses)
- HR3 IRP-grade-A-or-higher (dependency-audit skill enforces)
- HR4 local-only-data (self-hosted GitHub Actions runner)
- HR5 fail-loud-on-source-drift
- HR6 no-openai-local-LLM-only (LocalAI + Apertus-8B for embeddings)
- HR7 manifest-pinning-hard-fail (consumer verifies MANIFEST on startup)
- HR8 license-drift-zero-tolerance
- HR9 legal-review-gate-required (APPROVAL.yaml)

## commands

| Task | Command |
|------|---------|
| Install (once pyproject lands, P0.1 T2) | `python -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'` |
| Install hooks | `bash scripts/install-hooks.sh` |
| Setup runner | `bash scripts/setup_runner.sh` |

## automations

- `.githooks/pre-commit` — ruff/mypy/pytest (Phase 0.0 grace path)
- `.githooks/pre-push` — P9 hard-gate (signoff required at `.git/reviews/<sha>.signoff.json`)
- `.github/workflows/ci.yml` — baseline CI on self-hosted runner
- `automations/p9-pre-push.md` — signoff schema + gate behavior
- `automations/self-hosted-runner.md` — runner install + tear-down
- `automations/ci-pr.md` — CI trigger reference
- `automations/logging.md` — structured JSON logging + redaction contract
- `automations/pre-commit.md` — pre-commit hook contract
- `automations/docker.md` — Docker + compose contract
- `automations/secrets.md` — `.env` management + rotation

## reference-library

| Key | File | Use When |
|-----|------|----------|
| **LIB-PRINCIPLES** | `.claude/library/LIB-PRINCIPLES.md` | P1-P9 governance (mirrors terms-analysis) |

More library files land at Phase 0.0 Task P8 (LIB-ARCH, LIB-STACK, LIB-TEST).

## next-session-pickup

Phase 0.0 tasks remaining (see full plan at `../terms-analysis/docs/plans/2026-07-04-legal-corpus-ingester.md`):
- P7 (`98a6f06`) landed locally, not yet pushed — bundle with P8-P10 push
- P8: full `.claude/` governance scaffolding (this file is a stub)
- P9: governance manifest + hash-tracking scripts
- P10: `docs/TOOLS.md`, `docs/AGENTS.md`, `.claude/skills/`
