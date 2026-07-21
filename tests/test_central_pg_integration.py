from __future__ import annotations

import copy
import getpass
import json
import shutil
import socket
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import pytest

from kctl import db
from kctl import publish
from kctl import review
from kctl import transfer
from kctl.central_schema import (
    EnvironmentBindingError,
    MigrationDriftError,
    check_compatibility,
    migrate,
    require_runtime_compatibility,
)


psycopg = pytest.importorskip("psycopg")
from psycopg import errors  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402


MIGRATION_ROLE = "kctl_migration"
RUNTIME_ROLE = "kctl_runtime"
GIT_REVISION = "b" * 40


@pytest.fixture(scope="module")
def postgres_dsn() -> Iterator[str]:
    if not all(shutil.which(command) for command in ("initdb", "pg_ctl")):
        pytest.fail(
            "PostgreSQL server binaries are required for central integration tests"
        )
    root = Path(tempfile.mkdtemp(prefix="kctl-pg-"))
    data = root / "data"
    socket_dir = root / "socket"
    socket_dir.mkdir()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    initdb = Path(shutil.which("initdb") or "initdb").resolve()
    pg_ctl = Path(shutil.which("pg_ctl") or "pg_ctl").resolve()
    initdb_args = [
        str(initdb),
        "--no-locale",
        "--encoding=UTF8",
        "--auth=trust",
        "-D",
        str(data),
    ]
    adjacent_share = initdb.parents[1] / "share" / "postgresql"
    if (adjacent_share / "postgres.bki").exists():
        initdb_args.extend(["-L", str(adjacent_share)])
    subprocess.run(initdb_args, check=True, capture_output=True, text=True)
    subprocess.run(
        [
            str(pg_ctl),
            "-D",
            str(data),
            "-l",
            str(root / "postgres.log"),
            "-o",
            f"-F -h '' -k {socket_dir} -p {port}",
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    admin_dsn = (
        f"dbname=postgres user={getpass.getuser()} host={socket_dir} port={port}"
    )
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(f"CREATE ROLE {MIGRATION_ROLE} LOGIN")
                cur.execute(f"CREATE ROLE {RUNTIME_ROLE} LOGIN")
                cur.execute(f"GRANT CREATE ON DATABASE postgres TO {MIGRATION_ROLE}")
        yield f"dbname=postgres host={socket_dir} port={port}"
    finally:
        subprocess.run(
            [str(pg_ctl), "-D", str(data), "-m", "fast", "-w", "stop"],
            check=False,
            capture_output=True,
            text=True,
        )
        shutil.rmtree(root, ignore_errors=True)


@contextmanager
def _connect(dsn: str, role: str) -> Iterator[Any]:
    with psycopg.connect(
        f"{dsn} user={role}", autocommit=True, row_factory=dict_row
    ) as conn:
        yield conn


def _schema(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:10]}"


def _migrate_current(dsn: str, schema: str, environment: str = "vuoro-dev") -> None:
    with _connect(dsn, MIGRATION_ROLE) as conn:
        result = migrate(
            conn,
            schema=schema,
            migration_role=MIGRATION_ROLE,
            runtime_role=RUNTIME_ROLE,
            environment_name=environment,
            environment_class="development",
        )
    assert result.installed_version == 2


def _artifact(
    tmp_path: Path,
    repo_id: str = "kctl",
    *,
    publication_count: int = 1,
    supersede_first_to: int | None = None,
) -> dict:
    if publication_count < 1:
        raise ValueError("publication_count must be positive")
    if supersede_first_to is not None and not (
        1 <= supersede_first_to < publication_count
    ):
        raise ValueError("supersede_first_to must select a later publication")
    conn = db.get_connection(tmp_path / f"{repo_id}.db")
    db.init_db(conn)
    entries: list[dict] = []
    for index in range(publication_count):
        number = index + 1
        candidate_id = db.insert_candidate(
            conn,
            {
                "source_event_id": number,
                "source_sprint_id": 381,
                "source_item_id": 1199,
                "track_name": "served-substrate",
                "source_actor": "codex:integration",
                "source_type": "actor",
                "source_created_at": f"2026-07-21T11:00:0{index}Z",
                "source_payload": json.dumps(
                    {"summary": f"Central knowledge {number}"}
                ),
                "event_type": "decision",
                "candidate_kind": "durable",
                "summary": f"Central knowledge {number}",
                "detail": f"Preserve Git identity and digest {number}",
                "tags": '["vuoro"]',
                "confidence": "high",
                "extracted_at": f"2026-07-21T11:00:0{index}Z",
            },
        )
        assert candidate_id is not None
        review.approve_candidate(
            conn,
            candidate_id,
            now=f"2026-07-21T11:05:0{index}Z",
            reviewed_by="human:integration",
        )
        supersedes_entry_id = entries[0]["id"] if supersede_first_to == index else None
        entries.append(
            publish.publish_candidate(
                conn,
                candidate_id,
                title=f"Central schema {number}",
                body=f"Git remains canonical, revision {number}.",
                category="decision",
                tags='["vuoro"]',
                supersedes_entry_id=supersedes_entry_id,
                now=f"2026-07-21T11:10:0{index}Z",
            )
        )
    value = transfer.build_artifact(
        conn,
        repo_id=repo_id,
        git_revision=GIT_REVISION,
        content_path="docs/knowledge/knowledge-base.md",
        exported_at="2026-07-21T11:15:00Z",
    )
    conn.close()
    return value


def test_empty_upgrade_retry_and_checksum_drift_fail_closed(postgres_dsn: str) -> None:
    schema = _schema("knowledge_upgrade")
    with _connect(postgres_dsn, MIGRATION_ROLE) as conn:
        first = migrate(
            conn,
            schema=schema,
            migration_role=MIGRATION_ROLE,
            runtime_role=RUNTIME_ROLE,
            environment_name="vuoro-dev",
            environment_class="development",
            target_version=1,
        )
        assert first.applied_versions == (1,)
        old = check_compatibility(conn, schema=schema, expected_role_kind="migration")
        assert not old.compatible
        assert "schema_too_old" in old.reasons
        upgraded = migrate(
            conn,
            schema=schema,
            migration_role=MIGRATION_ROLE,
            runtime_role=RUNTIME_ROLE,
            environment_name="vuoro-dev",
            environment_class="development",
        )
        repeated = migrate(
            conn,
            schema=schema,
            migration_role=MIGRATION_ROLE,
            runtime_role=RUNTIME_ROLE,
            environment_name="vuoro-dev",
            environment_class="development",
        )
        assert upgraded.applied_versions == (2,)
        assert repeated.applied_versions == ()
        with conn.cursor() as cur:
            cur.execute(
                f'UPDATE "{schema}".schema_migration SET sha256 = %s WHERE version = 1',
                ("0" * 64,),
            )
        with pytest.raises(MigrationDriftError, match="checksum"):
            migrate(
                conn,
                schema=schema,
                migration_role=MIGRATION_ROLE,
                runtime_role=RUNTIME_ROLE,
                environment_name="vuoro-dev",
                environment_class="development",
            )


def test_concurrent_migration_jobs_serialize(postgres_dsn: str) -> None:
    schema = _schema("knowledge_parallel")
    barrier = threading.Barrier(2)
    results: list[tuple[int, ...]] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            with _connect(postgres_dsn, MIGRATION_ROLE) as conn:
                barrier.wait()
                result = migrate(
                    conn,
                    schema=schema,
                    migration_role=MIGRATION_ROLE,
                    runtime_role=RUNTIME_ROLE,
                    environment_name="vuoro-dev",
                    environment_class="development",
                )
                results.append(result.applied_versions)
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not failures
    assert sorted(results, key=len) == [(), (1, 2)]


def test_runtime_role_is_compatible_but_cannot_execute_ddl(postgres_dsn: str) -> None:
    schema = _schema("knowledge_roles")
    _migrate_current(postgres_dsn, schema)
    with _connect(postgres_dsn, RUNTIME_ROLE) as conn:
        compatibility = require_runtime_compatibility(
            conn,
            schema=schema,
            expected_environment_name="vuoro-dev",
            expected_environment_class="development",
        )
        assert compatibility.compatible
        with pytest.raises(errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute(f'CREATE TABLE "{schema}".forbidden (id integer)')
        with pytest.raises(errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute(
                    f'UPDATE "{schema}".schema_migration SET name = name WHERE version = 1'
                )


def test_compatibility_rejects_column_constraint_and_index_drift(
    postgres_dsn: str,
) -> None:
    column_schema = _schema("knowledge_column_drift")
    constraint_schema = _schema("knowledge_constraint_drift")
    index_schema = _schema("knowledge_index_drift")
    for schema in (column_schema, constraint_schema, index_schema):
        _migrate_current(postgres_dsn, schema)
    with _connect(postgres_dsn, MIGRATION_ROLE) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f'ALTER TABLE "{column_schema}".knowledge_candidate '
                "ALTER COLUMN content_digest DROP NOT NULL"
            )
            cur.execute(
                f'ALTER TABLE "{constraint_schema}".knowledge_candidate '
                "DROP CONSTRAINT knowledge_candidate_status_check"
            )
            cur.execute(
                f'ALTER TABLE "{constraint_schema}".knowledge_candidate '
                "ADD CONSTRAINT knowledge_candidate_status_check CHECK (true)"
            )
            cur.execute(f'DROP INDEX "{index_schema}".knowledge_candidate_status_idx')
            cur.execute(
                f"CREATE INDEX knowledge_candidate_status_idx ON "
                f'"{index_schema}".knowledge_candidate (repo_id, status)'
            )
    with _connect(postgres_dsn, RUNTIME_ROLE) as conn:
        column = check_compatibility(conn, schema=column_schema)
        constraint = check_compatibility(conn, schema=constraint_schema)
        index = check_compatibility(conn, schema=index_schema)

    assert "column_shape:knowledge_candidate.content_digest" in column.reasons
    assert (
        "constraint_shape:knowledge_candidate.knowledge_candidate_status_check"
        in constraint.reasons
    )
    assert "index_shape:knowledge_candidate_status_idx" in index.reasons


def test_local_import_is_idempotent_and_preserves_only_content_references(
    postgres_dsn: str, tmp_path: Path
) -> None:
    schema = _schema("knowledge_import")
    _migrate_current(postgres_dsn, schema)
    value = _artifact(tmp_path)
    with _connect(postgres_dsn, RUNTIME_ROLE) as conn:
        require_runtime_compatibility(
            conn,
            schema=schema,
            expected_environment_name="vuoro-dev",
            expected_environment_class="development",
        )
        first = transfer.import_artifact(conn, schema=schema, value=value)
        retried = transfer.import_artifact(conn, schema=schema, value=value)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT git_revision, content_digest, content_path, content_anchor
                FROM "{schema}".knowledge_publication_reference
                """
            )
            reference = cur.fetchone()
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = %s
                  AND table_name = 'knowledge_publication_reference'
                """,
                (schema,),
            )
            columns = {row["column_name"] for row in cur.fetchall()}

    assert first["inserted_candidates"] == 1
    assert first["inserted_reviews"] == 1
    assert first["inserted_publications"] == 1
    assert retried["inserted_candidates"] == 0
    assert retried["inserted_reviews"] == 0
    assert retried["inserted_publications"] == 0
    assert reference["git_revision"] == GIT_REVISION
    assert reference["content_digest"] == value["publications"][0]["content_digest"]
    assert {"title", "body", "document_body"}.isdisjoint(columns)


def test_import_retry_rejects_each_changed_persisted_evidence_field(
    postgres_dsn: str, tmp_path: Path
) -> None:
    schema = _schema("knowledge_import_conflict")
    _migrate_current(postgres_dsn, schema)
    original = _artifact(tmp_path, repo_id="kctl-conflict")

    def changed(*fields: str) -> dict:
        value = copy.deepcopy(original)
        if "source.sprint_id" in fields:
            value["candidates"][0]["source"]["sprint_id"] += 1
        if "review.reviewed_by" in fields:
            value["candidates"][0]["review"]["reviewed_by"] = "human:other"
        if "publication.document_id" in fields:
            value["publications"][0]["document_id"] = "kctl-conflict:other-document"
        value["artifact_digest"] = transfer._artifact_digest(value)
        transfer.validate_artifact(value)
        return value

    variants = (
        changed("source.sprint_id"),
        changed("review.reviewed_by"),
        changed("publication.document_id"),
        changed(
            "source.sprint_id",
            "review.reviewed_by",
            "publication.document_id",
        ),
    )
    with _connect(postgres_dsn, RUNTIME_ROLE) as conn:
        transfer.import_artifact(conn, schema=schema, value=original)
        for variant in variants:
            with pytest.raises(transfer.TransferConflictError, match="conflicts"):
                transfer.import_artifact(conn, schema=schema, value=variant)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT candidate.source_sprint_id, review.reviewed_by,
                       publication.document_id
                FROM "{schema}".knowledge_candidate AS candidate
                JOIN "{schema}".knowledge_review AS review
                  ON review.candidate_id = candidate.candidate_id
                JOIN "{schema}".knowledge_publication_reference AS publication
                  ON publication.candidate_id = candidate.candidate_id
                """
            )
            retained = cur.fetchone()

    assert (
        retained["source_sprint_id"] == original["candidates"][0]["source"]["sprint_id"]
    )
    assert retained["reviewed_by"] == original["candidates"][0]["review"]["reviewed_by"]
    assert retained["document_id"] == original["publications"][0]["document_id"]


def test_first_import_establishes_supersession_and_identical_retry_is_noop(
    postgres_dsn: str, tmp_path: Path
) -> None:
    schema = _schema("knowledge_first_supersession")
    _migrate_current(postgres_dsn, schema)
    value = _artifact(
        tmp_path,
        repo_id="kctl-first-supersession",
        publication_count=2,
        supersede_first_to=1,
    )
    with _connect(postgres_dsn, RUNTIME_ROLE) as conn:
        first = transfer.import_artifact(conn, schema=schema, value=value)
        retried = transfer.import_artifact(conn, schema=schema, value=value)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT superseded_by IS NOT NULL AS linked
                FROM "{schema}".knowledge_publication_reference
                WHERE local_entry_id = %s
                """,
                (value["publications"][0]["local_entry_id"],),
            )
            linked = cur.fetchone()["linked"]

    assert linked
    assert first["inserted_publications"] == 2
    assert retried["inserted_publications"] == 0


