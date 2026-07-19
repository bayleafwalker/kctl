# AGENTS.md — kctl

> Environment reference: local devbox and workstation setup may differ. Keep tool install persistence, direnv/PATH setup, and session-level operational notes outside this repository.

## Tech Stack

Primary language: Python >= 3.11. Use `pytest` for testing. SQLite-backed local knowledge store. CLI built with `click`. Markdown for documentation. Package manager: `uv` / `pipx`.

## Environment setup

| Variable | Purpose |
|---|---|
| `KCTL_DB` | Override the database path (default: `~/.kctl/kctl.db`) |
| `KCTL_PROJECT` | Project scope identifier |

Validate that `KCTL_DB` points to the project-scoped database before use. No cluster context: kctl is local-first and optimized for one developer or sparse agent sessions.

## Development workflow

- Run targeted `pytest` checks after changes and report results.
- Never commit with failing tests.
- Commit at a logical, reviewable scope boundary.
- Behavior changes require matching tests.
- Keep sprintctl read-only from kctl; kctl must not mutate backlog or claim state.

## Purpose

kctl reads sprintctl event streams, extracts durable and coordination knowledge, and manages review-to-publication lifecycles. The candidate flow is `candidate -> approved|rejected`; approved candidates may become `published` entries and rendered projections.

## Stateful protocol verification

The governing protocol draft is `docs/protocols/knowledge-lifecycle.md`; repo-specific verification rules are in `.agents/overlays/kctl.state-protocols.md`.

Use the shared `verify-state-protocols` skill when changes affect extraction watermarks, source-event deduplication, candidate transitions, publication, supersession, or rendering. Default to Depth 1. Escalate to Depth 2 only if concurrent writers or independent processes become supported.

Use `survey` and `reconcile` read-only. Verification may add tests and model artifacts but must not silently repair lifecycle semantics. Preserve current limitations in reports: extraction is restartable through source-event deduplication, while publication currently spans multiple SQLite commits and is not an atomic or idempotent command.

The machine-readable routing and hook policy is `kctl.dispatch.json`. Validate reusable packets with `python /projects/dev/agentops/templates/dispatch/scripts/validate_verification_artifacts.py --root .`.

<!-- agentops-project-pointer:start -->
See `.agents/project.generated.md` for cross-repo project context (agentops-managed; do not hand-edit).
<!-- agentops-project-pointer:end -->
