---
doc_id: kctl.knowledge-lifecycle
status: draft
supersedes: null
---

# Knowledge lifecycle protocol

## Boundary and ownership

kctl reads sprintctl events without mutating sprintctl. Its authoritative state is the local SQLite store containing extraction watermarks, candidates, published entries, and supersession links. Rendered Markdown, JSON, and knowledge-artifact/v1 NDJSON are derived projections.

## Extraction

Extraction is scoped by sprint ID and the effective event-type set. Each scope has an independent watermark. A candidate is uniquely keyed by the sprintctl source event ID. The source may be a local SQLite database, a tenant-scoped remote PostgreSQL read connection, or a served Vuoro read client. Remote watermark keys identify the repository as `remote://<repo_id>` and never persist its connection URL. Served watermark keys identify the same repository as `served://<repo_id>` and never persist a profile endpoint or credential reference.

For extraction, the served source invokes only the authenticated `work.read.events` catalog operation through `vuoro-client`; it does not invoke the sprintctl CLI or open a sprintctl database. That operation is sprint-scoped and paginates with an ordinal `after_offset`, while the extraction watermark remains the durable source event ID. A served extraction therefore pages from ordinal zero and locally selects event IDs greater than its durable watermark. It must not store or compare an ordinal cursor as an event-ID watermark. The catalog has no `maintain.check` equivalent, so served preflight fails before attempting a sprint-read composition with the stable `served-operation-unavailable` result. Default extraction stops on that diagnostic; an operator may explicitly use `--no-preflight` only after recording sprint health through the owning sprintctl workflow.

The implementation may commit candidate rows before committing the new watermark. After interruption, rerunning the same scope can revisit events; duplicate candidate inserts are ignored by the source-event uniqueness constraint. This provides restartable at-least-once scanning with an at-most-one stored candidate per source event.

## Candidate transitions

| Current state | Allowed operation | New state |
|---|---|---|
| `candidate` | approve | `approved` |
| `candidate` | reject | `rejected` |
| `approved` | publish | `published` |
| `rejected` | none | terminal |
| `published` | none | terminal |

Invalid transitions must leave the candidate unchanged.

## Publication

Publication requires an approved candidate, valid category, and valid optional supersession target. It creates a knowledge entry, may update an older entry's `superseded_by`, and transitions the candidate to `published`.

These writes currently commit separately. A crash can therefore leave an entry created while its candidate remains `approved`, or leave supersession updated before the candidate transition. Retrying publication is not guaranteed idempotent. Until the implementation changes, recovery is inspect-and-reconcile rather than blind retry.

## Projection semantics

Rendered output is a view of published entries selected by kind, category, tag, or sprint. It must not imply that candidate or approved entries are published. Superseded entries remain part of history and are annotated rather than deleted.

The knowledge-artifact/v1 exporter writes a complete NDJSON projection of both
published streams with atomic replacement. It is read-only: it does not
advance a watermark, transition a candidate, or repair an incomplete
publication. A missing or malformed artifact is a reader-side degradation,
not evidence that kctl state changed.

The knowledge-proposal/v1 exporter is a second, separate read-only projection
scoped to `approved` candidates that have not yet been published. It is not a
review queue and does not change candidate status. Its records add a
suggested owner repository and a suggested next-action string; neither is a
sprintctl call, and neither marks the candidate as acted upon. A candidate
leaves this projection only when kctl's own review/publish commands change
its status, never as a side effect of exporting or reading the artifact.

## Safety properties

- One source event produces at most one stored candidate.
- Extraction watermarks are isolated by source identity and scope key. A served
  ordinal cursor is never a watermark.
- Candidate status follows only the declared transition graph.
- Only approved candidates are eligible for publication.
- A supersession target exists and cannot be the new entry itself.
- Rendered knowledge includes only state permitted by the selected published stream.
- The knowledge-artifact/v1 snapshot contains one record per published entry
  and preserves source and supersession references.

## Liveness and recovery

- No concurrent-writer progress or fairness guarantee is made.
- Extraction progresses when invoked and the source remains readable. Remote extraction uses a transaction-default read-only connection and never bootstraps or writes sprintctl schema or state. Served extraction invokes only the read catalog operation above and likewise never writes sprintctl state.
- Partial extraction converges on rerun through source-event deduplication.
- Partial publication requires explicit reconciliation; blind retry is unsafe.
- An interrupted artifact export preserves either the previous complete
  snapshot or a subsequent complete replacement; it never requires lifecycle
  repair.

## Evidence

Reusable test intent lives in `verification/contexts/knowledge-lifecycle.json`. Claims remain `documented-only` unless an executed result packet establishes a stronger evidence class.