def test_retry_rejects_every_supersession_change_direction(
    postgres_dsn: str, tmp_path: Path
) -> None:
    null_to_value = _artifact(
        tmp_path,
        repo_id="kctl-null-to-value",
        publication_count=2,
    )
    value_to_null = _artifact(
        tmp_path,
        repo_id="kctl-value-to-null",
        publication_count=2,
        supersede_first_to=1,
    )
    value_to_different = _artifact(
        tmp_path,
        repo_id="kctl-value-to-different",
        publication_count=3,
        supersede_first_to=1,
    )

    cases: list[tuple[str, dict, dict]] = []
    for label, original in (
        ("null_to_value", null_to_value),
        ("value_to_null", value_to_null),
        ("value_to_different", value_to_different),
    ):
        changed = copy.deepcopy(original)
        if label == "null_to_value":
            changed["publications"][0]["superseded_by_local_entry_id"] = changed[
                "publications"
            ][1]["local_entry_id"]
        elif label == "value_to_null":
            changed["publications"][0]["superseded_by_local_entry_id"] = None
        else:
            changed["publications"][0]["superseded_by_local_entry_id"] = changed[
                "publications"
            ][2]["local_entry_id"]
        changed["artifact_digest"] = transfer._artifact_digest(changed)
        transfer.validate_artifact(changed)
        cases.append((label, original, changed))

    for label, original, changed in cases:
        schema = _schema(f"knowledge_{label}")
        _migrate_current(postgres_dsn, schema)
        with _connect(postgres_dsn, RUNTIME_ROLE) as conn:
            transfer.import_artifact(conn, schema=schema, value=original)
            with pytest.raises(
                transfer.TransferConflictError, match="supersession conflicts"
            ):
                transfer.import_artifact(conn, schema=schema, value=changed)
            identical = transfer.import_artifact(conn, schema=schema, value=original)
        assert identical["inserted_publications"] == 0


