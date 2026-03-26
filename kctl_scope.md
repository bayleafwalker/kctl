You are working on a knowledge extraction and promotion tool (`kctl`) that serves as a companion to `sprintctl`.

Your task is to **design and implement** this tool so that durable knowledge — decisions, patterns, resolved blockers, lessons — is not lost when sprints close.

---

# Context

## What this is

`kctl` is a **read-extract-promote pipeline** for agent-generated knowledge.

Agents working through `sprintctl` sprints produce valuable context as a side effect: they make decisions, discover patterns, resolve blockers, and learn things about the codebase. Today that context lives in sprint events and dies when the sprint becomes stale. `kctl` recovers it.

## What this is not

- Not a wiki or documentation generator
- Not a search engine over sprint history
- Not a replacement for structured docs (ADRs, runbooks, READMEs)
- Not tightly coupled to sprintctl internals — it reads the DB, it doesn't extend it

## Relationship to sprintctl

```
sprintctl (owns)          kctl (reads)
┌──────────────┐          ┌──────────────┐
│ sprint.db    │          │ kctl.db      │
│  sprints     │─ read ──▶│  candidates  │
│  work_items  │  only    │  entries     │
│  events  ◄───┼──────────│  (triggers   │
│  tracks      │ maintain │   maintain   │
│              │  check   │   pre-flight)│
└──────────────┘          └──────────────┘
```

**Hard boundaries:**

- kctl NEVER writes to sprintctl's database
- kctl NEVER transitions sprint/item/claim state
- kctl ALWAYS calls `sprintctl maintain check` before extraction (ensures fresh state)
- sprintctl has no runtime dependency on kctl — it runs fine without it

---

# Objectives

Build a tool that:

- Extracts knowledge candidates from sprintctl event streams
- Supports a promotion pipeline: `candidate → approved → published`
- Makes published knowledge queryable by tag, topic, and source sprint
- Renders knowledge entries as context-injectable documents (markdown)
- Stays minimal — no web UI, no ORM, stdlib + Click only
- Is testable with the same patterns as sprintctl (in-memory SQLite, pure functions)

---

# Required output

Structure the implementation into the following sections:

---

## 1. Architecture overview

Define:

- Core components: extractor, store, promoter, renderer
- Data flow: sprintctl events → extraction → candidate → review → approved → published
- Where SQLite, CLI, and rendered output fit
- The pre-flight contract with sprintctl

### Extractor

Reads sprintctl's event table. Identifies knowledge-bearing events based on `event_type` conventions. Produces candidate entries. Must be idempotent — re-running against the same events must not create duplicates.

**Knowledge-bearing event types** (convention from sprintctl):

| event_type         | What it captures                                   |
|--------------------|----------------------------------------------------|
| `decision`         | Architectural or process decision with reasoning   |
| `blocker-resolved` | How a blocker was unblocked, what was learned       |
| `pattern-noted`    | Reusable pattern identified during work             |
| `risk-accepted`    | Explicit risk acceptance with reasoning             |
| `lesson-learned`   | Retrospective insight from completed or failed work |

These are the **default** set. The extractor should accept additional types via configuration. Event types are freeform strings in sprintctl — kctl filters, it does not enforce.

### Structured payload convention

sprintctl events that target kctl extraction should carry structured payloads:

```json
{
  "summary": "Auth tokens must use asymmetric signing in multi-service deployments",
  "detail": "Symmetric HMAC breaks when services don't share a secret rotation schedule. Switched to RS256 after third key sync failure.",
  "tags": ["auth", "architecture", "multi-service"],
  "confidence": "high"
}
```

All fields are optional. The extractor must handle events with no payload, partial payloads, or unstructured payload text. Missing `summary` → extractor uses the event_type + work item title as a fallback. Missing `tags` → extractor leaves tags empty for manual tagging during review.

---

## 2. Data model

kctl owns its own SQLite database (`~/.kctl/kctl.db`, overridable via `KCTL_DB`).

### KnowledgeCandidate

The raw extraction output. One candidate per qualifying sprintctl event.

