"""Deployment-owned PostgreSQL schema and local-transfer entrypoints.

Only the explicit migration command may execute DDL.  Runtime compatibility
and local artifact import use a separately recorded role and fail closed when
the schema, migration ledger, environment binding, or role authority differs
from this contract.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from vuoro_schema_runtime import (
    MigrationAsset,
    identifier,
    quote_identifier,
    render_schema_sql,
    sha256_text,
    validate_contiguous_migrations,
)

from . import db as _db
from . import transfer
from .central_migrations import MIGRATION_ASSETS


CURRENT_SCHEMA_VERSION = 4
MIN_RUNTIME_SCHEMA_VERSION = 3
MAX_RUNTIME_SCHEMA_VERSION = 3
DOMAIN_API_VERSION = "knowledge/v1"
MIGRATION_LOCK_NAMESPACE = "kctl-central-schema"
ENVIRONMENT_CLASSES = {"development", "production", "recovery"}

_COLUMN_SHAPE: dict[str, dict[str, tuple[str, str]]] = {
    "schema_migration": {
        "version": ("int4", "NO"),
        "name": ("text", "NO"),
        "sha256": ("text", "NO"),
        "applied_at": ("timestamptz", "NO"),
    },
    "knowledge_candidate": {
        "candidate_id": ("uuid", "NO"),
        "repo_id": ("text", "NO"),
        "local_candidate_id": ("int8", "NO"),
        "source_event_id": ("int8", "NO"),
        "source_sprint_id": ("int8", "NO"),
        "source_item_id": ("int8", "YES"),
        "source_track": ("text", "YES"),
        "source_actor": ("text", "YES"),
        "source_type": ("text", "YES"),
        "source_created_at": ("timestamptz", "YES"),
        "source_payload": ("jsonb", "YES"),
        "event_type": ("text", "NO"),
        "candidate_kind": ("text", "NO"),
        "summary": ("text", "NO"),
        "detail": ("text", "YES"),
        "tags": ("jsonb", "NO"),
        "confidence": ("text", "YES"),
        "status": ("text", "NO"),
        "content_digest": ("text", "NO"),
        "basis_git_revision": ("text", "NO"),
        "extracted_at": ("timestamptz", "NO"),
        "imported_at": ("timestamptz", "NO"),
    },
    "knowledge_review": {
        "review_id": ("int8", "NO"),
        "candidate_id": ("uuid", "NO"),
        "decision": ("text", "NO"),
        "reviewed_at": ("timestamptz", "NO"),
        "reviewed_by": ("text", "NO"),
        "review_notes": ("text", "YES"),
        "content_digest": ("text", "NO"),
        "basis_git_revision": ("text", "NO"),
        "imported_at": ("timestamptz", "NO"),
    },
    "knowledge_publication_reference": {
        "publication_id": ("uuid", "NO"),
        "repo_id": ("text", "NO"),
        "local_entry_id": ("int8", "NO"),
        "candidate_id": ("uuid", "NO"),
        "document_id": ("text", "NO"),
        "content_path": ("text", "NO"),
        "content_anchor": ("text", "NO"),
        "git_revision": ("text", "NO"),
        "content_digest": ("text", "NO"),
        "category": ("text", "NO"),
        "source_kind": ("text", "NO"),
        "tags": ("jsonb", "NO"),
        "published_at": ("timestamptz", "NO"),
        "superseded_by": ("uuid", "YES"),
        "inline_supersedes": ("uuid", "YES"),
        "imported_at": ("timestamptz", "NO"),
    },
    "schema_principal": {
        "role_kind": ("text", "NO"),
        "role_name": ("name", "NO"),
        "environment_name": ("text", "NO"),
        "environment_class": ("text", "NO"),
        "recorded_at": ("timestamptz", "NO"),
    },
}
_REQUIRED_COLUMNS = {table: set(columns) for table, columns in _COLUMN_SHAPE.items()}
_REQUIRED_STRUCTURAL_CONSTRAINTS = {
    ("schema_migration", "schema_migration_pkey"): ("p", ("version",), None, ()),
    ("knowledge_candidate", "knowledge_candidate_pkey"): (
        "p",
        ("candidate_id",),
        None,
        (),
    ),
    ("knowledge_candidate", "knowledge_candidate_local_key"): (
        "u",
        ("repo_id", "local_candidate_id"),
        None,
        (),
    ),
    ("knowledge_candidate", "knowledge_candidate_source_event_key"): (
        "u",
        ("repo_id", "source_event_id"),
        None,
        (),
    ),
    ("knowledge_review", "knowledge_review_pkey"): (
        "p",
        ("review_id",),
        None,
        (),
    ),
    ("knowledge_review", "knowledge_review_candidate_key"): (
        "u",
        ("candidate_id",),
        None,
        (),
    ),
    ("knowledge_review", "knowledge_review_candidate_fk"): (
        "f",
        ("candidate_id",),
        "knowledge_candidate",
        ("candidate_id",),
    ),
    (
        "knowledge_publication_reference",
        "knowledge_publication_reference_pkey",
    ): ("p", ("publication_id",), None, ()),
    (
        "knowledge_publication_reference",
        "knowledge_publication_reference_candidate_id_key",
    ): ("u", ("candidate_id",), None, ()),
    ("knowledge_publication_reference", "knowledge_publication_candidate_fk"): (
        "f",
        ("candidate_id",),
        "knowledge_candidate",
        ("candidate_id",),
    ),
    ("knowledge_publication_reference", "knowledge_publication_local_key"): (
        "u",
        ("repo_id", "local_entry_id"),
        None,
        (),
    ),
    (
        "knowledge_publication_reference",
        "knowledge_publication_repo_identity_key",
    ): ("u", ("repo_id", "publication_id"), None, ()),
    (
        "knowledge_publication_reference",
        "knowledge_publication_supersession_fk",
    ): (
        "f",
        ("repo_id", "superseded_by"),
        "knowledge_publication_reference",
        ("repo_id", "publication_id"),
    ),
    (
        "knowledge_publication_reference",
        "knowledge_publication_inline_supersession_fk",
    ): (
        "f",
        ("repo_id", "inline_supersedes"),
        "knowledge_publication_reference",
        ("repo_id", "publication_id"),
    ),
    ("schema_principal", "schema_principal_pkey"): (
        "p",
        ("role_kind",),
        None,
        (),
    ),
    ("schema_principal", "schema_principal_role_name_key"): (
        "u",
        ("role_name",),
        None,
        (),
    ),
}
_REQUIRED_CHECK_FRAGMENTS = {
    ("knowledge_candidate", "knowledge_candidate_candidate_kind_check"): (
        "candidate_kind",
        "'durable'",
        "'coordination'",
    ),
    ("knowledge_candidate", "knowledge_candidate_status_check"): (
        "status",
        "'candidate'",
        "'approved'",
        "'rejected'",
        "'published'",
    ),
    ("knowledge_candidate", "knowledge_candidate_content_digest_check"): (
        "content_digest",
        "sha256:",
        "64",
    ),
    ("knowledge_candidate", "knowledge_candidate_basis_git_revision_check"): (
        "basis_git_revision",
        "40",
        "24",
    ),
    ("knowledge_review", "knowledge_review_decision_check"): (
        "decision",
        "'approved'",
        "'rejected'",
    ),
    ("knowledge_review", "knowledge_review_content_digest_check"): (
        "content_digest",
        "sha256:",
        "64",
    ),
    (
        "knowledge_publication_reference",
        "knowledge_publication_reference_content_path_check",
    ): ("content_path", r"\.\.", r"\\"),
    (
        "knowledge_publication_reference",
        "knowledge_publication_reference_git_revision_check",
    ): ("git_revision", "40", "24"),
    (
        "knowledge_publication_reference",
        "knowledge_publication_reference_content_digest_check",
    ): ("content_digest", "sha256:", "64"),
    (
        "knowledge_publication_reference",
        "knowledge_publication_reference_category_check",
    ): (
        "category",
        "'decision'",
        "'pattern'",
        "'lesson'",
        "'risk'",
        "'reference'",
        "'tenet'",
        "'direction'",
    ),
    (
        "knowledge_publication_reference",
        "knowledge_publication_reference_source_kind_check",
    ): ("source_kind", "'durable'", "'coordination'"),
    ("schema_principal", "schema_principal_role_kind_check"): (
        "role_kind",
        "'migration'",
        "'runtime'",
    ),
    ("schema_principal", "schema_principal_environment_class_check"): (
        "environment_class",
        "'development'",
        "'production'",
        "'recovery'",
    ),
}
_REQUIRED_INDEXES = {
    "knowledge_candidate_status_idx": (
        "knowledge_candidate",
        ("repo_id", "candidate_kind", "status", "extracted_at"),
        (False, False, False, True),
    ),
    "knowledge_publication_document_idx": (
        "knowledge_publication_reference",
        ("repo_id", "document_id", "published_at"),
        (False, False, True),
    ),
}


class CentralSchemaError(RuntimeError):
    """Base error for explicit central knowledge schema operations."""


class MigrationRoleError(CentralSchemaError):
    """The migration entrypoint is running as an unexpected database role."""


class MigrationDriftError(CentralSchemaError):
    """Applied migrations differ from the immutable packaged assets."""


class SchemaCompatibilityError(CentralSchemaError):
    """The served knowledge runtime cannot safely use the selected schema."""


class EnvironmentBindingError(CentralSchemaError):
    """An initialized schema cannot be relabeled as another environment."""


# Preserve the existing domain-module type name while its implementation comes
# from the shared, database-independent runtime.
Migration = MigrationAsset


@dataclass(frozen=True)
class MigrationResult:
    schema: str
    installed_version: int
    applied_versions: tuple[int, ...]
    migration_role: str
    runtime_role: str
    environment_name: str
    environment_class: str


@dataclass(frozen=True)
class Compatibility:
    schema: str
    domain_api_version: str
    installed_schema_version: int | None
    minimum_schema_version: int
    maximum_schema_version: int
    current_role: str
    expected_role_kind: str
    configured_role: str | None
    environment_name: str | None
    environment_class: str | None
    compatible: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reasons"] = list(self.reasons)
        return value


def _identifier(value: str, field: str) -> str:
    return identifier(value, field)


def _quoted(value: str, field: str) -> str:
    return quote_identifier(value, field)


def _environment_name(value: str) -> str:
    if not value or len(value) > 128 or any(ord(character) < 32 for character in value):
        raise ValueError("environment_name must be 1-128 printable characters")
    return value


def _environment_class(value: str) -> str:
    if value not in ENVIRONMENT_CLASSES:
        raise ValueError(
            "environment_class must be development, production, or recovery"
        )
    return value


def load_migrations() -> tuple[MigrationAsset, ...]:
    migrations: list[MigrationAsset] = []
    for version, (name, sql) in enumerate(MIGRATION_ASSETS, start=1):
        migrations.append(
            MigrationAsset(
                version=version,
                name=name,
                sql=sql,
                sha256=sha256_text(sql),
            )
        )
    versions = [migration.version for migration in migrations]
    expected = list(range(1, CURRENT_SCHEMA_VERSION + 1))
    if versions != expected:
        raise CentralSchemaError(
            f"migration assets must be contiguous through {CURRENT_SCHEMA_VERSION}: {versions}"
        )
    return validate_contiguous_migrations(migrations)


def _statements(sql: str) -> tuple[str, ...]:
    """Split the plain packaged DDL; assets contain no procedural blocks."""
    return tuple(statement.strip() for statement in sql.split(";") if statement.strip())


def _current_user(conn: Any) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT current_user")
        row = cur.fetchone()
    return str(row[0] if not isinstance(row, dict) else row["current_user"])


def _bootstrap_ledger(conn: Any, schema: str) -> None:
    schema_ident = _quoted(schema, "schema")
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema_ident}")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {schema_ident}.schema_migration (
                version     integer     PRIMARY KEY CHECK (version > 0),
                name        text        NOT NULL,
                sha256      text        NOT NULL CHECK (sha256 ~ '^[0-9a-f]{{64}}$'),
                applied_at  timestamptz NOT NULL DEFAULT clock_timestamp()
            )
            """
        )


