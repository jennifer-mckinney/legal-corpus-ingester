---
name: corpus-publish
description: Guided workflow for cutting and publishing a versioned corpus bundle. Use when asked to "publish corpus", "cut a bundle", "publish to terms-analysis", or "release corpus". Verifies all sources fresh, runs round-trip validation, executes publish, flips symlink, and verifies consumer health.
allowed-tools: Bash, Read, Grep, Glob
---

# Corpus Publish Workflow

## Pre-publish checklist

1. **All sources fresh** — run `ingester status`; confirm no source is stale (last fetch < 7 days)
2. **No ALERTS.md entries** — `cat out/current/ALERTS.md` should be empty or non-existent
3. **Tests passing** — `pytest` exits 0
4. **Governance manifest intact** — `bash scripts/governance/verify-hashes.sh` exits 0

## Steps

1. **Validate round-trip**
   ```bash
   ingester validate-round-trip out/current
   ```
   Expected: parse → chunk → embed → serialize → deserialize → terms-analysis retrieve → non-empty result

2. **Publish bundle**
   ```bash
   ingester refresh --publish
   ```
   This writes `out/YYYY.MM.PATCH/`, flips `out/current` symlink atomically (ADR-004 + ADR-012).

3. **Verify symlink**
   ```bash
   ls -la out/current  # should point to new bundle
   cat out/current/MANIFEST.yaml  # verify corpus_version bumped
   ```

4. **Signal terms-analysis**
   ```bash
   # SIGHUP the FastAPI process (or POST /reload as fallback — ADR-012)
   kill -HUP $(pgrep -f "uvicorn app.main:app") 2>/dev/null || \
     curl -s -X POST http://localhost:8000/reload
   ```

5. **Verify consumer health**
   ```bash
   curl -s http://localhost:8000/health | jq '.corpus_version'
   ```
   Confirm `corpus_version` matches the new bundle version.
   Check for absence of `X-Corpus-Mismatch` header (ADR-014).

6. **Commit state**
   ```bash
   git add state/manifest.json state/
   git commit -m "chore(corpus): publish bundle YYYY.MM.PATCH"
   ```

## If publish fails

- Round-trip validation fails → check embedder model alignment; verify LocalAI is running with correct model SHA
- MANIFEST mismatch on consumer → `X-Corpus-Mismatch` header names the drift dimension; re-run with matching embedder_model
- Symlink flip fails → check disk space; check `out/` write permissions

## References

- `@.claude/library/LIB-ARCH.md` — Publisher Protocol + MANIFEST schema (ADR-008) + publish mechanism (ADR-004 + ADR-012)
- `automations/publish-handoff.md` — publish automation contract
