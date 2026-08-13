from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from kctl import db
from kctl import publish
from kctl import review
from kctl import transfer
from kctl.central_schema import (
    CURRENT_SCHEMA_VERSION,
    Migration,
    _REQUIRED_COLUMNS,
    load_migrations,
    migrate,
)
from kctl.central_migrations import MIGRATION_ASSETS
from vuoro_schema_runtime import MigrationAsset, render_schema_sql


GIT_REVISION = "a" * 40
NOW1 = "2026-07-21T10:00:00Z"
NOW2 = "2026-07-21T10:05:00Z"
NOW3 = "2026-07-21T10:10:00Z"


def _candidate(event_id: int, summary: str) -> dict:
    return {
        "source_event_id": event_id,
        "source_sprint_id": 381,
        "source_item_id": 1199,
        "track_name": "served-substrate",
        "source_actor": "codex:test",
        "source_type": "actor",
        "source_created_at": NOW1,
        "source_payload": json.dumps({"summary": summary}),
        "event_type": "decision",
        "candidate_kind": "durable",
        "summary": summary,
        "detail": f"Detail for {summary}",
        "tags": '["vuoro","migration"]',
        "confidence": "high",
        "extracted_at": NOW1,
    }


def _local_store(path: Path) -> sqlite3.Connection:
    conn = db.get_connection(path)
    db.init_db(conn)
    old_id = db.insert_candidate(conn, _candidate(1001, "Old knowledge"))
    new_id = db.insert_candidate(conn, _candidate(1002, "New knowledge"))
    assert old_id is not None and new_id is not None
    review.approve_candidate(conn, old_id, now=NOW2, reviewed_by="human:reviewer")
    old_entry = publish.publish_candidate(
        conn,
        old_id,
        title="Old title",
        body="Old body",
        category="decision",
        tags='["vuoro"]',
        now=NOW3,
    )
    review.approve_candidate(conn, new_id, now=NOW2, reviewed_by="human:reviewer")
    publish.publish_candidate(
        conn,
        new_id,
        title="New title",
        body="New body",
        category="decision",
        tags='["vuoro"]',
        supersedes_entry_id=old_entry["id"],
        now="2026-07-21T10:15:00Z",
    )
    pending_id = db.insert_candidate(conn, _candidate(1003, "Pending knowledge"))
    assert pending_id is not None
    return conn


def test_migration_assets_are_contiguous_and_publications_store_references_only() -> (
    None
):
    migrations = load_migrations()

    assert [migration.version for migration in migrations] == list(
        range(1, CURRENT_SCHEMA_VERSION + 1)
    )
    assert all(len(migration.sha256) == 64 for migration in migrations)
    assert "knowledge_candidate" in migrations[0].sql
    assert "knowledge_publication_reference" in migrations[1].sql
    assert "inline_supersedes" in migrations[2].sql
    assert "title" not in _REQUIRED_COLUMNS["knowledge_publication_reference"]
    assert "body" not in _REQUIRED_COLUMNS["knowledge_publication_reference"]
    assert "inline_supersedes" in _REQUIRED_COLUMNS[
        "knowledge_publication_reference"
    ]