def _applied_migrations(conn: Any, schema: str) -> dict[int, tuple[str, str]]:
    schema_ident = _quoted(schema, "schema")
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT version, name, sha256 FROM {schema_ident}.schema_migration ORDER BY version"
        )
        rows = cur.fetchall()
    result: dict[int, tuple[str, str]] = {}
    for row in rows:
        if isinstance(row, dict):
            result[int(row["version"])] = (str(row["name"]), str(row["sha256"]))
        else:
            result[int(row[0])] = (str(row[1]), str(row[2]))
    return result


def _configured_principal(
    conn: Any, *, schema: str, role_kind: str
) -> tuple[str, str, str] | None:
    schema_ident = _quoted(schema, "schema")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT role_name::text, environment_name, environment_class
            FROM {schema_ident}.schema_principal
            WHERE role_kind = %s
            """,
            (role_kind,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return (
            str(row["role_name"]),
            str(row["environment_name"]),
            str(row["environment_class"]),
        )
    return (str(row[0]), str(row[1]), str(row[2]))


def _revoke_rotated_runtime(
    conn: Any, *, schema: str, old_runtime_role: str, runtime_role: str
) -> None:
    if old_runtime_role == runtime_role:
        return
    schema_ident = _quoted(schema, "schema")
    old_ident = _quoted(old_runtime_role, "recorded runtime_role")
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (old_runtime_role,))
        if cur.fetchone() is None:
            return
        cur.execute(
            f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema_ident} FROM {old_ident}"
        )
        cur.execute(
            f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema_ident} FROM {old_ident}"
        )
        cur.execute(f"REVOKE ALL ON SCHEMA {schema_ident} FROM {old_ident}")


def _record_principals(
    conn: Any,
    *,
    schema: str,
    migration_role: str,
    runtime_role: str,
    environment_name: str,
    environment_class: str,
) -> None:
    schema_ident = _quoted(schema, "schema")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {schema_ident}.schema_principal (
                role_kind, role_name, environment_name, environment_class
            ) VALUES
                ('migration', %s, %s, %s),
                ('runtime', %s, %s, %s)
            ON CONFLICT (role_kind) DO UPDATE
            SET role_name = EXCLUDED.role_name,
                environment_name = EXCLUDED.environment_name,
                environment_class = EXCLUDED.environment_class,
                recorded_at = clock_timestamp()
            """,
            (
                migration_role,
                environment_name,
                environment_class,
                runtime_role,
                environment_name,
                environment_class,
            ),
        )


