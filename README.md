# kctl

A local-first knowledge promotion and backlog-seeding CLI for a single developer working through [sprintctl](https://github.com/bayleafwalker/sprintctl) sprints.

kctl recovers durable knowledge from sprint exhaust — decisions, patterns, resolved blockers, lessons — reviews it, promotes it to a committed knowledge base, and makes it available to seed future sprints.

## What kctl is

- A tool for one developer (or one sparse agentic session at a time)
- A consumer of sprintctl event streams and sprint artifacts
- A review-gated pipeline: candidate → approved → published → rendered markdown
- A source of structured backlog seeds derived from reviewed knowledge
- Local-first: SQLite on disk, convergence through committed markdown

## What kctl is not

- Not a wiki, search engine, or documentation generator
- Not a multi-team or enterprise knowledge management platform
- Not a replacement for structured docs (ADRs, runbooks, READMEs)
- Not a hosted service or remote knowledge store
- Not optimised for concurrent contributors across machines

## The lifecycle

```
sprintctl events
      │
      ▼
  kctl extract          — scan knowledge-bearing events, create candidates
      │
      ▼
  kctl review list      — inspect candidates
  kctl review approve   — approve (optionally edit title/tags)
  kctl review reject    — discard
      │
      ▼
  kctl publish          — promote approved candidate → knowledge entry (add body + category)
      │
      ▼
  kctl render           — emit knowledge.md, committed alongside sprint.md
      │
      ▼
  future sprint setup   — reviewed knowledge seeds new sprint goals, tracks, and work items
```

**Three distinct states:**

| State | Meaning |
|---|---|
| `candidate` | Extracted from a sprintctl event; not yet reviewed |
| `approved` | Reviewed and accepted; waiting for a body to be written before publishing |
| `published` | Promoted to a `knowledge_entry`; included in rendered output |

Rejected candidates are retained for audit but excluded from all outputs.

## Backlog seeding

The primary reason to maintain a knowledge base is to inform future work. After closing a sprint:

1. Run `kctl render --output knowledge.md` and commit it.
2. When planning the next sprint in sprintctl, open `knowledge.md` (or use `kctl render --category decision --sprint-id N`) as reference.
3. Patterns, risks, and decisions surfaced in past sprints become explicit inputs to new sprint goals and track definitions.

For agentic sessions, use machine-readable outputs instead of markdown prose:

```sh
kctl status --json                         # pipeline counts + approved list
kctl review list --status approved --json  # approved candidates ready to publish
kctl render --sprint-id N                  # knowledge from a specific sprint
```

An agent populating a new sprint can read `kctl status --json`, inspect approved entries, and turn them into sprintctl work items or track goals without parsing markdown.

## Requirements

- Python 3.11+
- [click](https://click.palletsprojects.com/) (only non-stdlib dependency)
- sprintctl database (for extraction; read-only access)

## Installation

Install globally via [pipx](https://pipx.pypa.io/):

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
export KCTL_PROJECT=my-project              # label used in rendered output headers (default: homelab-analytics)
export KCTL_EVENT_TYPES=decision,blocker-resolved,pattern-noted,risk-accepted,lesson-learned
```

Per-project setup — add to `.envrc`:

```sh
export SPRINTCTL_DB="$PWD/.sprintctl/sprintctl.db"
export KCTL_DB="$PWD/.kctl/kctl.db"
export KCTL_PROJECT="my-project"
```

Add to `.gitignore`:

```gitignore
.sprintctl/
.kctl/
```

## Quickstart

```sh
# Extract knowledge candidates from sprintctl events
kctl extract

# Review what was found
kctl review list

# Approve a candidate (optionally refine it)
kctl review approve --id 1 --title "Use RS256 for auth tokens" --tags '["auth","architecture"]'

# Publish the approved candidate as a knowledge entry
kctl publish --id 1 --body "Symmetric HMAC breaks when services don't share a secret rotation schedule. Switched to RS256 after third key sync failure." --category decision

# Render published entries to markdown
kctl render --output knowledge.md
git add knowledge.md
```

## Commands

### Extract

```sh
kctl extract                                            # incremental — new events only
kctl extract --sprint-id 1                             # scope to one sprint
kctl extract --full                                    # re-scan all events
kctl extract --event-types decision,pattern-noted      # override event type filter
kctl extract --no-preflight                            # skip sprintctl stale-item check
```

Scans sprintctl's event table for knowledge-bearing event types and creates candidates. Idempotent — re-running against the same events creates no duplicates. By default, only events newer than the last extraction are scanned (watermark in `extractor_state`).

**Default event types:** `decision`, `blocker-resolved`, `pattern-noted`, `risk-accepted`, `lesson-learned`

Extraction reports how many candidates had structured payloads (agent-emitted, with `summary`/`detail`/`tags` fields) vs. bare events (fallback summary derived from event type and item title). Structured payloads are higher quality and need less editing at review time.

### Review

```sh
kctl review list                          # candidates awaiting review (default)
kctl review list --status approved        # approved, pending publish
kctl review list --status all             # all statuses
kctl review list --tag auth               # filter by tag
kctl review list --sprint-id 1            # filter by source sprint
kctl review list --json                   # machine-readable output for agents

kctl review show --id 5

kctl review approve --id 5
kctl review approve --id 5 --title "Revised title" --tags '["auth","lessons"]'
kctl review reject --id 5 --reason "duplicate of #3"
```

Approve and reject are the only mutations available from review. Status transitions are enforced:
- `candidate → approved` or `candidate → rejected`
- No further transitions from `rejected`
- `approved → published` happens via `kctl publish`

### Publish

```sh
kctl publish --id 5 --body "Full detail text." --category decision
kctl publish --id 5 --title "Override title" --body "..." --category pattern --tags '["auth"]'
```

Promotes an approved candidate to a knowledge entry. Requires `--body` and `--category`. `--title` defaults to the candidate's summary if omitted. `--tags` defaults to the candidate's tags if omitted.

**Categories:** `decision`, `pattern`, `lesson`, `risk`, `reference`

This is where the reviewed candidate gets its durable body — the actual knowledge content that will appear in rendered output and inform future sprints.

### Render

```sh
kctl render                              # all published entries to stdout
kctl render --category decision          # filter by category
kctl render --tag auth                   # filter by tag
kctl render --sprint-id 1               # entries from a specific sprint
kctl render --output knowledge.md        # write to file
```

Renders published knowledge entries as structured markdown, grouped by category (decisions, patterns, lessons, risks, references). The document header uses `KCTL_PROJECT` (defaults to `homelab-analytics` if unset).

Commit the output alongside the sprint render:

```sh
kctl render --output knowledge.md
git add knowledge.md sprint.md
```

The committed markdown is the shared, durable record. The local kctl database is working state.

### Status

```sh
kctl status                              # pipeline counts across all sprints
kctl status --sprint-id 1               # scoped to one sprint
kctl status --json                       # machine-readable output for agents
```

Shows counts of candidates awaiting review, approved-but-unpublished, and published entries. Lists approved candidates by ID and summary for quick reference.

JSON output format:

```json
{
  "sprint_id": null,
  "counts": { "candidate": 2, "approved": 1, "published": 5 },
  "approved": [
    { "id": 3, "summary": "Use RS256 for auth tokens", "event_type": "decision" }
  ]
}
```

### Preflight

```sh
kctl preflight
kctl preflight --sprintctl-db /path/to/sprint.db
```

Runs a stale-item check against active sprintctl sprints and reports work items with no activity beyond the configured threshold (default: 4 hours; override with `SPRINTCTL_STALE_THRESHOLD`). This runs automatically before `kctl extract` — use `--no-preflight` to skip. Warnings do not block extraction.

## Architecture

```
kctl/
  db.py       — schema, migrations, all data access; transition enforcement
  extract.py  — event scanning, candidate building, idempotent insertion
  review.py   — status transitions, validation
  publish.py  — candidate → entry promotion
  cli.py      — Click entry point; thin dispatch only, no business logic
tests/
  conftest.py — shared fixtures (on-disk sprintctl-like DB, kctl test DB)
  test_extract.py
  test_review.py
  test_publish_render_status.py
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
                                  │
                                  ▼
                           knowledge.md   ← committed, shared
```

sprintctl has no runtime dependency on kctl. kctl never writes to sprintctl's database. The relationship is one-way: kctl consumes the event stream that sprintctl produces as a side effect of sprint execution.

## Integration with sprintctl's envrc template

For setup instructions covering both tools together — including the direnv template — see [sprintctl's CONTRIBUTING.md](https://github.com/bayleafwalker/sprintctl/blob/main/CONTRIBUTING.md). An [envrc.example](https://github.com/bayleafwalker/sprintctl/blob/main/envrc.example) in the sprintctl repo covers both `SPRINTCTL_DB` and `KCTL_DB`.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
PYTHONPATH=. .venv/bin/python -m pytest tests/ -v
```
