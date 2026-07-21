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
from kctl.application import (
    CentralKnowledgeApplication,
    KnowledgeConflictError,
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
    assert result.installed_version == 2


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
    with pytest.raises(KnowledgeConflictError, match="omitted"):
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
