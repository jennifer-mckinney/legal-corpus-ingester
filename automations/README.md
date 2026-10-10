# automations/

One-page documentation for every automated behavior in this repo.

Each file documents: trigger, artifact produced, failure mode, and escalation path.

| File | Trigger | Purpose |
|------|---------|---------|
| `pre-commit.md` | `git commit` | Pre-commit hook checks |
| `ci-pr.md` | GitHub PR | CI on pull request |
| `logging.md` | Runtime | Structured logging setup |
| `docker.md` | Build | Docker build automation |
| `secrets.md` | Configuration | Secrets management |
| `p9-pre-push.md` | GitHub PR to `main` | P9 security + grumpy review CI jobs (`p9-review.yml`) |
| `health-check.md` | Cron 3 AM UTC daily | Nightly corpus health check |
| `refresh.md` | Cron Sun 3 AM UTC | Weekly corpus refresh |
| `vcr-drift.md` | Cron Sun 4 AM UTC | Weekly VCR cassette drift canary |
| `approval-expiry.md` | Cron daily | Approval expiry watcher |
| `license-drift.md` | On demand / CI | License drift audit |
| `publish-handoff.md` | Post-refresh | Symlink flip + SIGHUP publish handoff |
