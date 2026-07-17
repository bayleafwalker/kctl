---
doc_id: kctl.knowledge-artifact
status: draft
supersedes: null
---

# knowledge-artifact/v1

## Purpose and ownership

knowledge-artifact/v1 is kctl's read-only export for cockpit knowledge
reads. It projects only entries that kctl has already published; it is not a
review queue, a kctl database dump, or a command surface. kctl remains the
owner of candidate review, publication, and supersession. The cockpit may
display this artifact but cannot mutate the lifecycle it describes.

The authoritative state is kctl's SQLite knowledge_entry joined to its source
knowledge_candidate. This artifact and committed Markdown are derived
projections.

## Location and scope

An exporter writes a complete NDJSON snapshot to:

    $KCTL_ARTIFACTS_ROOT/<repo_id>/knowledge/knowledge-artifact-v1.ndjson

repo_id is the repository scope supplied by the export invocation. It must
match the repository whose kctl store is being exported, and uses the same
identifier grammar as dispatch manifests. Deployments that share the cockpit
artifact root set KCTL_ARTIFACTS_ROOT to that root; the cockpit reads the same
path through COCKPIT_ARTIFACTS_ROOT.

The file contains one JSON object per line and no header, blank lines, or
candidate, approved, or rejected records. An empty published set is a valid
zero-byte snapshot.

## Record contract

Each line validates against docs/protocols/knowledge-artifact-v1.schema.json.
Required fields are:

| Field | Meaning |
| --- | --- |
| schema_version | Exactly knowledge-artifact/v1. |
| repo_id, entry_id | Stable composite identity for the published entry. |
| stream | durable or coordination, matching kctl's published stream. |
| status | Always published in v1; it is lifecycle information, not a writable state request. |
| title, body, category, tags | The published knowledge content. |
| content_digest | sha256: plus the SHA-256 of canonical UTF-8 JSON for title and body: lexical key order, no whitespace, and unescaped Unicode. |
| source | The original sprintctl event, plus the source sprint, optional work item, and track captured by kctl. event_ref is exactly sprintctl:event:<event_id>. |
| published_at | The entry's kctl publication timestamp. |
| rendered_at | The UTC time this record was written into the artifact snapshot. |
| superseded_by | The successor entry id, or null when the entry remains current. Superseded entries remain visible history. |

The canonical digest input is the JSON object with body and title keys, in
lexical key order, compact separators, and UTF-8 without ASCII escaping. The
current kctl JSON render already exposes entry, stream, source sprint, track,
publication timestamp, and supersession data. The exporter adds the source
event join, artifact identity, digest, and snapshot timestamp without
changing publication semantics.

## Snapshot, idempotency, and recovery

The exporter for this contract writes the entire selected published set in
ascending entry_id order to a sibling temporary file, flushes it, then
atomically replaces the destination. It does not append individual records.
An interrupted export therefore leaves either the previous complete snapshot
or the next complete snapshot; a subsequent export safely converges.

rendered_at is expected to change on a successful rewrite. Idempotency is
semantic rather than byte-for-byte: repeating an export for unchanged
published state yields the same (repo_id, entry_id) set and the same
published content, source fields, digest, and supersession relation, with no
duplicate records. Consumers must key records by (repo_id, entry_id) and may
prefer the greatest rendered_at if a retained snapshot is observed more than
once.

The v1 exporter has no concurrent-writer guarantee. It is safe for one
process at a time, matching kctl's local-first SQLite model.

## Consumer rules and non-goals

- Treat absent files, an empty snapshot, or an unavailable artifact root as
  degraded-but-readable-empty state; do not substitute Markdown scraping or
  direct SQLite reads.
- Reject a malformed line as artifact corruption for that read and report the
  degradation. Do not infer or repair kctl lifecycle state.
- Display published and superseded history accurately. Do not turn
  superseded_by into deletion.
- Never expose candidates, reviewer identity, raw source payloads, local
  SQLite paths, credentials, or write controls through this artifact.
- A future schema version uses a distinct filename and schema_version; v1
  consumers must not silently reinterpret it.

## Evidence and delivery boundary

This contract is a Depth-0 projection review. Its reusable context is
verification/contexts/knowledge-artifact.json and its fixture is
verification/examples/knowledge-artifact-v1.ndjson. The contract is complete
with this design item (#954). Implementing and testing the kctl export
producer is the separate #955 item; cockpit consumption remains blocked until
that producer ships.
