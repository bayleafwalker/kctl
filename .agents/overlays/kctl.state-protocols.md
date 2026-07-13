# kctl state-protocol overlay

## Closed subjects

| Subject | State owner | Default depth | Primary anchors |
|---|---|---:|---|
| Scoped extraction watermark | `extractor_state_v2` | 1 | `kctl.extract:build_scope_key`, `extract_candidates` |
| Source-event deduplication | `knowledge_candidate.source_event_id` | 1 | `kctl.db:insert_candidate` |
| Candidate lifecycle | `knowledge_candidate.status` | 1 | `kctl.db:transition_candidate` |
| Publication and supersession | `knowledge_entry` plus candidate status | 1 | `kctl.publish:publish_candidate` |
| Rendered knowledge | Derived Markdown/JSON projection | 1 | render module and CLI |

## Required scenarios

- Repeating extraction for the same event set creates no duplicate candidates.
- A filtered extraction scope cannot advance another scope's watermark.
- Crash after some candidate inserts but before watermark update converges on rerun.
- Invalid candidate transitions leave state unchanged.
- Publication rejects non-approved candidates and missing supersession targets.
- Crash between entry insertion, supersession update, and candidate publication is detected and reconcilable.
- Repeated rendering over stable published state is deterministic modulo explicitly documented timestamps.

## Current limitations

- kctl does not claim safe concurrent writers across processes.
- Candidate insertion and watermark updates use separate commits but source-event uniqueness makes extraction restartable.
- Publication currently performs entry insertion, optional supersession, and candidate transition through separate commits. Do not claim atomic publication or retry idempotency.

Use temporary SQLite databases. Record the extraction scope key, source event range, injected interruption point, and resulting candidate/entry sets.