def _configure_runtime_privileges(
    conn: Any,
    *,
    schema: str,
    migration_role: str,
    runtime_role: str,
    installed_version: int,
) -> None:
    schema_ident = _quoted(schema, "schema")
    migration_ident = _quoted(migration_role, "migration_role")
    runtime_ident = _quoted(runtime_role, "runtime_role")
    statements = [
        f"REVOKE ALL ON SCHEMA {schema_ident} FROM PUBLIC",
        f"GRANT USAGE ON SCHEMA {schema_ident} TO {runtime_ident}",
        f"REVOKE CREATE ON SCHEMA {schema_ident} FROM {runtime_ident}",
        f"REVOKE ALL ON {schema_ident}.schema_migration FROM PUBLIC, {runtime_ident}",
        f"GRANT SELECT ON {schema_ident}.schema_migration TO {runtime_ident}",
        (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {migration_ident} IN SCHEMA {schema_ident} "
            "REVOKE ALL ON TABLES FROM PUBLIC"
        ),
        (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {migration_ident} IN SCHEMA {schema_ident} "
            "REVOKE ALL ON SEQUENCES FROM PUBLIC"
        ),
    ]
    if installed_version >= 1:
        statements.extend(
            [
                (
                    f"REVOKE ALL ON {schema_ident}.knowledge_candidate "
                    f"FROM PUBLIC, {runtime_ident}"
                ),
                (
                    f"GRANT SELECT, INSERT ON {schema_ident}.knowledge_candidate "
                    f"TO {runtime_ident}"
                ),
                (
                    f"GRANT UPDATE (summary, detail, tags, confidence, status, "
                    f"content_digest, basis_git_revision) ON "
                    f"{schema_ident}.knowledge_candidate TO {runtime_ident}"
                ),
                (
                    f"REVOKE ALL ON {schema_ident}.knowledge_review "
                    f"FROM PUBLIC, {runtime_ident}"
                ),
                (
                    f"GRANT SELECT, INSERT ON {schema_ident}.knowledge_review "
                    f"TO {runtime_ident}"
                ),
            ]
        )
        statements.append(
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema_ident} TO {runtime_ident}"
        )
    if installed_version >= 2:
        statements.extend(
            [
                (
                    f"REVOKE ALL ON {schema_ident}.knowledge_publication_reference "
                    f"FROM PUBLIC, {runtime_ident}"
                ),
                (
                    f"GRANT SELECT, INSERT ON "
                    f"{schema_ident}.knowledge_publication_reference TO {runtime_ident}"
                ),
                (
                    f"GRANT UPDATE (superseded_by) ON "
                    f"{schema_ident}.knowledge_publication_reference TO {runtime_ident}"
                ),
                f"REVOKE ALL ON {schema_ident}.schema_principal FROM PUBLIC, {runtime_ident}",
                f"GRANT SELECT ON {schema_ident}.schema_principal TO {runtime_ident}",
            ]
        )
    with conn.cursor() as cur:
        for statement in statements:
            cur.execute(statement)


