# AGENTS.md — kctl

> Environment reference: local devbox and workstation setup may differ. Keep tool install persistence, direnv/PATH setup, and session-level operational notes outside this repository.


## Tech Stack

Primary language: Python ≥ 3.11. Use `pytest` for testing. SQLite-backed local knowledge store. CLI built with `click`. Markdown for documentation. Package manager: `uv` / `pipx`.

## Environment setup

### Required environment variables

| Variable | Purpose |
|---|---|
| `KCTL_DB` | Override the database path (default: `~/.kctl/kctl.db`) |
| `KCTL_PROJECT` | Project scope identifier |

**Validation:**
```bash
echo $KCTL_DB   # for project-scoped work, must contain the project path, not ~/
kctl --help     # confirm kctl is available
```

> Using the home-directory default silently operates on the wrong knowledge database when working within a project that has its own `.kctl/` directory.

No cluster context — kctl is a local-first CLI tool.

## Development workflow

- Run `pytest` after making changes. Report pass/fail count before committing.
- **Never commit with failing tests.**
- **Commit after each logical unit of work.** One item = one commit. Run tests before each commit.
- Behavior changes must include updated or new tests in the same commit.

### Self-healing test loop

If tests fail after a change, diagnose the root cause, fix, and re-run — up to **5 cycles** — before escalating. Only escalate if still failing after 5 attempts or if a design decision is required.

## Purpose

kctl is a local-first reader/review CLI for sprintctl event streams. It extracts durable knowledge (decisions, patterns, lessons, risks, blockers) from sprint events and manages a review-to-publish lifecycle:

`candidate` → `reviewed` → `published` → rendered markdown

Designed for single developer or sparse agentic sessions. No remote/hosted service.
