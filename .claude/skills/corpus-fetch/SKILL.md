---
name: corpus-fetch
description: Guided workflow for fetching a legal corpus source. Use when asked to "fetch corpus", "refresh <source>", "ingest GDPR", "add a new source", or "update <jurisdiction> corpus". Walks through license verification, VCR cassette check, fetch execution, FetchResult validation, and provenance commit.
allowed-tools: Bash, Read, Grep, Glob, WebSearch, WebFetch
---

# Corpus Fetch Workflow

## Steps

1. **Identify source** from `$ARGUMENTS`
   - Look up source config in `config/sources/<source_id>.yaml`
   - If config doesn't exist: prompt user to create it first

2. **License verification** (HR8 + HR9)
   - Read `sources/registry.yaml` for SPDX + `license_text_sha256`
   - If license risk flagged: verify `APPROVAL.yaml` exists + `today < expiry`
   - If APPROVAL.yaml missing: STOP — dispatch researcher agent to investigate

3. **VCR cassette check**
   - Check `tests/fixtures/cassettes/<source_id>/` for existing cassettes
   - If missing: first run will record (`record_mode='new_episodes'`)
   - In CI: cassettes MUST exist (`record_mode='none'`)

4. **Execute fetch**
   ```bash
   ingester fetch <source_id>
   ```
   Watch for: HTTP status != 200, SPDX mismatch, byte_count drift > 10%

5. **Validate FetchResult**
   - HTTP 200 returned
   - `content` non-empty
   - `license_spdx` matches registry entry
   - Provenance record written to `state/`

6. **Commit cassette + provenance**
   ```bash
   git add tests/fixtures/cassettes/<source_id>/ state/
   git commit -m "chore(<source_id>): record VCR cassette + provenance snapshot"
   ```

## If fetch fails

- 404 → Source URL changed; check upstream. Update `config/sources/<source_id>.yaml`. Re-run.
- SPDX mismatch → HR8 violation. Do NOT proceed. Create a drift ticket / open ADR-006 update PR.
- byte_count > 50% drop → likely source restructure. Compare against cassette. Open issue.

## References

- `@.claude/library/LIB-ARCH.md` — Fetcher Protocol + FetchResult schema
- `@.claude/library/LIB-STACK.md` — httpx + VCR.py
- `docs/plans/2026-07-04-legal-corpus-ingester.md` (in terms-analysis) — per-source task specs
