---
doc_id: kctl.central-knowledge-workflow
status: draft
governing_decision: agentops/docs/plans/agentops/vuoro-served-substrate-plan.md
---

# Central knowledge workflow contract

## Authority boundary

Kctl owns served candidate, review, supersession, and publication-reference
semantics. The central PostgreSQL schema is authoritative for that shared
workflow. Git remains authoritative for authored and published document
content. The central schema therefore stores candidate content needed for
review, but a publication stores only a repository-relative path, anchor,
full Git revision, and content digest. It has no published title or body
column and cannot become a second document repository.

Local SQLite remains supported during rollout and as an explicit recovery
input. A local-to-central transfer is a bounded migration operation, not
continuous dual authority. Local rendering and the existing read-only
knowledge artifacts remain usable and do not call the central database.

## Stored state

| Relation | Purpose | Stable identity |
| --- | --- | --- |
| `knowledge_candidate` | candidate content, provenance, lifecycle status, digest, and export-basis Git revision | `(repo_id, local_candidate_id)` and `(repo_id, source_event_id)` |
| `knowledge_review` | one imported approve/reject decision and its reviewer evidence | `candidate_id` |
| `knowledge_publication_reference` | Git-owned publication identity, digest, and supersession link without document content | `(repo_id, local_entry_id)` |
| `schema_migration` | immutable migration version, name, and SHA-256 | `version` |
| `schema_principal` | migration/runtime role and environment binding | `role_kind` |

Candidates retain the local lifecycle states `candidate`, `approved`,
`rejected`, and `published`. Reviews record `approved` or `rejected`.
Publication references point to a candidate and may point to a successor in
the same repository scope. The transfer validator rejects self-links,
missing targets, and cycles before a database transaction begins.

## Content and Git identity

Candidate digests use the existing knowledge-proposal digest over canonical
JSON containing `summary` and `detail`. Publication digests use the existing
knowledge-artifact digest over canonical JSON containing `title` and `body`.
The transfer artifact carries the local content solely to recompute and
verify these digests. Import does not persist published title or body.

Every transferred candidate and publication carries the same full 40- or
64-hex Git revision as the enclosing snapshot. Publication paths are
repository-relative POSIX paths without traversal. Import rejects an existing
stable key when its source identity, status, digest, Git revision, path, or
anchor differs; it never silently overwrites immutable evidence.

## Migration and compatibility

Schema changes run only through `python -m kctl.central_schema migrate` using the
deployment migration role. Migrations take a transaction-scoped PostgreSQL
advisory lock derived from the schema, apply contiguous packaged assets,
record each immutable checksum, configure bounded runtime grants, and commit
atomically. Concurrent jobs serialize. After acquiring the lock, a retry
re-reads the ledger; an already-current retry applies no versions.

Runtime startup and import use the read-only compatibility path. It verifies
the supported version range, migration names and checksums, expected relation
columns, recorded role and environment, and that the runtime principal is not
a superuser, owner, owner-role member, schema creator, or migration-ledger
writer. Compatibility never creates a schema or runs a migration.

## Transfer and retry semantics

`python -m kctl.central_schema export-local` reads an existing SQLite file in read-only
mode and atomically replaces one self-digesting JSON snapshot. Validation
recomputes the snapshot digest and every candidate/publication content digest
and checks all references before import.

`import-local` requires a compatible runtime role and matching environment.
It holds a transaction-scoped advisory lock per schema and repository, then
inserts candidates, reviews, and publication references in one transaction.
Stable UUIDs and immutable-key comparisons make a completed retry a no-op.
An interrupted connection has the PostgreSQL transaction outcome: after an
unknown response, retry the exact artifact. A changed artifact under an
existing local identity fails as a conflict rather than updating central
history.

## Served application boundary

The Click-independent central application core exposes single-candidate
extraction intake, bounded candidate and publication-reference reads, review
decisions, publication-reference recording, and supersession. Each operation
first requires a compatible runtime role and uses DML only. The Vuoro adapter
owns domain-qualified `knowledge.*` operation names and JSON Schemas, while the
generic service shell continues to own transport identity, authority, and
envelope enforcement.

Candidate intake derives the same stable UUID used by local transfer from the
repository and local candidate ID. The repository/source-event and
repository/local-candidate keys must identify the same immutable evidence.
An exact retry is a no-op; changed content, provenance, digest, or Git basis
under either identity is a conflict.

Approve and reject record the resolved Vuoro actor, candidate digest, and the
candidate's Git basis in `knowledge_review`. The invocation basis must match
that candidate basis. An exact decision retry returns the existing review,
including after a later publication transition; a changed or opposite retry
is rejected. These operations decide candidate review state only. They cannot
ratify an authored document or edit a document status.

A publication-reference operation accepts no title or body. It records only a
repository-relative path, anchor, full Git revision, digest, and classification,
then marks an approved candidate published in the same PostgreSQL transaction.
Optional initial supersession and the explicit supersession operation require
same-repository references and reject self-links, changed successors, and
cycles. Exact retries retain stable publication and evidence references.

List operations enforce a maximum of 100 rows in the application core as well
as in catalog schemas. Recreating the application or service does not alter
retry identity because candidate, review, and publication rows are the durable
evidence. This central atomicity does not change or silently repair the legacy
SQLite publication path, whose separate-commit recovery limitation remains as
documented in `knowledge-lifecycle.md`.

## Environment isolation and rollback

Each deployment schema records `environment_name` and `environment_class`.
The `vuoro-dev` caller must require `environment_name=vuoro-dev` and
`environment_class=development`; a differently bound schema fails
compatibility before import. Separate DSNs, credentials, databases, network
policy, and Kubernetes Jobs remain appservice-owned controls.

Rollback preserves Git and the local SQLite/export artifact as recovery
inputs. Migration history is append-only: do not edit released SQL, delete
ledger rows, or run a down migration in place. Restore a database backup or
ship a forward migration, then re-run compatibility with the selected service
image before traffic. Production execution, backup creation, and concrete
role provisioning are outside this repository.

## Evidence boundary

The hermetic PostgreSQL gate covers empty/current upgrade, serialized
concurrent migration, retry, checksum drift, runtime DDL and ledger-write
denial, local import retry, Git/digest preservation, absence of published
content columns, and isolated environment schemas. This is bounded
concurrency evidence, not a claim of distributed transaction or cross-schema
atomicity.