| Field              | Type     | Required | Notes                                              |
|--------------------|----------|----------|----------------------------------------------------|
| id                 | INTEGER  | auto     | Primary key                                        |
| source_event_id    | INTEGER  | yes      | The sprintctl event.id this was extracted from      |
| source_sprint_id   | INTEGER  | yes      | The sprint the event belongs to                    |
| source_item_id     | INTEGER  | no       | The work item the event was linked to (if any)     |
| event_type         | TEXT     | yes      | Copied from sprintctl event                        |
| summary            | TEXT     | yes      | From payload, or auto-generated fallback           |
| detail             | TEXT     | no       | From payload                                       |
| tags               | TEXT     | no       | JSON array of strings                              |
| confidence         | TEXT     | no       | From payload: `high`, `medium`, `low`              |
| status             | TEXT     | yes      | `candidate` / `approved` / `rejected` / `published`|
| extracted_at       | TEXT     | yes      | When kctl created this record                      |
| reviewed_at        | TEXT     | no       | When status changed from `candidate`               |
| reviewed_by        | TEXT     | no       | Who reviewed (agent name or `human`)               |

**Uniqueness constraint:** `source_event_id` must be UNIQUE. This is how idempotent re-extraction works — if the event was already extracted, skip it.

**Status transitions:**

```
candidate → approved → published
candidate → rejected
```

No other transitions. `published` and `rejected` are terminal.

### KnowledgeEntry

Published knowledge — the durable output. Created when a candidate is promoted to `published`.

| Field           | Type     | Required | Notes                                              |
|-----------------|----------|----------|----------------------------------------------------|
| id              | INTEGER  | auto     | Primary key                                        |
| candidate_id    | INTEGER  | yes      | FK to KnowledgeCandidate                           |
| title           | TEXT     | yes      | May be edited from original summary during review  |
| body            | TEXT     | yes      | May be edited from original detail during review   |
| tags            | TEXT     | yes      | JSON array, may be refined during review           |
| category        | TEXT     | yes      | `decision` / `pattern` / `lesson` / `risk` / `reference` |
| source_sprint   | TEXT     | yes      | Sprint name (denormalized for portability)         |
| source_track    | TEXT     | no       | Track name if the event was linked to a work item  |
| created_at      | TEXT     | yes      | When published                                     |
| superseded_by   | INTEGER  | no       | FK to another KnowledgeEntry if this is outdated   |

**Why denormalize source_sprint and source_track?** KnowledgeEntry should be portable — readable and useful even if the sprintctl DB is gone. No foreign keys into sprintctl's schema.

### ExtractorState

Tracks extraction progress to support incremental runs.

| Field              | Type     | Required | Notes                                       |
|--------------------|----------|----------|-----------------------------------------------|
| id                 | INTEGER  | auto     | Primary key                                   |
| sprintctl_db_path  | TEXT     | yes      | Which sprintctl DB this state tracks          |
| last_event_id      | INTEGER  | yes      | Highest event.id successfully processed       |
| last_run_at        | TEXT     | yes      | Timestamp of last extraction run              |

This allows `kctl extract` to process only new events since the last run, rather than re-scanning everything.

---

## 3. CLI surface

Minimal and composable. Same patterns as sprintctl: Click, thin dispatch, exit codes for scripting.

### Extract

```sh
# Extract candidates from sprintctl events (incremental by default)
kctl extract
kctl extract --sprint-id 1          # scope to one sprint
kctl extract --full                  # re-scan all events, skip existing candidates
kctl extract --event-types decision,pattern-noted   # override default event type filter
```

**Pre-flight:** Before scanning events, kctl calls sprintctl maintain check (or imports the function). If the sprintctl DB has stale items or overdue sprints, kctl prints a warning but proceeds. Extraction should not block on sprint hygiene — but the operator should know.

### Review

```sh
# List candidates awaiting review
kctl review list
kctl review list --status candidate          # default filter
kctl review list --status approved           # ready to publish
kctl review list --tag auth                  # filter by tag
kctl review list --sprint-id 1               # filter by source sprint

# Show a candidate in detail
kctl review show --id 5

# Approve or reject
kctl review approve --id 5
kctl review approve --id 5 --title "Revised title" --tags '["auth","lessons"]'
kctl review reject --id 5 --reason "duplicate of #3"
```

