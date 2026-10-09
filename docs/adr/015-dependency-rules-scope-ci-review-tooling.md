# ADR-015: Dependency rules C3 and C7 cover the product, not CI review tooling

- **Status:** Accepted
- **Date:** 2026-10-09
- **Decided by:** owner, 2026-10-09
- **Governs:** `PRINCIPLES.md` constraints C3 (no packages from companies facing investor lawsuits, no VC-funded LLM houses) and C7 (every dependency audited to IRP Grade A)

## Context

Card terms-analysis#191 replaces the local P9 pre-push gate with two CI review jobs in `.github/workflows/p9-review.yml`. Each job runs `anthropics/claude-code-action` against the Claude API to review the pull request diff.

On PR #20, GitHub Copilot flagged the action at `p9-review.yml:79` (and again at the second job). It said the action and the direct Anthropic API call break `PRINCIPLES.md` C3 and C7, because they are a dependency from a VC-funded LLM vendor with no grade-A audit and no approval in `.claude/library/LIB-STACK.md`. Copilot asked for a local review mechanism instead, or for an explicit policy exception recorded in an ADR.

C3 and C7 were written to protect the product: what the ingester fetches, cleans, chunks, embeds, indexes and publishes, and what the consumer (terms-analysis) loads. They did not say whether they also cover tools that only review source code during development.

## Decision

C3 and C7 apply to the product's runtime and data path. That means every model, library and service that processes, embeds, indexes or publishes legal text, or that ships in the published bundle or the installed package.

Development-time CI review tooling is exempt. This covers `anthropics/claude-code-action` and the Claude API calls it makes from `.github/workflows/p9-review.yml`. It never touches the product's data path and never ships in the artifact.

## Conditions

The exemption holds only while all of these are true. If any one stops being true, the exemption lapses and C3 and C7 apply in full.

1. **SHA-pinned action.** Every `uses:` of the action is pinned to a full 40-character commit SHA, not a tag or branch.
2. **Read-only tool allowlist.** The review agent gets only read tools (`Read`, `Grep`, `Glob`), an edit permission limited to its own verdict file, and the inline-comment tool. `Bash`, `WebFetch` and `WebSearch` are disallowed. Reads outside the checkout and reads of credentials and `.git` are denied.
3. **No product data beyond the PR diff.** The jobs run on GitHub-hosted runners, never the self-hosted corpus runner (C4). What reaches the API is the PR diff, the changed-file list, the commit list and tracked repository files the read tools open. Corpus bundles, indexes, embeddings and `out/` are never in that checkout.
4. **Not in the artifact.** The action and its API client are not package dependencies and are not in the published bundle.

## Consequences

- PR #20 can keep the CI review jobs without a grade-A audit entry or a LIB-STACK listing for the action.
- The product's dependency bar is unchanged. Any model or library on the data path still needs C3 and C7 in full, including the bans on Meta-origin and VC-funded LLM vendors.
- Changing any condition above (unpinning the action, widening the tool allowlist, moving the jobs to the self-hosted runner, or sending corpus data) needs a new ADR that supersedes this one.
- Other development-time tools are not exempted by this ADR. Each one needs its own decision.
