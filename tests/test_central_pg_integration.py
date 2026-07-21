from __future__ import annotations

import copy
import getpass
import json
import os
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
from kctl.application import (
    CentralKnowledgeApplication,
    KnowledgeConflictError,
    KnowledgeNotFoundError,
    MutationEvidence,
    StaleBasisError,
)
from kctl.proposal import proposal_digest
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
    assert result.installed_version == 3


def _served_application(dsn: str, schema: str) -> CentralKnowledgeApplication:
    return CentralKnowledgeApplication(
        schema=schema,
        connection_factory=lambda: psycopg.connect(
            f"{dsn} user={RUNTIME_ROLE}", autocommit=True, row_factory=dict_row
        ),
        expected_environment_name="vuoro-dev",
        expected_environment_class="development",
    )


def _evidence(
    basis: str = GIT_REVISION,
    *,
    actor: str = "human:reviewer",
    request_id: str = "request-1",
    key: str = "key-1",
) -> MutationEvidence:
    return MutationEvidence(
        actor=actor,
        environment="vuoro-dev",
        request_id=request_id,
        catalog_revision="catalog-test",
        idempotency_key=key,
        basis_revision=basis,
    )


def _served_candidate(
    local_id: int,
    *,
    repo_id: str = "kctl-served",
    summary: str | None = None,
) -> dict[str, Any]:
    summary = summary or f"Served candidate {local_id}"
    detail = f"Candidate detail {local_id}"
    return {
        "repo_id": repo_id,
        "local_candidate_id": local_id,
        "source_event_id": 10_000 + local_id,
        "source_sprint_id": 381,
        "source_item_id": 1200,
        "source_track": "served-substrate",
        "source_actor": "codex:extractor",
        "source_type": "actor",
        "source_created_at": f"2026-07-21T12:00:{local_id:02d}Z",
        "source_payload": {"summary": summary, "tags": ["vuoro"]},
        "event_type": "decision",
        "candidate_kind": "durable",
        "summary": summary,
        "detail": detail,
        "tags": ["vuoro"],
        "confidence": "high",
        "content_digest": proposal_digest(summary, detail),
        "basis_git_revision": GIT_REVISION,
        "extracted_at": f"2026-07-21T12:01:{local_id:02d}Z",
    }


def _served_publication(
    local_id: int,
    candidate_id: str,
    *,
    repo_id: str = "kctl-served",
    supersedes: str | None = None,
) -> dict[str, Any]:
    return {
        "repo_id": repo_id,
        "local_entry_id": local_id,
        "candidate_id": candidate_id,
        "document_id": f"{repo_id}:knowledge-base",
        "content_path": "docs/knowledge/knowledge-base.md",
        "content_anchor": f"entry-{local_id}",
        "git_revision": GIT_REVISION,
        "content_digest": "sha256:" + f"{local_id:x}"[-1] * 64,
        "category": "decision",
        "source_kind": "durable",
        "tags": ["vuoro"],
        "published_at": f"2026-07-21T12:10:{local_id:02d}Z",
        "supersedes_publication_id": supersedes,
    }


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
        assert upgraded.applied_versions == (2, 3)
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