def migrate(
    conn: Any,
    *,
    schema: str,
    migration_role: str,
    runtime_role: str,
    environment_name: str,
    environment_class: str,
    target_version: int = CURRENT_SCHEMA_VERSION,
) -> MigrationResult:
    """Apply serialized deployment migrations with an explicit DDL role."""
    _identifier(schema, "schema")
    _identifier(migration_role, "migration_role")
    _identifier(runtime_role, "runtime_role")
    _environment_name(environment_name)
    _environment_class(environment_class)
    if migration_role == runtime_role:
        raise ValueError("migration_role and runtime_role must be different roles")
    if target_version < 1 or target_version > CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"target_version must be between 1 and {CURRENT_SCHEMA_VERSION}"
        )
    applied_now: list[int] = []
    with conn.transaction():
        current_user = _current_user(conn)
        if current_user != migration_role:
            raise MigrationRoleError(
                f"migration connection role is {current_user!r}, expected {migration_role!r}"
            )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"{MIGRATION_LOCK_NAMESPACE}:{schema}",),
            )
        _bootstrap_ledger(conn, schema)
        applied = _applied_migrations(conn, schema)
        versions = sorted(applied)
        if versions and versions != list(range(1, versions[-1] + 1)):
            raise MigrationDriftError("migration ledger versions are not contiguous")
        if versions and versions[-1] > CURRENT_SCHEMA_VERSION:
            raise MigrationDriftError(
                f"installed schema version {versions[-1]} is newer than this package"
            )
        migrations = load_migrations()
        for migration in migrations:
            recorded = applied.get(migration.version)
            if recorded is not None:
                if recorded != (migration.name, migration.sha256):
                    raise MigrationDriftError(
                        f"migration {migration.version} checksum or name differs from its asset"
                    )
                continue
            if migration.version > target_version:
                break
            schema_ident = _quoted(schema, "schema")
            rendered = render_schema_sql(migration.sql, schema)
            if "__SCHEMA__" in rendered:
                raise CentralSchemaError(
                    f"unresolved schema placeholder in migration {migration.version}"
                )
            with conn.cursor() as cur:
                for statement in _statements(rendered):
                    cur.execute(statement)
                cur.execute(
                    f"""
                    INSERT INTO {schema_ident}.schema_migration (version, name, sha256)
                    VALUES (%s, %s, %s)
                    """,
                    (migration.version, migration.name, migration.sha256),
                )
            applied_now.append(migration.version)

        installed = max(_applied_migrations(conn, schema), default=0)
        if installed >= 2:
            current_runtime = _configured_principal(
                conn, schema=schema, role_kind="runtime"
            )
            if current_runtime is not None:
                if current_runtime[1:] != (environment_name, environment_class):
                    raise EnvironmentBindingError(
                        "refusing to change initialized schema environment binding "
                        f"from {current_runtime[1]!r}/{current_runtime[2]!r} to "
                        f"{environment_name!r}/{environment_class!r}"
                    )
                _revoke_rotated_runtime(
                    conn,
                    schema=schema,
                    old_runtime_role=current_runtime[0],
                    runtime_role=runtime_role,
                )
            _record_principals(
                conn,
                schema=schema,
                migration_role=migration_role,
                runtime_role=runtime_role,
                environment_name=environment_name,
                environment_class=environment_class,
            )
        _configure_runtime_privileges(
            conn,
            schema=schema,
            migration_role=migration_role,
            runtime_role=runtime_role,
            installed_version=installed,
        )
    return MigrationResult(
        schema=schema,
        installed_version=installed,
        applied_versions=tuple(applied_now),
        migration_role=migration_role,
        runtime_role=runtime_role,
        environment_name=environment_name,
        environment_class=environment_class,
    )


