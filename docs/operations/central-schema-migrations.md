# Central knowledge schema operations

Central schema migration is an explicit deployment action. Neither ordinary
`kctl` startup nor the future Vuoro knowledge adapter calls it automatically.

## Deployment sequence

1. Confirm that the secret-backed migration DSN, schema, environment name,
   and environment class identify the intended deployment. Record a database
   backup/restore point.
2. Run the packaged migrator in one foreground deployment Job:

   ```bash
   export KCTL_CENTRAL_MIGRATION_DSN='postgresql://...'
   python -m kctl.central_schema migrate \
     --schema knowledge \
     --migration-role kctl_migration \
     --runtime-role kctl_runtime \
     --environment-name vuoro-dev \
     --environment-class development
   ```

3. Use the runtime identity to run the read-only gate:

   ```bash
   export KCTL_CENTRAL_RUNTIME_DSN='postgresql://...'
   python -m kctl.central_schema check-compatibility \
     --schema knowledge \
     --expected-environment-name vuoro-dev \
     --expected-environment-class development
   ```

4. Start the served adapter only after both commands pass. Record the image
   digest, migration JSON, compatibility JSON, environment identity (without
   credentials), and backup reference.

The migrator serializes by schema with a transaction advisory lock. Retrying
an already-current deployment returns an empty `applied_versions` list.
Checksum, version, shape, role, or environment drift fails closed.

## Roles

Appservice creates the concrete login roles and secrets. The migration role
must be distinct from the runtime role and is the only role allowed to create
or alter central knowledge objects and write `schema_migration`.

The migrator grants the runtime role schema `USAGE`, candidate/review/
publication-reference DML, sequence use, and read access to migration and
principal records. It revokes schema `CREATE`, relation ownership, migration
ledger writes, and public relation privileges. The compatibility gate also
rejects superusers and roles that can assume an object owner; a role that can
alter objects is not a safe runtime role even when schema `CREATE` was
revoked.

## Local transfer

Create and validate a recovery/import snapshot from an existing local store:

```bash
python -m kctl.central_schema export-local \
  --db-path .kctl/kctl.db \
  --repo-id kctl \
  --git-revision "$(git rev-parse HEAD)" \
  --content-path docs/knowledge/knowledge-base.md \
  --exported-at 2026-07-21T12:00:00Z \
  --output /secure/path/kctl-central-transfer.json

python -m kctl.central_schema validate-artifact /secure/path/kctl-central-transfer.json
```

Import with the runtime identity after migration:

```bash
python -m kctl.central_schema import-local \
  --schema knowledge \
  --expected-environment-name vuoro-dev \
  --expected-environment-class development \
  /secure/path/kctl-central-transfer.json
```

The exact artifact is safe to retry. A different digest or Git reference for
an already-imported local identity is a conflict that requires operator
reconciliation. Treat snapshots as recovery material because they contain
candidate and local published content needed to verify digests; access-control
and retention policy are deployment responsibilities.

Retry validation compares every artifact-derived column persisted for the
candidate, review, and publication reference, including source provenance,
reviewer evidence, document identity, tags, and timestamps. Only
database-generated bookkeeping such as `imported_at` and the review sequence
is outside that equality check.

## Verification dependency

The PostgreSQL migration and retry gates require the `remote` extra and local
PostgreSQL server binaries. Run the repository suite with:

```bash
uv run --all-extras pytest
```

The current GitHub Actions workflow installs only the `dev` extra, so it skips
these critical tests; changing `.github/workflows/ci.yml` is outside the
manifest-allowed roots for this item and remains follow-up CI debt.

## Recovery

Never modify a released migration or delete its ledger row. On failure, keep
the runtime stopped, preserve Job logs, and restore the recorded database
backup or deploy a reviewed forward migration. Git and local exports remain
the authoritative content/recovery inputs. A rollback image may start only
when its compatibility range includes the restored schema version.
