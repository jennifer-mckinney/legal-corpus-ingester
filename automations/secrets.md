# secrets.md -- managing `.env` and CI env vars

Purpose: document how `.env` is managed locally, the rotation policy for
values inside it, and how CI reads environment variables without moving
data off-host.

## How to manage `.env`

- Copy `.env.example` to `.env` at the repo root.
- Edit `.env` locally with values appropriate to your machine.
- Never commit `.env`. It is covered by `.gitignore` at the repo root
  (verified in Task P7).
- If you find `.env` appearing in `git status`, stop and re-check the
  `.gitignore` rule before staging anything else.

## Rotation policy

When a value in `.env` needs rotating (for example the LocalAI URL
changes, the served model name changes, or a legal-review override flag
needs revocation):

- Replace the value in the local `.env` file.
- Restart any long-running processes that read env at startup (the
  ingester CLI, any local dev server, any background runners).
- If the value was an override flag, remove the line entirely rather
  than commenting it out, so future audits do not misread intent.

There is no shared secret store to update because `.env` values stay on
the developer or runner host by design (see next section).

## How CI reads env vars

CI runs on a self-hosted runner via
`runs-on: [self-hosted, legal-corpus-ingester]` (see
`automations/self-hosted-runner.md`). The runner reads environment
variables from the host process environment.

CI does not use GitHub-hosted secrets. Storing values in GitHub secrets
would move data to Azure/Microsoft infrastructure, which appears to
conflict with HR4 (all data stays local, no external API calls). Values
needed at CI time should be set on the runner host, for example in the
runner service unit or the shell profile the runner uses.

See `automations/self-hosted-runner.md` for the host-side management
steps.

## Legal-review overrides

`INGESTER_LEGAL_REVIEW_APPROVED_<source>` env vars are an emergency-only
bypass for the APPROVAL.yaml gate that ADR-007 defines (arriving in
Phase 0.1 Task 26).

The preferred path is APPROVAL.yaml with `signed_artifact_sha256`. Use
the env-var override only when a signed APPROVAL cannot be produced in
time (for example, an urgent statute deadline where the reviewer is
unavailable). When the env-var override is used:

- Record the reason in the ingestion run's state file.
- Follow up with a signed APPROVAL.yaml after the fact.
- Remove the override flag from `.env` once the signed APPROVAL lands.

**Audit logging requirement:** Any code that reads `INGESTER_LEGAL_REVIEW_APPROVED_*` MUST emit a structured log entry at `WARNING` level containing `source_id`, `override_env_var_name`, `timestamp`, and `run_id` before proceeding. This is enforced at P9 review time for Phase 0.1 Task 26.

## What NOT to put in `.env`

- No personal filesystem paths. Use repo-relative paths (`./out`,
  `./state`, `./config`) so `.env.example` remains portable.
- No auth tokens for external services. HR4 excludes external services
  from the data path. If a future feature appears to need an external
  credential, it needs to be re-evaluated against HR2 (no
  investor-lawsuit vendors) and HR4 (local-only data) before any token
  is added here.
- No third-party API keys. Same reasoning as above.

If a value seems to belong in `.env` but conflicts with the constraints
above, treat that as a signal to open an ADR rather than to add the
value quietly.