def _shape_reasons(conn: Any, schema: str) -> list[str]:
    reasons: list[str] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name, udt_name, is_nullable
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = ANY(%s)
            """,
            (schema, list(_REQUIRED_COLUMNS)),
        )
        rows = cur.fetchall()
    observed: dict[str, dict[str, tuple[str, str]]] = {
        table: {} for table in _REQUIRED_COLUMNS
    }
    for row in rows:
        table = str(row[0] if not isinstance(row, dict) else row["table_name"])
        column = str(row[1] if not isinstance(row, dict) else row["column_name"])
        if table in observed:
            data_type = str(row[2] if not isinstance(row, dict) else row["udt_name"])
            nullable = str(row[3] if not isinstance(row, dict) else row["is_nullable"])
            observed[table][column] = (data_type, nullable)
    for table, required_shape in _COLUMN_SHAPE.items():
        required = set(required_shape)
        if not observed[table]:
            reasons.append(f"relation_missing:{table}")
            continue
        for column in sorted(required - set(observed[table])):
            reasons.append(f"column_missing:{table}.{column}")
        for column in sorted(set(observed[table]) - required):
            reasons.append(f"column_unexpected:{table}.{column}")
        for column in sorted(required & set(observed[table])):
            if observed[table][column] != required_shape[column]:
                reasons.append(f"column_shape:{table}.{column}")
    # Published content remains Git-owned: the central relation must never
    # acquire body/title columns through out-of-band drift.
    for forbidden in ("body", "title", "document_body"):
        if forbidden in observed["knowledge_publication_reference"]:
            reasons.append(
                f"canonical_content_column_forbidden:knowledge_publication_reference.{forbidden}"
            )
    reasons.extend(_constraint_reasons(conn, schema))
    reasons.extend(_index_reasons(conn, schema))
    return reasons


def _constraint_reasons(conn: Any, schema: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT relation.relname AS table_name,
                   constraint_record.conname,
                   constraint_record.contype,
                   ARRAY(
                       SELECT attribute.attname
                       FROM unnest(constraint_record.conkey)
                            WITH ORDINALITY AS key_record(attnum, position)
                       JOIN pg_attribute AS attribute
                         ON attribute.attrelid = constraint_record.conrelid
                        AND attribute.attnum = key_record.attnum
                       ORDER BY key_record.position
                   ) AS columns,
                   foreign_namespace.nspname AS foreign_schema,
                   foreign_relation.relname AS foreign_table,
                   ARRAY(
                       SELECT attribute.attname
                       FROM unnest(constraint_record.confkey)
                            WITH ORDINALITY AS key_record(attnum, position)
                       JOIN pg_attribute AS attribute
                         ON attribute.attrelid = constraint_record.confrelid
                        AND attribute.attnum = key_record.attnum
                       ORDER BY key_record.position
                   ) AS foreign_columns,
                   constraint_record.convalidated,
                   constraint_record.condeferrable,
                   constraint_record.condeferred,
                   pg_get_expr(
                       constraint_record.conbin,
                       constraint_record.conrelid,
                       true
                   ) AS expression
            FROM pg_constraint AS constraint_record
            JOIN pg_class AS relation
              ON relation.oid = constraint_record.conrelid
            JOIN pg_namespace AS namespace_record
              ON namespace_record.oid = relation.relnamespace
            LEFT JOIN pg_class AS foreign_relation
              ON foreign_relation.oid = constraint_record.confrelid
            LEFT JOIN pg_namespace AS foreign_namespace
              ON foreign_namespace.oid = foreign_relation.relnamespace
            WHERE namespace_record.nspname = %s
              AND relation.relname = ANY(%s)
            """,
            (schema, list(_REQUIRED_COLUMNS)),
        )
        rows = cur.fetchall()
    constraints: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict):
            values = row
        else:
            values = {
                "table_name": row[0],
                "conname": row[1],
                "contype": row[2],
                "columns": row[3],
                "foreign_schema": row[4],
                "foreign_table": row[5],
                "foreign_columns": row[6],
                "convalidated": row[7],
                "condeferrable": row[8],
                "condeferred": row[9],
                "expression": row[10],
            }
        constraints[(str(values["table_name"]), str(values["conname"]))] = {
            "type": str(values["contype"]),
            "columns": tuple(values["columns"] or ()),
            "foreign_schema": values["foreign_schema"],
            "foreign_table": values["foreign_table"],
            "foreign_columns": tuple(values["foreign_columns"] or ()),
            "validated": bool(values["convalidated"]),
            "deferrable": bool(values["condeferrable"]),
            "deferred": bool(values["condeferred"]),
            "expression": str(values["expression"] or "").lower(),
        }
    reasons: list[str] = []
    for key, expected in _REQUIRED_STRUCTURAL_CONSTRAINTS.items():
        observed = constraints.get(key)
        if observed is None:
            reasons.append(f"constraint_missing:{key[0]}.{key[1]}")
            continue
        expected_type, columns, foreign_table, foreign_columns = expected
        if (
            observed["type"] != expected_type
            or observed["columns"] != columns
            or not observed["validated"]
            or observed["deferrable"]
            or observed["deferred"]
            or (
                foreign_table is not None
                and (
                    observed["foreign_schema"] != schema
                    or observed["foreign_table"] != foreign_table
                    or observed["foreign_columns"] != foreign_columns
                )
            )
        ):
            reasons.append(f"constraint_shape:{key[0]}.{key[1]}")
    for key, fragments in _REQUIRED_CHECK_FRAGMENTS.items():
        observed = constraints.get(key)
        if observed is None:
            reasons.append(f"constraint_missing:{key[0]}.{key[1]}")
            continue
        expression = observed["expression"]
        if (
            observed["type"] != "c"
            or not observed["validated"]
            or observed["deferrable"]
            or observed["deferred"]
            or any(fragment.lower() not in expression for fragment in fragments)
            or " or true" in expression
        ):
            reasons.append(f"constraint_shape:{key[0]}.{key[1]}")
    return reasons


