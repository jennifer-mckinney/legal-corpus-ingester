# Subagent Dispatch Guide

Per `.claude/library/LIB-PRINCIPLES.md` P8 (agent separation of duties), every implementation task in this project is dispatched to a role-scoped agent. The orchestrator (Claude or user) coordinates but does not execute code.

## Agent roles

| Subagent type | Role | Scope | Signoff authority |
|---------------|------|-------|-------------------|
| `general-purpose` | **Coder** — implements per task spec + writes own unit tests | One task at a time | None |
| `general-purpose` (isolated) | **Test Helper** — writes spec-conformance tests from spec ONLY; no visibility into Coder's diff | Once per stage complete | None |
| `general-purpose` (isolated) | **Critic** — runs Coder unit tests + Test Helper spec tests; reports pass/fail | After Coder + Test Helper return | None |
| `grumpy-developer` | **Grumpy Reviewer** — adversarial code-quality review | Every PR to `main` (CI job `grumpy-review`) | Merge gate: a CRITICAL, HIGH or MEDIUM finding fails the job; LOW and NIT are non-blocking (P9, owner decision 2026-10-09) |
| `security-engineer` | **Security Reviewer** — STRIDE review, merge gate | Every PR to `main` (CI job `security-review`) | Merge gate: a CRITICAL, HIGH or MEDIUM finding fails the job; LOW is non-blocking |
| `researcher` | **Researcher** — upstream source verification, license research | When adding a new source | None |
| `Explore` (read-only) | **Explorer** — codebase exploration when scope is uncertain | Ad hoc | None |
| `code-simplifier` | **Simplifier** — end-of-phase cleanup pass | Optional at phase boundary | None |
| `frontend-qa` | **Frontend QA** — E2E via Playwright | When UI exists (later phases) | Signoff on UI acceptance |

## Dispatch rules (P8 restated)

1. **One ask per agent.** Multi-ask prompts violate role isolation.
2. **Coder does NOT know Test Helper exists.** Isolation prevents convergence on implementation-biased tests.
3. **Critic reports pass/fail only.** No signoff authority — Decision = orchestrator or user.
4. **Orchestrator spot-checks disk state** after every agent return: `git diff --stat`, `mtime` on claimed files, `grep` for asserted changes. Trust the diff, not the report.

## Dispatch pattern per task

```
1. Orchestrator reads task spec from plan
2. Dispatch Coder with single-task role-scoped prompt
   - Exact file paths + acceptance criteria
   - Environment bounds (which files may/may not be touched)
3. Coder returns: diff + own unit test results
4. Orchestrator spot-checks disk state
5. (Optional) Dispatch Test Helper + Critic if stage boundary
6. Push the feature branch and open a PR to main
7. CI runs security-review + grumpy-review on the PR
8. Any blocking finding (CRITICAL, HIGH, MEDIUM) → fix-Coder dispatch → push → CI re-runs (repeat until both jobs pass); non-blocking LOW/NIT findings become cards
9. Owner merges once both jobs pass on the PR head
```

## Prompt template locations

| Role | Template location |
|------|-------------------|
| Coder | `docs/prompts/coder.md` *(Phase 0.1 Task TBD)* |
| Test Helper | `docs/prompts/test-helper.md` *(Phase 0.1)* |
| Critic | `docs/prompts/critic.md` *(Phase 0.1)* |
| Grumpy Reviewer | `docs/prompts/grumpy.md` *(Phase 0.1)* |
| Security Reviewer | `docs/prompts/security.md` *(Phase 0.1)* |
| Researcher | `docs/prompts/researcher.md` *(Phase 0.1)* |
| Explorer | `docs/prompts/explore.md` *(Phase 0.1)* |

## PEAS discipline per dispatch

Every dispatch prompt must name:
- **Performance**: single-line success measure (e.g., "`pytest tests/unit/` exits 0 with ≥80% coverage")
- **Environment**: exact files the agent may and may not touch
- **Actuators**: implicit from `subagent_type` tool-gating
- **Sensors**: implicit from prompt + tool-result stream

Reference: `~/.claude/library/PEAS.md`

## P9 review gate

Every PR to `main` runs two CI jobs, `security-review` and `grumpy-review` (`.github/workflows/p9-review.yml`). Each fails when its reviewer reports a `CRITICAL`, `HIGH` or `MEDIUM` finding, or writes no valid `p9-verdict.json` (owner decision 2026-10-09). `LOW` and `NIT` findings do not fail the job; they are posted inline and printed by the gate as non-blocking so they can be filed as cards. The local pre-push signoff hook and `.git/reviews/` signoffs are retired (terms-analysis#191). Only the owner can waive a finding, at merge time.

See `automations/p9-pre-push.md` for the workflow, the verdict contract and the owner setup steps.
