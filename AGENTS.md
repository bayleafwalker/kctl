# AGENTS.md — kctl

> Shared machine/environment guidance and session notes live outside this repository.

## Tech Stack

Primary language: Python >= 3.11; tests use `pytest`. SQLite-backed local knowledge store, `click` CLI, Markdown docs, `uv`/`pipx` packaging.

## Environment

| Variable | Purpose |
|---|---|
| `KCTL_DB` | Override the database path (default: `~/.kctl/kctl.db`) |
| `KCTL_PROJECT` | Project scope identifier |

Before use, validate that `KCTL_DB` points to the project-scoped database. kctl is local-first, with no cluster context.

## Workflow

- Run targeted `pytest` checks after changes and report results; never commit with failing tests.
- Behavior changes require matching tests.
- Keep sprintctl read-only from kctl; kctl must not mutate backlog or claim state.

## Ownership

kctl reads sprintctl event streams, extracts durable and coordination knowledge, and owns review-to-publication. Candidates transition `candidate -> approved|rejected`; approved candidates may become `published` entries and rendered projections. kctl never writes sprintctl: sprintctl remains sole backlog, sprint, and claim authority.

## Stateful protocol verification

The governing protocol is `docs/protocols/knowledge-lifecycle.md`; repo-specific verification rules are in `.agents/overlays/kctl.state-protocols.md`.

Use the shared `verify-state-protocols` skill when changes affect extraction watermarks, source-event deduplication, candidate transitions, publication, supersession, or rendering. Default to Depth 1. Escalate to Depth 2 only if concurrent writers or independent processes become supported.

Use `survey` and `reconcile` read-only. Verification may add tests and model artifacts but must not silently repair lifecycle semantics. Preserve current limitations in reports: extraction is restartable through source-event deduplication, while publication currently spans multiple SQLite commits and is not an atomic or idempotent command.

The machine-readable routing and hook policy is `kctl.dispatch.json`. Validate reusable packets with `python /projects/dev/agentops/templates/dispatch/scripts/validate_verification_artifacts.py --root .`.

<!-- agentops-project-pointer:start -->
See `.agents/project.generated.md` for cross-repo project context (agentops-managed; do not hand-edit).
<!-- agentops-project-pointer:end -->

<!-- agentops-environment-pointer:start -->
See `.agents/environment.generated.md` for the active Vuoro environment's constraints and runbooks (agentops-managed; do not hand-edit).
<!-- agentops-environment-pointer:end -->