def test_version_three_upgrade_preserves_existing_graph_as_unknown_inline_evidence(
    postgres_dsn: str,
) -> None:
    schema = _schema("knowledge_inline_upgrade")
    with _connect(postgres_dsn, MIGRATION_ROLE) as conn:
        migrate(
            conn,
            schema=schema,
            migration_role=MIGRATION_ROLE,
            runtime_role=RUNTIME_ROLE,
            environment_name="vuoro-dev",
            environment_class="development",
            target_version=2,
        )
        publication_ids: list[str] = []
        with conn.cursor() as cur:
            for local_id in (1, 2):
                cur.execute(
                    f"""
                    INSERT INTO "{schema}".knowledge_candidate (
                        repo_id, local_candidate_id, source_event_id,
                        source_sprint_id, event_type, candidate_kind, summary,
                        tags, status, content_digest, basis_git_revision,
                        extracted_at
                    ) VALUES (
                        'kctl-upgrade', %s, %s, 381, 'decision', 'durable', %s,
                        '[]'::jsonb, 'published', %s, %s, clock_timestamp()
                    ) RETURNING candidate_id
                    """,
                    (
                        local_id,
                        20_000 + local_id,
                        f"Upgrade candidate {local_id}",
                        "sha256:" + f"{local_id:x}" * 64,
                        GIT_REVISION,
                    ),
                )
                candidate_id = cur.fetchone()["candidate_id"]
                cur.execute(
                    f"""
                    INSERT INTO "{schema}".knowledge_publication_reference (
                        repo_id, local_entry_id, candidate_id, document_id,
                        content_path, content_anchor, git_revision,
                        content_digest, category, source_kind, tags, published_at
                    ) VALUES (
                        'kctl-upgrade', %s, %s, 'kctl-upgrade:knowledge-base',
                        'docs/knowledge/knowledge-base.md', %s, %s, %s,
                        'decision', 'durable', '[]'::jsonb, clock_timestamp()
                    ) RETURNING publication_id::text
                    """,
                    (
                        local_id,
                        candidate_id,
                        f"entry-{local_id}",
                        GIT_REVISION,
                        "sha256:" + f"{local_id + 2:x}" * 64,
                    ),
                )
                publication_ids.append(cur.fetchone()["publication_id"])
            cur.execute(
                f'UPDATE "{schema}".knowledge_publication_reference '
                "SET superseded_by = %s WHERE publication_id = %s",
                (publication_ids[1], publication_ids[0]),
            )
        upgraded = migrate(
            conn,
            schema=schema,
            migration_role=MIGRATION_ROLE,
            runtime_role=RUNTIME_ROLE,
            environment_name="vuoro-dev",
            environment_class="development",
        )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT publication_id::text, superseded_by::text,
                       inline_supersedes::text
                FROM "{schema}".knowledge_publication_reference
                ORDER BY local_entry_id
                """
            )
            rows = cur.fetchall()

    assert upgraded.applied_versions == (3,)
    assert rows[0]["superseded_by"] == publication_ids[1]
    assert rows[1]["superseded_by"] is None
    assert all(row["inline_supersedes"] is None for row in rows)


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
    assert sorted(results, key=len) == [(), (1, 2, 3)]


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


def test_served_candidate_intake_review_retries_and_stale_basis_survive_restart(
    postgres_dsn: str,
) -> None:
    schema = _schema("knowledge_served_review")
    _migrate_current(postgres_dsn, schema)
    application = _served_application(postgres_dsn, schema)
    candidate = _served_candidate(1)

    first = application.intake_candidate(candidate, evidence=_evidence(key="intake-1"))
    replay = application.intake_candidate(
        candidate,
        evidence=_evidence(request_id="intake-retry", key="intake-1"),
    )
    restarted = _served_application(postgres_dsn, schema)
    restart_replay = restarted.intake_candidate(
        candidate,
        evidence=_evidence(request_id="after-restart", key="intake-1"),
    )

    assert first["replayed"] is False
    assert replay["replayed"] is True
    assert restart_replay["replayed"] is True
    assert replay["candidate"]["candidate_id"] == first["candidate"]["candidate_id"]
    assert restart_replay["evidence_ref"] == first["evidence_ref"]

    changed = copy.deepcopy(candidate)
    changed["summary"] = "changed on retry"
    changed["content_digest"] = proposal_digest(changed["summary"], changed["detail"])
    with pytest.raises(KnowledgeConflictError, match="immutable evidence"):
        application.intake_candidate(changed, evidence=_evidence(key="intake-1"))

    duplicate_source = _served_candidate(2)
    duplicate_source["source_event_id"] = candidate["source_event_id"]
    with pytest.raises(KnowledgeConflictError, match="identity|identities"):
        application.intake_candidate(
            duplicate_source, evidence=_evidence(key="intake-2")
        )

    candidate_id = first["candidate"]["candidate_id"]
    with pytest.raises(StaleBasisError, match="does not match"):
        application.approve_candidate(
            candidate_id=candidate_id,
            notes="reviewed",
            evidence=_evidence("a" * 40, key="approve-stale"),
        )
    approved = application.approve_candidate(
        candidate_id=candidate_id,
        notes="reviewed",
        evidence=_evidence(key="approve-1"),
    )
    approved_replay = restarted.approve_candidate(
        candidate_id=candidate_id,
        notes="reviewed",
        evidence=_evidence(request_id="approve-retry", key="approve-1"),
    )
    assert approved["candidate"]["status"] == "approved"
    assert approved["review"]["reviewed_by"] == "human:reviewer"
    assert approved["review"]["content_digest"] == candidate["content_digest"]
    assert approved_replay["replayed"] is True
    assert approved_replay["evidence_ref"] == approved["evidence_ref"]
    with pytest.raises(KnowledgeConflictError, match="review evidence"):
        restarted.approve_candidate(
            candidate_id=candidate_id,
            notes="changed review",
            evidence=_evidence(key="approve-1"),
        )

    rejected_source = _served_candidate(3)
    rejected = application.intake_candidate(
        rejected_source, evidence=_evidence(key="intake-3")
    )
    rejected_result = application.reject_candidate(
        candidate_id=rejected["candidate"]["candidate_id"],
        reason="not durable",
        evidence=_evidence(key="reject-3"),
    )
    assert rejected_result["candidate"]["status"] == "rejected"
    with pytest.raises(KnowledgeConflictError, match="review evidence"):
        application.approve_candidate(
            candidate_id=rejected["candidate"]["candidate_id"],
            notes=None,
            evidence=_evidence(key="approve-3"),
        )

    bounded = application.list_candidates(repo_id="kctl-served", limit=1)
    shown = restarted.show_candidate(candidate_id=candidate_id)
    assert bounded["count"] == 1
    assert len(bounded["candidates"]) == 1
    assert shown["candidate"]["candidate_id"] == candidate_id
    assert shown["review"]["decision"] == "approved"


def test_served_publication_references_supersession_and_changed_retries(
    postgres_dsn: str,
) -> None:
    schema = _schema("knowledge_served_publication")
    _migrate_current(postgres_dsn, schema)
    application = _served_application(postgres_dsn, schema)
    publications: list[dict[str, Any]] = []

    for local_id in (1, 2, 3):
        intake = application.intake_candidate(
            _served_candidate(local_id),
            evidence=_evidence(key=f"intake-{local_id}"),
        )
        candidate_id = intake["candidate"]["candidate_id"]
        application.approve_candidate(
            candidate_id=candidate_id,
            evidence=_evidence(key=f"approve-{local_id}"),
        )
        supersedes = (
            publications[0]["publication"]["publication_id"] if local_id == 2 else None
        )
        publication = _served_publication(local_id, candidate_id, supersedes=supersedes)
        publications.append(
            application.record_publication_reference(
                publication,
                evidence=_evidence(key=f"publish-{local_id}"),
            )
        )

    first, second, third = publications
    assert first["candidate"]["status"] == "published"
    assert second["publication"]["git_revision"] == GIT_REVISION
    assert set(second["publication"]).isdisjoint({"title", "body", "document_body"})

    second_input = _served_publication(
        2,
        second["candidate"]["candidate_id"],
        supersedes=first["publication"]["publication_id"],
    )
    restarted = _served_application(postgres_dsn, schema)
    second_replay = restarted.record_publication_reference(
        second_input,
        evidence=_evidence(request_id="publish-retry", key="publish-2"),
    )
    assert second_replay["replayed"] is True
    assert second_replay["evidence_ref"] == second["evidence_ref"]

    changed = copy.deepcopy(second_input)
    changed["content_digest"] = "sha256:" + "f" * 64
    with pytest.raises(KnowledgeConflictError, match="immutable evidence"):
        restarted.record_publication_reference(
            changed, evidence=_evidence(key="publish-2")
        )
    changed_supersession = copy.deepcopy(second_input)
    changed_supersession["supersedes_publication_id"] = None
    with pytest.raises(KnowledgeConflictError, match="immutable evidence"):
        restarted.record_publication_reference(
            changed_supersession, evidence=_evidence(key="publish-2")
        )
    with pytest.raises(StaleBasisError, match="does not match"):
        restarted.record_publication_reference(
            _served_publication(4, second["candidate"]["candidate_id"]),
            evidence=_evidence("a" * 40, key="publish-stale"),
        )

    linked = restarted.supersede_publication(
        predecessor_id=second["publication"]["publication_id"],
        successor_id=third["publication"]["publication_id"],
        evidence=_evidence(key="supersede-2-3"),
    )
    linked_replay = application.supersede_publication(
        predecessor_id=second["publication"]["publication_id"],
        successor_id=third["publication"]["publication_id"],
        evidence=_evidence(request_id="supersede-retry", key="supersede-2-3"),
    )
    assert (
        linked["predecessor"]["superseded_by"] == third["publication"]["publication_id"]
    )
    assert linked_replay["replayed"] is True
    with pytest.raises(KnowledgeConflictError, match="different successor"):
        application.supersede_publication(
            predecessor_id=first["publication"]["publication_id"],
            successor_id=third["publication"]["publication_id"],
            evidence=_evidence(key="supersede-conflict"),
        )

    bounded = application.list_publications(repo_id="kctl-served", limit=2)
    shown = restarted.show_publication(
        publication_id=third["publication"]["publication_id"]
    )
    assert bounded["count"] == 2
    assert len(bounded["publications"]) == 2
    assert (
        shown["publication"]["publication_id"] == third["publication"]["publication_id"]
    )


def test_publication_creation_evidence_is_independent_of_later_explicit_edges(
    postgres_dsn: str,
) -> None:
    schema = _schema("knowledge_creation_evidence")
    _migrate_current(postgres_dsn, schema)
    application = _served_application(postgres_dsn, schema)

    def create(local_id: int, *, repo_id: str = "kctl-retry") -> dict[str, Any]:
        candidate = _served_candidate(local_id, repo_id=repo_id)
        intake = application.intake_candidate(
            candidate, evidence=_evidence(key=f"intake-{repo_id}-{local_id}")
        )
        candidate_id = intake["candidate"]["candidate_id"]
        application.approve_candidate(
            candidate_id=candidate_id,
            evidence=_evidence(key=f"approve-{repo_id}-{local_id}"),
        )
        request = _served_publication(local_id, candidate_id, repo_id=repo_id)
        result = application.record_publication_reference(
            request, evidence=_evidence(key=f"publish-{repo_id}-{local_id}")
        )
        return {"request": request, "result": result}

    first = create(1)
    second = create(2)
    third = create(3)
    fourth_candidate = _served_candidate(4, repo_id="kctl-retry")
    fourth_intake = application.intake_candidate(
        fourth_candidate, evidence=_evidence(key="intake-kctl-retry-4")
    )
    application.approve_candidate(
        candidate_id=fourth_intake["candidate"]["candidate_id"],
        evidence=_evidence(key="approve-kctl-retry-4"),
    )
    fourth_request = _served_publication(
        4,
        fourth_intake["candidate"]["candidate_id"],
        repo_id="kctl-retry",
        supersedes=third["result"]["publication"]["publication_id"],
    )
    fourth = application.record_publication_reference(
        fourth_request, evidence=_evidence(key="publish-kctl-retry-4")
    )

    application.supersede_publication(
        predecessor_id=first["result"]["publication"]["publication_id"],
        successor_id=second["result"]["publication"]["publication_id"],
        evidence=_evidence(key="explicit-1-2"),
    )
    restarted = _served_application(postgres_dsn, schema)
    no_inline_replay = restarted.record_publication_reference(
        second["request"], evidence=_evidence(key="publish-kctl-retry-2")
    )
    inline_replay = restarted.record_publication_reference(
        fourth_request, evidence=_evidence(key="publish-kctl-retry-4")
    )
    assert no_inline_replay["replayed"] is True
    assert no_inline_replay["publication"]["inline_supersedes"] is None
    assert inline_replay["replayed"] is True
    assert (
        inline_replay["publication"]["inline_supersedes"]
        == third["result"]["publication"]["publication_id"]
    )

    changed_inline = copy.deepcopy(fourth_request)
    changed_inline["supersedes_publication_id"] = first["result"]["publication"][
        "publication_id"
    ]
    with pytest.raises(KnowledgeConflictError, match="immutable evidence"):
        restarted.record_publication_reference(
            changed_inline, evidence=_evidence(key="publish-kctl-retry-4")
        )

    application.supersede_publication(
        predecessor_id=second["result"]["publication"]["publication_id"],
        successor_id=fourth["publication"]["publication_id"],
        evidence=_evidence(key="explicit-2-4"),
    )
    unrelated_edge_replay = _served_application(
        postgres_dsn, schema
    ).record_publication_reference(
        fourth_request, evidence=_evidence(key="publish-kctl-retry-4")
    )
    assert unrelated_edge_replay["replayed"] is True
    assert (
        unrelated_edge_replay["publication"]["inline_supersedes"]
        == third["result"]["publication"]["publication_id"]
    )


def test_explicit_supersession_rejects_missing_cross_repo_and_cycles(
    postgres_dsn: str,
) -> None:
    schema = _schema("knowledge_supersession_guards")
    _migrate_current(postgres_dsn, schema)
    application = _served_application(postgres_dsn, schema)

    def create(local_id: int, repo_id: str) -> str:
        intake = application.intake_candidate(
            _served_candidate(local_id, repo_id=repo_id),
            evidence=_evidence(key=f"intake-{repo_id}-{local_id}"),
        )
        candidate_id = intake["candidate"]["candidate_id"]
        application.approve_candidate(
            candidate_id=candidate_id,
            evidence=_evidence(key=f"approve-{repo_id}-{local_id}"),
        )
        result = application.record_publication_reference(
            _served_publication(local_id, candidate_id, repo_id=repo_id),
            evidence=_evidence(key=f"publish-{repo_id}-{local_id}"),
        )
        return result["publication"]["publication_id"]

    first = create(1, "kctl-guards")
    second = create(2, "kctl-guards")
    other_repo = create(1, "other-guards")
    pending = application.intake_candidate(
        _served_candidate(3, repo_id="kctl-guards"),
        evidence=_evidence(key="intake-kctl-guards-3"),
    )
    pending_id = pending["candidate"]["candidate_id"]
    application.approve_candidate(
        candidate_id=pending_id,
        evidence=_evidence(key="approve-kctl-guards-3"),
    )
    missing_inline = _served_publication(3, pending_id, repo_id="kctl-guards")
    missing_inline["supersedes_publication_id"] = "00000000-0000-4000-8000-000000000002"
    with pytest.raises(KnowledgeNotFoundError, match="not found"):
        application.record_publication_reference(
            missing_inline, evidence=_evidence(key="missing-inline")
        )
    cross_repo_inline = copy.deepcopy(missing_inline)
    cross_repo_inline["supersedes_publication_id"] = other_repo
    with pytest.raises(KnowledgeConflictError, match="one repository"):
        application.record_publication_reference(
            cross_repo_inline, evidence=_evidence(key="cross-repo-inline")
        )
    with pytest.raises(KnowledgeNotFoundError, match="not found"):
        application.supersede_publication(
            predecessor_id="00000000-0000-4000-8000-000000000001",
            successor_id=second,
            evidence=_evidence(key="missing-predecessor"),
        )
    with pytest.raises(KnowledgeConflictError, match="one repository"):
        application.supersede_publication(
            predecessor_id=first,
            successor_id=other_repo,
            evidence=_evidence(key="cross-repo"),
        )
    application.supersede_publication(
        predecessor_id=first,
        successor_id=second,
        evidence=_evidence(key="first-second"),
    )
    with pytest.raises(KnowledgeConflictError, match="cycle"):
        application.supersede_publication(
            predecessor_id=second,
            successor_id=first,
            evidence=_evidence(key="cycle"),
        )


if os.environ.get("KCTL_REAL_VUORO_TESTS") == "1":

    def test_real_vuoro_postgres_restart_preserves_publication_creation_evidence(
        postgres_dsn: str,
    ) -> None:
        """Run only in the explicitly composed Vuoro+kctl verification environment."""
        import asyncio

        import httpx
        from vuoro_client import AsyncVuoroClient, Profile
        from vuoro_client.errors import InvocationRejectedError
        from vuoro_service.app import ServiceSettings, create_app
        from vuoro_service.catalog import CatalogRegistry
        from vuoro_service.contracts import DomainCompatibility
        from vuoro_service.identity import Identity, StaticBearerIdentityResolver

        from kctl.vuoro import VuoroKnowledgeAdapter

        schema = _schema("knowledge_real_vuoro_restart")
        _migrate_current(postgres_dsn, schema)
        repo_id = "kctl-real-restart"
        publication_requests: dict[int, dict[str, Any]] = {}
        publications: dict[int, dict[str, Any]] = {}

        async def invoke(
            operation: str,
            arguments: dict[str, Any],
            *,
            key: str,
            request_id: str,
        ) -> dict[str, Any]:
            # Recreate every layer so retries cannot depend on process memory.
            application = _served_application(postgres_dsn, schema)
            registry = CatalogRegistry()
            VuoroKnowledgeAdapter(application).register(registry)
            service = create_app(
                settings=ServiceSettings(
                    environment_name="vuoro-dev",
                    environment_class="development",
                    compatibility_state="compatible",
                    domains={
                        "knowledge": DomainCompatibility(
                            api_version="knowledge/v1",
                            schema_version="3",
                            state="compatible",
                        )
                    },
                ),
                registry=registry,
                identity_resolver=StaticBearerIdentityResolver(
                    {
                        "token": Identity(
                            actor="human:reviewer",
                            environment="vuoro-dev",
                            authorities=frozenset(
                                {
                                    "knowledge.candidate.intake",
                                    "knowledge.review",
                                    "knowledge.publication-reference.write",
                                    "knowledge.read",
                                }
                            ),
                        )
                    }
                ),
            )
            async with AsyncVuoroClient(
                Profile(
                    "restart-test",
                    "http://test",
                    "identity",
                    expected_environment="vuoro-dev",
                ),
                lambda _reference: "token",
                transport=httpx.ASGITransport(app=service),
            ) as client:
                return await client.invoke(
                    operation,
                    arguments,
                    request_id=request_id,
                    basis_revision=GIT_REVISION,
                    idempotency_key=key,
                )

        async def create(local_id: int, *, inline: int | None = None) -> None:
            candidate = _served_candidate(local_id, repo_id=repo_id)
            intake = await invoke(
                "knowledge.candidate.intake",
                {"candidate": candidate},
                key=f"intake-{local_id}",
                request_id=f"intake-{local_id}",
            )
            candidate_id = intake["candidate"]["candidate_id"]
            await invoke(
                "knowledge.candidate.approve",
                {"candidate_id": candidate_id},
                key=f"approve-{local_id}",
                request_id=f"approve-{local_id}",
            )
            inline_id = (
                publications[inline]["publication_id"] if inline is not None else None
            )
            request = _served_publication(
                local_id,
                candidate_id,
                repo_id=repo_id,
                supersedes=inline_id,
            )
            publication_requests[local_id] = request
            result = await invoke(
                "knowledge.publication-reference.record",
                {"publication": request},
                key=f"publish-{local_id}",
                request_id=f"publish-{local_id}",
            )
            publications[local_id] = result["publication"]

        async def history() -> None:
            await create(1)
            await create(2)
            await invoke(
                "knowledge.publication-reference.supersede",
                {
                    "predecessor_id": publications[1]["publication_id"],
                    "successor_id": publications[2]["publication_id"],
                },
                key="explicit-1-2",
                request_id="explicit-1-2",
            )
            no_inline_replay = await invoke(
                "knowledge.publication-reference.record",
                {"publication": publication_requests[2]},
                key="publish-2",
                request_id="publish-2-after-restart",
            )
            assert no_inline_replay["replayed"] is True
            assert no_inline_replay["publication"]["inline_supersedes"] is None

            await create(3)
            await create(4, inline=3)
            inline_replay = await invoke(
                "knowledge.publication-reference.record",
                {"publication": publication_requests[4]},
                key="publish-4",
                request_id="publish-4-after-restart",
            )
            assert inline_replay["replayed"] is True
            assert (
                inline_replay["publication"]["inline_supersedes"]
                == publications[3]["publication_id"]
            )

            changed = copy.deepcopy(publication_requests[4])
            changed["supersedes_publication_id"] = publications[1]["publication_id"]
            with pytest.raises(InvocationRejectedError) as conflict:
                await invoke(
                    "knowledge.publication-reference.record",
                    {"publication": changed},
                    key="publish-4",
                    request_id="publish-4-changed-inline",
                )
            assert conflict.value.code == "knowledge-evidence-conflict"

            await invoke(
                "knowledge.publication-reference.supersede",
                {
                    "predecessor_id": publications[2]["publication_id"],
                    "successor_id": publications[4]["publication_id"],
                },
                key="explicit-2-4",
                request_id="explicit-2-4",
            )
            later_edge_replay = await invoke(
                "knowledge.publication-reference.record",
                {"publication": publication_requests[4]},
                key="publish-4",
                request_id="publish-4-after-unrelated-edge",
            )
            assert later_edge_replay["replayed"] is True
            assert (
                later_edge_replay["publication"]["inline_supersedes"]
                == publications[3]["publication_id"]
            )

        asyncio.run(history())
