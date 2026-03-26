# kctl

A minimal knowledge extraction and promotion CLI for agent-driven workflows. A companion to [sprintctl](https://github.com/bayleafwalker/sprintctl).

**Not a wiki or documentation generator.** kctl recovers durable knowledge — decisions, patterns, resolved blockers, lessons — from sprintctl event streams before it goes stale when sprints close.

## Why this exists

Agents working through sprintctl sprints produce valuable context as a side effect: they make decisions, discover patterns, and resolve blockers. Today that context lives in sprint events and dies when the sprint becomes stale. kctl recovers it.

## Anti-goals

- Not a search engine over sprint history
- Not a replacement for structured docs (ADRs, runbooks, READMEs)
- No web UI, no hosted dependency
- Not tightly coupled to sprintctl internals — it reads the DB, it doesn't extend it

## Requirements

- Python 3.11+
- [click](https://click.palletsprojects.com/) (only non-stdlib dependency)
- sprintctl (optional, for pre-flight integration)

## Installation

Install globally via [pipx](https://pipx.pypa.io/), not as a project dependency:

```sh
pipx install git+https://github.com/bayleafwalker/kctl.git
```

For local development:

```sh
pip install -e .
```

## Configuration

```sh
export KCTL_DB=/path/to/kctl.db             # default: ~/.kctl/kctl.db
export SPRINTCTL_DB=/path/to/sprintctl.db   # default: ~/.sprintctl/sprintctl.db
export KCTL_EVENT_TYPES=decision,blocker-resolved,pattern-noted,risk-accepted,lesson-learned
```

## Quickstart

```sh
# Extract knowledge candidates from sprintctl events
kctl extract

# Review what was found
kctl review list

# Approve a candidate (optionally refine it)
kctl review approve --id 1 --title "Use RS256 for auth tokens" --tags '["auth","architecture"]'

# Reject noise
kctl review reject --id 2 --reason "duplicate of #1"
```

## Commands

### Extract

```sh
kctl extract                                            # incremental — new events only
kctl extract --sprint-id 1                             # scope to one sprint
kctl extract --full                                    # re-scan all events
kctl extract --event-types decision,pattern-noted      # override event type filter
```

Scans sprintctl's event table for knowledge-bearing event types and creates candidates. Idempotent — re-running against the same events creates no duplicates.

**Default event types:** `decision`, `blocker-resolved`, `pattern-noted`, `risk-accepted`, `lesson-learned`

### Review

```sh
kctl review list                          # candidates awaiting review (default)
kctl review list --status approved        # ready to publish
kctl review list --tag auth               # filter by tag
kctl review list --sprint-id 1            # filter by source sprint

kctl review show --id 5

kctl review approve --id 5
kctl review approve --id 5 --title "Revised title" --tags '["auth","lessons"]'
kctl review reject --id 5 --reason "duplicate of #3"
```

Approve and reject are the only mutations. Status transitions are enforced: `candidate → approved` or `candidate → rejected`. No other transitions are allowed.

## Architecture

```
kctl/
  db.py       — schema, migrations, all data access; transition enforcement
  extract.py  — event scanning, candidate building, idempotent insertion
  review.py   — status transitions, validation
  cli.py      — Click entry point; thin dispatch only, no business logic
tests/
  conftest.py — shared fixtures (in-memory DBs for both kctl and sprintctl)
  test_extract.py
  test_review.py
```

kctl owns its own SQLite database and never writes to sprintctl's. The sprintctl connection is always opened read-only.

## Relationship to sprintctl

```
sprintctl (owns)          kctl (reads)
┌──────────────┐           ┌──────────────┐
│ sprint.db    │           │ kctl.db      │
│  sprints     │─ read ── >│  candidates  │
│  work_items  │  only     │  entries     │
│  events      │           │              │
└──────────────┘           └──────────────┘
```

kctl never writes to sprintctl's database. sprintctl has no runtime dependency on kctl.

## Integration into projects

kctl is developer tooling, not an application dependency. Install it globally via [pipx](https://pipx.pypa.io/):

```sh
pipx install kctl
```

For Nix-based setups, a flake is planned alongside sprintctl's. Until then, `pipx` is the canonical method.

For setup instructions covering both tools together — including the direnv template — see [sprintctl's CONTRIBUTING.md](https://github.com/bayleafwalker/sprintctl/blob/main/CONTRIBUTING.md). An [envrc.example](https://github.com/bayleafwalker/sprintctl/blob/main/envrc.example) lives in the sprintctl repo and covers both `SPRINTCTL_DB` and `KCTL_DB`.

### Per-project database paths

Each project should scope both databases to its working directory. Add to `.envrc`:

```sh
export SPRINTCTL_DB="$PWD/.sprintctl/sprintctl.db"
export KCTL_DB="$PWD/.kctl/kctl.db"
```

### .gitignore

Add `.kctl/` alongside `.sprintctl/`:

```gitignore
.sprintctl/
.kctl/
```

### Committed artifact

The output of `kctl review list --status approved` is the shareable artifact — commit it as `knowledge.md` (or similar) alongside the sprint render:

```sh
kctl review list --status approved > knowledge.md
git add knowledge.md sprint.md
```

This mirrors the sprintctl pattern: local databases are transient working state, committed markdown is the shared record.

## Multi-contributor workflows

kctl follows the same local-DB-per-contributor model as sprintctl. See the [sprintctl README — Multi-contributor workflows](https://github.com/bayleafwalker/sprintctl#multi-contributor-workflows) for the full rationale.

The short version applied to kctl:

- Each contributor runs `kctl extract` against their own sprintctl database, so extraction is naturally scoped to your events — not someone else's.
- Approved knowledge entries converge through git when committed as `knowledge.md`. No special coordination is needed; git merge handles the text files.
- Contributor A's approved entries and contributor B's approved entries are independent until both are committed and merged. That's the intended model.

The repo is the integration layer. Local kctl databases are not shared or replicated.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
PYTHONPATH=. .venv/bin/python -m pytest tests/ -v
```

## Phases

| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Extract, review pipeline (candidate → approved/rejected) | Complete |
| 2 | Publish, query, render (candidate → entry, full-text search, markdown output) | Planned |
| 3 | FTS5, supersede chains, bulk operations, export formats | Planned |