Approve and reject are the only status mutations. Approve optionally accepts edits to title, body, and tags. The `--reason` on reject is stored in a `reviewed_by` + `reviewed_at` update (reason can go in a payload column or a simple notes field — keep it light).

### Publish

```sh
# Publish all approved candidates as knowledge entries
kctl publish
kctl publish --id 5                  # publish a specific approved candidate

# Supersede an existing entry
kctl publish --id 8 --supersedes 3   # entry from candidate #8 replaces entry #3
```

Publishing creates a KnowledgeEntry from the approved candidate. If `--supersedes` is provided, the old entry's `superseded_by` is set.

### Query

```sh
# Search published knowledge
kctl query "auth token signing"              # full-text search across title + body
kctl query --tag auth                        # filter by tag
kctl query --category decision               # filter by category
kctl query --sprint "Sprint 1"               # filter by source sprint
kctl query --active                          # exclude superseded entries (default)
kctl query --all                             # include superseded entries
```

This is the main consumption interface. Agents and humans use this to find relevant knowledge before starting new work.

### Render

```sh
# Render published entries as a markdown document
kctl render                                  # all active entries
kctl render --tag auth --tag architecture     # filtered
kctl render --category decision              # just decisions
kctl render --output /path/to/knowledge.md   # write to file
kctl render --format context                 # compact format for agent context injection
```

Two render modes:

- **document** (default): Human-readable markdown with headers, metadata, full bodies. Suitable for reading or committing to a repo.
- **context**: Compact, structured format designed to be injected into an agent's system prompt or context window. Minimal chrome, maximum information density.

### Maintain hook

```sh
# Explicitly run sprintctl pre-flight (normally automatic during extract)
kctl preflight
kctl preflight --sprintctl-db /path/to/sprint.db
```

Calls `sprintctl maintain check` and reports results. Useful for verifying the link between the two tools is working.

---

## 4. Pre-flight contract

### What kctl does before extraction

1. Resolve the sprintctl DB path (`SPRINTCTL_DB` env var or default `~/.sprintctl/sprintctl.db`)
2. Open the sprintctl DB **read-only** (`?mode=ro` in the connection string)
3. Run the equivalent of `sprintctl maintain check` — compute staleness, sprint risk
4. Print diagnostics if issues are found (stale items, overdue sprint)
5. Proceed with extraction regardless — stale data is flagged, not blocked

### How to call maintain check

Two options, in order of preference:

1. **Python import** (if sprintctl is installed in the same environment): Import `sprintctl.calc` and `sprintctl.db`, call the functions directly. Faster, no subprocess overhead. This is the expected default for co-installed tools.

2. **Subprocess** (if sprintctl is a separate install): Shell out to `sprintctl maintain check --json` (requires sprintctl to support JSON output from maintain check). More decoupled but slower.

kctl should try the import path first, fall back to subprocess, and warn if neither works. Extraction should still proceed — knowledge recovery is more important than perfect hygiene.

---

## 5. Extraction logic

### Event scanning

```python
def extract_candidates(sprintctl_conn, kctl_conn, event_types: set[str], since_event_id: int = 0) -> list[dict]:
    """
    Scan sprintctl events for knowledge-bearing entries.
    Returns list of newly created candidates.
    """
    events = sprintctl_conn.execute(
        "SELECT e.*, wi.title AS item_title, t.name AS track_name "
        "FROM event e "
        "LEFT JOIN work_item wi ON e.work_item_id = wi.id "
        "LEFT JOIN track t ON wi.track_id = t.id "
        "WHERE e.id > ? AND e.event_type IN ({}) "
        "ORDER BY e.id ASC".format(",".join("?" * len(event_types))),
        [since_event_id, *event_types],
    ).fetchall()

    created = []
    for ev in events:
        # Skip if already extracted (idempotent)
        if candidate_exists(kctl_conn, ev["id"]):
            continue
        candidate = build_candidate(ev)
        insert_candidate(kctl_conn, candidate)
        created.append(candidate)

    update_extractor_state(kctl_conn, sprintctl_db_path, max_event_id, now)
    return created
```

### Candidate building

