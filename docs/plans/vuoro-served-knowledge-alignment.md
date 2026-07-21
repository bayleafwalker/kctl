---
doc_id: kctl-vuoro-served-knowledge-alignment
status: ratified
ratified_at: 2026-07-21
ratified_by: operator
governing_decision: agentops/docs/plans/agentops/vuoro-served-substrate-plan.md
---

# Kctl alignment with the Vuoro knowledge module

Kctl remains the owner of knowledge candidate, review, approval, rejection,
supersession, and publication-reference semantics. Git remains canonical for
authored and published document content. The Vuoro knowledge module centralizes
the cross-machine review workflow and exposed operations; it does not become a
second document repository.

## Required changes

- Extract candidate/review handlers from Click and SQLite presentation code.
- Define a central knowledge workflow schema and deployment migration entrypoint
  with separate migration/runtime roles.
- Register candidate, review, publication-reference, and read operations in the
  Vuoro catalog with stable JSON Schemas.
- Preserve content digests and Git revision references across local export and
  central migration.
- Keep local authored projections and Git publication usable during rollout.

Human-only document ratification remains a Git validation gate owned by
agentops. Kctl may surface ratification evidence but does not autonomously set
`status: ratified` or make its workflow database canonical for document text.

`vuoro-dev` acceptance covers candidate deduplication, approve/reject,
supersession, publication references, retries, and service restart without
using production knowledge state.

## Backlog registration

- **#1199** — central review schema, deployment migration, and role split.
- **#1200** — knowledge application core and Vuoro adapter/catalog; blocked by
  #1199.
