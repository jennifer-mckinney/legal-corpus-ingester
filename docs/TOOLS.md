# External Tool Prerequisites

All tools must be present before running any ingester command or CI workflow.

## Required tools

| Tool | Purpose | Install | Version pin |
|------|---------|---------|-------------|
| Python 3.10+ | Runtime | `brew install python@3.12` | ≥3.10 required |
| Docker Desktop / colima | Container builds + local runner | `brew install --cask docker` | latest |
| Docker Compose v2 | Multi-service orchestration | bundled with Docker Desktop | v2+ |
| `gh` CLI | Repo create, PR, runner registration | `brew install gh` | ≥2.40 |
| Git | VCS | system (Xcode CLT) | ≥2.30 |
| LocalAI + Apertus-8B | Embedding inference (HR6) | see `automations/localai-setup.md` | model SHA pinned in MANIFEST |
| GitHub Actions self-hosted runner | CI on-host (HR4) | Task P2 — `bash scripts/setup_runner.sh` | 2.335.1 |
| `jq` | JSON parsing in governance scripts | `brew install jq` | any |
| `shellcheck` | Shell-script lint | `brew install shellcheck` | any |

## Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'      # pyproject.toml lands at Phase 0.1 Task 2
```

## Post-install verification

```bash
bash scripts/install-hooks.sh          # pre-commit hook (P9 review runs in CI)
bash scripts/governance/verify-hashes.sh  # governance manifest intact
docker build -t legal-corpus-ingester:dev .  # Docker baseline
```

## MCP tools available during Claude sessions

These are available in the user's global Claude Code config and can be used during development sessions:

| MCP tool | Provider | Purpose |
|----------|---------|---------|
| `mcp__mermaid__generate` | Shawn Peng's Mermaid MCP | Diagram generation (forest theme) |
| `mcp__llm__perplexity_*` | LLM MCP (Perplexity sonar/sonar-pro) | Cross-checking upstream statute claims, license research |
| `mcp__llm__gemini_*` | LLM MCP (Gemini) | Alternative research queries |
| `mcp__plugin_playwright_playwright__*` | Playwright MCP | E2E testing when UI lands (later phases) |

## Self-hosted runner

Runner ID: `2` (MacBook-Pro, labels: `self-hosted, legal-corpus-ingester, macOS, ARM64`)
Status: managed as launchd service via `~/actions-runner/` — see `automations/self-hosted-runner.md`.

```bash
# Verify runner is online
gh run list --repo jennifer-mckinney/legal-corpus-ingester --limit 3
```