```python
def build_candidate(event: dict) -> dict:
    payload = json.loads(event["payload"]) if event["payload"] else {}
    summary = payload.get("summary") or f'{event["event_type"]}: {event.get("item_title", "no item")}'
    return {
        "source_event_id": event["id"],
        "source_sprint_id": event["sprint_id"],
        "source_item_id": event["work_item_id"],
        "event_type": event["event_type"],
        "summary": summary,
        "detail": payload.get("detail"),
        "tags": json.dumps(payload.get("tags", [])),
        "confidence": payload.get("confidence"),
        "status": "candidate",
    }
```

### Edge cases

- **Empty payload**: Generate a minimal candidate with auto-summary. It's better to surface a low-quality candidate for review than to silently drop a knowledge signal.
- **Duplicate event IDs**: UNIQUE constraint on `source_event_id` handles this. INSERT OR IGNORE or check-before-insert.
- **sprintctl DB schema changes**: kctl should validate that the expected tables and columns exist before querying. If the schema doesn't match, fail with a clear error about version mismatch rather than a raw SQLite error.

---

## 6. Render formats

### Document format (default)

```markdown
# Knowledge Base

*Rendered: 2026-03-26T14:00:00Z — 12 active entries*

---

## Decisions

### Auth tokens must use asymmetric signing in multi-service deployments
*Source: Sprint 1 / backend — published 2026-03-25*
*Tags: auth, architecture, multi-service*

Symmetric HMAC breaks when services don't share a secret rotation schedule.
Switched to RS256 after third key sync failure.

---

### Use SQLite WAL mode for concurrent read access
*Source: Sprint 1 / infra — published 2026-03-24*
*Tags: database, concurrency*

WAL allows readers and a single writer simultaneously. Required for
calculate-on-call patterns where CLI reads overlap with maintenance writes.

---

## Patterns

...
```

Entries grouped by category, ordered by recency within each group. Superseded entries excluded unless `--all`.

### Context format

Compact, no markdown chrome, designed for prompt injection:

```
[KNOWLEDGE: 12 entries]

[DECISION] Auth tokens must use asymmetric signing in multi-service deployments
tags: auth, architecture, multi-service | sprint: Sprint 1 | track: backend
Symmetric HMAC breaks when services don't share a secret rotation schedule. Switched to RS256 after third key sync failure.

[DECISION] Use SQLite WAL mode for concurrent read access
tags: database, concurrency | sprint: Sprint 1 | track: infra
WAL allows readers and a single writer simultaneously.

[PATTERN] ...
```

---

## 7. Configuration

Same pattern as sprintctl: environment variables, escalate to config file if needed.

```sh
KCTL_DB=/path/to/kctl.db                      # default: ~/.kctl/kctl.db
SPRINTCTL_DB=/path/to/sprintctl.db             # shared with sprintctl
KCTL_EVENT_TYPES=decision,blocker-resolved,pattern-noted,risk-accepted,lesson-learned
KCTL_PREFLIGHT=true                            # run maintain check before extract (default: true)
```

If configuration grows, use `~/.kctl/config.toml`:

```toml
[sprintctl]
db = "~/.sprintctl/sprintctl.db"
preflight = true

[extract]
event_types = ["decision", "blocker-resolved", "pattern-noted", "risk-accepted", "lesson-learned"]

[render]
default_format = "document"     # or "context"
```

---

## 8. Implementation plan

### Phase 1 (must ship)

Minimal extraction and review pipeline.

**Build:**
- `kctl/db.py` — SQLite init, migrations, data access for candidates and extractor state
- `kctl/extract.py` — event scanning, candidate building, idempotent insertion
- `kctl/cli.py` — Click entry point: `extract`, `review list`, `review show`, `review approve`, `review reject`
- `tests/test_extract.py` — extraction against a fixture sprintctl DB
- `tests/test_review.py` — status transition tests

**Skip:**
- Publish/KnowledgeEntry (candidates are useful even without promotion)
- Render (query the DB directly or use `review list`)
- Pre-flight (hardcode a warning if sprintctl DB is missing)
- Full-text search

**Acceptance criteria:**
- `kctl extract` scans a sprintctl DB and creates candidates for knowledge-bearing events
- Re-running `kctl extract` does not create duplicates
- `kctl review list` shows candidates, filterable by status and tag
- `kctl review approve --id N` and `kctl review reject --id N` transition status correctly
- Transitions are enforced: only `candidate → approved`, `candidate → rejected`

