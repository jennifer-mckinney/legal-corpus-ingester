# legal-corpus-ingester

Standalone tool that ingests, cleans, chunks, embeds, and publishes legal statute + court-judgment corpus consumed by [terms-analysis](https://github.com/jennifer-mckinney/terms-analysis).

See `docs/plans/2026-07-04-legal-corpus-ingester.md` in the terms-analysis repo for the full implementation plan.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
ingester init
ingester status
```

## Development

```bash
# Run tests
pytest

# Run governance checks
bash scripts/governance/verify-hashes.sh
bash scripts/governance/sync-lib-principles.sh
```
