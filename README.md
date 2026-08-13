# kctl

A local-first reader and review CLI for [sprintctl](https://github.com/bayleafwalker/sprintctl) event streams.

kctl extracts two distinct streams from execution:
- durable knowledge that can be reviewed, published, and rendered into a committed knowledge base
- handoff and coordination items that remain reviewable but separate from durable knowledge

## What kctl is

- A tool for one developer or one sparse agentic session at a time
- A read-only consumer of sprintctl events, work items, and sprint context
- A review surface that separates durable knowledge from coordination items
- A durable knowledge pipeline: `candidate -> approved -> published -> rendered markdown`
- A producer of local artifacts and machine-readable outputs that agents can use when deciding sprintctl actions
- Local-first: SQLite on disk, convergence through committed markdown

## What kctl is not

- Not a sprint or backlog writer
- Not a direct sprint seeding tool
- Not a replacement for sprintctl's write-side command surface
- Not a wiki, search engine, or documentation generator
- Not a hosted service or remote knowledge store
- Not optimised for concurrent contributors across machines

## Two streams

kctl keeps these streams separate:

| Stream | Source examples | Reviewable | Publishable | Rendered |
|---|---|---:|---:|---:|
| `durable` | `decision`, `pattern-noted`, `lesson-learned`, `risk-accepted`, `blocker-resolved` | yes | yes | yes |
| `coordination` | `claim-handoff`, `claim-ownership-corrected`, `claim-ambiguity-detected`, `coordination-failure` | yes | yes, via `kctl publish --coordination` | yes, via `kctl render --coordination` |

Coordination items still stay distinct from durable knowledge, but approved coordination lessons can now be promoted when they carry workflow-improvement value.

Repository policy can choose how those published coordination entries are rendered. One repository may keep a separate ops-oriented output such as `knowledge-base-ops.md`, while another may fold approved coordination knowledge into the same committed `knowledge-base.md` used for durable entries through a thin repo-local wrapper or publish step.

## The lifecycle

- durable candidates: `extract -> review -> publish -> render knowledge.md`
- coordination candidates: `extract -> review -> publish --coordination -> render --coordination` into a repo-policy target

Agents then read kctl artifacts and choose the relevant sprintctl actions.

## Acting on kctl artifacts

kctl never writes back into sprintctl. sprintctl remains the only tool that owns backlog and sprint mutation.

The intended loop is:

1. Extract and review what mattered during execution.
2. Publish durable knowledge and render `knowledge.md`.
3. Use `kctl status --json`, `kctl review list --json`, or rendered markdown to decide what matters next.
4. Have an agent or operator invoke the appropriate sprintctl commands.

Example artifact surfaces for agents:

```sh
kctl status --json                                  # durable pipeline state by default
kctl status --kind all --json                       # durable + coordination counts separated
kctl review list --status approved --json           # approved durable candidates ready to publish
kctl review show --id 42 --json                     # one candidate with full source context
kctl review list --kind coordination --json         # coordination review stream
kctl render --json                                  # durable knowledge entries as structured JSON
kctl export --artifacts-root /path/to/_artifacts --repo-id my-project
kctl export-proposal --artifacts-root /path/to/_artifacts --repo-id my-project  # approved-but-unpublished proposals
kctl preflight --json                               # stale-item warnings as structured JSON
kctl render --sprint-id N                           # durable knowledge from one sprint
```

An agent shaping sprint work can read kctl artifacts, inspect source track, sprint, provenance, and coordination context, then choose the relevant sprintctl action. kctl is the reader and reviewer; sprintctl remains the writer.

For cockpit consumption, the versioned knowledge-artifact/v1 contract defines
a separate published-entry NDJSON projection under
_artifacts/<repo>/knowledge/. The contract is intentionally read-only; its
exporter is delivered separately from the existing render --json command.

### Cockpit artifact export

The optional export command writes one complete, read-only NDJSON snapshot of
all published durable and coordination entries. It requires an explicit
artifact root and repository scope (or the KCTL_ARTIFACTS_ROOT and
KCTL_PROJECT environment variables):

    kctl export --artifacts-root /projects/dev/_artifacts --repo-id agentops

The destination is _artifacts/<repo>/knowledge/knowledge-artifact-v1.ndjson.
Each rerun atomically replaces the complete snapshot; it never appends
records. See docs/protocols/knowledge-artifact-v1.md for the stable consumer
contract.

### Backlog proposal export

`kctl export-proposal` writes a second, separate read-only NDJSON snapshot of
`approved`-but-not-yet-published candidates, shaped as bounded suggestions
for backlog refinement rather than sprintctl command output:

    kctl export-proposal --artifacts-root /projects/dev/_artifacts --repo-id kctl

The destination is _artifacts/<repo>/knowledge/knowledge-proposal-v1.ndjson.
Each record carries the reviewed content, its sprintctl provenance, and a
suggested owner repo plus a suggested next-action string such as `propose
sprintctl item add in repo kctl`. This command never calls sprintctl and a
record is never accepted state: only a human or a separately authorized
agent running an explicit `sprintctl item add ...` (or similar) themselves
turns a proposal into a real backlog item. See
docs/protocols/knowledge-proposal-v1.md for the contract and
docs/examples/knowledge-proposal-consumer.md for a worked example.

## Requirements

- Python 3.11+
- [click](https://click.palletsprojects.com/)
- A sprintctl database with read-only access for extraction

## Installation

Install globally via [pipx](https://pipx.pypa.io/):

```sh
pipx install git+https://github.com/bayleafwalker/kctl.git
```

Release wheels are published on the kctl GitHub Releases page (kctl is not
published to PyPI). For a reproducible install, download the wheel from the
desired release, compare its SHA-256 with the release notes, and install the
verified local file:

```sh
pipx install ./kctl-0.1.2-py3-none-any.whl
```

For local development:

```sh
pip install -e .
```

## Configuration

```sh
export KCTL_DB=/path/to/kctl.db             # default: ~/.kctl/kctl.db
export SPRINTCTL_DB=/path/to/sprintctl.db   # default: ~/.sprintctl/sprintctl.db
export KCTL_PROJECT=my-project              # rendered output header label
export KCTL_EVENT_TYPES=decision,pattern-noted,lesson-learned
```

For a sprintctl PostgreSQL remote source, install `kctl[remote]` and set
`SPRINTCTL_BACKEND=remote` plus `SPRINTCTL_URL`. kctl reads that source through
a connection whose transactions are read-only; it never bootstraps or writes
the sprintctl schema. Its extraction watermark is keyed as
`remote://<repo_id>` (not by the URL), so credentials are never persisted in
the kctl database. The repo ID defaults to the current repository; set
`KCTL_SPRINTCTL_REPO_ID` or pass `--sprintctl-repo-id` when reading another
repository.

For a served sprintctl source, install `kctl[served]` on Python 3.12+ and set
`SPRINTCTL_BACKEND=served` plus `SPRINTCTL_VUORO_PROFILE` to the same
`vuoro-client-profile/v1` profile used by sprintctl. kctl invokes
`work.read.events` directly through `vuoro-client`; it never shells out to
the sprintctl CLI or opens its database. Served extraction requires an
explicit `--sprint-id`, because the catalog's event read is sprint-scoped.
Its watermark is keyed as `served://<repo_id>` and uses durable event IDs;
the ordinal served pagination cursor is never persisted as that watermark.

Served `kctl preflight` and `kctl doctor` invoke Sprintctl's owning
`work.maintain.check` catalog aggregate. They never reconstruct the diagnostic
from partial sprint reads and never open either Kctl SQLite or a direct
Sprintctl store. If an older deployed catalog lacks the operation, both fail
visibly and extraction stops; use `--no-preflight` only after recording the
applicable sprint health evidence through the owning Sprintctl workflow.

If `KCTL_EVENT_TYPES` is unset, kctl extracts these defaults:

- durable: `decision`, `blocker-resolved`, `pattern-noted`, `risk-accepted`, `lesson-learned`
- coordination: `claim-handoff`, `claim-ownership-corrected`, `claim-ambiguity-detected`, `coordination-failure`

Per-project setup in `.envrc`:

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
# Extract default durable + coordination candidates
kctl extract

# Review durable candidates (default stream)
kctl review list

# Coordination items stay available separately
kctl review list --kind coordination

# Approve a durable candidate and refine it
kctl review approve \
  --id 1 \
  --title "Use RS256 for auth tokens" \
  --detail "Use asymmetric verification across services." \
  --tags '["auth","architecture"]'

# Publish approved durable knowledge
kctl publish \
  --id 1 \
  --body "Symmetric HMAC breaks when services don't share a secret rotation schedule. Switched to RS256 after repeated key sync failures." \
  --category decision

# Publish approved coordination knowledge
kctl publish \
  --id 7 \
  --body "Claim-token recovery failed during background handoff. Promote these lessons into an ops playbook instead of leaving them in review-only state." \
  --category lesson \
  --coordination

# Render published entries to markdown
kctl render --output knowledge.md

# One valid repo policy: keep coordination in a separate file
kctl render --coordination --output knowledge-base-ops.md

# Another valid repo policy: keep kctl's separate stream rendering,
# then combine durable + approved coordination entries in a repo-local wrapper
# before writing the single committed knowledge-base.md

# Commit the rendered knowledge file or files your repo policy uses
```

## Commands

### Extract

```sh
kctl extract                                                   # incremental; default durable + coordination streams
kctl extract --sprint-id 1                                     # scope to one sprint
kctl extract --full                                            # re-scan the current extraction scope
kctl extract --event-types decision,pattern-noted              # override event types
kctl extract --no-preflight                                    # skip sprintctl stale-item check
```

Scans sprintctl's event table and creates candidates. Extraction is idempotent: re-running against the same source events creates no duplicates. The extraction watermark is scoped by both `--sprint-id` and the effective event type filter, so filtered runs cannot advance a single global watermark incorrectly.

Extraction reports:
- how many candidates carried structured payloads versus bare fallback payloads
- how many were classified as durable versus coordination

Sprint is used as a container reference only. Most meaningful context comes from the source work item, track, event type, actor, source type, and raw payload.

### Review

```sh
kctl review list                                   # durable candidates awaiting review
kctl review list --kind coordination               # coordination candidates awaiting review
kctl review list --status approved                 # approved, pending publish
kctl review list --status all --kind all           # all statuses across both streams
kctl review list --tag auth                        # filter by tag
kctl review list --sprint-id 1                     # filter by source sprint
kctl review list --json                            # machine-readable output

kctl review show --id 5
kctl review show --id 5 --json                     # one candidate as machine-readable JSON

kctl review approve --id 5
kctl review approve --id 5 --title "Revised title" --detail "Expanded context" --tags '["auth","lessons"]'
kctl review reject --id 5 --reason "duplicate of #3"
```

Review supports only `approve` and `reject`. Status transitions are enforced:

- `candidate -> approved`
- `candidate -> rejected`
- `approved -> published` via `kctl publish`

`review show` and JSON output surface:
- source actor, source type, source timestamp, and raw payload
- extracted provenance fields such as `git_branch`, `git_sha`, `evidence_item_id`, and `evidence_event_id`
- parsed coordination context such as `mode`, `reason`, `from_identity`, and `to_identity`

`kctl review show --json` returns one object with candidate fields, plus:
- `provenance` (parsed VCS/evidence fields from payload)
- `coordination_context` (parsed handoff/claim context)
- `source_payload` (decoded payload object when JSON)

### Publish

```sh
kctl publish --id 5 --body "Full detail text." --category decision
kctl publish --id 5 --title "Override title" --body "..." --category pattern --tags '["auth"]'
kctl publish --id 7 --body "..." --category decision --supersedes 3
kctl publish --id 12 --body "..." --category lesson --coordination
```

Promotes an approved candidate to a knowledge entry. Requires `--body` and `--category`. `--title` defaults to the candidate summary if omitted. `--tags` defaults to the candidate tags if omitted.

Coordination candidates require `--coordination`. That keeps durable publication as the default path while making workflow lessons publishable without forcing a single repository policy for rendered output.

Categories:
- `decision`
- `pattern`
- `lesson`
- `risk`
- `reference`

`--supersedes` marks an older entry as superseded by the newly published one.

### Render

```sh
kctl render                              # all published durable entries to stdout
kctl render --coordination               # published coordination entries to stdout
kctl render --category decision          # filter by category
kctl render --tag auth                   # filter by tag
kctl render --sprint-id 1                # entries from a specific sprint
kctl render --json                       # machine-readable output
kctl render --output knowledge.md        # write durable knowledge to file
kctl render --coordination --output knowledge-base-ops.md
```

Renders published entries as structured markdown, grouped by category. Durable knowledge remains the default. `--coordination` switches to the coordination knowledge base, which is intended for workflow and operating lessons. Each entry shows its source track and sprint container. Superseded entries are annotated.

`kctl render --json` returns:
- `project`, `generated_at`, and `filters`
- `source_kind`, `count`, and `counts_by_category`
- `entries` with title/body/category/tags plus source sprint and track fields

The document header uses `KCTL_PROJECT` and defaults to `homelab-analytics` if unset.

### Status

```sh
kctl status                              # durable pipeline counts across all sprints
kctl status --sprint-id 1                # scope to one sprint
kctl status --kind coordination          # coordination review state
kctl status --kind all --json            # counts separated by stream
```

Shows counts of candidates awaiting review, approved-but-unpublished, and published entries. By default this is the durable knowledge pipeline. `--kind coordination` scopes to coordination items, and `--kind all` returns both streams separately.

Example JSON output:

```json
{
  "sprint_id": null,
  "kind": "durable",
  "counts": { "candidate": 2, "approved": 1, "published": 5 },
  "approved": [
    {
      "id": 3,
      "candidate_kind": "durable",
      "summary": "Use RS256 for auth tokens",
      "event_type": "decision",
      "source_track": "backend",
      "source_sprint_id": 1
    }
  ]
}
```

### Preflight

```sh
kctl preflight
kctl preflight --sprint-id 1
kctl preflight --sprintctl-db /path/to/sprint.db
kctl preflight --json
kctl doctor --sprint-id 1 --json
```

Runs sprintctl's own stale-item diagnostics and reports warnings before extraction. This follows sprintctl's current semantics, including `SPRINTCTL_STALE_THRESHOLD` and `SPRINTCTL_PENDING_STALE_THRESHOLD`, instead of maintaining a separate SQL shadow in kctl.

`kctl extract` runs preflight automatically unless `--no-preflight` is supplied. Stale-item warnings do not block extraction, but a preflight runtime failure (including a deployed catalog missing `work.maintain.check`) does; explicitly pass `--no-preflight` only after recording the applicable health evidence.
`kctl preflight --json` emits a structured payload with:
- `ok` (boolean)
- `sprint_id`
- `warnings` (array)
- `error` (`null` on success/warnings; string on hard failures such as missing DB/schema mismatch or preflight runtime failure)

When a hard preflight failure occurs, `warnings` is returned as an empty array and the failure is reported in `error`.

`kctl doctor --json` reports the selected backend/source identity and a
`maintain_check` object with availability, scope, warnings, and any hard
failure. In served mode it is a source-only preflight: it does not initialize
the local knowledge store. Other top-level commands are explicitly classified;
publication, render, and export remain unavailable in served mode and fail with
`served-operation-unavailable` rather than falling back to SQLite.

## Architecture

```text
kctl/
  db.py       - schema, migrations, all data access, transition enforcement
  extract.py  - event scanning, stream classification, scoped watermarks, preflight adapter
  review.py   - status transitions and review validation
  publish.py  - durable candidate -> entry promotion and supersession
  artifact.py - read-only knowledge-artifact/v1 export of published entries
  proposal.py - read-only knowledge-proposal/v1 export of approved-but-unpublished candidates
  cli.py      - Click entry point plus human and JSON artifact surfaces
tests/
  conftest.py
  test_extract.py
  test_preflight.py
  test_review.py
  test_publish_render_status.py
  test_knowledge_artifact_export.py
  test_knowledge_artifact_contract.py
  test_knowledge_proposal_export.py
  test_knowledge_proposal_contract.py
```

kctl owns its own SQLite database and never writes to sprintctl's. Its local
SQLite and remote PostgreSQL source connections are always opened read-only.
Durable knowledge and coordination items share extraction infrastructure but
are stored and reviewed as separate streams.

## Relationship to sprintctl

```text
sprintctl (owns writes)       kctl (reads + reviews)
┌──────────────┐             ┌──────────────┐
│ sprint.db    │             │ kctl.db      │
│  sprints     │ -- read --> │  candidates  │
│  work_items  │    only     │  entries     │
│  events      │             │              │
└──────────────┘             └──────────────┘
                                     |
                     ┌───────────────┴───────────────┐
                     v                               v
            knowledge.md / JSON            coordination review JSON
                     |
                     v
            agents choose sprintctl commands
```

sprintctl owns backlog, sprint state, and all write-side actions. kctl never feeds into sprints on its own. The relationship is one-way: kctl consumes the event stream that sprintctl produces, then emits artifacts that agents or operators can use when deciding which sprintctl actions to run.

## Integration with sprintctl's envrc template

For setup instructions covering both tools together, including the direnv template, see [sprintctl's CONTRIBUTING.md](https://github.com/bayleafwalker/sprintctl/blob/main/CONTRIBUTING.md). The sprintctl repo's `envrc.example` covers both `SPRINTCTL_DB` and `KCTL_DB`.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
PYTHONPATH=. .venv/bin/python -m pytest tests/ -v
```