### Phase 2

Publish, query, and render.

**Build:**
- KnowledgeEntry table and promotion logic
- `kctl publish` — create entries from approved candidates
- `kctl query` — search published entries by tag, category, text
- `kctl render` — document and context format output
- Pre-flight integration (import or subprocess)
- `tests/test_publish.py`, `tests/test_render.py`

### Phase 3 (optional)

- Full-text search via SQLite FTS5
- Supersede chain tracking and display
- Bulk operations (`kctl review approve --all --tag auth`)
- Export to structured formats (JSON, YAML) for integration with other tools
- Git integration: auto-commit rendered knowledge docs to a repo path

---

## 9. File structure

```
kctl/
  __init__.py
  db.py           — SQLite init, migrations, data access
  extract.py      — event scanning, candidate building
  review.py       — status transitions, validation
  publish.py      — candidate → entry promotion (Phase 2)
  render.py       — markdown/context rendering (Phase 2)
  query.py        — search and filter logic (Phase 2)
  preflight.py    — sprintctl maintain check integration
  cli.py          — Click entry point
tests/
  conftest.py     — shared fixtures (in-memory DBs for both kctl and sprintctl)
  test_extract.py
  test_review.py
  test_publish.py
  test_render.py
```

---

## 10. Dependencies

- Python 3.11+
- click (CLI framework — same as sprintctl)
- sprintctl (optional runtime dependency for pre-flight import path)
- No other non-stdlib dependencies

---

## 11. Design constraints and anti-goals

### Must not become

- **A documentation system.** kctl extracts atomic knowledge entries. It does not generate READMEs, ADRs, or runbooks. Those are downstream consumers of kctl output.
- **A sprint analytics tool.** kctl does not compute velocity, burndown, or team metrics. It captures *what was learned*, not *how fast things moved*.
- **A replacement for structured logging.** Events in sprintctl are the source of truth. kctl reads them; it does not become a parallel event store.
- **Tightly coupled to sprintctl internals.** kctl depends on sprintctl's event table schema and the documented event type conventions. If sprintctl changes its event schema, kctl should fail clearly at the schema-check step rather than silently misinterpreting data.

### Failure modes to guard against

| Failure mode                | Guardrail                                                     |
|-----------------------------|---------------------------------------------------------------|
| Knowledge rot (entries go stale) | `superseded_by` chain; render excludes superseded by default |
| Candidate pile-up (never reviewed) | `kctl review list` defaults to `--status candidate`; maintain a count in `kctl extract` output |
| Extraction from stale sprint data | Pre-flight calls maintain check; warnings surfaced           |
| Schema drift between tools  | Schema validation on connect; clear version mismatch errors  |
| Over-extraction (noise)     | Configurable event type filter; review step as quality gate   |

### Things that are explicitly soft

- Payload structure conventions (not enforced by either tool)
- Tag vocabulary (no controlled vocabulary — tags are freeform)
- Review timing (no SLA or staleness detection on unreviewed candidates)
- Pre-flight results (warnings, not blockers)

### Things that are enforced

- Candidate status transitions (`candidate → approved/rejected`, then `approved → published`)
- Idempotent extraction (UNIQUE on `source_event_id`)
- Read-only access to sprintctl DB (connection opened with `?mode=ro`)
- No writes to sprintctl DB under any circumstance

---

# Style requirements

- Match sprintctl conventions: dict-based rows, no ORM, pure functions where possible
- CLI output should be human-readable, parseable by agents, and scriptable (exit codes matter)
- Tests use in-memory SQLite — create a fixture sprintctl DB with known events
- Timestamps in UTC ISO format, passed in explicitly (not generated inside logic functions)

---

# Final check

Before finishing, ensure:

- kctl never writes to sprintctl's DB
- Extraction is idempotent against the same event set
- Every status transition is enforced in db.py, not just cli.py
- The review step is a real gate — no auto-publish path
- Published entries are portable (no foreign keys into sprintctl schema)
- The tool is useful even if Phase 2 is never built (candidates alone have value)

---

Deliver a working Phase 1 implementation with tests. Phase 2 components should be structurally anticipated (e.g. KnowledgeEntry table in migrations) but not built yet.
