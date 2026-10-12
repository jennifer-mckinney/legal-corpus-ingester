# #71 state cache: best practice and a higher bar for Q1-Q7 (research)

**Question:** For each open question Q1-Q7 in `docs/plans/2026-10-12-71-state-cache.md` (PR #110), what is the current documented best practice, what design beats it, and does that design survive the project constraints?
**Dispatched by:** lead, on the owner's instruction of 2026-10-12: "research what the latest best practices are and lets do better than those - raise the bar for Q1-Q7".
**Date:** 2026-10-12 (all URLs accessed 2026-10-12)
**Scope note:** the plan file is not edited here. A separate agent folds these answers into it.

## 0. Constraints used to screen every idea

1. GitHub-hosted runners only.
2. Standard CI/CD over custom machinery (vendor-documented patterns, no bespoke services).
3. Open source; no new third-party action unless SHA-pinned and widely used.
4. No secret beyond `GITHUB_TOKEN`, least privilege; any `actions: read` / `actions: write` must be justified.
5. Public repo: no state may leak.
6. "A path that can't tell 'nothing to do' from 'not wired' is the highest-risk defect class": every state must fail loud.

## 1. Facts that shape every answer

| # | Fact | Source | URL |
|---|---|---|---|
| F1 | A run can restore caches from its own branch and the default branch; not from child or sibling branches. A cache made by a PR run is scoped to `refs/pull/.../merge` and only re-runs of that PR can restore it. | GitHub Docs, dependency caching | https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching |
| F2 | "only these workflow triggers can create or overwrite caches in the default branch's scope: push, workflow_dispatch, repository_dispatch, delete, registry_package, page_build, schedule". Runs from any other event that resolves to the default branch get read-only access. | same | same |
| F3 | Anyone with read access can open a PR and read a cache's contents, including forks reading base-branch caches. Do not put secrets in the cache path. | same | same |
| F4 | Entries are immutable; unused for over 7 days means evicted; 10 GB default per repository, evicted oldest last-access first; with several partial matches `restore-keys` returns "the most recently created cache". | same | same |
| F5 | Cache access does not depend on `GITHUB_TOKEN` permissions: "any workflow on the default branch can read/write the cache". An attacker who can run code in any default-branch job can fill the 10 GB to evict a real entry and plant a replacement. Cache hits match a client-set key and version. Advice: do not consume the cache where integrity matters more than speed. | Adnan Khan, Cacheract | https://adnanthekhan.com/2024/12/21/cacheract-the-monster-in-your-build-cache/ |
| F6 | SLSA Build L3: "It MUST NOT be possible for one build to inject false entries into a build cache used by another build, also known as 'cache poisoning'." | SLSA v1.0 requirements | https://slsa.dev/spec/v1.0/requirements |
| F7 | In this repo, `ci.yml` runs on `push` to `main` and runs `pip install -e '.[dev]'`; `health.yml`, `vcr-drift.yml`, `approval-expiry.yml` and `refresh.yml` run on `schedule` and also `pip install`. By F2 every one of them is a trusted writer of the default-branch cache scope, and by F5 the restore-only action does not stop code in the same job from writing. | repo, `.github/workflows/*.yml` at e5269d5 | local read, not a URL |
| F8 | `actions/cache/save` has no outputs; a save failure or an unavailable cache feature only logs a warning and the step stays green; a save is skipped as a no-op when the key equals the restored key. | actions/cache `src/saveImpl.ts` and `save/README.md` at the pinned SHA | https://github.com/actions/cache/blob/55cc8345863c7cc4c66a329aec7e433d2d1c52a9/src/saveImpl.ts , https://github.com/actions/cache/blob/55cc8345863c7cc4c66a329aec7e433d2d1c52a9/save/README.md |
| F9 | `actions/cache/restore` with `lookup-only: true` checks "if a cache entry exists ... without downloading"; `fail-on-cache-miss: true` throws on a miss; but when the cache feature is unavailable the step returns green with `cache-hit=false` and no failure; `cache-hit` is `true` only on an exact key match. | actions/cache `src/restoreImpl.ts`, `action.yml`, `restore/README.md` at the pinned SHA | https://github.com/actions/cache/blob/55cc8345863c7cc4c66a329aec7e433d2d1c52a9/src/restoreImpl.ts |
| F10 | Pinned SHA check: tag `v6.1.0` resolves to `55cc8345863c7cc4c66a329aec7e433d2d1c52a9` (`gh api repos/actions/cache/git/ref/tags/v6.1.0`, type `commit`, so not an annotated-tag object), published 2026-06-26T19:17:06Z, the floating `v6` tag points at the same commit. `v5.1.0` was published an hour later (2026-06-26T20:21:47Z) as a back-port of the same fix; `v6.1.0` is the highest major. The plan's SHA is correct. | GitHub API / releases | https://github.com/actions/cache/releases |
| F11 | Scheduled workflows in public repos are "automatically disabled when no repository activity has occurred in 60 days"; scheduled runs can be delayed and "some queued jobs may be dropped" under load. | GitHub Docs, events that trigger workflows | https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows |
| F12 | REST: `GET /repos/{o}/{r}/actions/caches` (params `key` prefix, `ref`, `sort`) needs Actions **read**; `DELETE` by key or id needs Actions **write**. `gh cache list --key` and `--ref` wrap it. Repository-level cache retention and storage limits are settable by `PUT .../actions/cache/retention-limit` and `.../storage-limit` (write). Docs give no default or maximum for them. | GitHub REST docs and fine-grained-token permission table | https://docs.github.com/en/rest/actions/cache , https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens , https://cli.github.com/manual/gh_cache_list |
| F13 | Running a workflow that references a non-existent environment "will create an environment with the referenced name" with no protection rules or secrets. | GitHub Docs, manage environments | https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments |
| F14 | Artifact attestations need `id-token: write`, `attestations: write`, `contents: read` with `actions/attest`; `gh attestation verify` can enforce `--signer-workflow` and `--source-ref`; auth is a `GH_TOKEN`. | GitHub Docs and gh manual | https://docs.github.com/en/actions/security-for-github-actions/using-artifact-attestations/using-artifact-attestations-to-establish-provenance-for-builds , https://cli.github.com/manual/gh_attestation_verify |
| F15 | OpenSSF Scorecard: Token-Permissions (read-only at the top level, writes declared per job, risk High), Pinned-Dependencies (hash pins, risk Medium), Dangerous-Workflow (Critical). No Scorecard check covers caches. | Scorecard checks | https://github.com/ossf/scorecard/blob/main/docs/checks.md |
| F16 | zizmor has a `cache-poisoning` audit: reads from caches inside release workflows and writes via privileged triggers; remedy is to drop or condition caching. Also flags excessive permissions (`permissions: {}` at workflow level) and unpinned uses. | zizmor audits | https://docs.zizmor.sh/audits/ |
| F17 | ADR practice: "Don't alter existing information in an ADR. Instead, amend ... or supersede"; write an ADR when future readers need the "why"; skip when "limited in scope and time and risk and cost". This repo's `docs/adr/README.md`: "To propose a change to any accepted ADR: open a PR with a new ADR file that supersedes the prior one, update the table above, and update the relevant constraint in `PRINCIPLES.md`." | ADR repo; local README | https://github.com/joelparkerhenderson/architecture-decision-record |
| F18 | Concurrency: with the default settings a queued run in a busy group sits `pending` and a newer pending run replaces it. `permissions:` with any entry sets every unlisted scope to `none`. | GitHub Docs, workflow syntax | https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax |

**What F5 and F7 mean together.** The cache is not an integrity boundary in this repo. Any default-branch workflow, including `ci.yml` on push, can plant an entry newer than the real one, and `restore-keys` then prefers it (F4). The state only decides "announce or stay quiet", so the worst case is a suppressed announcement, which is exactly the silent failure #71 exists to prevent. The answers below treat integrity as best-effort and say where more would cost more.

Note on sources: pages from docs.github.com, cli.github.com, slsa.dev, zizmor and Scorecard were read through a fetch summariser, so the quoted phrases are as returned by it. The actions/cache facts (F8-F10) were read from raw files at the pinned SHA.

## 2. Q1: where the first state record is seeded

**Best practice today.** Dependency-cache guidance assumes a miss is harmless and the job rebuilds and saves (https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching). For state a miss is not harmless. The vendor offers `fail-on-cache-miss` for "this entry must exist" (F9). No source I found describes a seed ceremony for cache-held state; I found no precedent and say so.

**Raise the bar.**
1. Keep the plan's explicit, never-automatic seed (option a), default branch only, refusing unless `ABSENT`.
2. Put provenance in the marker: `seeded_by_run`, `seeded_by_actor` (`github.actor`), `seeded_at_sha` (`github.sha`). Costs three fields.
3. The seed job ends by proving the save landed with the Q7 mechanism (exact-key lookup, hard fail), so a seed run can never be green with nothing saved. The plan's check (`gh cache list --key`) needs `actions: read`; Q7's does not.
4. The seed's "does an entry already exist" test is the restore step's `cache-matched-key` over the full prefix (already in the plan), not a REST call.

**Screen.** Items 1-4 add no permission, no action, no secret; the marker fields hold a run id, a public actor login and a commit SHA, nothing private (a public repo shows all three). Pass.

**Rejected: a protected `environment` (required reviewer) around the seed job.** F13: referencing an environment that does not exist silently creates one with no protection, so a missing owner setup passes green with no gate. That is a "not wired looks like fine" path, and the gate lives in repository settings, outside code review. For a single-owner repo it also adds an approval click with no second person. `workflow_dispatch` already needs write access, and the default-branch guard plus the marker provenance give the audit trail.

**Recommended answer: (a), with provenance fields and a Q7-verified save. Confidence: HIGH** on rejecting auto-seed (plan's own reasoning, plus F9), MEDIUM on the absence of precedent (negative search result).

## 3. Q2: what health reports when there is no saved state

**Best practice today.** Absence of data is its own state, never "OK" (the project's own rule). GitHub's `fail-on-cache-miss` is the vendor mechanism for a hard miss (F9), but it ends the step before a classifier can say why and cannot express "warn while unwired".

**Raise the bar.**
1. Keep the plan's tri-state and option (b): `ABSENT` is a warning while `REFRESH_WIRED` is False and red after. Make the warning loud, not a log line: a `::warning::` annotation plus a line in `$GITHUB_STEP_SUMMARY` that says "unwired grace, flips to red at G2".
2. Make the flip structural: T-H1 must parametrise `REFRESH_WIRED=True` with `ABSENT` and assert non-zero, so wiring refresh cannot ship with the grace still on.
3. Add a freshness rule that applies once wired: a `PRESENT` record whose `recorded_at` is older than 8 days (weekly cadence plus one day) is red `stale`. This needs only the record on disk, no API. It catches the case the plan misses: the cache restores fine but refresh has silently stopped, for example the schedule was auto-disabled (F11) or runs were dropped under load (F11).
4. State the residual risk in the ADR: if the schedule itself is disabled, health stops too, and nothing in the repo can notice. I did not verify which events count as "repository activity" for the 60-day rule (F11 does not say).

**Screen.** Items 1-3 are code and tests in files the plan already touches; no permission. Item 3 is a small amount of custom logic (a date compare) and is the one place this question leans away from "standard", but it is a few lines in `health_check.py`. Pass, with that note.

**Rejected: (a) always red from merge.** It trains the owner to ignore a nightly red for the unwired window, which weakens the signal exactly when it matters. (c) is rejected as the plan does.

**Recommended answer: plan option (b) plus the structural flip test and the 8-day stale rule. Confidence: MEDIUM** (design, no external source for the 8-day threshold; it follows from the weekly cron).

## 4. Q3: save state after a failed run

**Best practice today.** The vendor documents `actions/cache/save` guarded by `always()` to save after a failure; `save-always` on the combined action is no longer the pattern (https://github.com/actions/cache/blob/55cc8345863c7cc4c66a329aec7e433d2d1c52a9/save/README.md , lines 74 and 109). Common practice is `if: always()` and a key from the primary key.

**Raise the bar.**
1. Keep `!cancelled()` plus the `seeded`/`present` allowlist and the default-branch guard (the plan, and issue #111's wording fix). `always()` is too wide: a cancelled run can be mid-write.
2. Add a no-regress rule in `state_verify`: if the restored record's `recorded_at` (or version, if the record has a sortable one) is newer than the one about to be saved, classify `INVALID` and do not save. This stops a stale or replayed state from becoming the newest entry (F4: newest wins).
3. Add a content digest to the key (see Q7). A save then carries a self-check that the next restore verifies.

**Screen.** All code in `state_store.py`; no permission. Pass.

**Rejected: save only on success (b).** It discards checkpoints from a partly successful wired refresh, and on a failed run it leaves the entry's age to health's nightly restore alone. **Rejected also: `always()` (c).**

**Recommended answer: (a) as reworded in #111 (allowlist, `!cancelled()`), plus the no-regress rule. Confidence: HIGH** on `!cancelled()` and allowlist; MEDIUM on the no-regress rule (depends on what ordering field the record carries; implementer confirms).

## 5. Q4: pruning old saved state

**Best practice today.** Rely on eviction: 7 days unaccessed and the 10 GB cap evicting oldest last-access first (F4). Entries are a few KB. Active deletion (`gh cache delete`, REST DELETE) needs Actions write (F12).

**Raise the bar: bound it, measure it, do not delete.**
1. Reason about churn instead of pruning it. Restore touches only the matched (newest) entry. Older entries are never accessed again and age out in 7 days. With one weekly refresh plus occasional dispatches there are about 1-3 live entries. Put that bound in the ADR with the arithmetic.
2. LRU protects the real entry from other workflows' churn: health restores nightly, so it is always among the most recently accessed and is evicted last (F4). If 10 GB pressure ever appears from another cache, our entry goes last.
3. Reserve the key prefix: a test (T-W) fails if any workflow other than `refresh.yml` and `health.yml` mentions `ingester-state-`. A same-prefix write from another workflow would be a newer entry that wins (F4, F5).
4. Report entry age in every health summary (days since `recorded_at`), so eviction margin is visible before it bites.

**Screen.** No permission, no action. Pass.

**Rejected: a prune step with `actions: write`.** It widens the token for the whole refresh job (Scorecard Token-Permissions, F15) and its failure mode is the failure we are preventing: a prune bug that deletes the newest entry produces `ABSENT`, which is a red run and a reseed. **Also rejected: raising repository cache retention** through the retention-limit endpoint (F12): it needs an admin credential beyond `GITHUB_TOKEN`, and the docs give no maximum I could verify.

**Recommended answer: (a), no prune, plus the prefix-reservation test, the churn bound written in the ADR, and an age line in the summary. Confidence: HIGH.**

## 6. Q5: non-main branches and dispatch

**Best practice today.** GitHub's guidance: scope isolation by ref (F1), only trusted triggers write the default scope (F2), low-trust runs are read-only (F2), keep secrets out (F3), pin actions by SHA and give jobs least privilege (https://docs.github.com/en/actions/reference/security/secure-use). SLSA names cache poisoning as a Build L3 threat (F6). Scorecard has no cache check (F15); zizmor has one, aimed at release workflows (F16).

**Raise the bar.**
1. Treat the cache as untrusted input (F5, F7) and say so in the ADR: the classifier validates schema, marker, digest and no-regress, so a bad entry fails loud as `INVALID` instead of being believed.
2. Trigger allowlist for the two state workflows: `on:` may contain only `schedule` and `workflow_dispatch`. A test fails if `pull_request*`, `pull_request_target`, `workflow_run` or `issue*` appear. Those are exactly the events GitHub documents as read-only or low-trust (F2), and adding one later is the realistic way poisoning gets in.
3. Gate the save on both ref and event: `github.ref == default branch && (github.event_name == 'schedule' || github.event_name == 'workflow_dispatch')`.
4. Take plan option (c): branch runs restore and classify but never announce, record or save.
5. Simplify the key: because saves happen only on the default branch, drop `github.ref_name` from `key`/`restore-keys` and use the literal prefix `ingester-state-v1-`. A branch dispatch still sees only its own scope plus main's (F1) and never writes. This removes the two-line `restore-keys` explanation and test T-W2's two-ref rendering. Trade-off: if branch saves are ever allowed, entries would no longer be separated by name; scope isolation by GitHub (F1) still separates them.
6. Keep a "no state files hold anything but `version`, `fingerprint`, timestamps and run ids" schema test, because the cache is readable by anyone with read access (F3).

**Screen.** No permission or action. Items 2, 3, 6 are static tests on workflow YAML, the same style as the existing `test_workflow_shell_contract.py`. Pass.

**Conflict: signing the state (artifact attestation) would beat the best practice.** Design: an `actions/attest` step signs a manifest of the state; restore runs `gh attestation verify --repo <o>/<r> --signer-workflow <o>/<r>/.github/workflows/refresh.yml --source-ref refs/heads/main` (F14). That rejects an entry planted by `ci.yml` on push, which is the one poisoning path F7 shows is real. Cost against the constraints:
- Needs `id-token: write` and `attestations: write` on the writing job: new permissions, and Token-Permissions scoring (F15) wants them per job only.
- It does not stop code inside the refresh job itself (the `pip install` runs in the same job, so a compromised dependency could mint the token). Closing that needs a separate attest-only job fed by an artifact: more machinery.
- Public attestations and a public transparency record for a hash (nothing sensitive, but publicly listed; the docs I fetched did not state visibility, not verified).
- Whether `GITHUB_TOKEN` with only `contents: read` suffices for the verify call: not verified.
This is "raise the bar" against "standard over custom, least privilege". The mechanism is vendor-documented, so it is not custom code, but it is real new surface for a threat whose worst case here is one suppressed announcement. **Not decided silently: recommend deferring, with a written trigger** (adopt when refresh is wired and a downstream consumer relies on the announcement, or when any workflow gains a new write trigger). The owner can overrule.

**Rejected: option (a), branch runs that can announce.** An announcement from a branch is not recorded, so main re-announces the same bundle. **Also rejected: reconciling the record against the announcement issues** (a second ledger the poisoner cannot write): issues are mutable by triagers and it is a bespoke cross-check.

**Recommended answer: option (c), the trigger allowlist, the event-and-ref save gate, the literal key prefix, and the "cache is untrusted" ADR statement; attestation deferred as an owner decision. Confidence: HIGH** on the gates (documented), MEDIUM on deferral (a risk judgement).

## 7. Q6: does this need ADR-017

**Best practice today.** Write an ADR for a decision with lasting architectural effect, and do not rewrite accepted ADRs, supersede or append (F17). This repo's README is stricter: a change to an accepted ADR is a new ADR that supersedes it.

**Raise the bar.** Yes, a new ADR-017, and make it do more than record a choice:
1. Include a short threat model: the cache is a public-readable (F3), default-branch-writable (F2, F5), non-integrity-protected store; what the classifier does about it; the residual risks (poisoning by a default-branch workflow, schedule auto-disable F11, 7-day eviction F4).
2. State the data boundary against HR4/C4: only version, fingerprint, timestamps and ids; no corpus content; readable by anyone with read access.
3. List revisit triggers (attestation, a wired refresh with a consumer, any new write trigger).
4. ADR-016's Consequences paragraph says "not decided here". Add one dated forward-pointer line to ADR-016 ("decided in ADR-017") and the README table row. That touches an Accepted ADR, which the README does not explicitly allow for pointers: flag it in the PR for the owner. The pointer carries no decision, so it is an append, not a rewrite (F17).

**Screen.** Documentation only. Pass.

**Rejected: amending ADR-016's decision text.** It mixes the runner decision with the persistence decision and conflicts with the README rule.

**Recommended answer: new ADR-017 with the threat model and revisit triggers, plus a one-line pointer in ADR-016. Confidence: HIGH.**

## 8. Q7: proving the save landed

**Best practice today.** There is none built in: the save step has no outputs and turns every failure into a warning (F8). Common practice is to trust it and notice later. The documented checks are `gh cache list --key` or `GET /actions/caches?key=` (Actions read, F12), and a restore with `lookup-only` (F9).

**Raise the bar: verify with the cache service itself, with no new permission.**
1. After the save, run `actions/cache/restore` again with `key:` set to the exact key just saved, no `restore-keys`, `lookup-only: true`, `fail-on-cache-miss: true`. The service answers from the same runtime token the save used, so no `actions: read`.
2. Do not trust that step alone. F9: when the cache feature is unavailable it returns green with `cache-hit=false`. Add a shell step that fails unless `steps.<id>.outputs.cache-hit == 'true'`. This is the "nothing to do vs not wired" rule applied to the verifier.
3. Make the key carry a content digest: `ingester-state-v1-<run_id>-<run_attempt>-<first 16 hex of sha256 over the sorted (relative path, file sha256) list of the state root>`. `state_verify` computes it and the save key uses it (so the save key is no longer the restore step's `cache-primary-key`; the plan's step 8 changes). On every restore, `check` recomputes the digest and compares it to the suffix of `cache-matched-key`; a mismatch is `INVALID` (`digest-mismatch`).
   - It catches a real gap in the plan: a truncated or partly extracted archive that lost `last-published-bundle.json` but kept `store.json` classifies as `SEEDED` today and re-announces. The digest turns that into red.
   - It gives a cross-run proof of bytes: health's recomputed digest equals the digest in the key refresh wrote, with no API and no extra permission.
   - It is **not** a defence against an attacker, who can compute a digest too (F5). Say so in the ADR.
4. Write `save-attempted-key`, `save-verified=true|false` and `digest` to the run summary, and keep the plan's T-R2 as the runtime check.
5. Optional stronger step, off by default: before the verify restore, move the state root aside, restore the exact key, and `diff -r` against the moved copy. This proves the bytes round-trip, at the cost of a few shell lines. Recommend it only for the seed run (once), not weekly.

**Screen.**
- Item 1-2: vendor inputs plus one shell assertion; no permission, no new action (the same `actions/cache/restore` SHA). Pass. I found no documented precedent for restore-after-save as a verifier: it is an application of documented inputs, and read-after-write consistency of the cache service is **not documented**. Test it on the first dispatches (T-R1/T-R2 run it N times); if it ever misses a just-saved key, fall back to the REST check below.
- Item 3: roughly 20 lines in `state_store.py`; custom code, but inside a module the plan already adds. No permission. The key length limit is not a concern at ~100 characters (limit not verified here).
- Item 5: shell only.

**Conflict.** Items 2-3 add small custom logic where the plan chose "report save-attempted-key and let the next run confirm". That choice is the most standard, but its failure shows up to 24 hours later and only as a stale key. The digest and the same-run lookup cost about 30 lines and no permission; recommend them, flag it as the one place raising the bar adds code.

**Rejected: the REST check (`gh cache list --key K --json key` / `GET /actions/caches?key=`) as the primary proof.** It needs `actions: read` on the writing job plus `GH_TOKEN` (F12), a new scope on the job that also holds `issues: write`, and a second API with its own consistency window. Keep it as the documented fallback if the lookup-only step proves flaky; `actions: read` is the justification then (read the list, no write).

**Recommended answer: exact-key `lookup-only` + `fail-on-cache-miss` + explicit `cache-hit == 'true'` assertion after every save, a content digest in the key re-checked on every restore, and the summary fields. Confidence: MEDIUM** (all inputs and outputs verified in source at the pinned SHA; the post-save visibility timing is not documented and must be measured).

## 9. Conflicts between "raise the bar" and "standard over custom"

| # | Where | Raise-the-bar idea | Standard alternative | Recommendation |
|---|---|---|---|---|
| C1 | Q7 | Digest in the key, same-run lookup verifier (~30 lines custom + shell assertion) | Report `save-attempted-key`, verify at the next health run | Take the raise-the-bar version: no permission, catches truncated archives. Owner may prefer the plan's version |
| C2 | Q5 | Artifact attestation of the state, verified by `--signer-workflow` | Cache treated as untrusted, validated, no signing | Defer. Needs `id-token: write` + `attestations: write`, still not safe against the refresh job's own dependencies without a separate attest job. Written revisit trigger. Owner decision |
| C3 | Q2 | 8-day `stale` rule in `health_check.py` | Absent/present only | Take it: ~5 lines, no permission |
| C4 | Q4 | REST pruning of old entries | Rely on eviction | Rejected, no conflict left: needs `actions: write` and risks deleting the good entry |
| C5 | Q6 | Pointer line in an Accepted ADR-016 | Pure supersede | Take the pointer, flag for owner because the README does not explicitly permit it |
| C6 | Q1 | Protected environment for the seed | Dispatch + branch guard | Rejected (F13: silent auto-create) |

## 10. New permissions the recommended design needs

None. `refresh.yml` keeps `contents: read` and `issues: write`; the seed job and `health.yml` get `contents: read`. Cache restore, save and the lookup-only verify use the runner's cache token, not `GITHUB_TOKEN` scopes (F5; the actions/cache README asks for no `permissions:` entry). Not needed: `actions: read` (only if the REST fallback in Q7 is ever adopted), `actions: write` (only for pruning, rejected), `id-token` and `attestations` (only if C2 is taken).

## 11. Additional items for the plan author (found while checking)

1. The pinned SHA in the plan is correct (F10). The implementer should still re-run the `gh api` check at implementation time, as the plan says.
2. Plan step 8 saves under `steps.state_restore.outputs.cache-primary-key`; with the digest in the key (Q7) the save key is built from `state_verify` output instead, and T-W2/T-W5 change accordingly.
3. `ci.yml` (push to main, `pip install`) is a trusted cache writer (F7). The prefix-reservation and trigger-allowlist tests (Q4, Q5) are the cheap defence in this repo.

## Recommendation (summary)

Q1 explicit manual seed on the default branch with provenance and a verified save; Q2 warn while unwired, red once wired, plus an 8-day stale rule; Q3 save on `!cancelled()` with the seeded/present allowlist and a no-regress rule; Q4 no pruning, bound and measure instead; Q5 option (c), trigger allowlist, event-and-ref save gate, untrusted-cache stance, attestation deferred; Q6 new ADR-017 with a threat model; Q7 digest-in-key plus exact-key lookup-only verification.

**Trade-off:** about 50 lines of extra code and tests in `state_store.py` and `health_check.py`, in exchange for a save that is proven in the same run, truncated archives that fail loud, and no new permission.
**Confidence:** HIGH on Q1, Q3 (gate), Q4, Q5 (gates), Q6; MEDIUM on Q2 (threshold), Q7 (post-save timing not documented) and on deferring C2.