def test_concurrent_local_import_retries_apply_once(
    postgres_dsn: str, tmp_path: Path
) -> None:
    schema = _schema("knowledge_import_parallel")
    _migrate_current(postgres_dsn, schema)
    value = _artifact(
        tmp_path,
        repo_id="kctl-parallel",
        publication_count=2,
        supersede_first_to=1,
    )
    barrier = threading.Barrier(2)
    results: list[dict] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            with _connect(postgres_dsn, RUNTIME_ROLE) as conn:
                barrier.wait()
                results.append(
                    transfer.import_artifact(conn, schema=schema, value=value)
                )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not failures
    assert sorted(result["inserted_candidates"] for result in results) == [0, 2]
    assert sorted(result["inserted_reviews"] for result in results) == [0, 2]
    assert sorted(result["inserted_publications"] for result in results) == [0, 2]


def test_vuoro_dev_schema_is_isolated_from_another_environment(
    postgres_dsn: str, tmp_path: Path
) -> None:
    dev_schema = _schema("knowledge_dev")
    other_schema = _schema("knowledge_other")
    _migrate_current(postgres_dsn, dev_schema, "vuoro-dev")
    _migrate_current(postgres_dsn, other_schema, "other-dev")
    with _connect(postgres_dsn, MIGRATION_ROLE) as conn:
        with pytest.raises(EnvironmentBindingError, match="refusing to change"):
            migrate(
                conn,
                schema=dev_schema,
                migration_role=MIGRATION_ROLE,
                runtime_role=RUNTIME_ROLE,
                environment_name="production",
                environment_class="production",
            )
    value = _artifact(tmp_path, repo_id="vuoro-isolation")
    with _connect(postgres_dsn, RUNTIME_ROLE) as conn:
        transfer.import_artifact(conn, schema=dev_schema, value=value)
        mismatch = check_compatibility(
            conn,
            schema=other_schema,
            expected_environment_name="vuoro-dev",
            expected_environment_class="development",
        )
        assert not mismatch.compatible
        assert "environment_name_mismatch" in mismatch.reasons
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT count(*) AS count FROM "{dev_schema}".knowledge_candidate'
            )
            assert cur.fetchone()["count"] == 1
            cur.execute(
                f'SELECT count(*) AS count FROM "{other_schema}".knowledge_candidate'
            )
            assert cur.fetchone()["count"] == 0
