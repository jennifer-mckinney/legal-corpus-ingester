# VCR drift canary

## Purpose

VCR cassettes are snapshots of live upstream HTTP responses recorded at
ingest time and committed to the repo. They let integration tests run
offline and deterministically. Over time, upstream sources restructure
URLs, change response schemas, add or remove XML elements, or alter
headers. When that happens, the cassettes in the repo no longer match what
a fresh fetch would produce. The drift canary exists to surface that
divergence before it causes silent corpus corruption.

The canary re-records all cassettes against live sources on a weekly
schedule, diffs the results against the committed versions, and escalates
when anything changed.

## Trigger

GitHub Actions cron `0 4 * * 0` - Sunday 4 AM UTC. The weekly cadence
balances detection latency against the cost of a live fetch run. It also
runs on `workflow_dispatch` so any team member can trigger it manually
before cutting a release or after a source announces a schema change.

## What it does

The workflow runs in three phases.

**Phase 1 - stash baseline.** Before touching the cassette directory, the
workflow copies `tests/fixtures/cassettes/` to `/tmp/vcr-baseline/` on the
runner. This preserves the committed state so the diff in phase 3 has
something to compare against.

**Phase 2 - re-record.** The integration test suite runs with
`--vcr-record=all`, which forces `pytest-recording` (wrapping `vcrpy`) to
discard each cached cassette and make a live HTTP request. The resulting
cassette files are written back to `tests/fixtures/cassettes/` in place.
If any source returns 4xx or 5xx, the fetch raises and the pytest exit code
is non-zero. A failed re-record exits the workflow immediately; no drift
report is produced and no issue is opened (see Failure mode below).

**Phase 3 - diff and report.** `scripts/vcr_drift_report.py` compares the
freshly written cassettes against the baseline stash. For each cassette
present in both dirs it inspects `interactions[*].request.uri` and
`interactions[*].response.body.string`. It also notes cassettes that exist
in one dir but not the other. The script writes a markdown drift report to
`drift-report.md` and exits non-zero if any cassette changed.

## What it produces

**Artifact.** `drift-report.md` is uploaded as a GitHub Actions artifact
named `vcr-drift-report-<run_id>` on every run regardless of outcome
(`if: always()`). This gives a persistent audit trail of what was checked,
even when no drift was found.

**Issue.** If the drift-report step exits non-zero (cassettes changed), the
workflow opens a GitHub issue titled "VCR drift detected YYYY-MM-DD" with
the full drift report as the issue body. The issue is labeled
`corpus-drift` and `needs-review`.

## How to read the drift report

The report groups cassettes into four buckets:

| Bucket | Meaning |
|--------|---------|
| Unchanged | Cassette is bit-for-bit identical; upstream is stable. |
| Changed | At least one interaction differs; inspect carefully. |
| Baseline only | Cassette exists in repo but not after re-record. Endpoint may be gone. |
| Current only | New cassette recorded that has no baseline counterpart. Endpoint added. |

When inspecting a "Changed" cassette:

- URI drift in `interactions[*].request.uri` indicates a URL restructuring
  on the upstream source. All downstream config pointing at the old URI
  needs updating.
- Body structure drift in `interactions[*].response.body.string` indicates
  a schema change - different XML element names, moved fields, altered
  envelope format. The cleaner for that source likely needs updating too.
- Header-only changes (Content-Type charset additions, caching headers,
  server version strings) are typically noise. The drift report notes them
  but they do not by themselves indicate a corpus integrity risk.

## Escalation

**Drift detected.** An issue is opened with the drift report. A human
reviews the diff and determines whether the upstream change is compatible
with the current cleaner or requires a schema update. Until the issue is
closed, the affected source is considered unreliable.

**Severe drift.** If the upstream format changed in a way that the current
cleaner cannot handle (for example, the GDPR XML envelope switched from
AkomaNtoso to a proprietary schema), the maintainer marks the source as
publish-blocked and updates `ALERTS.md` with the source name, the detected
change, and the date. The publish pipeline treats a publish-blocked source
as a hard failure per HR5.

**No drift.** The artifact is uploaded. No issue is opened. The workflow
exits 0. The run is evidence of a healthy upstream snapshot.

## Failure mode

If the re-record step fails because an upstream source returned an HTTP
error (4xx client error or 5xx server error), the workflow exits non-zero.
The cassettes in the working directory are in an undefined state. No drift
report is generated and no issue is opened - the failure is visible in the
workflow run log directly. This distinguishes "upstream is down right now"
from "upstream changed its schema": the former fails the re-record, the
latter succeeds the re-record but changes the cassette contents.

## Why self-hosted runner

Hard requirement HR4 mandates that all corpus fetches remain local. A
GitHub-hosted runner executes on Azure infrastructure, which means the HTTP
requests to EUR-Lex, Congress.gov, or other sources would originate from
and transit through Azure. The cassette re-record makes live HTTP calls to
each source. Running on the self-hosted `legal-corpus-ingester` runner
keeps those fetches on-machine, consistent with every other ingest
workflow in this repo.

## Relationship to ALERTS.md

The drift canary is the automated tripwire. `ALERTS.md` is the human
escalation record. When the canary fires and a human confirms the drift
constitutes a schema change, they update `ALERTS.md` and set publish to
blocked for the affected source. The issue opened by the canary references
`ALERTS.md` as the escalation target.

## Running manually

```bash
# Stash current cassettes
mkdir -p /tmp/vcr-baseline
cp -r tests/fixtures/cassettes/ /tmp/vcr-baseline/

# Re-record against live sources
source .venv/bin/activate
pytest tests/integration/ --vcr-record=all -v

# Generate the drift report
python scripts/vcr_drift_report.py \
  --baseline /tmp/vcr-baseline/cassettes \
  --current tests/fixtures/cassettes \
  --output drift-report.md

# Inspect
cat drift-report.md
```

Restore committed cassettes afterward with `git checkout -- tests/fixtures/cassettes/`
so the working tree does not contain live-recorded cassettes that differ from
the committed baseline.