def test_shared_runtime_preserves_every_migration_asset_byte_for_byte() -> None:
    migrations = load_migrations()

    assert Migration is MigrationAsset
    assert all(isinstance(migration, MigrationAsset) for migration in migrations)
    assert len(migrations) == len(MIGRATION_ASSETS)
    for version, (migration, (name, sql)) in enumerate(
        zip(migrations, MIGRATION_ASSETS, strict=True), start=1
    ):
        assert migration.version == version
        assert migration.name.encode("utf-8") == name.encode("utf-8")
        assert migration.sql.encode("utf-8") == sql.encode("utf-8")
        assert migration.sha256 == hashlib.sha256(sql.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("schema", ["knowledge", "vuoro_dev_knowledge", "a"])
def test_shared_runtime_rendering_is_byte_equivalent_to_local_substitution(
    schema: str,
) -> None:
    for migration in load_migrations():
        expected = migration.sql.replace("__SCHEMA__", f'"{schema}"')
        actual = render_schema_sql(migration.sql, schema)

        assert actual.encode("utf-8") == expected.encode("utf-8")
        assert "__SCHEMA__" not in actual


def test_local_export_round_trips_digests_git_identity_and_supersession(
    tmp_path: Path,
) -> None:
    conn = _local_store(tmp_path / "kctl.db")
    try:
        value = transfer.build_artifact(
            conn,
            repo_id="kctl",
            git_revision=GIT_REVISION,
            content_path="docs/knowledge/knowledge-base.md",
            exported_at="2026-07-21T10:20:00Z",
        )
    finally:
        conn.close()

    destination = tmp_path / "knowledge-transfer.json"
    transfer.write_artifact(destination, value)
    loaded = transfer.read_artifact(destination)

    assert loaded == value
    assert destination.stat().st_mode & 0o777 == 0o600
    assert len(value["candidates"]) == 3
    assert len(value["publications"]) == 2
    assert all(
        candidate["basis_git_revision"] == GIT_REVISION
        for candidate in value["candidates"]
    )
    old, new = value["publications"]
    assert old["superseded_by_local_entry_id"] == new["local_entry_id"]
    assert all(
        publication["git_revision"] == GIT_REVISION
        for publication in value["publications"]
    )
    assert value["artifact_digest"].startswith("sha256:")


def test_transfer_validation_rejects_content_tampering_and_reference_escape(
    tmp_path: Path,
) -> None:
    conn = _local_store(tmp_path / "kctl.db")
    try:
        value = transfer.build_artifact(
            conn,
            repo_id="kctl",
            git_revision=GIT_REVISION,
            content_path="docs/knowledge/knowledge-base.md",
            exported_at="2026-07-21T10:20:00Z",
        )
    finally:
        conn.close()

    tampered = copy.deepcopy(value)
    tampered["publications"][0]["body"] = "Changed without updating its digest"
    tampered["artifact_digest"] = transfer._artifact_digest(tampered)
    with pytest.raises(transfer.TransferArtifactError, match="content_digest"):
        transfer.validate_artifact(tampered)

    escaped = copy.deepcopy(value)
    escaped["publications"][0]["content_path"] = "../outside.md"
    escaped["artifact_digest"] = transfer._artifact_digest(escaped)
    with pytest.raises(transfer.TransferArtifactError, match="repository-relative"):
        transfer.validate_artifact(escaped)


def test_transfer_validation_rejects_missing_and_cyclic_supersession(
    tmp_path: Path,
) -> None:
    conn = _local_store(tmp_path / "kctl.db")
    try:
        value = transfer.build_artifact(
            conn,
            repo_id="kctl",
            git_revision=GIT_REVISION,
            content_path="docs/knowledge/knowledge-base.md",
            exported_at="2026-07-21T10:20:00Z",
        )
    finally:
        conn.close()

    missing = copy.deepcopy(value)
    missing["publications"][0]["superseded_by_local_entry_id"] = 9999
    missing["artifact_digest"] = transfer._artifact_digest(missing)
    with pytest.raises(transfer.TransferArtifactError, match="missing supersession"):
        transfer.validate_artifact(missing)

    cyclic = copy.deepcopy(value)
    first, second = cyclic["publications"]
    second["superseded_by_local_entry_id"] = first["local_entry_id"]
    cyclic["artifact_digest"] = transfer._artifact_digest(cyclic)
    with pytest.raises(transfer.TransferArtifactError, match="cycle"):
        transfer.validate_artifact(cyclic)


def test_migration_and_runtime_roles_must_be_distinct() -> None:
    with pytest.raises(ValueError, match="must be different"):
        migrate(
            None,
            schema="knowledge",
            migration_role="same_role",
            runtime_role="same_role",
            environment_name="vuoro-dev",
            environment_class="development",
        )
