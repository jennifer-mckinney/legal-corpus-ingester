# automations/

One-page documentation for every automated behavior in this repo.

Each file documents: trigger, artifact produced, failure mode, and escalation path.

| File | Trigger | Purpose |
|------|---------|---------|
| `pre-commit.md` | `git commit` | Pre-commit hook checks |
| `ci-pr.md` | GitHub PR | CI on pull request |
| `logging.md` | Runtime | Structured logging setup |
| `docker.md` | Build | Docker build automation |
| `self-hosted-runner.md` | Setup | GitHub Actions self-hosted runner |
| `secrets.md` | Configuration | Secrets management |
| `p9-pre-push.md` | `git push` | P9 security + grumpy pre-push gate |

Additional automations added in Phase 0.1 Tasks 30-35:
- `health-check.md` — nightly health check
- `refresh.md` — weekly corpus refresh
- `vcr-drift.md` — weekly VCR cassette drift canary
- `approval-expiry.md` — approval expiry watcher
- `license-drift.md` — license drift audit
- `publish-handoff.md` — symlink flip + SIGHUP publish handoff
