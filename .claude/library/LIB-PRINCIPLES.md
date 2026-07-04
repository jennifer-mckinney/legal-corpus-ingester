# LIB-PRINCIPLES — governance & working principles
loads: auto
scope: project
xref: [[.claude/CLAUDE.md]] [[docs/BRD_Terms_Policies_Reviewer.md]] [[docs/PRD_Terms_Policies_Reviewer.md]] [[PRODUCT.md]] [[LIB-VOICE]] [[LIB-CONTEXT]] [[PEAS]] [[_AUTOMATION/CLAUDE.md#governance-rules]]

## principles

### P1: no-inference-ask
rule: never infer user intent, scope, priorities, file paths, feature semantics, or acceptance criteria — if not documented in BRD, PRD, PRODUCT, an existing LIB, or explicit prior conversation, ask before executing
apply_when:
  - writing a spec section (verify each claim traces to BRD/PRD/PRODUCT/LIB)
  - implementing a feature (verify presence in BRD/PRD)
  - deciding file paths, section structure, naming (check `docs/`, `.claude/library/`, `.claude/rules/`)
tool: `AskUserQuestion` for 2-4 discrete options; free-text for open option space
batch: prefer one round of 4 questions over four sequential rounds of 1
because: silent inference produces work that looks right, ships, reveals misalignment weeks later; asking costs one round-trip, unwinding costs a redesign
rejected_alt_1: ask only when uncertain — "uncertain" is itself an inference
rejected_alt_2: ask only when consequential — small decisions compound

### P2: always-anchor-to-BRD-PRD
rule: load and cross-reference `docs/BRD_Terms_Policies_Reviewer.md` + `docs/PRD_Terms_Policies_Reviewer.md` before executing any task touching architecture / features / data model / UI copy / verdict semantics / jurisdictions / context chips / scope box
if_no_anchor: name the drift, ask before proceeding
apply_when:
  - session start (skim BRD + PRD TOC)
  - business-logic edit (locate the requirement, cite in commit/PR)
  - drafting docs/specs (name BRD/PRD sections it traces to)
resolutions_when_task_maps_nowhere:
  (a) intentional expansion → update BRD/PRD first
  (b) mistake → reject the task
  (c) unstated goal → capture in follow-up doc, get into BRD/PRD before implementation
enforcement: review-based today; pre-commit hook / PR-template checkbox proposed
because: BRD + PRD are constitutional; every other doc descends from them; skipping produces drift

### P3: surface-drift-do-not-silently-execute
rule: when a request drifts from BRD/PRD, name the drift out loud and ask before proceeding
apply_when:
  - request would add a field/endpoint/chip/jurisdiction/domain/UI state not in BRD/PRD → pause, ask
  - request implies removing/reversing documented behavior → name documented behavior, cite anchor, ask
framing: neutral, not corrective — "this request would add X, which I don't see in BRD/PRD — intentional expansion or scope down?"
not_a_reject_rule: drift is not automatically wrong; anchor docs may be stale — surface it, do not reject it
examples:
  drift_to_reject: hardcoded US-CA default jurisdiction contradicts shipped `jurisdictions=[]` = no-filter
  drift_to_escalate: new `for_medical_provider` chip not in BRD taxonomy
  drift_to_update: PDF export beyond PRD spec — update PRD to name format, then implement

### P4: hard-scope-limits-non-negotiable
rule: tool analyzes document text only; the runtime-permission scope limit surfaces verbatim, always, in the "what else worth checking" note in results
limit_runtime_permissions: camera / microphone / contacts / location — tool reads policy text, not install-time permission requests; readers directed to (a) the app's Terms of Use, (b) App Store Privacy Nutrition Label / Play Store Data Safety section, (c) install-time permissions in device Settings
requests_to_expand_these: drift under P3 — surface and ask
because: analyzing the policy IS analyzing the contract. "Real-world practice divergence" was previously listed as a second limit; dropped 2026-07-03 per user directive on grounds that behavior monitoring is a separate discipline (compliance monitoring, breach research, investigative journalism), not a scope limit of a policy-analysis tool. Conflating tool scope with due-diligence-in-general read defensive and mixed categories.
retro_anchor: user review 2026-07-03 following Phase 5.d E2E; codified in docs/plans/2026-07-03-results-view-revamp-report-card.md §7 D-Q9
xref: [[LIB-VOICE#V11]]

### P5: local-only-open-source-only
rule: hard requirements restated from `.claude/CLAUDE.md`
  - all dependencies open source (Apache 2.0, MIT, BSD preferred)
  - no tools from companies facing investor lawsuits (excludes Meta-origin, no FAISS)
  - all dependencies IRP Grade A or higher
  - all data local; no external API calls
  - LLM failures fall back to rule-only findings
  - no OpenAI; LLM inference local-only via LocalAI (Apertus-8B, EuroLLM-22B)
  - confidence < 0.80 triggers human-in-the-loop review
violations: drift under P3 — surface and ask
xref: [[.claude/CLAUDE.md#hard-requirements]] [[LIB-STACK]] [[LIB-LEGAL]]

### P6: two-voice-no-em-dash-tentative
rule: restated from LIB-VOICE
  - intake first-person warm; results third-person observational
  - no em-dashes (`—`, U+2014) in tool voice
  - tentative framings ("may / perhaps / possibly / might"); never "you should / we recommend / the tool determined"
  - verdict labels actionable ("Worth a closer read"), not grades ("USE CAUTION")
  - scope box always visible, never collapsible
xref: [[LIB-VOICE]]

### P7: attribution-and-personal-path-hygiene
rule: restated from `~/.claude/CLAUDE.md` and my-skills project rules
  - every commit includes `Co-Authored-By: Jennifer McKinney <jennifer.mckinney@croiai.com>` and `Co-Authored-By: Claude <noreply@anthropic.com>`
  - no personal paths (`/Users/<name>/…`) in public documentation, examples, or committed files
  - no emojis in professional documentation unless explicitly requested
  - credit Shawn Peng for Mermaid MCP Server in diagram-related commits and documentation

### P8: agent-separation-of-duties
rule: no single agent writes code AND spec-conformance tests AND signs off on its own work
scope: one ask per agent; agents run in isolation; agents do not know about each other
orchestrator_visibility: only orchestrator (Claude) knows about all agents; agents have no visibility to other agents' existence, output, prompts, or scope; cross-agent coordination is orchestrator's sole responsibility
roles:
  coder: implements per spec; MAY write unit tests for own code (structural, edge-case); MUST NOT write spec-conformance tests; MUST NOT sign off
  test_helper: writes spec-conformance tests from spec only; no visibility to Coder's diff, unit tests, or output; existing spec-conformance tests written by prior Test Helper role can be reused (do not re-dispatch when the test already codifies the spec)
  critic: runs Coder's unit tests + Test Helper's spec-conformance tests against Coder's code; reports pass/fail with diffs; no authority to modify code or tests; no signoff authority
  decision: orchestrator (Claude) or user; only role with signoff authority
override_authority: only orchestrator (Claude) can grant override permissions to any role; agents cannot self-authorize; orchestrator receives override authority only when user explicitly grants it in-session per task
peas_accountability: orchestrator (Claude) is accountable to uphold PEAS philosophy and quality of dispatched tasks; every dispatch MUST have a clear single-line performance measure (P) and explicit environment/scope bounds (E-lite); actuators (A) are implicit from `subagent_type` and tool-gating; sensors (S) are implicit from prompt + tool-result stream; PEAS full quartet at [[PEAS]] applies to agent DESIGN, not per-prompt ceremony
peas_failure_mode: if a dispatched agent produces vague, out-of-scope, or multi-ask output, orchestrator is at fault for weak PEAS discipline in the dispatch prompt — not the agent
orchestrator_agent_audit: after each agent returns, orchestrator (Claude) MUST read the agent's report AND spot-check claimed changes on disk (file mtimes, grep for asserted changes, diff summary); scope drift, unexecuted-but-claimed tasks, or vague success measures surface here; low-touch operational practice, not a tool
enforcement: prompt-based today; automation follow-ups tracked
proposed_automation:
  - `/dcd-dispatch` skill generates role-scoped prompts from single spec with PEAS bounds
  - session-handoff schema records which agent played which role per merged chunk
  - PR-template checkbox: "reviewed by a distinct Critic agent"
because: closed loop between Coder and its own spec-tests proves internal consistency, not correctness against requirements; multi-ask agents lose focus and cross role boundaries; agents that know about each other silently coordinate outside orchestrator's visibility
rejected_alt_1: Doer runs its own spec-conformance tests as a shortcut → this is the failure mode being prevented
rejected_alt_2: Critic optional when pytest green → green pytest from Coder's own tests is not evidence of correctness
rejected_alt_3: Orchestrator plays Test Helper when Orchestrator is Claude → collapses Decision + Test Helper into single instance
rejected_alt_4: Agents share context to coordinate → violates orchestrator_visibility; coordination is orchestrator's job alone
rejected_alt_5: Skip Test Helper for small changes → dropped 2026-07-03; existing spec-conformance tests can be reused instead
retro_anchor: codified 2026-07-03 after tech-spec audit remediation; refined same day — Coder MAY write unit tests, no skip_test_helper, override authority orchestrator-only, one ask per agent, orchestrator sole knower of all agents, PEAS as orchestrator accountability (design-time discipline) not per-prompt ceremony
xref: [[PEAS]] [[LIB-TEST]] [[_AUTOMATION/CLAUDE.md#multi-agent-architecture]]

### P9: pre-push-independent-review
rule: before ANY push to remote, orchestrator MUST dispatch security-engineer + grumpy-developer agents (or explicit equivalents) to independently review the assembled commit(s); hard requirement, not optional
scope: applies to feature, release, hotfix branches; applies whether push is one commit or many; applies to first push AND any subsequent push
review_agents:
  security-engineer: STRIDE-style threat-model review — auth, secrets, user input, RLS, CSP, dependencies, session/cookie state, migration safety, endpoint deprecation contract
  grumpy-developer: blunt code-quality review — swallowed errors, dead code, brittle assumptions, missed edges, tautological tests, dispatch-boundary artifacts from multi-Doer sessions
custom: reviewer prompts MUST be customized to the actual diff and session context, not defaults
gate: security-engineer AND grumpy-developer findings of ANY severity (CRITICAL / HIGH / MEDIUM / LOW / NIT) block push and MUST be fixed — zero tolerance for BOTH per 2026-07-04 user directive, no follow-up-issue path; either gate resolves via (a) fix-Coder + re-verify (the loop-pattern below), or (b) explicit user override in-session per P8 override_authority via `override.used=true` in the signoff JSON
security_findings_zero_tolerance: user directive 2026-07-03 — every security-engineer finding is a fix-now item regardless of severity; codified after F1-F7 review where two MEDIUMs would have shipped as follow-ups under prior gate
because: local pytest + orchestrator spot-check is not sufficient for pushed code; two independent adversarial reviewers catch what dispatch Coders and orchestrator miss; especially load-bearing after multi-agent sessions where domain boundaries were crossed
enforcement: prompt-based today; automation follow-up: pre-push git hook that refuses push until a signed reviewer-log exists for the current HEAD
loop_pattern: (amendment 2026-07-04) every push runs this fixed-point loop until convergence
  1. Coder(s) implement the change → commit LOCALLY (no push)
  2. Orchestrator dispatches security-engineer + grumpy-developer IN PARALLEL on the delta
  3. If EITHER reviewer returns findings of ANY severity → dispatch fix-Coder scoped to those findings → commit locally → GOTO 2
  4. If BOTH reviewers return zero findings → orchestrator writes `.git/reviews/<HEAD-SHA>.signoff.json` → push
automation: `.githooks/pre-push` (both repos) enforces the signoff schema; missing or non-PASS signoff hard-refuses push. See `automations/p9-pre-push.md`
whack_a_mole_avoidance: when a name-based deny-list / exact-match rule keeps growing across rounds (each round finds another edge case), switch to a STRUCTURAL fix — pattern-based rules, schema-driven ordering, input normalization. Structural fixes close classes of attack instead of individual names. Two structural-fix wins on 2026-07-04:
  - terms-analysis grumpy F2: switched `_ACTION_ITEMS_BY_CHIP.items()` iteration → `typing.get_args(ContextChip)` schema-driven order
  - ingester rounds 6-9 logging redaction: added `_REDACT_SUFFIXES` + `_normalize_key(camelCase→snake_case)` instead of growing exact-name list further
loop_failure_mode: if the loop does NOT converge after 3-4 rounds on the same axis, PAUSE and ask user whether to keep patching or switch to a structural fix. Silent iteration is a Coder-role violation.
retro_anchor: codified 2026-07-03 (security zero-tolerance) then extended 2026-07-04 (grumpy zero-tolerance + loop_pattern + whack_a_mole guidance)
xref: [[P8]] [[LIB-TEST]]

## enforcement-summary
today: review-based; no hook or linter enforces any of P1-P8
proposed:
  - pre-commit hook: reject logic-touching commits with no BRD/PRD anchor in message
  - PR template: require BRD/PRD anchor for `feat:` and `refactor:`
  - `/em-dash-scan` skill
  - `/scope-drift-check` skill: cross-reference PR diff against BRD/PRD

## reference-anchors
- `docs/BRD_Terms_Policies_Reviewer.md` — Business Requirements (~47KB)
- `docs/PRD_Terms_Policies_Reviewer.md` — Product Requirements (~80KB)
- `PRODUCT.md` — brand personality, target users, design principles
- `.claude/CLAUDE.md` — project identity, hard requirements, reference library index
- `.claude/library/LIB-VOICE.md` — copy voice (P6 detail)
- `.claude/library/LIB-CONTEXT.md` — context chip taxonomy, weight tiers
- `.claude/library/LIB-{ARCH,STACK,LEGAL,TEST,API,RULES,EVAL}.md` — domain reference
- `.claude/rules/code-style.md`, `.claude/rules/testing.md` — code style + 3-rule testing policy
- `_AUTOMATION/CLAUDE.md` — hub-level governance (autonomy thresholds, constitutional docs)