def _index_reasons(conn: Any, schema: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT index_relation.relname AS index_name,
                   table_relation.relname AS table_name,
                   access_method.amname AS access_method,
                   index_record.indisunique,
                   index_record.indisvalid,
                   index_record.indisready,
                   index_record.indoption::int2[] AS key_options,
                   pg_get_expr(
                       index_record.indpred,
                       index_record.indrelid,
                       true
                   ) AS predicate,
                   ARRAY(
                       SELECT pg_get_indexdef(
                           index_record.indexrelid,
                           position,
                           true
                       )
                       FROM generate_series(
                           1,
                           index_record.indnkeyatts
                       ) AS position
                   ) AS key_definitions
            FROM pg_index AS index_record
            JOIN pg_class AS index_relation
              ON index_relation.oid = index_record.indexrelid
            JOIN pg_class AS table_relation
              ON table_relation.oid = index_record.indrelid
            JOIN pg_namespace AS namespace_record
              ON namespace_record.oid = table_relation.relnamespace
            JOIN pg_am AS access_method
              ON access_method.oid = index_relation.relam
            WHERE namespace_record.nspname = %s
              AND index_relation.relname = ANY(%s)
            """,
            (schema, list(_REQUIRED_INDEXES)),
        )
        rows = cur.fetchall()
    observed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict):
            values = row
        else:
            values = {
                "index_name": row[0],
                "table_name": row[1],
                "access_method": row[2],
                "indisunique": row[3],
                "indisvalid": row[4],
                "indisready": row[5],
                "key_options": row[6],
                "predicate": row[7],
                "key_definitions": row[8],
            }
        observed[str(values["index_name"])] = {
            "table": str(values["table_name"]),
            "method": str(values["access_method"]),
            "unique": bool(values["indisunique"]),
            "valid": bool(values["indisvalid"]),
            "ready": bool(values["indisready"]),
            "descending": tuple(
                bool(int(option) & 1) for option in values["key_options"]
            ),
            "predicate": values["predicate"],
            "keys": tuple(str(key).lower() for key in values["key_definitions"]),
        }
    reasons: list[str] = []
    for name, (table, columns, descending) in _REQUIRED_INDEXES.items():
        index = observed.get(name)
        if index is None:
            reasons.append(f"index_missing:{name}")
            continue
        keys_match = (
            len(index["keys"]) == len(columns)
            and all(
                key == column
                for key, column in zip(index["keys"], columns, strict=True)
            )
            and index["descending"] == descending
        )
        if (
            index["table"] != table
            or index["method"] != "btree"
            or index["unique"]
            or not index["valid"]
            or not index["ready"]
            or index["predicate"] is not None
            or not keys_match
        ):
            reasons.append(f"index_shape:{name}")
    return reasons


def _runtime_authority_reasons(conn: Any, schema: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT principal.rolsuper,
                   namespace.nspowner = principal.oid AS owns_schema,
                   pg_has_role(current_user, namespace.nspowner, 'MEMBER')
                       AS can_assume_schema_owner,
                   has_schema_privilege(current_user, namespace.oid, 'CREATE')
                       AS can_create,
                   COALESCE(bool_or(relation.relowner = principal.oid), false)
                       AS owns_relation,
                   COALESCE(bool_or(
                       relation.relowner IS NOT NULL
                       AND pg_has_role(current_user, relation.relowner, 'MEMBER')
                   ), false) AS can_assume_relation_owner,
                   COALESCE(bool_or(
                       relation.relname = 'schema_migration'
                       AND relation.relkind IN ('r', 'p')
                       AND (
                           has_table_privilege(current_user, relation.oid, 'INSERT')
                           OR has_table_privilege(current_user, relation.oid, 'UPDATE')
                           OR has_table_privilege(current_user, relation.oid, 'DELETE')
                           OR has_table_privilege(current_user, relation.oid, 'TRUNCATE')
                       )
                   ), false) AS can_write_ledger
            FROM pg_namespace AS namespace
            JOIN pg_roles AS principal ON principal.rolname = current_user
            LEFT JOIN pg_class AS relation
              ON relation.relnamespace = namespace.oid
             AND relation.relkind IN ('r', 'p', 'S', 'i', 'I')
            WHERE namespace.nspname = %s
            GROUP BY principal.oid, principal.rolsuper, namespace.oid, namespace.nspowner
            """,
            (schema,),
        )
        row = cur.fetchone()
    if row is None:
        return ["runtime_authority_unknown"]
    if isinstance(row, dict):
        values = [
            row["rolsuper"],
            row["owns_schema"],
            row["can_assume_schema_owner"],
            row["can_create"],
            row["owns_relation"],
            row["can_assume_relation_owner"],
            row["can_write_ledger"],
        ]
    else:
        values = list(row)
    labels = [
        "runtime_is_superuser",
        "runtime_owns_schema",
        "runtime_can_assume_schema_owner",
        "runtime_has_schema_create",
        "runtime_owns_relation",
        "runtime_can_assume_relation_owner",
        "runtime_can_write_migration_ledger",
    ]
    return [
        label for label, present in zip(labels, values, strict=True) if bool(present)
    ]


