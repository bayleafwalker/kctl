---
doc_id: kctl.frozen-source-preflight
status: draft
---

# Frozen knowledge source preflight

`python -m kctl.source_preflight --snapshot <file> --sha256 <external-sha256>`
qualifies coverage of explicit frozen SQLite bytes. It never reads `KCTL_DB`,
opens a served profile, imports data, migrates a source or destination, changes
review state, or authorizes S4 cutover. It complements the existing central
transfer artifact; it does not widen that older destination contract.

## Prepare a standalone snapshot

Use SQLite's backup API to a **new** private file, then set `journal_mode=DELETE`
on that new copy and close it. For example, with source and destination paths
chosen explicitly by the operator:

```python
from pathlib import Path
from contextlib import closing
import hashlib
import sqlite3

source = Path('/private/source/kctl.db')
snapshot = Path('/private/review/kctl-frozen.sqlite')
# The review directory must already have appropriate access controls.
with snapshot.open('xb'):
    snapshot.chmod(0o600)
with closing(sqlite3.connect(source.absolute().as_uri() + '?mode=ro', uri=True)) as reader:
    with closing(sqlite3.connect(snapshot)) as frozen:
        reader.backup(frozen)
        frozen.execute('PRAGMA journal_mode=DELETE')
print(hashlib.sha256(snapshot.read_bytes()).hexdigest())
```

Retain the hash independently before qualification. This example creates a new
snapshot and does not change the source's journal mode. Do not copy a live
SQLite main file in place of a backup: committed rows may exist only in its WAL.
Even checkpointed WAL databases and backup output may retain WAL header flags.
Preflight refuses WAL-header inputs; it never discovers/replays sidecars or
rewrites header bytes. It establishes coverage of the supplied standalone
snapshot only, not of a live source, recoverable tail or full estate inventory.

## Coverage and digests

The externally supplied SHA-256 binds the exact file bytes, bounded to 64 MiB.
A no-follow, nonblocking open requires a regular file. The bytes are deserialized
into an in-memory SQLite connection with `trusted_schema=OFF` and `query_only=ON`.
The source file is never opened as a SQLite database and cannot gain sidecars.

The accepted source shape is knowledge schema version 9, with the exact known
tables, column names/types and integer primary-key identities. Actual row identities must also be unique
integers; a declared SQLite primary key alone does not prove this. Unknown tables,
views, triggers, virtual tables, extra/hidden columns and newer versions are
refused rather than silently omitted. SQLite integrity checking must pass.
This shape check is not a proof that all historical constraints or workflows
were followed.

Coverage includes every row and column in `schema_version`, `knowledge_candidate`,
`knowledge_entry`, `extractor_state`, `extractor_state_v2` and `sqlite_sequence`.
At most 100,000 total rows are admitted, including schema and allocation counters.
Candidates with NULL sprintctl identities and authored `source_origin` values,
and tenet/direction publications, remain represented. No transfer-v1 category
or source-ID restriction is applied. Stored JSON strings are hashed as authored
text, without parsing, trimming, reconstructing or replacing them.

The report contains table counts and one SHA-256 per table/row ordinal. Row order
is primary id order, `sqlite_sequence.name` order, or schema version order. The
`kctl-source-row/v1` digest input is compact, sorted-key, UTF-8 JSON containing
`domain`, `table`, sorted `columns`, and corresponding type-tagged `values`.
Tags are `null`, `text`, `blob` (base64), `integer` (decimal string), and `real`
(Python float hex for finite values, otherwise its infinity string). The
`kctl-source-schema/v1` digest binds the ordered `(type,name,tbl_name,sql)` schema
objects with the same cell encoding. These are new comparison digests, not
original authored or central-transfer digests. The whole-file SHA-256 remains
the original byte identity.

Output omits paths, bodies, payloads, origin names and reviewer identities.
Hashes and numeric IDs still identify source records; retain reports under
access controls appropriate to the source. The original frozen snapshot must
be retained separately: a redacted report cannot reconstruct its content.

## Publication findings and limits

Coverage and publication consistency are separate results. Findings name
partial/duplicate/missing publications, missing candidates or supersession
targets, stream mismatches, and supersession cycles, using only table names,
IDs and fixed finding codes. All affected rows remain counted and hashed.
Traversal is linear in the number of entries; findings are bounded by three
times the total row bound. No source inconsistency is repaired or hidden.

A report exits successfully when source coverage can be computed, even when
`publication_consistency=inconsistent`. Invalid, changed or unsupported source
bytes fail with a controlled error and no partial JSON output. Consistent
publication relationships do not establish historical reviewer authenticity,
source completeness, extraction-watermark correctness or destination admission.
Every report explicitly states `qualification=source-coverage-only`,
`s4_authority_established=false`, `cutover_authorized=false`, and
`live_tail_coverage_established=false`. TS-13 migration still needs its owning
source/destination contract and verified import; this report grants neither.
