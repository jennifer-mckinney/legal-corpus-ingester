---
name: ralph-loop
description: Start an iterative self-referential development loop. Use when asked to "ralph loop", "iterate on X until done", "loop until tests pass", or for any complex multi-step task requiring iterative refinement. Runs Claude in a while-true loop with the same prompt until a completion promise is met or max iterations reached.
argument-hint: "PROMPT [--max-iterations N] [--completion-promise TEXT]"
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Ralph Loop — Iterative Development Skill

## What Is Ralph Loop?

A self-referential development loop where the same prompt is fed back after each iteration. You see your previous work in files and git history, allowing iterative refinement toward a goal.

## How It Works

1. A state file `.claude/ralph-loop.local.md` tracks loop state (iteration, max, completion promise)
2. A stop hook intercepts session exit and feeds the SAME PROMPT back
3. Each iteration, you see the codebase with your previous changes already applied
4. Loop ends when: max iterations reached, completion promise detected, or `/cancel-ralph` invoked

## Starting a Loop

Parse `$ARGUMENTS` for:

| Argument | Format | Default | Purpose |
|----------|--------|---------|---------|
| PROMPT | free text | (required) | The task to iterate on |
| `--max-iterations` | integer | 0 (unlimited) | Safety cap on iterations |
| `--completion-promise` | quoted text | null | Phrase to output when genuinely done |

Create the state file:

```bash
mkdir -p .claude
cat > .claude/ralph-loop.local.md <<EOF
---
active: true
iteration: 1
max_iterations: $MAX_ITERATIONS
completion_promise: "$COMPLETION_PROMISE"
started_at: "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
---

$PROMPT
EOF
```

## Iteration Protocol

Each iteration:

1. **Read state**: `.claude/ralph-loop.local.md` for iteration count and prompt
2. **Assess progress**: Check files, tests, git log for what changed in previous iterations
3. **Work on the task**: Make concrete progress toward the goal
4. **Report status**: Summarize what was done and what remains

## Completion Promise Rules

**CRITICAL**: If a `--completion-promise` is set:

- You may ONLY output `<promise>PHRASE</promise>` when the statement is **completely and unequivocally TRUE**
- Do NOT output false promises to escape the loop
- Do NOT lie even if you think you should exit
- The loop is designed to continue until genuine completion — trust the process

## Project-Specific Loop Templates

### Test Coverage Loop
```
/ralph-loop Write tests until 80% coverage --completion-promise 'Coverage exceeds 80%' --max-iterations 20
```
Each iteration: run `pytest --cov=src/legal_corpus_ingester --cov-report=term-missing -v`, identify lowest-coverage module, write tests for it, verify they pass.

### Pipeline Stage Implementation Loop
```
/ralph-loop Implement the fetcher stage until all unit tests pass --completion-promise 'All fetcher tests pass' --max-iterations 15
```
Each iteration: check `src/legal_corpus_ingester/fetchers/`, write or fix one fetcher, run `pytest tests/unit/fetchers/ -v`.

### Dependency Audit Loop
```
/ralph-loop Audit all dependencies against hard requirements --completion-promise 'All deps score A or higher' --max-iterations 10
```
Each iteration: pick next unaudited dep from `docs/plans/2026-07-04-legal-corpus-ingester.md` (in terms-analysis), run `/dependency-audit`, document results.

### Code Review Loop
```
/ralph-loop Review and fix all code quality issues --completion-promise 'No issues remain' --max-iterations 10
```
Each iteration: run `/review`, fix highest-priority issue, verify fix.

## Cancelling

To cancel an active loop: remove the state file.
```bash
rm .claude/ralph-loop.local.md
```

## Monitoring

```bash
# Current iteration:
grep '^iteration:' .claude/ralph-loop.local.md

# Full state:
head -10 .claude/ralph-loop.local.md
```

## Arguments
- `$ARGUMENTS`: Full command line including prompt and options
- If no arguments given, show usage help and available project-specific templates