def check_compatibility(
    conn: Any,
    *,
    schema: str,
    expected_role_kind: str = "runtime",
    expected_environment_name: str | None = None,
    expected_environment_class: str | None = None,
) -> Compatibility:
    """Inspect compatibility using SELECTs only; never bootstrap or migrate."""
    _identifier(schema, "schema")
    if expected_role_kind not in {"runtime", "migration"}:
        raise ValueError("expected_role_kind must be runtime or migration")
    if expected_environment_name is not None:
        _environment_name(expected_environment_name)
    if expected_environment_class is not None:
        _environment_class(expected_environment_class)
    current_user = _current_user(conn)

    def result(
        installed: int | None,
        reasons: list[str],
        configured: tuple[str, str, str] | None = None,
    ) -> Compatibility:
        return Compatibility(
            schema=schema,
            domain_api_version=DOMAIN_API_VERSION,
            installed_schema_version=installed,
            minimum_schema_version=MIN_RUNTIME_SCHEMA_VERSION,
            maximum_schema_version=MAX_RUNTIME_SCHEMA_VERSION,
            current_role=current_user,
            expected_role_kind=expected_role_kind,
            configured_role=configured[0] if configured else None,
            environment_name=configured[1] if configured else None,
            environment_class=configured[2] if configured else None,
            compatible=not reasons,
            reasons=tuple(reasons),
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT has_schema_privilege(current_user, oid, 'USAGE') AS has_usage
            FROM pg_namespace WHERE nspname = %s
            """,
            (schema,),
        )
        namespace = cur.fetchone()
    if namespace is None:
        return result(None, ["schema_not_initialized"])
    has_usage = bool(
        namespace[0] if not isinstance(namespace, dict) else namespace["has_usage"]
    )
    if not has_usage:
        return result(None, ["schema_access_denied"])

    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"{schema}.schema_migration",))
        row = cur.fetchone()
    ledger_exists = (
        row[0] if not isinstance(row, dict) else next(iter(row.values()))
    ) is not None
    if not ledger_exists:
        return result(None, ["schema_not_initialized"])

    reasons: list[str] = []
    try:
        applied = _applied_migrations(conn, schema)
    except Exception:
        return result(None, ["migration_ledger_unreadable"])
    versions = sorted(applied)
    installed = versions[-1] if versions else 0
    if versions != list(range(1, installed + 1)):
        reasons.append("migration_versions_not_contiguous")
    known = {migration.version: migration for migration in load_migrations()}
    for version, recorded in applied.items():
        migration = known.get(version)
        if migration is None:
            if version > CURRENT_SCHEMA_VERSION:
                continue
            reasons.append(f"migration_unknown:{version}")
        elif recorded != (migration.name, migration.sha256):
            reasons.append(f"migration_checksum_mismatch:{version}")
    if installed < MIN_RUNTIME_SCHEMA_VERSION:
        reasons.append("schema_too_old")
    if installed > MAX_RUNTIME_SCHEMA_VERSION:
        reasons.append("schema_too_new")

    configured: tuple[str, str, str] | None = None
    if MIN_RUNTIME_SCHEMA_VERSION <= installed <= MAX_RUNTIME_SCHEMA_VERSION:
        reasons.extend(_shape_reasons(conn, schema))
        if "relation_missing:schema_principal" not in reasons:
            configured = _configured_principal(
                conn, schema=schema, role_kind=expected_role_kind
            )
        if configured is None:
            reasons.append("role_not_configured")
        else:
            if current_user != configured[0]:
                reasons.append("role_kind_mismatch")
            if (
                expected_environment_name is not None
                and configured[1] != expected_environment_name
            ):
                reasons.append("environment_name_mismatch")
            if (
                expected_environment_class is not None
                and configured[2] != expected_environment_class
            ):
                reasons.append("environment_class_mismatch")
        if expected_role_kind == "runtime":
            reasons.extend(_runtime_authority_reasons(conn, schema))
    return result(installed, reasons, configured)


def require_runtime_compatibility(
    conn: Any,
    *,
    schema: str,
    expected_environment_name: str | None = None,
    expected_environment_class: str | None = None,
) -> Compatibility:
    compatibility = check_compatibility(
        conn,
        schema=schema,
        expected_role_kind="runtime",
        expected_environment_name=expected_environment_name,
        expected_environment_class=expected_environment_class,
    )
    if not compatibility.compatible:
        raise SchemaCompatibilityError(
            "central knowledge schema is incompatible: "
            + ", ".join(compatibility.reasons)
        )
    return compatibility


def _connect(dsn: str) -> Any:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - minimal installations
        raise CentralSchemaError(
            "central schema commands require kctl[remote]"
        ) from exc
    return psycopg.connect(dsn, row_factory=dict_row)


def _env_dsn(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise CentralSchemaError(f"{name} is required")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kctl-central-schema")
    commands = parser.add_subparsers(dest="command", required=True)

    migration = commands.add_parser("migrate")
    migration.add_argument("--schema", default="knowledge")
    migration.add_argument("--migration-role", required=True)
    migration.add_argument("--runtime-role", required=True)
    migration.add_argument("--environment-name", required=True)
    migration.add_argument(
        "--environment-class", choices=sorted(ENVIRONMENT_CLASSES), required=True
    )
    migration.add_argument("--target-version", type=int, default=CURRENT_SCHEMA_VERSION)
    migration.add_argument("--dsn-env", default="KCTL_CENTRAL_MIGRATION_DSN")

    check = commands.add_parser("check-compatibility")
    check.add_argument("--schema", default="knowledge")
    check.add_argument(
        "--expected-role-kind", choices=("runtime", "migration"), default="runtime"
    )
    check.add_argument("--expected-environment-name")
    check.add_argument(
        "--expected-environment-class", choices=sorted(ENVIRONMENT_CLASSES)
    )
    check.add_argument("--dsn-env", default="KCTL_CENTRAL_RUNTIME_DSN")

    export = commands.add_parser("export-local")
    export.add_argument("--repo-id", required=True)
    export.add_argument("--git-revision", required=True)
    export.add_argument("--content-path", default="docs/knowledge/knowledge-base.md")
    export.add_argument("--exported-at", required=True)
    export.add_argument("--db-path", type=Path, default=None)
    export.add_argument("--output", type=Path, required=True)

    validate = commands.add_parser("validate-artifact")
    validate.add_argument("artifact", type=Path)

    import_local = commands.add_parser("import-local")
    import_local.add_argument("artifact", type=Path)
    import_local.add_argument("--schema", default="knowledge")
    import_local.add_argument("--expected-environment-name", required=True)
    import_local.add_argument(
        "--expected-environment-class",
        choices=sorted(ENVIRONMENT_CLASSES),
        required=True,
    )
    import_local.add_argument("--dsn-env", default="KCTL_CENTRAL_RUNTIME_DSN")
    return parser


def _read_local_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "export-local":
        db_path = args.db_path or _db.get_db_path()
        with _read_local_connection(db_path) as conn:
            value = transfer.build_artifact(
                conn,
                repo_id=args.repo_id,
                git_revision=args.git_revision,
                content_path=args.content_path,
                exported_at=args.exported_at,
            )
        transfer.write_artifact(args.output, value)
        print(
            json.dumps(
                {
                    "schema_version": transfer.SCHEMA_VERSION,
                    "output": str(args.output),
                    "artifact_digest": value["artifact_digest"],
                    "candidate_count": len(value["candidates"]),
                    "publication_count": len(value["publications"]),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "validate-artifact":
        value = transfer.read_artifact(args.artifact)
        print(
            json.dumps(
                {
                    "schema_version": value["schema_version"],
                    "artifact_digest": value["artifact_digest"],
                    "valid": True,
                },
                sort_keys=True,
            )
        )
        return 0

    dsn = _env_dsn(args.dsn_env)
    with _connect(dsn) as conn:
        if args.command == "migrate":
            result = migrate(
                conn,
                schema=args.schema,
                migration_role=args.migration_role,
                runtime_role=args.runtime_role,
                environment_name=args.environment_name,
                environment_class=args.environment_class,
                target_version=args.target_version,
            )
            print(json.dumps(asdict(result), sort_keys=True))
            return 0
        if args.command == "check-compatibility":
            compatibility = check_compatibility(
                conn,
                schema=args.schema,
                expected_role_kind=args.expected_role_kind,
                expected_environment_name=args.expected_environment_name,
                expected_environment_class=args.expected_environment_class,
            )
            print(json.dumps(compatibility.to_dict(), sort_keys=True))
            return 0 if compatibility.compatible else 2
        if args.command == "import-local":
            require_runtime_compatibility(
                conn,
                schema=args.schema,
                expected_environment_name=args.expected_environment_name,
                expected_environment_class=args.expected_environment_class,
            )
            value = transfer.read_artifact(args.artifact)
            result = transfer.import_artifact(conn, schema=args.schema, value=value)
            print(json.dumps(result, sort_keys=True))
            return 0
    raise AssertionError("unreachable")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
