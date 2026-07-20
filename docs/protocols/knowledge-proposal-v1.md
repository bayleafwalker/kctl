---
doc_id: kctl.knowledge-proposal
status: draft
supersedes: null
---

# knowledge-proposal/v1

## Purpose and ownership

knowledge-proposal/v1 is kctl's read-only export of **approved-but-not-yet-published**
knowledge, shaped as bounded suggestions for backlog refinement. It is not a
review queue, a command surface, or accepted state. Per the ownership matrix
in `agentops/docs/plans/agentops/state-event-command-matrix.md`, this is an
*observation (proposal artifact)*: it never mutates authoritative state.
sprintctl remains the sole backlog writer. kctl remains the owner of
candidate review and publication; this artifact only projects what kctl
already knows to be approved.

The authoritative state is kctl's SQLite `knowledge_candidate` table, filtered
to `status = 'approved'`. This artifact is a derived projection, not a new
store.

## Scope: approved, not published

A candidate appears in this artifact only while it is `approved` — reviewed
and confirmed valuable, but not yet promoted by `kctl publish` into a
`knowledge_entry`. The moment a candidate is published, it drops out of this
artifact (it already has a durable home in the knowledge base) and only
appears in `knowledge-artifact-v1.ndjson` going forward. `candidate` and
`rejected` candidates never appear here — see `docs/protocols/knowledge-lifecycle.md`
for the full transition graph.

## Location and scope

An exporter writes a complete NDJSON snapshot to:

    $KCTL_ARTIFACTS_ROOT/<repo_id>/knowledge/knowledge-proposal-v1.ndjson

`repo_id` is the repository scope whose kctl store is being exported (the
repo the knowledge was extracted from), supplied the same way as `kctl
export`'s `--repo-id`. The file contains one JSON object per line, no header,
and no candidate/rejected/published records. An empty approved set is a
valid zero-byte snapshot.

## Record contract

| Field | Meaning |
| --- | --- |
| schema_version | Exactly `knowledge-proposal/v1`. |
| repo_id | The repo whose kctl store produced this candidate. |
| candidate_id | The kctl candidate identity. Stable key alongside repo_id. |
| stream | `durable` or `coordination`, matching the candidate's kind. |
| status | Always `approved` in v1 — informational, not a writable state request. |
| summary, detail, tags | The reviewed knowledge content, as edited during `kctl review approve`. |
| content_digest | `sha256:` plus the SHA-256 of canonical UTF-8 JSON for `summary` and `detail`. |
| provenance | The originating sprintctl event (`event_ref` is exactly `sprintctl:event:<event_id>`), sprint, optional work item and track, plus who/when approved it inside kctl. |
| suggested_owner_repo | The repo a human or agent should consider filing a backlog item against. Defaults to `repo_id`; an exporter invocation may override it for every record in that run. |
| suggested_next_action | A human-readable suggestion string, e.g. `propose sprintctl item add in repo <suggested_owner_repo>`. It is prose, not a command kctl runs. |
| extracted_at, rendered_at | When kctl first extracted the candidate, and when this snapshot line was written. |

There is no `superseded_by`, `published_at`, or sprintctl item reference:
this artifact never claims a sprintctl item exists. See
`docs/protocols/knowledge-proposal-v1.schema.json` for the enforced shape.

## Non-goals (the boundary this contract preserves)

- This artifact never creates, updates, or closes a sprintctl item.
- This artifact never calls a sprintctl mutation command, directly or via
  subprocess.
- A record appearing here is **not** accepted state. Acceptance only exists
  once a human or a separately authorized agent runs an explicit sprintctl
  command (e.g. `sprintctl item add ...`) — see
  `docs/examples/knowledge-proposal-consumer.md` for a worked walkthrough.
- `suggested_owner_repo` and `suggested_next_action` are proposals, not
  routing authority. A consumer may disagree and file the item elsewhere, or
  not file it at all.
- kctl does not track whether a proposal was acted on. If the underlying
  candidate is later published, it simply stops appearing in this artifact;
  kctl has no notion of "proposal accepted/rejected."

## Snapshot, idempotency, and recovery

Same durability model as `knowledge-artifact-v1`: the exporter writes the
complete selected set in ascending `candidate_id` order to a sibling
temporary file, flushes it, and atomically replaces the destination. An
interrupted export leaves either the previous complete snapshot or the next
complete snapshot. Idempotency is semantic: repeating an export over
unchanged approved state yields the same `(repo_id, candidate_id)` set and
content, with a fresh `rendered_at`.

## Evidence and delivery boundary

Fixture-based deterministic export coverage lives in
`tests/test_knowledge_proposal_export.py`. It asserts: only `approved`
candidates are included; published, rejected, and still-pending candidates
are excluded; the snapshot atomically replaces rather than appends; an empty
approved set produces a zero-byte file; and `suggested_owner_repo` /
`suggested_next_action` are deterministic given the export invocation.
